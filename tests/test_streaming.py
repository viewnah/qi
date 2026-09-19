"""流式契约测试(text_delta / LLMDelta / tool_call 增量合并 / litellm 容错)。

两层:
  1. **纯函数层**:`merge_tool_call_delta` / `merged_tool_calls` —— 流式工具调用是拆片发的,
     按 index 归并、参数拼接,这类逻辑最容易写错,所以直接单测,不经过 litellm。
  2. **调用层**:`stream_llm` 的统一入口形状、AgentRunner 的 text_delta 顺序、
     `LiteLLMClient.astream` 的两处容错(stream_options 被拒 / 流式不支持)。

litellm 用 monkeypatch 的假 `acompletion` 替换:不联网、不依赖 provider。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from qi_agent.system_prompt import build_system_prompt  # noqa: E402

from qi_agent.abort import AbortSignal
from qi_agent.auth import AuthStore
from qi_agent.config import ResolvedModel
from qi_agent.llm import (
    ChatResponse,
    LiteLLMClient,
    LLMDelta,
    ToolCallOut,
    merge_tool_call_delta,
    merged_tool_calls,
    stream_llm,
)
from qi_agent.registry import ToolCatalog
from qi_agent.runner import AgentRunner, RunnerSettings, RunSpec, stop_after_turns
from qi_agent.tools import ToolContext, register_builtin_tools


# ── 测试替身 ──────────────────────────────────────────────

class ChatOnlyLLM:
    """只实现 chat(旧约定/第三方实现):必须仍能工作。"""

    def __init__(self, resp: ChatResponse):
        self.resp = resp
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=None):
        self.calls += 1
        return self.resp


class StreamingStubLLM:
    """逐字流式的替身:按脚本发文本增量,末尾给 tool_calls / usage。"""

    def __init__(self, script: list[list[LLMDelta]]):
        self.script = list(script)
        self.seen: list[list] = []

    async def chat(self, messages, tools=None, temperature=None):  # pragma: no cover
        raise AssertionError("有 astream 时不应调用 chat")

    async def astream(self, messages, tools=None, temperature=None):
        self.seen.append(list(messages))
        for delta in (self.script.pop(0) if self.script else []):
            yield delta
        if not self.script:
            return


def _text_delta(text: str) -> LLMDelta:
    return LLMDelta(text=text)


def _finished(tool_calls=None, usage=None) -> LLMDelta:
    return LLMDelta(finished=True, tool_calls=tool_calls or [], usage=usage or {})


def _catalog() -> ToolCatalog:
    c = ToolCatalog()
    register_builtin_tools(c)
    return c


def _agent_unit(tmp_path, catalog, name: str = "w"):
    """一个最小**运行单元**(P-E4c 起 core 不再有 agent,所以直接造 spec)。

    名字与签名保持原样只是为了少改调用点;`tmp_path` 不再需要 —— 角色(agent.md)归 qi-agents。
    """
    del tmp_path
    tools = sorted(catalog.names)
    return RunSpec(name=name,
                   prompt=build_system_prompt(None, tools=catalog.resolve(tools)),
                   tools=tools)
def _spec() -> ResolvedModel:
    return ResolvedModel(provider="ollama", model="x", api="openai-completions",
                         base_url="http://localhost:11434/v1", api_key_ref=None,
                         reasoning=False, context_window=8192, max_tokens=1024)


def _client(tmp_path: Path) -> LiteLLMClient:
    return LiteLLMClient(_spec(), AuthStore(path=tmp_path / "auth.json"))


# litellm 流式 chunk 的形状:chunk.choices[0].delta.content / .tool_calls[i].function.arguments

def _chunk(text=None, tool_calls=None, usage=None):
    delta = SimpleNamespace(content=text, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)


def _tc(index, id=None, name=None, args=None):
    fn = SimpleNamespace(name=name, arguments=args)
    return SimpleNamespace(index=index, id=id, function=fn)


def _completion(text: str = "", tool_calls=None, usage=None):
    """伪造**非流式** ModelResponse 的形状:choices[0].message。"""
    msg = SimpleNamespace(content=text, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


class _FakeAcompletion:
    """假的 litellm.acompletion。

    形状要与真 API 一致:`stream=True` → 异步迭代器(逐块);否则 → ModelResponse。
    否则"流式失败后降级到 chat()"这条链会被假对象蒙过去。
    """

    def __init__(self, chunks=None, fail_if=None, fail_always=False, completion=None):
        self.chunks = chunks or []
        self.fail_if = fail_if            # 返回 True 的 kwargs 组合 → 抛错
        self.fail_always = fail_always
        self.completion = completion      # 非流式时返回的 ModelResponse 形状
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_always or (self.fail_if and self.fail_if(kwargs)):
            raise RuntimeError("provider 拒绝该参数")
        if not kwargs.get("stream"):
            return self.completion if self.completion is not None else _completion("")

        async def _gen():
            for c in self.chunks:
                yield c

        return _gen()

    @property
    def stream_calls(self) -> list[dict]:
        return [c for c in self.calls if c.get("stream")]


@pytest.fixture
def fake_litellm(monkeypatch):
    import litellm

    def install(fake):
        monkeypatch.setattr(litellm, "acompletion", fake)
        return fake

    return install


# ── 1. 纯函数:tool_call 增量合并 ──────────────────────────

def test_merge_tool_call_delta_joins_argument_fragments():
    """首片带 id+name,后续片只带参数碎片 → 必须按 index 归并、参数拼接。"""
    merged: dict[int, dict] = {}
    merge_tool_call_delta(merged, _tc(0, id="call_1", name="grep", args='{"pat'))
    merge_tool_call_delta(merged, _tc(0, args='tern": "hel'))
    merge_tool_call_delta(merged, _tc(0, args='lo"}'))

    calls = merged_tool_calls(merged)
    assert len(calls) == 1
    assert calls[0].id == "call_1" and calls[0].name == "grep"
    assert calls[0].args == {"pattern": "hello"}


def test_merge_tool_call_delta_handles_interleaved_parallel_calls():
    """并行调用是**交错**发片的:按 index 分槽,不能靠到达顺序拼。"""
    merged: dict[int, dict] = {}
    merge_tool_call_delta(merged, _tc(0, id="a", name="ls", args='{"path"'))
    merge_tool_call_delta(merged, _tc(1, id="b", name="read", args='{"path"'))
    merge_tool_call_delta(merged, _tc(0, args=': "."}'))
    merge_tool_call_delta(merged, _tc(1, args=': "x.py"}'))

    calls = merged_tool_calls(merged)
    assert [(c.name, c.args) for c in calls] == [("ls", {"path": "."}), ("read", {"path": "x.py"})]
    assert [c.id for c in calls] == ["a", "b"]


def test_merged_tool_calls_skips_nameless_and_tolerates_bad_json():
    """无名片段(部分 provider 的空壳)跳过;参数 JSON 坏了退化为 {},不打挂整轮。"""
    merged: dict[int, dict] = {}
    merge_tool_call_delta(merged, _tc(0, args='{"a": 1}'))          # 一直没有 name
    merge_tool_call_delta(merged, _tc(1, name="grep", args='{"broken":'))

    calls = merged_tool_calls(merged)
    assert len(calls) == 1
    assert calls[0].name == "grep" and calls[0].args == {}
    assert calls[0].id == "call_1"                                   # 无 id 时给出稳定兜底


def test_merge_tool_call_delta_without_index_uses_slot_order():
    """没有 index 的 provider:退化为按出现顺序开新槽,而不是覆盖同一个。"""
    merged: dict[int, dict] = {}
    merge_tool_call_delta(merged, _tc(None, name="a", args="{}"))
    merge_tool_call_delta(merged, _tc(None, name="b", args="{}"))
    assert [c.name for c in merged_tool_calls(merged)] == ["a", "b"]


# ── 2. 调用层:stream_llm 统一入口 ────────────────────────

@pytest.mark.asyncio
async def test_stream_llm_falls_back_to_chat_without_astream():
    """只有 chat 的实现 → 整段文本作为**单块** + 终止块。形状对消费者统一。"""
    llm = ChatOnlyLLM(ChatResponse(text="完整回答",
                                   tool_calls=[ToolCallOut(id="c", name="ls", args={})],
                                   usage={"total_tokens": 7}))
    deltas = [d async for d in stream_llm(llm, [], tools=None)]
    assert [d.text for d in deltas if d.text] == ["完整回答"]
    assert deltas[-1].finished is True
    assert deltas[-1].tool_calls[0].name == "ls"
    assert deltas[-1].usage == {"total_tokens": 7}
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_stream_llm_prefers_astream_when_present():
    """有 astream 的实现走流式,不回落 chat。"""
    llm = StreamingStubLLM([[_text_delta("你"), _text_delta("好"), _finished()]])
    deltas = [d async for d in stream_llm(llm, [], tools=None)]
    assert [d.text for d in deltas if d.text] == ["你", "好"]
    assert deltas[-1].finished is True


# ── 3. AgentRunner:text_delta 的顺序与汇总 ────────────────

@pytest.mark.asyncio
async def test_runner_emits_text_deltas_in_true_order(tmp_path):
    """工具调用**之间**的叙述不再丢弃,且与工具事件保持真实因果顺序。"""
    catalog = _catalog()
    unit = _agent_unit(tmp_path, catalog)
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    llm = StreamingStubLLM([
        # 第 1 轮:边说边调工具
        [_text_delta("我先"), _text_delta("查一下"), _finished(
            tool_calls=[ToolCallOut(id="c1", name="grep",
                                    args={"pattern": "hello", "path": "a.txt"})],
            usage={"prompt_tokens": 10})],
        # 第 2 轮:给结论
        [_text_delta("找到"), _text_delta("了"), _finished(usage={"prompt_tokens": 20})],
    ])
    runner = AgentRunner(unit, catalog, llm, RunnerSettings(stop_after=stop_after_turns(5)),
                         tool_ctx=ToolContext(agent_name=unit.name, workdir=tmp_path))
    events = [e async for e in runner.run("找 hello")]

    kinds = [e.kind for e in events]
    # 顺序:第 1 轮叙述 → tool_start → tool_end → 第 2 轮叙述
    assert kinds.index("text_delta") < kinds.index("tool_start")
    assert kinds.index("tool_start") < kinds.index("tool_end")
    assert kinds.index("tool_end") < kinds.index("text_delta", kinds.index("tool_end"))

    # 增量拼接 == 最终 text 事件(向后兼容:CLI/TUI 仍读 text)
    deltas = "".join(e.text for e in events if e.kind == "text_delta")
    final = next(e for e in events if e.kind == "text").text
    assert deltas == "我先查一下找到 了".replace(" ", "")
    assert final == "找到了"

    # 进模型的 assistant 消息也必须是完整文本(否则上下文缺字)
    assistant = [m for m in llm.seen[1] if m.role == "assistant"]
    assert assistant[-1].content == "我先查一下"

    usage = next(e for e in events if e.kind == "agent_end").data["usage"]
    assert usage["prompt_tokens"] == 30 and usage["llm_calls"] == 2


@pytest.mark.asyncio
async def test_runner_keeps_working_with_chat_only_llm(tmp_path):
    """旧实现(只有 chat)→ 退化路径也要发出 text_delta 与 text,且内容一致。"""
    catalog = _catalog()
    unit = _agent_unit(tmp_path, catalog)
    llm = ChatOnlyLLM(ChatResponse(text="就这些"))
    runner = AgentRunner(unit, catalog, llm, RunnerSettings(stop_after=stop_after_turns(3)),
                         tool_ctx=ToolContext(agent_name=unit.name, workdir=tmp_path))
    events = [e async for e in runner.run("hi")]
    assert [e.text for e in events if e.kind == "text_delta"] == ["就这些"]
    assert next(e for e in events if e.kind == "text").text == "就这些"


# ── 4. LiteLLMClient.astream:真实流式 + 两处容错 ────────────

@pytest.mark.asyncio
async def test_litellm_astream_streams_text_and_reports_usage(tmp_path, fake_litellm):
    """正常流:逐块文本 + 末块 usage + 合并后的 tool_calls;请求带 include_usage。"""
    fake = fake_litellm(_FakeAcompletion(chunks=[
        _chunk(text="你好"),
        _chunk(text=",世界"),
        _chunk(tool_calls=[_tc(0, id="c1", name="ls", args='{"path"')]),
        _chunk(tool_calls=[_tc(0, args=': "."}')]),
        _chunk(usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}),
    ]))
    deltas = [d async for d in _client(tmp_path).astream([])]

    assert [d.text for d in deltas if d.text] == ["你好", ",世界"]
    done = deltas[-1]
    assert done.finished and done.tool_calls[0].args == {"path": "."}
    assert done.usage == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    assert fake.stream_calls[0]["stream"] is True
    assert fake.stream_calls[0]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_litellm_astream_retries_without_stream_options(tmp_path, fake_litellm):
    """容错 1:provider 拒绝 stream_options → 去掉该参数重试一次,并且**粘性**记住。"""
    fake = fake_litellm(_FakeAcompletion(
        chunks=[_chunk(text="ok")],
        fail_if=lambda kw: "stream_options" in kw,
    ))
    client = _client(tmp_path)
    deltas = [d async for d in client.astream([])]
    assert [d.text for d in deltas if d.text] == ["ok"]
    assert "stream_options" not in fake.stream_calls[-1]
    assert client._no_usage_opt is True

    # 第二次调用不再先试带 stream_options 的那次(否则每轮白废一次请求)
    before = len(fake.stream_calls)
    _ = [d async for d in client.astream([])]
    assert all("stream_options" not in c for c in fake.stream_calls[before:])


@pytest.mark.asyncio
async def test_litellm_astream_falls_back_to_chat_when_streaming_unsupported(tmp_path, fake_litellm):
    """容错 2:流式根本打不开 → 走 chat() 非流式降级,且降级是粘性的(不每轮重试)。"""
    fake = fake_litellm(_FakeAcompletion(
        fail_if=lambda kw: kw.get("stream") is True,
        completion=_completion("非流式回答"),
    ))
    client = _client(tmp_path)
    deltas = [d async for d in client.astream([])]
    # 降级路径:整段文本作为单块 + 终止块(形状不变,只是不再逐字)
    assert [d.text for d in deltas if d.text] == ["非流式回答"]
    assert deltas[-1].finished is True
    assert client._stream_broken is True

    before = len(fake.stream_calls)
    _ = [d async for d in client.astream([])]
    assert len(fake.stream_calls) == before      # 不再尝试流式请求


@pytest.mark.asyncio
async def test_litellm_astream_does_not_retry_after_partial_text(tmp_path, fake_litellm):
    """已吐出部分文本后失败 → 直接抛,不降级重试(重试会导致文本重复)。"""
    class _MidFailure(_FakeAcompletion):
        async def __call__(self, **kwargs):
            self.calls.append(kwargs)

            async def _gen():
                yield _chunk(text="前半段")
                raise RuntimeError("流中断")

            return _gen()

    fake_litellm(_MidFailure())
    client = _client(tmp_path)
    seen: list[str] = []
    with pytest.raises(RuntimeError, match="流中断"):
        async for delta in client.astream([]):
            if delta.text:
                seen.append(delta.text)
    assert seen == ["前半段"]                  # 已发出的部分不会被重复
    assert client._stream_broken is False      # 这不是"不支持流式"


@pytest.mark.asyncio
async def test_litellm_astream_tolerates_empty_choices_chunks(tmp_path, fake_litellm):
    """usage 块常常 choices 为空:不能因此 IndexError。"""
    fake_litellm(_FakeAcompletion(chunks=[
        _chunk(text="a"),
        SimpleNamespace(choices=[], usage={"total_tokens": 3}),
    ]))
    deltas = [d async for d in _client(tmp_path).astream([])]
    assert [d.text for d in deltas if d.text] == ["a"]
    assert deltas[-1].usage == {"total_tokens": 3}


# ── 5. 挂住的流:不再有回合级超时,只能靠中断(对齐 pi)────────

@pytest.mark.asyncio
async def test_hung_stream_is_not_killed_by_a_loop_timeout(tmp_path):
    """qi 曾经在 agent loop 外套 `asyncio.timeout(600)`:一次慢请求会把**整轮**打成
    “执行超时”错误。pi 的超时在 provider/SDK 那一层(retry.provider.timeoutMs +
    客户端重试),所以这里不再有任何回合级超时 —— 挂住的流只由中断结束。
    """
    catalog = _catalog()
    unit = _agent_unit(tmp_path, catalog)

    class HangingLLM:
        async def chat(self, messages, tools=None, temperature=None):  # pragma: no cover
            raise AssertionError("有 astream 时不应调用 chat")

        async def astream(self, messages, tools=None, temperature=None):
            yield _text_delta("开始")
            await asyncio.sleep(30)             # 挂住(旧实现会在这里触发“执行超时”)
            yield _finished()

    abort = AbortSignal()

    async def fire() -> None:
        await asyncio.sleep(0.1)
        abort.abort()

    runner = AgentRunner(unit, catalog, HangingLLM(), RunnerSettings(),
                         tool_ctx=ToolContext(agent_name=unit.name, workdir=tmp_path))

    async def consume() -> list:
        return [e async for e in runner.run("hi", abort=abort)]

    events, _ = await asyncio.wait_for(asyncio.gather(consume(), fire()), timeout=10)
    # 没有“执行超时”错误 —— 结束是中断驱动的
    assert not [e for e in events if e.kind == "error"]
    assert [e.text for e in events if e.kind == "text_delta"] == ["开始"]
    end = next(e for e in events if e.kind == "agent_end")
    assert end.data["aborted"] is True
    assert "开始" in end.text                      # 已流出的部分保留


# ── 6. 叙述落盘:直播与回放必须一致 ───────────────────────

@pytest.mark.asyncio
async def test_runner_declares_each_assistant_message(tmp_path):
    """每轮 LLM 回复单独声明一次,并带上该轮宣布的工具名。"""
    catalog = _catalog()
    unit = _agent_unit(tmp_path, catalog)
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    llm = StreamingStubLLM([
        [_text_delta("我先查一下"), _finished(
            tool_calls=[ToolCallOut(id="c1", name="grep",
                                    args={"pattern": "hello", "path": "a.txt"})])],
        [_text_delta("找到了"), _finished()],
    ])
    runner = AgentRunner(unit, catalog, llm, RunnerSettings(stop_after=stop_after_turns(5)),
                         tool_ctx=ToolContext(agent_name=unit.name, workdir=tmp_path))
    events = [e async for e in runner.run("找 hello")]

    declared = [e for e in events if e.kind == "assistant_message"]
    assert [e.text for e in declared] == ["我先查一下", "找到了"]
    assert [e.data["step"] for e in declared] == [1, 2]
    assert declared[0].data["tool_calls"] == ["grep"]     # 第 1 轮宣布了工具
    assert declared[1].data["tool_calls"] == []           # 第 2 轮是最终回答


@pytest.mark.asyncio
async def test_runtime_persists_narration_before_its_tools(tmp_path, monkeypatch):
    """叙述必须**落盘且顺序正确**(在它触发的工具卡之前),但不进对话上下文。"""
    from qi_agent import paths
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore

    (tmp_path / "models.json").write_text(
        '{"providers": {"ollama": {"api": "openai-completions", "models": [{"id": "x"}]}}}',
        encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        '{"defaultProvider": "ollama", "defaultModel": "x"}', encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))

    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    sessions = SessionStore(root=tmp_path / "sess")
    llm = StreamingStubLLM([
        [_text_delta("我先查一下"), _finished(
            tool_calls=[ToolCallOut(id="c1", name="grep",
                                    args={"pattern": "hello", "path": "a.txt"})])],
        [_text_delta("找到了"), _finished()],
    ])
    rt = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                   session_store=sessions, llm=llm)
    session = sessions.create("t", cwd=tmp_path)
    _ = [e async for e in rt.stream("找 hello", session)]

    shaped = [(e.get("type"), e.get("custom_type")) for e in session.entries]
    assert ("custom", "assistant_narration") in shaped
    # 真实因果顺序:叙述 → 工具卡(而不是工具卡 → 叙述)
    assert shaped.index(("custom", "assistant_narration")) < shaped.index(("tool", None))
    narration = next(e for e in session.entries if e.get("custom_type") == "assistant_narration")
    assert narration["content"] == "我先查一下"

    # 最终回答只作为 message 出现一次(不与叙述重复)
    assert session.entries[-1]["role"] == "assistant"
    assert session.entries[-1]["content"] == "找到了"
    assert sum(1 for e in session.entries if e.get("content") == "找到了") == 1

    # 关键:叙述**不进**对话上下文(_history 只读 message)
    history = [m.content for m in rt._history(session)]
    assert "找到了" in history
    assert "我先查一下" not in history
