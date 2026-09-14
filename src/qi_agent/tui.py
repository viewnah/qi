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

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from rich.markdown import Markdown as RichMarkdown
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .auth import AuthStore
from .cli import _load_registry
from .config import ConfigError, ResolvedModel, resolve_default_model, resolve_model
from .llm import THINKING_LEVELS, LiteLLMClient, ThinkingLLMClient, normalize_thinking_level
from .loader import LoadError
from .registry import ToolCatalog
from .runtime import MAX_TOOL_ENTRY_CHARS, QiRuntime
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
  /thinking [级别]   思考级别(off|minimal|low|medium|high|xhigh|max;等同 shift+tab)
  /model [p/m]       当前模型 / 切换模型(等同 ctrl+l)
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
  /compact /tree /fork /clone /scoped-models /settings /share /trust
"""

PLANNED_COMMANDS = frozenset({
    "/compact", "/tree", "/fork", "/clone",
    "/scoped-models", "/settings", "/share", "/trust",
})
"""pi 有、qi 暂未实现的命令 —— 单独提示“计划中”,不冒充“未知命令”。"""

TUI_COMMANDS: dict[str, str] = {
    "/help": "本帮助",
    "/hotkeys": "快捷键",
    "/quit": "退出",
    "/clear": "清屏",
    "/new": "新会话",
    "/resume": "选/恢复历史会话",
    "/sessions": "列出历史会话",
    "/name": "设置会话显示名",
    "/session": "会话信息",
    "/model": "当前/切换模型",
    "/export": "导出会话 JSONL",
    "/import": "从 JSONL 导入会话",
    "/reload": "重载 agents/插件/配置",
    "/copy": "复制最后一条回答",
    "/login": "登录指引(密钥不进会话)",
    "/logout": "删除已存凭证",
    "/changelog": "显示 CHANGELOG.md",
    "/agents": "列出 agent",
    "/mode": "切换分派模式",
    "/agent": "manual 锁定执行 agent",
    "/tools": "工具清单",
}
"""`/` 补全的候选(命令 → 说明);与 `_command` 的已实现分支一一对应。"""

COMPLETION_ROWS = 8
"""补全面板最多显示几行。"""

BASH_PREVIEW_LINES = 20
"""bash 输出折叠时的预览行数(对齐 pi 的 BashExecutionComponent)。"""

HOTKEYS_TEXT = """\
快捷键(对齐 pi 的部分)

  输入(多行编辑器,对齐 pi-tui/components/editor.js)
  enter                   提交
  shift+enter / ctrl+j    换行
  tab                     补全:行首 `/` = 命令、`@` = 相对路径;无候选时 = 缩进
  ↑ / ↓                   补全面板开着时选候选,否则移动光标
  ctrl+b / ctrl+f         光标左 / 右
  alt+b / alt+f、alt+←/→、ctrl+←/→  按词移动
  ctrl+w / alt+backspace  删前一个词
  alt+d                   删后一个词
  ctrl+u / ctrl+k        删到行首 / 删到行尾
  ctrl+-                  撤销
  ctrl+v                  粘贴(支持多行 / 括号粘贴)

  bash 模式(行首 `!`)
  !<命令>                 执行 shell,并把「命令 + 输出」记入上下文
  !!<命令>                同样执行,但输出不进上下文(边框变暗)

  应用
  escape                  中断当前回合;有排队时先把排队退回编辑器
  shift+tab               循环思考级别(off→minimal→…→max)
  ctrl+t                  显示/隐藏思考块
  enter                   提交(回合进行中 = 排队,当前回合结束后发送)
  alt+enter               排队 follow-up(排在 steer 之后发送)
  alt+up                  取回排队消息到编辑器
  ctrl+c                  清空编辑器;连按两次退出
  ctrl+d                  编辑器为空时退出(非空 = 删右侧字符)
  ctrl+o                  展开/折叠工具输出
  ctrl+x                  复制最后一条回答
  ctrl+g                  用 $EDITOR 编辑当前内容
  ctrl+l                  选择模型(models.json 里的)
  ctrl+p / ctrl+shift+p   切换下一个 / 上一个模型
  ctrl+z                  挂起(回到 shell,fg 回来)

尚未对齐(pi 有,qi 缺能力或驱动不了):
  ctrl+n 会话列表过滤   ctrl+r 重命名会话   ctrl+y/alt+y kill-ring 的 yank
  ctrl+v 粘贴图片(现在只会粘文本)
"""

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
"""pi 的 loader 帧(pi-tui `loader.js`)。"""

SPINNER_INTERVAL = 0.08
"""pi 的 loader 间隔 80ms。"""

FOOTER_LINES = 3
"""footer 占的行数(cwd / 统计+模型 / 状态);编辑器 3 行由边框各 1 行 + 输入 1 行组成。"""

MAX_EDITOR_ROWS = 8
"""编辑器最多长到几行(再多在编辑器内部滚动,不抢 transcript 的空间)。"""

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
        hints = ["escape interrupt", "ctrl+c clear/exit", "ctrl+o tools", "/ commands",
                 "@ files", "! bash"]
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
    """思考块(pi 的 thinking):灰色斜体 markdown;流式追加。"""

    def __init__(self, text: str, palette: Palette) -> None:
        super().__init__(RichMarkdown(text.strip(), code_theme=cast(Any, syntax_theme(palette)))
                         if text.strip() else "", classes="msg thinking-msg")
        self.text_content = text
        self._palette = palette
        self.styles.padding = (0, 1)
        self.styles.background = "transparent"
        self.styles.text_style = "italic"
        self.styles.color = palette.hex("thinkingText")

    def append_delta(self, delta: str) -> None:
        self.text_content += delta
        self.update(RichMarkdown(self.text_content.strip(),
                                 code_theme=cast(Any, syntax_theme(self._palette))))


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


class BashBlock(Static):
    """`!` / `!!` 手动 bash(对齐 pi 的 `bash-execution.js`)。

    pi 用「上下 `─` 边框 + `$ command` + 输出」而不是工具块底色;边框色
    `bashMode`(绿),`!!`(不进上下文)用 `dim`。宽度变化时重画边框。
    """

    rendered: Text
    """最近一次画出来的内容(不能叫 content:Static 已有同名 property)。"""

    def __init__(self, command: str, palette: Palette, excluded: bool) -> None:
        super().__init__("", classes="msg bash-msg")
        self.command = command
        self._palette = palette
        self._excluded = excluded
        self._output = ""
        self._code: int | None = None
        self._expanded = False
        self.rendered = Text("")
        self._repaint()

    def set_result(self, output: str, code: int | None) -> None:
        self._output = output
        self._code = code
        self._repaint()

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._repaint()

    def on_resize(self) -> None:
        self._repaint()

    def _repaint(self) -> None:
        p = self._palette
        width = max(20, self.size.width or (self.app.size.width - 2) or 80)
        color_key = "dim" if self._excluded else "bashMode"
        border = Style(color=p.hex(color_key))
        block = Text()
        block.append("─" * width, style=border)
        block.append("\n $ ", style=border)
        block.append(self.command, style=Style(color=p.hex(color_key), bold=True))
        title = "  (不进上下文)" if self._excluded else ""
        if title:
            block.append(title, style=Style(color=p.hex("dim")))
        block.append("\n")
        if self._code is None:
            block.append("运行中…", style=Style(color=p.hex("muted")))
        else:
            out = self._output.rstrip("\n")
            lines = out.split("\n") if out else ["(无输出)"]
            limit = len(lines) if self._expanded else BASH_PREVIEW_LINES
            shown = lines[:limit]
            block.append("\n".join(shown), style=Style(color=p.hex("toolOutput")))
            remaining = len(lines) - len(shown)
            if remaining > 0:
                block.append(f"\n... ({remaining} more lines,",
                             style=Style(color=p.hex("muted")))
                block.append(" ctrl+o", style=Style(color=p.hex("accent")))
                block.append(" to expand)", style=Style(color=p.hex("muted")))
            mark = "✓" if self._code == 0 else "✗"
            mark_key = "success" if self._code == 0 else "error"
            block.append(f"\n{mark} exit {self._code}", style=Style(color=p.hex(mark_key)))
        block.append("\n")
        block.append("─" * width, style=border)
        self.rendered = block
        self.update(block)


# ── App ─────────────────────────────────────────────────


class Editor(TextArea):
    """多行编辑器(对齐 pi 的 `pi-tui/components/editor.js` 键位)。

    Textual 的 `TextArea` 已经提供多行编辑/选区/撤销/括号粘贴,这里只补齐 pi 的差异:
      · enter = 提交(pi `tui.input.submit`)
      · shift+enter / ctrl+j = 换行(pi `tui.input.newLine`)
      · ctrl+b/f。alt+b/f、alt+←/→、alt+d = 光标词移动/删词(pi 的别名)
      · ctrl+- = 撤销(pi `tui.editor.undo`;Textual 默认把 undo 绑在 ctrl+z,
        而 ctrl+z 在 pi 里是挂起,所以改绑到 pi 的键)
    未实现:kill-ring 的 yank/yank-pop(ctrl+y / alt+y)—— Textual 没有 kill-ring,
    ctrl+y 仍是它默认的 redo。
    """

    class Submitted(Message):
        """Enter 提交。"""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class Interrupt(Message):
        """escape:关闭补全面板 / 请求中断当前回合。

        为什么要在这里接管:Textual 的 `Screen._key_escape` 会把 escape 当成「清选区」
        先吃掉,App 级非 priority 绑定收不到;而改成 priority 又会抢掉模态选择器的
        escape。编辑器持焦时自行处理最干净(模态打开时焦点不在编辑器,不受影响)。
        """

    class Complete(Message):
        """tab:接受当前补全候选。"""

    class MoveCompletion(Message):
        """↑/↓:在补全面板里移动。"""

        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    class FollowUp(Message):
        """alt+enter:排队 follow-up(pi 的 app.message.followUp)。"""

    class RestoreQueue(Message):
        """alt+up:把排队中的消息取回编辑器(pi 的 app.message.dequeue)。"""

    class CycleThinking(Message):
        """shift+tab:循环思考级别(pi 的 app.thinking.cycle)。

        在编辑器里接管而不是 App binding:Textual 的 `Screen` 把 shift+tab 绑给了
        `focus_previous`,非 priority 的 App 绑定抢不到,而 priority 会连带
        影响模态里的行为。
        """
    BINDINGS = [
        Binding("enter", "submit", "提交", priority=True, show=False),
        Binding("shift+enter", "newline", "换行", priority=True, show=False),
        Binding("ctrl+j", "newline", "换行", show=False),
        Binding("ctrl+b", "cursor_left", show=False),
        Binding("ctrl+f", "cursor_right", show=False),
        Binding("alt+left", "cursor_word_left", show=False),
        Binding("alt+right", "cursor_word_right", show=False),
        Binding("alt+b", "cursor_word_left", show=False),
        Binding("alt+f", "cursor_word_right", show=False),
        Binding("alt+d", "delete_word_right", show=False),
        Binding("ctrl+-", "undo", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("soft_wrap", True)
        kwargs.setdefault("tab_behavior", "indent")   # 不抢焦点
        kwargs.setdefault("show_line_numbers", False)
        super().__init__("", **kwargs)

    async def _on_key(self, event: events.Key) -> None:
        # TextArea 在 tab_behavior="indent" 下会把 escape 当「换焦点」、tab 当「缩进」
        # 并 stop 事件;补全面板开着时这三个键要归补全(pi 的 tui.input.tab / select.*)。
        panel_open = bool(getattr(self.app, "_completions_open", False))
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.post_message(self.Interrupt())
            return
        if panel_open and event.key in ("tab", "up", "down"):
            event.stop()
            event.prevent_default()
            if event.key == "tab":
                self.post_message(self.Complete())
            else:
                self.post_message(self.MoveCompletion(-1 if event.key == "up" else 1))
            return
        if event.key == "alt+enter":          # pi:排队 follow-up
            event.stop()
            event.prevent_default()
            self.post_message(self.FollowUp())
            return
        if event.key == "alt+up":             # pi:取回排队
            event.stop()
            event.prevent_default()
            self.post_message(self.RestoreQueue())
            return
        if event.key == "shift+tab":          # pi:循环思考级别
            event.stop()
            event.prevent_default()
            self.post_message(self.CycleThinking())
            return
        await super()._on_key(event)

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self.text))

    def action_newline(self) -> None:
        self.insert("\n")

    @property
    def value(self) -> str:
        """与 Input 同名的读取口(业务代码/测试都读 value)。"""
        return self.text

    def reset(self) -> None:
        """清空(不能叫 clear:TextArea.clear 的返回类型是 EditResult)。"""
        self.load_text("")


class ModelSelector(ModalScreen[str | None]):
    """ctrl+l / `/model`:选模型(对齐 pi 的模型选择器,只列 models.json 里的)。

    返回 `"<provider>\x00<model>"`,取消返回 None。
    """

    BINDINGS = [("escape", "dismiss(None)", "取消")]

    def __init__(self, options: list[tuple[str, str, bool]]) -> None:
        super().__init__()
        self._options = options

    def compose(self) -> ComposeResult:
        items = []
        for provider, model, current in self._options:
            label = f"{provider}/{model}" + ("    ← 当前" if current else "")
            items.append(Option(label, id=f"{provider}\x00{model}"))
        with Vertical(id="model-box"):
            yield Static("选择模型(↑↓ 选择 · enter 确认 · escape 取消)", id="model-hint")
            yield OptionList(*items, id="model-list")

    def on_mount(self) -> None:
        self.query_one("#model-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))


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
    #editor { border: none; height: auto; max-height: 8; padding: 0 1; background: transparent; }
    #editor .text-area--cursor-line { background: transparent; }
    /* `/` 与 `@` 补全面板(pi 的 autocomplete):默认隐藏,有候选才显示 */
    #completions { display: none; width: 1fr; height: auto; max-height: 8;
                   padding: 0 1; background: $surface; }
    #completions.visible { display: block; }
    #footer { height: auto; width: 1fr; background: transparent; scrollbar-size: 0 0; }
    /* 模型选择器(ctrl+l):模态,只在需要时出现 */
    ModelSelector { align: center middle; }
    #model-box { width: 64; max-height: 70%; background: $surface; border: round $primary;
                 padding: 0 1; }
    #model-hint { color: $text-muted; }
    #model-list { background: transparent; }
    """

    # pi 没有命令面板;Textual 默认用 ctrl+p 开面板,而 pi 的 ctrl+p = 切模型。
    ENABLE_COMMAND_PALETTE = False

    # 键位对齐 pi(`core/keybindings.js`):同名同义。
    # ctrl+c / ctrl+x / ctrl+d 加 priority=True —— Textual 的 Input 默认把它们绑到
    # 「复制/剪切/删右侧」,而 pi 里 ctrl+c = 清空编辑器、ctrl+x = 复制消息、
    # ctrl+d = 空输入框时退出,必须抢过来(非空时 ctrl+d 仍自己调 delete_right)。
    BINDINGS = [
        Binding("escape", "interrupt", "中断"),
        Binding("ctrl+c", "clear_or_exit", "清空/退出", priority=True),
        Binding("ctrl+d", "exit_or_delete", "退出", priority=True),
        Binding("ctrl+o", "toggle_expand", "展开工具"),
        Binding("ctrl+t", "toggle_thinking", "折叠思考块"),
        Binding("ctrl+x", "copy_answer", "复制回答", priority=True),
        Binding("ctrl+g", "external_editor", "外部编辑器"),
        Binding("ctrl+l", "select_model", "选择模型"),
        Binding("ctrl+p", "cycle_model", "切换模型"),
        Binding("ctrl+shift+p", "cycle_model_back", "切换模型(反向)"),
        Binding("ctrl+z", "suspend_process", "挂起"),
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
        # 消息队列(pi 的 steer / follow-up):回合进行中提交的消息不并发跑,而是排队。
        # 必须在 _default_status() 之前初始化 —— 它会把排队数写进状态行。
        self._pending_steer: list[str] = []
        self._pending_follow: list[str] = []
        self._status = self._default_status()
        self.footer_text = Text("")
        self._branch: str | None = None
        self._last_answer = ""
        self._exit_armed = False
        # 补全(pi 的 autocomplete):候选列表 + 当前替换区间
        self._completions: list[tuple[str, str]] = []
        self._completions_open = False
        self._bash_blocks: list[BashBlock] = []
        # 思考(pi 的 thinking):级别 + 是否展示思考块
        self._thinking_level = "off"
        self._show_thinking = True
        self._thinking_widgets: list[ThinkingMessage] = []
        self._live_thinking: ThinkingMessage | None = None
        self._reasoning_warned = False

    # -- 布局 -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="body"):
            yield VerticalScroll(id="log")
            yield OptionList(id="completions")
            yield Static("", id="border-top")
            yield Editor(id="editor")
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
            self._thinking_level = normalize_thinking_level(
                getattr(self._rt, "thinking_level", None))
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
        self.query_one("#editor", Editor).focus()
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
        """transcript 最多占 终端高 - (编辑器实际行数 + 上下边框 + footer),超出内部滚动。"""
        try:
            editor = self.query_one("#editor", Editor)
            log = self.query_one("#log")
        except NoMatches:  # pragma: no cover - 挂载前/卸载后的调用
            return
        reserved = self._editor_rows(editor) + 2 + FOOTER_LINES
        if self._completions_open:
            reserved += min(len(self._completions), COMPLETION_ROWS)
        log.styles.max_height = max(3, self.size.height - reserved)

    def _editor_rows(self, editor: Editor) -> int:
        """编辑器会占几行(含软换行估算)。

        transcript 的上限必须在布局前算出来,不能反查 `editor.size.height`(循环依赖),
        所以按终端宽估算包裹行数;估多了只是 transcript 少一行,估少了会把输入框挤掉。
        同时受终端高限制(矮终端下不能让编辑器把屏幕吃光)。
        """
        width = max(8, self.size.width - 2)
        rows = 0
        for line in editor.text.split("\n"):
            rows += max(1, -(-len(line) // width))
        # 终端高 - (上下边框 2 + footer 3 + transcript 至少 3) 才是编辑器的安全上限
        ceiling = max(1, min(MAX_EDITOR_ROWS, self.size.height - 8))
        return max(1, min(ceiling, rows))

    def on_resize(self) -> None:
        self._sync_log_height()
        self._repaint_borders()
        self._refresh_footer()

    def _repaint_borders(self) -> None:
        try:
            bottom = self.query_one("#border-bottom", Static)
            top = self.query_one("#border-top", Static)
        except NoMatches:      # App 正在卸载(worker 的 finally 可能跑到这里)
            return
        width = max(1, self.size.width)
        p = self._palette
        border = Style(color=p.hex("border"))
        bottom.update(Text("─" * width, style=border))
        top.update(self._top_border())

    def _top_border(self) -> Text:
        """空闲 = 整行 `─`;工作中 = `── ⠋ Working ───…`(pi 把 loader 嵌在上边框)。

        输入以 `!` / `!!` 开头时整条边框换成 `bashMode`(绿) / `dim` —— 对齐 pi 的
        `updateEditorBorderColor()`。
        """
        width = max(1, self.size.width)
        p = self._palette
        color_key = self._editor_color_key()
        border = Style(color=p.hex(color_key))
        if not self._working:
            return Text("─" * width, style=border)
        label = f" {SPINNER_FRAMES[self._frame]} Working "
        head = "── "
        rest = "─" * max(0, width - len(head) - len(label))
        line = Text(head, style=Style(color=p.hex("border")))
        line.append(SPINNER_FRAMES[self._frame], style=Style(color=p.hex("accent")))
        line.append(" Working ", style=Style(color=p.hex("muted")))
        line.append(rest, style=Style(color=p.hex("border")))
        return line

    def _editor_color_key(self) -> str:
        """边框色:`!` = bashMode(绿),`!!` = dim,否则 border。"""
        try:
            text = self.query_one("#editor", Editor).text.lstrip()
        except (NoMatches, ScreenStackError):  # 未挂载/已卸载时有纯粹的调用
            return "border"
        if text.startswith("!!"):
            return "dim"
        if text.startswith("!"):
            return "bashMode"
        return "border"

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
            level = self._thinking_level
            model_name = (f"{model_name} • thinking off" if level == "off"
                          else f"{model_name} • {level}")
        right = Text(model_name, style=dim)
        # 右对齐、放不下就截断(pi footer.js 的同款处理:否则 Text 会折行,footer 变 4 行
        # → 预留行数失真 → inline 区域把输入框挤掉)
        available = width - left.cell_len - 2
        if available <= 0:
            second = left
        else:
            if right.cell_len > available:
                right = right.copy()
                right.truncate(available, overflow="ellipsis")
            second = Text()
            second.append_text(left)
            second.append(" " * max(2, width - left.cell_len - right.cell_len))
            second.append_text(right)

        third = Text(self._status, style=Style(color=p.hex("muted")))
        third.truncate(width, overflow="ellipsis")
        first.truncate(width, overflow="ellipsis")
        self.footer_text = Text("\n").join([first, second, third])
        try:
            footer = self.query_one("#footer", Static)
        except NoMatches:      # App 正在卸载
            return
        footer.update(self.footer_text)

    # -- 输入 -----------------------------------------------------------
    def on_editor_submitted(self, event: Editor.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#editor", Editor).reset()
        self._sync_log_height()
        if not text:
            return
        if self._working:
            # 回合进行中:不并发跑第二个回合(会互踩会话),按 pi 排队
            self._enqueue(text, "steer")
            return
        self._submit(text)

    def on_editor_interrupt(self, event: Editor.Interrupt) -> None:
        """escape:先关补全面板,再考虑中断(对齐 pi:escape 先取消选择器)。"""
        if self._completions_open:
            self._close_completions()
            return
        self.action_interrupt()

    def on_editor_complete(self, event: Editor.Complete) -> None:
        self._apply_completion()

    def on_editor_move_completion(self, event: Editor.MoveCompletion) -> None:
        self._move_completion(event.delta)

    def on_editor_follow_up(self, event: Editor.FollowUp) -> None:
        """alt+enter:排队 follow-up(空闲时就直接发)。"""
        editor = self.query_one("#editor", Editor)
        text = editor.text.strip()
        if not text:
            return
        editor.reset()
        self._sync_log_height()
        if self._working:
            self._enqueue(text, "follow")
        else:
            self._submit(text)

    def on_editor_restore_queue(self, event: Editor.RestoreQueue) -> None:
        self._restore_queue()

    def on_editor_cycle_thinking(self, event: Editor.CycleThinking) -> None:
        self.action_cycle_thinking()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """编辑器变高/变矮、进入/退出 bash 模式时,同步布局与边框色。"""
        if event.text_area.id != "editor":
            return
        self._repaint_borders()
        self._refresh_completions()
        self._sync_log_height()

    def _submit(self, text: str) -> None:
        if text.startswith("!"):
            self._run_bash(text)
            return
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
                elif ev.kind == "thinking_delta":
                    if self._live_thinking is None:
                        self._live_thinking = ThinkingMessage("", self._palette)
                        self._live_thinking.display = self._show_thinking
                        self._thinking_widgets.append(self._live_thinking)
                        self._append(self._live_thinking)
                    self._live_thinking.append_delta(ev.text)
                    self._scroll_end()
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
                    self._live_thinking = None      # 收束思考块(保留可 ctrl+t 切换)
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
            self._live_thinking = None
            self._report_reasoning_dropped()
            self._scroll_end()
            # 回合结束再抽队列(排队消息不并发跑,避免两个回合互踩同一会话)
            self.call_after_refresh(self._drain_queue)

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
            self._pending_steer.clear()
            self._pending_follow.clear()
            self._note("已开新会话(auto)")
            self._restore_status()
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
        elif cmd == "/model":
            options = self._model_options()
            if not options:
                self._note("models.json 里没有可选模型", "warning")
            elif not arg:
                current = self._model.label if self._model else "(未解析)"
                lines = [f"当前: {current}", "可选:"]
                lines += [f"  {p}/{m}" + ("  ← 当前" if cur else "") for p, m, cur in options]
                lines.append("切换: /model <provider/model> 或 ctrl+l / ctrl+p")
                self._note("\n".join(lines), "text")
            else:
                provider, _, model = arg.partition("/")
                if not model:
                    matches = [(p, m) for p, m, _ in options if m == provider]
                    if len(matches) != 1:
                        self._note(f"用法: /model <provider>/<model>(只给模型名匹配到 {len(matches)} 个)",
                                   "warning")
                        self._scroll_end()
                        return
                    provider, model = matches[0]
                self._switch_model(provider, model)
        elif cmd == "/thinking":
            if not arg:
                lines = [f"当前: {self._thinking_level}",
                         "可选: " + " | ".join(THINKING_LEVELS)]
                if self._model is not None and not self._model.reasoning:
                    lines.append(f"注:{self._model.label} 未声明 reasoning,级别不会随请求发送")
                self._note("\n".join(lines), "text")
            elif arg.lower() not in THINKING_LEVELS:
                self._note("用法: /thinking " + "|".join(THINKING_LEVELS), "warning")
            else:
                self._set_thinking_level(arg.lower())
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
            self._copy_answer()
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
                self._restore_status()
            else:
                self._note("用法: /mode auto|manual", "warning")
        elif cmd == "/agent":
            if arg and rt is not None and rt.registry.get(arg):
                self._agent = arg
                self._auto = False
                self._note(f"锁定 agent: {arg}(manual)")
                self._restore_status()
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

    # -- 动作(键位对齐 pi `core/keybindings.js`)-------------------------
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
        for bash in self._bash_blocks:
            bash.set_expanded(self._expanded)
        self._flash("工具输出:" + ("已展开" if self._expanded else "已折叠"))

    # -- 思考级别(pi 的 app.thinking.*)----------------------
    def action_cycle_thinking(self) -> None:
        """shift+tab:循环思考级别(off→minimal→low→…→max→off)。"""
        index = (THINKING_LEVELS.index(self._thinking_level)
                 if self._thinking_level in THINKING_LEVELS else 0)
        self._set_thinking_level(THINKING_LEVELS[(index + 1) % len(THINKING_LEVELS)])

    def action_toggle_thinking(self) -> None:
        """ctrl+t:显示/隐藏思考块(对齐 pi 的 app.thinking.toggle)。"""
        self._show_thinking = not self._show_thinking
        for widget in self._thinking_widgets:
            widget.display = self._show_thinking
        self._flash("思考块:" + ("显示" if self._show_thinking else "隐藏"))

    def _report_reasoning_dropped(self) -> None:
        """provider 拒绝了 reasoning_effort 时提示一次(别让用户以为级别生效了)。"""
        if self._reasoning_warned or self._rt is None:
            return
        # 用 getattr:这个函数在 worker 的 finally 里跑,抛异常会把整轮弄挂
        client = getattr(self._rt, "llm_exec", None)
        if isinstance(client, ThinkingLLMClient) and client.reasoning_dropped:
            self._reasoning_warned = True
            self._flash("该 provider 不接受 reasoning_effort,已按不思考运行", 4.0)

    def _set_thinking_level(self, level: str) -> None:
        """设置级别:下一回合生效(写到 runtime 的 llm_exec 上,与切模型同一处)。"""
        self._thinking_level = normalize_thinking_level(level)
        client = getattr(self._rt, "llm_exec", None) if self._rt is not None else None
        # 可选能力:测试替身/第三方实现可能连 llm_exec 都没有
        if isinstance(client, ThinkingLLMClient):
            client.thinking_level = self._thinking_level
        self._refresh_footer()
        notice = ""
        if self._model is not None and not self._model.reasoning:
            notice = "(当前模型未声明 reasoning,不会随请求发送)"
        self._flash(f"思考级别: {self._thinking_level}{notice}")

    def action_interrupt(self) -> None:
        """escape:中断当前回合(对齐 pi 的 app.interrupt)。排队消息退回编辑器。"""
        if not self._working:
            return
        self.workers.cancel_all()
        self._set_working(False)
        self._live = None
        if self._queue_count():
            self._restore_queue()
        else:
            self._flash("已中断")

    def action_clear_or_exit(self) -> None:
        """ctrl+c:清空编辑器;连按两次退出 —— 对齐 pi 的 app.clear/app.exit。"""
        editor = self.query_one("#editor", Editor)
        if editor.text:
            editor.reset()
            self._sync_log_height()
            self._arm_exit()
            return
        if self._exit_armed:
            self.exit()
        else:
            self._arm_exit()

    def _arm_exit(self) -> None:
        self._exit_armed = True
        self._flash("再按一次 ctrl+c 退出")
        self.set_timer(2.0, self._disarm_exit)

    def _disarm_exit(self) -> None:
        self._exit_armed = False

    def action_exit_or_delete(self) -> None:
        """ctrl+d:编辑器为空时退出,非空时删除右侧字符(对齐 pi 的 app.exit)。"""
        editor = self.query_one("#editor", Editor)
        if editor.text:
            editor.action_delete_right()
        else:
            self.exit()

    def action_copy_answer(self) -> None:
        """ctrl+x:复制最后一条回答(对齐 pi 的 app.message.copy)。"""
        self._copy_answer()

    def _copy_answer(self) -> None:
        if not self._last_answer.strip():
            self._flash("还没有回答可复制")
            return
        self.copy_to_clipboard(self._last_answer)
        self._flash("已复制最后一条回答")

    def action_external_editor(self) -> None:
        """ctrl+g:用 $EDITOR 编辑当前输入(对齐 pi 的 app.editor.external)。"""
        editor = self.query_one("#editor", Editor)
        command = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        handle = tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False)
        path = Path(handle.name)
        try:
            handle.write(editor.text)
            handle.close()
            with self.suspend():
                subprocess.call([*shlex.split(command), str(path)])
            editor.load_text(path.read_text(encoding="utf-8").strip())
            self._sync_log_height()
        except OSError as exc:
            self._flash(f"外部编辑器不可用: {exc}")
        finally:
            path.unlink(missing_ok=True)

    # -- 模型(ctrl+l / ctrl+p;对齐 pi 的 app.model.*)----------------
    def _model_options(self) -> list[tuple[str, str, bool]]:
        rt = self._rt
        if rt is None:
            return []
        current = self._model
        out: list[tuple[str, str, bool]] = []
        providers = getattr(getattr(rt, "cfg", None), "providers", None) or {}
        for provider, prov in sorted(providers.items()):
            for entry in prov.models:
                is_current = bool(current and current.provider == provider
                                  and current.model == entry.id)
                out.append((provider, entry.id, is_current))
        return out

    def _switch_model(self, provider: str, model: str) -> None:
        """运行期换模型:换掉 runtime 的 llm_exec,下一回合生效。"""
        rt = self._rt
        if rt is None:
            return
        try:
            resolved = resolve_model(rt.cfg, provider, model)
            rt.llm_exec = LiteLLMClient(resolved, AuthStore())
        except Exception as exc:  # 配置/凭证异常不该把 TUI 弄崩
            self._note(f"切换模型失败: {exc}", "error")
            self._scroll_end()
            return
        self._model = resolved
        self._refresh_footer()
        self._flash(f"模型: {resolved.label}")

    def action_select_model(self) -> None:
        """ctrl+l:模型选择器(对齐 pi 的 app.model.select)。"""
        options = self._model_options()
        if not options:
            self._flash("models.json 里没有可选模型")
            return

        def picked(value: str | None) -> None:
            if value:
                provider, _, model = value.partition("\x00")
                self._switch_model(provider, model)

        self.push_screen(ModelSelector(options), picked)

    def action_cycle_model(self) -> None:
        self._cycle_model(1)

    def action_cycle_model_back(self) -> None:
        self._cycle_model(-1)

    def _cycle_model(self, step: int) -> None:
        options = self._model_options()
        if not options:
            self._flash("models.json 里没有可选模型")
            return
        flat = [(provider, model) for provider, model, _ in options]
        current = (self._model.provider, self._model.model) if self._model else None
        index = flat.index(current) if current in flat else -1
        provider, model = flat[(index + step) % len(flat)]
        self._switch_model(provider, model)

    # -- 补全(pi 的 autocomplete:`/` 命令与 `@` 文件)------------
    def _completion_candidates(self) -> tuple[list[tuple[str, str]], int, int]:
        """根据光标前的 token 给出候选。

        返回 `(candidates [(value, label)], start, end)`,start/end 是要被替换的区间。
        规则对齐 pi:
          · 行首的 `/xxx`(不含第二个 `/`)= 命令名补全
          · 当前 token 以 `@` 开头 = 相对路径补全
        """
        editor = self.query_one("#editor", Editor)
        text = editor.text
        row, col = editor.cursor_location
        lines = text.split("\n")
        if row >= len(lines):
            return [], 0, 0
        line = lines[row]
        col = min(col, len(line))
        before = line[:col]

        # 1) 命令补全:行首 /xxx,且还没输入空格或第二个 /
        if before.startswith("/") and " " not in before and "/" not in before[1:]:
            prefix = before
            items = [(name, detail) for name, detail in sorted(TUI_COMMANDS.items())
                     if name.startswith(prefix)]
            if len(items) == 1 and items[0][0] == prefix:
                items = []          # 已完整匹配,不必再提示
            row_start = len("\n".join(lines[:row])) + (row > 0)
            return items, row_start, row_start + col

        # 2) 文件补全:当前 token 以 @ 开头
        token_start = max(before.rfind(" ") + 1, before.rfind("\t") + 1, 0)
        token = before[token_start:]
        if not token.startswith("@"):
            return [], 0, 0
        query = token[1:]
        row_start = len("\n".join(lines[:row])) + (row > 0)
        items = self._file_candidates(query)
        return items, row_start + token_start, row_start + col

    def _file_candidates(self, query: str) -> list[tuple[str, str]]:
        """按 `@` 后的相对路径列目录(目录优先,以 `/` 结尾)。

        没装 fd 也能用:直接扫描目录,不做 .gitignore 过滤(pi 用 fd)。
        """
        if self._rt is None:
            return []
        base = Path(self._rt.cwd)
        query_path = Path(query) if query else Path("")
        if query.endswith("/") or query == "":
            directory, stem = base / query_path, ""
        else:
            directory, stem = base / query_path.parent, query_path.name
        show_hidden = stem.startswith(".")
        try:
            entries = sorted(directory.iterdir(),
                             key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return []
        out: list[tuple[str, str]] = []
        for entry in entries:
            if entry.name.startswith(".") and not show_hidden:
                continue
            if not entry.name.startswith(stem):
                continue
            if len(out) >= COMPLETION_ROWS:
                break
            rel = entry.relative_to(base).as_posix()
            is_dir = entry.is_dir()
            value = f"@{rel}" + ("/" if is_dir else "")
            out.append((value, rel + ("/" if is_dir else "")))
        return out

    def _refresh_completions(self) -> None:
        """重算候选并同步面板显隐(不改文本,只负责菜单)。"""
        try:
            panel = self.query_one("#completions", OptionList)
        except NoMatches:  # pragma: no cover
            return
        candidates, _, _ = self._completion_candidates()
        self._completions = candidates
        if not candidates:
            self._completions_open = False
            panel.remove_class("visible")
            panel.clear_options()
            self._sync_log_height()
            return
        panel.clear_options()
        for value, label in candidates:
            detail = TUI_COMMANDS.get(value, "")
            text = f"{label}    {detail}" if detail else label
            panel.add_option(Option(text, id=value))
        panel.highlighted = 0
        panel.add_class("visible")
        self._completions_open = True
        self._sync_log_height()

    def _move_completion(self, step: int) -> None:
        panel = self.query_one("#completions", OptionList)
        count = panel.option_count
        if not count:
            return
        current = panel.highlighted if panel.highlighted is not None else 0
        panel.highlighted = (current + step) % count

    def _close_completions(self) -> None:
        self._completions_open = False
        self._completions = []
        try:
            panel = self.query_one("#completions", OptionList)
        except NoMatches:  # pragma: no cover
            return
        panel.remove_class("visible")
        panel.clear_options()
        self._sync_log_height()

    def _apply_completion(self) -> None:
        """tab:把选中候选写回编辑器(替换当前 token)。"""
        candidates, start, end = self._completion_candidates()
        if not candidates:
            self._close_completions()
            return
        panel = self.query_one("#completions", OptionList)
        index = panel.highlighted if panel.highlighted is not None else 0
        value = candidates[min(index, len(candidates) - 1)][0]
        editor = self.query_one("#editor", Editor)
        text = editor.text
        # 命令补全补一个空格(pi 同款:`/name `),目录补全保留 `/` 继续往下补
        suffix = " " if value.startswith("/") else ""
        new_text = text[:start] + value + suffix + text[end:]
        editor.load_text(new_text)
        cursor = start + len(value) + len(suffix)
        editor.move_cursor(self._offset_to_location(new_text, cursor))
        if value.endswith("/"):
            self._refresh_completions()
        else:
            self._close_completions()

    @staticmethod
    def _offset_to_location(text: str, offset: int) -> tuple[int, int]:
        """字符偏移 → TextArea 的 (row, col)。"""
        offset = max(0, min(offset, len(text)))
        row = text.count("\n", 0, offset)
        line_start = text.rfind("\n", 0, offset) + 1
        return row, offset - line_start

    # -- `!` / `!!` 手动 bash(pi 的 handleBashCommand)----------
    def _run_bash(self, text: str) -> None:
        excluded = text.startswith("!!")
        command = (text[2:] if excluded else text[1:]).strip()
        if not command:
            self._note("用法: !<命令>(!! 同样执行,但输出不进上下文)", "warning")
            self._scroll_end()
            return
        block = BashBlock(command, self._palette, excluded)
        self._bash_blocks.append(block)
        self._append(block)
        self.run_worker(self._exec_bash(command, excluded, block), exclusive=False)

    async def _exec_bash(self, command: str, excluded: bool, block: BashBlock) -> None:
        """执行用户手敲的命令;`!` 会把「命令 + 输出」落成一条 user 消息供后续回合参考。"""
        rt = self._rt
        cwd = str(rt.cwd) if rt is not None else str(Path.cwd())
        try:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            raw, _ = await proc.communicate()
            code = proc.returncode
        except OSError as exc:
            block.set_result(f"无法执行: {exc}", -1)
            self._scroll_end()
            return
        output = raw.decode("utf-8", "replace")
        block.set_result(output, code)
        self._scroll_end()
        if excluded or rt is None or self._session is None:
            return
        body = output.strip() or "(无输出)"
        if len(body) > MAX_TOOL_ENTRY_CHARS:
            body = body[:MAX_TOOL_ENTRY_CHARS] + "\n…(已截断)"
        content = (f"[用户手动执行 bash]\n$ {command}\n{body}\n(exit {code})")
        rt.sessions.append(self._session,
                           {"type": "message", "role": "user", "content": content})

    # -- 消息队列(pi 的 steer / follow-up)--------------------
    def _queue_count(self) -> int:
        return len(self._pending_steer) + len(self._pending_follow)

    def _enqueue(self, text: str, kind: str) -> None:
        """排队一条消息。steer 先送达,follow-up 排在后面(不要乱序)。"""
        if kind == "follow":
            self._pending_follow.append(text)
        else:
            self._pending_steer.append(text)
        label = "follow-up" if kind == "follow" else "当前回合结束后发送"
        self._note(f"已排队({label}):{text}", "dim")
        self._restore_status()
        self._scroll_end()

    def _drain_queue(self) -> None:
        """回合结束后,把队首那条发出去(steer 优先)。"""
        if self._working or not self.is_running:
            return
        if self._pending_steer:
            text = self._pending_steer.pop(0)
        elif self._pending_follow:
            text = self._pending_follow.pop(0)
        else:
            return
        self._restore_status()
        self._submit(text)

    def _restore_queue(self) -> None:
        """alt+up:把排队的消息整段放回编辑器(不动正在跑的那轮)。"""
        queued = [*self._pending_steer, *self._pending_follow]
        if not queued:
            self._flash("没有排队的消息")
            return
        self._pending_steer.clear()
        self._pending_follow.clear()
        editor = self.query_one("#editor", Editor)
        current = editor.text.rstrip("\n")
        restored = "\n".join([current, *queued]) if current else "\n".join(queued)
        editor.load_text(restored)
        editor.move_cursor(self._offset_to_location(restored, len(restored)))
        self._sync_log_height()
        self._restore_status()
        self._flash(f"已取回 {len(queued)} 条排队消息")

    # -- 底部状态行的瞬时提示(pi 的状态区,不加额外 chrome)----------
    def _default_status(self) -> str:
        mode = "auto" if self._auto else f"manual:{self._agent or '-'}"
        status = f"qi · {mode}"
        if self._queue_count():
            status += f" · 排队 {self._queue_count()}"
        return status

    def _flash(self, message: str, seconds: float = 2.0) -> None:
        self._status = message
        self._refresh_footer()
        self.set_timer(seconds, self._restore_status)

    def _restore_status(self) -> None:
        self._status = self._default_status()
        self._refresh_footer()


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
