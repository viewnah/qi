"""会话(P3):JSONL 每会话文件(pi 风格,entry 带 type)。

位置:~/.qi/sessions/<ts>_<id>.jsonl(全局,PLAN B1)。
entry 五类: message / tool / dispatch / state / custom(agent-config 决策)。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import paths


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Session:
    id: str
    path: Path
    title: str = ""
    created_at: str = ""
    entries: list[dict] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        return sum(1 for e in self.entries if e.get("type") == "message")


class SessionError(Exception):
    pass


class SessionStore:
    def __init__(self, root: Path | None = None):
        self.root = root or (paths.global_home() / "sessions")
        self.root.mkdir(parents=True, exist_ok=True)

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(self.root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    def _read(self, path: Path) -> list[dict]:
        entries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def create(self, title: str = "") -> Session:
        sid = uuid.uuid4().hex[:12]
        path = self.root / f"{time.strftime('%Y%m%dT%H%M%S')}_{sid}.jsonl"
        now = _now()
        header = {"type": "session", "id": sid, "title": title, "created_at": now}
        path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")
        return Session(id=sid, path=path, title=title, created_at=now, entries=[header])

    def get(self, session_id: str) -> Session | None:
        for p in self._files():
            entries = self._read(p)
            if entries and str(entries[0].get("id", "")).startswith(session_id):
                return Session(id=entries[0]["id"], path=p,
                               title=entries[0].get("title", ""),
                               created_at=entries[0].get("created_at", ""),
                               entries=entries)
        return None

    def latest(self) -> Session | None:
        files = self._files()
        if not files:
            return None
        return self.get(files[0].stem)

    def list(self) -> list[Session]:
        out = []
        for p in self._files():
            entries = self._read(p)
            if not entries:
                continue
            h = entries[0]
            out.append(Session(id=h.get("id", "?"), path=p, title=h.get("title", ""),
                               created_at=h.get("created_at", ""), entries=entries))
        return out

    def delete(self, session_id: str) -> bool:
        s = self.get(session_id)
        if not s:
            return False
        s.path.unlink(missing_ok=True)
        return True

    def append(self, session: Session, entry: dict) -> None:
        entry.setdefault("ts", _now())
        with session.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        session.entries.append(entry)

    def save(self, session: Session) -> None:
        """整文件重写(改名/标题等)。"""
        with session.path.open("w", encoding="utf-8") as fh:
            for e in session.entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
