"""TUI 视觉基线(stub runtime,不联网)。

锁住 pi 的行样式:工具标题格式、输出截断、分派行、以及消息块/工具块/编辑器边框/
footer 真的取到了 pi 调色板里的颜色。像素级对齐由人工比对,这里防的是“改坏了没人发现”。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qi_agent import tui as tui_mod
from qi_agent.config import ResolvedModel
from qi_agent.models import AgentEvent
from qi_agent.llm import LiteLLMClient
from qi_agent.theme import load_palette
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


def test_banner_lists_agents_and_skills():
    text = _renderer().banner("0.1.0", ["general", "code-analyst"], ["termio"]).plain
    assert "qi v0.1.0" in text
    # 只写 qi 真的实现了的快捷键(escape 中断、ctrl+c 清空/退出、ctrl+o 展开)
    assert "escape interrupt" in text
    assert "ctrl+c clear/exit" in text and "ctrl+o tools" in text
    assert "! bash" in text and "@ files" in text      # 补全与 bash 模式已实现,可以写
    assert "[Agents]" in text and "general, code-analyst" in text
    assert "[Skills]" in text and "termio" in text


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
        from qi_agent.session import SessionStore

        self.sessions = SessionStore()
        self.cfg = None
        self.cwd = Path.cwd()
        self.registry = _FakeRegistry()

    async def stream(self, prompt, session, agent_override=None):
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

        # footer:第一行 cwd、第二行统计 + 右对齐模型、第三行状态
        footer = app.footer_text.plain
        assert "↑12k ↓678" in footer
        assert "deepseek/deepseek-v4.1-flash • medium" in footer
        assert "qi · auto" in footer


# ── `/` 命令:对齐 pi 的部分 + “计划中”不冒充未知 ──────────


async def _command_notes(app, monkeypatch) -> list[tuple[str, str]]:
    """把 `_note` 换成收集器,便于断言命令输出。"""
    notes: list[tuple[str, str]] = []
    app._note = lambda text, tone="dim": notes.append((text, tone))  # type: ignore[method-assign]
    return notes


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

        app._command("/help")
        assert "计划中" in notes[-1][0] and "/hotkeys" in notes[-1][0]
        assert "/sessions" in notes[-1][0] and "/export" in notes[-1][0]

        app._command("/hotkeys")
        assert "ctrl+o" in notes[-1][0] and "尚未对齐" in notes[-1][0]

        app._command("/session")
        assert "会话: " in notes[-1][0] and "deepseek/deepseek-v4.1-flash" in notes[-1][0]

        app._command("/sessions")            # help 里承诺过,必须真的有实现
        assert "会话(最新在前)" in notes[-1][0]

        assert app._rt is not None
        app._rt.cwd = Path("/tmp/short")          # tmp_path 太长会被 footer 截断(pi 同款)
        app._command("/name 我的会话")
        assert "我的会话" in notes[-1][0]
        assert app._session is not None and app._session.title == "我的会话"
        assert "我的会话" in app.footer_text.plain      # 名字进 footer(对齐 pi)

        app._command("/copy")                # 还没有回答 → 底部状态行提示,不进 transcript
        assert app._status == "还没有回答可复制"

        app._command("/login")
        assert "用法" in notes[-1][0]
        app._command("/login deepseek")
        assert "qi auth login deepseek" in notes[-1][0]   # 密钥不进会话记录

        app._command("/logout")
        assert "用法" in notes[-1][0]

        app._command("/model")               # 已实现(不再“计划中”)
        assert "当前: " in notes[-1][0] and "切换: " in notes[-1][0]

        app._command("/thinking")            # pi 有、qi 未实现 → “计划中”
        assert "计划中" in notes[-1][0]

        app._command("/changelog")
        assert "CHANGELOG" in notes[-1][0]

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


class SlowRuntime(FakeRuntime):
    """文本先流式出来,然后卡住 —— 用来测 escape 中断。"""

    async def stream(self, prompt, session, agent_override=None):
        yield AgentEvent(kind="text_delta", agent="general", text="开始……")
        await asyncio.sleep(5)
        yield AgentEvent(kind="assistant_message", agent="general", text="不该到这一步",
                         data={"step": 1, "tool_calls": []})


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
        assert isinstance(app._rt.llm_exec, LiteLLMClient)
        assert app._rt.llm_exec.spec.model == "m2"        # 真的换到运行期模型上
        assert "alpha/m2" in app.footer_text.plain

        app.action_cycle_model()                          # ctrl+p
        assert (app._model.provider, app._model.model) == ("beta", "m3")
        app.action_cycle_model()
        assert (app._model.provider, app._model.model) == ("alpha", "m1")   # 环绕
        app.action_cycle_model_back()                     # ctrl+shift+p
        assert (app._model.provider, app._model.model) == ("beta", "m3")

        app._command("/model")                            # 列表 + 当前
        assert "当前: beta/m3" in notes[-1][0]
        app._command("/model m2")                         # 只给模型名也能唯一匹配
        assert (app._model.provider, app._model.model) == ("alpha", "m2")

        app.action_select_model()                         # ctrl+l → 模态选择器
        await pilot.pause(0.1)
        assert isinstance(app.screen, tui_mod.ModelSelector)
        from textual.widgets import OptionList as _OptionList

        assert app.screen.query_one("#model-list", _OptionList).option_count == 3
        await pilot.press("escape")
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

    app = QiTui(palette=PALETTE)
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
async def test_footer_stays_three_lines_and_truncates(tmp_path, monkeypatch):
    """footer 必须恒为 3 行:超长 cwd / 模型名要被截断,不能折行(折行会挤掉输入框)。"""
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
        assert len(lines) == 3                      # 不折行
        assert all(len(line) <= 60 for line in lines)


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

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)

        await _editor_with(app, pilot, "/se")            # 命令补全
        values = [v for v, _ in app._completion_candidates()[0]]
        assert "/session" in values and "/sessions" in values
        assert "/help" not in values
        assert app._completions_open is True

        await _editor_with(app, pilot, "讲一下 @al")     # 文件补全
        values = [v for v, _ in app._completion_candidates()[0]]
        assert values == ["@alpha.py"]

        await _editor_with(app, pilot, "@")              # 目录优先、隐藏文件默认不列
        values = [v for v, _ in app._completion_candidates()[0]]
        assert "@subdir/" in values and "@alpha.py" in values

        await _editor_with(app, pilot, "普通文本")        # 没有触发词
        assert app._completion_candidates()[0] == []
        assert app._completions_open is False


@pytest.mark.asyncio
async def test_tab_applies_completion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha.py").write_text("x")
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)

        editor = await _editor_with(app, pilot, "/se")
        assert app._completions_open
        await pilot.press("down")                        # 面板里下移
        await pilot.press("tab")                         # tab = 接受补全
        await pilot.pause(0.05)
        assert editor.text == "/sessions "               # 第二个候选 + 尾随空格
        assert app._completions_open is False

        editor = await _editor_with(app, pilot, "@al")
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert editor.text == "@alpha.py"

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
