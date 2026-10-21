"""A2A:上游的 Agent Card **不需要认证**(`GET /a2a/agents`),所以这一类未登录也能装。

落成 `a2a.json` 的一个条目。三种等价写法里选**最不需要假设的那一种**:

    {"cardUrl": "<base>/a2a/agents/<id>/card.json"}   需要"tenant 就是 agent id"这个假设
    {"baseUrl": "<origin>", "tenant": "<tenant>"}     卡片自己就给了两半,不猜

所以有 `tenant` + 接口 url 时用第二种;缺了才退回 `cardUrl`。
"""

from __future__ import annotations

from urllib.parse import urlparse

from ..actions import PatchJson, Planned
from ..catalog import StoreItem, content_sig
from ..client import StoreClient
from .base import PlanContext, check_name

KIND = "a2a"


def fetch(client: StoreClient) -> list[StoreItem]:
    cards = client.get_public("/a2a/agents")
    if isinstance(cards, dict):                    # 容错:上游若换成信封/分页体也认得
        cards = cards.get("items") or cards.get("data") or []
    if not isinstance(cards, list):
        return []
    return [_item(card) for card in cards if isinstance(card, dict)]


def _first_interface(card: dict) -> dict:
    interfaces = card.get("supportedInterfaces") or card.get("supported_interfaces") or []
    return interfaces[0] if interfaces and isinstance(interfaces[0], dict) else {}


def _item(card: dict) -> StoreItem:
    iface = _first_interface(card)
    tenant = str(iface.get("tenant") or "").strip() or None
    rpc_url = str(iface.get("url") or "").strip() or None
    name = str(card.get("name") or "").strip() or str(card.get("description") or "a2a")
    key = tenant or name
    skills = card.get("skills") or []
    return StoreItem(
        id=f"a2a:{key}",
        kind=KIND,
        name=name,
        description=str(card.get("description") or ""),
        version=content_sig(name, tenant, rpc_url, card.get("version")),
        updated_at=str(card.get("version") or ""),
        requires_config=False,
        badges={"streaming": bool((card.get("capabilities") or {}).get("streaming")),
                "skills": len(skills)},
        meta={"tenant": tenant, "rpc_url": rpc_url,
              "skills": [s.get("name") for s in skills if isinstance(s, dict)]},
    )


def _origin(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def build_entry(item: StoreItem, base_url: str) -> dict:
    tenant = item.meta.get("tenant")
    rpc_url = item.meta.get("rpc_url")
    origin = _origin(str(rpc_url)) if rpc_url else None
    if tenant and origin:
        return {"baseUrl": origin, "tenant": tenant}
    # 兜底:卡片没给 tenant 时用 cardUrl(此时只能假设 id 就是 tenant/agent id)
    key = str(item.id).split(":", 1)[-1]
    return {"cardUrl": f"{base_url.rstrip('/')}/a2a/agents/{key}/card.json"}


def plan(item: StoreItem, ctx: PlanContext) -> Planned:
    key = check_name(ctx.name_override or item.name, "A2A 条目")
    entry = build_entry(item, ctx.base_url)
    note = " · ".join(f"{k}={v}" for k, v in entry.items())
    return Planned(
        item=item,
        target=ctx.a2a_file(),
        actions=[PatchJson(ctx.a2a_file(), "a2aServers", key, entry)],
        needs_config=[],
        note=note,
    )
