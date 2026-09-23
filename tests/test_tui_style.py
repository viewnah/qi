"""TUI 视觉基线(stub runtime,不联网)。

锁住 pi 的行样式:工具标题格式、输出截断、分派行、以及消息块/工具块/编辑器边框/
footer 真的取到了 pi 调色板里的颜色。像素级对齐由人工比对,这里防的是“改坏了没人发现”。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import sys
import time
from typing import Any, cast
from pathlib import Path

import pytest

from qi_agent import tui as tui_mod
from qi_agent.config import ResolvedModel
from qi_agent.models import AgentEvent
from qi_agent.llm import THINKING_LEVELS, LiteLLMClient
from qi_agent.session import SessionStore
from qi_agent.settings import QiSettings
from qi_agent.theme import load_palette
from textual.widgets import Input, Static
from qi_agent.tui import (
    MAX_EDITOR_ROWS,
    SPINNER_FRAMES,
    Editor,
    QiTui,
    TuiRenderer,
    ToolBlock,
    UserMessage,
)

PALETTE = load_palette("dark")
MODEL = ResolvedModel(provider="deepseek", model="deepseek-v4.1-flash",
                      api="openai-completions", base_url=None, api_key_ref=None,
                      reasoning=True, context_window=1_000_000, max_tokens=16384)

_MODELS = ('{"providers": {"ollama": {"api": "openai-completions", '
           '"models": [{"id": "x"}]}}}')
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _renderer() -> TuiRenderer:
    return TuiRenderer(PALETTE, Path("/tmp"))


def _span_style(line, index: int):
    """Rich 的 span.style 可能是 str;统一成 Style 再断言。"""
    from rich.style import Style

    raw = line.spans[index].style
    return raw if isinstance(raw, Style) else Style.parse(raw or "")


# ── 纯渲染(不启动 App)──────────────────────────────


def test_tool_titles_match_pi_format():
    r = _renderer()
    assert r.tool_title("read", {"path": "src/a.py"}).plain == "read src/a.py"
    assert r.tool_title("read", {"path": "src/a.py", "start_line": 3, "end_line": 9}).plain \
        == "read src/a.py:3-9"
    assert r.tool_title("write", {"path": "x.py"}).plain == "write x.py"
    assert r.tool_title("edit", {"path": "x.py"}).plain == "edit x.py"
    assert r.tool_title("ls", {}).plain == "ls ."
    assert r.tool_title("find", {"pattern": "*.py", "path": "."}).plain == "find *.py in ."
    assert r.tool_title("grep", {"pattern": "TODO", "path": "."}).plain == "grep /TODO/ in ."
    assert r.tool_title("bash", {"command": "ls -la"}).plain == "$ ls -la"
    assert r.tool_title("clarify", {"question": "哪个环境?"}).plain == "clarify 哪个环境?"


def test_tool_titles_use_pi_colors():
    line = _renderer().tool_title("read", {"path": "a.py"})
    name_style = _span_style(line, 0)
    path_style = _span_style(line, 1)
    assert name_style.bold                                  # 工具名 bold
    assert path_style.color is not None
    assert path_style.color.get_truecolor().hex == PALETTE.hex("accent")


def test_tool_body_truncates_and_hints_expand():
    output = "\n".join(f"line{i}" for i in range(30))
    body = _renderer().tool_body("ls", output, expanded=False)      # ls 上限 20 行
    assert "line19" in body.plain and "line20" not in body.plain
    assert "... (10 more lines, ctrl+o to expand)" in body.plain


def test_tool_body_expanded_shows_all():
    output = "\n".join(f"line{i}" for i in range(30))
    body = _renderer().tool_body("ls", output, expanded=True)
    assert "line29" in body.plain and "more lines" not in body.plain


def test_read_result_hidden_until_expanded():
    """pi 的 read 折叠态不展示文件内容,展开或出错才展示。"""
    body = _renderer().tool_body("read", "secret content", expanded=False)
    assert body.plain == ""
    assert "secret content" in _renderer().tool_body("read", "secret content", expanded=True).plain
    assert "boom" in _renderer().tool_body("read", "boom", expanded=False, is_error=True).plain


def test_dispatch_line_is_pi_style():
    line = _renderer().dispatch_line("general", {"display_name": "qi", "source": "router",
                                                 "confidence": 0.9})
    assert line.plain == "● → qi (router, 0.90)"


def test_banner_matches_pi_compact_header():
    text = _renderer().banner("0.1.0", ["termio"], ["qi-web", "qi-agents"]).plain
    assert "qi v0.1.0" in text
    # 只写 qi 真的实现了的快捷键(escape 中断、ctrl+c/ctrl+d 清空退出、ctrl+o 展开)
    assert "escape interrupt" in text
    assert "ctrl+c/ctrl+d clear/exit" in text and "ctrl+o tools" in text
    assert "! bash" in text and "/ commands" in text
    assert "@ files" not in text                       # pi 紧凑行里没有它
    # 引导语照搬 pi 句式(只换产品名)
    assert "Qi can explain its own features" in text
    # 资源清单:[Skills] / [Extensions] 平铺(没有 [Agents]:core 没有角色概念)
    assert "[Agents]" not in text
    assert "[Skills]" in text and "  termio" in text
    assert "[Extensions]" in text and "  qi-web, qi-agents" in text


def test_banner_omits_empty_resource_sections():
    text = _renderer().banner("0.1.0", [], []).plain
    assert "[Skills]" not in text and "[Extensions]" not in text


def test_working_border_embeds_spinner():
    app = QiTui(palette=PALETTE)
    app._working = True
    line = app._top_border()
    assert "Working" in line.plain
    assert SPINNER_FRAMES[0] in line.plain
    assert line.plain.startswith("── ")


def test_idle_border_is_a_full_rule():
    app = QiTui(palette=PALETTE)
    app._working = False
    line = app._top_border()
    assert set(line.plain) == {"─"}


def test_working_and_idle_border_shapes():
    """编辑器上边框:空闲是整行 `─`,工作态嵌入 `⠋ Working`(pi 的 loader)。"""
    app = QiTui(palette=PALETTE)
    app._working = False
    assert set(app._top_border().plain) == {"─"}
    app._working = True
    line = app._top_border()
    assert line.plain.startswith("── " + SPINNER_FRAMES[0] + " Working ")


# ── App 集成(stub runtime)────────────────────────────


class _FakeRegistry:
    names = ("general", "code-analyst")

    def all(self):
        return []

    def get(self, name):
        return object() if name in self.names else None


PROMPTS: list[str] = []          # 本轮提交过的 prompt(测试里清空)


class FakeRuntime:
    def __init__(self, *args, **kwargs):
        from types import SimpleNamespace

        from qi_agent.session import SessionStore

        self.sessions = SessionStore()
        self.cfg: Any = None      # 测试里会被换成假 cfg(所以类型真的就是“任意”)
        self.cwd = Path.cwd()
        self.registry = _FakeRegistry()
        self.settings = QiSettings()
        # 与 QiRuntime 对齐的可写字段(思考级别相关)
        self.thinking_level = "off"
        self.llm_exec = SimpleNamespace(thinking_level="off", reasoning_dropped=False)
        self.top_skills: list = []      # 顶层技能(core 的能力,TUI banner 用它)
        self.extensions: list = []      # /reload 的提示读它
        self.notes: list[str] = []      # 启动提示(TUI 会逐条展示)
        # TUI 的 `_command` / `_help_text` 会读扩展命令登记处
        from qi_agent.extensions import CommandRegistry

        self.commands = CommandRegistry()
        self.model_switches: list[tuple[str, str, str]] = []   # `set_model` 的记账

    # ── 模型 / 思考级别 / 会话名:替身**建模**契约 ──────────
    #
    # 试过“委托给真实现”(cast 成 QiRuntime 调未绑定方法),但真方法会一路调到它的**私有**
    # 助手(`_emit_notice`),替身得把那些也补上 —— 那不是建模,是把 runtime 搬一遍。
    # 所以回到本仓一贯的做法:替身只建契约(能跑、能记账),**真行为由 runtime 级测试覆盖**
    # (`tests/test_extension_model.py`:换 llm_exec、发 model_select / thinking_level_select…)。

    def set_model(self, provider: str, model: str, *, source: str = "set"):
        """用真解析器拿 `ResolvedModel`(纯函数),并记账。"""
        from qi_agent.config import resolve_model

        self.model_switches.append((provider, model, source))
        return resolve_model(self.cfg, provider, model)

    def set_thinking_level(self, level: str, *, source: str = "set") -> str:
        from qi_agent.llm import normalize_thinking_level

        self.thinking_level = normalize_thinking_level(level)
        self.llm_exec.thinking_level = self.thinking_level   # 按契约写回 client
        return self.thinking_level

    def set_session_title(self, session, title: str, *, source: str = "auto") -> None:
        self.sessions.set_title(session, title)

    async def start_session(self, session, reason: str = "startup") -> None:
        """真实 QiRuntime 的会话级事件;假运行时不用它(不派发任何事件)。"""

    async def compact_session(self, session, instructions=None):
        return None                    # 测试默认:没什么可压

    async def summarize_branch_for_jump(self, session, source_branch, from_id, target_id):
        return None

    async def stream(self, prompt, session, agent_override=None, abort=None):
        PROMPTS.append(prompt)
        yield AgentEvent(kind="dispatch", agent="general", text="qi (router, 0.90)",
                         data={"confidence": 0.9, "source": "router", "agent": "general",
                               "display_name": "qi", "reasoning": ""})
        yield AgentEvent(kind="text_delta", agent="general", text="我来读一下。\n")
        yield AgentEvent(kind="assistant_message", agent="general", text="我来读一下。\n",
                         data={"step": 1, "tool_calls": ["read"]})
        yield AgentEvent(kind="tool_start", agent="general", tool="read",
                         data={"args": {"path": "pyproject.toml"}})
        yield AgentEvent(kind="tool_end", agent="general", tool="read",
                         text="[project]\nname=qi\n",
                         data={"status": "ok", "duration_ms": 12})
        yield AgentEvent(kind="assistant_message", agent="general",
                         text="## 结论\n\n就是 qi。\n", data={"step": 2, "tool_calls": []})
        yield AgentEvent(kind="text", agent="general", text="")
        yield AgentEvent(kind="agent_end", agent="general", text="",
                         data={"usage": {"prompt_tokens": 12345, "completion_tokens": 678}})


def _tui_env(tmp_path, monkeypatch) -> None:
    from qi_agent import paths

    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)


@pytest.mark.asyncio
async def test_prompt_input_is_a_bare_prompt_line(tmp_path, monkeypatch):
    """pi 的输入行 = `prompt + value`(`> ` 前缀,**没有框也没有底色**)。

    pi-tui 的 `Input` 渲染就是 `this.prompt + value`,`prompt` 默认 `"> "`
    (`components/input.js`)—— Textual 的 `Input` 自带 tall 边框,所以 qi 拼了一个
    `> ` 前缀 + 无边框输入。
    """
    from textual.containers import Horizontal

    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = _fake_cfg()
        app._command("/login alpha")
        await pilot.pause(0.15)
        screen = app.screen
        assert isinstance(screen, tui_mod.PromptScreen)

        row = screen.query_one(".input-row")
        assert isinstance(row, Horizontal)
        assert row.styles.height is not None and str(row.styles.height) == "1"   # 只占一行
        assert str(row.query_one(".input-prompt").render()) == "> "

        field = screen.query_one("#prompt-input", Input)
        assert field.password is True                           # key 仍然遮罩
        assert not field.styles.border_top[0]                    # 不再有框(Textual 用 "")
        assert field.styles.background.is_transparent

        await pilot.press("escape")
        await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_command_selector_uses_pi_editor_slot_shell(tmp_path, monkeypatch):
    """命令选择器 = pi 的 `showSelector()` 形状:占编辑器那一格、全宽、上下 `─`、**不遮罩**。

    pi 把 `editorContainer` 的内容换成选择器组件 —— 所以它跟编辑器同宽、底边贴在同一条线
    (footer 之上),而**不是**居中浮层;组件本身没有底色(终端底色透上来)。
    """
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = _fake_cfg()
        app._command("/model")
        await pilot.pause(0.15)
        screen = app.screen
        assert isinstance(screen, tui_mod.ModelSelector)

        # 不遮罩(pi 没有 modal backdrop)、不是居中浮层
        assert screen.styles.background.is_transparent
        assert str(screen.styles.align_vertical) == "bottom"

        box = screen.query_one("#model-box")
        assert box.styles.width is not None and box.styles.width.value == 1   # 全宽
        assert box.styles.border_top[0] == "solid"                            # pi 的 DynamicBorder
        assert box.styles.border_bottom[0] == "solid"
        assert box.region.width == 100
        # 底边贴在 footer 之上(占的就是编辑器那一格)
        assert box.styles.margin.bottom == app._footer_rows()
        assert box.region.bottom == 30 - app._footer_rows()

        # 标题 accent + bold、键位提示 muted(pi 的颜色分工)
        hint = screen.query_one("#model-hint")
        assert hint.styles.color is not None
        assert hint.styles.color.hex.lower() == PALETTE.hex("accent").lower()
        assert hint.styles.text_style is not None and hint.styles.text_style.bold

        # 行格式 = pi 的模型选择器:`→ ` + `  `/`✓ ` + id + muted `[provider]`
        rows = screen.rendered_text().plain.split("\n")
        assert rows[0].startswith("→   m1 [alpha]")

        await pilot.press("escape")
        await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_thinking_selector_marks_current_and_default(tmp_path, monkeypatch):
    """`/thinking` 无参:pi 的 `ThinkingSelectorComponent` 行(`✓ ` 当前 + 说明 + ` · default`)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app._thinking_level = "low"
        app._command("/thinking")
        await pilot.pause(0.15)
        screen = app.screen
        assert isinstance(screen, tui_mod.ThinkingSelector)
        rows = screen.rendered_text().plain.split("\n")
        assert any(row.startswith("→ ✓ low") for row in rows)          # 当前档
        assert any("· default" in row for row in rows)                 # 默认档标记
        assert all(level in screen.rendered_text().plain for level in THINKING_LEVELS)

        # 选一个:enter 真的把级别设下去
        screen.highlighted = THINKING_LEVELS.index("high")
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert app._thinking_level == "high"
    """默认 fullscreen:qi 拥有视口 —— transcript 拿 `1fr`,底部三件套钉在末尾。

    这就是“滚轮只在 TUI 内移动”的前提:内容装不下时由 `#log` 自己在内滚动,
    而不是把行推到终端回滚缓冲里。
    """
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.1)
        assert app._fullscreen is True
        assert "regular" not in app.screen.classes       # 走 fullscreen 那套 CSS

        log = app.query_one("#log")
        assert str(log.styles.height) == "1fr"           # 占满剩余空间并**内部**滚动
        assert log.styles.max_height is None             # 不塞 inline style 盖掉 1fr
        assert log.region.height > 0

        # 底部固定:footer 贴终端底边,editor 在它上方(布局没被 transcript 顶下去)
        footer = app.query_one("#footer")
        assert footer.region.bottom == 24
        assert app.query_one("#editor").region.y < footer.region.y


@pytest.mark.asyncio
async def test_fullscreen_transcript_scrolls_without_stealing_focus(tmp_path, monkeypatch):
    """滚轮滚的是 transcript(不是终端回滚缓冲);点 transcript 也不抢走编辑器的焦点。

    这两条是 fullscreen “qi 拥有视口”的实际含义:焦点被抢走后用户点一下就打不了字。
    """
    from textual import events

    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause(0.1)
        log = app.query_one("#log")
        for index in range(40):
            app._append(Static(f"line{index}"))
        await pilot.pause(0.2)

        assert log.max_scroll_y > 0                     # 内容装不下 → 由视口自己在滚
        bottom = log.scroll_y
        log.post_message(events.MouseScrollUp(log, 5, 5, 0, 1, 0, False, False, False))
        await pilot.pause(0.15)
        assert log.scroll_y < bottom                    # 滚轮真的滚了 transcript

        await pilot.click("#log")
        await pilot.pause(0.05)
        focused = app.focused
        assert focused is not None and focused.id == "editor"   # 焦点仍在编辑器


@pytest.mark.asyncio
async def test_regular_mode_is_still_inline(tmp_path, monkeypatch):
    """`tuiMode: regular` = inline:Screen 不撑满,transcript 高度由 `_sync_log_height` 算。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE, tui_mode="regular")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.1)
        assert app._fullscreen is False
        assert "regular" in app.screen.classes           # 选到 inline 那套 CSS
        assert app.query_one("#log").styles.max_height is not None   # 按终端高算出来的上限


@pytest.mark.asyncio
async def test_tui_renders_pi_blocks_and_footer(tmp_path, monkeypatch):
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app._submit("读一下 pyproject.toml")
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)

        children = list(app.query_one("#log").children)
        users = [w for w in children if isinstance(w, UserMessage)]
        tools = [w for w in children if isinstance(w, ToolBlock)]
        assistants = [w for w in children if getattr(w, "text", "") == "## 结论\n\n就是 qi。\n"]

        # 用户消息:pi 的 userMessageBg 底色块 + markup 原文
        assert users and users[0].source == "读一下 pyproject.toml"
        assert users[0].styles.background.hex == PALETTE.hex("userMessageBg")

        # 工具块:成功态底色 + pi 的工具标题;read 折叠态不展示内容
        assert tools and tools[-1].styles.background.hex == PALETTE.hex("toolSuccessBg")
        assert tools[-1].content.plain.startswith("read pyproject.toml")
        assert "name=qi" not in tools[-1].content.plain

        # 助手 markdown 进 transcript(分派行由 test_dispatch_line_is_pi_style 覆盖)
        assert assistants

        # footer:第一行 cwd、第二行统计 + 右对齐模型(第三行只在有状态时出现)
        footer = app.footer_text.plain
        assert "↑12k ↓678" in footer
        assert "deepseek/deepseek-v4.1-flash • thinking off" in footer
        # P-E4c:分派模式取消后默认不再有 `qi · auto` 那行(同 pi 的空闲 footer)
        assert len(footer.split("\n")) == 2


# ── `/` 命令:对齐 pi 的部分 + “计划中”不冒充未知 ──────────


async def _command_notes(app, monkeypatch) -> list[tuple[str, str]]:
    """把 `_note` 换成收集器,便于断言命令输出。"""
    notes: list[tuple[str, str]] = []
    app._note = lambda text, tone="dim": notes.append((text, tone))  # type: ignore[method-assign]
    return notes


def _static_plain(widget: Static) -> str:
    """Static 当前渲染出来的纯文本 —— `render()` 的静态类型是 `ConsoleRenderable`,
    实际是 rich `Text` 时才有 `.plain`(与本文件其他 `getattr(x, "plain")` 同口径)。"""
    plain = getattr(widget.render(), "plain", None)
    return plain if isinstance(plain, str) else str(widget.render())


@pytest.mark.asyncio
async def test_tui_command_surface(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = _fake_cfg()          # /model 需要有可选模型
        notes = await _command_notes(app, monkeypatch)

        app._command("/help")               # pi 没有 /help:命令靠 `/` 补全被发现
        assert "未知命令" in notes[-1][0]

        app._command("/hotkeys")
        assert "ctrl+o" in notes[-1][0] and "尚未对齐" in notes[-1][0]

        app._command("/session")
        assert "会话: " in notes[-1][0] and "deepseek/deepseek-v4.1-flash" in notes[-1][0]

        assert app._rt is not None
        app._rt.cwd = Path("/tmp/short")          # tmp_path 太长会被 footer 截断(pi 同款)
        app._command("/name 我的会话")
        assert "我的会话" in notes[-1][0]
        assert app._session is not None and app._session.title == "我的会话"
        assert "我的会话" in app.footer_text.plain      # 名字进 footer(对齐 pi)

        app._command("/copy")                # 还没有回答 → 底部状态行提示,不进 transcript
        assert app._status == "还没有回答可复制"

        app._command("/model")               # 无参 = 开选择器(pi 的 `showModelSelector`)
        await pilot.pause(0.1)
        assert isinstance(app.screen, tui_mod.ModelSelector)
        await pilot.press("escape")
        await pilot.pause(0.1)

        app._command("/thinking")            # 无参 = 开选择器(pi 的 `showThinkingSelector`)
        await pilot.pause(0.1)
        assert isinstance(app.screen, tui_mod.ThinkingSelector)
        await pilot.press("escape")
        await pilot.pause(0.1)

        app._command("/scoped-models")       # 已实现:弹出 Ctrl+P 轮换清单
        await pilot.pause(0.1)
        assert isinstance(app.screen, tui_mod.ScopedModelsSelector)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.ScopedModelsSelector)

        app._command("/changelog")
        assert "CHANGELOG" in notes[-1][0]

        # 还没落盘的会话(懒建:一句话都没聊)没有文件可导出 —— 要说得清楚,
        # 而不是抛一个 `[Errno 2] No such file`(用户看不到所以然)
        app._command("/export")
        assert "还没落盘" in notes[-1][0]

        # 真聊一轮(落盘)之后再导出就正常了
        app._session_store().append(app._session, {
            "type": "message", "role": "assistant", "content": "答"})
        app._command("/export")
        assert "已导出" in notes[-1][0]
        assert Path(notes[-1][0].split("→ ")[1].strip()).is_file()

        app._command("/import /nonexistent.jsonl")
        assert "不存在" in notes[-1][0]

        app._command("/reload")
        assert "已重载" in notes[-1][0]

        app._command("/不存在")
        assert "未知命令" in notes[-1][0]


@pytest.mark.asyncio
async def test_tui_import_session_and_copy_answer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    sid = "deadbeef1234"
    source = tmp_path / "incoming.jsonl"
    source.write_text(
        json.dumps({"type": "session", "id": sid, "title": "imported",
                    "created_at": "2024-01-01T00:00:00"}) + "\n"
        + json.dumps({"type": "message", "role": "user", "content": "hi"}) + "\n",
        encoding="utf-8",
    )

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        notes = await _command_notes(app, monkeypatch)

        app._command(f"/import {source}")
        assert "已导入" in notes[-1][0]
        assert app._session is not None and app._session.id == sid
        assert app._session.message_count == 1

        app._command(f"/import {source}")     # 重复导入 → 明确提示,不静默覆盖
        assert "已存在" in notes[-1][0]

        app._command("/import")               # 缺参数
        assert "用法" in notes[-1][0]

        # 回答完成后 /copy 能拿到最后一条(FakeRuntime 的最后一条不带工具调用)
        app._submit("读一下 pyproject.toml")
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        assert "就是 qi" in app._last_answer
        app._command("/copy")
        assert app._status == "已复制最后一条回答"


# ── 键位对齐:escape / ctrl+x / ctrl+l / ctrl+p ────────────


def _fake_cfg():
    """两个 provider / 三个模型:用来验证模型选择与轮换顺序。"""
    from qi_agent.config import ModelEntry, ProviderConfig, QiConfig

    return QiConfig(providers={
        "alpha": ProviderConfig(api="openai-completions",
                                models=[ModelEntry(id="m1"), ModelEntry(id="m2")]),
        "beta": ProviderConfig(api="openai-completions", models=[ModelEntry(id="m3")]),
    })


async def _blocked_until(gate: asyncio.Event, abort) -> None:
    """等 gate;给了中断信号时,信号先到就提前返回(替身也要遵守协作式中断契约)。"""
    waiters = {asyncio.ensure_future(gate.wait())}
    if abort is not None:
        waiters.add(asyncio.ensure_future(abort.wait()))
    _done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task


class SlowRuntime(FakeRuntime):
    """文本先流式出来,然后卡住 —— 用来测 escape 中断。

    中断是**协作式**的,所以替身也得听信号:真实 QiRuntime 在流式途中被 abort 时会立刻收尾。
    没有信号才落到 5s 睡眠(旧行为,给不测中断的用例当“永远跑不完”用)。
    """

    async def stream(self, prompt, session, agent_override=None, abort=None):
        yield AgentEvent(kind="text_delta", agent="general", text="开始……")
        if abort is None:
            await asyncio.sleep(5)
        else:
            await asyncio.wait_for(abort.wait(), timeout=5)
        yield AgentEvent(kind="agent_end", agent="general", text="开始……",
                         data={"aborted": True})


class FailingRuntime(FakeRuntime):
    """回合一开就抛 —— 复现 AuthenticationError 把 TUI 带走的那条路径。"""

    async def stream(self, prompt, session, agent_override=None, abort=None):
        raise RuntimeError("AuthenticationError: Invalid 'Authorization' header or token")
        yield AgentEvent(kind="agent_end")          # pragma: no cover - 只为成为异步生成器


def _log_text(app) -> str:
    """把 transcript 里所有 Static 的纯文本拼起来(断言错误提示真的进了屏)。"""
    chunks: list[str] = []
    for widget in app.query_one("#log").children:
        content = getattr(widget, "content", None)
        plain = getattr(content, "plain", None)    # _note 传的是 rich.Text
        if plain:
            chunks.append(plain)
        elif isinstance(content, str):
            chunks.append(content)
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_turn_error_does_not_exit_tui(tmp_path, monkeypatch):
    """一轮失败只提示、不退出:异常不能再从 worker 抛到 Textual。

    回归:`litellm.AuthenticationError` 曾直接穿透 worker,Textual 把整个 TUI 关掉。
    """
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FailingRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app._submit("你好")
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)

        assert app._exit is False                 # 关键:界面没被异常带走
        assert app._working is False              # 收尾照做(spinner 停掉)
        shown = _log_text(app)
        assert "回合失败" in shown and "AuthenticationError" in shown


@pytest.mark.asyncio
async def test_unhandled_worker_error_does_not_exit_tui(tmp_path, monkeypatch):
    """兜底:新加的 worker 忘了 try/except 时,exit_on_error=False 也只提示不退出。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        notes: list[tuple[str, str]] = []
        app._note = lambda text, tone="dim": notes.append((text, tone))  # type: ignore[method-assign]

        async def boom() -> None:
            raise RuntimeError("worker 里的意外异常")

        app.run_worker(boom(), exit_on_error=False)
        await pilot.pause(0.2)
        assert app._exit is False
        assert any("后台任务失败" in text for text, _ in notes)


@pytest.mark.asyncio
async def test_escape_interrupts_running_turn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", SlowRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app._submit("你好")
        await pilot.pause(0.3)
        assert app._working is True and "Working" in app._top_border().plain
        await pilot.press("escape")            # pi 的 app.interrupt
        await pilot.pause(0.2)
        assert app._working is False
        assert "Working" not in app._top_border().plain
        assert app._status == "已中断"


@pytest.mark.asyncio
async def test_ctrl_x_copies_last_answer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    copied: list[str] = []
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.copy_to_clipboard = copied.append      # type: ignore[method-assign]
        await pilot.press("ctrl+x")                # 还没有回答
        await pilot.pause(0.05)
        assert app._status == "还没有回答可复制" and copied == []
        app._last_answer = "答案正文"
        await pilot.press("ctrl+x")                # pi 的 app.message.copy
        await pilot.pause(0.05)
        assert copied == ["答案正文"] and app._status == "已复制最后一条回答"


@pytest.mark.asyncio
async def test_model_keys_and_command(tmp_path, monkeypatch):
    """ctrl+l / ctrl+p / ctrl+shift+p / `/model` 都真的换掉 runtime 的模型。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = _fake_cfg()
        notes = await _command_notes(app, monkeypatch)

        app._command("/model alpha/m2")
        assert app._model is not None
        assert (app._model.provider, app._model.model) == ("alpha", "m2")
        # TUI 的职责是“用对的参数、对的来源去调 runtime”(source="set":显式切的)。
        # “换掉 llm_exec / 发 `model_select`” 是 **runtime 的行为**,由
        # `tests/test_extension_model.py` 用真 runtime 覆盖 —— 在 TUI 测试里断言它,
        # 就是在断言替身而不是断言 qi。
        # `cast(Any, …)`:测试里 `QiRuntime` 已被换成带记账字段的替身(静态类型看不到)。
        rt = cast(Any, app._rt)
        assert rt.model_switches[-1] == ("alpha", "m2", "set")
        assert "alpha/m2" in app.footer_text.plain

        app.action_cycle_model()                          # ctrl+p
        assert (app._model.provider, app._model.model) == ("beta", "m3")
        assert rt.model_switches[-1][2] == "cycle"         # 轮转的来源不同
        app.action_cycle_model()
        assert (app._model.provider, app._model.model) == ("alpha", "m1")   # 环绕
        app.action_cycle_model_back()                     # ctrl+shift+p
        assert (app._model.provider, app._model.model) == ("beta", "m3")

        app._command("/model m2")                         # 只给模型名也能唯一匹配
        assert (app._model.provider, app._model.model) == ("alpha", "m2")

        # `/model` 无参、ctrl+l 走**同一个选择器**(pi 的 `showModelSelector`)
        app._command("/model")
        await pilot.pause(0.1)
        selector = app.screen
        assert isinstance(selector, tui_mod.ModelSelector)
        shown = selector.rendered_text().plain
        assert "✓ m2" in shown                           # `✓ ` 标当前(pi 的模型选择器)
        assert "[alpha]" in shown and "[beta]" in shown  # provider 徽标(muted)
        await pilot.press("down")                         # → beta/m3
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert (app._model.provider, app._model.model) == ("beta", "m3")

        app.action_select_model()                         # ctrl+l
        await pilot.pause(0.1)
        assert isinstance(app.screen, tui_mod.ModelSelector)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.ModelSelector)
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.ModelSelector)


# ── 多行编辑器(对齐 pi pi-tui/components/editor.js)──────────


def test_editor_bindings_cover_pi_keys():
    from textual.binding import Binding

    keys = {b.key: b.action for b in Editor.BINDINGS if isinstance(b, Binding)}
    assert keys["enter"] == "submit"                     # tui.input.submit
    assert keys["shift+enter"] == "newline"              # tui.input.newLine
    assert keys["ctrl+j"] == "newline"
    assert keys["ctrl+b"] == "cursor_left" and keys["ctrl+f"] == "cursor_right"
    assert keys["alt+b"] == "cursor_word_left" and keys["alt+f"] == "cursor_word_right"
    assert keys["alt+left"] == "cursor_word_left" and keys["alt+right"] == "cursor_word_right"
    assert keys["alt+d"] == "delete_word_right"
    assert keys["ctrl+-"] == "undo"                      # pi 的 undo 键(不是 ctrl+z)


@pytest.mark.asyncio
async def test_editor_enter_submits_and_newline_keys_insert(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)
    PROMPTS.clear()

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = app.query_one("#editor", Editor)

        editor.load_text("第一行")
        editor.move_cursor((0, len("第一行")))        # load_text 后光标在 (0,0),移到行尾
        await pilot.press("shift+enter")            # pi 的换行键
        await pilot.press("ctrl+j")                 # 另一个换行键
        await pilot.pause(0.05)
        assert editor.text == "第一行\n\n"
        assert PROMPTS == []                        # 换行不提交

        editor.insert("第二行")
        await pilot.press("enter")                  # enter = 提交
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        assert PROMPTS == ["第一行\n\n第二行"]        # 多行原文完整送出
        assert editor.text == ""                     # 提交后清空
        assert app.footer_text.plain                # footer 仍在(布局没被撑坏)


@pytest.mark.asyncio
async def test_editor_grows_and_transcript_yields_space(tmp_path, monkeypatch):
    """编辑器长高 → transcript 上限下降(否则 inline 区域会把输入框挤掉)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE, tui_mode="regular")      # 高度算术只在 regular(inline) 里用
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause(0.1)
        editor = app.query_one("#editor", Editor)

        editor.load_text("一行")
        await pilot.pause(0.05)
        assert app._editor_rows(editor) == 1
        assert editor.size.height == 1                       # height: auto 真的按内容长

        editor.load_text("a\nb\nc")
        await pilot.pause(0.05)
        assert app._editor_rows(editor) == 3
        assert editor.size.height == 3

        editor.load_text("\n".join(str(i) for i in range(30)))   # 超长 → 封顶
        await pilot.pause(0.05)
        assert app._editor_rows(editor) == MAX_EDITOR_ROWS
        assert app.query_one("#log").styles.max_height is not None

        # 软换行也要算进去(单行很长时按终端宽估行数)
        editor.load_text("x" * 400)
        await pilot.pause(0.05)
        assert app._editor_rows(editor) > 1


@pytest.mark.asyncio
async def test_editor_word_nav_and_undo(tmp_path, monkeypatch):
    """ctrl+b/f、alt+b/f 词移动与 ctrl+- 撤销都要在这套编辑器里可用。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = app.query_one("#editor", Editor)

        editor.load_text("one two three")
        editor.move_cursor((0, 0))
        await pilot.press("ctrl+f", "ctrl+f", "ctrl+f")      # 前移 3 个字
        await pilot.pause(0.05)
        assert editor.cursor_location[1] == 3
        await pilot.press("alt+f")                            # 跳到下一个词尾
        await pilot.pause(0.05)
        assert editor.cursor_location[1] > 3

        editor.load_text("")
        await pilot.press("a", "b")
        await pilot.pause(0.05)
        assert editor.text == "ab"
        await pilot.press("ctrl+-")                           # pi 的 undo 键
        await pilot.pause(0.05)
        assert editor.text in ("", "a")


@pytest.mark.asyncio
async def test_footer_is_two_lines_at_idle_and_truncates(tmp_path, monkeypatch):
    """footer 空闲时**只有 2 行**(cwd / 统计+模型):第三行(状态)没内容就不占行。

    超长 cwd / 模型名要被截断,不能折行(折行会挤掉输入框)。"""
    import dataclasses

    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cwd = Path("/tmp/" + "very-long-directory-name/" * 4)
        app._model = dataclasses.replace(MODEL, provider="a-very-long-provider-name",
                                         model="a-very-long-model-name")
        assert app._session is not None
        app._session.title = "一个挺长的会话名字"
        app._refresh_footer()

        lines = app.footer_text.plain.split("\n")
        assert len(lines) == 2                      # 不折行、也不多一行空的
        assert all(len(line) <= 60 for line in lines)


@pytest.mark.asyncio
async def test_footer_status_line_only_when_it_has_content(tmp_path, monkeypatch):
    """第三行是**有条件**的(同 pi:第三行只放扩展状态/排队),空时不出现。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause(0.1)
        assert len(app.footer_text.plain.split("\n")) == 2

        app.set_extension_status("lsp", "LSP 就绪")      # ctx.ui.set_status
        lines = app.footer_text.plain.split("\n")
        assert len(lines) == 3 and lines[2] == "LSP 就绪"

        # 多行状态要被压成一行(否则 footer 撑到 4 行,预留行数失真)
        app._flash("第一行\n第二行")
        lines = app.footer_text.plain.split("\n")
        assert len(lines) == 3 and lines[2] == "第一行 第二行"
        app._restore_status()

        app.set_extension_status("lsp", None)           # 清掉 → 行也收回去
        assert len(app.footer_text.plain.split("\n")) == 2


@pytest.mark.asyncio
async def test_empty_extension_widget_slots_take_no_space(tmp_path, monkeypatch):
    """扩展挂件的两个槽空着时**不占行**(否则输入框与 footer 之间会空一大块)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause(0.1)
        above = app.query_one("#ext-widgets-above")
        below = app.query_one("#ext-widgets-below")
        assert above.region.height == 0 and below.region.height == 0
        # 编辑器下边框与 footer 上下相邻(中间不允许再插空行)
        assert app.query_one("#border-bottom").region.bottom == app.query_one("#footer").region.y


# ── 补全(`/` 命令 与 `@` 文件)──────────────────────────


async def _editor_with(app, pilot, text: str):
    editor = app.query_one("#editor", Editor)
    editor.load_text(text)
    editor.move_cursor(app._offset_to_location(text, len(text)))
    await pilot.pause(0.05)
    return editor


@pytest.mark.asyncio
async def test_completion_candidates_commands_and_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha.py").write_text("x")
    (tmp_path / "beta.txt").write_text("x")
    (tmp_path / "subdir").mkdir()
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)
    monkeypatch.setattr(tui_mod, "_which_fd", lambda: None)   # 锁住「没装 fd」回退路径

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)

        await _editor_with(app, pilot, "/se")            # 命令补全
        values = [c.value for c in app._completion_candidates()[0]]
        assert "/session" in values
        assert "/help" not in values          # pi 没有这个命令
        assert app._completions_open is True

        await _editor_with(app, pilot, "讲一下 @al")     # 文件补全
        values = [c.value for c in app._completion_candidates()[0]]
        assert values == ["@alpha.py"]

        await _editor_with(app, pilot, "@")              # 目录优先、隐藏文件默认不列
        values = [c.value for c in app._completion_candidates()[0]]
        assert "@subdir/" in values and "@alpha.py" in values

        await _editor_with(app, pilot, "@=")             # `=` 也是分隔符,不再当 token 继续匹配
        assert app._completion_candidates()[0] == []

        await _editor_with(app, pilot, "普通文本")        # 没有触发词
        assert app._completion_candidates()[0] == []
        assert app._completions_open is False


@pytest.mark.asyncio
async def test_file_completion_fd_quotes_and_recursive(tmp_path, monkeypatch):
    """fd 全树搜索(可跨目录)+ 带空格路径自动补成对引号(pi 的 buildCompletionValue)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    fd = tmp_path / "fake-fd"
    fd.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "tail = sys.argv[-1]\n"
        "if '[' not in tail:\n"
        "    tail = ''\n"
        "if not tail:\n"
        "    print('sub/'); print('sub/dir with space/')\n"
        "    print('sub/dir with space/y.md'); print('a.py')\n"
        "elif tail.endswith('y'):\n"
        "    print('sub/dir with space/y.md')\n"
        "elif 'space' in tail:\n"
        "    print('sub/dir with space/')\n"
        "else:\n"
        "    print('sub/'); print('sub/dir with space/')\n",
        encoding="utf-8")
    fd.chmod(0o755)
    monkeypatch.setattr(tui_mod, "_which_fd", lambda: str(fd))

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)

        await _editor_with(app, pilot, "@")             # fd 结果也是目录优先
        cands = app._completion_candidates()[0]
        assert [c.value for c in cands][:2] == ["@sub/", '@"sub/dir with space/"']
        assert cands[1].label == "dir with space/"       # 嵌套项 label 只给末段
        assert cands[1].detail == "sub/dir with space"

        await _editor_with(app, pilot, "@sub/")          # 嵌套一层;带空格自动加引号
        assert [c.value for c in app._completion_candidates()[0]] == [
            "@sub/", '@"sub/dir with space/"']

        await _editor_with(app, pilot, '@"sub/dir with space/y')   # 引号内继续补
        cands = app._completion_candidates()[0]
        assert [c.value for c in cands] == ['@"sub/dir with space/y.md"']

        # 接受带空格的文件:引号闭合 + 补一个空格
        editor = await _editor_with(app, pilot, "@\"sub/dir with space/y")
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert editor.text == '@"sub/dir with space/y.md" '

        # 目录:不补空格,光标停在收尾引号前继续往下补
        editor = await _editor_with(app, pilot, '@"sub/dir with space/')
        assert [c.value for c in app._completion_candidates()[0]] == [
            '@"sub/dir with space/"']
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert editor.text == '@"sub/dir with space/"'
        assert editor.cursor_location == (0, len(editor.text) - 1)


@pytest.mark.asyncio
async def test_tab_applies_completion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha.py").write_text("x")
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)
    monkeypatch.setattr(tui_mod, "_which_fd", lambda: None)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)

        editor = await _editor_with(app, pilot, "/se")
        assert app._completions_open
        await pilot.press("down")                        # 面板里下移
        await pilot.press("tab")                         # tab = 接受补全
        await pilot.pause(0.05)
        # `/se` 的候选按名字排序是 /session、/settings(后者这轮从"计划中"升为真命令),
        # 所以 down 之后 tab 接受的是第二个
        assert editor.text == "/settings "
        assert app._completions_open is False

        editor = await _editor_with(app, pilot, "@al")
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert editor.text == "@alpha.py "               # 文件补完带一个空格(pi 同款)

        # 面板没开时 tab 仍然是缩进(pi 的 tab 是补全,但没候选时不该吃掉输入)
        editor = await _editor_with(app, pilot, "")
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert editor.text.startswith(" ")

        # escape 先关面板,不该被当成中断
        editor = await _editor_with(app, pilot, "/se")
        assert app._completions_open
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert app._completions_open is False
        assert app._status != "已中断"


@pytest.mark.asyncio
async def test_completion_argument_candidates(tmp_path, monkeypatch):
    """`/model` `/thinking` `/login` 的第一个参数有候选(pi 的 getArgumentCompletions)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = _fake_cfg()

        await _editor_with(app, pilot, "/thinking ")     # 级别全列
        values = [c.value for c in app._completion_candidates()[0]]
        assert values == list(tui_mod.THINKING_LEVELS)
        assert app._completions_open is True

        await _editor_with(app, pilot, "/thinking hi")   # 参数前缀过滤
        assert [c.value for c in app._completion_candidates()[0]] == ["high"]

        await _editor_with(app, pilot, "/model beta")    # 模型候选是 provider/model
        assert [c.value for c in app._completion_candidates()[0]] == ["beta/m3"]

        await _editor_with(app, pilot, "/model m2")      # 只给模型名也能补出 provider/model
        assert [c.value for c in app._completion_candidates()[0]] == ["alpha/m2"]

        await _editor_with(app, pilot, "/login ")        # provider 候选
        assert [c.value for c in app._completion_candidates()[0]] == ["alpha", "beta"]

        # tab 写回的是参数本身,既不补尾随空格也不当作命令
        editor = await _editor_with(app, pilot, "/model beta")
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert editor.text == "/model beta/m3"
        assert app._completions_open is False


@pytest.mark.asyncio
async def test_completion_panel_matches_pi_style(tmp_path, monkeypatch):
    """补全面板 = pi 的 SelectList 版式:在输入框**下面**、无底色、`→ ` 前缀 + muted 说明。

    pi 的 `editor.js` 把 SelectList 画在 `renderBottomBorder()` 之后 —— 也就是下边框之下;
    项内格式来自 `select-list.js`:`→ `/`  ` 前缀 + 标签列对齐 + `muted` 说明,
    选中行用 `accent`(selectedText),**整行没有背景色**。
    """
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        # DOM 顺序:下边框之后才是面板(即输入框下方),不是上方
        body = [child.id for child in app.query_one("#body").children]
        assert body.index("completions") == body.index("border-bottom") + 1
        assert body.index("completions") > body.index("editor")

        await _editor_with(app, pilot, "/se")
        panel = app.query_one("#completions", tui_mod.CompletionPanel)
        assert "visible" in panel.classes
        assert panel.styles.background.is_transparent        # pi 没有底色
        # 屏幕位置:紧贴在编辑器下边框之下(即输入框**下方**),且在 footer 之上
        assert panel.region.y == app.query_one("#border-bottom").region.bottom
        assert panel.region.y < app.query_one("#footer").region.y

        # 渲染:首行是选中项(`→ ` + accent),说明是 muted;没有 `[...]` 标签前缀
        text = panel.rendered_text()
        first = text.plain.split("\n")[0]
        assert first.startswith("→ /se")
        assert "[" not in first
        # 选中行的「前缀 + 标签」用 accent(pi 的 selectedText),说明用 muted
        accent = _span_style(text, 0)
        assert accent.color is not None
        assert accent.color.get_truecolor().hex == PALETTE.hex("accent")
        muted_hex = PALETTE.hex("muted")
        assert any(
            (style := _span_style(text, index)).color is not None
            and style.color.get_truecolor().hex == muted_hex
            for index in range(len(text.spans))
        )

        # 带来源的候选:标签进**说明**(pi 的 `[p] 说明`),不是标签前缀
        app._completion_candidates = (  # type: ignore[method-assign]
            lambda: ([tui_mod.Candidate("/tpl", "tpl", "项目模板", "p")], 0, 0))
        app._refresh_completions()
        shown = panel.rendered_text().plain
        assert "tpl" in shown and "[p] 项目模板" in shown
        assert "[p] tpl" not in shown


# ── 输入历史(pi 的 editor.addToHistory / navigateHistory)──


@pytest.mark.asyncio
async def test_editor_history_up_down_keeps_draft(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = app.query_one("#editor", Editor)
        editor.add_to_history("第一条")
        editor.add_to_history("第二条")
        editor.add_to_history("第二条")                   # 连续重复不入
        assert editor._history == ["第二条", "第一条"]

        editor.load_text("草稿")
        editor.move_cursor((0, len("草稿")))
        await pilot.pause(0.05)
        await pilot.press("up")                          # 光标不在行首 → 只移动光标
        await pilot.pause(0.05)
        assert editor.text == "草稿"

        editor.move_cursor((0, 0))
        await pilot.press("up")
        await pilot.pause(0.05)
        assert editor.text == "第二条"
        await pilot.press("up")
        await pilot.pause(0.05)
        assert editor.text == "第一条"
        await pilot.press("down")
        await pilot.pause(0.05)
        assert editor.text == "第二条"
        await pilot.press("down")                        # 回到 -1 = 草稿
        await pilot.pause(0.05)
        assert editor.text == "草稿"
        assert editor._history_index == -1

        # 翻历史时手动改动 → 退出浏览(回草稿后光标在行尾,先回到行首再 ↑)
        editor.move_cursor((0, 0))
        await pilot.press("up")
        await pilot.pause(0.05)
        assert editor.text == "第二条"
        editor.insert("X")
        await pilot.pause(0.05)
        assert editor._history_index == -1
        assert editor.text == "X第二条"


@pytest.mark.asyncio
async def test_editor_history_records_chat_but_not_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = app.query_one("#editor", Editor)

        editor.load_text("你好")
        await pilot.press("enter")                        # 对话 → 进历史
        await pilot.pause(0.2)
        assert editor._history[0] == "你好"

        editor.load_text("/help")                         # 内置命令 → 不进历史
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert editor._history == ["你好"]


# ── kill-ring(pi 的 ctrl+y / alt+y)─────────────────────


def test_kill_ring_push_prepend_accumulate_rotate():
    ring = tui_mod.KillRing()
    assert ring.peek() is None and len(ring) == 0
    ring.push("two", prepend=True, accumulate=False)
    ring.push("one ", prepend=True, accumulate=True)      # 连续 kill → 合并到前面
    assert ring.peek() == "one two" and len(ring) == 1
    ring.push("", prepend=False, accumulate=False)        # 空文本不入环
    assert len(ring) == 1
    ring.push("tail", prepend=False, accumulate=False)
    assert ring.peek() == "tail"
    ring.rotate()                                         # yank-pop 轮换
    assert ring.peek() == "one two"
    ring.rotate()
    assert ring.peek() == "tail"                          # 两条时来回转


@pytest.mark.asyncio
async def test_kill_ring_yank_pop_and_accumulate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _editor_with(app, pilot, "one two")

        await pilot.press("ctrl+w")                      # kill "two"
        await pilot.pause(0.05)
        assert editor.text == "one "
        await pilot.press("ctrl+w")                      # 连续 kill → 与上一条合并
        await pilot.pause(0.05)
        assert editor.text == "" and len(editor._kill_ring) == 1
        await pilot.press("ctrl+y")                      # yank 回合并后的一整段
        await pilot.pause(0.05)
        assert editor.text == "one two"

        # 两次不连续的 kill → 两条;alt+y 在环里轮换
        editor._kill_ring = tui_mod.KillRing()
        editor.load_text("AAA BBB")
        editor.move_cursor((0, len("AAA BBB")))
        await pilot.press("ctrl+w")                      # kill "BBB"
        await pilot.pause(0.05)
        await pilot.press("ctrl+a")                      # 非 kill/yank 键 → 断开合并
        await pilot.pause(0.05)
        await pilot.press("ctrl+k")                      # kill 到行尾("AAA ")
        await pilot.pause(0.05)
        assert editor.text == "" and len(editor._kill_ring) == 2
        await pilot.press("ctrl+y")
        await pilot.pause(0.05)
        assert editor.text == "AAA "
        await pilot.press("alt+y")                       # 换成环里的上一条
        await pilot.pause(0.05)
        assert editor.text == "BBB"
        await pilot.press("alt+y")
        await pilot.pause(0.05)
        assert editor.text == "AAA "                     # 两条来回轮换

        # 中间接了别的键就不是 yank-pop 了
        await pilot.press("left")
        await pilot.pause(0.05)
        await pilot.press("alt+y")
        await pilot.pause(0.05)
        assert editor.text == "AAA "


# ── `!` / `!!` 手动 bash ───────────────────────────────


@pytest.mark.asyncio
async def test_bash_mode_runs_and_records_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._session is not None
        before = len(app._session.entries)

        app._submit("!echo hello-qi")                    # 单 !:进上下文
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        block = app._bash_blocks[-1]
        assert block.command == "echo hello-qi"
        assert "hello-qi" in block.rendered.plain
        assert "exit 0" in block.rendered.plain
        assert "不进上下文" not in block.rendered.plain
        added = app._session.entries[before:]
        assert len(added) == 1
        assert added[0]["role"] == "user"
        assert "[用户手动执行 bash]" in added[0]["content"]
        assert "hello-qi" in added[0]["content"]

        before = len(app._session.entries)
        app._submit("!!echo quiet")                      # 双 !:不进上下文
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        block = app._bash_blocks[-1]
        assert block.command == "echo quiet"
        assert "不进上下文" in block.rendered.plain
        assert len(app._session.entries) == before        # 没有新增消息

        notes: list[tuple[str, str]] = []
        app._note = lambda text, tone="dim": notes.append((text, tone))  # type: ignore[method-assign]
        app._submit("!")                                  # 空命令:给用法,不起 worker
        await pilot.pause(0.05)
        assert "用法: !<命令>" in notes[-1][0]
        assert len(app._bash_blocks) == 2                  # 没有多出第三个块


@pytest.mark.asyncio
async def test_editor_border_color_tracks_bash_mode(tmp_path, monkeypatch):
    """`!` / `!!` 前缀把编辑器边框换成 bashMode / dim(pi 的 updateEditorBorderColor)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = app.query_one("#editor", Editor)

        assert app._editor_color_key() == "border"
        editor.load_text("!"); await pilot.pause(0.05)
        assert app._editor_color_key() == "bashMode"
        editor.load_text("!!"); await pilot.pause(0.05)
        assert app._editor_color_key() == "dim"
        editor.load_text(""); await pilot.pause(0.05)
        assert app._editor_color_key() == "border"

        # 边框真的按这个色画(bashMode = #b5bd68)
        editor.load_text("!ls"); await pilot.pause(0.05)
        from textual.widgets import Static

        from rich.style import Style

        base = app._top_border().style
        if not isinstance(base, Style):
            base = Style.parse(base or "")
        color = base.color
        assert color is not None
        assert color.get_truecolor().hex == PALETTE.hex("bashMode")


# ── 消息队列(pi 的 steer / follow-up)──────────────────


class BlockingRuntime(FakeRuntime):
    """回合卡在 gate 上 —— 让"回合进行中"的断言完全可控(不靠 sleep 抢时间)。

    escape 走协作式中断,所以 gate 与信号赛跑:谁先到都算数。
    """

    gate: asyncio.Event | None = None

    async def stream(self, prompt, session, agent_override=None, abort=None):
        PROMPTS.append(prompt)
        if BlockingRuntime.gate is not None:
            await _blocked_until(BlockingRuntime.gate, abort)
        if abort is not None and abort.aborted:
            yield AgentEvent(kind="agent_end", agent="general", text="",
                             data={"aborted": True})
            return
        yield AgentEvent(kind="assistant_message", agent="general", text=f"echo:{prompt}",
                         data={"step": 1, "tool_calls": []})
        yield AgentEvent(kind="agent_end", agent="general", text="", data={"usage": {}})


async def _settle(app, pilot, rounds: int = 6) -> None:
    """等所有回合(含队列抽干后新起的那些)跑完。"""
    for _ in range(rounds):
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        if not app._working and not app._queue_count():
            return


async def _type_and_submit(app, pilot, text: str) -> None:
    editor = app.query_one("#editor", Editor)
    editor.load_text(text)
    editor.move_cursor((0, len(text)))
    await pilot.press("enter")


@pytest.mark.asyncio
async def test_enter_during_turn_queues_instead_of_racing(tmp_path, monkeypatch):
    """回合进行中按 enter:排队,结束后按顺序发出(不并发跑第二个回合)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", BlockingRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)
    PROMPTS.clear()
    BlockingRuntime.gate = asyncio.Event()

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app._submit("first")
        await pilot.pause(0.1)
        assert app._working is True

        await _type_and_submit(app, pilot, "second")   # 回合中的 enter
        await pilot.pause(0.05)
        assert app._pending_steer == ["second"]        # 排队,不是并发
        assert PROMPTS == ["first"]
        assert "排队 1" in app._status

        BlockingRuntime.gate.set()                     # 放行 → 抽队列
        await _settle(app, pilot)
        assert PROMPTS == ["first", "second"]          # 顺序发出
        assert app._queue_count() == 0
    BlockingRuntime.gate = None


@pytest.mark.asyncio
async def test_follow_up_queues_after_steer(tmp_path, monkeypatch):
    """alt+enter = follow-up;排序在 steer 之后。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", BlockingRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)
    PROMPTS.clear()
    BlockingRuntime.gate = asyncio.Event()

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app._submit("first")
        await pilot.pause(0.1)

        await _type_and_submit(app, pilot, "steer-1")
        editor = app.query_one("#editor", Editor)
        editor.load_text("follow-1")
        editor.move_cursor((0, len("follow-1")))
        await pilot.press("alt+enter")                 # pi 的 app.message.followUp
        await pilot.pause(0.05)
        assert app._pending_steer == ["steer-1"]
        assert app._pending_follow == ["follow-1"]
        assert editor.text == ""                       # 两种都清空编辑器

        BlockingRuntime.gate.set()
        await _settle(app, pilot)
        assert PROMPTS == ["first", "steer-1", "follow-1"]
    BlockingRuntime.gate = None


@pytest.mark.asyncio
async def test_dequeue_and_interrupt_return_queue(tmp_path, monkeypatch):
    """alt+up 取回排队;escape 中断时也退回编辑器。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", BlockingRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)
    PROMPTS.clear()
    BlockingRuntime.gate = asyncio.Event()

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = app.query_one("#editor", Editor)
        app._submit("first")
        await pilot.pause(0.1)
        assert app._working is True

        await _type_and_submit(app, pilot, "queued-text")
        assert app._pending_steer == ["queued-text"]

        await pilot.press("alt+up")                    # pi 的 app.message.dequeue
        await pilot.pause(0.05)
        assert app._pending_steer == []
        assert editor.text == "queued-text"            # 回到编辑器

        await _type_and_submit(app, pilot, "pending-2")
        assert app._pending_steer == ["pending-2"]
        await pilot.press("escape")                    # 中断 + 退回排队
        await pilot.pause(0.1)
        assert app._working is False
        assert app._pending_steer == []
        assert "pending-2" in editor.text

        # /new 清空队列
        app._enqueue("to-be-dropped", "steer")
        app._command("/new")
        await pilot.pause(0.05)
        assert app._queue_count() == 0
    BlockingRuntime.gate = None


# ── 会话树 / fork / clone(pi 的 /tree /fork /clone)──────────


def _seed_branch(app) -> list[str]:
    """在 app 的当前会话里铺一条 Q1/A1/Q2 的分支,返回各 entry id。"""
    store = app._session_store()
    session = app._session
    assert session is not None
    store.append(session, {"type": "message", "role": "user", "content": "Q1"})
    store.append(session, {"type": "message", "role": "assistant", "content": "A1"})
    store.append(session, {"type": "message", "role": "user", "content": "Q2"})
    app._replay_branch(session)
    ids = [str(e.get("id")) for e in session.tree_entries]
    return ids


# ── 双击 escape(pi 的 settings.doubleEscapeAction)─────────


@pytest.mark.asyncio
async def test_double_escape_default_opens_tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        _seed_branch(app)
        assert app.query_one("#editor", Editor).text == ""

        await pilot.press("escape")                       # 第一次:只记时间
        await pilot.pause(0.05)
        assert not isinstance(app.screen, tui_mod.PickerScreen)

        await pilot.press("escape")                       # 窗口内第二次 → /tree
        await pilot.pause(0.1)
        assert isinstance(app.screen, tui_mod.TreeSelector)
        assert "会话树" in app.screen._hint()


@pytest.mark.asyncio
async def test_double_escape_action_fork_and_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        _seed_branch(app)
        assert app._rt is not None

        app._rt.settings = QiSettings(doubleEscapeAction="none")
        app._last_escape = time.monotonic()
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.PickerScreen)

        app._rt.settings = QiSettings(doubleEscapeAction="fork")
        app._last_escape = time.monotonic()
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(app.screen, tui_mod.PickerScreen)   # /fork 的用户消息选择器
        assert "fork" in app.screen._title

        # 编辑器非空时不触发(escape 只当普通中断)
        await pilot.press("escape")                       # 关掉选择器
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.PickerScreen)
        editor = app.query_one("#editor", Editor)
        editor.load_text("还没发出去")
        editor.move_cursor((0, 0))
        app._last_escape = time.monotonic()
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.PickerScreen)
        assert editor.text == "还没发出去"


@pytest.mark.asyncio
async def test_tree_lists_tree_and_jumps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        ids = _seed_branch(app)
        session = app._session
        assert session is not None

        app._command("/tree")
        await pilot.pause(0.1)
        selector = app.screen
        assert isinstance(selector, tui_mod.TreeSelector)
        rows = selector._rows()                           # (id, text, label)
        assert [row.id for row in rows] == ids
        assert "●" in rows[-1].text                       # 当前节点
        assert rows[0].text.strip().startswith("│")        # 当前分支
        await pilot.press("escape")
        await pilot.pause(0.05)

        app._jump_to(ids[0])                              # 跳到第一条
        await pilot.pause(0.05)
        assert session.current == ids[0]
        assert session.message_count == 1
        shown = [w.source for w in app.query_one("#log").children
                 if isinstance(w, UserMessage)]
        assert shown == ["Q1"]                            # transcript 换成该分支
        assert "已跳到节点" in app._status

        # 在旧节点继续 → 新分支;旧分支仍在文件里
        # (FakeRuntime 不落盘,所以这里直接用 store 追加,等价于 runtime 在 current 下 append)
        app._session_store().append(
            session, {"type": "message", "role": "user", "content": "Q1-另一问"})
        assert [e.get("content") for e in session.branch()] == ["Q1", "Q1-另一问"]
        assert session.branch_points == 1
        assert session.message_count_of(ids[2]) == 3      # Q1/A1/Q2 那条仍然可回溯


@pytest.mark.asyncio
async def test_fork_creates_new_session_and_prefills_editor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        _seed_branch(app)
        original = app._session
        assert original is not None

        notes: list[tuple[str, str]] = []
        app._note = lambda text, tone="dim": notes.append((text, tone))  # type: ignore[method-assign]

        app._command("/fork 2")                           # 第 2 条用户消息 = Q2
        await pilot.pause(0.1)
        forked = app._session
        assert forked is not None and forked.id != original.id
        assert [e.get("content") for e in forked.branch()] == ["Q1", "A1"]  # 不含被 fork 的那条
        assert app.query_one("#editor", Editor).text == "Q2"               # 放回编辑器
        assert any("已 fork" in text for text, _ in notes)
        # 原会话不受影响
        assert [e.get("content") for e in original.branch()] == ["Q1", "A1", "Q2"]

        # 无参 → 弹选择器,列出**当前(已 fork 的)会话**分支上的用户消息
        app._command("/fork")
        await pilot.pause(0.1)
        assert isinstance(app.screen, tui_mod.PickerScreen)
        assert [label for _, label in app.screen._options] == ["你: Q1"]
        await pilot.press("escape")
        await pilot.pause(0.05)

        app._command("/fork 99")                          # 越界 → 明确提示,不静默
        assert any("找不到那条用户消息" in text for text, _ in notes)


def test_fork_numeric_arg_is_index_not_id_prefix():
    """`/fork 2` 必须稳定指向第 2 条用户消息:entry id 是随机串,以前会让
    「id 以 2 开头」的第 1 条抢走 —— 按运行随机失败。"""
    app = QiTui(palette=PALETTE)
    session = object()
    app._user_message_options = lambda _session: [      # type: ignore[method-assign]
        ("2abc-def", "你: Q1"), ("f00d", "你: Q2"),
    ]
    assert app._resolve_user_message(session, "2") == "f00d"    # 序号优先
    assert app._resolve_user_message(session, "1") == "2abc-def"
    assert app._resolve_user_message(session, "f0") == "f00d"   # 非纯数字才当 id 前缀
    assert app._resolve_user_message(session, "9") is None


@pytest.mark.asyncio
async def test_clone_copies_branch_and_is_independent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        _seed_branch(app)
        original = app._session
        assert original is not None

        notes: list[tuple[str, str]] = []
        app._note = lambda text, tone="dim": notes.append((text, tone))  # type: ignore[method-assign]

        app._command("/clone 副本")
        await pilot.pause(0.1)
        cloned = app._session
        assert cloned is not None and cloned.id != original.id
        assert cloned.title == "副本"
        assert [e.get("content") for e in cloned.branch()] == ["Q1", "A1", "Q2"]
        assert cloned.path != original.path
        assert any("已 clone" in text for text, _ in notes)

        # 往副本里加东西不影响原件
        app._session_store().append(cloned, {"type": "message", "role": "user", "content": "只副本"})
        assert original.message_count == 3 and cloned.message_count == 4


@pytest.mark.asyncio
async def test_resume_replays_branch_and_session_shows_tree_info(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        _seed_branch(app)
        other = app._session
        assert other is not None
        store = app._session_store()

        fresh = store.create("另一个", cwd=tmp_path)
        store.append(fresh, {"type": "message", "role": "user", "content": "别的会话"})
        app._switch_session(fresh)
        await pilot.pause(0.05)
        assert [w.source for w in app.query_one("#log").children
                if isinstance(w, UserMessage)] == ["别的会话"]

        app._command(f"/resume {other.id}")
        await pilot.pause(0.1)
        assert app._session is not None and app._session.id == other.id
        assert [w.source for w in app.query_one("#log").children
                if isinstance(w, UserMessage)] == ["Q1", "Q2"]   # 回放当前分支

        notes: list[tuple[str, str]] = []
        app._note = lambda text, tone="dim": notes.append((text, tone))  # type: ignore[method-assign]
        app._command("/session")
        assert "条(当前分支)" in notes[-1][0] and "个分支点" in notes[-1][0]


# ── 回合中的命令立即执行 / TUI 会话选择参数 ────────────────


@pytest.mark.asyncio
async def test_slash_command_runs_immediately_during_turn(tmp_path, monkeypatch):
    """回合进行中的 `/x` 与 `!x` 必须立即执行 —— 否则 `/quit` 会被推到回合结束。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", BlockingRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)
    PROMPTS.clear()
    BlockingRuntime.gate = asyncio.Event()

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        notes: list[str] = []
        app._note = lambda text, tone="dim": notes.append(text)   # type: ignore[method-assign]

        app._submit("first")
        await pilot.pause(0.1)
        assert app._working is True

        await _type_and_submit(app, pilot, "/hotkeys")
        await pilot.pause(0.05)
        assert any("ctrl+o" in text for text in notes)      # 命令真的渲染了
        assert app._pending_steer == []                     # 没有被当成对话排队

        await _type_and_submit(app, pilot, "/thinking high")
        await pilot.pause(0.05)
        assert app._thinking_level == "high"                # 命令真的生效

        BlockingRuntime.gate.set()
        await _settle(app, pilot)
    BlockingRuntime.gate = None


@pytest.mark.asyncio
async def test_tui_session_selection_flags(tmp_path, monkeypatch):
    """`qi -c` / `--session` / `--fork` / `-n`:进 TUI 也要生效(以前被无视)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    store = SessionStore()
    history = store.create("历史会话", cwd=tmp_path)
    store.append(history, {"type": "message", "role": "user", "content": "历史问题"})
    store.append(history, {"type": "message", "role": "assistant", "content": "历史回答"})

    def shown(app) -> list[str]:
        return [w.source for w in app.query_one("#log").children
                if isinstance(w, UserMessage)]

    # -c:续最近一个会话,并把它的分支回放出来
    app = QiTui(palette=PALETTE, cont=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._session is not None and app._session.id == history.id
        assert shown(app) == ["历史问题"]

    # --session <id>:指定会话
    app = QiTui(palette=PALETTE, session_id=history.id)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._session is not None and app._session.id == history.id

    # --session 指向不存在的 id:新建 + 明确提示(不静默)
    app = QiTui(palette=PALETTE, session_id="nope")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._session is not None and app._session.id != "nope"
        assert "会话不存在" in app._startup_note

    # --fork <id>:复制该会话的分支到新会话
    app = QiTui(palette=PALETTE, fork_id=history.id)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._session is not None and app._session.id != history.id
        assert [e.get("content") for e in app._session.branch()] == ["历史问题", "历史回答"]
        assert "已从" in app._startup_note

    # -n <名字>:新会话的标题
    app = QiTui(palette=PALETTE, name="我的名字")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._session is not None and app._session.title == "我的名字"
        assert app._rt is not None
        app._rt.cwd = Path("/tmp/short")      # tmp_path 太长会被 footer 截断(pi 同款)
        app._refresh_footer()
        assert "我的名字" in app.footer_text.plain

    # --no-session:临时会话(**不落盘**,名字明确)
    app = QiTui(palette=PALETTE, no_session=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._session is not None and app._session.title == "ephemeral"
        assert app._session.ephemeral is True, "--no-session 必须是内存会话"


# ── 懒建:没说话就不在磁盘上留东西 ──────────────────────────
#
# 开发机上真实事故:`~/.qi/agent/sessions/` 攒了 3000+ 个空会话。TUI 这条路径的成因是
# "进界面就落一个文件"(`_pick_session` 里无条件的 `store.create()`)。这组测试钉住新语义:
# 会话**对象**照常有(否则下游到处要判 None),**文件**推迟到第一条助手回答。

@pytest.mark.asyncio
async def test_bare_start_writes_no_file_until_first_answer(tmp_path, monkeypatch):
    """裸 `qi`:会话对象在,**文件**推迟到第一条助手回答 —— 看一眼前就走不留空会话。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        store = app._session_store()
        # 会话对象在(pi 的 SessionManager 构造时就 newSession(),不变量同形)
        assert app._session is not None
        assert app._session.unflushed is True
        # 但磁盘上什么都没有:进来看一眼就走 → 不留垃圾
        assert store.list() == []
        assert not app._session.path.exists()
        # 甚至 `/session` 这类命令也照常工作(会话对象在,只是没文件)
        assert not app._session.message_count


@pytest.mark.asyncio
async def test_no_session_writes_nothing_even_after_chatting(tmp_path, monkeypatch):
    """`--no-session`:聊完一整轮也**一个文件都不留**(以前它照样落盘)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE, no_session=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        await _type_and_submit(app, pilot, "你好")
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        assert app._session is not None and app._session.ephemeral is True
        assert list(app._session_store().root.glob("*.jsonl")) == []


@pytest.mark.asyncio
async def test_new_session_command_does_not_pile_up_empty_files(tmp_path, monkeypatch):
    """连点 `/new` 不堆空会话(web 端 §18.28 的同一条结论;pi 的 `/new` 也不落文件)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        store = app._session_store()
        for _ in range(3):
            app._command("/new")
            await app.workers.wait_for_complete()
            await pilot.pause(0.05)
        assert store.list() == [], "/new 不落文件"
        assert app._session is not None and app._session.unflushed is True


# ── 压缩(pi 的 /compact)────────────────────────────────


@pytest.mark.asyncio
async def test_compact_command_and_block(tmp_path, monkeypatch):
    """`/compact` 走 worker 并渲染成 pi 同款底色块;ctrl+o 可展开。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None

        app._command("/compact")                       # FakeRuntime 返回 None
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        assert "没有可压缩的内容" in app._status

        # 真的压一次(直接塞 entry,验证渲染与 ctrl+o)
        entry = {"type": "compaction", "summary": "## Goal\n做完某件事",
                 "firstKeptEntryId": None, "tokensBefore": 12345,
                 "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
        app._append_compaction(entry)
        await pilot.pause(0.05)
        blocks = app._compaction_blocks
        assert len(blocks) == 1
        assert "12,345" in blocks[0].body_plain and "ctrl+o" in blocks[0].body_plain
        assert blocks[0].styles.background.hex.lower() == PALETTE.hex("customMessageBg")
        assert "7" in app.footer_text.plain or "↑" in app.footer_text.plain

        app.action_toggle_expand()                     # ctrl+o → 展开摘要正文
        await pilot.pause(0.05)
        assert blocks[0]._expanded is True
        assert "Goal" in blocks[0].body_plain

        # 真正落盘后回放分支时,压缩块也要从 entry 重建出来
        assert app._session is not None
        app._session_store().append(app._session, dict(entry))
        app._replay_branch(app._session)
        await pilot.pause(0.05)
        assert len(app._compaction_blocks) == 1
        assert "Goal" in app._compaction_blocks[0].body_plain or             "Goal" in app._compaction_blocks[0].summary


# ── settings 驱动的界面参数 ────────────────────────────────


@pytest.mark.asyncio
async def test_ui_settings_applied_to_widgets(tmp_path, monkeypatch):
    """hideThinkingBlock / editorPaddingX / outputPad / autocompleteMaxVisible 真的生效。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.settings = QiSettings(hideThinkingBlock=True, editorPaddingX=0,
                                      outputPad=0, autocompleteMaxVisible=3)
        app._apply_ui_settings()

        assert app._show_thinking is False                  # 思考块默认隐藏
        assert app._output_pad == 0                         # 助手消息左侧不缩进
        assert app._completion_rows == 3                    # 补全面板最多 3 行
        editor = app.query_one("#editor", Editor)
        assert editor.styles.padding.left == 0
        assert editor.styles.padding.top == 0               # 只动左右

        await _editor_with(app, pilot, "/")                 # 面板行数跟着设置走
        panel = app.query_one("#completions", tui_mod.CompletionPanel)
        assert panel._rows == 3                             # 最多 3 行候选项
        lines = panel.rendered_text().plain.split("\n")
        # pi 的 SelectList:`maxVisible` 个候选项之后再补一行 `(n/m)` 滚动提示
        assert len([ln for ln in lines if not ln.strip().startswith("(")]) == 3
        assert lines[-1].strip().startswith("(1/")
        candidates, _, _ = app._completion_candidates()
        assert len(candidates) > 3                          # 候选本身不裁,面板滚动


@pytest.mark.asyncio
async def test_quiet_startup_hides_banner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)

    def quiet_runtime(*args, **kwargs):
        runtime = FakeRuntime()
        runtime.settings = QiSettings(quietStartup=True)
        return runtime

    monkeypatch.setattr(tui_mod, "QiRuntime", quiet_runtime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._quiet_startup is True
        assert not app.query_one("#log").children           # 启动头(含 shortcuts 提示)没写


# ── 凭证(/login · /logout)──────────────────────────────


def _auth_path() -> Path:
    """当前环境下的 auth.json(测试的 QI_AGENT_HOME 是临时目录,不会碰真的 ~/.qi)。"""
    from qi_agent.auth import AuthStore

    return AuthStore().path


@pytest.mark.asyncio
async def test_login_picks_provider_collects_key_and_saves(tmp_path, monkeypatch):
    """`/login` 无参:provider 选择器 → 遮罩输入 → 写 auth.json;key **不进 transcript**。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = _fake_cfg()
        notes = await _command_notes(app, monkeypatch)

        app._command("/login")
        await pilot.pause(0.05)
        picker = app.screen
        assert isinstance(picker, tui_mod.PickerScreen)          # 先选 provider
        assert [value for value, _ in picker._options] == ["alpha", "beta"]
        assert any("(无凭证)" in label for _, label in picker._options)
        await pilot.press("enter")                                # 选 alpha
        await pilot.pause(0.05)

        assert isinstance(app.screen, tui_mod.PromptScreen)       # 再输 key
        field = app.screen.query_one("#prompt-input", Input)
        assert field.password is True                             # 遮罩(不进屏幕历史)
        field.value = "sk-tui-secret"
        await pilot.press("enter")
        await pilot.pause(0.05)

        assert _auth_path().is_file()
        assert _auth_path().stat().st_mode & 0o777 == 0o600      # 0600
        from qi_agent.auth import AuthStore
        assert AuthStore().get("alpha") == "sk-tui-secret"
        assert any("已保存 alpha" in text for text, _ in notes)
        assert not any("sk-tui-secret" in text for text, _ in notes)   # 只进 auth store
        assert app._model is not None                             # 有模型 → 不自动换
        assert any("当前模型不变" in text for text, _ in notes)


@pytest.mark.asyncio
async def test_login_with_argument_skips_the_picker(tmp_path, monkeypatch):
    """`/login <provider>` 直奔输入框(pi 的 `handleLoginCommand`)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = _fake_cfg()
        await _command_notes(app, monkeypatch)
        app._command("/login beta")
        await pilot.pause(0.05)
        assert isinstance(app.screen, tui_mod.PromptScreen)       # 没有选择器那一步
        app.screen.query_one("#prompt-input", Input).value = "sk-beta"
        await pilot.press("enter")
        await pilot.pause(0.05)
        from qi_agent.auth import AuthStore
        assert AuthStore().get("beta") == "sk-beta"


@pytest.mark.asyncio
async def test_login_adopts_a_model_when_none_is_usable(tmp_path, monkeypatch):
    """当前**没模型**时登录后顺带选中该 provider 的第一个(pi 只在 previousModel 未知时选)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = _fake_cfg()
        app._model = None                       # 模型没解析出来(pi 的 isUnknownModel)
        await _command_notes(app, monkeypatch)
        app._command("/login alpha")
        await pilot.pause(0.05)
        app.screen.query_one("#prompt-input", Input).value = "sk-a"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert app._model is not None and app._model.label == "alpha/m1"


@pytest.mark.asyncio
async def test_login_cancel_changes_nothing(tmp_path, monkeypatch):
    """escape 取消选择器(或空白 key) → 不写盘、不报错。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = _fake_cfg()
        notes = await _command_notes(app, monkeypatch)

        app._command("/login")
        await pilot.pause(0.05)
        await pilot.press("escape")              # 取消选择器
        await pilot.pause(0.05)
        assert any("已取消登录" in text for text, _ in notes)
        assert not _auth_path().is_file()        # 一个字节也没写

        app._command("/login alpha")
        await pilot.pause(0.05)
        await pilot.press("enter")               # 空 key → 当取消
        await pilot.pause(0.05)
        assert not _auth_path().is_file()


@pytest.mark.asyncio
async def test_logout_removes_credentials(tmp_path, monkeypatch):
    """`/logout` 无参给已存凭证选择器;`/logout <p>` 直接删。环境变量与 models.json 不受影响。"""
    from qi_agent.auth import AuthStore

    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)
    AuthStore().set_key("alpha", "sk-alpha")
    AuthStore().set_key("beta", "sk-beta")

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        notes = await _command_notes(app, monkeypatch)

        app._command("/logout")
        await pilot.pause(0.05)
        picker = app.screen
        assert isinstance(picker, tui_mod.PickerScreen)
        assert [value for value, _ in picker._options] == ["alpha", "beta"]
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert AuthStore().get("alpha") is None and AuthStore().get("beta") == "sk-beta"
        assert any("已删除 alpha" in text for text, _ in notes)

        app._command("/logout gamma")
        await pilot.pause(0.05)
        assert any("没有已存凭证" in text for text, _ in notes)    # 不存在时不静默


# ── /scoped-models(pi 的 Ctrl+P 轮换清单)─────────────────


@pytest.mark.asyncio
async def test_scoped_models_saves_and_limits_cycling(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = _fake_cfg()                        # alpha/m1,m2 + beta/m3
        assert app._cycle_labels() == ["alpha/m1", "alpha/m2", "beta/m3"]

        app._command("/scoped-models")                   # 命令不再提示「计划中」
        await pilot.pause(0.1)
        selector = app.screen
        assert isinstance(selector, tui_mod.ScopedModelsSelector)
        listing = selector.query_one("#scoped-list", tui_mod.SelectionList)
        assert listing.option_count == 3
        assert len(listing.selected) == 3                # 未配置 = 全选

        listing.deselect("alpha/m2")
        await pilot.press("ctrl+s")                      # 保存 → 只剩 m1/m3
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.ScopedModelsSelector)
        assert app._rt.settings.enabledModels == ["alpha/m1", "beta/m3"]
        assert app._cycle_labels() == ["alpha/m1", "beta/m3"]

        persisted = json.loads((tmp_path / "home" / "settings.json").read_text(encoding="utf-8"))
        assert persisted["enabledModels"] == ["alpha/m1", "beta/m3"]

        # ctrl+p 只在勾选的模型里转
        app._model = dataclasses.replace(MODEL, provider="alpha", model="m1")
        app.action_cycle_model()
        assert (app._model.provider, app._model.model) == ("beta", "m3")
        app.action_cycle_model()
        assert (app._model.provider, app._model.model) == ("alpha", "m1")

        # escape 取消:不改内存也不落盘
        app._command("/scoped-models")
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert app._rt.settings.enabledModels == ["alpha/m1", "beta/m3"]

        # 全选保存 → 回写空列表(空 = 不限,pi 同款)
        app._command("/scoped-models")
        await pilot.pause(0.1)
        await pilot.press("ctrl+a")
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert app._rt.settings.enabledModels == []
        assert app._cycle_labels() == ["alpha/m1", "alpha/m2", "beta/m3"]


@pytest.mark.asyncio
async def test_settings_panel_is_modal_and_saves_user_settings(tmp_path, monkeypatch):
    """`/settings` 必须在**跑着的主循环里**开成模态面板 —— 曾经是独立的小 App,
    `App.run()` 内部的 `asyncio.run()` 必然 `RuntimeError: asyncio.run() cannot be
    called from a running event loop`,命令一敲就报错。回归就锁这个 + 存/取消语义。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        notes = await _command_notes(app, monkeypatch)
        settings_file = tmp_path / "home" / "settings.json"

        app._command("/settings")                       # 曾在这里抛 RuntimeError
        await pilot.pause(0.1)
        panel = app.screen
        assert isinstance(panel, tui_mod.SettingsPanel)  # 主循环里的模态,不是新 App
        # 未设的 theme 显示**生效值** auto(SETTING_CHOICES 的第一行)
        assert panel.rendered_text().plain.splitlines()[0] == "→ theme = auto"

        # enter 换值:auto → dark(next_choice 绕回第一个)
        await pilot.press("enter")
        assert panel.rendered_text().plain.splitlines()[0] == "→ theme = dark"

        await pilot.press("ctrl+s")                     # 保存 → 关面板 + 写用户级 settings
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.SettingsPanel)
        assert app._rt.settings.theme == "dark"          # 同时应用到当前界面
        persisted = json.loads(settings_file.read_text(encoding="utf-8"))
        assert persisted["theme"] == "dark"
        assert any("已保存 theme = dark" in text for text, _ in notes)

        # 不动直接保存 → 「没有改动。」,盘上不动
        before = settings_file.read_text(encoding="utf-8")
        app._command("/settings")
        await pilot.pause(0.1)
        panel = app.screen
        assert isinstance(panel, tui_mod.SettingsPanel)
        assert panel.rendered_text().plain.splitlines()[0] == "→ theme = dark"  # 新值成了基线
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert any("没有改动" in text for text, _ in notes)
        assert settings_file.read_text(encoding="utf-8") == before

        # 改了但 escape 取消:内存与盘都不动
        app._command("/settings")
        await pilot.pause(0.1)
        await pilot.press("enter")                      # dark → light
        await pilot.press("space")                      # space 与 enter 同义:light → auto
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.SettingsPanel)
        assert app._rt.settings.theme == "dark"
        assert settings_file.read_text(encoding="utf-8") == before


# ── 会话选择器(/resume;对齐 pi 的 SessionSelectorComponent)──────


def test_session_search_syntax_tokens_phrases_and_regex():
    """过滤框的三态语法:`re:` 正则 / `"短语"` 精确 / 空格分词模糊(pi 的 parseSearchQuery)。"""
    plain = tui_mod.parse_search_query("abc def")
    assert plain["mode"] == "tokens" and plain["error"] is None
    assert [(t.kind, t.value) for t in plain["tokens"]] == [("fuzzy", "abc"), ("fuzzy", "def")]

    mixed = tui_mod.parse_search_query('foo "node cve" bar')
    assert [(t.kind, t.value) for t in mixed["tokens"]] == [
        ("fuzzy", "foo"), ("phrase", "node cve"), ("fuzzy", "bar")]

    # 引号没闭合 = 打字中间态 → 退化成空格分词,不报错
    broken = tui_mod.parse_search_query('foo "node cve')
    assert broken["error"] is None
    assert all(t.kind == "fuzzy" for t in broken["tokens"])

    regex = tui_mod.parse_search_query("re:^2026.*tui$")
    assert regex["mode"] == "regex" and regex["error"] is None
    assert tui_mod.parse_search_query("re:[unclosed")["error"]
    assert tui_mod.parse_search_query("re:")["error"]


def test_session_search_matching_is_fuzzy_phrase_and_regex():
    text = "9d291f93ac15 tui 梳理仓库结构 /Users/me/proj"
    assert tui_mod.match_session_text(text, tui_mod.parse_search_query("仓库")) is not None
    # 模糊:子序列也算命中("gt 结构" 这种跨词的也认)
    assert tui_mod.match_session_text(text, tui_mod.parse_search_query("9d2tui")) is not None
    assert tui_mod.match_session_text(text, tui_mod.parse_search_query("无关词")) is None
    # 短语必须连续
    assert tui_mod.match_session_text(text, tui_mod.parse_search_query('"仓库结构"')) is not None
    assert tui_mod.match_session_text(text, tui_mod.parse_search_query('"结构仓库"')) is None
    # 正则
    assert tui_mod.match_session_text(text, tui_mod.parse_search_query(r"re:^9d\w+ tui")) is not None
    assert tui_mod.match_session_text(text, tui_mod.parse_search_query("re:^zzz")) is None


def test_session_age_is_compact():
    now = 1_700_000_000.0
    assert tui_mod.format_age(now - 10, now) == "now"
    assert tui_mod.format_age(now - 300, now) == "5m"
    assert tui_mod.format_age(now - 3 * 3600, now) == "3h"
    assert tui_mod.format_age(now - 2 * 86400, now) == "2d"
    assert tui_mod.format_age(now - 10 * 86400, now) == "1w"
    assert tui_mod.format_age(now - 100 * 86400, now) == "3mo"
    assert tui_mod.format_age(now - 800 * 86400, now) == "2y"
    assert tui_mod.format_age(0, now) == "?"


@pytest.mark.asyncio
async def test_session_selector_lists_filters_and_acts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        store = app._session_store()
        # 选择器默认只看**当前目录**,所以测试里的会话要带上 cwd(真实会话都写了 cwd)
        zeta = store.create("zeta", cwd=tmp_path)
        alpha = store.create("alpha", cwd=tmp_path)
        unnamed = store.create("", cwd=tmp_path)
        other = store.create("别的目录", cwd=tmp_path / "elsewhere")   # 用于 tab 切范围
        here = str(tmp_path.resolve())
        order = [s.id for s in store.list() if s.cwd == here]

        app._command("/resume")
        await pilot.pause(0.1)
        selector = app.screen
        assert isinstance(selector, tui_mod.SessionSelector)
        # 选会话默认只看**当前目录**(pi 的 scope=current)
        assert [s.id for s in selector._visible()] == order

        # 列表是自绘的 Static —— 不再是 OptionList(那圈 tall 边框与整行高亮底就是没对齐 pi 的来源)
        listing = selector.query_one("#session-list", Static)
        assert listing.styles.background.hex == "#00000000"         # 无底色
        assert listing.styles.border_top[0] in (None, "")           # 无边框

        # 输入即过滤:`"短语"` 是连续子串(模糊匹配会命中 id/cwd 里的子序列,这里要精确)
        box = selector.query_one("#session-filter", tui_mod.Input)
        box.value = '"alpha"'
        await pilot.pause(0.05)
        assert [s.id for s in selector._visible()] == [alpha.id]
        box.value = "re:^" + alpha.id
        await pilot.pause(0.05)
        assert [s.id for s in selector._visible()] == [alpha.id]
        box.value = "re:["
        await pilot.pause(0.05)
        assert selector._visible() == []                     # 正则语法错:列表空 + 头部报错
        assert _static_plain(selector.query_one("#session-list", Static)).strip()
        box.value = ""
        await pilot.pause(0.05)

        # tab:当前目录 ↔ 全部(pi 的 scope 切换)
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert selector._scope == "all"
        assert other.id in [s.id for s in selector._visible()]    # 全部 = 也含别的目录
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert selector._scope == "current"
        assert other.id not in [s.id for s in selector._visible()]

        await pilot.press("ctrl+n")                      # 只看命名会话
        await pilot.pause(0.05)
        named = sorted(s.id for s in store.list() if tui_mod.has_title(s.title) and s.cwd == here)
        assert sorted(s.id for s in selector._visible()) == named
        await pilot.press("ctrl+n")
        await pilot.pause(0.05)
        assert len(selector._visible()) == len(order)

        # ctrl+s 三档循环:threaded → recent → fuzzy
        await pilot.press("ctrl+s")
        await pilot.pause(0.05)
        assert selector._sort == "recent"
        assert [s.id for s in selector._visible()] == order      # recent = 按最后活动时间倒序
        await pilot.press("ctrl+s")
        await pilot.pause(0.05)
        assert selector._sort == "fuzzy"
        await pilot.press("ctrl+s")
        await pilot.pause(0.05)
        assert selector._sort == "threaded"

        await pilot.press("ctrl+p")                      # 显示路径(太长就缩成 `…/段/段`)
        await pilot.pause(0.05)
        shown = _static_plain(selector.query_one("#session-list", Static))
        assert alpha.path.name in shown and "…/" in shown
        await pilot.press("ctrl+p")
        await pilot.pause(0.05)
        assert alpha.path.name not in _static_plain(selector.query_one("#session-list", Static))

        # ↑↓ 移动光标(自绘列表也要能手选)
        selector._index = 0
        await pilot.press("down")
        await pilot.pause(0.05)
        assert selector._index == 1
        await pilot.press("up")
        await pilot.pause(0.05)
        assert selector._index == 0

        # ctrl+r 重命名:输入框变名字编辑器,enter 保存并落盘
        index = [i for i, s in enumerate(selector._visible()) if s.id == zeta.id][0]
        selector._index = index
        selector._touched = True
        await pilot.press("ctrl+r")
        await pilot.pause(0.05)
        assert selector._renaming == zeta.id
        box.value = "重命名后"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert selector._renaming is None
        assert store.get(zeta.id).title == "重命名后"    # type: ignore[union-attr]

        # ctrl+d 删除要**先确认**(pi 的 delete confirmation),escape 能取消
        before = len(store.list())
        selector._index = [i for i, s in enumerate(selector._visible())
                           if s.id == unnamed.id][0]
        await pilot.press("ctrl+d")
        await pilot.pause(0.05)
        assert selector._confirming == unnamed.id
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert selector._confirming is None
        assert len(store.list()) == before               # 取消 = 没删

        await pilot.press("ctrl+d")
        await pilot.pause(0.05)
        await pilot.press("enter")                       # 确认删除
        await pilot.pause(0.05)
        assert len(store.list()) == before - 1
        assert store.get(unnamed.id) is None

        # 当前会话不能被删(pi 的口径):提示写在头部状态行,会话还在。
        #
        # 注意:启动时那个会话是 `reserve()` 出来的 —— **还没落盘**,所以它压根不在列表里
        # (这正是懒建的效果:进来看一眼就走不留文件)。要测"当前会话删不掉",得先切到一个
        # **已落盘**的会话上。
        selector._index = [i for i, s in enumerate(selector._visible())
                           if s.id == alpha.id][0]
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app._session is not None and app._session.id == alpha.id

        app._command("/resume")
        await pilot.pause(0.1)
        selector = app.screen
        assert isinstance(selector, tui_mod.SessionSelector)
        selector._index = [i for i, s in enumerate(selector._visible())
                           if s.id == alpha.id][0]
        await pilot.press("ctrl+d")
        await pilot.pause(0.05)
        assert selector._confirming is None                       # 连确认框都不弹
        assert selector._status is not None and "当前会话" in selector._status[0]
        assert store.get(alpha.id) is not None                    # 会话还在

        # enter 恢复高亮的那条(并关闭选择器)
        selector._index = [i for i, s in enumerate(selector._visible())
                           if s.id == zeta.id][0]
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.SessionSelector)
        assert app._session is not None and app._session.id == zeta.id


@pytest.mark.asyncio
async def test_session_selector_threaded_tree_and_unnamed_fallback(tmp_path, monkeypatch):
    """分支会话按 `parentSession` 缩进成树;没起过名的会话显示**第一句话**(pi 同款)。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        store = app._session_store()
        root = store.create("", cwd=tmp_path)        # 未命名:靠第一句话显示
        store.append(root, {"type": "message", "role": "user", "content": "梳理仓库结构"})
        child = store.fork_at(root, root.leaf, title="梳理仓库结构 @fork")

        app._command("/resume")
        await pilot.pause(0.1)
        selector = app.screen
        assert isinstance(selector, tui_mod.SessionSelector)
        ids = [s.id for s in selector._visible()]
        assert child.id in ids and root.id in ids
        prefixes = {s.id: prefix for s, prefix in selector.rendered_rows()}
        assert prefixes[child.id].strip() == "└─"            # 子会话挂了一条树枝
        assert prefixes[root.id] == ""                       # 根不缩进
        assert child.parent_session == str(root.path)        # header 里真写了 parentSession

        text = _static_plain(selector.query_one("#session-list", Static))
        assert "梳理仓库结构" in text                          # 未命名 → 显示第一句话

        # recent 排序下不画树(pi 只在 threaded 且无搜索时画)
        selector._sort = "recent"
        selector._refresh()
        await pilot.pause(0.05)
        assert all(prefix == "" for _s, prefix in selector.rendered_rows())


@pytest.mark.asyncio
async def test_session_selector_escape_cancels(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        store = app._session_store()
        store.create("只有一个", cwd=tmp_path)
        current = app._session.id if app._session else None

        app._command("/resume")
        await pilot.pause(0.1)
        assert isinstance(app.screen, tui_mod.SessionSelector)
        await pilot.press("ctrl+r")                      # 进重命名态
        await pilot.pause(0.05)
        await pilot.press("escape")                      # 第一次 escape:只退出重命名
        await pilot.pause(0.05)
        assert isinstance(app.screen, tui_mod.SessionSelector)
        await pilot.press("escape")                      # 第二次:关面板
        await pilot.pause(0.1)
        assert not isinstance(app.screen, tui_mod.SessionSelector)
        assert (app._session.id if app._session else None) == current


@pytest.mark.asyncio
async def test_modal_blocks_app_level_shortcuts(tmp_path, monkeypatch):
    """模态打开时 App 级快捷键必须让位 —— 否则 pi 选择器的 ctrl+d/t/u/l/o 全被抢。"""
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app._session_store().create("x", cwd=tmp_path)
        app._command("/resume")
        await pilot.pause(0.1)
        assert isinstance(app.screen, tui_mod.SessionSelector)

        thinking, expanded = app._show_thinking, app._expanded
        await pilot.press("ctrl+t")                       # 传输里是 App 的折叠思考块
        await pilot.press("ctrl+o")                       # App 的展开工具输出
        await pilot.pause(0.05)
        assert app._show_thinking == thinking
        assert app._expanded == expanded
        assert isinstance(app.screen, tui_mod.SessionSelector)   # 也没被 ctrl+c 之类关掉

        # 关掉模态后 App 快捷键恢复
        await pilot.press("escape")
        await pilot.pause(0.1)
        await pilot.press("ctrl+t")
        await pilot.pause(0.05)
        assert app._show_thinking != thinking


# ── /tree 过滤键与标签(pi 的 app.tree.*)──────────────────


@pytest.mark.asyncio
async def test_tree_filters_search_and_labels(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        session = app._session
        assert session is not None
        store = app._session_store()
        for entry in (
            {"type": "message", "role": "user", "content": "问一"},
            {"type": "message", "role": "assistant", "content": "答一"},
            {"type": "tool", "tool": "read", "status": "ok", "result": "x"},
            {"type": "state", "key": "model", "value": "m"},
        ):
            store.append(session, entry)
        app._replay_branch(session)

        app._command("/tree")
        await pilot.pause(0.1)
        selector = app.screen
        assert isinstance(selector, tui_mod.TreeSelector)
        box = selector.query_one("#session-filter", tui_mod.Input)

        def texts() -> list[str]:
            return [row.text for row in selector._rows()]

        # default:隐藏状态类 entry(pi 的 settings 类)
        assert not any("状态" in text for text in texts())
        assert any("工具" in text for text in texts())

        await pilot.press("ctrl+a")                       # 全部条目
        await pilot.pause(0.05)
        assert any("状态" in text for text in texts())

        await pilot.press("ctrl+t")                       # 隐藏工具结果
        await pilot.pause(0.05)
        assert not any("工具" in text for text in texts())

        await pilot.press("ctrl+u")                       # 只看用户消息
        await pilot.pause(0.05)
        assert texts() and all("你:" in text for text in texts())

        await pilot.press("ctrl+d")                       # 回到默认
        await pilot.pause(0.05)
        assert selector._mode == "default"

        box.value = "答一"                                 # 搜索(空格分词)
        await pilot.pause(0.05)
        assert len(texts()) == 1 and "答一" in texts()[0]
        box.value = ""
        await pilot.pause(0.05)

        # shift+l 打标签:输入框变标签编辑器,enter 保存并落盘
        listing = selector.query_one("#tree-list", tui_mod.OptionList)
        listing.highlighted = 0
        target = selector._rows()[0].id
        await pilot.press("shift+l")
        await pilot.pause(0.05)
        assert selector._editing == target
        box.value = "重点"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert selector._editing is None
        entry = next(e for e in session.entries if str(e.get("id")) == target)
        assert entry["label"] == "重点" and entry["labelTimestamp"]
        reread = store.get(session.id)
        assert reread is not None
        assert any(e.get("label") == "重点" for e in reread.entries)   # 真的落盘了

        await pilot.press("ctrl+l")                       # 只看有标签
        await pilot.pause(0.05)
        assert [row.id for row in selector._rows()] == [target]
        assert "[重点]" in texts()[0]

        await pilot.press("shift+t")                      # 标签带时间戳
        await pilot.pause(0.05)
        assert "(" in texts()[0]

        # 清标签(留空保存)→ labeled-only 变空
        await pilot.press("shift+l")
        await pilot.pause(0.05)
        box.value = ""
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert "label" not in entry
        assert selector._rows() == []

        await pilot.press("escape")
        await pilot.pause(0.05)
        assert not isinstance(app.screen, tui_mod.TreeSelector)


@pytest.mark.asyncio
@pytest.mark.parametrize("tone", ["dim", "text", "muted", "accent", "error", "warning",
                                  "info", "warn", "success", "nonsense", ""])
async def test_note_never_raises_on_any_tone(tmp_path, monkeypatch, tone):
    """`_note` 的色调不认时回落 `dim`,不报 `ThemeError`。

    补这条的原因:`/login` 里写过 `_note(..., "info")`,而调色板没有 `info` 这个键 ——
    `_note` 又在 worker 里跑,于是整条登录流程在真终端上报 `ThemeError: 未知主题色: info`。
    单测都在 monkeypatch `_note`,所以谁也没发现;这里**走真的 `_note`**。
    """
    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        before = len(app.query_one("#log").children)
        app._note(f"tone={tone!r}", tone)          # 真 `_note` → `palette.hex`
        await pilot.pause(0.05)
        assert len(app.query_one("#log").children) > before


def test_note_tones_cover_the_palette():
    """`NOTE_TONES` 里的每个值都必须是真调色板键(改色调表时别把 `info` 那类漏回来)。"""
    palette = load_palette("dark")
    for tone, key in tui_mod.NOTE_TONES.items():
        assert key in palette.colors, f"{tone} → {key} 不是调色板键"
