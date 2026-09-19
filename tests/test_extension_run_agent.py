"""P-E4a:`ctx.runAgent` —— 在宿主内起一个**受管的子运行**(E12)。

这是 qi-agents(以及任何“把任务交给另一个角色”的需求)的基石。几件必须钉住的事:

* **不碰会话**:子运行不落盘、不分派、不改 active_agent —— 它是“借一次工具循环”,
  不是“再来一轮对话”。否则父会话的历史里会多出几条看不懂的消息。
* **提示词与工具是真的隔离的**:子运行拿到的是它自己那份(不是父的)。
* **`tools` 缺省继承父的当前集合**,而不是 catalog 全部 —— 给子运行比父更多权限是**提权**
  (这正是 §11.7 记下的那个风险,在这里用保守默认绕开)。
* **扩展事件照常派发**:所以权限闸门对子运行一样生效 —— 这是“进程内”相对“子进程”的
  一个真实好处(子进程里宿主根本看不见子 agent 的工具调用)。
* **换模型不改变父的模型**:`spec.model` 是另建一个客户端,不碰 `llm_exec`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.llm import ChatResponse, LiteLLMClient, ToolCallOut  # noqa: E402
from qi_agent.registry import EXTENSION_ENTRY_FILE  # noqa: E402

_MODELS = json.dumps({
    "providers": {
        "ollama": {"api": "openai-completions", "models": [{"id": "x"}]},
        "beta": {"api": "openai-completions", "models": [{"id": "m3"}]},
    },
})
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


def _call(name: str, args: dict) -> ChatResponse:
    return ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name=name, args=args)])


class _ScriptedLLM:
    """按脚本依次回答;记下每次收到的消息表 **与工具 schema**(后者用来验隔离)。"""

    def __init__(self, script: list[ChatResponse]) -> None:
        self.script = list(script)
        self.calls: list[list] = []
        self.tools: list[list[str]] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append(list(messages))
        self.tools.append(sorted(t["function"]["name"] for t in (tools or [])))
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


#: 一个 `delegate` 工具:它调 `api.runAgent` 起子运行,并把子运行的结果还给父
_DELEGATE_EXT = """
from qi_agent.extensions import Tool

SUB_PROMPT = "你是子代理。只做一件事。"


def register(api):
    async def delegate(args, ctx):
        return await api.runAgent(
            {"system_prompt": SUB_PROMPT, "tools": ["read"], "name": "scout"},
            "查一下")

    api.registerTool(Tool("delegate", "委派", {"type": "object", "properties": {}},
                          delegate))
"""


# ── 基本:隔离 + 结果回传 ───────────────────────────────

@pytest.mark.asyncio
async def test_run_agent_is_isolated_and_returns_the_result(tmp_path, monkeypatch):
    llm = _ScriptedLLM([_call("delegate", {}), ChatResponse(text="子运行结果"),
                        ChatResponse(text="父的最终回答")])
    _install(tmp_path, "delegator", _DELEGATE_EXT)
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "帮我委派", session)

    # 三次调用:父第 1 轮(调工具)→ 子运行 → 父第 2 轮
    assert len(llm.calls) == 3
    # 子运行用的是**它自己的**提示词(不是父的)
    assert llm.contents(1)[0] == "你是子代理。只做一件事。"
    # 子运行的工具是 spec 里给的那份,不是父的全部
    assert llm.tools[1] == ["read"]
    assert "bash" in llm.tools[0]                     # 父那边照旧有 bash
    # 子结果回到了父(作为工具结果)
    tool_result = [m.content for m in llm.calls[2] if m.role == "tool"]
    assert tool_result and "子运行结果" in tool_result[0]
    assert [e for e in events if e.kind == "text"][-1].text == "父的最终回答"

    # **会话没被子运行污染**:只有父自己的 entry(user / tool / assistant)
    assert not any("子运行结果" in str(e.get("content", "")) for e in _entries(session)
                   if e.get("type") == "message")


@pytest.mark.asyncio
async def test_run_agent_inherits_the_parent_tool_set_by_default(tmp_path, monkeypatch):
    """`tools` 缺省 = **继承父的当前集合**(不是 catalog 全部)。

    给子运行比父更多权限是**提权** —— 这正是 §11.7 记下的那个风险,用保守默认绕开。
    """
    llm = _ScriptedLLM([_call("delegate", {}), ChatResponse(text="子"), ChatResponse(text="父")])
    _install(tmp_path, "delegator", _DELEGATE_EXT.replace(
        '{"system_prompt": SUB_PROMPT, "tools": ["read"], "name": "scout"}',
        '{"system_prompt": SUB_PROMPT, "name": "scout"}'))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    runtime.set_tool_names(["read"])                  # 父被收窄到只读
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "帮我委派", session)

    assert llm.tools[0] == ["read"]                   # 父:只读
    assert llm.tools[1] == ["read"]                   # 子:同样只读(继承,没放大)


@pytest.mark.asyncio
async def test_run_agent_rejects_an_empty_system_prompt(tmp_path, monkeypatch):
    """空提示词 → `runAgent` 报错 → 变成**工具错误结果**(回合不被打崩,而且信息指向原因)。

    子运行得有它自己那份提示词:留空会让它继承到“什么角色也不是”的基底,
    而这种问题在子运行的结果里看不出来(它能回答,只是回答的不是你要的角色)。
    """
    llm = _ScriptedLLM([_call("delegate", {}), ChatResponse(text="父:知道了")])
    _install(tmp_path, "delegator", _DELEGATE_EXT.replace('"你是子代理。只做一件事。"', '""'))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = await _drain(runtime, "帮我委派", session)

    # 子运行没跑起来(没有第二次 LLM 调用),父拿到了带原因的错误结果
    assert len(llm.calls) == 2
    tool_result = [m.content for m in llm.calls[1] if m.role == "tool"]
    assert tool_result and "system_prompt" in tool_result[0]
    assert [e for e in events if e.kind == "text"][-1].text == "父:知道了"


# ── 闸门对子运行也生效(进程内的真实好处)──────────────

_GATE_EXT = """
from qi_agent.extensions import Tool


def register(api):
    def gate(payload, ctx):
        # 只拦子运行里的 bash(父那边也拦,但父本来就没调)
        if payload["tool_name"] == "bash":
            return {"block": True, "reason": "子运行不许用 bash"}

    api.on("tool_call", gate)


async def unused(args, ctx):
    return ""
"""

_SUB_BASH_EXT = """
from qi_agent.extensions import Tool


def register(api):
    async def delegate(args, ctx):
        # 子运行里让模型调 bash
        return await api.runAgent({"system_prompt": "子代理", "tools": ["bash"]}, "跑个命令")

    api.registerTool(Tool("delegate", "委派", {"type": "object", "properties": {}},
                          delegate))
"""


@pytest.mark.asyncio
async def test_permission_gate_applies_inside_the_sub_run(tmp_path, monkeypatch):
    """权限闸门(`tool_call`)**对子运行一样生效** —— 进程内才做得到这件事。"""
    llm = _ScriptedLLM([_call("delegate", {}),
                        _call("bash", {"command": "echo hi"}),      # 子运行想跑 bash
                        ChatResponse(text="子:被拦了"), ChatResponse(text="父:好了")])
    _install(tmp_path, "delegator", _SUB_BASH_EXT)
    _install(tmp_path, "gate", _GATE_EXT)
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "委派", session)

    # 调用序:父第 1 轮(delegate)→ 子第 1 轮(bash)→ 子第 2 轮(得到拒绝)→ 父第 2 轮
    assert llm.tools[1] == ["bash"]                  # 子运行真的拿到了 bash(否则无意义)
    sub_tool_result = [m.content for m in llm.calls[2] if m.role == "tool"]
    assert sub_tool_result and "被扩展拦截" in sub_tool_result[0]
    assert "子运行不许用 bash" in sub_tool_result[0]     # ← 闸门在子运行里生效
    # 而子运行的最终文本回到了父(作为 delegate 工具的结果)
    parent_tool_result = [m.content for m in llm.calls[3] if m.role == "tool"]
    assert parent_tool_result and parent_tool_result[0] == "子:被拦了"


# ── 模型覆盖不改变父;进度回调 ──────────────────────────

@pytest.mark.asyncio
async def test_spec_model_builds_another_client_without_touching_the_parent(
        tmp_path, monkeypatch):
    from qi_agent import runtime as runtime_mod

    built: list[str] = []

    class _Spy(LiteLLMClient):
        def __init__(self, spec, auth_store=None, thinking_level="off", retry=None):
            built.append(spec.model)
            super().__init__(spec, auth_store, thinking_level=thinking_level, retry=retry)

    llm = _ScriptedLLM([_call("delegate", {}), ChatResponse(text="子"), ChatResponse(text="父")])
    _install(tmp_path, "delegator", _DELEGATE_EXT.replace(
        '"name": "scout"}', '"name": "scout", "model": "beta/m3"}'))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    monkeypatch.setattr(runtime_mod, "LiteLLMClient", _Spy)
    parent_client = runtime.llm_exec
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "帮我委派", session)

    assert built == ["m3"]                       # 子运行另建了一个
    assert runtime.llm_exec is parent_client     # 父的客户端没被换掉


@pytest.mark.asyncio
async def test_on_event_reports_sub_run_progress(tmp_path, monkeypatch):
    """`on_event` 让父界面看得到子运行的进度(工具调用等)。"""
    llm = _ScriptedLLM([_call("read", {"path": "x"}), ChatResponse(text="子读完")])
    runtime = _runtime(tmp_path, monkeypatch, llm)
    (runtime.workdir / "x").write_text("hi", encoding="utf-8")

    seen: list[str] = []

    async def _run() -> str:
        return await runtime.run_agent(
            {"system_prompt": "子代理", "tools": ["read"]}, "读个文件",
            on_event=lambda e: seen.append(e.kind))

    assert await _run() == "子读完"
    assert {"agent_start", "tool_start", "tool_end", "agent_end"} <= set(seen)
