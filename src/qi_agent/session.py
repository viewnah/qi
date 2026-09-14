"""会话(P3,格式 v2 = pi 的树):JSONL 每会话文件,entry 带 type/id/parentId。

位置:`~/.qi/agent/sessions/<ts>_<id>.jsonl`(全局,PLAN B1);settings.json 的
`sessionDir` 可覆盖。

**v2(当前)= 树**:每条 entry 带 `id` 与 `parentId`,历史是「从某个节点沿 parent
回溯到根」的那条链(§branch)。因此同一个文件里可以并存多条分支:回到旧节点继续提问,
新内容成为它的子节点,旧分支原样保留 —— 这是 pi `/tree` 的数据基础。
`version: 2` 写在 header。

**v1(旧)= 线性**:entry 没有 id/parentId,文件顺序即历史。读入时**在内存里补链**
(按文件顺序串成一条链),首次写入(append/save)时落盘 —— 与 pi「加载时迁移」等价,
但读路径(如 `sessions list`)不写文件。

其他:
- `entries` 仍是**文件里的全部 entry**(含 header 与其它分支);要历史请用 `branch()`。
- header(entries[0])是元数据(id/title/created_at/cwd/version),**不参与树**。
- `position` = 下一次 append 挂到哪个节点(默认文件最后一条 = `leaf`);`/tree` 改它。
- dispatch entry 除 agent(name)外还落盘 display_name:展示名应反映**当时**的值,
  回放时无需再装载 agent/插件。
- 用户消息的 agent_id 是“将处理它的 agent”,不是发言者。
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import paths


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _as_version(value: object) -> int:
    """header 里的版本号容错:坏值当 2(已迁移过),不因为一行脏数据就报错。"""
    try:
        return int(value)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 2


@dataclass
class Session:
    id: str
    path: Path
    title: str = ""
    created_at: str = ""
    cwd: str | None = None            # 会话工作目录(旧会话可能为 None,见 ensure_cwd)
    entries: list[dict] = field(default_factory=list)
    version: int = 2                  # 格式版本(1 = 线性旧格式)
    position: str | None = None       # 下一次 append 的父节点(None = 用 leaf)
    migrated: bool = False            # 读入时补过链,尚未落盘

    # ── 树 ──
    @property
    def tree_entries(self) -> list[dict]:
        """参与树的 entry(排除 header)。"""
        return [e for e in self.entries if e.get("type") != "session"]

    @property
    def leaf(self) -> str | None:
        """文件里最后一条 entry 的 id —— 落盘的「当前节点」(pi 同款约定)。"""
        for entry in reversed(self.entries):
            if entry.get("type") != "session" and entry.get("id"):
                return str(entry["id"])
        return None

    @property
    def current(self) -> str | None:
        """当前节点:优先用显式 position(`/tree` 改过),否则文件末尾。"""
        return self.position or self.leaf

    def branch(self, leaf: str | None = None) -> list[dict]:
        """从 `leaf`(默认当前节点)沿 parentId 回溯到根,返回**正序**的 entry 列表。

        这就是「历史」:会话上下文、回放、消息计数都只看这条链。
        文件损坏(悬空 parent/成环)时退化为文件顺序,不抛异常 —— 会话读不出来
        比丢几条分支更糟。
        """
        nodes = [e for e in self.entries if e.get("type") != "session"]
        if not nodes:
            return []
        by_id = {str(e["id"]): e for e in nodes if e.get("id")}
        target = leaf or self.current
        node = by_id.get(target) if target else None
        if node is None:
            node = nodes[-1]
        out: list[dict] = []
        seen: set[str] = set()
        while node is not None:
            node_id = str(node.get("id", ""))
            if node_id and node_id in seen:      # 环:停下,别死循环
                break
            seen.add(node_id)
            out.append(node)
            parent = node.get("parentId")
            node = by_id.get(str(parent)) if parent else None
        out.reverse()
        return out

    def visible_entries(self, leaf: str | None = None) -> list[dict]:
        """header + 当前分支 —— web `/messages` 的契约一直把 header 当作 entries[0]
        (前端 types.ts 也这么写),所以这里保留 header,只把其它分支挡掉。
        """
        header = [e for e in self.entries[:1] if e.get("type") == "session"]
        return [*header, *self.branch(leaf)]

    def children(self, entry_id: str | None) -> list[dict]:
        """某节点的直接子节点(用于树渲染);`entry_id=None` = 根层。"""
        return [e for e in self.tree_entries
                if (e.get("parentId") or None) == (entry_id or None)]

    @property
    def branch_points(self) -> int:
        """分叉点数(有 >1 个子节点的节点数)。"""
        counts: dict[str | None, int] = {}
        for e in self.tree_entries:
            key = e.get("parentId") or None
            counts[key] = counts.get(key, 0) + 1
        return sum(1 for n in counts.values() if n > 1)

    def message_count_of(self, leaf: str | None = None) -> int:
        return sum(1 for e in self.branch(leaf) if e.get("type") == "message")

    @property
    def message_count(self) -> int:
        """当前分支上的消息数(不是文件里所有分支的总和)。"""
        return self.message_count_of()


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
        """新建会话文件(v2 树格式)。

        `cwd` 写进 header:按项目分组与恢复时选对工作目录都靠它。
        省略时为 None,后续由 `ensure_cwd()` 回填。
        """
        sid = _new_id()
        path = self.root / f"{time.strftime('%Y%m%dT%H%M%S')}_{sid}.jsonl"
        now = _now()
        header: dict = {"type": "session", "version": 2, "id": sid,
                        "title": title, "created_at": now}
        if cwd is not None:
            header["cwd"] = str(Path(cwd).expanduser().resolve())
        path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")
        return Session(id=sid, path=path, title=title, created_at=now,
                       cwd=header.get("cwd"), entries=[header], version=2)

    @staticmethod
    def migrate(entries: list[dict]) -> bool:
        """把线性(v1)entry 就地补成树(v2):按文件顺序串链。返回是否改过。"""
        changed = False
        parent: str | None = None
        for entry in entries:
            if entry.get("type") == "session":
                if entry.get("version") != 2:
                    entry["version"] = 2
                    changed = True
                continue
            if not entry.get("id"):
                entry["id"] = _new_id()
                changed = True
            if "parentId" not in entry:
                entry["parentId"] = parent
                changed = True
            parent = str(entry["id"])
        return changed

    @staticmethod
    def _from_entries(path: Path, entries: list[dict]) -> Session:
        """用已读到的 entries 造 Session(header = entries[0]),顺带做 v1→v2 迁移。"""
        header = entries[0] if entries else {}
        migrated = SessionStore.migrate(entries)
        cwd = header.get("cwd")
        return Session(id=str(header.get("id", "?")), path=path,
                       title=header.get("title", ""),
                       created_at=header.get("created_at", ""),
                       cwd=cwd if isinstance(cwd, str) else None,
                       entries=entries,
                       version=_as_version(header.get("version")),
                       migrated=migrated)

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
        """在当前节点(`position`/`leaf`)下追加一个子节点。"""
        entry.setdefault("ts", _now())
        entry.setdefault("id", _new_id())
        entry.setdefault("parentId", session.current)
        session.entries.append(entry)
        session.position = str(entry["id"])
        if session.migrated:
            # 补链过的老 entry 只在内存里,必须整文件重写才能让新节点的 parent 可回溯
            session.migrated = False
            self.save(session)
            return
        with session.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def save(self, session: Session) -> None:
        """整文件重写(改名/标题/迁移/分叉等)。"""
        with session.path.open("w", encoding="utf-8") as fh:
            for e in session.entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        session.migrated = False

    # ── 树操作(pi 的 /tree、/fork、/clone)──
    def set_position(self, session: Session, entry_id: str | None) -> bool:
        """把「当前节点」移到某个 entry(下次提问会挂在它下面)。"""
        if entry_id is None:
            session.position = None
            return True
        known = {str(e.get("id")) for e in session.tree_entries}
        if entry_id not in known:
            return False
        session.position = entry_id
        return True

    def fork_at(self, session: Session, entry_id: str | None,
                title: str | None = None) -> Session:
        """把 `branch(entry_id)` 复制成一个**新会话文件**(pi 的 fork/clone 都是新文件)。

        `entry_id=None` = 空历史(从第一条消息之前开始)。复制过去的 entry 保持
        原 id/parentId:链在新文件里自洽,未来再引用时不至于指向另一个文件。
        """
        branch = session.branch(entry_id) if entry_id else []
        new = self.create(title if title is not None else session.title, cwd=session.cwd)
        for entry in branch:
            new.entries.append(copy.deepcopy(entry))
        self.save(new)
        new.position = new.leaf
        return new
