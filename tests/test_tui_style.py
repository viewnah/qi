"""TUI 视觉基线(stub runtime,不联网)。

锁住 pi 的行样式:工具标题格式、输出截断、分派行、以及消息块/工具块/编辑器边框/
footer 真的取到了 pi 调色板里的颜色。像素级对齐由人工比对,这里防的是“改坏了没人发现”。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qi_agent import tui as tui_mod
from qi_agent.config import ResolvedModel
from qi_agent.models import AgentEvent
from qi_agent.theme import load_palette
from qi_agent.tui import SPINNER_FRAMES, QiTui, TuiRenderer, ToolBlock, UserMessage

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
    assert "ctrl+c exit" in text and "ctrl+o tools" in text
    # 不再抄 pi 的提示行:qi 没实现的（escape/ctrl+d/! bash）不得出现在 banner 里
    assert "escape interrupt" not in text
    assert "! bash" not in text
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


class FakeRuntime:
    def __init__(self, *args, **kwargs):
        self.sessions = None
        self.cfg = None
        self.cwd = Path.cwd()
        self.registry = _FakeRegistry()

    async def stream(self, prompt, session, agent_override=None):
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
        notes = await _command_notes(app, monkeypatch)

        app._command("/help")
        assert "计划中" in notes[-1][0] and "/hotkeys" in notes[-1][0]
        assert "/sessions" in notes[-1][0] and "/export" in notes[-1][0]

        app._command("/hotkeys")
        assert "ctrl+o" in notes[-1][0] and "尚未实现" in notes[-1][0]

        app._command("/session")
        assert "会话: " in notes[-1][0] and "deepseek/deepseek-v4.1-flash" in notes[-1][0]

        app._command("/sessions")            # help 里承诺过,必须真的有实现
        assert "会话(最新在前)" in notes[-1][0]

        app._command("/name 我的会话")
        assert "我的会话" in notes[-1][0]
        assert app._session is not None and app._session.title == "我的会话"
        assert "我的会话" in app.footer_text.plain      # 名字进 footer(对齐 pi)

        app._command("/copy")                # 还没有回答
        assert "还没有回答" in notes[-1][0]

        app._command("/login")
        assert "用法" in notes[-1][0]
        app._command("/login deepseek")
        assert "qi auth login deepseek" in notes[-1][0]   # 密钥不进会话记录

        app._command("/logout")
        assert "用法" in notes[-1][0]

        app._command("/model")               # pi 有、qi 未实现 → “计划中”
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
        assert "已复制" in notes[-1][0]
