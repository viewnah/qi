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


def _has_assistant(session: Session) -> bool:
    """这个会话已经有助手回答了吗?—— `unflushed` 会话落盘的触发条件。

    为什么以 **assistant** 为界(而不是"有任何 entry"):文件一旦被写出来,它就会出现在
    `/resume` 列表里。用户敲了一句话、没等到回答就退出(打断 / 报错 / Ctrl+C)时,
    列表里多一条无回答的会话毫无价值 —— 而"真聊过一轮"才值得留档。pi 的 `_persist`
    用的正是这个判据。
    """
    return any(e.get("type") == "message" and e.get("role") == "assistant"
               for e in session.entries)


#: TUI 在会话自动命名上线前写死的默认标题(`SessionStore.create("tui", …)`)。它**不是名字**:
#: 那时每个新会话都叫 `tui`,列表里一列同名的 `tui` 等于没有名字。见 `has_title()`。
LEGACY_DEFAULT_TITLE = "tui"


def has_title(title: str | None) -> bool:
    """这个标题算「有名字」吗?

    空串与历史默认值 `LEGACY_DEFAULT_TITLE` 都不算 —— 两处要口径一致:
    自动命名拿它判断「要不要起名」,选择器拿它判断「显示标题还是回落第一句话」。
    """
    cleaned = (title or "").strip()
    return bool(cleaned) and cleaned != LEGACY_DEFAULT_TITLE


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
    #: **内存会话**:`--no-session` 用。entries 照常攒、能被查询与回放,但**任何写盘都被跳过**
    #: (append / save / set_title)。为什么不干脆不建 Session:整条回合链路(历史、用量、
    #: 事件、扩展的 `ctx.session_manager`)都假定"有当前会话" —— 给一个真对象、只是不落盘,
    #: 比在下游到处判 None 可靠得多。
    ephemeral: bool = False
    #: **尚未落过盘**:文件还没被创建出来。pi 的 `flushed` 同义 —— 见 `SessionStore._persist`:
    #: 文件推迟到**第一条 assistant 回答**才写在磁盘上。
    #:
    #: 为什么需要它:回合一开始就得有会话对象(历史、用量、扩展的 `ctx.session_manager`
    #: 都要),但"用户问了却没得到回答"(Ctrl+C、模型报错、问一句就走)不该在磁盘上留东西 ——
    #: 那正是空会话堆积的成因(`docs/session-format.md` §9.1)。用户那句话仍留在内存里,
    #: 真有回答时一起写出。
    unflushed: bool = False

    @property
    def parent_session(self) -> str | None:
        """本会话是从哪个会话分叉出来的(header 的 `parentSession`,pi 同名字段)。

        只有 `/fork` / `/clone` 建出来的会话有;老会话没有这个键 → None。
        pi 的会话选择器用它把分叉串成树(`threaded` 排序)。
        """
        header = self.entries[0] if self.entries else {}
        value = header.get("parentSession") if header.get("type") == "session" else None
        return str(value) if value else None

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

    # ── 选择器用的派生量(pi 的 SessionInfo 那几项)──
    @property
    def first_user_text(self) -> str:
        """当前分支上第一条用户消息 —— 没起过名时,**它就是列表里显示的那行**(pi 同款)。

        用户消息的 `content` 可能是非字符串(图片块等),那种跳过继续找。
        """
        for entry in self.branch():
            if entry.get("type") == "message" and entry.get("role") == "user":
                content = entry.get("content")
                if isinstance(content, str) and content.strip():
                    return content
        return ""

    @property
    def display_label(self) -> str:
        """列表里显示什么:有名字用名字,否则回落第一句话(再没有就占位)。"""
        if has_title(self.title):
            return self.title.strip()
        first = " ".join(self.first_user_text.split())
        return first or "(无消息)"

    @property
    def modified_ts(self) -> float:
        """最后活动时间:取 entry 里最晚的 `ts`;**没有 entry 时间就回落 header 的 `created_at`**
        (刚建的会话只有 header)。

        为什么不用文件 mtime:整文件重写(改名、迁移)会把它推到现在,排序就乱了。
        坏值忽略(不抛):一行脏数据不该让整个列表排不出来。
        """
        best = 0.0
        for entry in self.entries:
            raw = entry.get("ts") or (entry.get("created_at")
                                      if entry.get("type") == "session" else None)
            if not isinstance(raw, str):
                continue
            try:
                best = max(best, time.mktime(time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")))
            except ValueError:
                continue
        return best

    @property
    def search_text(self) -> str:
        """模糊/短语搜索的语料:id + 标题 + **全部**消息 + cwd(pi 的 `getSessionSearchText`)。

        用 `entries` 而不是 `branch()`:pi 也是把文件里所有消息拼进去 —— 搜的是
        「这个会话里聊过什么」,翻过的旧分支同样算。
        """
        parts = [self.id, self.title or "", self.cwd or ""]
        for entry in self.entries:
            if entry.get("type") != "message":
                continue
            content = entry.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
        return " ".join(parts)


def _as_int(value: object) -> int:
    """usage 里的计数:非整数(bool 也是 int 的子类,单独排除)一律当 0。

    provider 差异大 —— 有的回字符串、有的回 null。它们不影响会话能不能跑,所以不报错。
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


#: 「设置类」entry:记录**模型 / 思考级别**是什么时候被换掉的(pi 同款两类)。
#:
#: 为什么落盘:切换动作本身是**界面状态**,不会作为消息进 LLM 上下文 —— 于是 agent
#: 看不出中途换过模型(它只知道「当前是什么」),回放里也没有切换点。落一条 entry 就有了
#: 时间线上的事实,而**续会话时按它还原**才让「上次切到的模型」跟着回来(pi 的
#: `getSessionContextSettings`)。
#:
#: 它们**不进上下文**(`Runtime._history()` 只读 `message`),也不在 `/tree` 的默认视图里
#: 显示(`tui.entry_passes_tree_filter`)—— 与 `custom` 同一条取舍:看得见的归界面,
#: 进模型的归 message。
MODEL_CHANGE = "model_change"
THINKING_LEVEL_CHANGE = "thinking_level_change"


def context_settings(branch: list[dict]) -> dict:
    """从一条分支上读出**生效的**设置:`{"model": (provider, id) | None, "thinking_level": str | None}`。

    取**最后一条**(后者胜)—— 与 pi 的 `getSessionContextSettings` 同一口径。
    坏值/缺键当没有:一行脏数据不该让会话打不开。
    """
    model: tuple[str, str] | None = None
    level: str | None = None
    for entry in branch:
        kind = entry.get("type")
        if kind == MODEL_CHANGE:
            provider = str(entry.get("provider") or "").strip()
            model_id = str(entry.get("model_id") or "").strip()
            if provider and model_id:
                model = (provider, model_id)
        elif kind == THINKING_LEVEL_CHANGE:
            raw = str(entry.get("thinking_level") or "").strip()
            if raw:
                level = raw
    return {"model": model, "thinking_level": level}


def usage_summary(branch: list[dict]) -> dict:
    """当前分支的用量汇总(**会话级**,读落盘的 entries)。

    为什么在后端算:前端只拿得到**一个窗口**(`/api/sessions/{id}` 是分页的),
    长会话窗口前面还有几千条 —— 前端求和会静默少算。后端手上有整条分支。

    口径:
      - `turns` 用户轮数(`role=user` 的 message);
      - `steps` / `llm_calls` / token 累计**只读带 usage 的助手消息**;
      - 写入 usage 之前的老会话、以及被硬取消的轮没有 usage → 它们只计入 `turns`,
        不把 token 当成 0 也不补估值(没有就是没有);
      - `context_tokens` 取**最后一条**带该键的轮 —— 那是那一轮最后一次 LLM 调用
        看到的 prompt 大小(即"现在上下文里装着多少"),**不是**各轮 prompt 相加。
    """
    turns = 0
    tools = 0
    tool_failures = 0
    steps = 0
    llm_calls = 0
    totals: dict[str, int] = {}
    context_tokens = 0
    for entry in branch:
        kind = entry.get("type")
        if kind == "tool":
            tools += 1
            if entry.get("status") == "error":
                tool_failures += 1
            continue
        if kind != "message":
            continue
        if entry.get("role") == "user":
            turns += 1
            continue
        if entry.get("role") != "assistant":
            continue
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            continue
        steps += _as_int(usage.get("turns"))
        llm_calls += _as_int(usage.get("llm_calls"))
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            totals[key] = totals.get(key, 0) + _as_int(usage.get(key))
        context = _as_int(usage.get("context_tokens"))
        if context > 0:
            context_tokens = context
    # 有的 provider 不给 total_tokens:那就用两侧之和 —— 好过显示 0。
    total = totals.get("total_tokens") or (totals.get("prompt_tokens", 0)
                                           + totals.get("completion_tokens", 0))
    return {"turns": turns, "steps": steps, "tools": tools,
            "tool_failures": tool_failures, "llm_calls": llm_calls,
            "prompt_tokens": totals.get("prompt_tokens", 0),
            "completion_tokens": totals.get("completion_tokens", 0),
            "total_tokens": total, "context_tokens": context_tokens}


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

    def create(self, title: str = "", cwd: Path | str | None = None,
               session_id: str | None = None,
               parent_session: Path | str | None = None) -> Session:
        """新建会话文件(v2 树格式)。

        `cwd` 写进 header:按项目分组与恢复时选对工作目录都靠它。
        省略时为 None,后续由 `ensure_cwd()` 回填。

        `session_id` 给了就用它(CLI 的 `--session-id <id>`:精确 id,不存在则建),否则随机。

        `parent_session` = 这个会话从哪个文件分叉出来(pi header 的 `parentSession`)。
        只有 `/fork` / `/clone` 会传;会话选择器的树状(threaded)视图靠它串起来。
        """
        sid = session_id or _new_id()
        path = self.root / f"{time.strftime('%Y%m%dT%H%M%S')}_{sid}.jsonl"
        now = _now()
        header: dict = {"type": "session", "version": 2, "id": sid,
                        "title": title, "created_at": now}
        if cwd is not None:
            header["cwd"] = str(Path(cwd).expanduser().resolve())
        if parent_session is not None:
            header["parentSession"] = str(Path(parent_session).expanduser())
        path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")
        return Session(id=sid, path=path, title=title, created_at=now,
                       cwd=header.get("cwd"), entries=[header], version=2)

    def ephemeral(self, title: str = "", cwd: Path | str | None = None) -> Session:
        """建一个**不落盘**的会话(`qi --no-session`)。

        与 `create()` 的唯一差别:不写文件、`ephemeral=True`。`path` 仍给一个**看起来正常**
        的路径 —— 下游(`/session` 的信息行、`QI_SESSION_FILE` 环境变量、扩展)都能照常读它,
        只是永远没有那个文件。用 `session.ephemeral` 判真假,不要拿 `path.exists()` 判:
        那会在"文件刚好被删掉"时给出错误答案。
        """
        sid = _new_id()
        now = _now()
        header: dict = {"type": "session", "version": 2, "id": sid,
                        "title": title, "created_at": now}
        if cwd is not None:
            header["cwd"] = str(Path(cwd).expanduser().resolve())
        return Session(id=sid, path=self.root / f"{time.strftime('%Y%m%dT%H%M%S')}_{sid}.jsonl",
                       title=title, created_at=now, cwd=header.get("cwd"),
                       entries=[header], version=2, ephemeral=True)

    def reserve(self, title: str = "", cwd: Path | str | None = None) -> Session:
        """**预留**一个新会话:id 与路径定下来,但**文件先不建**(`unflushed=True`)。

        这是"裸 `qi` 进来"该走的路 —— 对齐 pi 的 `newSession()`:它同样只算好
        `sessionFile`、`flushed=false`,真写到磁盘要等第一条 assistant 回答
        (pi 的 `_persist`:`openSync(path, "wx")` 一次写出全部 entry)。

        为什么不能就在 `create()` 里建文件:`qi` 看一眼就走、或问了句就被 Ctrl+C,
        都会留下一个空会话;本机 3000+ 个空会话就是这么攒出来的。
        """
        sid = _new_id()
        now = _now()
        header: dict = {"type": "session", "version": 2, "id": sid,
                        "title": title, "created_at": now}
        if cwd is not None:
            header["cwd"] = str(Path(cwd).expanduser().resolve())
        return Session(id=sid, path=self.root / f"{time.strftime('%Y%m%dT%H%M%S')}_{sid}.jsonl",
                       title=title, created_at=now, cwd=header.get("cwd"),
                       entries=[header], version=2, unflushed=True)

    def flush(self, session: Session) -> None:
        """把一个 `unflushed` 的会话写出来(第一条 assistant 回答到达时调)。幂等。

        用 `"x"`(独占创建,即 `O_EXCL`)而不是 `"w"`:`unflushed` 期间**不该有**文件,
        真有就说明路径撞了 —— 那种情况宁可报错,也不要静默覆盖掉另一个会话
        (用户的会话是**不可再生**的数据)。
        """
        if session.ephemeral or not session.unflushed:
            return
        with session.path.open("x", encoding="utf-8") as fh:
            for entry in session.entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        session.unflushed = False
        session.migrated = False
        self.save(session)
        return session

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

    def open_file(self, path: Path | str) -> Session | None:
        """按**文件路径**打开一个会话(pi 的 `--session <path|id>` 里那个 path 形态)。

        `get()` 只按 header id / 文件名 stem 前缀匹配 —— 一个绝对路径永远不命中,
        而帮助文字里写的是 `path|id`。读不到 / 空文件返回 None(调用方自己报)。
        """
        p = Path(path).expanduser()
        if not p.is_file():
            return None
        entries = self._read(p)
        if not entries:
            return None
        return self._from_entries(p, entries)

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
        self._persist(session, entry)

    def _persist(self, session: Session, entry: dict) -> None:
        """把刚 append 的 entry 落到磁盘(pi 的 `_persist` 同形)。

        三条分支:
          - **内存会话**(`--no-session`):不碰磁盘;
          - **还没落过盘**(`unflushed`,裸 `qi` 建的新会话):也先不落 —— 文件推迟到
            第一条 **assistant** 回答。这样"问了句就被打断"同样不留文件;
          - 其余:追加一行(补过链的老会话整文件重写)。
        """
        if session.ephemeral:
            return
        if session.unflushed:
            # 有 assistant 回答了吗?有就现在把**全部** entries 一次性写出(含 header)
            if not _has_assistant(session):
                return
            self.flush(session)
            return
        if session.migrated:
            # 补链过的老 entry 只在内存里,必须整文件重写才能让新节点的 parent 可回溯
            session.migrated = False
            self.save(session)
            return
        with session.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def set_context_setting(self, session: Session, kind: str, payload: dict) -> bool:
        """落一条设置类 entry(model / 思考级别)。返回是否真写了。

        **同值不重写**:级别已经在文件末尾记过就不必再记一条 —— 否则每次启动都会给
        每个会话加一行(pi 也只在 `isChanging` 时才写)。判据只看**当前分支的最后一条**
        同类 entry:分支回退后再换回来,本来就是一次真实的切换。

        `kind` 只认 `MODEL_CHANGE` / `THINKING_LEVEL_CHANGE`;其它值当写错,不落盘
        (静默写一个客户端不认识的类型,比报错更难查)。
        """
        if kind not in (MODEL_CHANGE, THINKING_LEVEL_CHANGE):
            return False
        if kind == MODEL_CHANGE:
            entry: dict = {"type": MODEL_CHANGE,
                           "provider": str(payload.get("provider") or ""),
                           "model_id": str(payload.get("model_id") or "")}
            previous = next((e for e in reversed(session.branch())
                             if e.get("type") == MODEL_CHANGE), None)
            unchanged = (previous is not None
                         and previous.get("provider") == entry["provider"]
                         and previous.get("model_id") == entry["model_id"])
        else:
            entry = {"type": THINKING_LEVEL_CHANGE,
                     "thinking_level": str(payload.get("thinking_level") or "")}
            previous = next((e for e in reversed(session.branch())
                             if e.get("type") == THINKING_LEVEL_CHANGE), None)
            unchanged = (previous is not None
                         and previous.get("thinking_level") == entry["thinking_level"])
        if unchanged:
            return False
        self.append(session, entry)
        return True

    def save(self, session: Session) -> None:
        """整文件重写(改名/标题/迁移/分叉等)。内存会话跳过(没有文件可写)。"""
        if session.ephemeral:
            session.migrated = False
            return
        if session.unflushed:
            # 还没落过盘的会话:整文件重写就等于**把它提前建出来了** —— 那是 `flush` 的活。
            # 这里只可能来自"改标题"这类操作;让 `flush` 一次写全(下次 append 也会带走)。
            return
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

    def set_title(self, session: Session, title: str) -> None:
        """改标题:**内存 + header entry + 落盘**三处一起改。

        标题在会话文件里有**两份**:`session.title`(运行时读的)与 header entry 的
        `title`(磁盘上那份真相)。只改一份就会出现"列表里是新名、重开又变回旧的" ——
        所以收进一个方法,别在两处各写一遍(改名端点与自动命名都走它)。
        """
        header = (session.entries[0]
                  if session.entries and session.entries[0].get("type") == "session"
                  else None)
        previous = (session.title, header.get("title") if header else None)
        session.title = title
        if header is not None:
            header["title"] = title
        if session.ephemeral:
            return                      # 内存会话:内存与 header 都改了,没有文件要写
        try:
            self.save(session)
        except OSError:
            # 写盘失败就把内存**回滚**:否则会出现"列表里是新名、磁盘上还是旧名"，
            # 刷新一次标题就变回去 —— 那是最难查的一类不一致。
            session.title = previous[0]
            if header is not None:
                header["title"] = previous[1]
            raise

    def fork_at(self, session: Session, entry_id: str | None,
                title: str | None = None) -> Session:
        """把 `branch(entry_id)` 复制成一个**新会话文件**(pi 的 fork/clone 都是新文件)。

        `entry_id=None` = 空历史(从第一条消息之前开始)。复制过去的 entry 保持
        原 id/parentId:链在新文件里自洽,未来再引用时不至于指向另一个文件。
        """
        branch = session.branch(entry_id) if entry_id else []
        new = self.create(title if title is not None else session.title, cwd=session.cwd,
                          parent_session=session.path)
        for entry in branch:
            new.entries.append(copy.deepcopy(entry))
        self.save(new)
        new.position = new.leaf
        return new
