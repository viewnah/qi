"""实装账本 `~/.qi/agent/store/installed.json`。

**只在用户级**,每条带 `scope` 与绝对 `target`:项目级装的东西也记得住,换个 cwd 不会
"忘了自己装过什么"。`uninstall` 只读这份文件 —— 断网也要能卸。

三种记录形状(与 `actions.py` 一一对应):

    files[]    目录/文件类(skill):卸载时删这些文件,再删空的 target 目录
    patches[]  JSON 键类(mcp / a2a):卸载时只删那个键,文件留着
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .errors import ConfigError

FORMAT_VERSION = 1


@dataclass
class Patch:
    file: str
    section: str
    key: str


@dataclass
class Record:
    itemId: str
    store: str
    kind: str
    name: str
    version: str
    scope: str                      # global | project
    target: str = ""                # 目录 / 文件(target 可能为空,只有 patches)
    files: list[str] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)
    needs_config: list[str] = field(default_factory=list)
    installed_at: str = ""
    upstream_updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["patches"] = [asdict(p) for p in self.patches]
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Record":
        patches = [Patch(**p) for p in raw.get("patches") or []]
        return cls(
            itemId=str(raw.get("itemId")),
            store=str(raw.get("store") or ""),
            kind=str(raw.get("kind") or ""),
            name=str(raw.get("name") or ""),
            version=str(raw.get("version") or ""),
            scope=str(raw.get("scope") or "global"),
            target=str(raw.get("target") or ""),
            files=[str(f) for f in raw.get("files") or []],
            patches=patches,
            needs_config=[str(n) for n in raw.get("needs_config") or []],
            installed_at=str(raw.get("installed_at") or ""),
            upstream_updated_at=str(raw.get("upstream_updated_at") or ""),
        )


class Ledger:
    def __init__(self, items: list[Record] | None = None, file: Path | None = None) -> None:
        self.items: list[Record] = items or []
        self.file = file or paths.installed_file()

    # ── 读写 ──────────────────────────────────────────────

    @classmethod
    def load(cls, file: Path | None = None) -> "Ledger":
        path = file or paths.installed_file()
        if not path.is_file():
            return cls([], path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"账本不是合法 JSON:{path}", hint=str(exc)) from exc
        items = [Record.from_dict(r) for r in raw.get("items") or [] if isinstance(r, dict)]
        return cls(items, path)

    def save(self) -> Path:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": FORMAT_VERSION, "items": [r.as_dict() for r in self.items]}
        tmp = self.file.with_suffix(f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.file)
        return self.file

    # ── 查 ────────────────────────────────────────────────

    def find(self, item_id: str, scope: str | None = None) -> Record | None:
        for record in self.items:
            if record.itemId == item_id and (scope is None or record.scope == scope):
                return record
        return None

    def by_name(self, name: str, scope: str | None = None) -> Record | None:
        for record in self.items:
            if record.name == name and (scope is None or record.scope == scope):
                return record
        return None

    def owns_target(self, target: Path) -> bool:
        """这个目录/文件是不是本账本写的(决定"覆盖"算不算冲突)。"""
        text = str(target)
        return any(r.target == text for r in self.items)

    def owns_file(self, target: Path) -> bool:
        text = str(target)
        return any(text in r.files for r in self.items)

    def owns_patch(self, file: Path, section: str, key: str) -> bool:
        text = str(file)
        return any(p.file == text and p.section == section and p.key == key
                   for r in self.items for p in r.patches)

    # ── 写 ────────────────────────────────────────────────

    def upsert(self, record: Record) -> None:
        """同 itemId + 同 scope 覆盖(升级就是覆盖),否则追加。"""
        for index, current in enumerate(self.items):
            if current.itemId == record.itemId and current.scope == record.scope:
                self.items[index] = record
                return
        self.items.append(record)

    def remove(self, item_id: str, scope: str | None = None) -> Record | None:
        for index, record in enumerate(self.items):
            if record.itemId == item_id and (scope is None or record.scope == scope):
                return self.items.pop(index)
        return None
