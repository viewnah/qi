"""统一商品模型 + 目录(含缓存)。

上游没有"统一目录"端点,而且三种 kind 在三套响应形状里(`docs/hikqin-api.md` §2)。
所以这一层只做两件事:把各自的响应**投影**成 `StoreItem`,以及按 id/名字**解析**用户敲的东西。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .client import StoreClient
from .errors import NotFound

#: 缓存 TTL。目录是"看的东西",5 分钟足够;`--offline` 不看 TTL。
CACHE_TTL = 300


def content_sig(*parts: Any) -> str:
    """上游没有 semver → 用**内容指纹**当版本。`sig:` 前缀提醒它不是上游给的版本号。"""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return "sig:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


@dataclass
class StoreItem:
    id: str
    kind: str
    name: str
    description: str = ""
    version: str = ""
    updated_at: str = ""
    requires_config: bool = False
    safety: dict[str, Any] = field(default_factory=dict)
    badges: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)      # kind 私有(plan 要用)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "name": self.name,
            "description": self.description, "version": self.version,
            "updated_at": self.updated_at, "requires_config": self.requires_config,
            "safety": self.safety, "badges": self.badges, "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StoreItem":
        return cls(
            id=str(raw.get("id")), kind=str(raw.get("kind")), name=str(raw.get("name")),
            description=str(raw.get("description") or ""),
            version=str(raw.get("version") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            requires_config=bool(raw.get("requires_config")),
            safety=dict(raw.get("safety") or {}),
            badges=dict(raw.get("badges") or {}),
            meta=dict(raw.get("meta") or {}),
        )

    # ── 展示 ──

    def safety_label(self) -> str:
        status = str(self.safety.get("scan_status") or "")
        if not status:
            return "-"
        if self.safety.get("reviewed"):
            return f"{status}(已复核)"
        return status

    def is_unsafe(self) -> bool:
        return str(self.safety.get("scan_status") or "").lower() in ("critical", "high")


class Catalog:
    """一次取货的目录快照。`kinds` 是本次真的去查的那几种。"""

    def __init__(self, store_name: str, items: list[StoreItem], *,
                 fetched_at: float = 0.0, kinds: list[str] | None = None,
                 missing: list[tuple[str, str]] | None = None) -> None:
        self.store_name = store_name
        self.items = items
        self.fetched_at = fetched_at or time.time()
        self.kinds = kinds or sorted({item.kind for item in items})
        #: 这次没查到的 kind 及原因(未登录 / 网络) —— `search` 要把它说出来,
        #: 否则"商店看起来是空的"是最坏的误解。
        self.missing = missing or []

    # ── 查 ──

    def filter(self, *, kind: str | None = None, keyword: str | None = None) -> list[StoreItem]:
        found = self.items
        if kind:
            found = [item for item in found if item.kind == kind]
        if keyword:
            needle = keyword.strip().lower()
            found = [item for item in found
                     if needle in item.name.lower() or needle in item.description.lower()]
        return found

    def find(self, key: str) -> StoreItem:
        """解析 `[kind:]key`。三条规则见 README §3.1 —— 歧义**报错**,不猜。"""
        kind, name = _split(key)
        if kind:
            pool = [item for item in self.items if item.kind == kind]
            if not pool:
                raise NotFound(f"目录里没有 {kind} 这一类条目",
                               hint="可能这一类没查成(未登录?);`qi store search --kind mcp`")
            for item in pool:
                if item.id == key or item.name == name:
                    return item
            raise NotFound(f"{kind} 里没有 `{name}`",
                           hint=_candidates(pool, name))

        # 裸 key:**名称优先**(人记的是名字),再退 id。纯数字必须是 kind:id,这里直接拒绝。
        if key.isdigit():
            raise NotFound(
                f"`{key}` 是数字,必须带 kind 前缀",
                hint="例如 `qi store install skill:" + key + "`(skill 与 mcp 各有一套数字 id)",
            )
        exact = [item for item in self.items if item.name.lower() == name.lower()]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise NotFound(f"`{name}` 有歧义", hint=_candidates(exact, name))
        partial = [item for item in self.items if name.lower() in item.name.lower()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            raise NotFound(f"`{name}` 命中多个", hint=_candidates(partial, name))
        by_id = [item for item in self.items if item.id == key]
        if by_id:
            return by_id[0]
        raise NotFound(f"没找到 `{key}`",
                       hint="`qi store search " + name + "` 看看有没有同名的")

    # ── 缓存 ──

    def save_cache(self) -> Path:
        file = paths.cache_file(self.store_name)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({
            "version": 1, "store": self.store_name, "fetched_at": self.fetched_at,
            "kinds": self.kinds,
            "items": [item.as_dict() for item in self.items],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return file

    @classmethod
    def load_cache(cls, store_name: str) -> "Catalog | None":
        file = paths.cache_file(store_name)
        if not file.is_file():
            return None
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        items = [StoreItem.from_dict(i) for i in raw.get("items") or [] if isinstance(i, dict)]
        return cls(store_name, items, fetched_at=float(raw.get("fetched_at") or 0),
                   kinds=list(raw.get("kinds") or []))


def _split(key: str) -> tuple[str | None, str]:
    if ":" in key:
        prefix, _, rest = key.partition(":")
        if prefix in ("skill", "mcp", "a2a", "agent"):
            return prefix, rest
    return None, key


def _candidates(pool: list[StoreItem], name: str) -> str:
    same = [item for item in pool if name.lower() in item.name.lower()][:8]
    shown = same or pool[:8]
    lines = [f"  {item.id:<24} {item.name}" for item in shown]
    more = "" if len(pool) <= len(shown) else f"\n  …还有 {len(pool) - len(shown)} 条"
    return "\n".join(lines) + more


def load(client: StoreClient, *, store_name: str, kinds: list[str] | None = None,
         offline: bool = False, allow_cache: bool = True) -> Catalog:
    """抓一次目录。`offline=True` 只读缓存(不看 TTL)。"""
    from .kinds import KINDS, get_kind

    if offline:
        cached = Catalog.load_cache(store_name)
        if cached is None:
            raise NotFound(f"没有离线缓存:{paths.cache_file(store_name)}",
                           hint="先在线跑一次 `qi store search`,再 --offline")
        return cached

    items: list[StoreItem] = []
    missing: list[tuple[str, str]] = []
    wanted = kinds or list(KINDS)
    for kind_name in wanted:
        module = get_kind(kind_name)            # 未实现的 kind 在这里报「P1/未实现」
        try:
            items.extend(module.fetch(client))
        except Exception as exc:                # noqa: BLE001
            # **一个 kind 失败不该让整个目录变空**(a2a 不需要登录,mcp 可能需要权限)。
            # 记下原因,由 `search` 打在表尾 —— 静默吞掉就是"商店是空的"那种误解。
            missing.append((kind_name, str(exc)))
    catalog = Catalog(store_name, items, kinds=[k for k in wanted
                                                if k not in {m[0] for m in missing}],
                      missing=missing)
    if allow_cache and not missing:
        try:
            catalog.save_cache()
        except OSError:
            pass                                # 缓存写不了不影响本次结果
    return catalog
