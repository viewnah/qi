"""`a2a` 代理工具:一个约 200 token 的入口,按 action 调远端智能体。

为什么不给每个远端技能注册一个工具:A2A 名片里的 `skills` 可以很长,一个网关能挂十几个
智能体 —— 全塞进工具清单会在对话开始前烧掉半个上下文窗口(与 qi-mcp 同一个理由)。
默认只给一个代理;高频固定调用才用 `directTools` 开直连。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from qi_agent.extensions import Tool, ToolError

from . import config, state
from .card import (
    AgentCard,
    CardError,
    card_from_entry,
    cards_from_payload,
    describe,
    discovery_url,
    one_line,
    select_card,
)
from .client import A2AClient, RpcError, SendResult

#: 单个工具结果的上限:远端一次回几十 KB 会把上下文打爆。
MAX_TEXT = 40_000
#: `list` / `card` 最多列多少个智能体(网关可能挂很多)。
MAX_CARDS = 50

DESCRIPTION = """调用 A2A(Agent2Agent)协议的智能体。

`server` 是配置里的条目名(一个条目通常就是一个智能体),用 action="list" 查看。
- list:列出已配置的条目(不联网)
- card:看某个条目的 Agent Card(名字 / tenant / 技能 / JSON-RPC 端点)
- send:发一条消息;返回最终文本、状态、task_id 与 context_id
- resume:任务处于 input-required 时回传 decisions(approve / edit / reject)
- get / cancel:查询或取消任务

多轮对话请把上一次返回的 context_id 传回来(不传时本扩展会复用本会话最近一次)。"""

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "card", "send", "resume", "get", "cancel"],
            "description": "要做什么",
        },
        "server": {"type": "string", "description": "条目名(见 list);只配了一个时可省略"},
        "agent": {"type": "string", "description": "条目下有多个智能体时,用 tenant 或名字选一个"},
        "message": {"type": "string", "description": "send 要发出去的文本"},
        "context_id": {"type": "string", "description": "多轮对话的 contextId(可省略)"},
        "task_id": {"type": "string", "description": "resume / get / cancel 的任务 id"},
        "decisions": {
            "type": "array",
            "items": {"type": "object"},
            "description": "resume 的审批决策,如 [{\"type\":\"approve\"}]",
        },
        "discover": {
            "type": "boolean",
            "description": "list 时是否联网刷新名片(默认 false,只看配置)",
        },
    },
    "required": ["action"],
}


# ── 上下文 ──────────────────────────────────────────────

def _cwd(ctx: Any) -> Path:
    value = getattr(ctx, "workdir", None) or getattr(ctx, "cwd", None)
    return Path(value) if value else Path.cwd()


def _trusted(ctx: Any) -> bool:
    return bool(getattr(ctx, "project_trusted", True))


def _entries(ctx: Any) -> dict[str, config.A2AEntry]:
    return config.active(_cwd(ctx), trusted=_trusted(ctx))


def _pick_entry(entries: dict[str, config.A2AEntry], server: str) -> config.A2AEntry:
    if server:
        entry = entries.get(server)
        if entry is None:
            options = ", ".join(sorted(entries)) or "(无)"
            raise ToolError(f"找不到条目 {server!r};可选:{options}")
        return entry
    if len(entries) == 1:
        return next(iter(entries.values()))
    if not entries:
        raise ToolError(
            "没有任何 A2A 条目。写 ~/.qi/agent/a2a.json 或 <cwd>/.qi/a2a.json,"
            '例如 {"a2aServers": {"hikqin": {"baseUrl": "http://127.0.0.1:8080"}}}')
    options = ", ".join(sorted(entries))
    raise ToolError(f"配置了多个条目,请用 server 指定一个;可选:{options}")


# ── 名片 ────────────────────────────────────────────────

async def fetch_cards(client: A2AClient, entry: config.A2AEntry) -> list[AgentCard]:
    """拿条目的名片;没有发现地址时用配置合成一张。"""
    url = discovery_url(entry)
    if not url:
        return [card_from_entry(entry)]
    payload = await client.get_json(url)
    return cards_from_payload(payload, entry)


def _remember(ctx: Any, entry: config.A2AEntry, card: AgentCard, result: SendResult) -> None:
    state.remember_context(ctx, entry.name, card.tenant, result.context_id)
    if result.state == "input-required" and result.task_id:
        state.remember_task(ctx, entry.name, card.tenant, result.task_id)
    else:
        state.remember_task(ctx, entry.name, card.tenant, "")


# ── 结果渲染 ────────────────────────────────────────────

def _clip(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(截断,共 {len(text)} 字符)"


def _render_interrupt(result: SendResult) -> list[str]:
    payload = result.interrupt or {}
    lines = ["需要决策(input-required):"]
    actions = payload.get("action_requests") or []
    for index, item in enumerate(actions, start=1):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("action") or "(未命名)"
        ident = item.get("id") or ""
        args = item.get("args")
        args_text = json.dumps(args, ensure_ascii=False) if args is not None else "{}"
        lines.append(f"  {index}. {name}" + (f" id={ident}" if ident else "")
                     + f" args={args_text}")
    allowed: set[str] = set()
    for review in payload.get("review_configs") or []:
        if isinstance(review, dict):
            allowed.update(str(d) for d in (review.get("allowed_decisions") or []))
    lines.append(f"允许的 decisions:{', '.join(sorted(allowed)) or 'approve, edit, reject'}")
    task_hint = f'task_id="{result.task_id}", ' if result.task_id else ""
    lines.append(f'回传:a2a(action="resume", {task_hint}decisions=[{{"type": "approve"}}])')
    return lines


def format_result(result: SendResult, entry: config.A2AEntry) -> str:
    header = f"[{result.state or 'unknown'}] server={entry.name}"
    if result.task_id:
        header += f" task_id={result.task_id}"
    if result.context_id:
        header += f" context_id={result.context_id}"
    lines = [header]
    if result.text:
        lines += ["", _clip(result.text)]
    if result.interrupt:
        lines += ["", *_render_interrupt(result)]
    return "\n".join(lines)


# ── 动作 ────────────────────────────────────────────────

async def _list_entries(args: dict, ctx: Any,
                         client_factory: Callable[[config.A2AEntry], Any]) -> str:
    cwd = _cwd(ctx)
    trusted = _trusted(ctx)
    all_entries = config.resolve(cwd, trusted=trusted)
    active = {name: entry for name, entry in all_entries.items() if not entry.disabled}
    disabled = sorted(name for name, entry in all_entries.items() if entry.disabled)

    lines = [f"A2A 条目({len(active)} 个已启用):"]
    if not active:
        lines.append("  (无)")
    discover = bool(args.get("discover"))
    for name in sorted(active):
        entry = active[name]
        label = "项目" if entry.scope == "project" else "全局"
        lines.append(f"- {name}  [{label}]  {entry.summary()}")
        if entry.description:
            lines.append(f"    {entry.description}")
        if entry.wants_direct():
            lines.append(f"    直连工具:{config.entry_tool_name(entry)}")
        headers = entry.headers
        if headers:
            lines.append(f"    请求头:{', '.join(sorted(headers))}")
        unknown = config.unknown_fields(entry)
        if unknown:
            lines.append(f"    ⚠ 未识别字段:{', '.join(unknown)}(不会生效)")
        if discover:
            try:
                async with client_factory(entry) as client:
                    cards = await fetch_cards(client, entry)
                for card in cards[:MAX_CARDS]:
                    lines.append(f"    · {one_line(card)}")
            except (RpcError, CardError) as exc:
                lines.append(f"    ! 名片获取失败:{exc}")

    if disabled:
        lines.append(f"已关闭:{', '.join(disabled)}")

    lines.append("")
    for scope, path in config.scope_files(cwd):
        label = "全局" if scope == "global" else "项目"
        state_text = "" if path.is_file() else "(不存在)"
        lines.append(f"{label}: {path} {state_text}".rstrip())
    if not trusted:
        lines.append("项目未受信任:项目层 a2a.json 已跳过(用 `-a` 信任本次运行)")
    return "\n".join(lines)


async def _card_action(args: dict, ctx: Any,
                       client_factory: Callable[[config.A2AEntry], Any]) -> str:
    entry = _pick_entry(_entries(ctx), str(args.get("server") or ""))
    async with client_factory(entry) as client:
        cards = await fetch_cards(client, entry)
    agent = str(args.get("agent") or "")
    if agent:
        return describe(select_card(cards, agent))
    selected = cards[:MAX_CARDS]
    lines = [f"条目 {entry.name}({len(cards)} 个智能体):"]
    lines += [describe(card) for card in selected]
    if len(cards) > len(selected):
        lines.append(f"…(还有 {len(cards) - len(selected)} 个未列出)")
    return "\n".join(lines)


def _decisions(args: dict) -> list[dict] | None:
    raw = args.get("decisions")
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ToolError(f"decisions 不是合法 JSON:{exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ToolError("decisions 必须是非空数组")
    for item in raw:
        if not isinstance(item, dict):
            raise ToolError(f"decisions 的元素必须是对象,收到 {item!r}")
    return raw


async def send_with_entry(entry: config.A2AEntry, args: dict, ctx: Any,
                          client: A2AClient) -> str:
    """用**指定条目**发消息/回传决策(直连工具与代理工具共用这条路)。"""
    message = str(args.get("message") or "").strip()
    decisions = _decisions(args)
    task_id = str(args.get("task_id") or "")
    resume = bool(task_id and decisions) if args.get("action") is None else \
        str(args.get("action")).lower() == "resume"

    if resume:
        if not task_id:
            raise ToolError("resume 需要 task_id(上一次 input-required 返回的那个)")
        if not decisions:
            raise ToolError("resume 需要 decisions,例如 [{\"type\": \"approve\"}]")
    elif not message and not decisions:
        raise ToolError("send 需要 message")

    cards = await fetch_cards(client, entry)
    card = select_card(cards, str(args.get("agent") or ""))
    context_id = str(args.get("context_id") or "") or state.recall_context(
        ctx, entry.name, card.tenant)

    result = await client.send(card, message, context_id=context_id,
                               task_id=task_id, decisions=decisions)
    _remember(ctx, entry, card, result)
    return format_result(result, entry)


async def _send_action(args: dict, ctx: Any,
                       client_factory: Callable[[config.A2AEntry], Any], *,
                       resume: bool = False) -> str:
    entry = _pick_entry(_entries(ctx), str(args.get("server") or ""))
    payload = dict(args)
    if resume:
        payload["action"] = "resume"
    async with client_factory(entry) as client:
        return await send_with_entry(entry, payload, ctx, client)


async def _task_action(args: dict, ctx: Any,
                       client_factory: Callable[[config.A2AEntry], Any], *,
                       cancel: bool) -> str:
    entry = _pick_entry(_entries(ctx), str(args.get("server") or ""))
    task_id = str(args.get("task_id") or "")
    if not task_id:
        raise ToolError(f"{'cancel' if cancel else 'get'} 需要 task_id")
    async with client_factory(entry) as client:
        cards = await fetch_cards(client, entry)
        card = select_card(cards, str(args.get("agent") or ""))
        result = (await client.cancel_task(card, task_id) if cancel
                  else await client.get_task(card, task_id))
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    return f"[{'canceled' if cancel else 'task'}] server={entry.name}\n\n{_clip(text)}"


# ── 工具 ────────────────────────────────────────────────

def build_tool(client_factory: Callable[[config.A2AEntry], Any] | None = None) -> Tool:
    """构造代理工具。`client_factory` 只给测试注入假客户端用。"""
    factory = client_factory or A2AClient

    async def execute(args: dict, ctx: Any) -> str:
        action = str(args.get("action") or "").strip().lower()
        try:
            if action == "list":
                return await _list_entries(args, ctx, factory)
            if action == "card":
                return await _card_action(args, ctx, factory)
            if action == "send":
                return await _send_action(args, ctx, factory)
            if action == "resume":
                return await _send_action(args, ctx, factory, resume=True)
            if action == "get":
                return await _task_action(args, ctx, factory, cancel=False)
            if action == "cancel":
                return await _task_action(args, ctx, factory, cancel=True)
            raise ToolError(f"未知 action {action!r};可用:list / card / send / resume / get / cancel")
        except (config.ConfigError, CardError, RpcError) as exc:
            raise ToolError(str(exc)) from exc

    return Tool(
        name="a2a",
        description=DESCRIPTION,
        parameters=PARAMETERS,
        execute=execute,
        prompt_snippet="调用 A2A 智能体(先 list 看条目,再 send)",
        prompt_guidelines=[
            "a2a:调远端 A2A 智能体前先用 action=\"list\" 看条目名;"
            "多轮对话把上一次的 context_id 传回去;"
            "状态是 input-required 时必须用 action=\"resume\" 回传 decisions。",
        ],
    )


__all__ = ["build_tool", "fetch_cards", "format_result", "send_entry_direct"]


async def send_entry_direct(entry: config.A2AEntry, args: dict, ctx: Any,
                            client: A2AClient) -> str:
    """直连工具复用同一个发送路径(参数形状不同:没有 action/server)。"""
    payload = dict(args)
    payload["action"] = "resume" if (payload.get("task_id") and payload.get("decisions")) else "send"
    return await send_with_entry(entry, payload, ctx, client)
