"""上下文压缩与分支摘要(移植 pi 的 `core/compaction`)回归。

四层:
  1. 估算与切点:token 口径(chars/4)、turn 边界、**绝不在 tool 结果上切**、split turn
  2. 准备阶段:`/compact` 的参数、重复压缩的边界与 previous summary
  3. 摘要调用:提示词形状(结构化格式 / UPDATE / Additional focus)、返回的 entry 载荷
  4. 运行时:`_history` 变成「摘要 + 保留段」;超阈值时自动压缩并产出事件
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from qi_agent.compaction import (
    SUMMARIZATION_PROMPT,
    is_context_entry,
    is_summarizable_entry,
    is_turn_start,
    UPDATE_SUMMARIZATION_PROMPT,
    BRANCH_SUMMARY_PROMPT,
    Preparation,
    branch_to_summarize,
    common_ancestor,
    compact,
    context_tokens,
    entry_tokens,
    estimate_tokens,
    find_cut_point,
    prepare_compaction,
    serialize_conversation,
    should_compact,
    summarize_branch,
    summary_context_message,
)
from qi_agent.llm import ChatResponse
from qi_agent.session import SessionStore


# ── 工具 ─────────────────────────────────────────────────


def _chain(entries: list[dict]) -> list[dict]:
    """把 entry 串成一条分支(补 parentId,与 store.append 的语义一致)。"""
    parent = None
    for index, entry in enumerate(entries):
        entry.setdefault("id", f"e{index}")
        entry["parentId"] = parent
        parent = entry["id"]
    return entries


def _turns(count: int, size: int = 400) -> list[dict]:
    """count 个 turn(每个 user+assistant+tool),每条约 size 字符。"""
    entries: list[dict] = []
    for n in range(count):
        entries += [
            {"type": "message", "role": "user", "content": f"Q{n} " + "x" * size},
            {"type": "message", "role": "assistant", "content": "a" * size},
            {"type": "tool", "tool": "ls", "args": {}, "status": "ok", "result": "r" * size},
        ]
    return _chain(entries)


# ── 1. 估算与切点 ────────────────────────────────────────


def test_estimate_tokens_matches_pi_heuristic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 5) == 2          # ceil(5/4)
    assert estimate_tokens("a" * 4000) == 1000


def test_entry_tokens_counts_only_context_visible_parts():
    assert entry_tokens({"type": "message", "role": "user", "content": "a" * 40}) == 10
    assert entry_tokens({"type": "message", "role": "assistant", "content": "a" * 40}) == 10
    # tool entry 在 qi 里**不进跨轮上下文**(_history 只读 message),所以算 0;
    # 但它会进**摘要输入**(见 is_summarizable_entry)
    assert entry_tokens({"type": "tool", "args": {"x": "y" * 400}, "result": "a" * 40}) == 0
    assert is_summarizable_entry({"type": "tool", "result": "x"}) is True
    assert is_context_entry({"type": "tool", "result": "x"}) is False
    assert entry_tokens({"type": "compaction", "summary": "a" * 40}) == 10
    assert entry_tokens({"type": "branch_summary", "summary": "a" * 40}) == 10
    # 叙述 / dispatch / state 不进上下文 → 0(与 runtime._history 一致)
    assert entry_tokens({"type": "custom", "custom_type": "assistant_narration",
                         "content": "a" * 400}) == 0
    assert entry_tokens({"type": "dispatch", "agent": "g"}) == 0
    assert entry_tokens({"type": "state", "key": "active_agent", "value": "g"}) == 0


def test_context_tokens_sums_visible_entries():
    branch = _turns(2, size=40)
    # 2 个 turn × (user 43 + assistant 40 + tool 40) chars → /4
    assert context_tokens(branch) == sum(entry_tokens(e) for e in branch)
    assert context_tokens(branch) > 0


def test_should_compact_threshold():
    assert should_compact(90_000, 100_000, reserve_tokens=16_384) is True
    assert should_compact(80_000, 100_000, reserve_tokens=16_384) is False
    assert should_compact(90_000, 100_000, enabled=False) is False
    assert should_compact(90_000, 0) is False            # 窗口未知:不擅自压


def test_cut_point_prefers_turn_boundary_and_keeps_recent():
    """预算正好跨在 turn 边界上 → 普通切点(不 split);跨在 turn 中间 → split turn。"""
    branch = _turns(6)                    # 每 turn 上下文 200 token(user+assistant;tool 不计)

    cut, turn_start, split = find_cut_point(branch, 0, len(branch), keep_recent_tokens=400)
    assert split is False and turn_start == -1
    assert is_turn_start(branch[cut])                    # 保留段从某轮 user 开始
    assert context_tokens(branch[cut:]) >= 400
    assert context_tokens(branch[:cut]) > 0              # 确实有东西被压掉

    cut2, turn_start2, split2 = find_cut_point(branch, 0, len(branch), keep_recent_tokens=650)
    assert split2 is True                                # 预算在 turn 中间就被跨过
    assert turn_start2 != -1 and turn_start2 < cut2
    assert is_turn_start(branch[turn_start2])            # 前缀从那一轮的 user 开始
    assert branch[cut2]["type"] != "tool"


def test_cut_point_never_lands_on_tool_result():
    branch = _turns(6)
    for keep in (50, 100, 300, 600, 900, 1200, 1500):
        cut, _turn, _split = find_cut_point(branch, 0, len(branch), keep_recent_tokens=keep)
        assert branch[cut]["type"] != "tool", f"keep={keep} 切到了 tool 结果上"


def test_split_turn_when_one_turn_exceeds_budget():
    """单个 turn 就超预算 → 从中间切,并给出 turn 前缀(pi 的 split turn)。"""
    entries = _chain([
        {"type": "message", "role": "user", "content": "big " + "x" * 8000},
        {"type": "message", "role": "assistant", "content": "a" * 8000},
        {"type": "tool", "tool": "ls", "args": {}, "status": "ok", "result": "r" * 8000},
    ])
    cut, turn_start, split = find_cut_point(entries, 0, len(entries), keep_recent_tokens=200)
    assert split is True
    assert turn_start == 0                       # 前缀从这轮的 user 开始
    assert cut > turn_start                      # 切在 turn 中间(assistant)
    assert entries[cut]["type"] != "tool"


def test_prepare_compaction_skips_short_session():
    assert prepare_compaction(_turns(1, size=40)) is None       # 没什么可压
    assert prepare_compaction([]) is None
    # 最后一条就是 compaction(刚压过,没有新内容)→ 不再压
    branch = _chain([{"type": "message", "role": "user", "content": "x" * 400},
                     {"type": "compaction", "summary": "s", "firstKeptEntryId": "e0"}])
    assert prepare_compaction(branch) is None


def test_prepare_compaction_produces_plan():
    branch = _turns(6)
    prep = prepare_compaction(branch, keep_recent_tokens=650)
    assert prep is not None
    assert not prep.empty
    assert prep.tokens_before == context_tokens(branch)
    assert prep.previous_summary is None
    assert prep.messages_to_summarize
    # 切点是保留段的第一条;「历史摘要 + turn 前缀」合起来正好覆盖它前面的内容
    kept_index = next(i for i, e in enumerate(branch) if e["id"] == prep.first_kept_entry_id)
    covered = [*prep.messages_to_summarize, *prep.turn_prefix_messages]
    assert [e["id"] for e in covered] == \
        [e["id"] for e in branch[:kept_index] if is_summarizable_entry(e)]


def test_repeated_compaction_uses_previous_summary_and_boundary():
    """第二次压缩:从上次的 firstKeptEntryId 起重新摘要,并把旧摘要当迭代上下文。"""
    branch = _turns(6)
    prep = prepare_compaction(branch, keep_recent_tokens=400)
    assert prep is not None
    kept_index = next(i for i, e in enumerate(branch) if e["id"] == prep.first_kept_entry_id)
    kept_id = branch[kept_index]["id"]

    # 第一次压缩:把 compaction entry 追加在末尾(pi 就是这样 —— 它不移动 leaf)
    branch.append({"id": "cmp1", "parentId": branch[-1]["id"], "type": "compaction",
                   "summary": "旧摘要", "firstKeptEntryId": kept_id})
    # 之后又跑了两轮(新的内容挂在 compaction 之后)
    branch = _chain([*branch, *[
        {"type": "message", "role": "user", "content": "new " + "x" * 2000},
        {"type": "message", "role": "assistant", "content": "b" * 2000},
    ]])

    prep2 = prepare_compaction(branch, keep_recent_tokens=400)
    assert prep2 is not None
    assert prep2.previous_summary == "旧摘要"
    # 边界回到上次保留段的开头(而不是 compaction 自己),所以老消息会被重新纳入摘要
    assert kept_id in {e["id"] for e in [*prep2.messages_to_summarize,
                                         *prep2.turn_prefix_messages]}


# ── 2. 摘要调用 ──────────────────────────────────────────


def test_serialize_conversation_shape():
    text = serialize_conversation([
        {"type": "message", "role": "user", "content": "你好"},
        {"type": "message", "role": "assistant", "content": "在"},
        {"type": "tool", "tool": "ls", "result": "a.txt"},
        {"type": "custom", "custom_type": "assistant_narration", "content": "不该出现"},
        {"type": "compaction", "summary": "旧摘要"},
    ])
    assert "[User]: 你好" in text
    assert "[Assistant]: 在" in text
    assert "[Tool ls]: a.txt" in text
    assert "不该出现" not in text                 # 叙述不进上下文,也不进摘要输入
    assert "[Previous summary]: 旧摘要" in text


def test_summary_context_message_is_user_role():
    msg = summary_context_message("## Goal\n干活")
    assert msg.role == "user"
    assert "上下文已压缩" in msg.content and "## Goal" in msg.content
    assert "另一条分支的摘要" in summary_context_message("x", kind="branch").content


class _StubLLM:
    def __init__(self, text: str = "## Goal\n摘要正文"):
        self.text = text
        self.calls: list[list] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append(messages)
        return ChatResponse(text=self.text, usage={"prompt_tokens": 11, "completion_tokens": 22})


@pytest.mark.asyncio
async def test_compact_builds_entry_and_prompt():
    branch = _turns(6)
    prep = prepare_compaction(branch, keep_recent_tokens=400)
    assert prep is not None and not prep.is_split_turn
    llm = _StubLLM()
    entry = await compact(llm, prep)

    assert entry["type"] == "compaction"
    assert entry["summary"] == "## Goal\n摘要正文"
    assert entry["firstKeptEntryId"] == prep.first_kept_entry_id
    assert entry["tokensBefore"] == prep.tokens_before
    assert entry["usage"]["prompt_tokens"] == 11

    sent = "\n".join(m.content for m in llm.calls[0])
    assert "context summarization assistant" in sent          # system prompt
    assert "<conversation>" in sent and "[User]: Q0" in sent
    assert SUMMARIZATION_PROMPT in sent                        # 首次压缩用初始提示词
    assert UPDATE_SUMMARIZATION_PROMPT not in sent


@pytest.mark.asyncio
async def test_compact_with_instructions_and_previous_summary():
    branch = _turns(6)
    prep = prepare_compaction(branch, keep_recent_tokens=650)
    assert prep is not None
    prep.previous_summary = "旧摘要"
    llm = _StubLLM()
    await compact(llm, prep, instructions="重点写依赖变更")

    sent = "\n".join(m.content for m in llm.calls[0])
    assert UPDATE_SUMMARIZATION_PROMPT in sent
    assert "<previous-summary>\n旧摘要\n</previous-summary>" in sent
    assert "Additional focus: 重点写依赖变更" in sent


@pytest.mark.asyncio
async def test_compact_split_turn_merges_two_summaries():
    """split turn:历史摘要 + 前缀摘要拼在一起;没有历史时只调一次前缀摘要。"""
    # (a) 只有一个超大 turn → 没有历史,只摘前缀
    only_prefix = _chain([
        {"type": "message", "role": "user", "content": "big " + "x" * 8000},
        {"type": "message", "role": "assistant", "content": "a" * 8000},
        {"type": "tool", "tool": "ls", "args": {}, "status": "ok", "result": "r" * 8000},
    ])
    prep = prepare_compaction(only_prefix, keep_recent_tokens=200)
    assert prep is not None and prep.is_split_turn and prep.messages_to_summarize == []
    llm = _StubLLM("## Goal\n一段")
    entry = await compact(llm, prep)
    assert "Turn Context (split turn)" in entry["summary"]
    assert len(llm.calls) == 1
    assert "PREFIX of a turn" in "\n".join(m.content for m in llm.calls[-1])

    # (b) 前面还有正常历史 → 两次调用(历史用初始提示词,前缀用 TURN_PREFIX)+ 拼接
    with_history = _chain([
        {"type": "message", "role": "user", "content": "先来一轮 " + "x" * 2000},
        {"type": "message", "role": "assistant", "content": "好 " + "y" * 2000},
        {"type": "message", "role": "user", "content": "然后一个超大 turn " + "z" * 12000},
        {"type": "message", "role": "assistant", "content": "a" * 12000},
    ])
    prep2 = prepare_compaction(with_history, keep_recent_tokens=300)
    assert prep2 is not None and prep2.is_split_turn
    assert prep2.messages_to_summarize and prep2.turn_prefix_messages
    llm2 = _StubLLM("## Goal\n两段")
    entry2 = await compact(llm2, prep2)
    assert len(llm2.calls) == 2
    assert "Turn Context (split turn)" in entry2["summary"]
    assert "两段\n\n---\n\n**Turn Context" in entry2["summary"]
    assert entry2["usage"]["completion_tokens"] == 44          # 两次调用用量相加


@pytest.mark.asyncio
async def test_summarize_branch_uses_branch_prompt():
    llm = _StubLLM("## Goal\n分支摘要")
    summary, usage = await summarize_branch(llm, _turns(1, size=40))
    assert summary == "## Goal\n分支摘要"
    assert usage["completion_tokens"] == 22
    assert BRANCH_SUMMARY_PROMPT in "\n".join(m.content for m in llm.calls[0])


def test_common_ancestor_and_branch_to_summarize():
    root = _chain([{"type": "message", "role": "user", "content": "Q0"},
                   {"type": "message", "role": "assistant", "content": "A0"}])
    old = [*root,
           {"id": "b1", "parentId": "e1", "type": "message", "role": "user", "content": "旧分支"},
           {"id": "b2", "parentId": "b1", "type": "message", "role": "assistant", "content": "旧回答"}]
    new = [*root,
           {"id": "c1", "parentId": "e1", "type": "message", "role": "user", "content": "新分支"}]

    assert common_ancestor(old, new) == "e1"
    picked = branch_to_summarize(old, "b2", new)
    assert [e["content"] for e in picked] == ["旧分支", "旧回答"]
    assert branch_to_summarize(new, "c1", new) == []          # 都在同一分支上:没有可摘要的


# ── 3. 运行时集成 ────────────────────────────────────────


_MODELS = '{"providers": {"o": {"api": "openai-completions", "models": [{"id": "x"}]}}}'


def _env(tmp_path: Path, monkeypatch, *, reserve: int, enabled: bool = True) -> None:
    from qi_agent import paths

    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        '{"defaultProvider": "o", "defaultModel": "x", "compaction": '
        f'{{"enabled": {str(enabled).lower()}, "reserveTokens": {reserve}, '
        '"keepRecentTokens": 600}}', encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))


class _RuntimeLLM:
    """既当执行模型又当摘要模型:靠 system prompt 区分。"""

    def __init__(self, window: int = 2000, answer: str = "ok"):
        self.spec = SimpleNamespace(context_window=window)
        self.answer = answer
        self.summaries = 0

    async def chat(self, messages, tools=None, temperature=None):
        joined = "\n".join(m.content for m in messages)
        if "context summarization assistant" in joined:
            self.summaries += 1
            return ChatResponse(text="## Goal\n自动压缩出的摘要",
                                usage={"prompt_tokens": 3, "completion_tokens": 4})
        return ChatResponse(text=self.answer)


def test_history_uses_summary_plus_kept_messages(tmp_path):
    store = SessionStore(root=tmp_path / "sessions")
    session = store.create("t", cwd=tmp_path)
    store.append(session, {"type": "message", "role": "user", "content": "Q1"})
    store.append(session, {"type": "message", "role": "assistant", "content": "A1"})
    kept_id = session.entries[-1]["id"]
    store.append(session, {"type": "message", "role": "user", "content": "Q2"})
    store.append(session, {"type": "compaction", "summary": "## Goal\n早前的活儿",
                           "firstKeptEntryId": kept_id, "tokensBefore": 999})
    store.append(session, {"type": "branch_summary", "summary": "另一条分支的活儿"})

    from qi_agent.runtime import QiRuntime

    runtime = object.__new__(QiRuntime)          # 只测纯方法
    history = runtime._history(session)
    assert "上下文已压缩" in history[0].content and "早前的活儿" in history[0].content
    # Q1 被压掉了,保留段从 firstKeptEntryId 开始
    assert [m.content for m in history[1:]] == ["A1", "Q2",
                                                "[另一条分支的摘要]\n另一条分支的活儿\n[摘要结束,以下是其后的消息]"]


@pytest.mark.asyncio
async def test_runtime_auto_compaction_triggers_and_appends_entry(tmp_path, monkeypatch):
    from qi_agent.runtime import QiRuntime

    monkeypatch.chdir(tmp_path)
    _env(tmp_path, monkeypatch, reserve=100)
    llm = _RuntimeLLM(window=1500)
    runtime = QiRuntime(cwd=tmp_path, disable_router=True, llm=llm)

    session = runtime.sessions.create("t", cwd=tmp_path)
    for n in range(6):                            # 灌到超过 1500-100 token
        runtime.sessions.append(session, {"type": "message", "role": "user",
                                          "content": f"Q{n} " + "x" * 1200})
        runtime.sessions.append(session, {"type": "message", "role": "assistant",
                                          "content": "a" * 1200})
    assert context_tokens(session.branch()) > 1400

    events = [e async for e in runtime.stream("新问题", session)]
    kinds = [e.kind for e in events]
    assert "compaction_start" in kinds
    assert kinds.index("compaction_start") < kinds.index("dispatch")   # 压缩在开跑之前
    assert "compaction_end" in kinds
    assert llm.summaries == 1

    # 再跑一轮:上下文已经被压小,不该再触发(回归:曾用原始 entry 估算 → 每轮重复压)
    events2 = [e async for e in runtime.stream("又一句", session)]
    assert "compaction_start" not in [e.kind for e in events2]
    assert llm.summaries == 1

    compaction = next(e for e in session.entries if e.get("type") == "compaction")
    assert compaction["summary"].startswith("## Goal")
    assert compaction["firstKeptEntryId"]
    # 压缩后历史 = 摘要 + 保留段(不再是从头开始的全部消息)
    history = runtime._history(session)
    assert "上下文已压缩" in history[0].content
    assert len(history) < 13


@pytest.mark.asyncio
async def test_runtime_auto_compaction_respects_disabled(tmp_path, monkeypatch):
    from qi_agent.runtime import QiRuntime

    monkeypatch.chdir(tmp_path)
    _env(tmp_path, monkeypatch, reserve=100, enabled=False)
    llm = _RuntimeLLM(window=1500)
    runtime = QiRuntime(cwd=tmp_path, disable_router=True, llm=llm)
    session = runtime.sessions.create("t", cwd=tmp_path)
    for n in range(6):
        runtime.sessions.append(session, {"type": "message", "role": "user",
                                          "content": f"Q{n} " + "x" * 1200})

    events = [e async for e in runtime.stream("新问题", session)]
    assert "compaction_start" not in [e.kind for e in events]
    assert llm.summaries == 0
    assert not any(e.get("type") == "compaction" for e in session.entries)


@pytest.mark.asyncio
async def test_runtime_manual_compact_and_branch_summary(tmp_path, monkeypatch):
    from qi_agent.runtime import QiRuntime

    monkeypatch.chdir(tmp_path)
    _env(tmp_path, monkeypatch, reserve=100)
    llm = _RuntimeLLM(window=100000)             # 窗口很大 → 不会自动压
    runtime = QiRuntime(cwd=tmp_path, disable_router=True, llm=llm)
    session = runtime.sessions.create("t", cwd=tmp_path)
    for n in range(6):
        runtime.sessions.append(session, {"type": "message", "role": "user",
                                          "content": f"Q{n} " + "x" * 1200})
        runtime.sessions.append(session, {"type": "message", "role": "assistant",
                                          "content": "a" * 1200})
    fork_point = session.entries[1]["id"]        # 第一条 user 消息
    source_branch = session.branch()             # 跳之前的快照
    abandoned_leaf = session.current
    assert abandoned_leaf is not None

    entry = await runtime.compact_session(session, "重点写测试")
    assert entry is not None and entry["type"] == "compaction"
    assert llm.summaries == 1

    # 分支摘要:从被放弃的 leaf 回到当前分支的祖先
    assert runtime.sessions.set_position(session, fork_point)
    summary_entry = await runtime.summarize_branch_for_jump(session, source_branch,
                                                            abandoned_leaf, fork_point)
    assert summary_entry is not None and summary_entry["type"] == "branch_summary"
    assert summary_entry["summary"].startswith("## Goal")
    assert summary_entry["fromId"] == abandoned_leaf
    # 摘要是挂在跳转点下面的 → 成了当前 leaf
    assert session.current == session.entries[-1]["id"]
    assert session.entries[-1]["type"] == "branch_summary"
    assert any(e.get("type") == "branch_summary" for e in session.branch())

    # 没有可摘要的分支时返回 None(不写空 entry)
    assert await runtime.summarize_branch_for_jump(session, source_branch,
                                                   session.current, fork_point) is None


@pytest.mark.asyncio
async def test_compact_session_returns_none_when_nothing_to_do(tmp_path, monkeypatch):
    from qi_agent.runtime import QiRuntime

    monkeypatch.chdir(tmp_path)
    _env(tmp_path, monkeypatch, reserve=100)
    runtime = QiRuntime(cwd=tmp_path, disable_router=True, llm=_RuntimeLLM(window=100000))
    session = runtime.sessions.create("t", cwd=tmp_path)
    runtime.sessions.append(session, {"type": "message", "role": "user", "content": "太短"})
    assert await runtime.compact_session(session) is None
    assert not any(e.get("type") == "compaction" for e in session.entries)


def test_preparation_dataclass_defaults():
    prep = Preparation(first_kept_entry_id="x")
    assert prep.empty is True
    assert prep.tokens_before == 0 and prep.previous_summary is None
