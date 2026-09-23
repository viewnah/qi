"""qi-mcp:qi 的 MCP 支持(形状照搬 `pi-mcp-adapter`,见 docs/extensions.md E25)。

分五个切片做,全部落地:

| 切片 | 内容 | 状态 |
| --- | --- | --- |
| 1 | 两层声明表 + `/mcp` 面板(`/mcp tools` 列工具) | ✅ |
| 2 | `mcp` 代理工具(search / describe / call)+ lazy 连接 | ✅ |
| 2b/2c | 真客户端:stdio + Streamable HTTP(端到端跑真 server) | ✅ |
| 3 | `directTools` 直连 + `includeTools`/`excludeTools` + `toolPrefix` | ✅ |
| 4 | 给 qi-agents 的**按值注入** API(它读角色目录的 mcp.json 交给这里) | ✅ |

为什么默认是**一个代理工具**而不是每 server 注册 N 个工具:`pi-mcp-adapter` 的实测理由是
**上下文** —— 一个 server 的工具定义轻松 10k+ token,连几个 server 就在对话开始前烧掉半个
上下文窗口。代理工具约 200 token,模型按需 `search` 发现、再 `call`。直连是每 server
`directTools` 显式开的("这些我常用,值得占着上下文")。
"""

from __future__ import annotations

from typing import Any

import asyncio

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
from .role import close_managers, register_role_mcp, role_specs
from .servers import ManagerError, ServerManager, ToolInfo

__all__ = ["register", "register_direct_tools", "register_role_mcp", "role_specs",
           "resolve", "active", "load_layers", "scope_files", "ServerSpec", "ConfigError",
           "MCP_FILE", "McpScope", "tool_name", "select_tools", "direct_selection"]


def _one_line(text: Any, limit: int = 88) -> str:
    """把描述压成一行(给 `/mcp tools` 清单用)。

    工具描述经常是多行/带空行的(有些 server 把整段 Markdown 塞在 description 里),
    直接进面板会把清单抻成一篇文档 —— 而这里要的是“一眼扫过有哪些工具”。
    """
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


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
    # **判定基准要在注册之前取**:注册完再比,"集合 === catalog" 会因为自己刚加进去的工具
    # 而永远不成立(那样这条判据等于没写)。`current` 也在这里取,后面直接用。
    current = list(api.getActiveTools())
    narrowed = set(current) != existing
    wanted = [(name, spec) for name, spec in manager.specs.items() if spec.direct_tools]
    # **先并发把工具拿到,再登记**。`tools_of` 现在是“只连这一个”(不再顺带连全部),
    # 所以串行循环会把 N 个 server 的启动时间相加 —— 与 `tools()` 同一个理由。
    # 顺序仍按声明表(`gather` 保序),登记顺序因此可预测。
    listed = await asyncio.gather(*(manager.tools_of(name) for name, _spec in wanted))
    for (name, spec), infos in zip(wanted, listed, strict=True):
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
    if added and narrowed:
        # **只在"工具集被显式收窄过"时才去动它** —— 这一步以前无条件执行,会把这批名字
        # 变成一个**静态覆盖列表**(`setActiveTools` 是"覆盖",不是"加进去"):此后任何
        # 动态注册的工具(另一个扩展的、或本扩展下一条 `session_start` 加的)都被挡在外面,
        # 而症状是"装了新工具却调不到",不报错。
        # 判据见上:`register` 之前 集合 === catalog = 没人收窄过 → catalog 里的工具本来就在。
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
        manager = managers.get(key)
        if manager is None:
            # manager 活得比 handler 调用长,所以把 ctx 的 ui 捕获进 on_note:
            # 诊断信息要落在**会话的** notes 上(无前端时 notify 本来就是写 notes)。
            manager = managers[key] = ServerManager(active(cwd), connect)
        if notify:
            # **后补也要生效**:代理工具那条路(notify=False)可能先把 manager 建出来,
            # 那时不补上,后面的连接失败/列工具失败就再也没处去(静默丢掉)。
            manager.set_on_note(lambda text: ctx.ui.notify(text))
        return manager

    async def on_session_start(payload: dict, ctx: Any) -> None:
        await register_direct_tools(api, ctx, manager=manager_for(ctx, notify=True))

    async def on_session_shutdown(payload: dict, ctx: Any) -> None:
        """退出 / 换会话 / 重载前**关掉真连接**。

        不接这个事件的话，stdio 的 `npx`/`node` 子进程与 HTTP 连接池会一直留着 ——
        `ServerManager.aclose()` 早就写好也测过，但没有**任何**调用方。
        两层缓存都要收：本模块闭包里的 `managers`（每 cwd 一个）与
        `role.py` 的模块级 `_MANAGERS`（每个「角色目录」一个，只增不减）。
        """
        await close_managers()                       # 角色私有那层
        cached = list(managers.values())
        managers.clear()                             # 先清表：清完就没人再能拿到它了
        for manager in cached:
            await manager.aclose()                   # 内部逐个容错，一个关不掉不连坐

    api.on("session_start", on_session_start)
    api.on("session_shutdown", on_session_shutdown)
    api.registerTool(build_proxy_tool(manager_for))

    # ── `/mcp` 面板 ──────────────────────────────────────
    #
    # 两个子命令的分工是"快/慢"：
    #   `/mcp`           声明表 —— 纯读 JSON，**零 I/O**，随时按随时回；
    #   `/mcp tools`     工具清单 —— 必须真连 server 才知道（元数据缓存是进程内的，
    #                    且首个 `search` 之前是空的），所以它**会等**，单独一条路。
    # 所以别把 `tools` 并进默认输出：那样连一个坏 server 都会让 `/mcp` 变慢。
    #
    # 命令与代理**工具**同名（都叫 `mcp`）不影响：两者不在同一个命名空间
    # （命令进 `CommandRegistry`，工具进 `ToolCatalog`），pi 就是这个形状。

    def _declared(ctx: Any, cwd: Any) -> None:
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
        lines.append("用 `/mcp tools` 看它们到底有哪些工具")
        ctx.ui.notify("\n".join(lines))

    async def _tools(rest: list[str], ctx: Any, cwd: Any) -> None:
        specs = active(cwd)
        if not specs:
            ctx.ui.notify(f"没有启用的 MCP server。先写一份 {MCP_FILE}"
                          "(~/.qi/agent/ 或 <git 根>/.qi/),或用 `/mcp` 看现有声明。")
            return
        wanted = rest[0] if rest else None
        if wanted is not None and wanted not in specs:
            # 名字打错就**不要**默默列全部 —— 那会让人以为写对了
            ctx.ui.notify(f"没有叫 `{wanted}` 的 server。可用的:"
                          + ", ".join(sorted(specs)))
            return

        manager = manager_for(ctx, notify=True)
        names = [wanted] if wanted else sorted(specs)
        if wanted is not None:
            per_server = {wanted: await manager.tools_of(wanted)}
        else:
            # 没点名就一次性并发连全部(`tools_of` 是“只要那一个”,循环调它会变成串行)
            every = await manager.tools()
            per_server = {n: [i for i in every if i.server == n] for n in names}
        lines: list[str] = []
        total = 0
        for name in names:
            spec = specs[name]
            infos = per_server.get(name, [])      # 已经过 include/exclude 过滤
            if name not in manager.failed:
                total += len(infos)
            lines.append(f"  {name} · {len(infos)} 个")
            # 标出哪些是**直连**的（直接出现在工具清单里，不用先 search）—— 没标的是
            # 代理可调。不然“我在工具清单里怎么找不到它”只能靠猜。
            direct = {info.qualified for info in direct_selection(infos, spec)}
            for info in sorted(infos, key=lambda i: i.name):
                flag = "  [直连]" if info.qualified in direct else ""
                lines.append(f"    {info.qualified}{flag} — {_one_line(info.description)}")

        head = f"MCP 工具({len([n for n in names if n not in manager.failed])} 个 server,共 {total} 个)"
        tail = ["用 `mcp({tool:\"<名字>\", args:{…}})` 调用,或 `mcp({search:\"关键词\"})` 找"]
        if manager.failed:
            # 失败原因**自己读 `failed`**，不依赖 `on_note` —— 那条路只归建 manager 的那次
            # 调用（`notify=True`），之后的调用拿不到，失败就静默了。
            tail.append("连接失败的:" + "; ".join(
                f"{k}({v})" for k, v in sorted(manager.failed.items()) if k in names))
        ctx.ui.notify("\n".join([head, *lines, *tail]))

    def _usage(ctx: Any) -> None:
        ctx.ui.notify("MCP 面板用法:\n"
                      "  /mcp                  声明表（全局/项目两层，不连接）\n"
                      "  /mcp tools [server]   列出工具（会按需连接 server）")

    _SUBCOMMANDS = ("tools", "tool")

    async def on_command(args: str, ctx: Any) -> None:
        # async 是因为 `tools` 要连 server；宿主对同步/异步两种 handler 都收
        # （`CommandHandler` 的契约），所以这里没有额外代价。
        cwd = getattr(ctx, "cwd", None)
        argv = (args or "").split()
        head = argv[0].lower() if argv else ""
        if head in _SUBCOMMANDS:
            await _tools(argv[1:], ctx, cwd)
        elif head in ("help", "?"):
            _usage(ctx)
        elif head:
            _usage(ctx)                      # 不认识的子命令：说清楚，不默默当没参数
        else:
            _declared(ctx, cwd)

    def _completions(prefix: str) -> list[dict]:
        """补 ``/mcp `` 后面的第一个词（宿主只对第一个参数调补全）。"""
        text = (prefix or "").strip().lower()
        options = (("tools", "列出工具（会按需连接 server）"),
                   ("help", "用法"))
        return [{"value": value, "label": value, "description": desc}
                for value, desc in options if value.startswith(text)]

    # 命令叫 `mcp`（与 pi 一致）—— 代理**工具**也叫 `mcp`，但两者不在同一个
    # 命名空间（命令进 `CommandRegistry`，工具进 `ToolCatalog`），不会撞。
    api.registerCommand("mcp", on_command,
                        description="MCP 面板:声明表 / 工具清单（`/mcp tools`）",
                        get_argument_completions=_completions)
