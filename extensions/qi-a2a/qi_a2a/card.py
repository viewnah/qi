"""Agent Card:发现地址推导、解析、目标选择。

**一个条目通常就是一张卡**。三种拓扑都能落到同一套字段:

| 拓扑 | cardUrl | 形状 |
| --- | --- | --- |
| 标准单智能体服务 | `https://x/.well-known/agent-card.json` | 对象 |
| Hikqin 单个智能体 | `…/a2a/agents/<id>/card.json` | 对象(自带 tenant) |
| Hikqin 全平台 | `…/a2a/agents` | 数组 → 展开成 N 个 |

卡片解析**直接用官方 `a2a-sdk` 的 proto 类型**(`a2a.types.AgentCard`):协议侧的
`ClientFactory.create()` 就吃这个类型,自己再定义一套只会多一层翻译。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from a2a.types import AgentCapabilities
from a2a.types import AgentCard as AgentCardProto
from a2a.types import AgentInterface
from google.protobuf import json_format

from .config import A2AEntry

#: SDK 的 `ClientFactory` 默认只认这一个绑定,配置兜底时照它写。
PROTOCOL_BINDING = "JSONRPC"


class CardError(Exception):
    """卡片拿不到 / 形状不对 / 选不出目标。消息面向模型,**带条目名**。"""


@dataclass(frozen=True)
class AgentCard:
    """一张卡 + 它属于哪个条目。

    `proto` 是**官方 SDK 的类型**,`rpc_url` / `tenant` 等只是它的读视图 ——
    直连与代理都拿同一个对象去建客户端,不会出现"显示的是 A、调用的是 B"。
    """

    proto: AgentCardProto
    entry_name: str = ""

    @property
    def name(self) -> str:
        return self.proto.name or self.entry_name

    @property
    def description(self) -> str:
        return self.proto.description

    @property
    def tenant(self) -> str:
        for interface in self.proto.supported_interfaces:
            if interface.tenant:
                return interface.tenant
        return ""

    @property
    def rpc_url(self) -> str:
        for interface in self.proto.supported_interfaces:
            if interface.url:
                return interface.url
        return ""

    @property
    def skills(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "tags": [str(tag) for tag in skill.tags if tag],
            }
            for skill in self.proto.skills
        )


def discovery_url(entry: A2AEntry) -> str:
    """按条目推导“去哪里拿卡”;空串 = 没有发现地址(只能靠 rpcUrl + tenant)。"""
    if entry.card_url:
        return entry.card_url
    if entry.base_url and entry.tenant:
        return f"{entry.base_url}/a2a/agents/{quote(entry.tenant, safe='')}/card.json"
    if entry.base_url:
        return f"{entry.base_url}/a2a/agents"
    return ""


def _configured_rpc(entry: A2AEntry) -> str:
    return entry.rpc_url or (f"{entry.base_url}/a2a" if entry.base_url else "")


def _parse(raw: dict, entry: A2AEntry) -> AgentCardProto:
    try:
        proto = json_format.Parse(json.dumps(raw), AgentCardProto(),
                                  ignore_unknown_fields=True)
    except (json_format.ParseError, TypeError, ValueError) as exc:
        raise CardError(f"条目 `{entry.name}`:名片解析失败:{exc}") from exc
    return proto


def _interface(entry: A2AEntry, url: str) -> AgentInterface:
    """构造一个配置兜底用的 interface。

    `protocolBinding` **必须**写:SDK 的 `ClientFactory.create` 按它在
    `supported_protocol_bindings`(默认 `["JSONRPC"]`)里匹配,空串会直接
    报 `no compatible transports found` —— 而名片里的真卡自己带了这个字段。
    """
    return AgentInterface(url=url, tenant=entry.tenant, protocol_binding=PROTOCOL_BINDING)


def _fill_defaults(proto: AgentCardProto, entry: A2AEntry) -> None:
    """补上 SDK 建客户端必需的那一层:至少一个 interface(url 必填)。

    名片里给了 `supportedInterfaces` 就照它用;没有时才用配置兜底 ——
    反过来会盖掉服务端自己声明的端点。
    """
    if not proto.supported_interfaces:
        url = _configured_rpc(entry)
        if not url:
            raise CardError(
                f"条目 `{entry.name}` 的名片没有 supportedInterfaces[].url,"
                "配置里也没给 rpcUrl / baseUrl")
        proto.supported_interfaces.append(_interface(entry, url))
    if entry.tenant and not any(i.tenant for i in proto.supported_interfaces):
        proto.supported_interfaces[0].tenant = entry.tenant
    # 名片**没声明**能力时按配置的意图补 —— 否则 SDK 会安静地走非流式。
    # 用 `HasField` 而不是判真值:proto3 里 `streaming: false` 是明确声明,
    # 与服务端压根没写这个字段不同,后者才该由配置决定。
    if not proto.capabilities.HasField("streaming"):
        proto.capabilities.streaming = entry.streaming


def card_from_dict(raw: dict, entry: A2AEntry) -> AgentCard:
    """把一张卡(或租户卡)归一成 `AgentCard`。"""
    proto = _parse(raw, entry)
    _fill_defaults(proto, entry)
    return AgentCard(proto=proto, entry_name=entry.name)


def card_from_entry(entry: A2AEntry) -> AgentCard:
    """没有发现地址时的合成卡:只用配置里的 `rpcUrl` / `tenant`。

    零网络请求 —— 适合“端点已知、不想每次多一次 GET 名片”的场景。
    """
    url = _configured_rpc(entry)
    if not url:
        raise CardError(
            f"条目 `{entry.name}` 没有可用的端点:填 cardUrl、baseUrl 或 rpcUrl 之一")
    proto = AgentCardProto(
        name=entry.name,
        description=entry.description,
        supported_interfaces=[_interface(entry, url)],
        capabilities=AgentCapabilities(streaming=entry.streaming),
    )
    return AgentCard(proto=proto, entry_name=entry.name)


def cards_from_payload(payload: Any, entry: A2AEntry) -> list[AgentCard]:
    """解析发现端点的响应:对象 = 单卡,数组 = 多卡(网关)。"""
    if isinstance(payload, list):
        raws = [item for item in payload if isinstance(item, dict)]
        if not raws:
            raise CardError(f"条目 `{entry.name}`:名片列表为空,或元素不是对象")
        if entry.discovery == "single" and len(raws) > 1:
            raise CardError(
                f"条目 `{entry.name}` 声明 discovery=single,但端点返回了 {len(raws)} 张卡;"
                "去掉 discovery 或改成 list")
    elif isinstance(payload, dict):
        raws = [payload]
    else:
        raise CardError(f"条目 `{entry.name}`:名片响应既不是对象也不是数组")

    cards = [card_from_dict(raw, entry) for raw in raws]
    for card in cards:
        if not card.rpc_url:
            raise CardError(
                f"条目 `{entry.name}` 的名片 {card.name!r} 没有 supportedInterfaces[].url;"
                "补一个 rpcUrl 或换一张卡")

    if entry.tenant:
        matches = [card for card in cards if card.tenant == entry.tenant]
        if not matches:
            found = ", ".join(sorted({c.tenant or "(无 tenant)" for c in cards})) or "(空)"
            raise CardError(
                f"条目 `{entry.name}` 固定 tenant={entry.tenant},但卡里没有它;现有:{found}")
        cards = matches
    return cards


def select_card(cards: list[AgentCard], agent: str = "") -> AgentCard:
    """按 tenant 或名字选一个智能体;多个且没点名 → 报错并列出可选值。"""
    if not cards:
        raise CardError("没有可用的名片")
    if agent:
        key = agent.strip()
        matches = [c for c in cards if c.tenant == key or c.name.casefold() == key.casefold()]
        if not matches:
            options = ", ".join(f"{c.tenant or '(无 tenant)'}({c.name})" for c in cards)
            raise CardError(f"找不到智能体 {agent!r};可选:{options}")
        cards = matches
    if len(cards) == 1:
        return cards[0]
    options = ", ".join(f"{c.tenant or '(无 tenant)'}({c.name})" for c in cards)
    raise CardError(f"这个条目下有 {len(cards)} 个智能体,用 agent 指定一个;可选:{options}")


def one_line(card: AgentCard, limit: int = 100) -> str:
    """给面板/错误信息用的一行摘要。"""
    text = " ".join((card.description or "").split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    tenant = card.tenant or "(无 tenant)"
    return f"{card.name} [{tenant}]" + (f" — {text}" if text else "")


def describe(card: AgentCard, *, max_skills: int = 8) -> str:
    """多行展示:名字、tenant、rpc、技能。技能条目数有上限(防止一个网关打爆上下文)。"""
    lines = [one_line(card), f"  rpc: {card.rpc_url}"]
    skills = list(card.skills)
    for skill in skills[:max_skills]:
        tags = f" [{', '.join(skill['tags'])}]" if skill["tags"] else ""
        desc = f" — {skill['description']}" if skill["description"] else ""
        lines.append(f"  - {skill['name'] or skill['id']}{tags}{desc}")
    if len(skills) > max_skills:
        lines.append(f"  …({len(skills) - max_skills} 个技能未列出)")
    return "\n".join(lines)
