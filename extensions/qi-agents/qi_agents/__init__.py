"""qi-agents:qi 的角色系统(P-E4c 从 core 移出,现在是一个扩展)。

三轮设计都压在这一份 `register()` 里,三件事:

1. **角色发现**:`~/.qi/agent/agents/<名>/agent.md`(用户)+ `<项目>/.qi/agents/<名>/agent.md`(项目)
   —— 见 `discovery.py`;
2. **角色选择**:`qi --ext agent=reviewer`(或 `--ext agent=reviewer`)把那个角色的说明注入本轮提示词。
   走 `registerFlag` + `before_agent_start`(**不新增 core 概念**,这是 E14 选的路);
3. **委派**:`subagent` 工具把任务交给另一个角色 —— 进程内受管子运行(`api.runAgent`,E12)。

为什么角色选择不写成 core 的 `--agent`:core 现在不认识"角色"(E15),而扩展用通用扩展面
就能做到这件事 —— `registerFlag` 声明旗标、`before_agent_start` 返回改过的提示词。core 一行不用改。
"""

from __future__ import annotations

from typing import Any

from qi_agent.extensions import ExtensionApi

from .discovery import DEFAULT_SCOPE, discover, format_roles
from .subagent import build_tool

__all__ = ["register"]


def _role_for_flag(api: ExtensionApi, cwd: Any) -> Any:
    """旗标点了名就找它;名字不对返回 None(由调用方决定怎么提示)。"""
    name = str(api.getFlag("agent") or "").strip()
    if not name:
        return None
    return discover(cwd, "both").get(name), name


async def _project_role_setup(role: Any, ctx: Any) -> str:
    """项目级角色要用就得先过信任 —— 返回跳过原因,空串 = 放行。

    **为什么这条闸门必须在**:项目级角色的正文是**仓库控制的提示词**(与 `subagent` 工具的
    `agentScope="project"` 同一条安全模型)。此前 `--ext agent=<项目角色>` 直接把它拼进系统
    提示词 —— `subagent` 那条路有闸门、这条路没有,同一个风险两个口径。

    与工具路径的差别只在**无界面时**:那里是"保守拒绝"(角色本来就没跑);这里是"跳过并说明"
    —— 用户敲的是"以这个角色起会话",静默地少掉角色层比说一句更糟。

    这个函数是 `async` 的:闸门要问人(`ctx.ui.confirm` 是协程),而事件总线支持 async handler。
    """
    if getattr(role, "source", "") != "project":
        return ""
    trusted = getattr(ctx, "project_trusted", None)
    if trusted is None:
        checker = getattr(ctx, "is_project_trusted", None)
        trusted = checker() if callable(checker) else True
    if trusted:
        return ""
    ui = getattr(ctx, "ui", None)
    if getattr(ctx, "has_ui", False) and ui is not None:
        consent = await ui.confirm(f"要用项目里的角色 {role.name} 吗?", title="项目级角色",
                                   default=False)
        return "" if consent else f"项目级角色 {role.name} 未获批准"
    return f"项目未被信任,项目级角色 {role.name} 未使用(用 `-a` 信任,或改用用户级角色)"


def register(api: ExtensionApi) -> None:
    # ── 1. 角色选择:一个旗标 + 一个钩子,不碰 core ──
    api.registerFlag("agent", type="string", default="",
                     description="以某个角色运行(名字见 `--ext agent=` 的提示 / /agents)")

    async def on_start(payload: dict, ctx: Any):
        found = _role_for_flag(api, ctx.cwd)
        if found is None:
            return None
        role, wanted = found
        notes = getattr(ctx, "notes", None)
        if role is None:
            if isinstance(notes, list):
                notes.append(f"--ext agent={wanted} 找不到这个角色;"
                             f"可用:{format_roles(discover(ctx.cwd, 'both'))}")
            return None
        blocked = await _project_role_setup(role, ctx)
        if blocked:
            if isinstance(notes, list):
                notes.append(blocked)
            return None
        # 追加到**基座之后**(与 v1 的顺序一致:基座 → 角色层 → 项目上下文 → …)
        return {"system_prompt": f"{payload['system_prompt']}\n\n{role.prompt}".rstrip()}

    api.on("before_agent_start", on_start)

    # ── 2. 委派:一个工具,三种模式 ──
    api.registerTool(build_tool(api))

    # ── 3. 列角色:补回 core 删掉的 `qi agents list`(TUI 里是 `/agents`)──
    def on_command(args: str, ctx: Any) -> None:
        scope = "both" if (args or "").strip() in ("both", "all") else DEFAULT_SCOPE
        roles = discover(ctx.cwd, scope)
        lines = [f"角色({scope},共 {len(roles)}):"]
        lines += [f"  {r.name}  [{r.source}]  {r.description}"
                  + (f"  tools={','.join(r.tools)}" if r.tools else "")
                  + (f"  model={r.model}" if r.model else "")
                  for r in sorted(roles.values(), key=lambda r: r.name)]
        if not roles:
            lines.append(f"  (无)装角色:把 agent.md 放进 ~/.qi/agent/agents/<名>/ 或 .qi/agents/<名>/")
        lines.append("用某个角色跑:`qi --ext agent=<名> \"任务\"`(要项目角色加 agentScope)")
        ctx.ui.notify("\n".join(lines))

    api.registerCommand("agents", on_command, description="列出角色(可跟 `both` 看项目级)")
