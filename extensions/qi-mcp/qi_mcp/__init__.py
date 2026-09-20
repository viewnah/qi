"""qi-mcp:qi 的 MCP 支持(形状照搬 `pi-mcp-adapter`,见 docs/extensions.md E25)。

分四个切片做,当前进度:

| 切片 | 内容 | 状态 |
| --- | --- | --- |
| 1 | 两层声明表 + `/mcp` 面板 | ✅ |
| 2 | `mcp` 代理工具(search / describe / call)+ lazy 连接 | ✅ |
| 2b/2c | 真客户端:stdio + Streamable HTTP(端到端跑真 server) | ✅ |
| 3 | `directTools` 直连 + `includeTools`/`excludeTools` + `toolPrefix` | ✅ 本切片 |
| 4 | 给 qi-agents 的**按值注入** API(它读角色目录的 mcp.json 交给这里) | ⏳ |

为什么默认是**一个代理工具**而不是每 server 注册 N 个工具:`pi-mcp-adapter` 的实测理由是
**上下文** —— 一个 server 的工具定义轻松 10k+ token,连几个 server 就在对话开始前烧掉半个
上下文窗口。代理工具约 200 token,模型按需 `search` 发现、再 `call`。直连是每 server
`directTools` 显式开的("这些我常用,值得占着上下文")。
"""

from __future__ import annotations

from typing import Any

from .client import connect
from .config import (  # noqa: F401  (对外导出)
    MCP_FILE,
    ConfigError,
    McpScope,
    ServerSpec,
    active,
    load_layers,
    resolve,
    scope_files,
    unknown_fields,
)
from .direct import direct_selection, prefix_mode, select_tools, tool_name
from .proxy import build_proxy_tool
from .role import register_role_mcp, role_specs
from .servers import ManagerError, ServerManager, ToolInfo

__all__ = ["register", "register_direct_tools", "register_role_mcp", "role_specs",
           "resolve", "active", "load_layers", "scope_files", "ServerSpec", "ConfigError",
           "MCP_FILE", "McpScope", "tool_name", "select_tools", "direct_selection"]


def _direct_tool(info: ToolInfo, name: str, manager: ServerManager) -> Any:
    """把一个 MCP 工具包成 qi 的工具(直连形态)。"""
    from qi_agent.extensions import Tool

    async def execute(args: dict, ctx: Any) -> str:
        # 与代理同一条原则:失败**变成可读文本**回给模型,而不是把一次调用变成回合失败。
        try:
            return await manager.call(info.server, info.name, args)
        except ManagerError as exc:
            return f"mcp 调用失败:{exc}"

    return Tool(name,
                info.description or f"{info.server} 的 {info.name}",
                info.schema or {"type": "object", "properties": {}},
                execute)


async def register_direct_tools(api: Any, ctx: Any, *, manager: ServerManager) -> list[str]:
    """按 `directTools` 把要直连的工具注册进 catalog,并把它们加进**当前工具集**。

    单独抽成函数而不是塞进事件处理器里,是为了能直接测 —— 事件适配器(`session_start`)
    只负责把 `ctx` 递过来。返回值是本次注册成功的工具名(诊断与测试用)。

    三处判断:
    - 已经存在的同名工具 → **跳过并说明**(静默覆盖会让用户看到"我的工具被顶掉了");
    - `disabled` 的 server → 连都不连(`active()` 已经滤掉了);
    - 连不上/列不出工具 → 由 manager 记 note,这里不抛(一个 server 坏了不该让会话起不来)。
    """
    added: list[str] = []
    existing = set(api.catalog.names)
    for name, spec in manager.specs.items():
        if not spec.direct_tools:                     # 默认 false → 只走代理
            continue
        infos = await manager.tools_of(name)          # 只连这一个 server
        for info in direct_selection(infos, spec):
            tool = _direct_tool(info, tool_name(info.server, info.name, prefix_mode(spec)),
                                manager)
            if tool.name in existing:
                ctx.ui.notify(f"直连工具 {tool.name} 与已有工具同名,跳过"
                              f"(给这个 server 设 toolPrefix 换个前缀)")
                continue
            api.registerTool(tool)
            existing.add(tool.name)
            added.append(tool.name)
    if added:
        current = api.getActiveTools()
        api.setActiveTools([*current, *[n for n in added if n not in current]])
    return added


def register(api: Any) -> None:
    """装载 qi-mcp。

    **故意不调用 `provides_config("mcp_servers")`** —— 按 E25,"agent 目录里的 mcp.json"
    由 qi-agents 读、按值交给这里,不是本扩展去扫,声明了反而是假的。
    """
    # 按 cwd 缓存 manager:一次会话一个(连接的复用与元数据缓存都挂在它身上)。
    managers: dict[str, ServerManager] = {}

    def manager_for(ctx: Any, *, notify: bool = False) -> ServerManager:
        cwd = getattr(ctx, "cwd", None) or getattr(ctx, "workdir", None)
        key = str(cwd or "")
        if key not in managers:
            # manager 活得比 handler 调用长,所以把 ctx 的 ui 捕获进 on_note:
            # 诊断信息要落在**会话的** notes 上(无前端时 notify 本来就是写 notes)。
            managers[key] = ServerManager(
                active(cwd), connect,
                on_note=(lambda text: ctx.ui.notify(text)) if notify else None)
        return managers[key]

    async def on_session_start(payload: dict, ctx: Any) -> None:
        await register_direct_tools(api, ctx, manager=manager_for(ctx, notify=True))

    api.on("session_start", on_session_start)
    api.registerTool(build_proxy_tool(manager_for))

    def on_command(args: str, ctx: Any) -> None:
        cwd = getattr(ctx, "cwd", None)
        specs = resolve(cwd)
        lines = [f"MCP 声明(共 {len(specs)} 个;层:全局 → 项目,同名项目覆盖)"]
        if not specs:
            lines.append(f"  (无)把一份 {MCP_FILE} 放到 ~/.qi/agent/ 或 <git 根>/.qi/")
        for spec in sorted(specs.values(), key=lambda s: s.name):
            flags = []
            if spec.disabled:
                flags.append("disabled")
            if spec.direct_tools:
                flags.append(f"directTools={spec.direct_tools}")
            unknown = unknown_fields(spec)
            if unknown:
                flags.append("未识别字段:" + ",".join(unknown))
            mark = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"  {spec.name}  ({spec.scope})  {spec.summary()}{mark}")
        lines.append("代理工具 `mcp` 已注册;直连的工具在会话开始时按 directTools 注册")
        ctx.ui.notify("\n".join(lines))

    api.registerCommand("mcp", on_command, description="列出 MCP 声明表(全局/项目两层)")
