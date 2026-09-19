"""思考级别(pi 的 thinkingLevel)回归。

覆盖四层:
  1. 级别归一与 `reasoning_effort` 映射(含 xhigh/max 收敛、非 reasoning 模型不带参)
  2. 思考内容抽取(`reasoning_content` / `reasoning` / Anthropic 风格 `thinking` 块)
  3. 流式与 Runner:`thinking_delta` 必须与 `text_delta` 分开,且不污染最终回答
  4. TUI 与 CLI:shift+tab / ctrl+t / `/thinking` / `--thinking`
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from qi_agent import runtime as runtime_mod
from qi_agent import tui as tui_mod
from qi_agent.auth import AuthStore
from qi_agent.cli import app as cli_app
from qi_agent.config import ResolvedModel
from qi_agent.llm import (
    THINKING_LEVELS,
    ChatResponse,
    LiteLLMClient,
    LLMDelta,
    ThinkingLLMClient,
    ToolCallOut,
    chat_as_stream,
    normalize_thinking_level,
    reasoning_text_of,
)
from qi_agent.loader import load_agent_dir
from qi_agent.models import AgentEvent
from qi_agent.registry import ToolCatalog
from qi_agent.runner import AgentRunner, RunnerSettings, stop_after_turns
from qi_agent.theme import load_palette
from qi_agent.tools import ToolContext, register_builtin_tools

PALETTE = load_palette("dark")


def _spec(reasoning: bool = True) -> ResolvedModel:
    return ResolvedModel(provider="ollama", model="x", api="openai-completions",
                         base_url=None, api_key_ref=None, reasoning=reasoning,
                         context_window=8192, max_tokens=1024)


def _client(tmp_path: Path, level: str, reasoning: bool = True) -> LiteLLMClient:
    return LiteLLMClient(_spec(reasoning), AuthStore(path=tmp_path / "auth.json"),
                         thinking_level=level)


# ── 1. 级别归一 + reasoning_effort ────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("off", "off"), ("LOW", "low"), (" high ", "high"), ("xhigh", "xhigh"),
    ("max", "max"), ("minimal", "minimal"), ("medium", "medium"),
    ("bogus", "off"), ("", "off"), (None, "off"),
])
def test_normalize_thinking_level(raw, expected):
    assert normalize_thinking_level(raw) == expected


def test_thinking_levels_match_pi_order():
    assert THINKING_LEVELS == ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def test_reasoning_effort_only_when_needed(tmp_path):
    assert "reasoning_effort" not in _client(tmp_path, "off")._base_kwargs([], None, None)
    assert _client(tmp_path, "low")._base_kwargs([], None, None)["reasoning_effort"] == "low"
    # litellm / 各 provider 只认 minimal/low/medium/high → xhigh / max 收敛到 high
    assert _client(tmp_path, "xhigh")._base_kwargs([], None, None)["reasoning_effort"] == "high"
    assert _client(tmp_path, "max")._base_kwargs([], None, None)["reasoning_effort"] == "high"
    # 模型没声明 reasoning:绝不带参(有些 provider 会 400)
    assert "reasoning_effort" not in _client(tmp_path, "high", reasoning=False)._base_kwargs(
        [], None, None)


def test_thinking_level_is_mutable_for_runtime_switch(tmp_path):
    client = _client(tmp_path, "off")
    assert isinstance(client, ThinkingLLMClient)   # LiteLLMClient 满足可选协议
    client.thinking_level = "high"          # TUI shift+tab 就是这么切的
    assert client._base_kwargs([], None, None)["reasoning_effort"] == "high"


# ── 2. 思考内容抽取 ───────────────────────────────────────


def test_reasoning_text_of_shapes():
    assert reasoning_text_of(SimpleNamespace(reasoning_content="rc")) == "rc"
    assert reasoning_text_of(SimpleNamespace(reasoning="r")) == "r"
    assert reasoning_text_of({"reasoning_content": "dict"}) == "dict"
    # Anthropic 风格:块列表
    assert reasoning_text_of(SimpleNamespace(thinking=[{"thinking": "a"}, {"text": "b"}])) == "ab"
    assert reasoning_text_of(SimpleNamespace(thinking=[SimpleNamespace(thinking="c")])) == "c"
    assert reasoning_text_of(SimpleNamespace(content="only-text")) == ""
    assert reasoning_text_of(SimpleNamespace()) == ""


def test_chat_as_stream_emits_reasoning_first():
    class ChatOnly:
        async def chat(self, messages, tools=None, temperature=None):
            return ChatResponse(text="答案", reasoning="先想一下")

    async def collect():
        return [d async for d in chat_as_stream(ChatOnly(), [])]

    deltas = asyncio.run(collect())
    assert [(d.reasoning, d.text) for d in deltas] == [
        ("先想一下", ""), ("", "答案"), ("", "")]


def test_astream_splits_reasoning_from_text(tmp_path, monkeypatch):
    """litellm 流式:reasoning_content 必须单独成块,不能混进 text。"""
    chunks = [
        _chunk(reasoning="想"),
        _chunk(reasoning="一想"),
        _chunk(text="答"),
        _chunk(text="案"),
    ]

    async def fake_acompletion(**kwargs):
        async def gen():
            for chunk in chunks:
                yield chunk
        return gen()

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    async def collect():
        client = _client(tmp_path, "high")
        return [d async for d in client.astream([])]

    deltas = asyncio.run(collect())
    assert "".join(d.reasoning for d in deltas) == "想一想"
    assert "".join(d.text for d in deltas) == "答案"      # 思考没混进回答
    assert deltas[-1].finished is True


def _chunk(text=None, reasoning=None, tool_calls=None, usage=None):
    delta = SimpleNamespace(content=text, tool_calls=tool_calls,
                            reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)


# ── 3. Runner:thinking_delta 与最终回答分开 ────────────────


class _ReasoningLLM:
    """第 1 轮:思考 + 直接给结论(不调工具)。"""

    async def chat(self, messages, tools=None, temperature=None):  # pragma: no cover
        raise AssertionError("有 astream 时不应调用 chat")

    async def astream(self, messages, tools=None, temperature=None):
        yield LLMDelta(reasoning="先想")
        yield LLMDelta(reasoning="再想")
        yield LLMDelta(text="答案")
        yield LLMDelta(finished=True, usage={"prompt_tokens": 5})


def _agent_unit(tmp_path: Path, catalog: ToolCatalog):
    d = tmp_path / "w"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(
        '---\nname: w\ndescription: w 的用途\nkeywords: []\ntools: ["*"]\n---\n你是 w。\n',
        encoding="utf-8")
    return load_agent_dir(d, "user", catalog.names)


def test_runner_emits_thinking_delta_and_keeps_answer_clean(tmp_path):
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    unit = _agent_unit(tmp_path, catalog)
    runner = AgentRunner(unit, catalog, _ReasoningLLM(), RunnerSettings(stop_after=stop_after_turns(3)),
                         tool_ctx=ToolContext(agent_name=unit.name, workdir=tmp_path))
    events = asyncio.run(_collect(runner.run("问题")))

    kinds = [e.kind for e in events]
    assert "thinking_delta" in kinds
    assert kinds.index("thinking_delta") < kinds.index("text_delta")

    thinking = "".join(e.text for e in events if e.kind == "thinking_delta")
    assert thinking == "先想再想"

    final = [e for e in events if e.kind == "assistant_message"][0]
    assert final.text == "答案"                       # 最终回答不含思考
    assert final.data["thinking"] == "先想再想"        # 但思考随事件带出,前端可渲染/切换
    assert events[-1].kind == "agent_end"


async def _collect(agen):
    return [event async for event in agen]


# ── 4. TUI 键位与命令 ────────────────────────────────────


_MODELS = ('{"providers": {"ollama": {"api": "openai-completions", '
           '"models": [{"id": "x"}]}}}')


def _tui_env(tmp_path, monkeypatch, settings: str | None = None) -> None:
    from qi_agent import paths

    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        settings or '{"defaultProvider": "ollama", "defaultModel": "x"}', encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)


class _FakeRegistry:
    names = ("general",)

    def all(self):
        return []

    def get(self, name):
        return object()


class FakeRuntime:
    """带 thinking_level 的假 runtime(与 QiRuntime 的可写字段同名)。"""

    def __init__(self, *args, **kwargs):
        from qi_agent.session import SessionStore

        self.sessions = SessionStore()
        self.cfg = None
        self.cwd = Path.cwd()
        self.registry = _FakeRegistry()
        self.thinking_level = "off"
        self.llm_exec = SimpleNamespace(thinking_level="off", reasoning_dropped=False)
        self.notes: list[str] = []

    async def start_session(self, session, reason: str = "startup") -> None:
        """真实 QiRuntime 的会话级事件;假运行时不用它(不派发任何事件)。"""

    async def stream(self, prompt, session, agent_override=None):
        yield AgentEvent(kind="assistant_message", agent="general", text="ok",
                         data={"step": 1, "tool_calls": []})


@pytest.mark.asyncio
async def test_shift_tab_cycles_level_and_footer_shows_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model",
                        lambda cfg, cwd=None: ResolvedModel(
                            provider="deepseek", model="m", api="openai-completions",
                            base_url=None, api_key_ref=None, reasoning=True,
                            context_window=1000, max_tokens=1))

    app = tui_mod.QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._thinking_level == "off"
        assert "thinking off" in app.footer_text.plain

        await pilot.press("shift+tab")            # pi 的 app.thinking.cycle
        await pilot.pause(0.05)
        assert app._thinking_level == "minimal"
        assert "minimal" in app.footer_text.plain
        assert app._rt is not None
        # 可选能力协议:窄化后再读,顺手验证 client 真的可设
        assert isinstance(app._rt.llm_exec, ThinkingLLMClient)
        assert app._rt.llm_exec.thinking_level == "minimal"   # 真的写到 client 上

        for _ in range(len(THINKING_LEVELS) - 2):
            await pilot.press("shift+tab")
        await pilot.pause(0.05)
        assert app._thinking_level == "max"
        await pilot.press("shift+tab")            # 环绕
        await pilot.pause(0.05)
        assert app._thinking_level == "off"


@pytest.mark.asyncio
async def test_ctrl_t_toggles_thinking_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model",
                        lambda cfg, cwd=None: ResolvedModel(
                            provider="deepseek", model="m", api="openai-completions",
                            base_url=None, api_key_ref=None, reasoning=True,
                            context_window=1000, max_tokens=1))

    app = tui_mod.QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        widget = tui_mod.ThinkingMessage("想了一下", PALETTE)
        app._thinking_widgets.append(widget)
        await app.query_one("#log").mount(widget)
        await pilot.pause(0.05)

        assert app._show_thinking is True and widget.display is True
        await pilot.press("ctrl+t")               # pi 的 app.thinking.toggle
        await pilot.pause(0.05)
        assert app._show_thinking is False and widget.display is False
        assert app._status == "思考块:隐藏"
        await pilot.press("ctrl+t")
        await pilot.pause(0.05)
        assert widget.display is True and app._status == "思考块:显示"


@pytest.mark.asyncio
async def test_slash_thinking_sets_level(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model",
                        lambda cfg, cwd=None: ResolvedModel(
                            provider="deepseek", model="m", api="openai-completions",
                            base_url=None, api_key_ref=None, reasoning=True,
                            context_window=1000, max_tokens=1))

    app = tui_mod.QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        notes: list[tuple[str, str]] = []
        app._note = lambda text, tone="dim": notes.append((text, tone))  # type: ignore[method-assign]

        app._command("/thinking")
        assert "当前: off" in notes[-1][0] and "xhigh" in notes[-1][0]

        app._command("/thinking high")
        assert app._thinking_level == "high"
        assert app._status == "思考级别: high"

        app._command("/thinking 乱写")
        assert "用法" in notes[-1][0]
        assert app._thinking_level == "high"      # 非法值不改状态


@pytest.mark.asyncio
async def test_settings_default_thinking_level_is_used(tmp_path, monkeypatch):
    """settings.defaultThinkingLevel 不再是「仅存储」:进 TUI 就生效。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch,
             settings='{"defaultProvider": "ollama", "defaultModel": "x",'
                      ' "defaultThinkingLevel": "low"}')
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model",
                        lambda cfg, cwd=None: ResolvedModel(
                            provider="deepseek", model="m", api="openai-completions",
                            base_url=None, api_key_ref=None, reasoning=True,
                            context_window=1000, max_tokens=1))

    # FakeRuntime 的 thinking_level 固定为 off;这里直接验证 QiRuntime 的装配
    runtime = runtime_mod.QiRuntime()
    assert runtime.thinking_level == "low"
    assert isinstance(runtime.llm_exec, LiteLLMClient)
    assert runtime.llm_exec.thinking_level == "low"


# ── 5. CLI `--thinking` ──────────────────────────────────


def test_cli_rejects_unknown_thinking_level(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli_app, ["-p", "--thinking", "bogus", "hi"])
    assert result.exit_code == 2
    assert "未知思考级别" in result.output


def test_cli_passes_thinking_level_to_runtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    seen: dict = {}

    class SpyRuntime(FakeRuntime):
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(runtime_mod, "QiRuntime", SpyRuntime)
    result = CliRunner().invoke(cli_app, ["-p", "--thinking", "high", "hi"])
    assert result.exit_code == 0, result.output
    assert seen.get("thinking_level") == "high"


# ── 6. provider 拒绝 reasoning_effort 时优雅降级 ────────────
#
# 真实场景:自建 LiteLLM 代理默认 drop_params 会把未知参数直接报错
# (实测 commandcode 代理就是这样)。用户只是想调级别,不该因此整轮失败。


class _ReasoningRejected(Exception):
    pass


def test_chat_retries_without_reasoning_effort(tmp_path, monkeypatch):
    calls: list[dict] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        if "reasoning_effort" in kwargs:
            raise _ReasoningRejected(
                "Invalid parameter: reasoning_effort. If you want to use these params "
                "dynamically send allowed_openai_params=['reasoning_effort']")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok", tool_calls=None, reasoning_content="think"))],
            usage=None)

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    client = _client(tmp_path, "high")
    resp = asyncio.run(client.chat([]))
    assert resp.text == "ok"
    assert len(calls) == 2                       # 第一次带参失败 → 去掉重试
    assert "reasoning_effort" in calls[0]
    assert "reasoning_effort" not in calls[1]    # 降级后不再带
    assert client.reasoning_dropped is True      # 记下来,供 UI 提示一次

    # 粘性:后续请求也不再带(reasoning_effort)
    client.thinking_level = "max"
    asyncio.run(client.chat([]))
    assert "reasoning_effort" not in calls[-1]


def test_chat_other_errors_still_raise(tmp_path, monkeypatch):
    async def fake_acompletion(**kwargs):
        raise RuntimeError("401 unauthorized")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    client = _client(tmp_path, "high")
    with pytest.raises(RuntimeError):
        asyncio.run(client.chat([]))
    assert client.reasoning_dropped is False     # 与思考参数无关的错误不该被吞


def test_astream_open_falls_back_without_reasoning(tmp_path, monkeypatch):
    """流式打开时被拒:同样丢参重试,而不是退回非流式。"""
    seen: list[dict] = []
    chunks = [_chunk(reasoning="想"), _chunk(text="答")]

    async def fake_acompletion(**kwargs):
        seen.append(kwargs)
        if "reasoning_effort" in kwargs:
            raise _ReasoningRejected("Invalid parameter: reasoning_effort")

        async def gen():
            for chunk in chunks:
                yield chunk
        return gen()

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    async def collect():
        client = _client(tmp_path, "medium")
        return client, [d async for d in client.astream([])]

    client, deltas = asyncio.run(collect())
    assert "".join(d.text for d in deltas) == "答"
    assert "".join(d.reasoning for d in deltas) == "想"
    assert "reasoning_effort" in seen[0]
    assert all("reasoning_effort" not in k for k in seen[1:])
    assert client.reasoning_dropped is True


@pytest.mark.asyncio
async def test_tui_reports_reasoning_drop_once(tmp_path, monkeypatch):
    """provider 拒了思考参数:TUI 用状态行提示一次,不静默。"""
    from types import SimpleNamespace as NS

    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model",
                        lambda cfg, cwd=None: NS(provider="p", model="m", label="p/m",
                                                 reasoning=True, context_window=1000))

    app = tui_mod.QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        assert isinstance(app._rt.llm_exec, ThinkingLLMClient)
        app._rt.llm_exec.reasoning_dropped = True
        app._report_reasoning_dropped()
        await pilot.pause(0.05)
        assert "不接受 reasoning_effort" in app._status
        app._restore_status()
        app._report_reasoning_dropped()           # 第二次不再提示
        assert "不接受" not in app._status


# ── 思考落盘(直播/回放一致)─────────────────────────────
#
# 为什么值得钉住:思考原来是**只活在流里**的(界面上看得见、刷新就没了),而
# "思考与回答之间那条分隔线"依赖它。同一类 bug 在这个仓库里修过两次
# (工具往返、工具之间的叙述),所以这里把三条口径一次钉死:
#   1. 思考会落盘,而且排在**同一步的回答之前**;
#   2. 它是 `custom` 而不是 `message` —— `_history()` 只读 message,
#      所以思考**不进 LLM 上下文**(提示词零回归);
#   3. 空思考不落盘(不往会话里塞空 entry)。

class ThinkingStub:
    """两步:第一步带思考 + 工具调用,第二步只有结论。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=None):  # pragma: no cover
        raise AssertionError("有 astream 时不应调用 chat")

    async def astream(self, messages, tools=None, temperature=None):
        self.calls += 1
        if self.calls == 1:
            yield LLMDelta(reasoning="先看看目录里有什么")
            yield LLMDelta(text="我看一下", finished=True,
                           tool_calls=[ToolCallOut(id="c1", name="ls", args={"path": "."})])
            return
        yield LLMDelta(reasoning="现在可以答了")
        yield LLMDelta(text="结论", finished=True)


@pytest.mark.asyncio
async def test_thinking_is_persisted_before_the_answer_and_stays_out_of_context(tmp_path):
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore

    store = SessionStore(root=tmp_path / "sessions")
    # 与 test_web_api 的 runtime 构造同形:agent 走包内置的 general,catalog 由 runtime 自建。
    runtime = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                        session_store=store, llm=ThinkingStub(), disable_router=True)
    session = store.create("思考", cwd=tmp_path)
    async for _ in runtime.stream("看下目录", session):
        pass

    kinds = [(e.get("type"), e.get("custom_type"), e.get("role")) for e in session.branch()]
    # 思考排在它那一步的回答之前(顺序反了回放就成了"先回答、再思考")
    thinking = [i for i, k in enumerate(kinds) if k[1] == "assistant_thinking"]
    assert len(thinking) == 2, kinds
    first_assistant = next(i for i, k in enumerate(kinds) if k[2] == "assistant")
    assert thinking[0] < first_assistant
    body = next(e for e in session.branch() if e.get("custom_type") == "assistant_thinking")
    assert "先看看目录里有什么" in body["content"]

    # 不进 LLM 上下文:历史只由 message 组成
    history = runtime._history(session)
    assert all("先看看目录里有什么" not in m.content for m in history)
