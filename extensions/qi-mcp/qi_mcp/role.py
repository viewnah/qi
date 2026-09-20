"""按值注入:角色**自己那层** mcp.json 交给 qi-mcp(E25 的最后一块)。

## 分工(E25 定死)

- **qi-agents** 拥有 agent 目录这个"自包含包"(`agent.md` + `skills/` + `mcp.json`),
  所以**由它读** `<角色目录>/mcp.json` —— 包主人点自己的东西,不是泄漏;
- **qi-mcp** 只认 server 定义与协议:收到按值传进来的声明就负责连与注册。
  它**从不去遍历 `.qi/agents/`**。

## 角色的 server 集合

= qi 两层(`~/.qi/agent/mcp.json` → `<git 根>/.qi/mcp.json`)**加上**角色私有的那份,
**角色私有同名覆盖 qi 级** —— 更具体的那份赢(v1 也是这个口径:agent 私有自动绑定)。

## 角色 `tools:` 里怎么写才拿到 MCP(E24 → E25 的裁决)

| 写到 | 拿到什么 |
| --- | --- |
| 什么都不写 | **没有任何 MCP 访问**(默认拒绝;连都不连) |
| `mcp` | 那个全局代理工具(覆盖 qi 两层);**角色私有 server 的工具额外直连** —— 因为全局代理看不见它们 |
| `mcp__github__*` / `mcp__github__create_issue` | 直连注册匹配的工具(**不给代理**) |

`mcp__<server>__<模式>` 里的模式按 `fnmatch` 匹配工具名(`*` / `?` 都行)。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import MCP_FILE, ConfigError, ServerSpec, read_file, resolve
from .direct import prefix_mode, tool_name
from .proxy import PROXY_NAME
from .servers import ManagerError, ServerManager

MCP_PREFIX = "mcp__"

#: 按 `(cwd, 角色目录)` 缓存 manager —— **连接复用与元数据缓存都挂在它身上**,
#: 所以同一角色跑多次不该重连。缓存由本模块持有:manager 是 qi-mcp 的资源,
#: 让调用方(qi-agents)去管它会多一个回调、而且那个回调拿不到 specs(本模块才算)。
_MANAGERS: dict[str, ServerManager] = {}


def reset_managers() -> None:
    """清缓存(测试用:每个用例要一个干净的连接世界)。"""
    _MANAGERS.clear()


def role_specs(role_dir: Path | None, cwd: Path | None = None) -> tuple[dict[str, ServerSpec],
                                                                       set[str]]:
    """合并 qi 两层 + 角色私有。返回 `(全部声明, 角色私有的名字集合)`。

    第二个返回值是"哪些是角色私有"—— 代理工具与它们的关系不同(见模块头:全局代理看不见
    角色私有 server,所以那些得直连注册)。

    **用 `resolve()` 而不是 `active()`**:`disabled` 的也要进表,否则角色点名了一个被关掉的
    server 会得到**静默**——而"你要的那个被我关了"正是该说出来的那种话。能不能连由
    `ServerManager` 与这里的注册循环各自把关(两处都查 `disabled`)。
    """
    table = dict(resolve(cwd))
    private: set[str] = set()
    if role_dir is not None:
        path = Path(role_dir) / MCP_FILE
        if path.is_file():
            for name, config in read_file(path).items():
                table[name] = ServerSpec(name=name, config=config, scope="project", path=path)
                private.add(name)
    return table, private


def mcp_entries(role_tools: list[str] | None) -> list[tuple[str, str | None]]:
    """从角色 `tools:` 里挑出 MCP 相关条目 → `[("proxy", None) | ("direct", 模式)]`。

    没有 MCP 条目就返回空 —— 调用方据此**一行 MCP 工作都不做**(不连、不列、不注册)。
    """
    out: list[tuple[str, str | None]] = []
    for entry in role_tools or []:
        text = str(entry).strip()
        if text == PROXY_NAME:
            out.append(("proxy", None))
        elif text.startswith(MCP_PREFIX):
            out.append(("direct", text))
    return out


def _split_pattern(pattern: str) -> tuple[str, str]:
    """`mcp__gh__create_*` → `("gh", "create_*")`;`mcp__gh__*` → `("gh", "*")`。"""
    rest = pattern[len(MCP_PREFIX):]
    server, sep, tools = rest.partition("__")
    return (server, tools) if sep else (server, "*")


async def register_role_mcp(api: Any, *, role_dir: Path | None = None,
                            cwd: Path | None = None, role_tools: list[str] | None = None,
                            on_note: Callable[[str], None] | None = None) -> list[str]:
    """把该角色的 MCP 工具注册进 catalog,返回**该角色该用哪些工具名**。

    返回的名字由调用方(qi-agents)放进子运行的 `RunSpec.tools` —— 这是"只给这一次子运行"
    的关键:注册只让工具**可被点名**,而真正的隔离在工具清单里。
    """
    entries = mcp_entries(role_tools)
    if not entries:
        return []
    note = on_note or (lambda text: None)
    try:
        specs, private = role_specs(role_dir, cwd)
    except ConfigError as exc:
        note(f"角色目录的 {MCP_FILE} 读不了:{exc}")
        return []
    if not specs:
        note("角色要了 MCP,但没有任何 mcp.json 声明(全局/项目/角色目录都没有)")        
        return []

    key = f"{cwd}|{role_dir}"
    if key not in _MANAGERS:
        _MANAGERS[key] = ServerManager(specs, _default_connect(), on_note=note)
    manager = _MANAGERS[key]
    existing = set(api.catalog.names)
    names: list[str] = []

    async def _register(server: str, patterns: list[str]) -> None:
        from qi_agent.extensions import Tool

        spec = specs.get(server)
        if spec is None:
            note(f"角色要的 MCP server `{server}` 不在声明表里")
            return
        if spec.disabled:
            note(f"角色要的 MCP server `{server}` 已 disabled")
            return
        infos = await manager.tools_of(server)
        if patterns != ["*"]:
            infos = [i for i in infos if any(_glob(p, i.name) for p in patterns)]
        for info in infos:
            name = tool_name(info.server, info.name, prefix_mode(spec))
            if name in existing:
                note(f"MCP 工具 {name} 与已有工具同名,跳过")
                continue

            async def execute(args: dict, ctx: Any, _info=info) -> str:
                try:
                    return await manager.call(_info.server, _info.name, args)
                except ManagerError as exc:
                    return f"mcp 调用失败:{exc}"

            api.registerTool(Tool(name, info.description or f"{info.server} 的 {info.name}",
                                  info.schema or {"type": "object", "properties": {}},
                                  execute))
            existing.add(name)
            names.append(name)

    for kind, pattern in entries:
        if kind == "proxy":
            names.append(PROXY_NAME)
            # 全局代理只看得到 qi 两层 → 角色私有的那些必须直连才拿得到
            for server in sorted(private):
                await _register(server, ["*"])
        elif pattern:
            server, tool_pattern = _split_pattern(pattern)
            await _register(server, [tool_pattern])
    return list(dict.fromkeys(names))          # 去重且保持顺序


def _glob(pattern: str, name: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(name, pattern) or fnmatch(f"mcp__{name}", pattern)


def _default_connect() -> Any:
    from .client import connect

    return connect
