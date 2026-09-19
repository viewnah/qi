"""P-E2c:`input` 与 `before_agent_start` 两个派发点。

两件事分别值得单独测:

* **`input`** 是用户输入的唯一咽喉。`handled` 的语义是“**这一轮不跑 agent**” ——
  如果它退化成“跑但忽略返回值”,会表现为“扩展说它处理了,但模型还是答了”;
  而如果 `handled` 且没给文本时悄无声息,又表现为“程序卡住了”。两边都要钉。
* **`before_agent_start`** 的返回是**链式**的,而且必须真的到达 runner。
  改完 prompt 却因为 runner 自己又建了一次而失效,是最容易写出来也最难发现的一类 bug
  (见 runner 里 `system_prompt` 非空就不重建那一行)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.llm import ChatResponse  # noqa: E402
from qi_agent.registry import EXTENSION_ENTRY_FILE  # noqa: E402

_MINIMAL_MODELS = json.dumps({
    "providers": {"ollama": {"api": "openai-completions",
                             "baseUrl": "http://127.0.0.1:11434/v1",
                             "models": [{"id": "x"}]}},
})


def _env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "models.json").write_text(_MINIMAL_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x"}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)


def _install(tmp_path: Path, name: str, body: str) -> None:
    d = tmp_path / "proj" / ".qi" / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(body, encoding="utf-8")


class _RecordingLLM:
    """记下每次 `chat()` 收到的完整消息表(第一条是 system)。"""

    def __init__(self, reply: str = "好") -> None:
        self.calls: list[list] = []
        self.reply = reply

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append(list(messages))
        return ChatResponse(text=self.reply, tool_calls=[])

    @property
    def system(self) -> str:
        return self.calls[-1][0].content

    @property
    def user(self) -> str:
        return self.calls[-1][-1].content


def _runtime(tmp_path, monkeypatch, llm):
    (tmp_path / "proj" / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch)
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=tmp_path / "proj",
                     approve_project=True, llm=llm)


async def _drain(runtime, text: str, session) -> list:
    return [e async for e in runtime.stream(text, session)]


def _entries(session) -> list[dict]:
    if not session.path.exists():
        return []
    return [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _messages(session) -> list[dict]:
    return [e for e in _entries(session) if e.get("type") == "message"]


# ── input:transform ─────────────────────────────────────

@pytest.mark.asyncio
async def test_input_transform_rewrites_what_the_model_sees(tmp_path, monkeypatch):
    """改写后的文本要影响**下游全部**:模型看到的、落盘的、以及后续判定。"""
    llm = _RecordingLLM()
    _install(tmp_path, "shout", """
def register(api):
    def on_input(payload, ctx):
        if payload["text"].startswith("?"):
            return {"action": "transform", "text": "简短回答:" + payload["text"][1:]}
    api.on("input", on_input)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "?这是啥", session)

    assert llm.user == "简短回答:这是啥"
    user_entries = [m for m in _messages(session) if m.get("role") == "user"]
    assert user_entries[-1]["content"] == "简短回答:这是啥"


@pytest.mark.asyncio
async def test_input_transform_chains_across_extensions(tmp_path, monkeypatch):
    llm = _RecordingLLM()
    _install(tmp_path, "a-first", """
def register(api):
    api.on("input", lambda p, c: {"action": "transform", "text": p["text"] + "1"})
""")
    _install(tmp_path, "b-second", """
def register(api):
    api.on("input", lambda p, c: {"action": "transform", "text": p["text"] + "2"})
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "q", session)
    assert llm.user == "q12"          # 后者看到前者改过的值(注册顺序 = 装载顺序)


@pytest.mark.asyncio
async def test_input_payload_carries_source(tmp_path, monkeypatch):
    llm = _RecordingLLM()
    seen: list[str] = []
    _install(tmp_path, "spy", f"""
import json
from pathlib import Path

LOG = Path({str(tmp_path / "seen.json")!r})

def register(api):
    def on_input(payload, ctx):
        LOG.write_text(json.dumps({{"source": payload["source"],
                                    "session": payload["session"]}}), encoding="utf-8")
    api.on("input", on_input)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "x", session)
    del seen

    seen_payload = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert seen_payload["source"] == "interactive"      # stream() 的默认值
    assert seen_payload["session"] == session.id


# ── input:handled ───────────────────────────────────────

@pytest.mark.asyncio
async def test_input_handled_skips_the_agent_entirely(tmp_path, monkeypatch):
    llm = _RecordingLLM()
    _install(tmp_path, "pong", """
def register(api):
    def on_input(payload, ctx):
        if payload["text"] == "ping":
            return {"action": "handled", "reply": "pong"}
    api.on("input", on_input)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "ping", session)

    assert llm.calls == []                              # **真的没跑 agent**
    assert [e.text for e in events if e.kind == "text"] == ["pong"]
    assert events[-1].kind == "agent_end"
    assert events[-1].data["handled_by"] == "pong"       # 谁接管的,可追溯
    assert [m["content"] for m in _messages(session) if m["role"] == "assistant"] == ["pong"]
    assert [m["content"] for m in _messages(session) if m["role"] == "user"] == ["ping"]


@pytest.mark.asyncio
async def test_input_handled_without_text_still_says_something(tmp_path, monkeypatch):
    """不给文本也必须**看得见** —— 静默吞掉会被当成“程序卡了”。"""
    llm = _RecordingLLM()
    _install(tmp_path, "silent", """
def register(api):
    api.on("input", lambda p, c: {"action": "handled"} if p["text"] == "/noop" else None)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "/noop", session)

    from qi_agent.runtime import INPUT_HANDLED_PLACEHOLDER

    assert llm.calls == []
    assert [e.text for e in events if e.kind == "text"] == [INPUT_HANDLED_PLACEHOLDER]
    # 回放里也在(否则刷新之后这一轮凭空消失)
    assert INPUT_HANDLED_PLACEHOLDER in [
        m["content"] for m in _messages(session) if m["role"] == "assistant"]


@pytest.mark.asyncio
async def test_input_first_handler_wins(tmp_path, monkeypatch):
    llm = _RecordingLLM()
    _install(tmp_path, "first", """
def register(api):
    api.on("input", lambda p, c: {"action": "handled", "reply": "第一个"})
""")
    _install(tmp_path, "second", """
def register(api):
    api.on("input", lambda p, c: {"action": "handled", "reply": "第二个"})
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "x", session)
    assert [e.text for e in events if e.kind == "text"] == ["第一个"]


@pytest.mark.asyncio
async def test_input_handler_crash_does_not_kill_the_turn(tmp_path, monkeypatch):
    """一个扩展的 input 崩了 → 记进 notes,这一轮照跑(拿原始文本)。"""
    llm = _RecordingLLM()
    _install(tmp_path, "boom", """
def register(api):
    api.on("input", lambda p, c: (_ for _ in ()).throw(RuntimeError("我坏了")))
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "原始", session)

    assert llm.user == "原始"                            # 下游不受影响
    assert any("boom" in n and "input" in n for n in runtime.notes)


# ── before_agent_start ──────────────────────────────────

@pytest.mark.asyncio
async def test_before_agent_start_can_replace_the_system_prompt(tmp_path, monkeypatch):
    """扩展改过的 prompt 必须**真的到达 runner**。"""
    llm = _RecordingLLM()
    _install(tmp_path, "injector", """
def register(api):
    def on_start(payload, ctx):
        return {"system_prompt": payload["system_prompt"] + "\\n\\n【扩展注入的角色说明】"}
    api.on("before_agent_start", on_start)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "你好", session)

    assert "【扩展注入的角色说明】" in llm.system
    assert "qi" in llm.system                              # 原 prompt 仍在(是追加不是替换)


@pytest.mark.asyncio
async def test_before_agent_start_chains(tmp_path, monkeypatch):
    """链式:第二个 handler 看到第一个改过的 `system_prompt`。"""
    llm = _RecordingLLM()
    _install(tmp_path, "one", """
def register(api):
    api.on("before_agent_start",
           lambda p, c: {"system_prompt": p["system_prompt"] + "\\n[1]"})
""")
    _install(tmp_path, "two", """
def register(api):
    api.on("before_agent_start",
           lambda p, c: {"system_prompt": p["system_prompt"] + "\\n[2]"})
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "x", session)

    assert llm.system.endswith("[1]\n[2]")


@pytest.mark.asyncio
async def test_before_agent_start_payload_has_prompt_and_agent(tmp_path, monkeypatch):
    llm = _RecordingLLM()
    _install(tmp_path, "spy", f"""
import json
from pathlib import Path

LOG = Path({str(tmp_path / "payload.json")!r})

def register(api):
    def on_start(payload, ctx):
        LOG.write_text(json.dumps({{"prompt": payload["prompt"],
                                    "agent": payload["agent"],
                                    "has_prompt": bool(payload["system_prompt"])}}),
                       encoding="utf-8")
    api.on("before_agent_start", on_start)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "问点什么", session)

    payload = json.loads((tmp_path / "payload.json").read_text(encoding="utf-8"))
    assert payload["prompt"] == "问点什么"
    assert payload["agent"] == "qi"                   # 内置兜底角色
    assert payload["has_prompt"] is True                   # 已经建好了才交给扩展


@pytest.mark.asyncio
async def test_before_agent_start_crash_keeps_the_built_prompt(tmp_path, monkeypatch):
    """handler 崩了 → 用**已经建好的** prompt 继续,不把这一轮搞死。"""
    llm = _RecordingLLM()
    _install(tmp_path, "boom", """
def register(api):
    api.on("before_agent_start", lambda p, c: (_ for _ in ()).throw(RuntimeError("炸")))
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "x", session)

    assert llm.system.startswith("你是运行在 qi 框架中的 AI 助手")
    assert any("boom" in n and "before_agent_start" in n for n in runtime.notes)


@pytest.mark.asyncio
async def test_no_extension_means_no_hook_work(tmp_path, monkeypatch):
    """零扩展时两个事件的派发点都不走 —— 行为与从前一致(prompt 由 runner 自己建)。"""
    llm = _RecordingLLM()
    runtime = _runtime(tmp_path, monkeypatch, llm)
    assert runtime.extensions == []
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "没有扩展", session)

    assert llm.user == "没有扩展"
    assert llm.system.startswith("你是运行在 qi 框架中的 AI 助手")
