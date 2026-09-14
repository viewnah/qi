"""TUI(pi 同款视觉)。

版式与配色对齐 pi(`@earendil-works/pi-coding-agent` 的 `modes/interactive`):

  · inline 渲染:不占全屏、不吞滚动历史(裸 `qi` / `qi "问题"` 的落点)
  · 启动 banner = `qi vX` + 快捷提示 + 资源清单([Agents]/[Skills])
  · 用户消息 = userMessageBg 底色块(padding 1,1),助手 = 无底色 markdown(padding 0,1)
  · 思考 = 灰色斜体;工具调用 = tool{Pending,Success,Error}Bg 底色块,标题 `read <path>`
  · 编辑器 = 上下 `─` 动态边框;工作中上边框内嵌 `⠋ Working` 指示器(80ms 换帧)
  · footer = cwd(+git 分支+会话名) / token 统计 + 模型 / 状态行

qi 特有的 auto 分派保留,但按 pi 的行样式渲染(`● → agent (source, 0.90)`)。
交互命令(/help /new /resume /agents /mode /agent /tools)沿用 qi 语义。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, cast

from rich.markdown import Markdown as RichMarkdown
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Input, Static

from .cli import _load_registry
from .config import ConfigError, ResolvedModel, resolve_default_model
from .loader import LoadError
from .registry import ToolCatalog
from .runtime import QiRuntime
from .session import SessionStore
from .theme import (
    Palette,
    format_cwd_line,
    format_tokens,
    git_branch,
    resolve_theme,
    rich_theme,
    syntax_theme,
    textual_theme,
)
from .tools import register_builtin_tools

# pi 的 markdown 标题一律左对齐;Rich 默认把 h1 居中(Heading.LEVEL_ALIGN)。
from rich.markdown import Heading as _RichHeading

_RichHeading.LEVEL_ALIGN = {f"h{i}": "left" for i in range(1, 7)}

HELP_TEXT = """\
qi TUI 命令(实现状态以本表为准)

 对话与会话
  /new               新会话
  /resume [id]       选/恢复历史会话(不给 id = 列出来)
  /sessions          列出历史会话
  /name <名字>       设置会话显示名(进 footer)
  /session           会话信息(文件/ID/消息数/模型/用量)
  /export [文件]     导出会话 JSONL(默认 ./qi-<id>.jsonl)
  /import <文件>     从 JSONL 导入并切换会话
  /reload            重载 agents / plugins / 配置

 回答与凭证
  /copy              复制最后一条回答到剪贴板
  /login <provider>  显示登录指引(密钥不进会话记录,请用终端)
  /logout [provider] 删除已存凭证(不给 provider = 列出来)
  /changelog         显示 CHANGELOG.md(本仓库暂无)

 qi 独有
  /agents            列出 agent
  /mode auto|manual  切换分派模式
  /agent <name>      manual 模式锁定执行 agent
  /tools             当前/全部 agent 工具清单
  /clear             清屏
  @name 开头         直接点名 agent

 界面
  /help              本帮助
  /hotkeys           快捷键
  /quit              退出

 计划中(对齐 pi,需先给后端加能力)
  /model /thinking /compact /tree /fork /clone /scoped-models /settings /share /trust
"""

PLANNED_COMMANDS = frozenset({
    "/model", "/thinking", "/compact", "/tree", "/fork", "/clone",
    "/scoped-models", "/settings", "/share", "/trust",
})
"""pi 有、qi 暂未实现的命令 —— 单独提示“计划中”,不冒充“未知命令”。"""

HOTKEYS_TEXT = """\
快捷键(已实现):
  ctrl+c    退出
  ctrl+l    清屏
  ctrl+o    展开/折叠工具输出
  enter     提交
  up/down   单行输入框内移动光标

尚未实现(pi 有):escape 中断、ctrl+d 空输入退出、ctrl+c 清空编辑器、
! bash 模式、@ 文件补全、/ 命令补全、多行编辑、消息队列。见 docs/tui.md。
"""

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
"""pi 的 loader 帧(pi-tui `loader.js`)。"""

SPINNER_INTERVAL = 0.08
"""pi 的 loader 间隔 80ms。"""

FOOTER_LINES = 3
"""footer 占的行数(cwd / 统计+模型 / 状态);编辑器 3 行由边框各 1 行 + 输入 1 行组成。"""

PREVIEW_LINES = {"read": 10, "write": 10, "grep": 15, "ls": 20, "find": 20}
"""折叠态下各工具的输出行上限(对齐 pi 各工具的 format*Result)。"""


# ── 渲染小工具 ──────────────────────────────────────────


def _shorten(value: str, home: str | None = None) -> str:
    home = home or str(Path.home())
    return "~" + value[len(home):] if home and value.startswith(home) else value


def _as_int(value: object) -> int:
    """事件里的数字字段容错(上游可能是 str/None/缺失)。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _fmt_args(args: dict) -> str:
    """兜底:把未知工具的 args 压成一行 `k=v k=v`。"""
    parts = []
    for key, value in (args or {}).items():
        text = str(value).replace("\n", " ")
        if len(text) > 40:
            text = text[:37] + "…"
        parts.append(f"{key}={text}")
    return " ".join(parts)


class TuiRenderer:
    """把 qi 的 tool 调用 / 分派事件渲染成 pi 的行样式。"""

    def __init__(self, palette: Palette, cwd: Path):
        self.p = palette
        self.cwd = cwd

    # -- markdown -------------------------------------------------------
    def markdown(self, text: str) -> RichMarkdown:
        # Rich 的类型注解写的是 str,但运行时接受 SyntaxTheme 实例(见 rich.syntax.Syntax)
        return RichMarkdown(text.strip(), code_theme=cast(Any, syntax_theme(self.p)))

    # -- tool -----------------------------------------------------------
    def tool_title(self, name: str, args: dict) -> Text:
        p = self.p
        args = args or {}
        line = Text()
        title = Style(color=p.hex("toolTitle"), bold=True)
        accent = Style(color=p.hex("accent"))
        muted = Style(color=p.hex("toolOutput"))
        path = args.get("file_path") or args.get("path")

        def add(text: str, style: Style) -> None:
            line.append(text, style=style)

        if name == "read":
            add("read", title)
            add(" " + _shorten(str(path or "...")), accent)
            if args.get("start_line") is not None or args.get("end_line") is not None:
                start = args.get("start_line", 1)
                end = args.get("end_line", "")
                add(f":{start}{f'-{end}' if end != '' else ''}", Style(color=p.hex("warning")))
        elif name == "write":
            add("write", title)
            add(" " + _shorten(str(path or "...")), accent)
        elif name == "edit":
            add("edit", title)
            add(" " + _shorten(str(path or "...")), accent)
        elif name == "ls":
            add("ls", title)
            add(" " + _shorten(str(path or ".")), accent)
        elif name == "find":
            add("find", title)
            add(" " + str(args.get("pattern") or ""), accent)
            add(f" in {_shorten(str(path or '.'))}", muted)
        elif name == "grep":
            add("grep", title)
            add(f" /{args.get('pattern') or ''}/", accent)
            add(f" in {_shorten(str(path or '.'))}", muted)
            if args.get("context"):
                add(f" (context {args['context']})", muted)
        elif name == "bash":
            add(f"$ {args.get('command') or '...'}", title)
        elif name == "clarify":
            add("clarify", title)
            add(" " + str(args.get("question") or ""), Style(color=p.hex("text")))
        else:
            add(name, title)
            snippet = _fmt_args(args)
            if snippet:
                add(" " + snippet, muted)
        return line

    def tool_body(self, name: str, output: str, *, expanded: bool,
                  is_error: bool = False) -> Text:
        """工具输出:标题下方空行 + toolOutput 行,超出上限给 muted 省略提示。

        `read` 在折叠态不展示内容(对齐 pi 的 `formatReadResult`:展开或出错时才有输出)。
        """
        p = self.p
        body = Text()
        if name == "read" and not expanded and not is_error:
            return body
        if not output.strip():
            return body
        lines = output.rstrip("\n").split("\n")
        limit = len(lines) if expanded else PREVIEW_LINES.get(name, 20)
        shown = lines[:limit]
        body.append("\n")
        body.append("\n".join(shown), style=Style(color=p.hex("toolOutput")))
        remaining = len(lines) - len(shown)
        if remaining > 0:
            body.append(f"\n... ({remaining} more lines,", style=Style(color=p.hex("muted"), dim=True))
            body.append(" ctrl+o", style=Style(color=p.hex("accent"), dim=True))
            body.append(" to expand)", style=Style(color=p.hex("muted"), dim=True))
        return body

    # -- 其它行 ---------------------------------------------------------
    def dispatch_line(self, agent: str | None, data: dict) -> Text:
        """qi 的 auto 分派:pi 没有这个概念,用它的行风格呈现(无底色)。"""
        p = self.p
        shown = str(data.get("display_name") or agent or "?")
        source = data.get("source") or "?"
        confidence = _as_float(data.get("confidence"))
        line = Text()
        line.append("● ", style=Style(color=p.hex("accent")))
        line.append("→ ", style=Style(color=p.hex("muted")))
        line.append(shown, style=Style(color=p.hex("text"), bold=True))
        line.append(f" ({source}, {confidence:.2f})", style=Style(color=p.hex("dim")))
        return line

    def banner(self, version: str, agents: list[str], skills: list[str]) -> Text:
        p = self.p
        dim = Style(color=p.hex("dim"))
        muted = Style(color=p.hex("muted"))
        text = Text()
        text.append("\n")
        text.append("qi", style=Style(color=p.hex("accent"), bold=True))
        text.append(f" v{version}", style=dim)
        text.append("\n")
        hints = ["ctrl+c exit", "ctrl+l clear", "ctrl+o tools", "/ commands", "@agent"]
        for index, hint in enumerate(hints):
            if index:
                text.append(" · ", style=muted)
            text.append(hint, style=Style(color=p.hex("text")))
        text.append("\n")
        text.append("qi 是多 agent 编码框架:专职角色 + auto 分派;/help 看全部命令。", style=dim)
        text.append("\n\n")
        if agents:
            text.append("[Agents]\n", style=dim)
            text.append("  " + ", ".join(agents) + "\n", style=Style(color=p.hex("text")))
        if skills:
            text.append("[Skills]\n", style=dim)
            text.append("  " + ", ".join(skills) + "\n", style=Style(color=p.hex("text")))
        return text


# ── 消息块 ──────────────────────────────────────────────


class Blank(Static):
    """消息之间的空行(pi 用 Spacer(1))。"""

    def __init__(self) -> None:
        super().__init__("", classes="blank")


class UserMessage(Static):
    """用户消息:userMessageBg 底色块 + padding(1,1),内容走 markdown。"""

    def __init__(self, text: str, renderer: TuiRenderer, palette: Palette) -> None:
        super().__init__(renderer.markdown(text), classes="msg user-msg")
        self.source = text
        self.styles.background = palette.hex("userMessageBg")
        self.styles.padding = (1, 1)


class AssistantMessage(Static):
    """助手消息:无底色,markdown,左侧 1 空格 padding(pi outputPad=1)。"""

    def __init__(self, palette: Palette) -> None:
        super().__init__("", classes="msg assistant-msg")
        self.styles.padding = (0, 1)
        self.styles.background = "transparent"
        self.text = ""

    def append_delta(self, delta: str, renderer: TuiRenderer) -> None:
        self.text += delta
        self.update(renderer.markdown(self.text))

    def set_text(self, text: str, renderer: TuiRenderer) -> None:
        self.text = text
        self.update(renderer.markdown(self.text))


class ThinkingMessage(Static):
    def __init__(self, text: str, palette: Palette) -> None:
        super().__init__(RichMarkdown(text.strip(), code_theme=cast(Any, syntax_theme(palette))),
                         classes="msg thinking-msg")
        self.styles.padding = (0, 1)
        self.styles.background = "transparent"
        self.styles.text_style = "italic"
        self.styles.color = palette.hex("thinkingText")


class ToolBlock(Static):
    """工具调用:pi 的 Box(paddingX=1, paddingY=1) + 状态底色。"""

    def __init__(self, title: Text, palette: Palette) -> None:
        super().__init__(title, classes="msg tool-msg")
        self._palette = palette
        self.title_text = title
        self.content: Text = title.copy()
        self.styles.padding = (1, 1)
        self.set_state("pending")

    def set_state(self, state: str) -> None:
        key = {"pending": "toolPendingBg", "ok": "toolSuccessBg", "error": "toolErrorBg"}[state]
        self.styles.background = self._palette.hex(key)

    def set_output(self, body: Text | None) -> None:
        combined = self.title_text.copy()
        if body is not None and body.plain:
            combined.append_text(body)
        self.content = combined
        self.update(combined)


# ── App ─────────────────────────────────────────────────


class QiTui(App):
    TITLE = "qi"
    SUB_TITLE = "多 agent · auto 分派"

    CSS = """
    /* pi 没有滚动条/边框 chrome:inline 区域尽量只剩内容本身 */
    Screen { height: auto; max-height: 100%; background: transparent; scrollbar-size: 0 0; }
    #body { height: auto; background: transparent; scrollbar-size: 0 0; }
    #log { height: auto; background: transparent; scrollbar-size: 0 0; }
    .blank { height: 1; background: transparent; }
    .msg { width: 1fr; height: auto; }
    #border-top, #border-bottom { height: 1; background: transparent; }
    #input { border: none; height: 1; padding: 0 1; background: transparent; }
    #footer { height: auto; width: 1fr; background: transparent; scrollbar-size: 0 0; }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "退出"),
        ("ctrl+l", "clear_log", "清屏"),
        ("ctrl+o", "toggle_expand", "展开工具"),
    ]

    def __init__(self, runtime: QiRuntime | None = None, initial_prompt: str | None = None,
                 palette: Palette | None = None):
        super().__init__()
        self._rt = runtime
        self._initial_prompt = (initial_prompt or "").strip() or None
        self._palette = palette or resolve_theme(probe=False)
        # Textual inline 区域一定有不透明底色:用 pi 调色板 + 探测到的终端背景色注册主题,
        # 才能既拿到 pi 的色彩,又看不出“被填色”。
        theme = textual_theme(self._palette)
        self.register_theme(theme)
        self.theme = theme.name
        self._renderer = TuiRenderer(self._palette, Path.cwd())
        self._session = None
        self._agent: str | None = None      # manual 锁定
        self._auto = True
        self._shown_name = "?"
        self._working = False
        self._frame = 0
        self._frame_timer = None
        self._expanded = False
        self._live: AssistantMessage | None = None
        self._tool_blocks: list[tuple[ToolBlock, str, str]] = []  # (块, 工具名, 输出)
        self._current_tool: ToolBlock | None = None
        self._model: ResolvedModel | None = None
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self._status = "qi · auto"
        self.footer_text = Text("")
        self._branch: str | None = None
        self._last_answer = ""

    # -- 布局 -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="body"):
            yield VerticalScroll(id="log")
            yield Static("", id="border-top")
            yield Input(id="input")
            yield Static("", id="border-bottom")
            yield Static("", id="footer")

    def on_mount(self) -> None:
        self.console.push_theme(rich_theme(self._palette))
        try:
            if self._rt is None:
                self._rt = QiRuntime()
            self._renderer = TuiRenderer(self._palette, self._rt.cwd)
            store = SessionStore()
            self._session = store.create("tui", cwd=self._rt.cwd) or store.latest()
            if self._session is None:
                self._session = store.create("tui", cwd=self._rt.cwd)
            try:
                self._model = resolve_default_model(self._rt.cfg, self._rt.cwd)
            except ConfigError:
                self._model = None
            skills = sorted({s.name for unit in self._rt.registry.all() for s in unit.skills})
            self._append(Static(self._renderer.banner(
                _version(), self._rt.registry.names, skills), classes="msg"))
        except (LoadError, ConfigError) as exc:
            self._append(Static(Text(f"启动失败: {exc}", style=self._palette.hex("error")),
                                classes="msg"))
            self._rt = None
        self._repaint_borders()
        self._refresh_footer()
        self._sync_log_height()
        self.query_one("#input", Input).focus()
        if self._initial_prompt and self._rt is not None:
            self.call_after_refresh(self._submit, self._initial_prompt)

    # -- 基础操作 -------------------------------------------------------
    def _append(self, widget) -> None:
        log = self.query_one("#log", VerticalScroll)
        if len(log.children):
            log.mount(Blank())
        log.mount(widget)
        self.call_after_refresh(self._scroll_end)

    def _scroll_end(self) -> None:
        try:
            self.query_one("#log", VerticalScroll).scroll_end(animate=False)
        except Exception:  # pragma: no cover - 卸载竞态
            pass

    def _sync_log_height(self) -> None:
        """transcript 最多占 终端高 - (编辑器 3 + footer 3),超出内部滚动。"""
        reserved = 3 + FOOTER_LINES
        try:
            self.query_one("#log").styles.max_height = max(3, self.size.height - reserved)
        except Exception:  # pragma: no cover
            pass

    def on_resize(self) -> None:
        self._sync_log_height()
        self._repaint_borders()
        self._refresh_footer()

    def _repaint_borders(self) -> None:
        width = max(1, self.size.width)
        p = self._palette
        border = Style(color=p.hex("border"))
        bottom = Text("─" * width, style=border)
        self.query_one("#border-bottom", Static).update(bottom)
        self.query_one("#border-top", Static).update(self._top_border())

    def _top_border(self) -> Text:
        """空闲 = 整行 `─`;工作中 = `── ⠋ Working ───…`(pi 把 loader 嵌在上边框)。"""
        width = max(1, self.size.width)
        p = self._palette
        border = Style(color=p.hex("border"))
        if not self._working:
            return Text("─" * width, style=border)
        label = f" {SPINNER_FRAMES[self._frame]} Working "
        head = "── "
        rest = "─" * max(0, width - len(head) - len(label))
        line = Text(head, style=border)
        line.append(SPINNER_FRAMES[self._frame], style=Style(color=p.hex("accent")))
        line.append(" Working ", style=Style(color=p.hex("muted")))
        line.append(rest, style=border)
        return line

    def _set_working(self, working: bool) -> None:
        self._working = working
        if working and self._frame_timer is None:
            self._frame_timer = self.set_interval(SPINNER_INTERVAL, self._tick)
        elif not working and self._frame_timer is not None:
            self._frame_timer.stop()
            self._frame_timer = None
        self._repaint_borders()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER_FRAMES)
        self._repaint_borders()

    # -- footer ---------------------------------------------------------
    def _refresh_footer(self) -> None:
        p = self._palette
        dim = Style(color=p.hex("dim"))
        width = max(1, self.size.width)
        cwd = str(self._rt.cwd) if self._rt is not None else str(Path.cwd())
        session_name = getattr(self._session, "title", None) or None
        if self._branch is None and self._rt is not None:
            self._branch = git_branch(cwd)      # footer 会频繁重画,git 只查一次
        first = Text(format_cwd_line(cwd, branch=self._branch, session_name=session_name),
                     style=dim)

        parts: list[str] = []
        if self._usage["prompt_tokens"]:
            parts.append(f"↑{format_tokens(self._usage['prompt_tokens'])}")
        if self._usage["completion_tokens"]:
            parts.append(f"↓{format_tokens(self._usage['completion_tokens'])}")
        window = self._model.context_window if self._model else 0
        percent = 0.0
        if window and (self._usage["prompt_tokens"] + self._usage["completion_tokens"]):
            percent = min(100.0, 100.0 * self._usage["prompt_tokens"] / window)
        percent_key = "error" if percent > 90 else "warning" if percent > 70 else "text"
        left = Text(" ".join(parts), style=dim)
        if parts:
            left.append(" ")
        left.append(f"{percent:.1f}%/{format_tokens(window)} (auto)" if window else "?/?",
                    style=Style(color=p.hex(percent_key)))

        model_name = self._model.label if self._model else "no-model"
        if self._model and self._model.reasoning:
            model_name = f"{model_name} • medium"
        right = Text(model_name, style=dim)
        gap = max(2, width - left.cell_len - right.cell_len)
        second = Text()
        second.append_text(left)
        second.append(" " * gap)
        second.append_text(right)

        third = Text(self._status, style=Style(color=p.hex("muted")))
        self.footer_text = Text("\n").join([first, second, third])
        self.query_one("#footer", Static).update(self.footer_text)

    # -- 输入 -----------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#input", Input).value = ""
        if not text:
            return
        self._submit(text)

    def _submit(self, text: str) -> None:
        if text.startswith("/"):
            self._append(Static(Text(f"> {text}", style=Style(color=self._palette.hex("dim"))),
                                classes="msg"))
            self._command(text)
            return
        if self._rt is None:
            self._append(Static(Text("运行时不可用。", style=self._palette.hex("error")), classes="msg"))
            return
        rt = self._rt
        self._append(UserMessage(text, self._renderer, self._palette))
        if self._session is None:
            self._session = SessionStore().create("tui", cwd=rt.cwd)
        override = None if self._auto else self._agent
        self.run_worker(self._run(text, override), exclusive=False)

    # -- 事件循环 -------------------------------------------------------
    async def _run(self, text: str, override: str | None) -> None:
        assert self._rt is not None and self._session is not None
        self._live = None
        renderer = self._renderer
        self._set_working(True)
        try:
            async for ev in self._rt.stream(text, self._session, agent_override=override):
                if ev.kind == "dispatch":
                    self._shown_name = str(ev.data.get("display_name") or ev.agent or "?")
                    self._append(Static(renderer.dispatch_line(ev.agent, ev.data), classes="msg"))
                elif ev.kind == "opening":
                    self._append(Static(Text(ev.text, style=Style(color=self._palette.hex("muted"))),
                                        classes="msg"))
                elif ev.kind == "text_delta":
                    if self._live is None:
                        self._live = AssistantMessage(self._palette)
                        self._append(self._live)
                    self._live.append_delta(ev.text, renderer)
                    self._scroll_end()
                elif ev.kind == "assistant_message":
                    if self._live is None:
                        self._live = AssistantMessage(self._palette)
                        self._append(self._live)
                    self._live.set_text(ev.text, renderer)
                    self._live = None
                    # 最终回答(不带工具调用的那条)才供 /copy 使用
                    if ev.text.strip() and not ev.data.get("tool_calls"):
                        self._last_answer = ev.text
                elif ev.kind == "tool_start":
                    block = ToolBlock(renderer.tool_title(ev.tool or "?", ev.data.get("args") or {}),
                                      self._palette)
                    self._current_tool = block
                    self._append(block)
                elif ev.kind == "tool_end":
                    block = self._current_tool
                    if block is None:
                        block = ToolBlock(renderer.tool_title(ev.tool or "?", {}), self._palette)
                        self._append(block)
                    status = "ok" if ev.data.get("status") == "ok" else "error"
                    block.set_state(status)
                    name = ev.tool or "?"
                    is_error = status != "ok"
                    block.set_output(renderer.tool_body(name, ev.text or "",
                                                        expanded=self._expanded,
                                                        is_error=is_error))
                    self._tool_blocks.append((block, name, ev.text or ""))
                    if status == "error" and ev.data.get("error"):
                        block.set_output(Text("\n" + str(ev.data["error"]),
                                              style=Style(color=self._palette.hex("error"))))
                    self._current_tool = None
                    self._scroll_end()
                elif ev.kind == "error":
                    self._append(Static(Text(ev.text, style=Style(color=self._palette.hex("error"))),
                                        classes="msg"))
                elif ev.kind == "agent_end":
                    usage = ev.data.get("usage") or {}
                    self._usage["prompt_tokens"] += _as_int(usage.get("prompt_tokens"))
                    self._usage["completion_tokens"] += _as_int(usage.get("completion_tokens"))
                    self._refresh_footer()
        finally:
            self._set_working(False)
            self._live = None
            self._scroll_end()

    # -- 命令 -----------------------------------------------------------
    def _note(self, text: str, tone: str = "dim") -> None:
        """命令行输出:统一消息块(无底色,padding 0,1)。"""
        self._append(Static(Text(text, style=Style(color=self._palette.hex(tone))), classes="msg"))

    def _command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        # /quit 与 /hotkeys /help 不依赖 runtime;其余需要一个可用的运行期
        if self._rt is None and cmd not in ("/quit", "/help", "/hotkeys"):
            self._note("运行时不可用。", "error")
            self._scroll_end()
            return
        rt = self._rt
        store = SessionStore()

        if cmd == "/help":
            self._note(HELP_TEXT, "text")
        elif cmd == "/hotkeys":
            self._note(HOTKEYS_TEXT, "text")
        elif cmd == "/quit":
            self.exit()
        elif cmd == "/clear":
            self.action_clear_log()

        # ── 会话 ──────────────────────────────────────────────
        elif cmd == "/new":
            if rt is None:
                return
            self._session = store.create("tui", cwd=rt.cwd)
            self._branch = git_branch(str(rt.cwd))
            self._agent = None
            self._auto = True
            self._usage = {"prompt_tokens": 0, "completion_tokens": 0}
            self._last_answer = ""
            self._note("已开新会话(auto)")
            self._refresh_footer()
        elif cmd in ("/resume", "/sessions"):
            sessions = store.list()[:10]
            if cmd == "/resume" and arg:
                s = store.get(arg)
                if s is None:
                    self._note(f"会话不存在 {arg}", "error")
                else:
                    self._session = s
                    self._refresh_footer()
                    self._note(f"已恢复 {s.id}")
            else:
                rows = ["会话(最新在前):"]
                rows += [f"  {s.id}  {s.title or '(未命名)'}  {s.created_at}" for s in sessions]
                self._note("\n".join(rows) if sessions else "(无会话)")
                if sessions and cmd == "/resume":
                    self._note("用 /resume <id> 恢复")
        elif cmd == "/session":
            session = self._session
            if session is None:
                self._note("当前没有会话", "warning")
                self._scroll_end()
                return
            usage = self._usage
            model = self._model.label if self._model else "?"
            lines = [
                f"会话: {session.id}",
                f"文件: {session.path}",
                f"目录: {session.cwd or '(未记录)'}",
                f"创建: {session.created_at or '?'}",
                f"消息: {session.message_count} 条",
                f"模型: {model}",
                f"用量: ↑{format_tokens(usage['prompt_tokens'])} "
                f"↓{format_tokens(usage['completion_tokens'])}",
            ]
            self._note("\n".join(lines), "text")
        elif cmd == "/name":
            session = self._session
            if session is None:
                self._note("当前没有会话", "warning")
                self._scroll_end()
                return
            if not arg:
                self._note("用法: /name <名字>", "warning")
            else:
                session.title = arg
                if session.entries and session.entries[0].get("type") == "session":
                    session.entries[0]["title"] = arg
                    store.save(session)
                self._refresh_footer()
                self._note(f"会话名已设为 {arg}")
        elif cmd == "/export":
            session = self._session
            if session is None:
                self._note("当前没有会话", "warning")
                self._scroll_end()
                return
            target = Path(arg).expanduser() if arg else Path.cwd() / f"qi-{session.id}.jsonl"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(session.path, target)
            except OSError as exc:
                self._note(f"导出失败: {exc}", "error")
            else:
                self._note(f"已导出 {session.path} → {target}")
        elif cmd == "/import":
            if not arg:
                self._note("用法: /import <file.jsonl>", "warning")
            else:
                self._import_session(Path(arg).expanduser())
        elif cmd == "/reload":
            self._reload_runtime()

        # ── 回答与凭证 ────────────────────────────────────────
        elif cmd == "/copy":
            if not self._last_answer.strip():
                self._note("还没有回答可复制", "warning")
            else:
                self.copy_to_clipboard(self._last_answer)
                self._note("已复制最后一条回答到剪贴板")
        elif cmd == "/login":
            providers = sorted(getattr(getattr(rt, "cfg", None), "providers", {}) or {})
            if not arg:
                self._note("用法: /login <provider>\n可用 provider: "
                           + (", ".join(providers) or "(models.json 里还没有 provider)"),
                           "warning")
            else:
                self._note(f"密钥不在 TUI 里采集(会进会话记录)。请在终端执行:\n"
                           f"    qi auth login {arg}")
        elif cmd == "/logout":
            from .auth import AuthStore

            auth = AuthStore()
            if not arg:
                self._note("已存凭证: " + (", ".join(auth.providers()) or "(无)")
                           + "\n用法: /logout <provider>")
            elif auth.remove(arg):
                self._note(f"已删除 {arg} 的凭证")
            else:
                self._note(f"{arg} 没有已存凭证", "warning")
        elif cmd == "/changelog":
            self._note(self._changelog())

        # ── qi 独有 ──────────────────────────────────────────
        elif cmd == "/agents":
            catalog = ToolCatalog()
            register_builtin_tools(catalog)
            reg = _load_registry(catalog)
            lines = []
            for u in reg.all():
                shown = (u.config.display_name or "").strip() or u.name
                lines.append(f"{shown} ({u.name} · {u.source}) tools={','.join(u.tools) or '全部'}")
                desc = u.config.description.splitlines()[0] if u.config.description else ""
                lines.append(f"   {desc}")
            self._note("\n".join(lines), "text")
        elif cmd == "/mode":
            mode = arg.lower()
            if mode in ("auto", "manual"):
                self._auto = mode == "auto"
                self._note(f"模式: {mode}")
            else:
                self._note("用法: /mode auto|manual", "warning")
        elif cmd == "/agent":
            if arg and rt is not None and rt.registry.get(arg):
                self._agent = arg
                self._auto = False
                self._note(f"锁定 agent: {arg}(manual)")
            else:
                self._note(f"未知 agent: {arg};可用 /agents 查看", "error")
        elif cmd == "/tools":
            self._note("内置工具: read ls find grep write edit bash clarify")
        elif cmd in PLANNED_COMMANDS:
            self._note(f"{cmd} 计划中(需先给后端加能力;见 /help 末尾)", "warning")
        else:
            self._note(f"未知命令 {cmd};/help 查看", "warning")
        self._scroll_end()

    # -- 命令用到的具体动作 ────────────────────────────────
    def _import_session(self, source: Path) -> None:
        """把外部 JSONL 拷进会话目录并切过去(对齐 pi 的 /import)。"""
        if self._rt is None:
            self._note("运行时不可用。", "error")
            return
        store = SessionStore()
        if not source.is_file():
            self._note(f"文件不存在: {source}", "error")
            return
        entries: list[dict] = []
        for line in source.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                self._note(f"不是合法的 JSONL: {source}", "error")
                return
        header = entries[0] if entries else {}
        if header.get("type") != "session" or not header.get("id"):
            self._note("缺少会话头(type/id),无法导入", "error")
            return
        sid = str(header["id"])
        if store.get(sid) is not None:
            self._note(f"会话已存在: {sid}(用 /resume {sid})", "warning")
            return
        dest = store.root / f"{time.strftime('%Y%m%dT%H%M%S')}_{sid}.jsonl"
        try:
            shutil.copy(source, dest)
        except OSError as exc:
            self._note(f"导入失败: {exc}", "error")
            return
        session = store.get(sid)
        if session is None:  # pragma: no cover - 刚拷进去就读不到属于异常
            self._note("导入后读取失败", "error")
            return
        self._session = session
        self._refresh_footer()
        self._note(f"已导入并切换到 {sid}(消息 {session.message_count} 条)")

    def _reload_runtime(self) -> None:
        """重载 agents / plugins / 配置(会话不变)。"""
        try:
            runtime = QiRuntime()
        except (LoadError, ConfigError) as exc:
            self._note(f"重载失败: {exc}", "error")
            return
        self._rt = runtime
        self._renderer = TuiRenderer(self._palette, runtime.cwd)
        self._branch = git_branch(str(runtime.cwd))
        try:
            self._model = resolve_default_model(runtime.cfg, runtime.cwd)
        except ConfigError:
            self._model = None
        self._refresh_footer()
        self._note(f"已重载:{len(runtime.registry.names)} 个 agent。"
                   "主题改动需重开 qi。")

    @staticmethod
    def _changelog() -> str:
        """CHANGELOG.md 前几行(对齐 pi 的 /changelog)。"""
        candidates = [Path.cwd() / "CHANGELOG.md",
                      Path(__file__).resolve().parents[2] / "CHANGELOG.md"]
        for path in candidates:
            if path.is_file():
                lines = path.read_text(encoding="utf-8").splitlines()
                head = lines[:40]
                more = f"\n…(共 {len(lines)} 行,见 {path})" if len(lines) > len(head) else ""
                return f"{path}\n\n" + "\n".join(head) + more
        return "未找到 CHANGELOG.md(qi 仓库暂无)"

    # -- 动作 -----------------------------------------------------------
    def action_clear_log(self) -> None:
        log = self.query_one("#log", VerticalScroll)
        for child in list(log.children):
            child.remove()
        self._tool_blocks.clear()
        self._live = None

    def action_toggle_expand(self) -> None:
        """ctrl+o:展开/折叠工具输出(对齐 pi 的 app.tools.expand)。"""
        self._expanded = not self._expanded
        for block, name, output in self._tool_blocks:
            block.set_output(self._renderer.tool_body(name, output, expanded=self._expanded))

def _version() -> str:
    from . import __version__

    return __version__


def run_tui(initial_prompt: str | None = None) -> None:
    """启动 TUI;`initial_prompt` 非空时进界面即提交(来自 `qi "问题"`)。"""
    setting = None
    try:
        from .settings import load_settings

        setting = load_settings()[0].theme
    except Exception:  # settings 坏了不该挡住进界面
        setting = None
    palette = resolve_theme(setting, probe=True)
    QiTui(initial_prompt=initial_prompt, palette=palette).run(inline=True, inline_no_clear=True)
