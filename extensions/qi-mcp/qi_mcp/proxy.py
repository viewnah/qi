"""`mcp` 代理工具:先 `search` 发现,再 `call` 调用(E25 定的默认暴露方式)。

**为什么是一个代理工具而不是每个 server 注册自己的工具**:上下文。一个 server 的工具定义
轻松 10k+ token,连几个 server 就在对话开始前烧掉半个窗口 —— 而多数工具这次对话用不到。
代理工具本身约 200 token,工具名与参数按需浮现。

调用形状(与 pi-mcp-adapter 一致):

    mcp({ search: "screenshot" })                            → 列出匹配的工具
    mcp({ tool: "mcp__chrome__take_screenshot", args: {...} }) → 调它
    mcp({})                                                  → 用法 + 概览(不列全部工具名)

`args` 收对象,也收 JSON 字符串(pi 两种都支持:有的 provider 对复杂 schema 更稳)。
"""

from __future__ import annotations

import json
from typing import Any

from qi_agent.extensions import Tool

from .servers import ManagerError, ServerManager, ToolInfo, split_qualified

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "search": {"type": "string",
                   "description": "按关键词找工具(匹配工具名与描述)"},
        "tool": {"type": "string",
                 "description": "要调用的工具名,形如 mcp__<server>__<tool>"},
        "args": {"type": ["object", "string"],
                 "description": "工具参数(对象,或 JSON 字符串)"},
    },
}

_DESC = ("访问 MCP server 的工具。本工具是一个**代理**:先用 search 找到要用的工具,"
         "再用 tool + args 调用 —— 这样不必把每个 MCP server 的全部工具定义都塞进上下文。")


def _render_params(schema: dict) -> str:
    """把 inputSchema 压成几行(pi 的呈现风格:名字 + 类型/enum + 默认值)。"""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return ""
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    lines = ["  Parameters:"]
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        kind = spec.get("type", "any")
        if isinstance(spec.get("enum"), list):
            kind = "enum: " + ", ".join(json.dumps(v, ensure_ascii=False)
                                        for v in spec["enum"])
        bits = [f"    {name} ({kind})"]
        if name in required:
            bits.append("(必填)")
        if "default" in spec:
            bits.append(f"[默认 {json.dumps(spec['default'], ensure_ascii=False)}]")
        if spec.get("description"):
            bits.append(f"- {spec['description']}")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def _render_tool(info: ToolInfo) -> str:
    head = f"{info.qualified}\n  {info.description}".rstrip()
    params = _render_params(info.schema)
    return f"{head}\n{params}" if params else head


def _overview(infos: list[ToolInfo], manager: ServerManager) -> str:
    """概览**只给每个 server 的工具数**,不列名字 —— 列名字正是这个工具要省掉的东西。"""
    counts: dict[str, int] = {}
    for info in infos:
        counts[info.server] = counts.get(info.server, 0) + 1
    if not counts:
        parts = ["当前没有可用的 MCP server 工具。"]
        if manager.failed:
            parts.append("连接失败的:" + "; ".join(f"{k}({v})" for k, v in manager.failed.items()))
        if not manager.specs:
            parts.append("还没有声明 —— 把 mcp.json 放到 ~/.qi/agent/ 或 <git 根>/.qi/。")
        return " ".join(parts)
    listed = ", ".join(f"{srv}({n})" for srv, n in sorted(counts.items()))
    text = f"已连接 {len(counts)} 个 MCP server,共 {len(infos)} 个工具:{listed}。"
    if manager.failed:
        text += " 连接失败的:" + "; ".join(f"{k}({v})" for k, v in manager.failed.items())
    return text + " 用 `search` 找工具,再用 `tool` + `args` 调用。"


def _parse_args(raw: Any) -> dict:
    """`args` 收对象或 JSON 字符串;坏 JSON 给可读错误(别抛栈)。"""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManagerError(f"args 不是合法 JSON:{exc}. 收到的前 80 字:{text[:80]!r}") from exc
        if not isinstance(parsed, dict):
            raise ManagerError(f"args 必须是 JSON 对象,收到的是 {type(parsed).__name__}")
        return parsed
    raise ManagerError(f"args 要传对象或 JSON 字符串,收到 {type(raw).__name__}")


async def _search(manager: ServerManager, query: str) -> str:
    infos = await manager.tools()
    hits = [i for i in infos if i.matches(query)]
    if not hits:
        return f"没有工具匹配 “{query}”。{_overview(infos, manager)}"
    return "\n\n".join(_render_tool(i) for i in hits)


def _pick(infos: list[ToolInfo], name: str) -> ToolInfo | ManagerError:
    """把调用名解析到一个工具:优先 `mcp__srv__tool`,再退到裸工具名(歧义时报清楚)。"""
    split = split_qualified(name)
    if split is not None:
        server, tool = split
        for info in infos:
            if info.server == server and info.name == tool:
                return info
        # 没找到:给**同 server 的相近名字**。名字打错是这里最常见的错,而只说
        # “不存在”会让模型再猜一次 —— 猜一次的代价就是一次往返。
        near = [i.qualified for i in infos if i.server == server
                and (tool.lower() in i.name.lower() or i.name.lower() in tool.lower())]
        hint = (f"名字相近的有:{', '.join(near[:5])}" if near
                else f"`{server}` 已连上,但它没有这个工具")
        return ManagerError(f"`{name}` 不存在。{hint}")
    bare = [i for i in infos if i.name == name]
    if len(bare) == 1:
        return bare[0]
    if not bare:
        near = [i.qualified for i in infos if name.lower() in i.name.lower()][:5]
        hint = f"名字相近的有:{', '.join(near)}" if near else "用 `search` 找找。"
        return ManagerError(f"没有叫 `{name}` 的工具。{hint}")
    return ManagerError(f"`{name}` 在多个 server 里都有,请用全名调用:"
                        + ", ".join(i.qualified for i in bare))


async def _call(manager: ServerManager, name: str, raw_args: Any) -> str:
    args = _parse_args(raw_args)
    infos = await manager.tools()
    picked = _pick(infos, name)
    if isinstance(picked, ManagerError):
        raise picked
    return await manager.call(picked.server, picked.name, args)


def build_tool(manager: ServerManager) -> Tool:
    """造出那个 `mcp` 代理工具。`manager` 由宿主(runtime 侧)按会话 cwd 提供。"""

    async def execute(args: dict, ctx: Any) -> str:
        # 错误一律**变成结果文本**回给模型,而不是抛出去 —— 让模型能自己改正(改关键词、
        # 补参数、换个工具),而不是把一次调用变成一次回合失败。
        try:
            search = args.get("search")
            if isinstance(search, str) and search.strip():
                return await _search(manager, search.strip())
            tool = args.get("tool")
            if isinstance(tool, str) and tool.strip():
                return await _call(manager, tool.strip(), args.get("args"))
            infos = await manager.tools()
        except ManagerError as exc:
            return f"mcp 调用失败:{exc}"
        usage = ('用法:`mcp({search:"关键词"})` 找工具;'
                 '`mcp({tool:"mcp__<server>__<tool>", args:{…}})` 调用。')
        return f"{usage} {_overview(infos, manager)}"

    return Tool("mcp", _DESC, _SCHEMA, execute,
                prompt_snippet="访问 MCP server 的工具(先 search 再 tool+args 调用)")
