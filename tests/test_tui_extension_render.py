"""P-E7 前端消费面:TUI 真的用上了扩展的渲染回调与组件层接口。

`tests/test_tui_style.py` 管的是**内置**渲染基线;这里管的是**扩展给的**东西有没有被消费
—— 登记了却没人用的接口是最难发现的一类半对齐(扩展以为自己接管了渲染,实际什么都没有变)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from textual.widgets import Static  # noqa: E402,F401

from qi_agent import tui as tui_mod  # noqa: E402
from qi_agent.extensions import (  # noqa: E402
    CommandRegistry,
    ExtensionBus,
    ExtensionContext,
    RendererRegistry,
    Tool,
)
from qi_agent.registry import ToolCatalog  # noqa: E402
from qi_agent.tui import (  # noqa: E402
    ExtensionToolBlock,
    QiTui,
    ToolBlock,
)
from test_tui_style import MODEL, PALETTE, FakeRuntime, _tui_env  # noqa: E402


async def _noop_execute(args, ctx):  # noqa: ANN001, ARG001
    return "ok"


def _tool(name: str, **kwargs) -> Tool:
    return Tool(name=name, description="d", parameters={"type": "object", "properties": {}},
                execute=_noop_execute, **kwargs)


class RenderFakeRuntime(FakeRuntime):
    """在基线替身上补出扩展渲染需要的三样:renderers / catalog / extension_ctx。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.renderers = RendererRegistry()
        self.catalog = ToolCatalog()
        self.bus = ExtensionBus()
        self.cwd = Path.cwd()
        self.mode = "tui"

    def extension_ctx(self, signal=None):  # noqa: ANN001, ARG002
        return ExtensionContext(cwd=self.cwd, mode="tui", notes=self.notes, host=self)


def _boot(tmp_path, monkeypatch, runtime: FakeRuntime) -> None:
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", lambda *a, **k: runtime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)


# ── 工具的 renderCall / renderResult ─────────────────────────

@pytest.mark.asyncio
async def test_tool_render_call_and_result_replace_default_block(tmp_path, monkeypatch):
    runtime = RenderFakeRuntime()
    runtime.catalog.register(_tool("fancy", render_call=lambda args, ctx: Static("CALL"),
                                   render_result=lambda result, ctx: Static(f"R:{result}")))
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)):
        block = app._tool_block("fancy", {"q": "x"})
        assert isinstance(block, ExtensionToolBlock)
        # renderCall 的组件已经在待挂槽位里(挂载时机由 on_mount 决定)
        assert getattr(block._widget, "render", None) is not None

        widget = app._tool_result_widget("fancy", "结果")
        assert widget is not None


@pytest.mark.asyncio
async def test_tool_without_hooks_still_uses_builtin_block(tmp_path, monkeypatch):
    runtime = RenderFakeRuntime()
    runtime.catalog.register(_tool("plain"))
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)):
        assert isinstance(app._tool_block("plain", {}), ToolBlock)
        assert app._tool_result_widget("plain", "x") is None


@pytest.mark.asyncio
async def test_broken_render_call_falls_back_and_is_visible(tmp_path, monkeypatch):
    def boom(args, ctx):  # noqa: ANN001, ARG001
        raise RuntimeError("扩展炸了")

    runtime = RenderFakeRuntime()
    runtime.catalog.register(_tool("broken", render_call=boom))
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)):
        said: list[str] = []
        app._note = lambda text, tone="dim": said.append(str(text))  # type: ignore[method-assign]
        block = app._tool_block("broken", {})
        # 回调坏了不该让工具卡片消失:退回内置 + 在界面上说一句
        assert isinstance(block, ToolBlock)
        assert any("渲染回调失败" in n for n in said)


# ── markdown transformer ────────────────────────────────────

@pytest.mark.asyncio
async def test_markdown_transformer_is_applied(tmp_path, monkeypatch):
    runtime = RenderFakeRuntime()
    runtime.renderers.add_markdown(lambda text, ctx=None: text.replace("秘密", "***"))
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)):
        assert app._transform_markdown("这是秘密") == "这是***"


# ── entry / message renderer 在回放里生效 ────────────────────

@pytest.mark.asyncio
async def test_entry_renderer_is_used_in_replay(tmp_path, monkeypatch):
    runtime = RenderFakeRuntime()
    runtime.renderers.add_entry("card", lambda entry, ctx=None: Static("CARD", id="card-widget"))
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        session = runtime.sessions.create("t")
        runtime.sessions.append(session, {"type": "custom", "custom_type": "card",
                                          "data": {"title": "hi"}})
        app._replay_branch(session)
        await pilot.pause(0.05)
        assert app.query("#card-widget"), "注册的 entry renderer 没有被回放消费"


@pytest.mark.asyncio
async def test_entry_renderer_returning_none_falls_back(tmp_path, monkeypatch):
    runtime = RenderFakeRuntime()
    runtime.renderers.add_entry("card", lambda entry, ctx=None: None)
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        session = runtime.sessions.create("t")
        runtime.sessions.append(session, {"type": "custom", "custom_type": "card",
                                          "data": {}})
        app._replay_branch(session)
        await pilot.pause(0.05)
        # 返回 None → 退回默认渲染,绝不白屏
        assert list(app.query_one("#log").children)


# ── 命令参数补全 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extension_argument_completions_are_used(tmp_path, monkeypatch):
    runtime = RenderFakeRuntime()
    runtime.commands = CommandRegistry()
    runtime.commands.add_command(
        "deploy", lambda a, c: None, description="部署",
        get_argument_completions=lambda prefix: ["prod", "staging"])
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)):
        items = app._extension_argument_candidates("/deploy", "")
        assert [c.value for c in items] == ["prod", "staging"]
        # 前缀过滤由调用方(pi 的 provider)负责;qi 交给补全面板自己的前缀匹配
        assert app._extension_argument_candidates("/nope", "") == []


@pytest.mark.asyncio
async def test_async_argument_completions_are_reported_not_silent(tmp_path, monkeypatch):
    async def completer(prefix):  # noqa: ANN001, ARG001
        return ["x"]

    runtime = RenderFakeRuntime()
    runtime.commands.add_command("deploy", lambda a, c: None, get_argument_completions=completer)
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)):
        said: list[str] = []
        app._note = lambda text, tone="dim": said.append(str(text))  # type: ignore[method-assign]
        assert app._extension_argument_candidates("/deploy", "") == []
        assert any("async" in n for n in said)


# ── 组件层:footer / header / widget / 终端输入 ────────────────

@pytest.mark.asyncio
async def test_set_footer_replaces_and_restores_builtin(tmp_path, monkeypatch):
    runtime = RenderFakeRuntime()
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        app.set_extension_footer(lambda ctx: Static("我的 footer"))
        await pilot.pause(0.05)
        assert app.query(".ext-footer")
        assert app.query_one("#footer").styles.display == "none"

        app.set_extension_footer(None)
        await pilot.pause(0.05)
        assert not app.query(".ext-footer")
        assert app.query_one("#footer").styles.display == "block"


@pytest.mark.asyncio
async def test_set_header_mounts_above_log(tmp_path, monkeypatch):
    runtime = RenderFakeRuntime()
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        app.set_extension_header(lambda ctx: Static("HEADER"))
        await pilot.pause(0.05)
        assert app.query(".ext-header")

        app.set_extension_header(None)
        await pilot.pause(0.05)
        assert not app.query(".ext-header")


@pytest.mark.asyncio
async def test_set_widget_goes_into_requested_slot(tmp_path, monkeypatch):
    runtime = RenderFakeRuntime()
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        app.set_extension_widget("k", ["一行"], {"placement": "belowEditor"})
        await pilot.pause(0.05)
        below = app.query_one("#ext-widgets-below")
        assert below.children
        app.set_extension_widget("k", None, {})
        await pilot.pause(0.05)
        assert not below.children


@pytest.mark.asyncio
async def test_terminal_input_handler_can_consume(tmp_path, monkeypatch):
    runtime = RenderFakeRuntime()
    _boot(tmp_path, monkeypatch, runtime)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)):
        seen: list[str] = []

        class FakeKey:
            key = "x"
            stopped = False

            def stop(self) -> None:
                self.stopped = True

        unsubscribe = app.add_terminal_input_handler(
            lambda key: (seen.append(key), {"consume": True})[1])
        event = FakeKey()
        app.on_key(event)                       # type: ignore[arg-type]
        assert seen == ["x"]
        assert event.stopped is True

        unsubscribe()
        event2 = FakeKey()
        app.on_key(event2)                      # type: ignore[arg-type]
        assert seen == ["x"]                    # 退订后不再收到
        assert event2.stopped is False
