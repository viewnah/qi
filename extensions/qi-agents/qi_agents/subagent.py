"""`subagent` 工具:把一个任务交给另一个角色。

这是 E12 定下的形态 —— **进程内受管子运行**(`api.runAgent`),不是子进程:
Python 每进程首调 LLM 要付 ~6.8s 的 litellm import,子进程等于 6.8s × N(8 并行 ≈ 54s),
而 Node 起一次只要 0.04s。pi 的子进程模型不能平移到这里。

三种模式(照搬 pi 官方 subagent 扩展的已验证形状):

    single    {agent, task}
    parallel  {tasks: [{agent, task}, …]}      并发 4,最多 8 个任务
    chain     {chain: [{agent, task}, …]}      顺序,`{previous}` 占位符传上一棒输出

**递归防护是结构性的**:子运行的工具清单里**去掉 `subagent` 本身**,所以子 agent 不可能
再起子 agent —— 比"记一个深度计数"更难写错(那种要靠每层都记得加一)。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from qi_agent.extensions import Tool

from .discovery import AgentScope, Role, discover, format_roles

# E25:角色自带的 MCP 走 qi-mcp —— 本扩展读角色目录的 mcp.json,按值交给它。
# 模块级 import(不是函数内延迟)有两层原因:①便于在测试里替换掉它来验接线;
# ②装了 qi-mcp 才有 MCP,开发/裁剪环境里没有时**降级**成“角色照跑、只是没有 MCP 工具”,
# 而不是整套角色系统 import 不进来。
try:
    from qi_mcp import register_role_mcp
except ImportError:          # pragma: no cover - 取决于环境里有没有装 qi-mcp
    register_role_mcp = None

MAX_PARALLEL_TASKS = 8
MAX_CONCURRENCY = 4
PER_TASK_CAP = 50_000          # 单个子结果进文本前截断(与工具输出同档)

_SCHEMA = {
    "type": "object",
    "properties": {
        "agent": {"type": "string", "description": "single 模式:角色名"},
        "task": {"type": "string", "description": "single 模式:任务"},
        "tasks": {"type": "array", "description": "parallel 模式:[{agent, task}]",
                  "items": {"type": "object", "properties": {
                      "agent": {"type": "string"}, "task": {"type": "string"}},
                      "required": ["agent", "task"]}},
        "chain": {"type": "array", "description": "chain 模式:[{agent, task}],task 里可用 {previous}",
                  "items": {"type": "object", "properties": {
                      "agent": {"type": "string"}, "task": {"type": "string"}},
                      "required": ["agent", "task"]}},
        "agentScope": {"type": "string", "enum": ["user", "project", "both"],
                       "description": '看哪一层的角色。默认 "user"(项目角色是仓库控制的提示词)'},
        "confirmProjectAgents": {"type": "boolean",
                                 "description": "用项目角色前先问一句(默认 true)",
                                 "default": True},
    },
}


def _clip(text: str) -> str:
    return text if len(text) <= PER_TASK_CAP else text[:PER_TASK_CAP] + "\n…(已截断)"


def _tasks_of(args: dict) -> list[dict]:
    """三种模式归一成任务列表(parallel 与 chain 的差别只在是否传 `{previous}`)。"""
    if isinstance(args.get("tasks"), list):
        return [t for t in args["tasks"] if isinstance(t, dict)]
    if isinstance(args.get("chain"), list):
        return [t for t in args["chain"] if isinstance(t, dict)]
    if args.get("agent") and args.get("task"):
        return [{"agent": args["agent"], "task": args["task"]}]
    return []


def _resolve(roles: dict[str, Role], name: object) -> Role | None:
    """按名字找角色。入参收 `object` —— 它来自 `args`(模型给的 JSON),什么都可能是。"""
    return roles.get(str(name or "").strip())


def _require(roles: dict[str, Role], name: object) -> Role:
    """调用前已校验过“没有未知角色”,这里是给类型检查器的显式收窄。"""
    role = _resolve(roles, name)
    if role is None:                      # 到不了:调用点已经排除了这种情形
        raise KeyError(str(name))
    return role


async def _role_mcp_names(api: Any, role: Role, ctx: Any,
                          role_tools: list[str] | None) -> list[str]:
    """把该角色的 MCP 工具拿到手(名字),交给 qi-mcp 去注册。

    - 角色 `tools:` 里没有 MCP 条目 → **一行 MCP 工作都不做**(qi-mcp 自己也这么判);
    - qi-mcp 不在 → `[]`(降级);
    - 坏掉的 mcp.json / 连不上的 server → 记 note(走 ctx.ui,它无前端时本来就写 notes),
      不让整个子运行失败。
    """
    if register_role_mcp is None or not role_tools:
        return []
    ui = getattr(ctx, "ui", None)
    return await register_role_mcp(
        api,
        role_dir=role.path.parent if role.path else None,
        cwd=getattr(ctx, "workdir", None),
        role_tools=list(role_tools),
        on_note=(ui.notify if ui is not None else None))


def _apply_disallowed(tools: list[str] | None, disallowed: list[str] | None,
                      api: Any) -> list[str] | None:
    """减掉角色声明的 `disallowed_tools`(denylist)。

    口径照 Claude Code(设计里引的就是它):**先应用 denylist,allowlist 在剩余池里解析** ——
    两边都列到就移除。支持 `fnmatch` 通配(`mcp__github__*` 一并减掉)。

    `tools=None` 是"继承父":要减就得先把父的**当前集合具象化**,否则无从下手。
    拿不到(没有 `getActiveTools`)就退回 `None` —— 宁可不减,不假装减了。
    """
    if not disallowed:
        return tools
    from fnmatch import fnmatch

    if tools is None:
        getter = getattr(api, "getActiveTools", None)
        if getter is None:
            return None
        tools = [str(t) for t in getter()]
    return [t for t in tools if not any(fnmatch(t, pattern) for pattern in disallowed)]


async def _run_one(api: Any, role: Role, task: str, ctx: Any,
                   sem: asyncio.Semaphore | None = None) -> dict:
    """跑一个角色。返回结构化结果(带 usage/错误),失败**不抛**给上层 ——
    一个子任务失败不该把另外几个已经跑完的结果一起扔掉。"""
    tools = None if role.tools is None else [t for t in role.tools if t != "subagent"]
    # E25:角色 `tools:` 里的 MCP 条目**换掉**成真实工具名 —— `mcp__gh__*` 是模式、不是工具名,
    # 直接留给 runner 会被报成“未知工具”;`mcp` 会由返回的名字重新带回。
    mcp_names = await _role_mcp_names(api, role, ctx, role.tools)
    if tools is not None:
        rest = [t for t in tools if t != "mcp" and not t.startswith("mcp__")]
        tools = [*rest, *mcp_names]
    # denylist 放最后:它要能减掉 MCP 直连出来的工具(`mcp__github__*`)
    tools = _apply_disallowed(tools, role.disallowed_tools, api)
    spec = {"system_prompt": role.prompt or f"你是 {role.name}。",
            "name": role.name, "tools": tools, "model": role.model}
    # `ctx.signal` 才是 AbortSignal(`ctx.abort()` 是**方法** —— 以前这里取错成那个方法,
    # 结果中断根本没传下去:E12 的“协作式中断直接透传”一直是空的)。
    signal = getattr(ctx, "signal", None)
    # 宿主可能只有其中一种写法(snake_case 是正式名,camelCase 是别名) —— 两种都试。
    run_agent = getattr(api, "run_agent", None) or getattr(api, "runAgent")
    try:
        if sem is not None:
            async with sem:
                text = await run_agent(spec, task, abort=signal)
        else:
            text = await run_agent(spec, task, abort=signal)
        return {"agent": role.name, "task": task, "ok": True, "output": text}
    except Exception as exc:  # noqa: BLE001 子运行失败 → 结果里如实说
        return {"agent": role.name, "task": task, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"}


def _render(results: list[dict]) -> str:
    """给**模型**看的散文(与 `details` 分开:那个是给界面看的结构化数据)。"""
    parts: list[str] = []
    for r in results:
        head = f"## {r['agent']}"
        if not r["ok"]:
            parts.append(f"{head}\n[失败] {r['error']}")
        else:
            parts.append(f"{head}\n{_clip(r['output'])}")
    return "\n\n".join(parts) or "(没有任务)"


def build_tool(api: Any) -> Tool:
    async def run_subagent(args: dict, ctx: Any) -> Any:
        from qi_agent.models import TOOL_ERROR, ToolOutcome

        scope: AgentScope = args.get("agentScope") or "user"
        cwd = getattr(ctx, "workdir", None)
        roles = discover(cwd, scope)
        tasks = _tasks_of(args)
        if not tasks:
            return (f"用法:single 给 `agent` + `task`;parallel 给 `tasks:[{{agent,task}}]`;"
                    f"chain 给 `chain:[…]`(task 里可用 {{previous}})\n可用角色:"
                    f"{format_roles(roles)}")
        if len(tasks) > MAX_PARALLEL_TASKS:
            return f"任务太多({len(tasks)} > {MAX_PARALLEL_TASKS},上限在求稳)"

        unknown = sorted({str(t.get("agent") or "") for t in tasks
                          if _resolve(roles, t.get("agent")) is None})
        if unknown:
            return f"未知角色 {unknown};可用角色:{format_roles(roles)}"

        # 项目级角色 = 仓库控制的提示词 → 先用前问一句(无界面时保守拒绝)
        if scope in ("project", "both") and bool(args.get("confirmProjectAgents", True)):
            wanted = [str(t.get("agent")) for t in tasks]
            from .discovery import project_roles

            risky = project_roles(cwd, wanted)
            if risky and not getattr(ctx, "project_trusted", True):
                ok = await ctx.ui.confirm(
                    "要用项目里的角色吗?",
                    title="项目级角色",
                    default=False) if getattr(ctx, "ui", None) is not None else False
                if not ok:
                    return f"已取消:项目级角色未获批准({[r.name for r in risky]})"

        chain = isinstance(args.get("chain"), list)
        if chain:
            results: list[dict] = []
            previous = ""
            for item in tasks:
                task = str(item.get("task") or "").replace("{previous}", previous)
                out = await _run_one(api, _require(roles, item.get("agent")), task, ctx)
                results.append(out)
                previous = out.get("output", "") if out["ok"] else previous
                if not out["ok"]:
                    break                      # 链断在哪儿,后面的就没意义了
        else:
            sem = asyncio.Semaphore(MAX_CONCURRENCY) if len(tasks) > 1 else None
            results = await asyncio.gather(*[
                _run_one(api, _require(roles, t.get("agent")), str(t.get("task") or ""), ctx, sem)
                for t in tasks])

        failed = [r for r in results if not r["ok"]]
        details = {"ui_version": 1, "ui": [
            {"type": "list", "items": [
                {"label": f"{r['agent']} · {r['task'][:40]}",
                 "state": "error" if not r["ok"] else "done",
                 "note": r.get("error", "")} for r in results]}]}
        return ToolOutcome(
            status=TOOL_ERROR if failed else "ok",
            result=_render(results),
            error="subagent_failed" if failed else None,
            details=details)

    return Tool("subagent", "把一个任务交给另一个角色(独立上下文);支持 single/parallel/chain",
                _SCHEMA, run_subagent, prompt_snippet="委派任务给另一个角色")
