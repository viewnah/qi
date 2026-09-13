"""会话(P3):JSONL 每会话文件(pi 风格,entry 带 type)。

位置:`~/.qi/agent/sessions/<ts>_<id>.jsonl`(全局,PLAN B1);settings.json 的
`sessionDir` 可覆盖。
entry 五类: message / tool / dispatch / state / custom(agent-config 决策)。

header(entries[0])当前字段:id / title / created_at / **cwd**。
`cwd` 是会话的工作目录(对齐 pi):按项目分组会话、恢复时选对目录都靠它。
v0.1 写的旧会话没有这个字段,首次被使用时由 `ensure_cwd()` 回填一次(不猜、不覆盖)。

dispatch entry 除 agent(name)外还落盘 display_name:展示名应反映**当时**的值,
回放时无需再装载 agent/插件。
用户消息的 agent_id 是“将处理它的 agent”,不是发言者。
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
    cwd: str | None = None            # 会话工作目录(旧会话可能为 None,见 ensure_cwd)
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

    def create(self, title: str = "", cwd: Path | str | None = None) -> Session:
        """新建会话文件。

        `cwd` 写进 header:按项目分组与恢复时选对工作目录都靠它。
        省略时为 None,后续由 `ensure_cwd()` 回填。
        """
        sid = uuid.uuid4().hex[:12]
        path = self.root / f"{time.strftime('%Y%m%dT%H%M%S')}_{sid}.jsonl"
        now = _now()
        header: dict = {"type": "session", "id": sid, "title": title, "created_at": now}
        if cwd is not None:
            header["cwd"] = str(Path(cwd).expanduser().resolve())
        path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")
        return Session(id=sid, path=path, title=title, created_at=now,
                       cwd=header.get("cwd"), entries=[header])

    @staticmethod
    def _from_entries(path: Path, entries: list[dict]) -> Session:
        """用已读到的 entries 造 Session(header = entries[0])。"""
        header = entries[0] if entries else {}
        cwd = header.get("cwd")
        return Session(id=str(header.get("id", "?")), path=path,
                       title=header.get("title", ""),
                       created_at=header.get("created_at", ""),
                       cwd=cwd if isinstance(cwd, str) else None,
                       entries=entries)

    def ensure_cwd(self, session: Session, cwd: Path | str) -> bool:
        """给旧会话回填 `cwd`(已有值则不动)。返回是否发生了写入。

        v0.1 的会话 header 没有 cwd;首次被使用时补上,这样历史会话也能按项目分组。
        整文件重写一次,此后 `session.cwd` 有值,不再进本分支。
        header 不合法(空文件/损坏)时**不猜**,直接返回 False。
        """
        if session.cwd or not session.entries:
            return False
        if session.entries[0].get("type") != "session":
            return False
        resolved = str(Path(cwd).expanduser().resolve())
        session.entries[0]["cwd"] = resolved
        session.cwd = resolved
        self.save(session)
        return True

    def get(self, session_id: str) -> Session | None:
        """按会话 id 或文件名 stem 前缀查找(docs/cli.md: `--session <path|id>`)。

        id = 文件名 `<ts>_<id>.jsonl` 里的 `<id>`;同时接受完整 stem。
        旧实现只比对 header id,而 `latest()` 传的是 stem(含 `<ts>_` 前缀),
        startswith 永不成立 → `qi -c` 永远找不到会话、每次都新建。
        """
        if not session_id:
            return None
        for p in self._files():
            entries = self._read(p)
            if not entries:
                continue
            hid = str(entries[0].get("id", ""))
            if hid.startswith(session_id) or p.stem.startswith(session_id):
                return self._from_entries(p, entries)
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
            out.append(self._from_entries(p, entries))
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
