"""会话树(格式 v2,pi 的 id/parentId 模型)回归。

锁住四件事:
  1. v1(线性)会话读入补链、首次写入落盘,链不丢
  2. `branch()` = 历史:回到旧节点继续会形成新分支,旧分支原样保留
  3. `fork_at` / clone = 复制当前分支到**新文件**(与 pi 的 /fork、/clone 一致)
  4. 上下文只走当前分支:别的分支上的 state / 开场白 / 消息不能串进来
"""

from __future__ import annotations

import json
from pathlib import Path

from qi_agent.runtime import QiRuntime
from qi_agent.session import SessionStore

HEADER_V1 = {"type": "session", "id": "old1", "title": "老会话",
             "created_at": "2024-01-01T00:00:00"}


def _write_v1(path: Path, entries: list[dict]) -> None:
    lines = [json.dumps(HEADER_V1, ensure_ascii=False)]
    lines += [json.dumps(e, ensure_ascii=False) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(root=tmp_path / "sessions")


# ── 1. v2 基本形状 + v1 迁移 ──────────────────────────────


def test_new_session_is_v2_tree(tmp_path):
    store = _store(tmp_path)
    session = store.create("t", cwd=tmp_path)
    header = session.entries[0]
    assert header["version"] == 2
    assert session.leaf is None and session.current is None   # 还没有任何 entry

    store.append(session, {"type": "message", "role": "user", "content": "hi"})
    first = session.entries[-1]
    assert first["id"] and first["parentId"] is None          # 第一个子节点挂在根上
    assert session.leaf == first["id"]
    assert session.position == first["id"]

    store.append(session, {"type": "message", "role": "assistant", "content": "yo"})
    second = session.entries[-1]
    assert second["parentId"] == first["id"]                  # 链式
    assert [e["content"] for e in session.branch()] == ["hi", "yo"]


def test_v1_session_is_migrated_on_load_and_persisted_on_first_write(tmp_path):
    store = _store(tmp_path)
    path = store.root / "20240101T000000_old1.jsonl"
    _write_v1(path, [
        {"type": "message", "role": "user", "content": "hi", "ts": "2024-01-01T00:00:01"},
        {"type": "message", "role": "assistant", "content": "yo", "ts": "2024-01-01T00:00:02"},
    ])

    loaded = store.get("old1")
    assert loaded is not None
    assert loaded.migrated is True                  # 内存里补过链
    assert [e["content"] for e in loaded.branch()] == ["hi", "yo"]
    # 读路径不写文件(list 也不该写)
    assert "parentId" not in json.loads(path.read_text(encoding="utf-8").splitlines()[1])

    store.append(loaded, {"type": "message", "role": "user", "content": "third"})

    reopened = store.get("old1")
    assert reopened is not None
    assert reopened.migrated is False               # 已落盘
    assert [e["content"] for e in reopened.branch()] == ["hi", "yo", "third"]
    # 新 entry 的 parent 指向被补链出来的 assistant,重开后仍可回溯
    assert reopened.entries[-1]["parentId"] == reopened.entries[-2]["id"]
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["version"] == 2


# ── 2. 分支:回到旧节点继续 ────────────────────────────────


def test_returning_to_old_node_creates_a_branch_and_keeps_the_old_one(tmp_path):
    store = _store(tmp_path)
    session = store.create("t", cwd=tmp_path)
    store.append(session, {"type": "message", "role": "user", "content": "Q1"})
    first = session.entries[-1]["id"]
    store.append(session, {"type": "message", "role": "assistant", "content": "A1"})
    store.append(session, {"type": "message", "role": "user", "content": "Q2"})

    # 回到 Q1 节点继续 → 新内容是 Q1 的第二个子节点
    assert store.set_position(session, first) is True
    store.append(session, {"type": "message", "role": "user", "content": "Q1-另一问"})

    assert [e["content"] for e in session.branch()] == ["Q1", "Q1-另一问"]
    assert session.message_count == 2                       # 只算当前分支
    assert session.branch_points == 1                       # Q1 有两个子节点
    # 旧分支还在文件里,并能单独回溯
    all_contents = [e.get("content") for e in session.tree_entries]
    assert "A1" in all_contents and "Q2" in all_contents
    old_leaf = next(e["id"] for e in session.entries if e.get("content") == "Q2")
    assert [e["content"] for e in session.branch(old_leaf)] == ["Q1", "A1", "Q2"]

    # 重开:position 不落盘(当前节点 = 文件末尾),但两条分支都读得回来
    reopened = store.get(session.id)
    assert reopened is not None
    assert [e["content"] for e in reopened.branch()] == ["Q1", "Q1-另一问"]
    assert reopened.message_count_of(old_leaf) == 3


def test_set_position_rejects_unknown_entry(tmp_path):
    store = _store(tmp_path)
    session = store.create("t", cwd=tmp_path)
    store.append(session, {"type": "message", "role": "user", "content": "Q1"})
    assert store.set_position(session, "not-an-id") is False
    assert store.set_position(session, None) is True        # None = 回到文件末尾
    assert session.current == session.leaf


# ── 3. fork / clone = 复制分支到新文件 ────────────────────


def test_fork_at_copies_branch_into_new_file(tmp_path):
    store = _store(tmp_path)
    session = store.create("原会话", cwd=tmp_path)
    store.append(session, {"type": "message", "role": "user", "content": "Q1"})
    store.append(session, {"type": "message", "role": "assistant", "content": "A1"})
    first = None
    for e in session.entries:
        if e.get("content") == "Q1":
            first = e["id"]

    assert first is not None
    forked = store.fork_at(session, first, title="分叉")

    assert forked.path != session.path
    assert forked.title == "分叉"
    assert [e["content"] for e in forked.branch()] == ["Q1"]      # 只到 Q1
    assert forked.entries[0]["version"] == 2
    assert len(session.branch()) == 2                             # 原会话不受影响
    # 新文件可独立重开
    reopened = store.get(forked.id)
    assert reopened is not None
    assert [e["content"] for e in reopened.branch()] == ["Q1"]


def test_fork_before_first_message_gives_empty_history(tmp_path):
    store = _store(tmp_path)
    session = store.create("原会话", cwd=tmp_path)
    store.append(session, {"type": "message", "role": "user", "content": "Q1"})

    forked = store.fork_at(session, None, title="空白")
    assert forked.branch() == []
    assert forked.message_count == 0

    store.append(forked, {"type": "message", "role": "user", "content": "重问"})
    assert [e["content"] for e in forked.branch()] == ["重问"]


def test_clone_copies_whole_active_branch(tmp_path):
    store = _store(tmp_path)
    session = store.create("t", cwd=tmp_path)
    store.append(session, {"type": "message", "role": "user", "content": "Q1"})
    store.append(session, {"type": "message", "role": "assistant", "content": "A1"})

    cloned = store.fork_at(session, session.current, title="副本")
    assert [e["content"] for e in cloned.branch()] == ["Q1", "A1"]
    assert cloned.id != session.id
    # 两份独立:克隆里继续不污染原件
    store.append(cloned, {"type": "message", "role": "user", "content": "只在副本"})
    assert session.message_count == 2
    assert cloned.message_count == 3


# ── 4. 上下文只走当前分支 ─────────────────────────────────




# ── 5. 坏文件不炸 ─────────────────────────────────────────


def test_dangling_parent_and_cycle_degrade_gracefully(tmp_path):
    store = _store(tmp_path)
    session = store.create("t", cwd=tmp_path)
    session.entries.append({"type": "message", "id": "a", "parentId": "ghost",
                            "role": "user", "content": "悬空"})
    assert [e["content"] for e in session.branch("a")] == ["悬空"]

    session.entries.append({"type": "message", "id": "b", "parentId": "c",
                            "role": "user", "content": "环-1"})
    session.entries.append({"type": "message", "id": "c", "parentId": "b",
                            "role": "user", "content": "环-2"})
    got = session.branch("b")          # 成环:停下,不死循环也不抛
    assert [e["content"] for e in got] == ["环-2", "环-1"]


def test_bad_version_header_does_not_crash(tmp_path):
    store = _store(tmp_path)
    path = store.root / "20240101T000000_bad.jsonl"
    path.write_text(json.dumps({"type": "session", "id": "bad", "version": "??"}) + "\n",
                    encoding="utf-8")
    session = store.get("bad")
    assert session is not None and session.version == 2


# ── CLI `--fork`(对齐 pi 的 `--fork <path|id>`)──────────────


_MODELS = '{"providers": {"o": {"api": "openai-completions", "models": [{"id": "x"}]}}}'
_SETTINGS = '{"defaultProvider": "o", "defaultModel": "x"}'


def test_cli_fork_flag_copies_branch_into_new_session(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from qi_agent import paths
    from qi_agent import runtime as runtime_mod
    from qi_agent.cli import app as cli_app
    from qi_agent.models import AgentEvent

    monkeypatch.chdir(tmp_path)
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))

    class FakeRuntime:
        def __init__(self, *args, **kwargs):
            self.sessions = SessionStore()
            self.cwd = tmp_path
            self.cfg = None
            self.notes: list[str] = []
            self.started: list[str] = []
            self.flag_errors: list[str] = []      # cli 会读它(打错的 --ext)

        async def start_session(self, session, reason: str = "startup") -> None:
            """真实 QiRuntime 的会话级事件;假运行时只记账。"""
            self.started.append(reason)

        async def stream(self, prompt, session, agent_override=None):
            yield AgentEvent(kind="text", agent="g", text="ok")

    monkeypatch.setattr(runtime_mod, "QiRuntime", FakeRuntime)

    store = SessionStore()
    source = store.create("源会话", cwd=tmp_path)
    store.append(source, {"type": "message", "role": "user", "content": "hi"})

    result = CliRunner().invoke(cli_app, ["-p", "--fork", source.id, "再问一句"])
    assert result.exit_code == 0, result.output
    assert "分叉出新会话" in result.output

    sessions = [s for s in store.list() if s.id != source.id]
    assert len(sessions) == 1
    forked = sessions[0]
    assert [e.get("content") for e in forked.branch()] == ["hi"]   # 源分支被复制
    assert forked.title == "源会话 @fork"
