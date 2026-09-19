"""P-E3c-2:`sendMessage` / `sendUserMessage` 与三档送达时机(`deliverAs`)。

pi 的三档语义(照搬,含义见 `ExtensionApi.sendMessage` 的 docstring):

* `steer` —— 本轮的**下一次 LLM 调用**之前
* `follow_up` —— agent **本该收工**时(有排队消息就不收工,继续跑一轮)
* `next_turn` —— 留到**下一次用户输入**

这一块最容易出的错是“送达了但**重复**送达”与“落盘了但还没送达”(历史与行为不一致),
所以三条都既断言行为、也断言落盘的那一条 entry。

> 测试源码的拼装用 `_tool_ext()` **函数**而不是字符串模板。写这个文件时连续踩了两个
> 同源坑:① 两段各自带 `register(api)` 的源码拼起来,后者覆盖前者(工具根本没注册);
> ② handlers 落在 `register` **外面**,它引用的 `api` 就是未定义。两种都是“源码拼装”
> 这件事本身容易错 —— 用一个函数把结构和缩进固定下来,比每次盯着模板写靠谱。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.llm import ChatResponse, ToolCallOut  # noqa: E402
from qi_agent.registry import EXTENSION_ENTRY_FILE  # noqa: E402

_MODELS = ('{"providers": {"ollama": {"api": "openai-completions", '
           '"models": [{"id": "x"}]}}}')
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)


def _install(tmp_path: Path, name: str, body: str) -> None:
    d = tmp_path / "proj" / ".qi" / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(body, encoding="utf-8")


def _tool_ext(log: Path, handlers: str = "") -> str:
    """注册 `echo` 工具 + 把 `handlers`(未缩进的事件订阅代码)**缩进进同一个 register**。"""
    body = [
        "import json",
        "from pathlib import Path",
        "",
        "from qi_agent.extensions import Tool",
        "",
        f"LOG = Path({str(log)!r})",
        "",
        "",
        "async def echo(args, ctx):",
        "    LOG.write_text(json.dumps(args, ensure_ascii=False), encoding='utf-8')",
        "    return 'echo 收到'",
        "",
        "",
        "def register(api):",
        "    api.registerTool(Tool('echo', '回声',",
        "                          {'type': 'object',",
        "                           'properties': {'text': {'type': 'string'}}}, echo))",
    ]
    for line in handlers.strip("\n").splitlines():
        body.append("    " + line if line.strip() else line)
    return "\n".join(body) + "\n"


def _call(name: str, args: dict) -> ChatResponse:
    return ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name=name, args=args)])


class _ScriptedLLM:
    def __init__(self, script: list[ChatResponse]) -> None:
        self.script = list(script)
        self.calls: list[list] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append(list(messages))
        return self.script.pop(0) if self.script else ChatResponse(text="完")

    def contents(self, index: int) -> list[str]:
        return [m.content for m in self.calls[index]]


def _runtime(tmp_path, monkeypatch, llm, **kw):
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch)
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=project, approve_project=True, llm=llm, **kw)


async def _drain(runtime, text: str, session) -> list:
    return [e async for e in runtime.stream(text, session)]


def _entries(session) -> list[dict]:
    if not session.path.exists():
        return []
    return [json.loads(line) for line in
            session.path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _injected(session) -> list[dict]:
    return [e for e in _entries(session) if e.get("injected_by")]


def _install_tool_ext(tmp_path: Path, name: str, handlers: str) -> None:
    _install(tmp_path, name, _tool_ext(tmp_path / "echo.json", handlers))


# ── steer(默认)────────────────────────────────────────

@pytest.mark.asyncio
async def test_steer_reaches_the_next_llm_call_in_the_same_turn(tmp_path, monkeypatch):
    llm = _ScriptedLLM([_call("echo", {"text": "x"}), ChatResponse(text="好了")])
    _install_tool_ext(tmp_path, "steer", """
def on_result(payload, ctx):
    api.sendMessage("注意:工具结果里有敏感字段。")

api.on("tool_result", on_result)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "跑一下", session)

    # 第一次调用发生在 tool_result 之前 → 不该看到;第二次该看到,而且**只一次**
    assert "注意:工具结果里有敏感字段。" not in llm.contents(0)
    assert llm.contents(1).count("注意:工具结果里有敏感字段。") == 1
    entries = _injected(session)
    assert len(entries) == 1
    assert entries[0]["deliver_as"] == "steer"
    assert entries[0]["injected_by"] == "sendMessage"
    assert entries[0]["source"] == "steer"
    assert entries[0]["role"] == "user"


# ── follow_up ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_follow_up_keeps_the_agent_working(tmp_path, monkeypatch):
    """`follow_up` 的语义是“别收工”:模型本来已经答完(没有工具调用),
    队列里有消息 → 再跑一轮。"""
    llm = _ScriptedLLM([ChatResponse(text="第一轮答完"), ChatResponse(text="第二轮的结论")])
    _install_tool_ext(tmp_path, "follow", """
def on_start(payload, ctx):
    api.sendMessage("顺手把测试也跑一遍。", deliver_as="follow_up")

api.on("agent_start", on_start)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "做点事", session)

    assert len(llm.calls) == 2                       # 因为 follow_up 多跑了一轮
    assert "顺手把测试也跑一遍。" in llm.contents(1)
    assert [e for e in events if e.kind == "text"][-1].text == "第二轮的结论"
    assert _injected(session)[0]["deliver_as"] == "follow_up"


@pytest.mark.asyncio
async def test_follow_up_respects_the_embedder_turn_cap(tmp_path, monkeypatch):
    """有排队消息但已到调用方的轮次上限 → **上限优先**(与有工具调用时同一条政策)。"""
    from qi_agent.runner import stop_after_turns

    llm = _ScriptedLLM([ChatResponse(text="一"), ChatResponse(text="二")])
    _install_tool_ext(tmp_path, "always", """
def on_turn(payload, ctx):
    # 用 `turn_end`(每轮都 fire)而不是 `agent_start`(一轮仅一次):
    # 只有这样队列才会一直非空,才真能验到“上限优先”。
    api.sendMessage("永远再来一次", deliver_as="follow_up")

api.on("turn_end", on_turn)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm, stop_after=stop_after_turns(2))
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "跑", session)

    assert any(e.kind == "error" and "轮次上限" in (e.text or "") for e in events)
    assert len(llm.calls) == 2                       # 停在上限,没跑飞


# ── next_turn ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_next_turn_waits_for_the_next_user_input(tmp_path, monkeypatch):
    llm = _ScriptedLLM([ChatResponse(text="一"), ChatResponse(text="二")])
    _install_tool_ext(tmp_path, "later", """
def on_start(payload, ctx):
    if payload["prompt"] == "第一句":
        api.sendMessage("上一轮留下的一句话", deliver_as="next_turn")

api.on("before_agent_start", on_start)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    await _drain(runtime, "第一句", session)
    assert "上一轮留下的一句话" not in llm.contents(0)    # 本轮不打扰
    assert _injected(session) == []                     # 也还没落盘(与送达同时)

    await _drain(runtime, "第二句", session)
    assert "上一轮留下的一句话" in llm.contents(1)        # 下一次输入时才送达
    assert _injected(session)[0]["deliver_as"] == "next_turn"


# ── sendUserMessage 与错误路径 ──────────────────────────

@pytest.mark.asyncio
async def test_send_user_message_is_marked_as_such(tmp_path, monkeypatch):
    llm = _ScriptedLLM([_call("echo", {"text": "x"}), ChatResponse(text="好了")])
    _install_tool_ext(tmp_path, "user-msg", """
def on_result(payload, ctx):
    api.sendUserMessage("用户其实想说的是这个。")

api.on("tool_result", on_result)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "跑一下", session)

    assert "用户其实想说的是这个。" in llm.contents(1)
    assert _injected(session)[0]["injected_by"] == "sendUserMessage"


@pytest.mark.asyncio
async def test_bad_deliver_as_is_visible_not_silent(tmp_path, monkeypatch):
    """非法档位:handler 会抛 → 总线记下来进 notes(不静默当成 steer)。"""
    llm = _ScriptedLLM([ChatResponse(text="好")])
    _install_tool_ext(tmp_path, "bad", """
def on_start(payload, ctx):
    api.sendMessage("x", deliver_as="urm")

api.on("before_agent_start", on_start)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "跑", session)

    assert any("deliver_as" in n for n in runtime.notes)
    assert _injected(session) == []                     # 没被入队


@pytest.mark.asyncio
async def test_blank_message_is_not_queued(tmp_path, monkeypatch):
    llm = _ScriptedLLM([ChatResponse(text="好")])
    _install_tool_ext(tmp_path, "blank", """
def on_start(payload, ctx):
    api.sendMessage("   ")

api.on("before_agent_start", on_start)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "跑", session)
    assert _injected(session) == []
    assert len(llm.calls) == 1
