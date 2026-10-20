"""`directTools`:把条目注册成独立工具(`a2a__<条目>`)。

默认**不开** —— 一个条目一个工具,条目多了会挤占系统提示词与工具清单。只有高频固定调用
(比如“每天问同一个数据分析智能体”)才值得开。

注册放在 `session_start`,但**不联网**:工具描述来自配置,名片在真正调用时才取。
这样项目未受信任、网关临时挂掉都不会拖慢启动。
"""

from __future__ import annotations

from typing import Any

from qi_agent.extensions import Tool, ToolError

from . import config
from .card import CardError
from .client import A2AClient, RpcError
from .proxy import send_entry_direct

DIRECT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "要发出去的文本"},
        "context_id": {"type": "string", "description": "多轮对话的 contextId(可省略)"},
        "task_id": {"type": "string", "description": "恢复中断任务时的 task id"},
        "decisions": {
            "type": "array",
            "items": {"type": "object"},
            "description": "恢复时回传的审批决策,如 [{\"type\":\"approve\"}]",
        },
    },
    "required": ["message"],
}


def _note(api: Any, ctx: Any, text: str) -> None:
    """提示走 ctx.ui(没有前端时会落进启动 notes),与 qi-mcp 同一条通道。"""
    ui = getattr(ctx, "ui", None)
    if ui is not None and callable(getattr(ui, "notify", None)):
        ui.notify(text)
        return
    notes = getattr(ctx, "notes", None)
    if isinstance(notes, list):
        notes.append(text)


def _make_execute(entry: config.A2AEntry):
    async def execute(args: dict, ctx: Any) -> str:
        try:
            async with A2AClient() as client:
                return await send_entry_direct(entry, args, ctx, client)
        except (config.ConfigError, CardError, RpcError) as exc:
            raise ToolError(str(exc)) from exc

    return execute


def register_direct(api: Any, entries: dict[str, config.A2AEntry], ctx: Any) -> list[str]:
    """给 `directTools` 打开的条目注册工具,返回本次注册成功的名字。"""
    added: list[str] = []
    existing = set(api.catalog.names)
    current = list(api.get_active_tools())
    # 判定基准必须在注册前取:注册完再比,"当前集合 === catalog" 会因自己刚加的工具
    # 而永远不成立(判据等于没写)。同 qi-mcp 的处理。
    narrowed = set(current) != existing

    for entry in entries.values():
        if not entry.wants_direct():
            continue
        name = config.entry_tool_name(entry)
        if name in existing:
            _note(api, ctx, f"直连工具 {name} 与已有工具同名,跳过(给条目设 toolPrefix)")
            continue
        api.register_tool(Tool(
            name=name,
            description=(entry.description or f"调用 A2A 智能体 {entry.name}"),
            parameters=DIRECT_PARAMETERS,
            execute=_make_execute(entry),
            prompt_snippet=f"调用 A2A 智能体 {entry.name}",
        ))
        existing.add(name)
        added.append(name)

    if added and narrowed:
        # `setActiveTools` 是**覆盖**,不是追加:无条件调用会把之后动态注册的工具挡在外面。
        api.set_active_tools([*current, *[n for n in added if n not in current]])
    return added
