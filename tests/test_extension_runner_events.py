"""P-E2c-2:runner 侧的事件 —— `turn_start`/`turn_end`/`context`/`tool_call`/`tool_result`。

这一组与前一组(input / before_agent_start)不同的地方在于:它们夹在**工具循环**里,
所以“一次回合多次触发”的语义必须钉住 —— `turn_start` 少发一次,扩展的计时/审计就
永远差一条;`tool_result` 的 patch 没生效,则表现为“扩展改了结果但模型还是看到旧的”。

工具类事件还自带一条**权责**边界:`tool_call` 的 block 必须是“工具**根本没执行**”,
而不是“执行了但结果被丢掉”。两者在日志上看起来一样,在副作用上完全相反。

> 测试源码里的一个坑(踩过一次):工具与 handler 必须在**同一个 `register`** 里。
> 把两段各自带 `register(api)` 的源码拼起来,后一个定义会覆盖前一个 —— 于是工具根本
> 没注册,而“工具没被执行”这类断言会**莫名其妙地通过**。所以下面用 `_echo_extension()`
> 统一拼装,并在需要的地方显式断言工具确实在 catalog 里。
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


def _indent(code: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in code.splitlines())


def _echo_extension(log: Path, handlers: str = "") -> str:
    """一个注册了 `echo` 工具的扩展;`handlers` 是事件订阅代码(会缩进进 `register`)。

    工具会把收到的参数写进 `log` —— “参数被改写了吗/工具到底跑了吗”都靠它判断。
    """
    return f'''
import json
from pathlib import Path

from qi_agent.extensions import Tool

LOG = Path({str(log)!r})


async def echo(args, ctx):
    LOG.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")
    return "echo 收到:" + json.dumps(args, ensure_ascii=False)


def register(api):
    api.registerTool(Tool("echo", "回声", {{
        "type": "object", "properties": {{"text": {{"type": "string"}}}}}}, echo))
{_indent(handlers)}
'''


class _ScriptedLLM:
    """按脚本依次返回;记录每次 `chat()` 收到的消息表。"""

    def __init__(self, script: list[ChatResponse]) -> None:
        self.script = list(script)
        self.calls: list[list] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append(list(messages))
        return self.script.pop(0) if self.script else ChatResponse(text="完")

    def messages_of(self, index: int) -> list:
        return self.calls[index]

    def tool_messages(self, index: int) -> list[str]:
        return [m.content for m in self.calls[index] if m.role == "tool"]


def _call(name: str, args: dict, call_id: str = "c1") -> ChatResponse:
    return ChatResponse(text="", tool_calls=[ToolCallOut(id=call_id, name=name, args=args)])


def _runtime(tmp_path, monkeypatch, llm):
    (tmp_path / "proj" / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch)
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=tmp_path / "proj", disable_router=True,
                     approve_project=True, llm=llm)


async def _drain(runtime, text: str, session) -> list:
    return [e async for e in runtime.stream(text, session)]


# ── turn_start / turn_end ───────────────────────────────

@pytest.mark.asyncio
async def test_turn_events_fire_per_turn(tmp_path, monkeypatch):
    """一次工具往返 = 两轮 → `turn_start` / `turn_end` 各两次,index 从 1 开始。"""
    llm = _ScriptedLLM([_call("echo", {"text": "hi"}), ChatResponse(text="好了")])
    turns = tmp_path / "turns.jsonl"
    _install(tmp_path, "turns", _echo_extension(tmp_path / "echo.json", f"""
def note(kind):
    def handler(payload, ctx):
        with Path({str(turns)!r}).open("a", encoding="utf-8") as f:
            f.write(json.dumps({{"kind": kind, "turn": payload["turn_index"]}}) + "\\n")
    return handler

api.on("turn_start", note("start"))
api.on("turn_end", note("end"))
"""))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    assert runtime.catalog.get("echo") is not None      # 工具真的注册上了
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "跑一下", session)

    rows = [json.loads(x) for x in turns.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"kind": "start", "turn": 1}, {"kind": "end", "turn": 1},
                    {"kind": "start", "turn": 2}, {"kind": "end", "turn": 2}]


# ── context ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_context_can_prune_what_the_model_sees(tmp_path, monkeypatch):
    """`context` 换掉的列表就是**这一轮真正发出去**的那一份。"""
    llm = _ScriptedLLM([_call("echo", {"text": "hi"}), ChatResponse(text="好了")])
    _install(tmp_path, "pruner", _echo_extension(tmp_path / "echo.json", """
def prune(payload, ctx):
    # 只留 system + 最后一条(演示裁剪:真实场景按体积/敏感度裁)
    msgs = payload["messages"]
    return {"messages": [msgs[0], msgs[-1]]}

api.on("context", prune)
"""))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    assert runtime.catalog.get("echo") is not None
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "跑一下", session)

    # 第一次调用:system + user(裁剪后仍是 2 条)
    assert len(llm.messages_of(0)) == 2
    # 第二次调用:中间那条 assistant(tool_calls) 被裁掉,只剩 system + tool 结果
    assert len(llm.messages_of(1)) == 2
    assert llm.messages_of(1)[-1].role == "tool"


@pytest.mark.asyncio
async def test_context_handler_crash_keeps_the_original_messages(tmp_path, monkeypatch):
    llm = _ScriptedLLM([ChatResponse(text="好")])
    _install(tmp_path, "boom", _echo_extension(tmp_path / "echo.json", """
api.on("context", lambda p, c: (_ for _ in ()).throw(RuntimeError("裁不动")))
"""))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "x", session)

    assert [m.role for m in llm.messages_of(0)] == ["system", "user"]
    assert any("boom" in n and "context" in n for n in runtime.notes)


# ── tool_call ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_call_block_prevents_execution(tmp_path, monkeypatch):
    """block 必须是“**根本没执行**” —— 不是“执行了但结果被丢掉”。

    两者在日志里长得一样,在副作用上完全相反(一个删了文件,一个没删)。
    所以这里先确认工具**在 catalog 里**(否则“没执行”是废话),再断言它没被调用。
    """
    llm = _ScriptedLLM([_call("echo", {"text": "hi"}), ChatResponse(text="被拦了")])
    echo_log = tmp_path / "echo.json"
    _install(tmp_path, "gate", _echo_extension(echo_log, """
def gate(payload, ctx):
    if payload["tool_name"] == "echo":
        return {"block": True, "reason": "测试里不许用 echo"}

api.on("tool_call", gate)
"""))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    assert runtime.catalog.get("echo") is not None      # 工具是能跑的,只是被拦了
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "跑一下", session)

    assert not echo_log.exists()                        # 真的没执行
    ends = [e for e in events if e.kind == "tool_end"]
    assert ends[0].data["status"] == "error"
    assert ends[0].data["error"] == "denied"
    assert "被扩展拦截" in ends[0].text
    # 模型看到“被拦了 + 原因”,才能换个思路
    assert "测试里不许用 echo" in llm.tool_messages(1)[0]


@pytest.mark.asyncio
async def test_tool_call_without_gate_runs_the_tool(tmp_path, monkeypatch):
    """对照组:同一份扩展去掉 gate,工具就真的跑了 —— 证明上一条不是“工具不存在”。"""
    llm = _ScriptedLLM([_call("echo", {"text": "hi"}), ChatResponse(text="好了")])
    echo_log = tmp_path / "echo.json"
    _install(tmp_path, "no-gate", _echo_extension(echo_log))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "跑一下", session)

    assert json.loads(echo_log.read_text(encoding="utf-8")) == {"text": "hi"}
    assert llm.tool_messages(1)[0].startswith("echo 收到:")


@pytest.mark.asyncio
async def test_tool_call_can_rewrite_arguments_and_the_event_shows_the_final_ones(
        tmp_path, monkeypatch):
    """改 `input` 真生效;而且 `tool_start` 事件里的 args 就是**真正执行**的那一份。"""
    llm = _ScriptedLLM([_call("echo", {"text": "原"}), ChatResponse(text="好了")])
    echo_log = tmp_path / "echo.json"
    _install(tmp_path, "rewriter", _echo_extension(echo_log, """
api.on("tool_call", lambda p, c: {"input": {**p["input"], "text": "改过的"}})
"""))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "跑一下", session)

    assert json.loads(echo_log.read_text(encoding="utf-8")) == {"text": "改过的"}
    starts = [e for e in events if e.kind == "tool_start"]
    assert starts[0].data["args"] == {"text": "改过的"}     # 事件里不是原始参数


@pytest.mark.asyncio
async def test_tool_call_handler_crash_fails_safe_by_blocking(tmp_path, monkeypatch):
    """闸门自己崩了 → **拦住**。放行等于“装了闸门反而更不安全”。"""
    llm = _ScriptedLLM([_call("echo", {"text": "hi"}), ChatResponse(text="被拦了")])
    echo_log = tmp_path / "echo.json"
    _install(tmp_path, "boom-gate", _echo_extension(echo_log, """
api.on("tool_call", lambda p, c: (_ for _ in ()).throw(RuntimeError("闸门崩了")))
"""))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    assert runtime.catalog.get("echo") is not None
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "跑一下", session)

    assert not echo_log.exists()
    assert [e for e in events if e.kind == "tool_end"][0].data["error"] == "denied"
    assert any("boom-gate" in n and "tool_call" in n for n in runtime.notes)


# ── tool_result ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_result_patch_changes_what_the_model_sees(tmp_path, monkeypatch):
    llm = _ScriptedLLM([_call("echo", {"text": "hi"}), ChatResponse(text="好了")])
    _install(tmp_path, "editor", _echo_extension(tmp_path / "echo.json", """
def patch(payload, ctx):
    return {"result": payload["result"] + " [已脱敏]",
            "details": {"redacted": True},
            "status": payload["status"]}

api.on("tool_result", patch)
"""))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    assert runtime.catalog.get("echo") is not None
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "跑一下", session)

    assert llm.tool_messages(1)[0].endswith("[已脱敏]")     # 模型看到的是改过的
    end = [e for e in events if e.kind == "tool_end"][0]
    assert end.text.endswith("[已脱敏]")                     # 界面看到的也是
    assert end.data["details"] == {"redacted": True}


@pytest.mark.asyncio
async def test_tool_result_untouched_when_nobody_subscribes(tmp_path, monkeypatch):
    """只订阅别的事件时,结果一字不改 —— 半扩展也不能有副作用。"""
    llm = _ScriptedLLM([_call("echo", {"text": "hi"}), ChatResponse(text="好了")])
    _install(tmp_path, "unrelated", _echo_extension(tmp_path / "echo.json", """
api.on("agent_start", lambda p, c: None)
"""))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    assert runtime.catalog.get("echo") is not None
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "跑一下", session)
    assert llm.tool_messages(1)[0].startswith("echo 收到:")


# ── agent_start / agent_end ─────────────────────────────

@pytest.mark.asyncio
async def test_agent_events_fire_once_per_run(tmp_path, monkeypatch):
    llm = _ScriptedLLM([ChatResponse(text="好")])
    log = tmp_path / "agent.jsonl"
    _install(tmp_path, "spy", _echo_extension(tmp_path / "echo.json", f"""
def note(kind):
    def handler(payload, ctx):
        with Path({str(log)!r}).open("a", encoding="utf-8") as f:
            f.write(json.dumps({{"kind": kind, "agent": payload["agent"],
                                 "turns": payload.get("turns")}}) + "\\n")
    return handler

api.on("agent_start", note("start"))
api.on("agent_end", note("end"))
"""))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "一次就完", session)

    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert rows[0] == {"kind": "start", "agent": "general", "turns": None}
    assert rows[1] == {"kind": "end", "agent": "general", "turns": 1}
