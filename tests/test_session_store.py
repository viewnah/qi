"""会话存储:按 id / 文件名 stem 查找(`qi -c` 的回归)。

历史 bug:`latest()` 传文件名 stem(`<ts>_<id>`),而 `get()` 只比对 header 里的
`<id>`,`startswith` 永不成立 → `qi -c/--continue` 永远报“无历史会话”并新建。
"""

from __future__ import annotations

from qi_agent.session import SessionStore, usage_summary


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


# ── 用量汇总(会话级)─────────────────────────────────────
#
# 为什么这套断言值得写:这个汇总是**输入卡下方那行统计**与未来任何"这个会话花了多少"
# 的唯一来源。三个地方最容易错 —— 数错轮数、把没记 usage 的老会话当成 0、
# 以及把"上下文占用"写成各轮 prompt 相加(那个数字会随轮数虚增,还会超过窗口)。

def _branch_with_usage() -> list[dict]:
    return [
        {"type": "session", "id": "s1"},
        {"type": "message", "role": "user", "content": "看下目录"},
        {"type": "tool", "tool": "ls", "status": "ok"},
        {"type": "message", "role": "assistant", "content": "好了",
         "usage": {"turns": 2, "llm_calls": 2, "prompt_tokens": 100,
                   "completion_tokens": 20, "total_tokens": 120, "context_tokens": 100}},
        {"type": "message", "role": "user", "content": "再改一下"},
        {"type": "tool", "tool": "edit", "status": "error"},
        {"type": "message", "role": "assistant", "content": "改完了",
         "usage": {"turns": 1, "llm_calls": 1, "prompt_tokens": 300,
                   "completion_tokens": 40, "total_tokens": 340, "context_tokens": 300}},
    ]


def test_usage_summary_counts_rounds_steps_and_tools():
    got = usage_summary(_branch_with_usage())
    assert got["turns"] == 2          # 用户消息,不是助手消息
    assert got["steps"] == 3          # 各轮 usage.turns 之和(2 + 1)
    assert got["llm_calls"] == 3
    assert got["tools"] == 2 and got["tool_failures"] == 1
    assert got["prompt_tokens"] == 400 and got["completion_tokens"] == 60
    assert got["total_tokens"] == 460
    # 上下文占用 = **最后一条**的 context_tokens,不是 100 + 300
    assert got["context_tokens"] == 300


def test_usage_summary_of_a_session_that_never_recorded_usage():
    """老会话(写入 usage 之前)或全程被硬取消:只数轮数,token 是 0。

    **不补估值**:没有就是没有 —— 编一个数字出来会让"这行统计"变成假数据。
    """
    branch = [
        {"type": "message", "role": "user", "content": "a"},
        {"type": "message", "role": "assistant", "content": "b"},
    ]
    got = usage_summary(branch)
    assert got["turns"] == 1
    assert (got["steps"], got["total_tokens"], got["context_tokens"]) == (0, 0, 0)


def test_usage_summary_without_total_tokens_falls_back_to_the_two_sides():
    """有的 provider 不给 total_tokens:那就用两侧之和,好过显示 0。"""
    branch = [{"type": "message", "role": "assistant", "content": "x",
               "usage": {"prompt_tokens": 10, "completion_tokens": 4}}]
    got = usage_summary(branch)
    assert got["total_tokens"] == 14


def test_usage_summary_ignores_dirty_values():
    """provider 给的可能是字符串 / null / bool:一律当 0,不能把它变成异常。

    这个汇总跑在**每次打开会话**的路径上 —— 它为脏数据抛错,整个会话就打不开了。
    """
    branch = [{"type": "message", "role": "assistant", "content": "x",
               "usage": {"turns": "2", "prompt_tokens": None, "completion_tokens": True,
                         "total_tokens": 7}}]
    got = usage_summary(branch)
    assert got["steps"] == 0 and got["total_tokens"] == 7
    assert got["prompt_tokens"] == 0 and got["completion_tokens"] == 0
