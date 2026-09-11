"""会话存储:按 id / 文件名 stem 查找(`qi -c` 的回归)。

历史 bug:`latest()` 传文件名 stem(`<ts>_<id>`),而 `get()` 只比对 header 里的
`<id>`,`startswith` 永不成立 → `qi -c/--continue` 永远报“无历史会话”并新建。
"""

from __future__ import annotations

from qi_agent.session import SessionStore


def _store(tmp_path) -> SessionStore:
    return SessionStore(root=tmp_path / "sessions")


def test_latest_finds_created_session(tmp_path):
    """核心回归:建完会话,latest() 必须能拿到它。-c 依赖此路径。"""
    store = _store(tmp_path)
    s = store.create("首个")
    latest = store.latest()
    assert latest is not None, "latest() 找不到刚创建的会话(即 qi -c 失效)"
    assert latest.id == s.id


def test_latest_returns_newest(tmp_path):
    """最新的会话胜出。用显式 mtime 排序,不靠 sleep(避免文件系统时间戳精度造成 flaky)。"""
    import os

    store = _store(tmp_path)
    old = store.create("旧")
    new = store.create("新")
    base = 1_700_000_000
    os.utime(old.path, (base, base))
    os.utime(new.path, (base + 10, base + 10))
    latest = store.latest()
    assert latest is not None and latest.id == new.id


def test_get_by_id_stem_and_prefix(tmp_path):
    store = _store(tmp_path)
    s = store.create("x")
    assert store.get(s.id) is not None                    # 短 id
    assert store.get(s.id[:6]) is not None                # id 前缀
    assert store.get(s.path.stem) is not None             # 完整文件名 stem
    assert store.get(s.path.stem[:12]) is not None        # stem 前缀(时间戳段)


def test_get_missing_and_empty(tmp_path):
    store = _store(tmp_path)
    store.create("x")
    assert store.get("不存在") is None
    assert store.get("") is None


def test_delete_by_stem(tmp_path):
    store = _store(tmp_path)
    s = store.create("x")
    assert store.delete(s.path.stem) is True
    assert store.get(s.id) is None


def test_continue_reuses_same_session(tmp_path):
    """模拟 qi -c:新一轮的 append 必须落到同一文件,而不是新会话。"""
    store = _store(tmp_path)
    first = store.create("t")
    first = store.latest()
    assert first is not None
    store.append(first, {"type": "message", "role": "user", "content": "hi"})
    again = store.latest()
    assert again is not None and again.path == first.path
    assert again.message_count == 1
