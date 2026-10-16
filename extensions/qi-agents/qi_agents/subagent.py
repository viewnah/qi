"""`subagent` 工具:把一个任务交给另一个角色 —— 单层拆解,也可分层往下派。

这是 E12 定下的形态 —— **进程内受管子运行**(`api.runAgent`),不是子进程:
Python 每进程首调 LLM 要付 ~6.8s 的 litellm import,子进程等于 6.8s × N(8 并行 ≈ 54s),
而 Node 起一次只要 0.04s。pi 的子进程模型不能平移到这里。

三种模式(照搬 pi 官方 subagent 扩展的已验证形状),**任何一层**拿到 `subagent` 的运行都能用:

    single    {agent, task}
    parallel  {tasks: [{agent, task}, …]}      并发 4,最多 8 个任务
    chain     {chain: [{agent, task}, …]}      顺序,`{previous}` 占位符传上一棒输出

**分层委派是预算制的**(子角色可以继续往下派,不再是“结构性不可能”):

    delegate           角色 frontmatter 的总开关(缺省 true;false = 到此为止)
    MAX_DEPTH = 3      深度硬上限(主会话 = 0,最多 3 层子运行)
    SPAWN_BUDGET = 64  一次顶层调用的整棵委派树共享 64 个名额(只减不增)
    disallowed_tools   里写 `subagent` 仍一票否决(denylist 在所有授权**之后**应用)

**深度与“继承父”的唯一事实是 `RunRegistry`**:宿主给子运行的 `ToolContext.agent_name`
就是 `spec["name"]`(runtime.py 构造 ToolContext 时用的就是它),`_run_one` 用唯一名
(`scout` / `scout#2`)把 `RunInfo(depth, tools, budget)` 登记在名下 —— 子调 `subagent`
时拿 `ctx.agent_name` 查表。查不到表、又不是主会话的调用者一律 **fail-closed 拒绝**。
不用 contextvar/进程级计数器:那些机制“坏了”时会静默读成深度 0 —— 恰好是放行无限递归
的方向;查表 miss 是显式失败,坏在安全的那一边。

子运行的过程走**两条输出面**(格式契约在 `render.py`):

* **时间线**(对话流,append-only):agent 切换时出一个标题块 `── 名字 ──`,
  每轮思考推 `✎ 预览`、工具活动推 `· 工具 参数`(缩进跟随,名字不做行前缀);
* `result` 文本 = **横幅分段**(`── 名字 (深度N, 状态) ──`),每段三区 ——
  `·` 活动 → `✎` 该层各轮思考 → 输出,给模型 / 导出 / 回放看;
  不画树地图:委派关系在活动行与横幅深度里已经自明;
* TUI 卡片**每个 agent 一张、平铺**(`Tool.render_call` 返回容器;卡标题 =
  名字 ← 由谁指派 · 深度,正文内滚自动跟底;`render_result` 返回 None —— 各卡
  收束时各自定格,树卡退役);`details.ui` 仍进事件与会话存档(TUI 不渲染
  details,它不是界面通道)。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from qi_agent.extensions import Tool

from .discovery import AgentScope, Role, discover, format_roles
from . import render as render_mod
from .render import banner

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
MAX_DEPTH = 3                  # 深度硬上限:主会话 = 0,最多 3 层子运行(对齐 Claude Code 缺省)
SPAWN_BUDGET = 64              # 一次顶层调用的整棵委派树共享的 spawn 名额(只减不增)
STEP_LOG_LIMIT = 30            # 每层进 result 的工具活动行上限(长链不把段撑爆)
THOUGHT_ROUNDS = 6             # 每层进 result 的思考轮次上限
THOUGHT_CHARS = 600            # 每轮思考进 result 的字符上限(思考是引用,截断不丢义)
STREAM_FLUSH_INTERVAL = 0.3    # 流式刷进本 agent 卡的节流间隔(秒) —— delta 是逐字的,不节流等于刷屏
DONE_SUMMARY_LINES = 3         # 时间线完成块的输出摘要行数(headless 专用;前端全文在该 agent 的卡里)

#: 时间线全局:最近出过标题的 run —— 同一 agent 连续内容不重复出标题;
#: 切换(或新的工具调用开一条新时间线)时,下一个内容必出一个新标题块。
_LAST_TITLE = ""
PER_TASK_CAP = 50_000          # 单个子结果进文本前截断(与工具输出同档)


class _Budget:
    """一次顶层委派树共享的 spawn 名额。**只减不增**:链断/失败不退名额 ——
    预算是防失控的闸,不是记账系统;松一口气的方向永远是“少派”。
    """

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    def take(self, n: int) -> bool:
        """原子地取 n 个名额;不够则一个都不动。"""
        if n <= self.remaining:
            self.remaining -= n
            return True
        return False


@dataclass
class _RunInfo:
    """一个**运行实例**的登记项 —— 分层委派的唯一事实来源(见模块文档)。"""

    depth: int
    tools: list[str] | None      # 该运行最终的工具清单(子角色“继承父”的来源)
    budget: _Budget
    #: 它派出的子结果。子运行结束后由发起方回收进 `children` —— 父层的
    #: `details` 因此能看到整棵树(孙及更深同理,层层回收)。
    spawned: list[dict] = field(default_factory=list)
    #: 该运行的名字(`reg_name`/`spec.name`,卡标题与完成块都用它)。
    name: str = ""
    #: 本次工具调用的**平铺架子**(每 agent 一卡):首层在 `run_subagent` 认领,
    #: 嵌套子层**继承发起者的同一个 list** —— 子调用没有自己的工具块,追加到
    #: 同一个架子上;各 run 的事件进自己那张卡,不落时间线(否则同一段过程
    #: 会在对话流与卡片各出现一遍)。
    holder: list | None = None


REGISTRY: dict[str, _RunInfo] = {}    # 唯一 run 名 → 登记项(run 结束即注销)


def _unique_name(base: str) -> str:
    """同名并发不撞:占用时加 `#2`、`#3` …(登记以**运行实例**为单位,不以角色名)。"""
    if base not in REGISTRY:
        return base
    n = 2
    while f"{base}#{n}" in REGISTRY:
        n += 1
    return f"{base}#{n}"


def _is_root(agent_name: str) -> bool:
    """调用者是不是主会话(`ToolContext.agent_name` = core 的 `CORE_AGENT_NAME`)。"""
    if not agent_name:
        return True
    try:
        from qi_agent.runtime import CORE_AGENT_NAME

        return agent_name == CORE_AGENT_NAME
    except Exception:                 # pragma: no cover - 防御:导入失败退回字面量
        return agent_name == "qi"

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


def _preview(text: str, lines: int = 5) -> str:
    """运行中推给时间线的思考预览:截 lines 行/400 字,每行自带缩进,首行带 `✎`。
    完整思考在 result 段里(卡片),所以超出只标注一句“全文见卡片”。"""
    parts = text.splitlines() or [text]
    more = len(parts) > lines
    body = "\n".join(parts[:lines])
    if len(body) > 400:
        body, more = body[:400] + "…", True
    if more:
        body += " …(全文见卡片)"
    return "\n".join("  ✎ " + ln if i == 0 else "  " + ln
                     for i, ln in enumerate(body.split("\n")))


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
        cwd=_cwd_of(ctx),
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


def _cwd_of(ctx: Any) -> Any:
    """`ctx` 的工作目录 —— 两个形状的名字不同(见 `_abort_signal` 的说明)。

    `ToolContext.workdir`(工具侧,`subagent` 走的就是这条)与
    `ExtensionContext.cwd`(handler 侧)。只认 `workdir` 会让工具路径下的角色发现
    退化成"从当前进程 cwd 找项目" —— `.qi/agents/` 在会话目录不在进程 cwd 时找不到。
    """
    return getattr(ctx, "workdir", None) or getattr(ctx, "cwd", None)


def _abort_signal(ctx: Any) -> Any:
    """本回合的中断信号(`AbortSignal`),取不到就 None。

    **为什么不能只写 `ctx.signal` 或只写 `ctx.abort`**:`ctx` 有两个形状,而这里
    两种都会遇到 ——

    * **工具侧**(`Tool.execute` 的第二个参数)= `ToolContext`,信号在 **`abort`** 字段上,
      没有 `signal`;
    * **事件 handler 侧** = `ExtensionContext`,信号在 **`signal`** 上,而 `abort` 是
      **方法**(pi 的 `ctx.abort()`)。

    所以上一版写的 `getattr(ctx, "signal", None)` 在工具路径上**恒为 None**,而更早那版
    `getattr(ctx, "abort", None)` 在 handler 路径上会拿到一个**绑定方法** —— 两次都是
    静默降级(拿 None / 拿错对象都不会抛),"Esc 杀不掉子 agent"于是有两张脸。

    判据落在**形状**上:可调用的一律不认(那是方法),再看它是否长得像信号。
    """
    for name in ("signal", "abort"):
        value = getattr(ctx, name, None)
        if value is None or callable(value):
            continue
        if hasattr(value, "aborted") or hasattr(value, "wait"):
            return value
    return None


async def _run_one(api: Any, role: Role, task: str, ctx: Any,
                   sem: asyncio.Semaphore | None = None, *,
                   parent: _RunInfo | None = None,
                   budget: _Budget | None = None,
                   holder: list | None = None) -> dict:
    """跑一个角色。返回结构化结果(带 usage/错误),失败**不抛**给上层 ——
    一个子任务失败不该把另外几个已经跑完的结果一起扔掉。

    `parent` = 发起这次调用的运行(主会话发起时是 None):它同时给出**深度**与
    “继承父”的来源 —— 嵌套下“父的集合”是发起者自己的清单,不是主会话的。
    `budget` 是这棵委派树共享的 spawn 名额(不传 = 新建,给直接调用方兜底)。
    `holder` = 本次工具调用的平铺架子(`run_subagent` 认领/继承,同一次调用的
    并行与链式兄弟共享同一个):卡里收流式与活动,时间线不再推思考;
    None/空 = 无卡(headless),时间线兜底。
    """
    depth = parent.depth + 1 if parent is not None else 1
    if budget is None:
        budget = _Budget(SPAWN_BUDGET)
    # `tools: None` = **继承父**。以前这里直接把 None 交给 `runAgent`,由宿主去解析成
    # “主会话的当前集合” —— 主会话的集合里有 `subagent`,而且也不精确(嵌套时发起者
    # 清单里被剥掉的工具会被重新塞回来)。所以在扩展这一侧先把清单具象化,
    # 让清单闸**只有一条出口**。
    tools: list[str] | None
    if role.tools is None:
        if parent is not None and parent.tools is not None:
            # 继承**发起者**这一层的清单(宿主的 tools=None 给的是主会话的)。
            tools = [str(t) for t in parent.tools]
        else:
            # 宿主可能只有其中一种写法(snake_case 是正式名,camelCase 是别名) —— 两种都试。
            # 鸭子类型调用:先把结果落成 `object`,再用 isinstance 收窄。
            # (`getattr` 的结果过了 `callable()` 之后会被静态检查当成 `(...) -> object`,
            #  直接迭代它会被判成“object is not iterable”。)
            getter = getattr(api, "get_active_tools", None) or getattr(api, "getActiveTools", None)
            names: object = getter() if callable(getter) else None
            tools = [str(t) for t in names] if isinstance(names, (list, tuple)) else None
    else:
        tools = [str(t) for t in role.tools]
    # E25:角色 `tools:` 里的 MCP 条目**换掉**成真实工具名 —— `mcp__gh__*` 是模式、不是工具名,
    # 直接留给 runner 会被报成“未知工具”;`mcp` 会由返回的名字重新带回。
    if tools is not None:
        mcp_names = await _role_mcp_names(api, role, ctx, role.tools)
        rest = [t for t in tools if t != "mcp" and not t.startswith("mcp__")]
        # 继承父那条路:父的 `mcp` 代理要留着(那是父本来就有的权限,不是提权);
        # 显式声明那条路:由 `mcp_names` 决定。
        keep_proxy = role.tools is None and "mcp" in tools
        tools = [*rest, *(mcp_names or (["mcp"] if keep_proxy else []))]
    # **分层委派的清单闸**:`subagent` 的去留 = 角色 `delegate` × 深度上限。
    # 深度在这一层算出来(不靠“每层记得加一”);算错的方向由 denylist 与
    # 登记表 fail-closed 兜底。
    can_delegate = bool(role.delegate) and depth < MAX_DEPTH
    if tools is not None and not can_delegate:
        tools = [t for t in tools if t != "subagent"]
    if tools is None and not can_delegate:
        # 拿不到父清单就无法证明 `subagent` 已被剥离 → fail-closed 不启动(而不是把 None
        # 交给宿主,让它用主会话的集合把 `subagent` 塞回来)。
        return {"agent": role.name, "task": task, "ok": False, "depth": depth,
                "error": "fail-closed:拿不到父工具清单,无法确认 subagent 已被剥离,不启动子运行"}
    # denylist 放最后:它要能减掉 MCP 直连出来的工具(`mcp__github__*`),
    # 也在这里对 `subagent` 一票否决(优先级高于 delegate)。
    tools = _apply_disallowed(tools, role.disallowed_tools, api)
    # 登记运行实例:唯一名进 spec(`ToolContext.agent_name` 就是它),
    # 发起方凭 `_RunInfo` 回收 children(见模块文档)。
    name = _unique_name(role.name)
    info = _RunInfo(depth=depth, tools=tools, budget=budget,
                    holder=holder, name=name)
    REGISTRY[name] = info
    spec = {"system_prompt": role.prompt or f"你是 {role.name}。",
            "name": name, "tools": tools, "model": role.model}
    # 中断信号:`ctx` 在工具路径上是 `ToolContext`(字段 `abort`)、在 handler 路径上是
    # `ExtensionContext`(字段 `signal`,而 `abort` 是方法)—— 见 `_abort_signal`。
    signal = _abort_signal(ctx)
    #: 本 run 自己的工具活动(`· 工具 参数`)与各轮思考 —— 进 result 对应段,
    #: 卡片上"这个 agent 干了什么、想了什么、说了什么"物理相邻成一块。
    log: list[str] = []
    thoughts: list[str] = []
    #: 最后一轮思考:收尾轮常不吐文本(mimo-flash 实测),output 空时靠它定位。
    last_thinking = ""
    #: 流式缓冲:当前轮的 thinking/text delta 累计(有卡时),轮末清零重来。
    stream_buf = ""
    last_flush = 0.0

    def _push(line: str, *, level: str = "info") -> None:
        """往时间线推一个内容块:agent 切换时先出黄标题块(名字只标一次),
        内容按深度缩进跟随 —— 不再把 `[run名]` 塞在每一行前面。"""
        global _LAST_TITLE
        try:
            ui = getattr(ctx, "ui", None)
            if not callable(getattr(ui, "notify", None)):
                return
            indent = "  " * max(0, depth - 1)
            if _LAST_TITLE != name:
                ui.notify(f"{indent}━━━ 委派 → {name} ━━━━━━━", level="warning")
                _LAST_TITLE = name
            ui.notify(indent + line, level=level)
        except Exception:  # noqa: BLE001 时间线是旁路:失败不该带倒子运行
            pass

    # 本次调用的平铺架子首层在 `run_subagent` 认领(嵌套继承同一个) —— 每个
    # agent 启动时往架子里追加**自己的一张卡**(全树拍平),事件进自己那张。
    # 空 holder 再认领一次兜底(入队若晚于执行的乱序;headless 队列本来就空)。
    stack = None
    if holder is not None:
        if not holder and render_mod.PENDING_CARDS:
            holder.append(render_mod.PENDING_CARDS.pop(0))
        stack = holder[0] if holder else None
    parent_name = (parent.name if parent is not None and parent.name
                   else str(getattr(ctx, "agent_name", None) or "qi"))
    me_card = None
    adder = getattr(stack, "add_agent", None)
    if callable(adder):
        me_card = adder(name, parent_name, depth)

    def _web(phase: str, **extra: Any) -> None:
        """把本 run 的活动推给宿主(**直播**,design/web-lab.md 二期)。

        走 `ctx.on_update` —— runner 把它转成 `tool_execution_update` 总线事件,
        外层自带**这次工具调用**的 `tool_call_id`:嵌套子运行经各自的 ctx 推出去,
        收敛到同一个 id 上,前端一张卡装下整棵委派树(与平铺架子同构)。

        三道护栏叠在一起,所以推不出去是常态而不是事故:宿主没订阅时 runner
        直接跳过(`bus.has`);handler 路径的 ctx 没有 `on_update` → 这里跳过;
        **旁路**:推失败不带倒子运行。

        `line` 是**完整展示行**(带 `· `/`✎ `):与结束后 `_render` 拼进 result 的
        同名行逐字一致 —— 直播关掉后,回放看到的是同一批文字(回放红线)。
        """
        push = getattr(ctx, "on_update", None)
        if not callable(push):
            return
        try:
            push({"qi": {"kind": "subagent", "phase": phase, "run": name,
                         "role": role.name, "parent": parent_name,
                         "depth": depth, "task": task, **extra}})
        except Exception:  # noqa: BLE001 直播是旁路:失败不该带倒子运行
            pass

    def _current_card() -> Any:
        """本 run **自己那张**平铺卡(入口建好);None = headless/建卡失败 →
        思考走时间线兜底、活动只进 log 与 result 段。"""
        return me_card

    def _flush_stream(mode: str) -> None:
        """把当前轮的流式缓冲推进本 agent 的卡(见 _AgentCard),并推给直播。

        节流发生在调用方(`STREAM_FLUSH_INTERVAL` 到点、轮末强制),所以直播帧
        天然不会逐字刷屏;headless 的 delta 在 `_on_event` 就被挡掉,进不到这。
        """
        _web("stream", mode=mode, text=stream_buf)
        try:
            setter = getattr(_current_card(), "set_stream", None)
            if callable(setter):
                setter(stream_buf, mode)
        except Exception:  # noqa: BLE001 卡片是旁路:失败不该带倒子运行
            pass

    def _push_done(ok_: bool, res_: dict) -> None:
        """run 收束 —— **前端时间线完全不出**:完成信息全归卡(卡标题已有
        `▸ 名字 ← 父 · 深度N ✓/✗`,对话流再出一遍 `── 名字 ✓ 深度N ──` =
        同一事写两处)。无前端(headless/notes,没有卡可看)才落完成块
        (节点 + 输出摘要) —— 那时它是唯一日志。失败色保留(error)。"""
        try:
            ui = getattr(ctx, "ui", None)
            if not callable(getattr(ui, "notify", None)):
                return
            if getattr(ui, "has_frontend", False):
                return  # 前端:连节点块也归卡,时间线零扩展输出
            indent = "  " * max(0, depth - 1)
            ui.notify(f"{indent}── {name} {'✓' if ok_ else '✗'} 深度{depth} ──",
                      level="info" if ok_ else "error")
            body = (str(res_.get("output") or "") if ok_
                    else str(res_.get("error") or "失败"))
            lines = body.splitlines()
            head = [ln for ln in lines[:DONE_SUMMARY_LINES] if ln.strip()]
            for ln in head or ["(无文本输出)"]:
                ui.notify(indent + "  " + ln)
            if len(lines) > DONE_SUMMARY_LINES:
                ui.notify(indent + f"  …(+{len(lines) - DONE_SUMMARY_LINES} 行,全文见卡片)")
        except Exception:  # noqa: BLE001 时间线是旁路:失败不该带倒子运行
            pass

    def _on_event(event: Any) -> None:
        """子运行的事件旁路 —— 三个去处:

        1. **流式**(本 agent 的平铺卡 + 宿主的直播通道,实时):`thinking_delta`/`text_delta`
           累计进当前轮缓冲,节流 `STREAM_FLUSH_INTERVAL` 秒刷一次 —— **卡片与直播是
           两条独立的旁路**:没有平铺卡(web 就没有)也要推直播。
        2. **时间线**(对话流,append-only):**前端只出节点** —— run 收束的完成块
           `── 名字 ✓ 深度N ──`(失败用 error 色);**过程归卡**(黄标题/工具
           活动/思考/输出都在该 agent 自己的卡里,时间线再出一套就是重复)。仅
           **无前端**(headless/notes,没有卡可显示)才把过程、`✎ 预览与结论
           摘要全量落时间线 —— 那时它是唯一日志;
        3. **分段**(result):`log`/`thoughts` 收进本 run 的段 —— 结束后卡片里
           一个 agent 的过程与输出物理相邻。

        渲染/收集失败都不能带倒子运行(旁路就是旁路)。
        """
        nonlocal last_thinking, stream_buf, last_flush
        kind = getattr(event, "kind", "")
        if kind in ("thinking_delta", "text_delta"):
            # **不要拿“有没有平铺卡”当开关**:那张卡是 TUI 的展示面,web(qi-lab)没有它 ——
            # 以前这里 `if _current_card() is None: return`,后果是 web 一个 stream 帧都收不到
            # (现象:“子运行的过程只有完全结束后才看得到”)。直播通道(`_web`)与卡片无关,
            # 两边各自旁路;无前端时 `_push` 也不是这里负责的。
            try:
                stream_buf += str(getattr(event, "text", "") or "")
                now = time.monotonic()
                if now - last_flush >= STREAM_FLUSH_INTERVAL:
                    last_flush = now
                    _flush_stream("think" if kind == "thinking_delta" else "text")
            except Exception:  # noqa: BLE001
                pass
            return
        if kind not in ("tool_start", "assistant_message"):
            return
        try:
            data = getattr(event, "data", None) or {}
            if kind == "assistant_message":
                thinking = data.get("thinking")
                has_think = isinstance(thinking, str) and bool(thinking.strip())
                if has_think:
                    last_thinking = thinking
                    if len(thoughts) < THOUGHT_ROUNDS:
                        thoughts.append(thinking if len(thinking) <= THOUGHT_CHARS
                                        else thinking[:THOUGHT_CHARS] + "…")
                        # 行与 `_render` 的 ✎ 区逐字一致(`✎ 第N轮: …`)
                        _web("think", line=f"✎ 第{len(thoughts)}轮: {thoughts[-1]}")
                    elif len(thoughts) == THOUGHT_ROUNDS:
                        thoughts.append("…(更多轮次的思考省略)")
                        _web("think", line=f"✎ 第{len(thoughts)}轮: {thoughts[-1]}")
                    if _current_card() is None and not getattr(
                            getattr(ctx, "ui", None), "has_frontend", False):
                        # 无前端 = 没有卡可显示:时间线是唯一的思考可见处;
                        # 有前端则只进卡(结束后点开看) —— 两边一起出就是重复
                        _push(_preview(thinking))
                # 轮末强制把本轮推出去,再清缓冲换下一轮。
                # **不包 `if card is not None`**:直播与卡片是两条旁路,web 没有卡也要推
                # (`_flush_stream` 内部对缺失的卡有处理)。
                last_flush = time.monotonic()
                _flush_stream("think" if has_think else "text")
                stream_buf = ""
                return
            tool = getattr(event, "tool", None) or "?"
            brief = json.dumps(data.get("args") or {}, ensure_ascii=False, default=str)
            if len(brief) > 80:
                brief = brief[:80] + "…"
            if len(log) < STEP_LOG_LIMIT:
                log.append(f"· {tool} {brief}")
                _web("act", line=log[-1])       # 直播 = result 活动区的同一行
            elif len(log) == STEP_LOG_LIMIT:
                log.append("· …(后续工具调用省略)")
            # 过程归卡:前端的活动行只进该 agent 的卡(时间线再出一遍就是重复);
            # 无前端(notes)才落时间线 —— 它是那场景的唯一日志。
            if not getattr(getattr(ctx, "ui", None), "has_frontend", False):
                _push(f"  · {tool} {brief}")
            adder = getattr(_current_card(), "add_activity", None)
            if callable(adder):
                adder(f"{tool} {brief}")
        except Exception:  # noqa: BLE001 时间线是旁路:失败不该带倒子运行
            pass

    _web("start")                        # 派单即推:卡在子运行真跑之前就出现
    # 宿主可能只有其中一种写法(snake_case 是正式名,camelCase 是别名) —— 两种都试。
    run_agent = getattr(api, "run_agent", None) or getattr(api, "runAgent")
    try:
        if sem is not None:
            async with sem:
                text = await run_agent(spec, task, abort=signal, on_event=_on_event)
        else:
            text = await run_agent(spec, task, abort=signal, on_event=_on_event)
        ok, error, output = True, None, text
    except Exception as exc:  # noqa: BLE001 子运行失败 → 结果里如实说
        ok, error, output = False, f"{type(exc).__name__}: {exc}", None
    finally:
        REGISTRY.pop(name, None)
    result: dict = {"agent": role.name, "task": task, "ok": ok, "depth": depth}
    if ok:
        if not (output or "").strip():
            # 收尾轮常不吐文本(实测 mimo-flash):思考过程本来就在本段的 ✎ 区,
            # 这里只指向它 —— 段绝不留空横幅(空段让委派失去意义)。
            output = (("(收尾轮未给最终文本,思考过程见本段 ✎ 部分)"
                       if thoughts or last_thinking.strip()
                       else "(无文本输出)"))
        result["output"] = output
    else:
        result["error"] = error
    if log:
        # 这个 run 自己干的事 —— 进 `_render` 的同名段(活动区)
        result["steps"] = log
    if thoughts:
        # 这个 run 每轮的思考 —— 进 `_render` 的同名段(✎ 思考区)
        result["thoughts"] = thoughts
    if info.spawned:
        # 它自己派出去的那棵子树 —— 发起方(如果有)把它挂进自己的 children。
        result["children"] = info.spawned
    _web("done", ok=ok, error=(error or "")[:300])
    _push_done(ok, result)
    if me_card is not None:
        # 平铺卡定格:标题转 ✓/✗、正文收起为摘要(点击展开该 agent 全文)
        setter = getattr(me_card, "set_done", None)
        if callable(setter):
            try:
                setter(ok, str(result.get("output") or result.get("error") or ""),
                       thoughts)
            except Exception:  # noqa: BLE001 卡片是旁路:失败不该带倒子运行
                pass
    return result


def _render(results: list[dict]) -> str:
    """给**模型**看的委派过程(与 `details` 分开:那个是进事件/存档的结构化数据)。

    格式 = **横幅分段**,契约在 `render.py`(`banner`/`BANNER_RE`,TUI 卡片按同一
    契约上色重画)—— 不画树地图:委派关系在活动行(`· subagent …`)与横幅深度
    里已经自明,地图是重复的第二份信息。每段三区:`·` 活动 → `✎` 该层各轮思考 →
    输出 —— 一个 agent 干了什么、想了什么、说了什么,物理相邻;空区直接跳过。
    归属不靠空格缩进:横幅自带名字、深度、状态,拷贝 / 导出 / 纯文本渲染都不丢。
    正文保持原样(它是引用,逐行重缩进会改写它),每层仍过 `_clip` 截断。
    """
    if not results:
        return "(没有任务)"
    segments: list[dict] = []

    def walk(rows: list[dict]) -> None:
        for r in rows:
            segments.append(r)
            walk(r.get("children") or [])

    walk(results)
    parts: list[str] = []
    for r in segments:
        head = banner(str(r.get("agent") or "?"), int(r.get("depth") or 1),
                      bool(r.get("ok")))
        blocks: list[str] = []
        steps = r.get("steps") or []
        if steps:
            blocks.append("\n".join(str(s) for s in steps))
        thoughts = r.get("thoughts") or []
        if thoughts:
            blocks.append("\n".join(f"✎ 第{i + 1}轮: {t}"
                                    for i, t in enumerate(thoughts)))
        blocks.append(_clip(str(r.get("output") or "")) if r.get("ok")
                      else f"[失败] {r.get('error')}")
        parts.append(head + "\n" + "\n\n".join(blocks))
    return "\n\n".join(parts)


def _ui_items(results: list[dict], indent: int = 0) -> list[dict]:
    """把结果树摊平成 `details["ui"]` 的 list 词汇表(design/web.md §16.2)。

    嵌套用 label 缩进表达(词汇表没有树节点);`depth` 是附加的机器可读字段 ——
    词汇表只约定已知键、不禁止附加键,不认识它的渲染端忽略即可。
    只放 label/state/note:树里可能很深,**输出正文不进 details**(它在 `result` 里,50k 截断)。
    """
    items: list[dict] = []
    for r in results:
        items.append({
            "label": "  " * indent + f"{r['agent']} · {str(r.get('task') or '')[:40]}",
            "state": "error" if not r["ok"] else "done",
            "note": str(r.get("error") or ""),
            "depth": int(r.get("depth") or 1),
        })
        items.extend(_ui_items(r.get("children") or [], indent + 1))
    return items


def build_tool(api: Any) -> Tool:
    async def run_subagent(args: dict, ctx: Any) -> Any:
        from qi_agent.models import TOOL_ERROR, ToolOutcome

        # 一次工具调用 = 一条新时间线:下一个内容必出黄标题块(嵌套子调用自然接上)
        global _LAST_TITLE
        _LAST_TITLE = ""

        # 身份闸(fail-closed,放最前):深度与“继承父”只认 RunRegistry。
        # 主会话(core 的 CORE_AGENT_NAME)不在表里 → 走根分支;其余查不到的一律拒绝 ——
        # 宁可让别的扩展的子运行拿到一句可读的拒绝,也不给“表漏了”留一个 depth=0 的后门
        # (depth=0 意味着新预算 + 主会话集合,恰是失控的方向)。
        agent_name = str(getattr(ctx, "agent_name", "") or "")
        me = REGISTRY.get(agent_name)
        if me is None and not _is_root(agent_name):
            return (f"无法确认调用者身份(agent_name={agent_name!r} 不在运行登记表里),"
                    f"已按 fail-closed 拒绝 —— 只有主会话与本扩展起的子运行能用 subagent")

        scope: AgentScope = args.get("agentScope") or "user"
        cwd = _cwd_of(ctx)
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

        budget = _Budget(SPAWN_BUDGET) if me is None else me.budget
        # 平铺架子:首层认领 render_call 入队的容器(同协程链上入队先于执行);
        # 嵌套子层**继承发起者的 holder** —— 子调用没有自己的工具块,追加到同一个
        # 架子上 = 每个 agent 一卡、全树拍平;各 run 的事件进自己那张卡。
        if me is not None:
            holder = me.holder if me.holder is not None else []
        else:
            holder = [render_mod.PENDING_CARDS.pop(0)] if render_mod.PENDING_CARDS else []
        chain = isinstance(args.get("chain"), list)
        if chain:
            results: list[dict] = []
            previous = ""
            for item in tasks:
                if not budget.take(1):
                    results.append({
                        "agent": str(item.get("agent") or ""),
                        "task": str(item.get("task") or ""), "ok": False,
                        "depth": me.depth + 1 if me is not None else 1,
                        "error": f"spawn 预算耗尽(一次顶层委派树上限 {SPAWN_BUDGET} 个名额,只减不增)"})
                    break
                task = str(item.get("task") or "").replace("{previous}", previous)
                out = await _run_one(api, _require(roles, item.get("agent")), task, ctx,
                                     parent=me, budget=budget, holder=holder)
                results.append(out)
                previous = out.get("output", "") if out["ok"] else previous
                if not out["ok"]:
                    break                      # 链断在哪儿,后面的就没意义了
        else:
            if not budget.take(len(tasks)):
                return (f"spawn 预算耗尽:本次要 {len(tasks)} 个名额,这棵委派树只剩 "
                        f"{budget.remaining}/{SPAWN_BUDGET}(只减不增)—— 缩小拆解规模,或分多轮派")
            sem = asyncio.Semaphore(MAX_CONCURRENCY) if len(tasks) > 1 else None
            results = await asyncio.gather(*[
                _run_one(api, _require(roles, t.get("agent")), str(t.get("task") or ""), ctx, sem,
                         parent=me, budget=budget, holder=holder)
                for t in tasks])

        if me is not None:
            # 挂到发起者名下:它跑完时,发起方把这份收进自己的 `children` → 父层 details 见到整棵树
            me.spawned.extend(results)

        failed = [r for r in results if not r["ok"]]
        details = {"ui_version": 1, "ui": [
            {"type": "list", "items": _ui_items(results)}]}
        return ToolOutcome(
            status=TOOL_ERROR if failed else "ok",
            result=_render(results),
            error="subagent_failed" if failed else None,
            details=details)

    # render_call / render_result 一起挂:只挂 result 时卡片是内置 ToolBlock,
    # tui.py 的 tool_end 会把自绘结果丢掉(见 render.py 模块文档)。
    return Tool("subagent", "把一个任务交给另一个角色(独立上下文);支持 single/parallel/chain,"
                "拿到 subagent 的角色可按深度/预算继续往下派",
                _SCHEMA, run_subagent, prompt_snippet="委派任务给另一个角色",
                render_call=render_mod.render_call,
                render_result=render_mod.render_result)
