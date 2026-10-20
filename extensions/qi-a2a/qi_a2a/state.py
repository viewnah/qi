"""进程内的会话便利缓存:`contextId` / `taskId`。

A2A 的 `contextId` 是**多轮对话的连续性**所在。工具结果里本来就会把 id 回给模型,
但模型常常只回一句“继续” —— 于是这里按 `(会话, 条目, tenant)` 记一份**最近值**,
省掉一次显式参数。它只是便利层:**模型传了就以参数为准**,进程重启即失,
真正的跨重启恢复靠对话历史里那个 id。
"""

from __future__ import annotations

from typing import Any

#: 上限只是防止长进程里无限增长;条目/租户数量本来就很有限。
_LIMIT = 200

_CONTEXTS: dict[tuple[str, str, str], str] = {}
_TASKS: dict[tuple[str, str, str], str] = {}


def _session_id(ctx: Any) -> str:
    env = getattr(ctx, "session_env", None) or {}
    return str(env.get("QI_SESSION_ID") or env.get("QI_SESSION_FILE") or "")


def _key(ctx: Any, entry: str, tenant: str) -> tuple[str, str, str]:
    return (_session_id(ctx), entry, tenant)


def _trim(table: dict[tuple[str, str, str], str]) -> None:
    while len(table) > _LIMIT:
        table.pop(next(iter(table)), None)


def remember_context(ctx: Any, entry: str, tenant: str, context_id: str) -> None:
    if not context_id:
        return
    _CONTEXTS[_key(ctx, entry, tenant)] = context_id
    _trim(_CONTEXTS)


def recall_context(ctx: Any, entry: str, tenant: str) -> str:
    return _CONTEXTS.get(_key(ctx, entry, tenant), "")


def remember_task(ctx: Any, entry: str, tenant: str, task_id: str) -> None:
    if not task_id:
        return
    _TASKS[_key(ctx, entry, tenant)] = task_id
    _trim(_TASKS)


def recall_task(ctx: Any, entry: str, tenant: str) -> str:
    return _TASKS.get(_key(ctx, entry, tenant), "")


def reset() -> None:
    """清空缓存(测试用)。"""
    _CONTEXTS.clear()
    _TASKS.clear()
