"""与 pi 的扩展面逐条对齐(P-E7):命名 / 参数形状 / 新事件 / 新 ctx 面。

这一批的共同点是“**照着 pi 写的扩展应当能直接跑**”,所以每条测试都用 pi 的写法
(camelCase 方法名、options 对象参数、pi 的事件名与键名)当输入,断言 qi 真的认。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.abort import AbortSignal  # noqa: E402
from qi_agent.extensions import (  # noqa: E402
    CommandRegistry,
    ExtensionApi,
    ExtensionBus,
    ExtensionContext,
    ExtensionUi,
    ModelView,
    RendererRegistry,
    Tool,
)
from qi_agent.llm import ChatResponse  # noqa: E402
from qi_agent.models import TOOL_OK, ToolOutcome  # noqa: E402
from qi_agent.registry import EXTENSION_ENTRY_FILE, ToolCatalog  # noqa: E402
from qi_agent.runner import AgentRunner, RunSpec  # noqa: E402

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


def _install(tmp_path: Path, name: str, body: str) -> None:
    d = tmp_path / "proj" / ".qi" / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(body, encoding="utf-8")


def _api(**kwargs) -> ExtensionApi:
    kwargs.setdefault("catalog", ToolCatalog())
    kwargs.setdefault("bus", ExtensionBus())
    return ExtensionApi(**kwargs)


# ── 命名:snake_case 正式 + camelCase 别名 ─────────────────────

def test_camelcase_names_are_aliases_of_the_snake_case_ones():
    """pi 的驼峰写法必须仍然可用(别名指同一个函数对象)。"""
    pairs = [
        ("registerTool", "register_tool"), ("getAllTools", "get_all_tools"),
        ("getActiveTools", "get_active_tools"), ("setActiveTools", "set_active_tools"),
        ("registerCommand", "register_command"), ("registerShortcut", "register_shortcut"),
        ("getCommands", "get_commands"), ("runAgent", "run_agent"),
        ("registerFlag", "register_flag"), ("getFlag", "get_flag"),
        ("registerProvider", "register_provider"), ("unregisterProvider", "unregister_provider"),
        ("appendEntry", "append_entry"), ("sendMessage", "send_message"),
        ("sendUserMessage", "send_user_message"),
        ("registerMessageRenderer", "register_message_renderer"),
        ("registerEntryRenderer", "register_entry_renderer"),
        ("registerMarkdownTransformer", "register_markdown_transformer"),
        ("setSessionName", "set_session_name"), ("getSessionName", "get_session_name"),
        ("setLabel", "set_label"), ("setModel", "set_model"),
        ("getThinkingLevel", "get_thinking_level"),
        ("setThinkingLevel", "set_thinking_level"),
        ("registerResolver", "register_resolver"), ("resolveTools", "resolve_tools"),
    ]
    for camel, snake in pairs:
        assert getattr(ExtensionApi, camel) is getattr(ExtensionApi, snake), camel


def test_ctx_camelcase_properties_are_aliases():
    ctx = ExtensionContext(cwd=Path("/tmp"), has_ui=True, thinking_level="high")
    assert ctx.hasUI is True
    assert ctx.thinkingLevel == "high"
    assert ctx.isProjectTrusted() is True
    assert ctx.sessionManager is ctx.session_manager
    assert ctx.modelRegistry is ctx.model_registry


# ── api:pi 的参数形状 ────────────────────────────────────────

def test_register_command_accepts_pi_options_object():
    registry = CommandRegistry()
    api = _api(_commands=registry)
    completions = lambda prefix: ["a"]  # noqa: E731
    api.registerCommand("deploy", {"handler": lambda a, c: None, "description": "部署",
                                   "getArgumentCompletions": completions})
    command = api.getCommands()[0]
    assert command["name"] == "deploy"
    assert command["description"] == "部署"
    assert command["has_argument_completions"] is True


def test_register_shortcut_and_flag_accept_pi_options_object():
    registry = CommandRegistry()
    api = _api(_commands=registry)
    api.registerShortcut("ctrl+p", {"handler": lambda c: None, "description": "计划"})
    assert registry.shortcuts()[0].description == "计划"

    from qi_agent.extensions import FlagRegistry
    flags = FlagRegistry()
    api2 = _api(_flags=flags)
    api2.registerFlag("plan", {"type": "string", "default": "x", "description": "计划模式"})
    assert api2.getFlag("plan") == "x"


def test_register_tool_accepts_pi_shaped_dict():
    catalog = ToolCatalog()
    api = _api(catalog=catalog, _name="probe")

    async def execute(tool_call_id, params, signal, on_update, ctx):   # pi 的五参形状
        return f"{tool_call_id}:{params['q']}"

    api.registerTool({"name": "pi_tool", "label": "PI 工具", "description": "d",
                      "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                      "promptSnippet": "一行摘要", "promptGuidelines": ["用 pi_tool 做某事。"],
                      "execute": execute})
    tool = catalog.get("pi_tool")
    assert isinstance(tool, Tool)
    assert tool.label == "PI 工具"
    assert tool.prompt_snippet == "一行摘要"
    assert tool.prompt_guidelines == ["用 pi_tool 做某事。"]
    info = api.getAllTools()[0]
    assert info["promptGuidelines"] == ["用 pi_tool 做某事。"]     # pi 的键名也在
    assert info["sourceInfo"] == info["source_info"]
    assert info["source_info"]["source"] == "probe"


@pytest.mark.asyncio
async def test_pi_arity_execute_receives_tool_call_id_and_on_update():
    """pi 的 `execute(toolCallId, params, signal, onUpdate, ctx)` 被按形参个数适配。"""
    seen: dict = {}

    async def execute(tool_call_id, params, signal, on_update, ctx):
        seen["id"] = tool_call_id
        seen["update"] = on_update is not None
        if on_update is not None:
            on_update({"partial": 1})
        return ToolOutcome(status=TOOL_OK, result="ok")

    from qi_agent.extensions import _adapt_execute
    from qi_agent.tools import ToolContext
    tool = Tool(name="t", description="d", parameters={"type": "object", "properties": {}},
                execute=_adapt_execute(execute))
    catalog = ToolCatalog()
    catalog.register(tool)
    runner = AgentRunner(RunSpec(name="qi", prompt="p", tools=["t"]), catalog, _StubLLM(),
                         tool_ctx=ToolContext(agent_name="qi", workdir=Path("/tmp")))
    outcome = await runner._execute([tool], _call("t", {}))
    assert outcome.ok
    assert seen["id"] == "call_1"
    assert seen["update"] is True


def _call(name: str, args: dict):
    from qi_agent.llm import ToolCallOut
    return ToolCallOut(id="call_1", name=name, args=args)


class _StubLLM:
    """满足 LLMClient 协议的最小替身(这些测试只调 `_execute`,不跑模型)。"""

    async def chat(self, messages, tools=None, temperature=None):  # noqa: ARG002
        return ChatResponse(text="", tool_calls=[])


class _ToolCallingLLM:
    """第一轮宣布一次工具调用,第二轮收工(走完整 `run()` 用)。"""

    def __init__(self) -> None:
        self.turns = 0

    async def chat(self, messages, tools=None, temperature=None):  # noqa: ARG002
        self.turns += 1
        if self.turns == 1:
            return ChatResponse(text="", tool_calls=[_call("t", {"q": "hi"})])
        return ChatResponse(text="done", tool_calls=[])


@pytest.mark.asyncio
async def test_tool_prepare_arguments_is_applied_before_execution():
    """`prepareArguments`(pi):执行前最后一次整理参数(走完整 `run()`,与落盘同源)。"""
    seen: dict = {}

    async def execute(args, ctx):  # noqa: ARG001
        seen["args"] = args
        return "ok"

    tool = Tool(name="t", description="d", parameters={"type": "object", "properties": {}},
                execute=execute,
                prepare_arguments=lambda raw: {"q": str(raw.get("q", "")).upper()})
    catalog = ToolCatalog()
    catalog.register(tool)
    runner = AgentRunner(RunSpec(name="qi", prompt="p", tools=["t"]), catalog,
                         _ToolCallingLLM())
    async for _event in runner.run("go"):
        pass
    assert seen["args"] == {"q": "HI"}


# ── api:新方法(会话名 / label / 模型 / provider)──────────────

@pytest.mark.asyncio
async def test_api_session_name_label_and_model(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True)
    session = runtime.sessions.create("t", cwd=project)
    api = ExtensionApi(catalog=runtime.catalog, bus=runtime.bus, _host=runtime,
                       _name="probe")

    async def turn():
        runtime._active_session = session
        try:
            api.setSessionName("新名字")
            assert api.getSessionName() == "新名字"
            api.setLabel("entry-1", "书签")
            stored = runtime.sessions.get(session.id)
            assert stored is not None
            assert stored.entries[-1]["label"] == "书签"
            assert api.getThinkingLevel() in ("off", "low", "high", "max", "minimal")
            assert isinstance(api.setModel("ollama/x"), bool)
        finally:
            runtime._active_session = None

    await turn()
    assert session.title == "新名字"


def test_api_unregister_provider(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=tmp_path, approve_project=True)
    api = ExtensionApi(catalog=runtime.catalog, bus=runtime.bus, _host=runtime, _name="p")
    api.registerProvider("proxy", {"baseUrl": "https://x", "api": "openai-completions",
                                   "models": [{"id": "m"}]})
    assert "proxy" in runtime.cfg.providers
    api.unregisterProvider("proxy")
    assert "proxy" not in runtime.cfg.providers


# ── ctx:新成员 ────────────────────────────────────────────

def test_model_view_is_a_str_with_pi_fields():
    view = ModelView("anthropic", "claude", api="anthropic-messages", reasoning=True,
                     context_window=200000, max_tokens=8192, name="Claude")
    assert view == "anthropic/claude"          # 旧用法(字符串比较)不变
    assert json.dumps({"model": view})          # 旧的 JSON 用法不变
    assert view.id == "claude" and view.provider == "anthropic"
    assert view.contextWindow == 200000 and view.maxTokens == 8192
    assert view.label == "anthropic/claude"


def test_ctx_exposes_mode_and_host_backed_methods(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=tmp_path, approve_project=True, mode="tui")
    ctx = runtime.extension_ctx()
    assert ctx.mode == "tui"
    assert ctx.isIdle() is True
    assert ctx.hasPendingMessages() is False
    assert isinstance(ctx.getSystemPrompt(), str) and ctx.getSystemPrompt()
    assert isinstance(ctx.getSystemPromptOptions(), dict)
    ctx.abort()                                 # 没有回合在跑:no-op,不该抛
    usage = ctx.getContextUsage()
    assert usage is None or "contextWindow" in usage


@pytest.mark.asyncio
async def test_ctx_session_ops_require_host():
    ctx = ExtensionContext(cwd=Path("/tmp"))
    with pytest.raises(RuntimeError, match="new_session"):
        await ctx.new_session()


# ── api.events.on 返回退订函数 ────────────────────────────────

def test_events_on_returns_unsubscribe():
    bus = ExtensionBus()
    api = _api(bus=bus)
    seen: list = []
    unsubscribe = api.events.on("ping", seen.append)
    api.events.emit("ping", 1)
    assert seen == [1]
    unsubscribe()
    api.events.emit("ping", 2)
    assert seen == [1]


# ── 新事件 ─────────────────────────────────────────────────

class _FakeLLM:
    """第一轮宣布一次工具调用,第二轮给文本。"""

    def __init__(self) -> None:
        self.turns = 0

    async def chat(self, messages, tools=None, temperature=None):
        self.turns += 1
        if self.turns == 1:
            from qi_agent.llm import ToolCallOut
            return ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="probe",
                                                                 args={})])
        return ChatResponse(text="done", tool_calls=[])


@pytest.mark.asyncio
async def test_message_tool_execution_and_settled_events(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    log = tmp_path / "events.jsonl"
    _install(tmp_path, "watcher", f"""
import json
from pathlib import Path
from qi_agent.extensions import Tool

LOG = Path({str(log)!r})

def register(api):
    def record(payload, ctx):
        LOG.write_text(json.dumps({{"tool_name": payload.get("tool_name")}}),
                       encoding="utf-8")
    for event in ("message_start", "message_update", "message_end",
                  "tool_execution_start", "tool_execution_update",
                  "tool_execution_end", "tool_result", "turn_start", "turn_end",
                  "input", "before_agent_start", "context", "agent_start", "agent_end"):
        api.on(event, lambda payload, ctx, _e=event: None)
    api.on("tool_execution_end", record)
    api.on("message_end", lambda payload, ctx: None)
    api.on("agent_settled", lambda payload, ctx: None)

    async def probe(args, ctx):
        if ctx.on_update is not None:
            ctx.on_update({{"partial": 1}})
        return "ok"

    api.registerTool(Tool(name="probe", description="d",
                          parameters={{"type": "object", "properties": {{}}}},
                          execute=probe))
""")

    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True, llm=_FakeLLM(),
                        has_ui=True)
    session = runtime.sessions.create("t", cwd=project)
    await runtime.start_session(session)
    async for _event in runtime.stream("跑一下", session):
        pass
    assert "tool_execution_end" in runtime.bus.events
    assert "message_start" in runtime.bus.events
    assert runtime.notes == [] or all("失败" not in n for n in runtime.notes)
    seen = json.loads(log.read_text(encoding="utf-8"))
    assert seen["tool_name"] == "probe"


@pytest.mark.asyncio
async def test_resources_discover_and_session_shutdown(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    skill_dir = tmp_path / "ext-skills" / "mine"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: mine\ndescription: 扩展给的技能\n---\n\n正文\n", encoding="utf-8")
    seen = tmp_path / "seen.json"
    # pi 的 `skillPaths` 是**资源根**(里面是 <name>/SKILL.md)。qi 两种都收:
    # 给根也行,直接给某个技能目录也行(见 runtime `_emit_resources_discover`)。
    _install(tmp_path, "res", f"""
import json
from pathlib import Path
SEEN = Path({str(seen)!r})

def register(api):
    def on_resources(payload, ctx):
        SEEN.write_text(json.dumps({{"reason": payload["reason"]}}), encoding="utf-8")
        return {{"skillPaths": [{str(skill_dir)!r}]}}
    api.on("resources_discover", on_resources)
    api.on("session_shutdown", lambda payload, ctx: None)
""")

    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True)
    session = runtime.sessions.create("t", cwd=project)
    await runtime.start_session(session, reason="startup")
    data = json.loads(seen.read_text(encoding="utf-8"))
    assert data["reason"] == "startup"
    assert any(getattr(s, "name", "") == "mine" for s in runtime.top_skills)
    assert "session_shutdown" in runtime.bus.events
    await runtime.emit_session_shutdown("quit")


# ── 会话操作:扩展发起的 + 可取消闸门 ──────────────────────────

@pytest.mark.asyncio
async def test_ctx_session_ops_and_cancel_gate(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _install(tmp_path, "gate", """
def register(api):
    def on_before(payload, ctx):
        return {"cancel": True}
    api.on("session_before_switch", on_before)
    api.on("session_before_fork", on_before)
    api.on("session_before_tree", on_before)
""")
    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True)
    session = runtime.sessions.create("t", cwd=project)
    # 项目级扩展在 `start_session` 的信任判定之后才装载(它们是仓库控制的代码)
    await runtime.start_session(session)
    runtime._active_session = session
    ctx = runtime.extension_ctx(session=session)
    assert (await ctx.new_session())["cancelled"] is True
    assert (await ctx.fork("whatever"))["cancelled"] is True
    assert (await ctx.navigate_tree("whatever"))["cancelled"] is True
    assert (await runtime.before_session_op("session_before_switch",
                                            {"reason": "new"})) == {"cancel": True}


@pytest.mark.asyncio
async def test_tui_style_before_session_op_without_extensions_passes(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=tmp_path, approve_project=True)
    assert await runtime.before_session_op("session_before_switch", {"reason": "new"}) is None


# ── 渲染三件套登记 + tool_call_id/on_update 的 tool_ctx ────────

def test_renderer_registry_registers_and_chains_markdown():
    renderers = RendererRegistry()
    api = _api(_renderers=renderers)
    api.registerMessageRenderer("card", lambda *a: "M")
    api.registerEntryRenderer("card", lambda *a: "E")
    api.registerMarkdownTransformer(lambda text, ctx=None: text.replace("a", "b"))
    api.registerMarkdownTransformer(lambda text, ctx=None: text.upper())
    assert renderers.message_renderer("card") is not None
    assert renderers.entry_renderer("card") is not None
    assert renderers.apply_markdown("abc") == "BBC"


@pytest.mark.asyncio
async def test_ui_prompt_events_are_dispatched():
    bus = ExtensionBus()
    seen: list[str] = []
    for name in ("ui_prompt_start", "ui_prompt_end"):
        bus.on(name, lambda payload, ctx, _n=name: seen.append(_n))

    class Frontend:
        async def confirm(self, message, *, title=None, default=False):  # noqa: ARG002
            return True

    ui = ExtensionUi(frontend=Frontend(), bus=bus,
                     ctx_factory=lambda signal=None: ExtensionContext(cwd=Path("/tmp")),
                     mode="tui")
    assert await ui.confirm("继续?") is True
    assert seen == ["ui_prompt_start", "ui_prompt_end"]


def test_ui_component_layer_reports_when_unavailable():
    notes: list[str] = []
    ui = ExtensionUi(frontend=None, notes=notes, mode="print")
    ui.setWidget("k", ["a"])
    assert any("set_widget" in n for n in notes)


def test_abort_signal_used_by_ctx_abort():
    signal = AbortSignal()
    ctx = ExtensionContext(cwd=Path("/tmp"), signal=signal)
    ctx.abort()
    assert signal.aborted is True


# ── pi 兼容层:payload 的 `type` 与驼峰键别名 ──────────────────

@pytest.mark.asyncio
async def test_payload_carries_pi_type_field_and_camel_aliases():
    """pi 的 handler 读 `event.type` / `event.toolName`;qi 的键名仍独立存在。"""
    bus = ExtensionBus()
    seen: dict = {}
    bus.on("tool_call", lambda payload, ctx: seen.update(payload))
    await bus.emit("tool_call",
                   {"tool_name": "read", "tool_call_id": "c1", "input": {"path": "a.py"}},
                   ctx=ExtensionContext(cwd=Path("/tmp")))
    assert seen["type"] == "tool_call"        # pi 的判别字段
    assert seen["toolName"] == "read"         # pi 的驼峰别名
    assert seen["toolCallId"] == "c1"
    assert seen["input"] == {"path": "a.py"}
    assert seen["tool_name"] == "read"        # qi 自己的键**一个不少**
    assert seen["tool_call_id"] == "c1"


@pytest.mark.asyncio
async def test_handler_added_keys_also_get_aliases():
    """handler 里新设的 `tool_name` 也要能被后面的 handler 以 `toolName` 读到。"""
    bus = ExtensionBus()
    seen: dict = {}

    def first(payload, ctx):
        return {"tool_name": "改写过的"}

    def second(payload, ctx):
        seen.update(payload)

    bus.on("tool_call", first)
    bus.on("tool_call", second)
    await bus.emit("tool_call", {"input": {}}, ctx=ExtensionContext(cwd=Path("/tmp")))
    assert seen["toolName"] == "改写过的"


def test_constrained_sampling_is_accepted_but_reported():
    """pi 的 `constrainedSampling`:qi 收下(pi 风格的工具定义不炸),但**说出来它被忽略**。"""
    from types import SimpleNamespace

    notes: list[str] = []
    api = _api(_host=SimpleNamespace(notes=notes))

    async def execute(args, ctx):  # noqa: ANN001, ARG001
        return "ok"

    api.register_tool({"name": "cs_tool", "execute": execute,
                       "constrainedSampling": {"type": "json_schema", "strict": "require"}})
    assert any("constrainedSampling" in n for n in notes)
    assert api.get_all_tools()[0]["name"] == "cs_tool"      # 照常注册


def test_tool_definition_accepts_pi_render_shell_and_execution_mode():
    api = _api()
    api.register_tool({"name": "t", "execute": lambda a, c: None,
                       "renderShell": "self", "executionMode": "parallel"})
    tool = api.catalog.get("t")
    assert tool.render_shell == "self"
    assert tool.execution_mode == "parallel"
