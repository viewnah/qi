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


def test_display_label_and_search_text_fall_back_to_first_message(tmp_path):
    """没起过名的会话:`display_label` 回落第一句话、`search_text` 覆盖全部消息。

    会话选择器就靠这两条(pi 的 `firstMessage` / `allMessagesText`)。
    """
    store = _store(tmp_path)
    named = store.create("梳理仓库结构")
    unnamed = store.create("")
    store.append(unnamed, {"type": "message", "role": "user", "content": "帮我看看\n这个仓库"})
    store.append(unnamed, {"type": "message", "role": "assistant", "content": "好的"})

    assert named.display_label == "梳理仓库结构"
    assert unnamed.display_label == "帮我看看 这个仓库"       # 多行压成一行
    assert store.create("").display_label == "(无消息)"
    assert "帮我看看" in unnamed.search_text and "好的" in unnamed.search_text


def test_fork_records_parent_session(tmp_path):
    """分叉写 header 的 `parentSession`(pi 同名字段)—— 选择器的树状视图靠它。"""
    store = _store(tmp_path)
    root = store.create("源会话", cwd=tmp_path)
    store.append(root, {"type": "message", "role": "user", "content": "一"})
    child = store.fork_at(root, root.leaf, title="源会话 @fork")

    assert child.parent_session == str(root.path)
    assert root.parent_session is None
    reloaded = store.get(child.id)
    assert reloaded is not None, "fork 出来的会话应该能按 id 读回来"
    assert reloaded.parent_session == str(root.path)   # 真的落盘了


def test_modified_ts_prefers_entry_time_and_falls_back_to_header(tmp_path):
    """排序用的"最后活动时间"取 entry 的 `ts`;刚建、只有 header 的会话回落 `created_at`。"""
    store = _store(tmp_path)
    session = store.create("x")
    created = session.modified_ts
    assert created > 0                                    # header.created_at 兜住了
    store.append(session, {"type": "message", "role": "user", "content": "一",
                           "ts": "2030-01-02T03:04:05"})
    assert session.modified_ts > created                  # 取最新的一条


# ── 懒建 / 内存会话(空会话不再堆在磁盘上)─────────────────────
#
# 为什么这些断言值得写:开发机上真实事故 —— `~/.qi/agent/sessions/` 攒了 3000+ 个空会话。
# 两个来源,这里各钉一组:
#   1. 裸 `qi` 一进来就落文件(测试更是每条用例都漏一个);
#   2. `--no-session` 名字叫"不落盘",实际走 `create()` 照样落文件。


def test_reserve_does_not_touch_the_disk(tmp_path):
    """`reserve()` 只定下 id/路径,**不建文件** —— 这正是裸 `qi` 进来时该有的行为。"""
    store = _store(tmp_path)
    session = store.reserve("", cwd=tmp_path)

    assert session.unflushed is True
    assert not session.path.exists(), "预留阶段不该建文件(否则看一眼前就留一个空会话)"
    assert session.entries[0]["type"] == "session"
    assert session.cwd == str(tmp_path.resolve())     # cwd 仍然写进 header(内存里)


def test_reserved_session_is_invisible_until_flushed(tmp_path):
    """预留的会话对 `list()` / `latest()` **不可见** —— 它还没有文件。

    这是"懒建"的另一半好处:`/resume` 列表里不会出现一个到此一游的空会话。
    """
    store = _store(tmp_path)
    store.reserve("", cwd=tmp_path)
    assert store.list() == []
    assert store.latest() is None


def test_flush_writes_everything_accumulated(tmp_path):
    """落盘时把**已经攒下的全部 entry**一次写出(不是只写最后那条)。

    否则启动阶段攒的设置类 entry(model/级别)会丢 —— 续会话时模型就退回默认了。
    """
    store = _store(tmp_path)
    session = store.reserve("", cwd=tmp_path)
    store.append(session, {"type": "thinking_level_change", "thinking_level": "high"})
    store.append(session, {"type": "message", "role": "user", "content": "你好"})
    assert not session.path.exists()                  # 还没有助手回答 → 仍然没文件

    store.append(session, {"type": "message", "role": "assistant", "content": "答"})

    assert session.path.is_file()
    assert session.unflushed is False
    kinds = [e.get("type") for e in session.entries]
    assert kinds == ["session", "thinking_level_change", "message", "message"]
    reloaded = store.get(session.id)                  # 真的读得回来(不是只在内存里对)
    assert reloaded is not None
    assert [e.get("content") for e in reloaded.branch()
            if e.get("type") == "message"] == ["你好", "答"]


def test_user_message_alone_does_not_flush(tmp_path):
    """只问不答(打断 / 报错 / Ctrl+C)**不落盘**。

    判据是"有没有 assistant 回答",不是"有没有 entry":文件一出现就会进 `/resume` 列表,
    而一个没有回答的会话在列表里没有价值。pi 的 `_persist` 用的是同一个判据。
    """
    store = _store(tmp_path)
    session = store.reserve("", cwd=tmp_path)
    store.append(session, {"type": "message", "role": "user", "content": "问了句就被打断"})
    assert not session.path.exists()
    assert session.message_count == 1                 # 内存里照常有(回放还在)


def test_flush_is_idempotent_and_never_overwrites(tmp_path):
    """已落盘的会话再 `flush()` 是空操作 —— 绝不覆盖已有文件。

    用 `wx`(独占创建)而不是 `w`:文件是**不可再生**的用户数据,宁可在"路径撞了"时
    报错,也不能静默盖掉另一个会话。
    """
    store = _store(tmp_path)
    session = store.reserve("", cwd=tmp_path)
    store.append(session, {"type": "message", "role": "assistant", "content": "答"})
    assert session.path.is_file()

    session.path.write_text('{"type": "session", "id": "别人"}\n', encoding="utf-8")
    store.flush(session)                              # 已 flush(unflushed=False)→ 直接返回
    assert '"id": "别人"' in session.path.read_text(encoding="utf-8")


def test_ephemeral_session_never_writes(tmp_path):
    """`--no-session` 的内存会话:entries 照常攒,磁盘上一个字节都不留。"""
    store = _store(tmp_path)
    session = store.ephemeral("ephemeral", cwd=tmp_path)
    store.append(session, {"type": "message", "role": "user", "content": "一"})
    store.append(session, {"type": "message", "role": "assistant", "content": "二"})
    store.set_title(session, "起了个名")
    store.save(session)

    assert session.ephemeral is True
    assert session.title == "起了个名"                 # 内存里改了
    assert session.message_count == 2
    assert list(tmp_path.glob("**/*.jsonl")) == []    # 磁盘上一个文件都没有


def test_create_still_writes_immediately(tmp_path):
    """`create()` **保持原样**:显式要一个会话(`-n` / `-c` / `--fork` / web API)就立刻落文件。

    懒建只针对"裸 `qi` 进来且没说话"这一种情况 —— 把它扩散到所有创建路径会把
    显式意图也变成"看不着的东西"。
    """
    store = _store(tmp_path)
    session = store.create("显式建的", cwd=tmp_path)
    assert session.unflushed is False
    assert session.path.is_file()
    assert session.path.read_text(encoding="utf-8").strip().startswith('{"type": "session"')


# ── 会话 id 会进文件名 → 必须挡住路径穿越 ──────────────────────

def test_unsafe_session_id_is_rejected(tmp_path):
    """`create(session_id=…)` 的 id **直接拼进** `<时间戳>_<id>.jsonl` → 挡住路径穿越。

    回归:以前 `sid = session_id or _new_id()` 原样拼 —— `--session-id ../../evil`
    会写到**会话目录之外**。这里选择报错而不是静默清洗:用户明确要一个句柄,
    悄悄换成别的名字只会得到“我指定的 id 没生效”且毫无线索。
    """
    import pytest

    store = _store(tmp_path)
    before = sorted(p.name for p in store.root.iterdir())
    for bad in ["../../evil", "a/b", ".", "..", ".hidden", "", "a b", "a\\b", "-x", "x\n"]:
        with pytest.raises(ValueError, match="不合法"):
            store.create("t", session_id=bad)
    # 拒绝就是**什么都没发生** —— 一个字节都不该落盘
    assert sorted(p.name for p in store.root.iterdir()) == before


def test_safe_session_id_is_used_verbatim_and_stays_in_root(tmp_path):
    """合法 id 原样使用,而且**没跑出会话目录**(就是上面那条要防的事)。"""
    store = _store(tmp_path)
    s = store.create("t", session_id="exact123")
    assert s.id == "exact123"
    assert s.path.parent == store.root
    assert s.path.name.endswith("_exact123.jsonl")


def test_generated_session_id_is_always_safe(tmp_path):
    """自己生成的 id 必须永远过得了那道校验 —— 否则正常路径会被自己的守卫打死。"""
    from qi_agent.session import _SAFE_ID

    store = _store(tmp_path)
    for _ in range(5):
        s = store.create("t")
        assert _SAFE_ID.match(s.id), s.id
        assert s.path.parent == store.root
