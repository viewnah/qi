"""MCP server:上游只给 `transport` / `url` / `headers`(明文),**永不下发 `auth_config`**。

按"上游给没给"分治(README §4.3):

* 给了的(`headers`)→ **原样抄**,不猜、不改写成变量;
* 没给的(`auth_config` 里的真实令牌)→ 写 `${VAR}` / `bearerTokenEnv` 引用,
  因为 qi-mcp 对这两种写法有"缺了就在发请求前报错"的保护,而空字符串会静默 401。
"""

from __future__ import annotations

from ..actions import PatchJson, Planned
from ..catalog import StoreItem, content_sig
from ..client import StoreClient
from .base import PlanContext, check_name, env_name

KIND = "mcp"
PAGE_SIZE = 100
MAX_PAGES = 50

#: 上游 transport → qi-mcp/config.py 的 `type`。`sse` 认得出,但 qi-mcp 连接时会明确报未实现。
TYPE_MAP = {"streamable-http": "streamable-http", "sse": "sse", "stdio": "stdio"}


def fetch(client: StoreClient) -> list[StoreItem]:
    items: list[StoreItem] = []
    page = 1
    while page <= MAX_PAGES:
        data = client.get("/mcp/servers", {"page": page, "page_size": PAGE_SIZE})
        if not isinstance(data, dict):
            break
        batch = data.get("items") or []
        items.extend(_item(raw) for raw in batch if isinstance(raw, dict))
        total = int(data.get("total") or 0)
        if len(batch) < PAGE_SIZE or len(items) >= total:
            break
        page += 1
    return items


def _item(raw: dict) -> StoreItem:
    server = dict(raw)
    name = str(server.get("name") or "")
    needs = _needs_config(server)
    return StoreItem(
        id=f"mcp:{server.get('id')}",
        kind=KIND,
        name=name,
        description=str(server.get("description") or ""),
        version=content_sig(server.get("url"), server.get("transport"),
                            server.get("headers"), server.get("auth_type"),
                            server.get("updated_at")),
        updated_at=str(server.get("updated_at") or ""),
        requires_config=bool(needs),
        badges={"tools": server.get("tool_count"),
                "connection": server.get("connection_status"),
                "transport": server.get("transport")},
        meta={"server": server},
    )


def _header_has(headers: object, name: str) -> bool:
    if not isinstance(headers, dict):
        return False
    return any(str(key).lower() == name for key in headers)


def _needs_config(server: dict) -> list[str]:
    """哪些环境变量需要用户自己补。`headers` 里已经有凭据的就不算。"""
    auth = str(server.get("auth_type") or "none")
    headers = server.get("headers")
    if auth == "bearer" and not _header_has(headers, "authorization"):
        return [env_name(str(server.get("name") or ""), "TOKEN")]
    if auth in ("apikey", "custom") and not headers:
        return [env_name(str(server.get("name") or ""), "API_KEY")]
    if auth == "basic" and not _header_has(headers, "authorization"):
        return [env_name(str(server.get("name") or ""), "BASIC")]
    return []


def build_entry(server: dict) -> tuple[dict, list[str]]:
    """上游 server → qi-mcp 的 `mcpServers.<名>`。返回 `(entry, needs_config)`。"""
    entry: dict = {}
    transport = str(server.get("transport") or "streamable-http")
    entry["type"] = TYPE_MAP.get(transport, transport)
    if server.get("url"):
        entry["url"] = str(server["url"])
    headers = server.get("headers")
    if isinstance(headers, dict) and headers:
        entry["headers"] = {str(k): str(v) for k, v in headers.items()}   # 原样抄
    needs = _needs_config(server)
    auth = str(server.get("auth_type") or "none")
    if needs:
        env = needs[0]
        if auth == "bearer":
            entry["auth"] = "bearer"
            entry["bearerTokenEnv"] = env
        elif auth in ("apikey", "custom"):
            merged = dict(entry.get("headers") or {})
            merged.setdefault("X-API-Key", f"${{{env}}}")
            entry["headers"] = merged
        elif auth == "basic":
            merged = dict(entry.get("headers") or {})
            merged.setdefault("Authorization", f"Basic ${{{env}}}")
            entry["headers"] = merged
    return entry, needs


def plan(item: StoreItem, ctx: PlanContext) -> Planned:
    server = dict(item.meta.get("server") or {})
    if not server:
        raise ValueError(f"{item.id} 缺少 server 定义(目录可能是离线缓存)")
    key = check_name(ctx.name_override or str(server.get("name") or item.name), "MCP server")
    entry, needs = build_entry(server)
    notes = [f"type={entry.get('type')}"]
    if entry.get("url"):
        notes.append(str(entry["url"]))
    if needs:
        notes.append("需补 " + ", ".join(needs))
    return Planned(
        item=item,
        target=ctx.mcp_file(),
        actions=[PatchJson(ctx.mcp_file(), "mcpServers", key, entry)],
        needs_config=needs,
        note=" · ".join(notes),
    )
