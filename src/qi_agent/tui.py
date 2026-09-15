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
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple, cast

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
from .settings import double_escape_action
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
  /tree              会话树:跳到任意节点继续(同文件内分支)
  /fork [序号|id]     从某条用户消息 fork 出新会话(消息放回编辑器)
  /clone [名字]       复制当前分支为新会话
  /name <名字>       设置会话显示名(进 footer)
  /session           会话信息(文件/ID/消息数/模型/用量)
  /thinking [级别]   思考级别(off|minimal|low|medium|high|xhigh|max;等同 shift+tab)
  /model [p/m]       当前模型 / 切换模型(等同 ctrl+l)
  /export [文件]     导出会话 JSONL(默认 ./qi-<id>.jsonl)
  /compact [提示]    压缩上下文:把旧消息压成摘要(可给一句关注点)
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
  /scoped-models /settings /share /trust
"""

PLANNED_COMMANDS = frozenset({
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
    "/tree": "跳到本会话的任意节点",
    "/fork": "从某条用户消息 fork 出新会话",
    "/clone": "复制当前分支为新会话",
    "/model": "当前/切换模型",
    "/thinking": "思考级别(off|minimal|low|medium|high)",
    "/export": "导出会话 JSONL",
    "/import": "从 JSONL 导入会话",
    "/reload": "重载 agents/插件/配置",
    "/copy": "复制最后一条回答",
    "/login": "登录指引(密钥不进会话)",
    "/logout": "删除已存凭证",
    "/changelog": "显示 CHANGELOG.md",
    "/compact": "压缩上下文(摘要旧消息)",
    "/agents": "列出 agent",
    "/mode": "切换分派模式",
    "/agent": "manual 锁定执行 agent",
    "/tools": "工具清单",
}
"""`/` 补全的候选(命令 → 说明);与 `_command` 的已实现分支一一对应。"""

COMPLETION_ROWS = 8
"""补全面板最多显示几行。"""

DOUBLE_ESCAPE_WINDOW = 0.5
"""空编辑器连按两次 escape 的判定窗口(秒);pi 用 500ms。"""


class Candidate(NamedTuple):
    """补全候选(对齐 pi 的 AutocompleteItem:value/label/description + 来源标签)。

    `source` 为空 = 内置;非空时按 pi 的写法在补全面板里前置 `[u]/[p]/[t]`
    (user / project / third-party)—— 将来 prompt template 与插件命令带着它进来。
    """

    value: str          # 写回编辑器的文本
    label: str          # 面板里显示的名字
    detail: str = ""    # 右侧说明
    source: str = ""    # 来源标签(空 = 内置)


ARG_COMPLETION_COMMANDS = ("/model", "/thinking", "/login")
"""参数补全(对齐 pi 的 `getArgumentCompletions`):这些命令的第一个参数给候选。"""

PATH_DELIMITERS = (" ", "\t", '"', "'", "=")
"""`@路径` token 的分隔符(对齐 pi autocomplete 的 PATH_DELIMITERS)。"""


def _which_fd() -> str | None:
    """`fd` 可执行文件路径(补全走全树搜索用);没装就回退扫目录。"""
    import shutil as _shutil

    return _shutil.which("fd")


def _is_token_start(text: str, index: int) -> bool:
    return index == 0 or text[index - 1] in PATH_DELIMITERS


def _last_delimiter(text: str) -> int:
    for i in range(len(text) - 1, -1, -1):
        if text[i] in PATH_DELIMITERS:
            return i
    return -1


def _unclosed_quote_start(text: str) -> int | None:
    """未闭合的 `"` 的起点(在引号里继续补)。"""
    inside = False
    start: int | None = None
    for i, char in enumerate(text):
        if char == '"':
            inside = not inside
            start = i if inside else None
    return start if inside else None


def _at_prefix(text: str) -> tuple[int, str, bool] | None:
    """从光标前的文本里取出 `@路径` 片段。

    返回 `(起始偏移, @ 后的原始查询, 是否带引号)`。对齐 pi 的 extractAtPrefix +
    extractQuotedPrefix:`@"带 空格"` 与未闭合的 `@"…` 都算带引号。
    """
    quote = _unclosed_quote_start(text)
    if quote is not None:
        if quote > 0 and text[quote - 1] == "@" and _is_token_start(text, quote - 1):
            return quote - 1, text[quote + 1:], True
        return None
    start = _last_delimiter(text) + 1
    if text[start:start + 1] == "@":
        return start, text[start + 1:], False
    return None


def _completion_value(display: str, is_dir: bool, quoted: bool) -> str:
    """`@` 候选写回编辑器的文本:带空格(或已在引号里)就补上成对引号。"""
    path = display + ("/" if is_dir else "")
    if quoted or " " in path:
        return f'@"{path}"'
    return f"@{path}"


def _fd_pattern(query: str) -> str:
    """`@a/b` → fd 正则 `a[\\/]b`(pi 的 buildFdPathQuery)。"""
    trailing = query.endswith("/")
    segments = [re.escape(part) for part in query.strip("/").split("/") if part]
    pattern = "[\\\\/]".join(segments)
    return pattern + "[\\\\/]" if trailing else pattern


def _fd_candidates(base: Path, query: str) -> list[tuple[str, bool]]:
    """用 fd 走全树(快、尊重 .gitignore);返回 `(相对路径, 是否目录)`,目录优先。"""
    fd = _which_fd()
    if not fd:
        return []
    args = [fd, "--base-directory", str(base), "--max-results", "100",
            "--type", "f", "--type", "d", "--follow", "--hidden",
            "--exclude", ".git", "--exclude", ".git/*", "--exclude", ".git/**"]
    if query:
        if "/" in query:
            args.append("--full-path")
        args.append(_fd_pattern(query))
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):   # fd 挂了不该弄崩补全
        return []
    if proc.returncode != 0:
        return []
    out: list[tuple[str, bool]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        is_dir = line.endswith("/")
        rel = line[:-1] if is_dir else line
        if rel == ".git" or rel.startswith(".git/") or "/.git/" in rel:
            continue
        out.append((rel, is_dir))
    out.sort(key=lambda item: (not item[1], item[0].lower()))
    return out


def _scan_candidates(base: Path, query: str) -> list[tuple[str, bool]]:
    """没装 fd 时的回退:只扫 query 所在的一层目录(目录优先、默认不列隐藏文件)。"""
    query_path = Path(query) if query else Path("")
    if query.endswith("/") or query == "":
        directory, stem = base / query_path, ""
    else:
        directory, stem = base / query_path.parent, query_path.name
    show_hidden = stem.startswith(".")
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return []
    out: list[tuple[str, bool]] = []
    for entry in entries:
        if entry.name.startswith(".") and not show_hidden:
            continue
        if not entry.name.startswith(stem):
            continue
        try:
            display = entry.relative_to(base).as_posix()
        except ValueError:                    # query 指到 cwd 外面(如 @/tmp/x):用绝对路径
            display = entry.as_posix()
        out.append((display, entry.is_dir()))
    return out

BASH_PREVIEW_LINES = 20
"""bash 输出折叠时的预览行数(对齐 pi 的 BashExecutionComponent)。"""

HOTKEYS_TEXT = """\
快捷键(对齐 pi 的部分)

  输入(多行编辑器,对齐 pi-tui/components/editor.js)
  enter                   提交
  shift+enter / ctrl+j    换行
  tab                     补全:行首 `/` = 命令、`@` = 相对路径;无候选时 = 缩进
  ↑ / ↓                   补全面板开着时选候选;行首/空编辑器时翻输入历史,否则移动光标
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
  escape ×2               空编辑器连按两次:settings.doubleEscapeAction(默认 tree / fork / none)
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


class CompactionBlock(Vertical):
    """压缩 / 分支摘要块(对齐 pi 的 compaction-summary-message)。

    `customMessageBg` 底色块:`[compaction]` / `[branch]` 标签 + 一行折叠提示,
    ctrl+o 展开后显示摘要正文(markdown)。
    """

    def __init__(self, summary: str, tokens_before: int, palette: Palette,
                 kind: str = "compaction") -> None:
        super().__init__(classes="msg compaction-msg")
        self.summary = summary
        self.tokens_before = tokens_before
        self.kind = kind
        self._palette = palette
        self._expanded = False
        self.body_plain = ""
        self.styles.background = palette.hex("customMessageBg")
        self.styles.padding = (1, 1)

    def compose(self) -> ComposeResult:
        p = self._palette
        label = "[compaction]" if self.kind == "compaction" else "[branch]"
        yield Static(Text(label, style=Style(color=p.hex("customMessageLabel"), bold=True)),
                     classes="compaction-label")
        yield Static("", classes="compaction-body")

    def on_mount(self) -> None:
        self.set_expanded(self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        p = self._palette
        if not expanded:
            self.body_plain = f"Compacted from {self.tokens_before:,} tokens (ctrl+o to expand)"
        else:
            prefix = (f"**Compacted from {self.tokens_before:,} tokens**\n\n"
                      if self.kind == "compaction" else "")
            self.body_plain = prefix + self.summary
        try:
            body = self.query_one(".compaction-body", Static)
        except NoMatches:      # pragma: no cover - 挂载前调用
            return
        if not expanded:
            body.update(Text(self.body_plain, style=Style(color=p.hex("customMessageText"))))
            return
        body.update(RichMarkdown(self.body_plain, code_theme=cast(Any, syntax_theme(p))))


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

    输入历史(pi 的 `addToHistory` / `navigateHistory`):无补全面板时 `↑` 取回上一条
    提交过的文本(对话与 `!` bash,内置命令不记),`↓` 往回走;首次翻历史时留住当前
    草稿,手动改动即退出浏览。
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
        # 输入历史(对齐 pi-tui editor 的 history/historyIndex/historyDraft)
        self._history: list[str] = []
        self._history_index = -1          # -1 = 没在翻历史
        self._history_draft: str | None = None
        self._history_applied: str | None = None   # 历史/草稿刚写回去的文本

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
        if not panel_open and event.key in ("up", "down") and self._history_key(event.key):
            event.stop()
            event.prevent_default()
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

    # -- 输入历史(对齐 pi 的 editor.addToHistory / navigateHistory)------
    def add_to_history(self, text: str) -> None:
        """提交成功后入历史:去空、去连续重复、上限 100 条(pi 同款)。"""
        trimmed = text.strip()
        if not trimmed:
            return
        if self._history and self._history[0] == trimmed:
            return
        self._history.insert(0, trimmed)
        del self._history[100:]

    def navigate_history(self, direction: int) -> None:
        """direction: -1 = 更旧(↑),+1 = 更新(↓)。"""
        if not self._history:
            return
        new_index = self._history_index - direction   # ↑(-1)让索引变大 = 更旧
        if new_index < -1 or new_index >= len(self._history):
            return
        if self._history_index == -1 and new_index >= 0:
            self._history_draft = self.text           # 首次进入:留住草稿
        self._history_index = new_index
        if new_index == -1:
            draft = self._history_draft or ""
            self._history_draft = None
            self._set_history_text(draft, at_start=False)
        else:
            self._set_history_text(self._history[new_index], at_start=direction == -1)

    def _set_history_text(self, text: str, *, at_start: bool) -> None:
        """写回历史/草稿,并把光标放行首(↑)或行尾(↓)—— pi 同款。"""
        self._history_applied = text
        self.load_text(text)
        if at_start:
            self.move_cursor((0, 0))
        else:
            self.move_cursor((text.count("\n"), len(text.rsplit("\n", 1)[-1])))

    def _history_key(self, key: str) -> bool:
        """↑/↓ 是否该翻历史(返回 True = 已消费)。

        pi 的规则:首行上移且(编辑器为空 或 正在翻历史 或 光标在第 0 列)→ 翻历史;
        已在历史里时 ↓ 回退。其余情况交给 TextArea 移动光标。
        """
        if key == "up":
            row, col = self.cursor_location
            if row == 0 and (not self.text or self._history_index > -1 or col == 0):
                self.navigate_history(-1)
                return True
            return False
        if self._history_index > -1:
            self.navigate_history(1)
            return True
        return False

    def _exit_history_browsing(self) -> None:
        self._history_index = -1
        self._history_draft = None
        self._history_applied = None

    @property
    def history_browsing(self) -> bool:
        """正在翻历史时不要在历史文本上弹补全面板(否则 ↑/↓ 会被面板抢走)。"""
        return self._history_index > -1

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """用户手动改动就退出历史浏览(对齐 pi:任何编辑都 exitHistoryBrowsing)。"""
        if event.text_area.id != "editor":
            return
        if self._history_index > -1 and self.text != self._history_applied:
            self._exit_history_browsing()

    def action_newline(self) -> None:
        self.insert("\n")

    @property
    def value(self) -> str:
        """与 Input 同名的读取口(业务代码/测试都读 value)。"""
        return self.text

    def reset(self) -> None:
        """清空(不能叫 clear:TextArea.clear 的返回类型是 EditResult)。"""
        self._exit_history_browsing()
        self.load_text("")


class PickerScreen(ModalScreen[str | None]):
    """通用选择器(模型 / 会话树 / fork 点共用同一套模态外壳)。

    返回选中的 `value`(entry id / `provider\x00model`);取消返回 None。
    """

    BINDINGS = [("escape", "dismiss(None)", "取消")]

    def __init__(self, title: str, options: list[tuple[str, str]],
                 current: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._current = current

    def compose(self) -> ComposeResult:
        items = []
        for value, label in self._options:
            if self._current is not None and value == self._current:
                label += "    ← 当前"
            items.append(Option(label, id=value))
        with Vertical(id="model-box"):
            yield Static(self._title, id="model-hint")
            yield OptionList(*items, id="model-list")

    def on_mount(self) -> None:
        listing = self.query_one("#model-list", OptionList)
        listing.focus()
        if self._current is not None:
            for index, (value, _) in enumerate(self._options):
                if value == self._current:
                    listing.highlighted = index
                    break

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))


class ModelSelector(PickerScreen):
    """ctrl+l / `/model`:选模型(对齐 pi 的模型选择器,只列 models.json 里的)。"""

    def __init__(self, options: list[tuple[str, str, bool]]) -> None:
        entries: list[tuple[str, str]] = []
        current: str | None = None
        for provider, model, is_current in options:
            value = f"{provider}\x00{model}"
            entries.append((value, f"{provider}/{model}"))
            if is_current:
                current = value
        super().__init__("选择模型(↑↓ 选择 · enter 确认 · escape 取消)", entries,
                         current=current)


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
    .compaction-label, .compaction-body { background: transparent; height: auto; }
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
                 palette: Palette | None = None, session_id: str | None = None,
                 cont: bool = False, fork_id: str | None = None,
                 no_session: bool = False, name: str | None = None):
        super().__init__()
        self._rt = runtime
        self._initial_prompt = (initial_prompt or "").strip() or None
        # 会话选择(对齐 CLI/pi:`qi -c` / `--session` / `--fork` / `-n` / `--no-session`)
        self._want_session_id = session_id
        self._want_cont = cont
        self._want_fork_id = fork_id
        self._want_no_session = no_session
        self._want_name = name
        self._startup_note = ""
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
        self._last_escape = 0.0              # 双击 escape(pi 的 doubleEscapeAction)
        # 补全(pi 的 autocomplete):候选列表 + 当前替换区间
        self._completions: list[Candidate] = []
        self._completions_open = False
        self._bash_blocks: list[BashBlock] = []
        self._compaction_blocks: list[CompactionBlock] = []
        self._compacting = False
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
            self._select_session()
            try:
                self._model = resolve_default_model(self._rt.cfg, self._rt.cwd)
            except ConfigError:
                self._model = None
            self._thinking_level = normalize_thinking_level(
                getattr(self._rt, "thinking_level", None))
            skills = sorted({s.name for unit in self._rt.registry.all() for s in unit.skills})
            banner = self._renderer.banner(_version(), self._rt.registry.names, skills)
            if self._session is not None and self._session.branch():
                self._replay_branch(self._session, banner=banner)   # 恢复历史(banner 在最上)
            else:
                self._append(Static(banner, classes="msg"))
            if self._startup_note:
                tone = "warning" if "不存在" in self._startup_note else "dim"
                self._note(self._startup_note, tone)
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
        except (NoMatches, ScreenStackError):  # 卸载竞态:没什么可滚的
            return

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
        editor = self.query_one("#editor", Editor)
        editor.reset()
        self._sync_log_height()
        if not text:
            return
        # pi 只把对话与 bash 记进输入历史,内置命令不入 —— 否则 ↑ 全被 /tree 之类古满
        if not text.startswith("/"):
            editor.add_to_history(text)
        # 命令(`/x`)与 bash(`!x`)回合进行中也**立即执行** —— pi 就是这样,
        # 否则 `/quit` 这种命令会被推到回合结束后,等于按不下去。
        if text.startswith("/") or text.startswith("!"):
            self._submit(text)
            return
        if self._working:
            # 普通对话:不并发跑第二个回合(会互踩会话),按 pi 排队
            self._enqueue(text, "steer")
            return
        self._submit(text)

    def on_editor_interrupt(self, event: Editor.Interrupt) -> None:
        """escape:先关补全面板;空编辑器连按两次触发 settings.doubleEscapeAction。"""
        if self._completions_open:
            self._close_completions()
            return
        editor = self.query_one("#editor", Editor)
        if not editor.text.strip() and not self._working:
            action = double_escape_action(getattr(self._rt, "settings", None))
            if action != "none":
                now = time.monotonic()
                if now - self._last_escape < DOUBLE_ESCAPE_WINDOW:
                    self._last_escape = 0.0
                    if action == "tree":
                        self.action_show_tree()
                    else:
                        self._command("/fork")
                    return
                self._last_escape = now
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
        if self.query_one("#editor", Editor).history_browsing:
            self._close_completions()      # 翻历史时不弹面板,否则 ↑/↓ 会被面板抢走
        else:
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
            self._session = self._session_store().create("tui", cwd=rt.cwd)
        override = None if self._auto else self._agent
        self.run_worker(self._run(text, override), exclusive=False)

    # -- 事件循环 -------------------------------------------------------
    async def _run(self, text: str, override: str | None) -> None:
        if self._rt is None or self._session is None:      # 防御:worker 可能在切换会话后跑
            return
        runtime, session = self._rt, self._session
        self._live = None
        renderer = self._renderer
        self._set_working(True)
        try:
            async for ev in runtime.stream(text, session, agent_override=override):
                if ev.kind == "dispatch":
                    self._shown_name = str(ev.data.get("display_name") or ev.agent or "?")
                    self._append(Static(renderer.dispatch_line(ev.agent, ev.data), classes="msg"))
                elif ev.kind == "opening":
                    self._append(Static(Text(ev.text, style=Style(color=self._palette.hex("muted"))),
                                        classes="msg"))
                elif ev.kind == "compaction_start":
                    self._compacting = True
                    self._flash(str(ev.text or "正在压缩上下文…"), 120)
                elif ev.kind == "compaction_end":
                    self._compacting = False
                    entry = ev.data.get("entry") or {}
                    self._append_compaction(entry or {"summary": ev.text,
                                                      "tokensBefore": ev.data.get("tokensBefore")})
                    self._flash("已自动压缩上下文")
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
        store = self._session_store()

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
            self._agent = None
            self._auto = True
            self._switch_session(store.create("tui", cwd=rt.cwd), note="已开新会话(auto)")
        elif cmd in ("/resume", "/sessions"):
            sessions = store.list()[:10]
            if cmd == "/resume" and arg:
                s = store.get(arg)
                if s is None:
                    self._note(f"会话不存在 {arg}", "error")
                else:
                    self._switch_session(s, note=f"已恢复 {s.id}(分支 {s.message_count} 条消息)")
            else:
                rows = ["会话(最新在前):"]
                rows += [(f"  {s.id}  {s.title or '(未命名)'}  {s.created_at}"
                          + (f"  分支点×{s.branch_points}" if s.branch_points else ""))
                         for s in sessions]
                self._note("\n".join(rows) if sessions else "(无会话)")
                if sessions and cmd == "/resume":
                    self._note("用 /resume <id> 恢复")
        elif cmd == "/tree":
            self.action_show_tree()
            self._scroll_end()
            return
        elif cmd == "/fork":
            session = self._session
            if session is None:
                self._note("当前没有会话", "warning")
            else:
                options = self._user_message_options(session)
                if not options:
                    self._note("这个会话还没有用户消息可 fork", "warning")
                elif arg:
                    target = self._resolve_user_message(session, arg)
                    if target is None:
                        self._note(f"找不到那条用户消息:{arg}(共 {len(options)} 条)", "warning")
                    else:
                        self._fork_from(session, target)
                else:
                    self.push_screen(
                        PickerScreen("从哪条用户消息 fork(选中后重新提问)", options),
                        lambda value: self._fork_from(session, value) if value else None)
                    self._scroll_end()
                    return
        elif cmd == "/clone":
            self._clone_session(arg)
        elif cmd == "/compact":
            session = self._session
            if session is None:
                self._note("当前没有会话", "warning")
            elif self._working:
                self._note("回合进行中,等它结束再 /compact", "warning")
            elif self._rt is None:
                self._note("运行时不可用。", "error")
            else:
                self.run_worker(self._compact_worker(arg), exclusive=False)
                self._scroll_end()
                return
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
                f"消息: {session.message_count} 条(当前分支)",
                f"分支: {len(session.tree_entries)} 节点 / {session.branch_points} 个分支点",
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
        store = self._session_store()
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
        for block in self._compaction_blocks:
            block.set_expanded(self._expanded)
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

    # -- 会话树(pi 的 /tree /fork /clone)--------------------
    def _session_store(self) -> SessionStore:
        """用 runtime 的 store(`sessionDir` 设置才会生效),而不是自己 new 一个。"""
        if self._rt is not None:
            return self._rt.sessions
        return SessionStore()

    def _entry_label(self, entry: dict) -> str:
        """树/选择器里的一行标签(单行、截断)。"""
        def clip(value: object, limit: int = 56) -> str:
            text = str(value or "").strip().splitlines()[0] if str(value or "").strip() else ""
            return text[:limit] + ("…" if len(text) > limit else "")

        kind = entry.get("type")
        if kind == "message":
            who = "你" if entry.get("role") == "user" else "助手"
            return f"{who}: {clip(entry.get('content'))}"
        if kind == "tool":
            return f"工具: {entry.get('tool')}  [{entry.get('status') or '?'}]"
        if kind == "dispatch":
            return f"分派: → {entry.get('display_name') or entry.get('agent')}"
        if kind == "custom":
            if entry.get("custom_type") == "assistant_narration":
                return f"叙述: {clip(entry.get('content'))}"
            return f"自定义: {entry.get('custom_type')}"
        if kind == "state":
            return f"状态: {entry.get('key')} = {entry.get('value')}"
        return str(kind)

    def _tree_options(self, session) -> list[tuple[str, str]]:
        """整棵树 → `(entry id, 缩进标签)`;`●` 当前节点,`│` 当前分支,`·` 其它分支。"""
        on_branch = {str(e.get("id")) for e in session.branch()}
        node = session.current
        out: list[tuple[str, str]] = []

        def walk(parent: str | None, depth: int) -> None:
            for child in session.children(parent):
                child_id = str(child.get("id"))
                mark = "●" if child_id == node else ("│" if child_id in on_branch else "·")
                out.append((child_id, "  " * depth + f"{mark} {self._entry_label(child)}"))
                walk(child_id, depth + 1)

        walk(None, 0)
        return out

    def _user_message_options(self, session) -> list[tuple[str, str]]:
        """当前分支上的用户消息(fork 的可选点)。"""
        return [(str(e.get("id")), self._entry_label(e))
                for e in session.branch()
                if e.get("type") == "message" and e.get("role") == "user"]

    def action_show_tree(self) -> None:
        """/tree:跳转当前会话的任意节点(同一文件内的分支导航)。"""
        session = self._session
        if session is None:
            return
        options = self._tree_options(session)
        if not options:
            self._flash("会话还是空的")
            return

        def picked(value: str | None) -> None:
            if value:
                self._jump_to(value)

        self.push_screen(PickerScreen(
            "会话树(↑↓ 选择 · enter 跳到该节点继续 · escape 取消)", options,
            current=session.current), picked)

    def _jump_to(self, entry_id: str) -> None:
        session = self._session
        if session is None:
            return
        old_leaf = session.current
        source_branch = session.branch()          # 跳之前的快照:分支摘要要用它
        target_branch_ids = {str(e.get("id")) for e in session.branch(entry_id)}
        if not self._session_store().set_position(session, entry_id):
            self._flash("节点不存在")
            return
        old_leaf = old_leaf or None
        self._replay_branch(session)
        self._flash(f"已跳到节点 {entry_id};下次提问从这里分叉")
        # 跳到了别的分支 → 把被放弃的那段压成摘要挂过来(否则切回来时上下文断了)
        if old_leaf and old_leaf not in target_branch_ids and self._rt is not None:
            self.run_worker(self._branch_summary_worker(session, source_branch, old_leaf, entry_id),
                            exclusive=False)

    def _resolve_user_message(self, session, arg: str) -> str | None:
        """`/fork` 参数:1-based 序号,或 entry id(前缀)。

        纯数字优先当序号:entry id 是随机串,否则 `/fork 2` 会随「第 1 条 id 是不是
        以 2 开头」而随机指向第 1 条 —— 不可复现的抖动。
        """
        options = self._user_message_options(session)
        if arg.isascii() and arg.isdigit():
            try:
                index = int(arg) - 1      # 不用 isdigit()单判:部分 Unicode 数字能过却过不了 int
            except ValueError:            # 防御:isascii+isdigit 已排除,留个兜底
                return None
            return options[index][0] if 0 <= index < len(options) else None
        for entry_id, _ in options:
            if entry_id == arg or entry_id.startswith(arg):
                return entry_id
        return None

    def _fork_from(self, session, entry_id: str) -> None:
        """在选中用户消息**之前**分叉出新会话,并把该消息放回编辑器(对齐 pi 的 /fork)。"""
        entry = next((e for e in session.branch() if str(e.get("id")) == entry_id), None)
        if entry is None:
            self._flash("找不到那条消息")
            return
        title = f"{session.title} @fork" if session.title else "fork"
        forked = self._session_store().fork_at(session, entry.get("parentId"), title=title)
        text = str(entry.get("content") or "")
        self._switch_session(forked, note=f"已 fork 出新会话 {forked.id}(那条消息已放回编辑器)")
        editor = self.query_one("#editor", Editor)
        editor.load_text(text)
        editor.move_cursor(self._offset_to_location(text, len(text)))
        self._sync_log_height()

    def _clone_session(self, title: str) -> None:
        session = self._session
        if session is None:
            return
        if session.current is None:
            self._note("还没有内容可 clone", "warning")
            self._scroll_end()
            return
        cloned = self._session_store().fork_at(
            session, session.current, title=title or f"{session.title} 副本")
        self._switch_session(cloned, note=f"已 clone 到新会话 {cloned.id}(分支已复制)")

    def _switch_session(self, session, note: str = "") -> None:
        """切到另一个会话:重放它的当前分支,清掉属于上一个会话的临时状态。"""
        self._session = session
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self._last_answer = ""
        self._pending_steer.clear()
        self._pending_follow.clear()
        self._branch = git_branch(session.cwd or str(Path.cwd()))
        self._replay_branch(session)
        self._restore_status()
        if note:
            self._note(note)
        self._scroll_end()

    def _select_session(self) -> None:
        """按 CLI 传来的意图选/建会话(对齐 `qi -c` / `--session` / `--fork` / `-n` / `--no-session`)。

        以前 TUI 无视这些参数、每次都新建一个名叫 `tui` 的会话 —— 文档里写了 `-c` 支持,
        但进 TUI 就失效了。现在与 headless 路径用同一套规则。
        """
        store = self._session_store()
        cwd = self._rt.cwd if self._rt is not None else Path.cwd()
        name = (self._want_name or "").strip()

        if self._want_no_session:
            self._session = store.create(name or "ephemeral", cwd=cwd)
            return
        if self._want_fork_id:
            source = store.get(self._want_fork_id)
            if source is None:
                self._session = store.create(name or "tui", cwd=cwd)
                self._startup_note = f"会话不存在: {self._want_fork_id}(已新建)"
                return
            title = name or (f"{source.title} @fork" if source.title else "fork")
            self._session = store.fork_at(source, source.current, title=title)
            self._startup_note = f"已从 {source.id} 分叉出新会话 {self._session.id}"
            return
        if self._want_session_id:
            found = store.get(self._want_session_id)
            if found is None:
                self._session = store.create(name or "tui", cwd=cwd)
                self._startup_note = f"会话不存在: {self._want_session_id}(已新建)"
            else:
                self._session = found
                if name:
                    self._session.title = name
            return
        if self._want_cont:
            self._session = store.latest() or store.create(name or "tui", cwd=cwd)
            return
        self._session = store.create(name or "tui", cwd=cwd)

    def _replay_branch(self, session, banner: Text | None = None) -> None:
        """把 transcript 换成该会话**当前分支**的内容(回放/跳分支/恢复会话共用)。

        `banner` 非空时先写它(恢复历史时启动头仍应在最上面)。
        """
        log = self.query_one("#log", VerticalScroll)
        for child in list(log.children):
            child.remove()
        self._tool_blocks.clear()
        self._bash_blocks.clear()
        self._compaction_blocks.clear()
        self._thinking_widgets.clear()
        self._live = None
        self._live_thinking = None
        if banner is not None:
            self._append(Static(banner, classes="msg"))
        renderer = self._renderer
        for entry in session.branch():
            kind = entry.get("type")
            if kind == "message" and entry.get("role") == "user":
                self._append(UserMessage(str(entry.get("content") or ""), renderer, self._palette))
            elif kind == "message" and entry.get("role") == "assistant":
                message = AssistantMessage(self._palette)
                message.set_text(str(entry.get("content") or ""), renderer)
                self._append(message)
            elif kind == "custom" and entry.get("custom_type") == "assistant_narration":
                message = AssistantMessage(self._palette)
                message.set_text(str(entry.get("content") or ""), renderer)
                self._append(message)
            elif kind == "dispatch":
                self._append(Static(renderer.dispatch_line(entry.get("agent"), entry),
                                    classes="msg"))
            elif kind == "tool":
                name = str(entry.get("tool") or "?")
                status = "ok" if entry.get("status") == "ok" else "error"
                block = ToolBlock(renderer.tool_title(name, entry.get("args") or {}),
                                  self._palette)
                block.set_state(status)
                result = str(entry.get("result") or "")
                block.set_output(renderer.tool_body(name, result, expanded=self._expanded,
                                                    is_error=status != "ok"))
                self._tool_blocks.append((block, name, result))
                self._append(block)
            elif kind in ("compaction", "branch_summary"):
                self._append_compaction(entry)
        self._sync_log_height()
        self._scroll_end()

    def _append_compaction(self, entry: dict) -> None:
        """把压缩/分支摘要 entry 渲染成 pi 同款底色块(摘要调用本身的用量也计入 footer)。"""
        kind = "branch" if entry.get("type") == "branch_summary" else "compaction"
        block = CompactionBlock(str(entry.get("summary") or ""),
                                _as_int(entry.get("tokensBefore")), self._palette, kind=kind)
        block.set_expanded(self._expanded)
        self._compaction_blocks.append(block)
        self._append(block)
        usage = entry.get("usage") or {}
        self._usage["prompt_tokens"] += _as_int(usage.get("prompt_tokens"))
        self._usage["completion_tokens"] += _as_int(usage.get("completion_tokens"))
        self._refresh_footer()

    async def _compact_worker(self, instructions: str) -> None:
        """/compact:手动压缩当前分支(对齐 pi 的 /compact [instructions])。"""
        if self._rt is None or self._session is None:
            return
        self._set_working(True)
        self._flash("正在压缩上下文…", 120)
        try:
            entry = await self._rt.compact_session(self._session, instructions or None)
        except Exception as exc:  # noqa: BLE001 压缩失败不该把 TUI 弄挂
            self._note(f"压缩失败: {exc}", "error")
            self._scroll_end()
            return
        finally:
            self._set_working(False)
        if entry is None:
            self._flash("没有可压缩的内容(会话太短或刚压过)")
            return
        self._append_compaction(entry)
        self._flash(f"已压缩:{_as_int(entry.get('tokensBefore')):,} tokens → 摘要")
        self._scroll_end()

    async def _branch_summary_worker(self, session, source_branch: list[dict],
                                     from_id: str, target_id: str) -> None:
        """`/tree` 跳走后,把被放弃的那段压成摘要挂到跳转点(pi 会先征求同意,qi 直接做并提示)。

        `source_branch` 是**跳之前**的分支快照 —— 跳转已改过 current,不能再现算。
        """
        if self._rt is None:
            return
        try:
            entry = await self._rt.summarize_branch_for_jump(session, source_branch,
                                                             from_id, target_id)
        except Exception as exc:  # noqa: BLE001 摘要失败只提示
            self._note(f"分支摘要失败: {exc}", "warning")
            self._scroll_end()
            return
        if entry is None:
            return
        self._append_compaction(entry)
        self._flash("已为离开的分支生成摘要(挂在跳转点)")
        self._scroll_end()

    # -- 补全(pi 的 autocomplete:`/` 命令、参数与 `@` 文件)------
    def _completion_candidates(self) -> tuple[list[Candidate], int, int]:
        """根据光标前的 token 给出候选。

        返回 `(candidates, start, end)`,start/end 是要被替换的区间。规则对齐 pi:
          · 行首的 `/xxx`(不含第二个 `/`)= 命令名补全
          · `/cmd <前缀>` = 参数补全(`/model` `/thinking` `/login`,pi 的 getArgumentCompletions)
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
        row_start = len("\n".join(lines[:row])) + (row > 0)

        # 1) 命令补全:行首 /xxx,且还没输入空格或第二个 /
        if before.startswith("/") and " " not in before and "/" not in before[1:]:
            items = [Candidate(name, name, detail)
                     for name, detail in sorted(TUI_COMMANDS.items())
                     if name.startswith(before)]
            if len(items) == 1 and items[0].value == before:
                items = []          # 已完整匹配,不必再提示
            return items, row_start, row_start + col

        # 1b) 参数补全:`/cmd <前缀>`(还没输入第二个参数)
        if before.startswith("/") and " " in before:
            cmd, _, rest = before.partition(" ")
            if " " not in rest and cmd.lower() in ARG_COMPLETION_COMMANDS:
                items = self._argument_candidates(cmd.lower(), rest)
                start = row_start + len(cmd) + 1
                return items, start, row_start + col

        # 2) 文件补全:`@路径` 或 `@"带空格的路径"`(pi 的 PATH_DELIMITERS / 引号规则)
        match = _at_prefix(before)
        if match is None:
            return [], 0, 0
        start, raw, quoted = match
        items = self._file_candidates(raw, quoted=quoted)
        return items, row_start + start, row_start + col

    def _argument_candidates(self, cmd: str, prefix: str) -> list[Candidate]:
        """`/model` `/thinking` `/login` 的第一个参数候选。"""
        items: list[Candidate] = []
        if cmd == "/model":
            for provider, model, is_current in self._model_options():
                value = f"{provider}/{model}"
                items.append(Candidate(value, value,
                                       "← 当前" if is_current else provider))
        elif cmd == "/thinking":
            items = [Candidate(level, level,
                               "← 当前" if level == self._thinking_level else "")
                     for level in THINKING_LEVELS]
        elif cmd == "/login":
            providers = sorted(getattr(getattr(self._rt, "cfg", None), "providers", {}) or {})
            items = [Candidate(name, name) for name in providers]
        lowered = prefix.lower()
        # 前缀匹配也认「模型名」那一段:`/model m2` 能补出 `alpha/m2`(pi 的模糊匹配的简化)
        out = [c for c in items
               if not lowered or c.value.lower().startswith(lowered)
               or c.value.rsplit("/", 1)[-1].lower().startswith(lowered)]
        if len(out) == 1 and out[0].value == prefix:
            return []               # 已完整匹配
        return out[:COMPLETION_ROWS]

    def _file_candidates(self, query: str, *, quoted: bool = False) -> list[Candidate]:
        """`@` 后的路径候选:优先 fd 全树搜索(pi 同款),没装 fd 就扫当前目录一层。

        `quoted` = 已在 `@"…"` 里:候选也补上成对引号,带空格的路径同理。
        """
        base = Path(self._rt.cwd) if self._rt is not None else Path.cwd()
        found = _fd_candidates(base, query) or _scan_candidates(base, query)
        out: list[Candidate] = []
        for display, is_dir in found[:COMPLETION_ROWS]:
            name = display.rsplit("/", 1)[-1]
            label = name + ("/" if is_dir else "")
            out.append(Candidate(_completion_value(display, is_dir, quoted), label,
                                 "" if display == name else display))
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
        for cand in candidates:
            tag = f"[{cand.source}] " if cand.source else ""
            text = f"{tag}{cand.label}" + (f"    {cand.detail}" if cand.detail else "")
            panel.add_option(Option(text, id=cand.value))
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
        picked = candidates[min(index, len(candidates) - 1)]
        value, label = picked.value, picked.label
        editor = self.query_one("#editor", Editor)
        text = editor.text
        # 命令补一个空格(pi 同款:`/name `);文件补一个空格;`@目录/` 与参数补全不补
        if value.startswith("/"):
            suffix = " "
        elif value.startswith("@"):
            suffix = "" if label.endswith("/") else " "
        else:
            suffix = ""            # 参数补全(/model、/thinking、/login)
        new_text = text[:start] + value + suffix + text[end:]
        editor.load_text(new_text)
        cursor = start + len(value) + len(suffix)
        if value.endswith('"') and label.endswith("/"):
            cursor -= 1          # 引号内继续补:光标停在收尾引号前
        editor.move_cursor(self._offset_to_location(new_text, cursor))
        if label.endswith("/"):
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


def run_tui(initial_prompt: str | None = None, *, session_id: str | None = None,
            cont: bool = False, fork_id: str | None = None,
            no_session: bool = False, name: str | None = None) -> None:
    """启动 TUI;`initial_prompt` 非空时进界面即提交(来自 `qi "问题"`)。

    会话选择参数与 headless 路径同义:`qi -c` / `--session` / `--fork` / `-n` / `--no-session`。
    """
    setting = None
    try:
        from .settings import load_settings

        setting = load_settings()[0].theme
    except Exception:  # settings 坏了不该挡住进界面
        setting = None
    palette = resolve_theme(setting, probe=True)
    QiTui(initial_prompt=initial_prompt, palette=palette, session_id=session_id, cont=cont,
          fork_id=fork_id, no_session=no_session, name=name).run(
              inline=True, inline_no_clear=True)
