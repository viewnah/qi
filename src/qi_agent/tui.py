"""TUI(pi 同款视觉)。

版式与配色对齐 pi(`@earendil-works/pi-coding-agent` 的 `modes/interactive`):

  · 渲染模式:`fullscreen`(qi 默认,备用屏 + qi 拥有视口,滚轮只滚 transcript)/
    `regular`(pi 默认的 inline:不占全屏、滚动交给终端)
  · 启动 banner = `qi vX` + 紧凑快捷键行 + 引导语 + 资源清单([Skills]/[Extensions])
  · 用户消息 = userMessageBg 底色块(padding 1,1),助手 = 无底色 markdown(padding 0,1)
  · 思考 = 灰色斜体;工具调用 = tool{Pending,Success,Error}Bg 底色块,标题 `read <path>`
  · 编辑器 = 上下 `─` 动态边框;工作中上边框内嵌 `⠋ Working` 指示器(80ms 换帧)
  · footer = cwd(+git 分支+会话名) / token 统计 + 模型 / 状态行(只在下述内容时出现)

v1 的分派行保留**只为回放旧会话**(v3 的 core 不再发 `dispatch`),按 pi 的行样式渲染
(`● → agent (source, 0.90)`)。
交互命令(/help /new /resume /agents /mode /agent /tools)沿用 qi 语义。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple, cast

from rich.markdown import Markdown as RichMarkdown
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, OptionList, SelectionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from .abort import AbortSignal
from .auth import AuthStore
from .config import (ConfigError, ResolvedModel, resolve_default_model, resolve_model,
                     selectable_models)
from .llm import (DEFAULT_THINKING_LEVEL, THINKING_LEVELS, LiteLLMClient, ThinkingLLMClient,
                  normalize_thinking_level)
from .loader import LoadError
from .registry import ToolCatalog
from .runtime import MAX_TOOL_ENTRY_CHARS, QiRuntime
from .session import Session, SessionStore, has_title
from .settings import (DEFAULT_TUI_MODE, TUI_MODES, SettingsError, double_escape_action,
                       next_choice, parse_value, set_value, tui_mode as resolve_tui_mode)
from .theme import (
    Palette,
    format_cwd_line,
    format_tokens,
    git_branch,
    resolve_theme,
    rich_theme,
    shorten_home,
    syntax_theme,
    textual_theme,
)
from .tools import register_builtin_tools

# pi 的 markdown 标题一律左对齐;Rich 默认把 h1 居中(Heading.LEVEL_ALIGN)。
from rich.markdown import Heading as _RichHeading

_RichHeading.LEVEL_ALIGN = {f"h{i}": "left" for i in range(1, 7)}


#: 没有「计划中」的命令了 —— pi 的斜杠命令面已全部对齐(留空集合而不是删掉常量:
#: `_command` 与补全都在读它,删了要改三处)。
PLANNED_COMMANDS: frozenset[str] = frozenset()
"""pi 有、qi 暂未实现的命令 —— 单独提示“计划中”,不冒充“未知命令”。"""

RESERVED_COMMANDS = frozenset({"/quit", "/hotkeys"})
"""扩展命令**不能顶掉**的几条。

它们都在 `_command` 的最前面就处理、而且不依赖 runtime(`/quit` 尤其重要):
顶掉 `/quit` 等于把用户锁在界面里 —— 而那时他已经没法用这个界面改回来了。
**其余内置命令可以被扩展覆盖**(pi 的顺序是“扩展命令先认领”),这条是刻意留的例外。
"""

TUI_COMMANDS: dict[str, str] = {
    "/hotkeys": "快捷键",
    "/quit": "退出",
    "/new": "新会话",
    "/resume": "选/恢复历史会话",
    "/name": "设置会话显示名",
    "/session": "会话信息",
    "/settings": "偏好面板(主题 / 思考级别 / 交互开关)",
    "/share": "把会话传成**私有** GitHub gist(需 GITHUB_TOKEN)",
    "/trust": "记住这个目录的信任决定(可跟 yes|no|forget)",
    "/tree": "跳到本会话的任意节点",
    "/fork": "从某条用户消息 fork 出新会话",
    "/clone": "复制当前分支为新会话",
    "/model": "当前/切换模型",
    "/scoped-models": "挑 Ctrl+P 轮换的模型",
    "/thinking": "思考级别(off|minimal|low|medium|high)",
    "/export": "导出会话 JSONL",
    "/import": "从 JSONL 导入会话",
    "/reload": "重载 agents/插件/配置",
    "/copy": "复制最后一条回答",
    "/login": "登录 provider(写 auth.json)",
    "/logout": "退出登录(删已存凭证)",
    "/changelog": "显示 CHANGELOG.md",
    "/compact": "压缩上下文(摘要旧消息)",
}
"""`/` 补全的候选(命令 → 说明);与 `_command` 的已实现分支一一对应。"""

COMPLETION_ROWS = 5
"""补全面板默认最多*显示*几行(pi 的 `autocompleteMaxVisible` 默认 5);候选本身不裁。"""

MAX_COMPLETION_ITEMS = 100
"""候选条数上限(只是防病态目录/搜索,面板本身只显示 `COMPLETION_ROWS` 行并可滚动)。"""

DOUBLE_ESCAPE_WINDOW = 0.5
"""空编辑器连按两次 escape 的判定窗口(秒);pi 用 500ms。"""


class Candidate(NamedTuple):
    """补全候选(对齐 pi 的 AutocompleteItem:value/label/description + 来源标签)。

    `source` 为空 = 内置;非空时按 pi 的写法拼在**说明前面**(`[u] 说明`,u/p/t =
    user / project / third-party)—— pi 的 `prefixAutocompleteDescription` 就是这个口径。
    """

    value: str          # 写回编辑器的文本
    label: str          # 面板里显示的名字
    detail: str = ""    # 右侧说明
    source: str = ""    # 来源标签(空 = 内置)


def _candidate_items(raw: Any) -> list[Candidate]:
    """把补全回调的返回值归一成 `Candidate`(收 `str` 与 `{value|label, description}`)。

    命令的 `getArgumentCompletions` 与 `add_autocomplete_provider` **共用这一处** ——
    两种补全只学一次元素形状。
    """
    items: list[Candidate] = []
    for entry in list(raw or []):
        if isinstance(entry, str):
            items.append(Candidate(entry, entry, ""))
        elif isinstance(entry, dict):
            value = str(entry.get("value") or entry.get("label") or "")
            if value:
                items.append(Candidate(value, value,
                                       str(entry.get("description") or "")))
    return items


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
  ctrl+u / ctrl+k        删到行首 / 删到行尾(删掉的文本进 kill-ring)
  ctrl+y / alt+y          yank 回最近删掉的文本 / 在 kill-ring 里轮换
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

会话选择器(/resume,占编辑器那一格):
  打字过滤(空格分词模糊 / "短语" 精确 / re:<正则>)/ tab 切当前目录↔全部
  ↑↓ 选择 · enter 恢复 · ctrl+s 排序(树状/最近/最相关)· ctrl+n 只看命名
  ctrl+p 显示路径 · ctrl+r 重命名 · ctrl+d 删除(需确认)· escape 取消

尚未对齐(pi 有,qi 缺能力或驱动不了):
  ctrl+v 粘贴图片(现在只会粘文本)
"""

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
"""pi 的 loader 帧(pi-tui `loader.js`)。"""

SPINNER_INTERVAL = 0.08

#: escape 的协作式中断宽限期:工具若不理中断信号(插件/长命令),到点就强制终止。
#: 有了它,escape 永远不会变成“死键”——这是相对 pi(纯协作)多出来的一道兜底。
INTERRUPT_GRACE_S = 3.0
"""pi 的 loader 间隔 80ms。"""

FOOTER_LINES = 3
"""footer 最多占的行数(cwd / 统计+模型 / 状态);编辑器 3 行由边框各 1 行 + 输入 1 行组成。

第三行（状态）是**有条件**的：只有排队中的消息、扩展状态或瞬时提示时才出（同 pi 的
`getExtensionStatuses()` 那条），空闲时 footer 只有 2 行。"""

MAX_EDITOR_ROWS = 8
"""编辑器最多长到几行(再多在编辑器内部滚动,不抢 transcript 的空间)。"""

PREVIEW_LINES = {"read": 10, "write": 10, "grep": 15, "ls": 20, "find": 20}
"""折叠态下各工具的输出行上限(对齐 pi 各工具的 format*Result)。"""


# ── 渲染小工具 ──────────────────────────────────────────


#: `_note` 收的色调 → 调色板键。pi 的 `notify` 等级也在里面(`info` = 普通正文)。
#: 不在这里的色调**一律当 `dim`** —— `_note` 只是个提示块,跑在 worker / 命令处理里,
#: 不该为一个配色词把流程打断。(补这段的原因:代码里写过 `_note(..., "info")`,
#: 而调色板没有 `info` → `ThemeError`,登录等流程在真终端上直接挂;单测都 monkeypatch
#: 了 `_note`,所以集体漏掉。)
NOTE_TONES: dict[str, str] = {
    "dim": "dim", "text": "text", "muted": "muted", "accent": "accent",
    "error": "error", "warning": "warning", "warn": "warning",
    "info": "text", "success": "success", "border": "border",
}


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
        """v1 的分派行(只在回放旧会话时出现):pi 没有这个概念,用它的行风格呈现。"""
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

    def banner(self, version: str, skills: list[str],
               extensions: list[str] | None = None) -> Text:
        """启动头(对齐 pi 的 builtInHeader:版本 + 紧凑快捷键行 + 引导语 + 资源清单)。

        pi 紧凑态第二行后面还挂一句 `Press ctrl+o to show full startup help and loaded
        resources.` —— 那要求 ctrl+o 真能展开启动头;qi 的 ctrl+o 目前只展开工具输出,
        所以不写那句(写了就是假的),资源清单直接平铺。
        """
        p = self.p
        dim = Style(color=p.hex("dim"))
        muted = Style(color=p.hex("muted"))
        text = Text()
        text.append("\n")
        text.append("qi", style=Style(color=p.hex("accent"), bold=True))
        text.append(f" v{version}", style=dim)
        text.append("\n")
        # 只写 qi 真的实现了的键(escape 中断;ctrl+c 清空 / ctrl+d 退出;ctrl+o 展开)
        hints = ["escape interrupt", "ctrl+c/ctrl+d clear/exit", "/ commands",
                 "! bash", "ctrl+o tools"]
        for index, hint in enumerate(hints):
            if index:
                text.append(" · ", style=muted)
            text.append(hint, style=Style(color=p.hex("text")))
        text.append("\n")
        text.append("Qi can explain its own features and look up its docs. "
                    "Ask it how to use or extend Qi.", style=dim)
        text.append("\n\n")
        for name, items in (("Skills", skills), ("Extensions", extensions or [])):
            if not items:
                continue
            text.append(f"[{name}]\n", style=dim)
            text.append("  " + ", ".join(items) + "\n", style=Style(color=p.hex("text")))
        return text


# ── 消息块 ──────────────────────────────────────────────


class Transcript(VerticalScroll):
    """transcript 视口(fullscreen 下用 `1fr` 在**内部**滚动)。

    `can_focus = False`:fullscreen 开着鼠标上报,点一下 transcript 不该把焦点从编辑器抢走
    (抢走了用户点完就没法打字)。滚轮不靠焦点 —— Textual 把 wheel 派给指针下的 scrollable。
    """

    can_focus = False


#: pi `select-list.js` 的两个阈值:说明太窄就不画、行宽不够也不画
PI_DESCRIPTION_MIN_WIDTH = 10
PI_DESCRIPTION_WIDTH_GATE = 40
#: `/` 命令的标签列宽范围(pi `SLASH_COMMAND_SELECT_LIST_LAYOUT`:12..32);其余固定 32
PI_SLASH_MIN_COLUMN = 12
PI_SLASH_MAX_COLUMN = 32
PI_DEFAULT_COLUMN = 32


def pi_select_text(items: list[Candidate], index: int, width: int, limit: int,
                   palette: Palette, *, column: int | None = None) -> Text:
    """按 pi 的 `SelectList` 版式渲染一列选项(箭头 + 对齐 + muted 说明 + `(n/m)`)。

    pi 两个地方用同一套视觉:补全面板(`editor.js` 里的 SelectList)与命令选择器
    (`showSelector()` 放进编辑器那一格的组件)。所以渲染只写这一份。

    * `→ ` 选中项前缀 + `accent`,未选中 `  ` + `text`;
    * 说明用 `muted`;来源标签(`[u]/[p]/[t]`)拼在**说明**里(pi 的口径);
    * 选项装不下时尾部补 `  (n/total)`(n = 选中项序号,同 pi);
    * `column=None` = 说明紧跟正文一个空格(pi 的模型选择器就把 `[provider]` 放在模型名后);
      给了数字 = 正文按该列宽对齐,放不下就不画说明(pi 的两个阈值)。
    """
    if not items:
        return Text("")
    limit = max(1, limit)
    span = min(limit, len(items))
    index = max(0, min(index, len(items) - 1))
    start = max(0, min(index - span // 2, len(items) - span))
    end = start + span
    muted = Style(color=palette.hex("muted"))
    accent = Style(color=palette.hex("accent"))
    plain = Style(color=palette.hex("text"))
    out = Text()
    for position in range(start, end):
        if out.plain:
            out.append("\n")
        item = items[position]
        selected = position == index
        head = ("→ " if selected else "  ") + item.label
        description = item.detail
        if item.source:
            description = (f"[{item.source}] {description}" if description
                           else f"[{item.source}]")
        head_width = Text(head).cell_len
        room = width - head_width - 2
        # pi 的两个门槛:行宽 > 40 才带说明,剩下的位置 > 10 才画得下
        fits = bool(description) and width > PI_DESCRIPTION_WIDTH_GATE \
            and room > PI_DESCRIPTION_MIN_WIDTH
        if fits:
            gap = " " if column is None else " " * max(1, column - head_width)
            out.append(head, style=accent if selected else plain)
            out.append(gap + description, style=muted)
        else:
            out.append(head, style=accent if selected else plain)
    if end - start < len(items):
        out.append("\n")
        out.append(f"  ({index + 1}/{len(items)})", style=muted)
    return out


class CompletionPanel(Static):
    """`/` 与 `@` 的补全面板(pi `SelectList` 同款版式)。

    pi 把它画在编辑器**下边框之下**:选中行 `→ ` 前缀 + accent,标签按列对齐,说明用
    muted(来源标签 `[u]/[p]/[t]` 写在说明里),**没有底色/边框/滚动条**。所以这里不用
    Textual 的 `OptionList` —— 它自带 `$surface` 底与整行高亮底,与 pi 不是一个观感。

    对外保留 `OptionList` 的那几个接口(`clear_options` / `option_count` / `highlighted`),
    调用点不必知道换了实现。
    """

    DEFAULT_CSS = """
    CompletionPanel { display: none; width: 1fr; height: auto; padding: 0 1;
                      background: transparent; }
    CompletionPanel.visible { display: block; }
    """

    #: 行数上限(由 `autocompleteMaxVisible` 播下来)

    def __init__(self, palette: Palette, id: str | None = None) -> None:
        super().__init__("", id=id)
        self._p = palette
        self._items: list[Candidate] = []
        self._index = 0
        self._rows = COMPLETION_ROWS
        self._text = Text("")

    def rendered_text(self) -> Text:
        """面板当前画出来的文本(诊断/测试用;真正落屏走 `_paint()` → `Static.update`)。"""
        return self._text

    # -- OptionList 同形接口 -------------------------------------------
    def clear_options(self) -> None:
        self._items = []
        self._index = 0
        self._text = Text("")
        self.update("")

    def set_items(self, items: list[Candidate], rows: int) -> None:
        self._items = list(items)
        self._rows = max(1, _as_int(rows) or COMPLETION_ROWS)
        self._index = 0
        self._paint()

    @property
    def option_count(self) -> int:
        return len(self._items)

    @property
    def highlighted(self) -> int:
        return self._index

    @highlighted.setter
    def highlighted(self, index: int) -> None:
        last = max(0, len(self._items) - 1)
        try:
            wanted = int(index)
        except (TypeError, ValueError):
            wanted = 0
        self._index = max(0, min(wanted, last))
        self._paint()

    def set_padding(self, padding_x: int) -> None:
        self.styles.padding = (0, padding_x)
        self._paint()

    def on_resize(self, _event: object = None) -> None:
        self._paint()

    # -- 渲染 -----------------------------------------------------------
    def _column(self) -> int:
        """标签列宽:命令按内容伸缩(12..32),其余固定 32(pi 的两个 layout)。"""
        if not self._items or not self._items[0].value.startswith("/"):
            return PI_DEFAULT_COLUMN
        widest = max((Text(c.label).cell_len for c in self._items), default=0) + 2
        return max(PI_SLASH_MIN_COLUMN, min(PI_SLASH_MAX_COLUMN, widest))

    def _paint(self) -> None:
        if not self._items:
            self.update("")
            self._text = Text("")
            return
        width = self.content_size.width or (self.size.width - 2)
        if width <= 1:
            width = 80
        self._text = pi_select_text(self._items, self._index, width, self._rows,
                                   self._p, column=self._column())
        self.update(self._text)


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
    """助手消息:无底色,markdown,左侧 padding 来自 `settings.outputPad`(pi 默认 1)。"""

    def __init__(self, palette: Palette, pad: int = 1) -> None:
        super().__init__("", classes="msg assistant-msg")
        self.styles.padding = (0, pad)
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


class ExtensionToolBlock(Vertical):
    """工具卡片的**扩展渲染**容器(pi 的 `renderCall` / `renderResult`)。

    有渲染钩子时由扩展的组件完全接管(与 pi 同义:给了 `renderResult` 就替换默认渲染);
    没有就继续走内置的 `ToolBlock`。组件必须在**本容器自己挂载之后**才能挂,
    所以先用一个待挂槽位存着,`on_mount` 里再真挂(Textual 不允许给自己未挂载的 widget mount 子项)。
    """

    def __init__(self, palette: Palette, *, shell: bool = True) -> None:
        """`shell=False` 对应 pi 的 `renderShell: "self"` —— 扩展的组件自己画框。

        默认(`shell=True`)给扩展的组件套上**默认工具卡片外壳**(与内置 `ToolBlock` 同一套:
        状态底色 + `(1,1)` 内边距)—— 否则“把渲染交给扩展”会让卡片突然没有底色,
        变成与内置工具不一致的观感。`"self"` 就是让扩展自己负责这个壳。
        """
        super().__init__(classes="msg tool-msg" if shell else "")
        self._palette = palette
        self._shell = shell
        self._widget: Any = None
        self._pending: Any = None
        self._state = "pending"
        if shell:
            self.styles.padding = (1, 1)
            self._apply_shell_state()

    def show(self, widget: Any) -> None:
        """挂上(或替换成)扩展给的组件。"""
        if widget is None:
            return
        old = self._widget
        self._widget = widget
        if self.is_mounted:
            if old is not None:
                # 旧组件可能已经被前端卸载了 —— 移除失败在这里是正常情况,不是错误
                with contextlib.suppress(Exception):
                    old.remove()
            self.mount(widget)
        else:
            self._pending = widget
        setter = getattr(widget, "set_state", None)
        if callable(setter):
            setter(self._state)

    def on_mount(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            self.mount(pending)

    def _apply_shell_state(self) -> None:
        if not self._shell:
            return
        key = {"pending": "toolPendingBg", "ok": "toolSuccessBg",
               "error": "toolErrorBg"}[self._state]
        self.styles.background = self._palette.hex(key)

    def set_state(self, state: str) -> None:
        self._state = state
        self._apply_shell_state()
        setter = getattr(self._widget, "set_state", None)
        if callable(setter):
            setter(state)

    def set_output(self, body: Any) -> None:
        """转给扩展组件(它自己知道怎么显示结果);没实现就什么也不做。"""
        setter = getattr(self._widget, "set_output", None)
        if callable(setter):
            setter(body)

    def set_expanded(self, expanded: bool) -> None:
        setter = getattr(self._widget, "set_expanded", None)
        if callable(setter):
            setter(expanded)


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


def _removed_text(before: str, after: str) -> str:
    """单次连续删除时,before → after 被删掉的那一段(最长公共前/后缀之外的局部)。"""
    start = 0
    limit = min(len(before), len(after))
    while start < limit and before[start] == after[start]:
        start += 1
    end_b, end_a = len(before), len(after)
    while end_b > start and end_a > start and before[end_b - 1] == after[end_a - 1]:
        end_b -= 1
        end_a -= 1
    return before[start:end_b]


class KillRing:
    """Emacs 风格的 kill-ring(对齐 pi-tui 的 `kill-ring.js`)。

    `push` 带 `accumulate` 时与最近一条合并(连续 kill 接成一段),`prepend` 区分
    向前/向后删除;`rotate` 供 yank-pop 轮换旧条目。
    """

    def __init__(self) -> None:
        self._ring: list[str] = []

    def push(self, text: str, *, prepend: bool, accumulate: bool) -> None:
        if not text:
            return
        if accumulate and self._ring:
            last = self._ring.pop()
            self._ring.append(text + last if prepend else last + text)
        else:
            self._ring.append(text)

    def peek(self) -> str | None:
        return self._ring[-1] if self._ring else None

    def rotate(self) -> None:
        if len(self._ring) > 1:
            self._ring.insert(0, self._ring.pop())

    def __len__(self) -> int:
        return len(self._ring)


class Editor(TextArea):
    """多行编辑器(对齐 pi 的 `pi-tui/components/editor.js` 键位)。

    Textual 的 `TextArea` 已经提供多行编辑/选区/撤销/括号粘贴,这里只补齐 pi 的差异:
      · enter = 提交(pi `tui.input.submit`)
      · shift+enter / ctrl+j = 换行(pi `tui.input.newLine`)
      · ctrl+b/f。alt+b/f、alt+←/→、alt+d = 光标词移动/删词(pi 的别名)
      · ctrl+- = 撤销(pi `tui.editor.undo`;Textual 默认把 undo 绑在 ctrl+z,
        而 ctrl+z 在 pi 里是挂起,所以改绑到 pi 的键)

    kill-ring(pi 的 `kill-ring.js`):`ctrl+k` / `ctrl+u` / `ctrl+w`(alt+backspace)/
    `alt+d` 删掉的文本进环,连续删会接成一段;`ctrl+y` yank 回来,`alt+y` 在环里轮换。
    差异(已知):ctrl+k 在行尾/空行时 Textual 走的是「并下一行 / 删整行」,
    这两支不进 kill-ring(pi 会推一个 `\n`)。

    输入历史(pi 的 `addToHistory` / `navigateHistory`):无补全面板时 `↑` 取回上一条
    提交过的文本(对话与 `!` bash,内置命令不记),`↓` 往回走;首次翻历史时留住当前
    草稿,手动改动即退出浏览。
    """

    KILL_KEYS = frozenset({"ctrl+k", "ctrl+u", "ctrl+w", "alt+backspace",
                           "alt+d", "alt+delete"})
    YANK_KEYS = frozenset({"ctrl+y", "alt+y"})

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
        Binding("ctrl+y", "yank", "粘回删掉的文本", show=False),
        Binding("alt+y", "yank_pop", "轮换删掉的文本", show=False),
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
        # kill-ring(pi 的 kill-ring.js):连续 kill 合并、yank-pop 轮换
        self._kill_ring = KillRing()
        self._last_action: str | None = None       # "kill" / "yank" / None
        self._last_yank: tuple[int, int] | None = None

    async def _on_key(self, event: events.Key) -> None:
        # 除了 kill/yank 系列,任何键都打断「连续 kill 合并」与 yank-pop(pi 同款)
        if event.key not in self.KILL_KEYS and event.key not in self.YANK_KEYS:
            self._last_action = None
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

    # -- kill-ring(对齐 pi 的 deleteToStartOfLine / deleteWord… / yank)----
    def _push_kill(self, deleted: str, *, prepend: bool) -> None:
        self._kill_ring.push(deleted, prepend=prepend,
                             accumulate=self._last_action == "kill")
        self._last_action = "kill"

    def _kill(self, action: str, *, prepend: bool) -> None:
        """执行 Textual 的删除动作,并把真正删掉的文本推入 kill-ring。"""
        before = self.text
        getattr(super(), action)()
        self._push_kill(_removed_text(before, self.text), prepend=prepend)

    def action_delete_to_start_of_line(self) -> None:
        self._kill("action_delete_to_start_of_line", prepend=True)

    def action_delete_to_end_of_line(self) -> None:
        self._kill("action_delete_to_end_of_line", prepend=False)

    def action_delete_word_left(self) -> None:
        self._kill("action_delete_word_left", prepend=True)

    def action_delete_word_right(self) -> None:
        self._kill("action_delete_word_right", prepend=False)

    def action_yank(self) -> None:
        """ctrl+y:把 kill-ring 最近一条粘回光标处(pi 的 tui.editor.yank)。"""
        text = self._kill_ring.peek()
        if text is None:
            return
        start = self._text_offset_at_cursor()
        self.insert(text)
        self._last_yank = (start, start + len(text))
        self._last_action = "yank"

    def action_yank_pop(self) -> None:
        """alt+y:紧跟在 yank 之后,把刚粘的那段换成环里的上一条(pi 的 yankPop)。"""
        if self._last_action != "yank" or len(self._kill_ring) <= 1 or self._last_yank is None:
            return
        start, end = self._last_yank
        text = self.text
        self.replace("", self._offset_to_location(text, start),
                     self._offset_to_location(text, end))
        self._kill_ring.rotate()
        replacement = self._kill_ring.peek() or ""
        self.insert(replacement)
        self._last_yank = (start, start + len(replacement))
        self._last_action = "yank"

    def _text_offset_at_cursor(self) -> int:
        """光标在纯文本里的字符偏移(不能叫 `_cursor_offset`:与 TextArea 内部同名)。"""
        row, col = self.cursor_location
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + col

    @staticmethod
    def _offset_to_location(text: str, offset: int) -> tuple[int, int]:
        offset = max(0, min(offset, len(text)))
        row = text.count("\n", 0, offset)
        return row, offset - text.rfind("\n", 0, offset) - 1

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


class EditorSlotPanel(ModalScreen[Any]):
    """pi 那种“占编辑器那一格”的选择器 / 输入面板。

    pi 的 `showSelector()`(`interactive-mode.js`)是把 `editorContainer` 的内容**换掉** ——
    选择器与编辑器同宽、贴在同一条底线上(上面 transcript 不动、下面 footer 不动),组件形状是
    「上下 `DynamicBorder` + accent bold 标题 + `→ ` 列表 + 键位提示」,**没有底色、没有遮罩**。

    Textual 这边仍用 `ModalScreen` 的机制(`push_screen` + 回调 / `await_screen`),但把外壳
    做成同一形状:全宽、底部对齐、上下 `─`、底色取探测到的终端底色(看不出“填色”)、backdrop
    透明。底边到屏幕底留出 footer 的**实际行数**(2~3,见 `_footer_rows`),这样它占的正好是
    编辑器那一格。子类只给 `TITLE` / `HINTS` 与 `compose_body()`。
    """

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("up", "move(-1)", "上一个", show=False),
        Binding("down", "move(1)", "下一个", show=False),
        # pi 的 SelectList 也认 j/k;没 priority,所以过滤框里敲 j 仍然是打字
        Binding("k", "move(-1)", "上一个", show=False),
        Binding("j", "move(1)", "下一个", show=False),
    ]

    #: 面板标题(注意别叫 `TITLE` —— 那是 Textual `Screen.TITLE`)
    PANEL_TITLE = ""
    HINTS = "↑↓ 选择 · enter 确认 · escape 取消"
    BOX_ID = "model-box"

    def compose(self) -> ComposeResult:
        with Vertical(id=self.BOX_ID):
            yield Static(self.title_text(), id="model-hint")
            yield Static("", classes="panel-gap")
            yield from self.compose_body()
            yield Static("", classes="panel-gap")
            yield Static(self.hints_text(), id="panel-hints")

    # -- 子类钩子 -------------------------------------------------------
    def title_text(self) -> str:
        return self.PANEL_TITLE

    def hints_text(self) -> str:
        return self.HINTS

    def compose_body(self) -> ComposeResult:
        yield from ()

    def on_panel_ready(self) -> None:
        """挂载后的钩子(设焦点、画列表)。"""

    def action_move(self, step: int) -> None:
        """↑↓ / j / k:默认没有可移动的列表,子类覆盖。"""

    def action_cancel(self) -> None:
        self.dismiss(None)

    # -- 外壳 -----------------------------------------------------------
    def on_mount(self) -> None:
        self._fit_above_footer()
        self.on_panel_ready()

    def _fit_above_footer(self) -> None:
        """把面板底边对齐到 footer 之上(pi 的选择器占的就是编辑器那一格)。"""
        offset = 0
        hook = getattr(self.app, "_editor_slot_offset", None)
        if callable(hook):
            try:
                offset = max(0, int(cast(Any, hook)()))
            except (TypeError, ValueError, NoMatches):
                offset = 0
        try:
            box = self.query_one(f"#{self.BOX_ID}")
        except NoMatches:      # pragma: no cover - 挂载竞态
            return
        box.styles.margin = (0, 0, offset, 0)

    def palette(self) -> Palette:
        """当前调色板 —— 屏幕自己不持有,`QiTui` 是唯一持有者。"""
        return getattr(self.app, "_palette")

    def body_width(self) -> int:
        """正文可用宽度(给按列对齐的渲染用)。"""
        try:
            node = self.query_one(f"#{self.BOX_ID}")
        except NoMatches:      # pragma: no cover
            return 80
        width = node.content_size.width or (node.size.width - 2)
        return width if width > 1 else 80


def prompt_row(field_id: str, *, placeholder: str = "", password: bool = False,
               value: str = "") -> Horizontal:
    """pi 的输入行:`> ` 前缀 + 无边框输入。

    pi-tui 的 `Input` 渲染成 `this.prompt + value`,`prompt` 默认 `"> "`(见
    `components/input.js`)—— 没有外框、没有底色。Textual 的 `Input` 自带 tall 边框,
    所以这里拼一个 `Horizontal` 并把边框/底色/内边距全去掉。
    """
    return Horizontal(
        Static("> ", classes="input-prompt"),
        Input(value=value, placeholder=placeholder, password=password, id=field_id),
        classes="input-row")


class PromptScreen(EditorSlotPanel):
    """一行文本输入(`ctx.ui.input`)。enter 提交,escape 取消(→ None)。"""

    HINTS = "enter 提交 · escape 取消"

    def __init__(self, title: str, default: str = "", secret: bool = False) -> None:
        super().__init__()
        self._title = title
        self._default = default
        self._secret = secret

    def title_text(self) -> str:
        return self._title

    def compose_body(self) -> ComposeResult:
        yield prompt_row("prompt-input", password=self._secret, value=self._default)

    def on_panel_ready(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class EditorScreen(EditorSlotPanel):
    """多行编辑器(`ctx.ui.editor`)。ctrl+s 保存,escape 取消(→ None)。"""

    HINTS = "ctrl+s 保存 · escape 取消"

    def __init__(self, title: str, prefill: str = "") -> None:
        super().__init__()
        self._title = title
        self._prefill = prefill

    def title_text(self) -> str:
        return self._title

    def compose_body(self) -> ComposeResult:
        yield TextArea(self._prefill, id="editor-modal")

    def on_panel_ready(self) -> None:
        self.query_one("#editor-modal", TextArea).focus()

    def action_save(self) -> None:
        self.dismiss(self.query_one("#editor-modal", TextArea).text)

    BINDINGS = [*EditorSlotPanel.BINDINGS, Binding("ctrl+s", "save", "保存")]


class CustomScreen(EditorSlotPanel):
    """`ctx.ui.custom` 的模态外壳:把扩展给的组件放进去,等它调 `done(value)` 或 escape。

    `box` 是一个可变单槽 —— 因为 `done` 回调必须在组件**造出来之前**就存在
    (pi 的 factory 拿到的就是 `done`),而组件又是 `compose` 时才取的。
    """

    HINTS = "escape 关闭"

    def __init__(self, box: dict, title: str | None = None) -> None:
        super().__init__()
        self._box = box
        self._title = title

    def title_text(self) -> str:
        return self._title or ""

    def compose_body(self) -> ComposeResult:
        widget = self._box.get("widget")
        if widget is not None:
            yield widget


class PickerScreen(EditorSlotPanel):
    """通用选择器(模型 / 思考级别 / 登录 / fork 点共用)。

    选项按 pi 的 `SelectList` 版式渲染(`→ ` + accent + muted 说明,无底色无边框)——
    不用 Textual 的 `OptionList`,因为它自带 `$surface` 底与整行高亮底。
    返回选中项的 `value`;取消返回 None。
    """

    def __init__(self, title: str, options: list[tuple[str, str]],
                 current: str | None = None, *, hints: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._current = current
        self._hints = hints
        self._index = self._current_index()
        self._list = Static("", id="model-list")
        self._text = Text("")

    def rendered_text(self) -> Text:
        """当前渲染出来的列表(诊断/测试用)。"""
        return self._text

    @property
    def highlighted(self) -> int:
        """当前高亮项(与 `OptionList` / `CompletionPanel` 同形,便于调用点与测试)。"""
        return self._index

    @highlighted.setter
    def highlighted(self, index: int) -> None:
        if not self._options:
            return
        try:                     # 与 `CompletionPanel` 同口径:怪值当 0,不抛
            wanted = int(index)
        except (TypeError, ValueError):
            wanted = 0
        self._index = max(0, min(wanted, len(self._options) - 1))
        self._paint()

    # -- 子类钩子 -------------------------------------------------------
    def title_text(self) -> str:
        return self._title

    def hints_text(self) -> str:
        return self._hints or super().hints_text()

    def rows(self) -> list[Candidate]:
        """要渲染的行;子类覆盖它以加 `✓ ` 当前标记 / muted 说明。"""
        return [Candidate(value, label) for value, label in self._options]

    def column(self) -> int | None:
        """说明列宽(pi 的两个 layout):`None` = 紧跟正文(pi 的模型选择器就是这样)。"""
        return None

    # -- 渲染 / 键位 -----------------------------------------------------
    def compose_body(self) -> ComposeResult:
        yield self._list

    def on_panel_ready(self) -> None:
        self._paint()

    def on_resize(self) -> None:
        self._paint()

    def _current_index(self) -> int:
        if self._current is None:
            return 0
        for index, (value, _) in enumerate(self._options):
            if value == self._current:
                return index
        return 0

    def _paint(self) -> None:
        self._text = pi_select_text(self.rows(), self._index, self.body_width(),
                                    len(self._options), self.palette(),
                                    column=self.column())
        self._list.update(self._text)

    def action_move(self, step: int) -> None:
        if not self._options:
            return
        self._index = (self._index + step) % len(self._options)
        self._paint()

    def action_confirm(self) -> None:
        if self._options:
            self.select(self._options[min(self._index, len(self._options) - 1)][0])

    def select(self, value: str) -> None:
        """确认某个选项(子类可覆盖以做额外记账)。"""
        self.dismiss(value)

    BINDINGS = [*EditorSlotPanel.BINDINGS, Binding("enter", "confirm", "确认", show=False)]


class ModelSelector(PickerScreen):
    """ctrl+l / `/model`:选模型(行格式照 pi 的模型选择器)。

    pi 的行是 `→ ` + `✓ `(当前)+ 模型 id + muted `[provider]` + ` · default`
    (`model-selector.js` 的 `renderList`)。
    """

    def __init__(self, options: list[tuple[str, str, bool]], *,
                 default: tuple[str, str] | None = None) -> None:
        self._entries = [(f"{provider}\x00{model}", provider, model, is_current)
                         for provider, model, is_current in options]
        self._default = default
        current = next((value for value, _, _, is_current in self._entries if is_current), None)
        super().__init__("选择模型", [(value, model) for value, _, model, _ in self._entries],
                         current=current,
                         hints="↑↓ 选择 · enter 确认 · escape 取消 · 轮换清单 /scoped-models")

    def rows(self) -> list[Candidate]:
        out: list[Candidate] = []
        for value, provider, model, is_current in self._entries:
            detail = f"[{provider}]"
            if self._default and (provider, model) == self._default:
                detail += " · default"
            out.append(Candidate(value, f"{'✓ ' if is_current else '  '}{model}", detail))
        return out


class ThinkingSelector(PickerScreen):
    """`/thinking` 无参:挑思考级别(pi 的 `ThinkingSelectorComponent`)。

    pi 的项是 `✓ `(当前)+ 级别 + 说明(`default` 那档再缀 ` · default`)。
    """

    #: 各级别一句话说明(pi 的 `LEVEL_DESCRIPTIONS`,换成中文)
    DESCRIPTIONS = {
        "off": "不请求思考",
        "minimal": "最少思考",
        "low": "轻量思考",
        "medium": "均衡(pi 的默认档)",
        "high": "更多思考",
        "xhigh": "很多思考(provider 侧)",
        "max": "最多思考",
    }

    def __init__(self, current: str, default: str | None = None) -> None:
        self._default = default
        super().__init__("思考级别", [(level, level) for level in THINKING_LEVELS],
                         current=current,
                         hints="↑↓ 选择 · enter 确认 · shift+tab 轮转 · escape 取消")

    def column(self) -> int | None:
        return PI_DEFAULT_COLUMN

    def rows(self) -> list[Candidate]:
        out: list[Candidate] = []
        for level in THINKING_LEVELS:
            description = self.DESCRIPTIONS.get(level, "")
            if level == self._default:
                description = f"{description} · default" if description else "default"
            out.append(Candidate(level, f"{'✓ ' if level == self._current else '  '}{level}",
                                 description))
        return out

class ScopedModelsSelector(EditorSlotPanel):
    """`/scoped-models`:挑 Ctrl+P 轮换哪些模型(对齐 pi 的 ScopedModelsSelectorComponent)。

    返回选中的 `provider/model` 列表;取消返回 None。**全选 = 空列表** = 轮换全部,
    与 pi 的「scopedModels 为空则用全部」语义一致。
    """

    PANEL_TITLE = "Ctrl+P 轮换哪些模型"
    HINTS = "space 勾选 · ctrl+s 保存 · ctrl+a 全选 · ctrl+x 全不选 · ctrl+p 切换 provider · escape 取消"

    BINDINGS = [
        ("escape", "dismiss(None)", "取消"),
        Binding("ctrl+s", "save", "保存", show=False),
        Binding("ctrl+a", "pick_all", "全选", show=False),
        Binding("ctrl+x", "pick_none", "全不选", show=False),
        Binding("ctrl+p", "toggle_provider", "切换该 provider", show=False),
    ]

    def __init__(self, options: list[tuple[str, str]], enabled: list[str]) -> None:
        super().__init__()
        self._options = options          # (provider/model, provider)
        self._enabled = enabled

    def compose_body(self) -> ComposeResult:
        from textual.widgets.selection_list import Selection

        known = {value for value, _ in self._options}
        chosen = {v for v in self._enabled if v in known} or known   # 空 = 全部
        selections = [Selection(value, value, value in chosen) for value, _ in self._options]
        yield SelectionList[str](*selections, id="scoped-list")

    def on_panel_ready(self) -> None:
        self.query_one("#scoped-list", SelectionList).focus()

    def _listing(self) -> SelectionList[str]:
        return self.query_one("#scoped-list", SelectionList)

    def action_save(self) -> None:
        picked = list(self._listing().selected)
        everything = [value for value, _ in self._options]
        # 全选 = 不限,回写空列表(pi:空 scopedModels = 轮换全部)
        self.dismiss([] if len(picked) == len(everything) else picked)

    def action_pick_all(self) -> None:
        self._listing().select_all()

    def action_pick_none(self) -> None:
        self._listing().deselect_all()

    def action_toggle_provider(self) -> None:
        listing = self._listing()
        if not self._options:
            return
        index = listing.highlighted if listing.highlighted is not None else 0
        provider = self._options[min(index, len(self._options) - 1)][1]
        targets = [value for value, prov in self._options if prov == provider]
        selected = set(listing.selected)
        if all(value in selected for value in targets):
            for value in targets:
                listing.deselect(value)
        else:
            for value in targets:
                listing.select(value)


def shorten_path(path: str, home: str | None = None) -> str:
    """列表右侧的路径:`~` 缩写(pi `shortenPath`)。"""
    return shorten_home(str(path), home)


def format_age(modified: float, now: float | None = None) -> str:
    """最后活动时间 → pi 那种紧凑年龄(`now` / `5m` / `3h` / `2d` / `1w` / `4mo` / `1y`)。

    相对时间比绝对时间戳有用:选会话时关心的是"这是刚才那条还是上周那条"。
    """
    if modified <= 0:
        return "?"
    delta = max(0.0, (now if now is not None else time.time()) - modified)
    minutes = int(delta // 60)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 7:
        return f"{days}d"
    if days < 30:
        return f"{days // 7}w"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def fuzzy_score(query: str, text: str) -> float | None:
    """pi-tui `fuzzyMatch` 的移植:子序列匹配 + 词边界/连续性打分(越小越靠前)。

    返回 None = 不匹配。分数只在**同一批**结果里比大小,绝对值无语义。
    """
    query_lower = query.lower()
    text_lower = text.lower()

    def match(needle: str) -> float | None:
        if not needle:
            return 0.0
        if len(needle) > len(text_lower):
            return None
        score = 0.0
        query_index = 0
        last_match = -1
        consecutive = 0
        while query_index < len(needle):
            found = text_lower.find(needle[query_index], last_match + 1)
            if found < 0:
                return None
            boundary = found == 0 or text_lower[found - 1] in " \t-_./:"
            if last_match == found - 1:
                consecutive += 1
                score -= consecutive * 5
            else:
                consecutive = 0
                if last_match >= 0:
                    score += (found - last_match - 1) * 2
            if boundary:
                score -= 10
            score += found * 0.1
            last_match = found
            query_index += 1
        if needle == text_lower:
            score -= 100
        return score

    primary = match(query_lower)
    if primary is not None:
        return primary
    # 字母数字颠倒(`2d` 也能命中 `d2`):pi 的同一个兜底
    swapped = ""
    if re.fullmatch(r"[a-z]+[0-9]+", query_lower):
        swapped = re.sub(r"^([a-z]+)([0-9]+)$", r"\2\1", query_lower)
    elif re.fullmatch(r"[0-9]+[a-z]+", query_lower):
        swapped = re.sub(r"^([0-9]+)([a-z]+)$", r"\2\1", query_lower)
    return match(swapped) if swapped else None


#: 搜索词的一个片段:`kind` ∈ `fuzzy`(子序列)/ `phrase`(连续子串)。
SEARCH_TOKEN = NamedTuple("SEARCH_TOKEN", [("kind", str), ("value", str)])


def parse_search_query(query: str) -> dict:
    """把过滤框里那串字解析成 pi 的三态查询(pi `parseSearchQuery`)。

    | 写法 | 语义 |
    | --- | --- |
    | `re:foo.*bar` | 正则(不分大小写);语法错 → `error` 非空,列表显示错误 |
    | `"node cve" fix` | 引号内是**短语**(连续子串),外部是模糊词 |
    | `abc def` | 全部模糊词,必须都命中 |

    引号不成对时退化成纯空格分词(pi 同款)—— 打字的中间态不该被判成错误。
    """
    trimmed = query.strip()
    if not trimmed:
        return {"mode": "tokens", "tokens": [], "error": None}
    if trimmed.startswith("re:"):
        pattern = trimmed[3:].strip()
        if not pattern:
            return {"mode": "regex", "regex": None, "error": "空正则"}
        try:
            return {"mode": "regex", "regex": re.compile(pattern, re.IGNORECASE), "error": None}
        except re.error as exc:
            return {"mode": "regex", "regex": None, "error": f"正则错误: {exc}"}
    tokens: list[SEARCH_TOKEN] = []
    buf = ""
    in_quote = False

    def flush(kind: str) -> None:
        nonlocal buf
        value = buf.strip()
        buf = ""
        if value:
            tokens.append(SEARCH_TOKEN(kind, value))

    for ch in trimmed:
        if ch == '"':
            if in_quote:
                flush("phrase")
                in_quote = False
            else:
                flush("fuzzy")
                in_quote = True
            continue
        if not in_quote and ch.isspace():
            flush("fuzzy")
            continue
        buf += ch
    if in_quote:                     # 引号没闭合:整串按空格分词,不报错
        return {"mode": "tokens", "error": None,
                "tokens": [SEARCH_TOKEN("fuzzy", t) for t in trimmed.split() if t]}
    flush("fuzzy")
    return {"mode": "tokens", "tokens": tokens, "error": None}


def match_session_text(text: str, parsed: dict) -> float | None:
    """一条会话是否命中查询;命中返回分数(越小越靠前),不命中返回 None。"""
    if parsed.get("error"):
        return None
    if parsed["mode"] == "regex":
        found = parsed["regex"].search(text)
        return None if found is None else found.start() * 0.1
    if not parsed["tokens"]:
        return 0.0
    total = 0.0
    normalized: str | None = None
    for token in parsed["tokens"]:
        if token.kind == "phrase":
            if normalized is None:
                normalized = " ".join(text.lower().split())
            phrase = " ".join(token.value.lower().split())
            index = normalized.find(phrase)
            if index < 0:
                return None
            total += index * 0.1
            continue
        score = fuzzy_score(token.value, text)
        if score is None:
            return None
        total += score
    return total


def session_tree_order(items: list[Session]) -> list[tuple[Session, int, bool, list[bool]]]:
    """把会话按 `parentSession` 串成树,再按前序遍历拍平(pi 的 `threaded` 排序)。

    每项返回 `(会话, 深度, 是否最后兄弟, 祖先是否延续)` —— 后两项就是树状前缀
    (`│  ` / `├─ ` / `└─ `)要用的东西。子节点按**最后活动时间倒序**排。

    父节点不在这一批里(父会话在别的目录 / 已被删)=> 当根处理 —— 不能因为找不到父节点
    就把整条会话藏起来。
    """
    by_path = {str(s.path): s for s in items}
    children: dict[str | None, list[Session]] = {}
    for session in items:
        parent = session.parent_session
        if parent is None or parent not in by_path:
            parent = None
        children.setdefault(parent, []).append(session)

    def latest(session: Session) -> float:
        """子树里最新的活动时间 —— 父节点跟着子节点一起往前排(pi 同款)。"""
        best = session.modified_ts
        for child in children.get(str(session.path), []):
            best = max(best, latest(child))
        return best

    out: list[tuple[Session, int, bool, list[bool]]] = []
    roots = sorted(children.get(None, []), key=lambda s: -latest(s))

    def walk(node: Session, depth: int, ancestor_continues: list[bool], is_last: bool) -> None:
        out.append((node, depth, is_last, ancestor_continues))
        # 非根的祖先要画竖线延续(pi 的 `depth > 0 ? !isLast : false`)
        continues = (not is_last) if depth > 0 else False
        kids = sorted(children.get(str(node.path), []), key=lambda s: -latest(s))
        for index, child in enumerate(kids):
            walk(child, depth + 1, [*ancestor_continues, continues], index == len(kids) - 1)

    for index, root in enumerate(roots):
        walk(root, 0, [], index == len(roots) - 1)
    return out


class SessionSelector(EditorSlotPanel):
    """`/resume` 的会话选择器(对齐 pi 的 `SessionSelectorComponent`)。

    外壳与其它选择器同款(`EditorSlotPanel`):贴底、全宽、上下 `─`、footer 之上。

    与 pi 的四处对齐:
      · **列表自己画**(`Static`,`_repaint_list`),不用 Textual 的 `OptionList` ——
        它自带 `tall $border-blurred` 边框与整行高亮底,而 pi 的列表是"一行一条、
        光标 `› `、选中整行 selectedBg、无边框无底色"(老的 `/resume` 就是这样漏了一整圈框);
      · 头部是**状态行**(范围 `◉ 当前目录 | ○ 全部` + `名字:` + `排序:`),
        键位提示**两行** —— pi 的 `SessionSelectorHeader` 是三行一组;
      · 排序三档 `threaded/recent/fuzzy`(pi 的 threaded/recent/relevance)、
        `tab` 切「当前目录 / 全部」、`re:<正则>` 与 `"短语"` 搜索语法;
      · `ctrl+d` 删除前先**确认**(pi 的 delete confirmation),且删不掉当前会话。

    返回选中的会话 id;取消返回 None。重命名与删除由 App 传进来的回调负责落盘,
    这里只负责交互与刷新 —— 改完重新 `provider()` 取一遍列表。
    """

    SORTS = ("threaded", "recent", "fuzzy")
    SORT_LABELS = {"threaded": "树状", "recent": "最近", "fuzzy": "最相关"}
    #: 列表最多显示几行(pi 的 `maxVisible = 10`)
    MAX_VISIBLE = 10
    BOX_ID = "session-box"
    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("enter", "confirm", "恢复", show=False),
        Binding("tab", "toggle_scope", "当前目录/全部", priority=True, show=False),
        Binding("ctrl+n", "toggle_named", "只看命名会话", priority=True, show=False),
        Binding("ctrl+s", "toggle_sort", "切换排序", priority=True, show=False),
        Binding("ctrl+p", "toggle_path", "显示/隐藏路径", priority=True, show=False),
        Binding("ctrl+r", "rename", "重命名", priority=True, show=False),
        Binding("ctrl+d", "delete", "删除", priority=True, show=False),
    ]

    def __init__(self, provider, *, on_rename, on_delete,
                 current: str | None = None, current_cwd: str | None = None) -> None:
        super().__init__()
        self._provider = provider          # () -> list[Session];结果缓存,见 `_all_sessions`
        self._on_rename = on_rename
        self._on_delete = on_delete
        self._current = current
        self._cwd = current_cwd
        self._named_only = False
        self._sort = "threaded"
        self._show_path = False
        self._scope = "current"            # current | all
        self._renaming: str | None = None
        self._renaming_from = ""
        self._confirming: str | None = None
        self._status: tuple[str, str] | None = None    # (文本, 色调)
        self._search_error: str | None = None
        self._rows: list[tuple[Session, str]] = []     # (会话, 树状前缀)
        self._cache: list[Session] | None = None       # `provider()` 的结果(见 `_all_sessions`)
        self._index = 0
        self._touched = False              # 用户动过方向键之后,刷新不再重置选中项
        self._list = Static("", id="session-list")

    PANEL_TITLE = "恢复会话"

    # -- 头部(状态行 + 两行键位提示)------------------------
    def _header_line(self) -> str:
        p = self.palette()
        if self._status is not None:        # 刚做完的动作用一行状态顶掉标题(pi 同款)
            text, tone = self._status
            return p.fg(tone, text)
        title = "恢复会话 · 当前目录" if self._scope == "current" else "恢复会话 · 全部"
        right = (f"名字:{'命名' if self._named_only else '全部'}"
                 f"  ·  排序:{self.SORT_LABELS.get(self._sort, self._sort)}")
        return f"{p.fg('accent', title)}{' ' * 3}{p.fg('muted', right)}"

    def _scope_line(self) -> str:
        p = self.palette()
        if self._scope == "current":
            return f"{p.fg('accent', '◉ 当前目录')}{p.fg('muted', ' | ○ 全部')}"
        return f"{p.fg('muted', '○ 当前目录 | ')}{p.fg('accent', '◉ 全部')}"

    def _keys_lines(self) -> tuple[str, str]:
        p = self.palette()
        if self._confirming is not None:
            # pi:确认删除时头部换成一句 error 色的问句,键位提示退成"enter/escape"
            return ("删除这个会话?enter 确认 · escape 取消", "")
        first = (p.fg("muted", "tab 切换范围 · 搜索:空格分词(模糊) · ")
                 + p.fg("muted", '"短语" 精确 · re:<正则> 正则'))
        second = p.fg("muted", " · ".join((
            "enter 恢复", "ctrl+s 排序", "ctrl+n 只看命名", "ctrl+d 删除",
            f"ctrl+p 路径({'on' if self._show_path else 'off'})", "ctrl+r 重命名",
            "escape 取消")))
        return first, second

    def title_text(self) -> str:
        # `#model-hint` 是 accent bold(pi 的面板标题);键位提示不该跟着变粗
        # → 除第一行外都用 `[not bold]` 退回(行内标记,比再拆一个 Static 便宜)
        not_bold = "[not bold]"
        if self._renaming is not None:
            p = self.palette()
            return "\n".join([
                p.fg("accent", "重命名会话"),
                "",
                not_bold + p.fg("muted", "enter 保存 · escape 取消(留空 = 不改)"),
            ])
        first, second = self._keys_lines()
        return "\n".join([self._header_line(), not_bold + self._scope_line(),
                          not_bold + first, not_bold + second])

    def _refresh_title(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#model-hint", Static).update(self.title_text())

    def hints_text(self) -> str:
        return ""                          # 提示行已经在标题块里(pi 也是多行一组)

    def compose_body(self) -> ComposeResult:
        yield prompt_row("session-filter", placeholder="输入以过滤(标题 / id / 内容)")
        yield self._list

    def on_panel_ready(self) -> None:
        self.query_one("#session-filter", Input).focus()
        self._refresh()

    # -- 列表 ---------------------------------------------------------
    def _all_sessions(self) -> list[Session]:
        """按范围取候选:当前目录(默认)/ 全部(pi 的 tab 切换)。

        **缓存 `provider()` 的结果**:它每次都把整个会话目录读进内存(`list()` 在本机
        3200 条会话上约 0.5s),而这里是在**每次按键**的路径上 —— 不缓存的话打字会卡。
        缓存由 `invalidate()` 在改名 / 删除后清掉(那两种操作会让列表内容变)。
        """
        if self._cache is None:
            self._cache = list(self._provider())
        items = self._cache
        if self._scope == "current" and self._cwd:
            same = [s for s in items if (s.cwd or "") == self._cwd]
            if same:
                return same
            # 当前目录一条都没有 → 回落到全部(pi 是提示"按 tab 看全部",qi 直接给全部:
            # 面板空着比"多列了几条"更让人困惑)
        return items

    def invalidate(self) -> None:
        """丢掉会话列表缓存(内容真的变了:改名 / 删除之后)。"""
        self._cache = None

    def _visible(self) -> list[Session]:
        return [session for session, _prefix in self._rows]

    def _recompute(self, *, keep: str | None = None) -> None:
        """过滤 + 排序 + 树状前缀,结果存进 `self._rows`。

        `keep` = 刷新后要尽量保持选中的会话(重命名 / 删除之后用)。
        """
        items = self._all_sessions()
        if self._named_only:
            items = [s for s in items if has_title(s.title)]
        query = self._input().value
        parsed = parse_search_query(query)
        self._search_error = parsed.get("error")
        scored: list[tuple[Session, float]] = []
        if not parsed.get("error"):
            if not query.strip():
                scored = [(s, 0.0) for s in items]
            else:
                for session in items:
                    score = match_session_text(session.search_text, parsed)
                    if score is not None:
                        scored.append((session, score))
        picked = [s for s, _ in scored]
        scores = {s.id: score for s, score in scored}

        if self._sort == "recent" and query.strip():
            # pi 的 recent = 「只过滤,保持原顺序(最近在前)」,不打分重排
            picked.sort(key=lambda s: -s.modified_ts)
        elif query.strip():
            # threaded / fuzzy 在有查询时都按**相关度**排(pi 的 `filterAndSortSessions`
            # 对 threaded 也是走 relevance 分支)—— 打了字还按时间排会让最相关的那条
            # 淹没在几十条弱命中里
            picked.sort(key=lambda s: (scores.get(s.id, 0.0), -s.modified_ts))
        elif self._sort == "threaded":
            self._rows = [(session, self._tree_prefix(depth, is_last, continues))
                          for session, depth, is_last, continues
                          in session_tree_order(picked)]
            self._restore_index(keep)
            return
        else:
            picked.sort(key=lambda s: -s.modified_ts)
        self._rows = [(s, "") for s in picked]
        self._restore_index(keep)

    @staticmethod
    def _tree_prefix(depth: int, is_last: bool, continues: list[bool]) -> str:
        """pi 的 `buildTreePrefix`:根不缩进,其余 `│  `/`   ` + `├─ `/`└─ `。"""
        if depth == 0:
            return ""
        parts = ["│  " if flag else "   " for flag in continues]
        return "".join(parts) + ("└─ " if is_last else "├─ ")

    def _restore_index(self, keep: str | None) -> None:
        if keep is not None:
            for index, (session, _prefix) in enumerate(self._rows):
                if session.id == keep:
                    self._index = index
                    return
        if not self._touched:
            # pi:用户还没动过选择时,选中项停在第一条(默认是最近的一条)
            self._index = 0
        self._index = max(0, min(self._index, max(0, len(self._rows) - 1)))

    def _row_text(self, session: Session, prefix: str, index: int, width: int) -> Text:
        """一行:光标 + 树状前缀 + 名字(没起过名时回落第一句话)+ 右对齐 `路径 消息数 年龄`。

        样式照 pi 的 `SessionList.render`:光标 `› ` 用 accent,**正文按状态上色**
        (当前 = accent、有名字 = warning、确认删除 = error),选中行整行 `selectedBg`;
        右侧信息一律 muted(确认删除时 error)。
        """
        p = self.palette()
        selected = index == self._index
        confirming = self._confirming == session.id
        text = " ".join(session.display_label.split())
        if confirming:
            text = f"删除? {text}"
        elif session.id == self._current:
            text = f"{text}  ← 当前"
        if session.branch_points:
            text += f"  ⑂{session.branch_points}"
        right = f"{session.message_count}  {format_age(session.modified_ts)}"
        if self._show_path:
            right = f"{self._short_path(str(session.path))}  {right}"
        elif self._scope == "all" and session.cwd:
            right = f"{self._short_path(session.cwd)}  {right}"
        head = prefix + text
        room = width - Text(head).cell_len - Text(right).cell_len - 3
        if room < 0:
            # 放不下:左半(名字/第一句话)优先,右侧信息用省略号收掉
            keep = max(4, width - Text(right).cell_len - 5)
            head = Text(head)[:keep].plain + "…"
            room = max(0, width - Text(head).cell_len - Text(right).cell_len - 3)
        tone = ("error" if confirming else "accent" if session.id == self._current
                else "warning" if has_title(session.title) else "text")
        line = Text()
        line.append("› " if selected else "  ", style=Style(color=p.hex("accent")))
        line.append(head, style=Style(color=p.hex(tone), bold=selected))
        line.append(" " * room)
        line.append(right, style=Style(color=p.hex("error") if confirming else p.hex("muted")))
        if selected:
            # pi 的选中行是**整行底色**(selectedBg)—— 一行一条的列表里,
            # 底色比只加粗好认,尤其名字长短不一时
            line.stylize(Style(bgcolor=p.hex("selectedBg")))
        return line

    @staticmethod
    def _short_path(path: str, keep: int = 2) -> str:
        """路径列:`~` 缩写;仍太长就只留末尾若干段(`…/Desktop/qi`)。

        不缩的话它会挤掉左边的名字 —— 而名字才是“这条会话是什么”的唯一线索。
        """
        shown = shorten_path(path)
        parts = [part for part in shown.split("/") if part]
        if Text(shown).cell_len <= 28 or len(parts) <= keep:
            return shown
        return "…/" + "/".join(parts[-keep:])

    def _repaint_list(self) -> None:
        if self._search_error:
            self._list.update(Text(f"  {self._search_error}",
                                   style=Style(color=self.palette().hex("error"))))
            return
        if not self._rows:
            hint = ("没有命名过的会话。ctrl+n 显示全部。" if self._named_only
                    else "当前目录没有会话。tab 看全部。")
            self._list.update(Text(f"  {hint}", style=Style(color=self.palette().hex("muted"))))
            return
        span = min(self.MAX_VISIBLE, len(self._rows))
        start = max(0, min(self._index - span // 2, len(self._rows) - span))
        end = start + span
        width = self.body_width()          # 循环外算一次:每行都 query 一次盒子没必要
        out = Text()
        for index in range(start, end):
            if out.plain:
                out.append("\n")
            session, prefix = self._rows[index]
            out.append_text(self._row_text(session, prefix, index, width))
        if start > 0 or end < len(self._rows):
            out.append("\n")
            out.append(f"  ({self._index + 1}/{len(self._rows)})",
                       style=Style(color=self.palette().hex("muted")))
        self._list.update(out)

    def rendered_rows(self) -> list[tuple[Session, str]]:
        """当前列表内容(诊断/测试用)。"""
        return list(self._rows)

    def _refresh(self, *, keep: str | None = None) -> None:
        self._recompute(keep=keep)
        self._repaint_list()
        self._refresh_title()

    def _highlighted_id(self) -> str | None:
        if not self._rows:
            return None
        index = max(0, min(self._index, len(self._rows) - 1))
        return self._rows[index][0].id

    def _input(self) -> Input:
        return self.query_one("#session-filter", Input)

    def _flash_status(self, text: str, tone: str = "accent") -> None:
        self._status = (text, tone)

    # -- 键位 ---------------------------------------------------------
    def action_cancel(self) -> None:
        if self._renaming is not None:          # 先退出重命名,再考虑关面板
            self._end_rename()
            return
        if self._confirming is not None:        # 删除确认先取消
            self._confirming = None
            self._refresh(keep=self._highlighted_id())
            return
        self.dismiss(None)

    def action_confirm(self) -> None:
        """enter:重命名态 = 保存;删除确认态 = 真的删;否则恢复选中的会话。"""
        if self._renaming is not None:
            self._commit_rename()
            return
        if self._confirming is not None:
            target, self._confirming = self._confirming, None
            self._on_delete(target)
            self.invalidate()                       # 删掉了:缓存里的那条不能再出现
            self._flash_status("会话已删除")
            self._refresh()
            return
        session_id = self._highlighted_id()
        if session_id is not None:
            self.dismiss(session_id)

    def action_move(self, step: int) -> None:
        if not self._rows:
            return
        self._touched = True
        self._index = max(0, min(self._index + step, len(self._rows) - 1))
        self._repaint_list()

    def action_toggle_scope(self) -> None:
        if self._renaming is not None:
            return                             # 重命名时 tab 留给输入框补全
        self._scope = "all" if self._scope == "current" else "current"
        self._touched = False
        self._status = None
        self._refresh()

    def action_toggle_named(self) -> None:
        self._named_only = not self._named_only
        self._touched = False
        self._refresh()

    def action_toggle_sort(self) -> None:
        self._sort = self.SORTS[(self.SORTS.index(self._sort) + 1) % len(self.SORTS)]
        self._touched = False
        self._refresh()

    def action_toggle_path(self) -> None:
        self._show_path = not self._show_path
        self._refresh(keep=self._highlighted_id())

    def action_rename(self) -> None:
        if self._renaming is not None:
            self._commit_rename()
            return
        session_id = self._highlighted_id()
        if session_id is None:
            return
        row = next((s for s, _ in self._rows if s.id == session_id), None)
        self._renaming = session_id
        self._renaming_from = self._input().value
        box = self._input()
        box.value = (row.title if row is not None and has_title(row.title) else "")
        box.cursor_position = len(box.value)
        self._refresh_title()

    def action_delete(self) -> None:
        session_id = self._highlighted_id()
        if session_id is None:
            return
        if session_id == self._current:
            # pi:不能删当前会话 —— 错误显示在头部状态行上
            self._flash_status("不能删除当前会话", "error")
            self._refresh_title()
            return
        self._confirming = session_id             # 先确认(pi 的 delete confirmation)
        self._refresh(keep=session_id)

    def _end_rename(self) -> None:
        self._renaming = None
        self._input().value = self._renaming_from
        self._refresh(keep=self._highlighted_id())

    def _commit_rename(self) -> None:
        session_id, self._renaming = self._renaming, None
        name = self._input().value.strip()
        self._input().value = self._renaming_from
        if session_id and name:
            self._on_rename(session_id, name)
            self.invalidate()                       # 名字变了:缓存里的旧名要重取
            self._flash_status(f"已重命名为 {name}")
        self._refresh(keep=session_id)

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._renaming is None:              # 重命名时输入框是名字,不是过滤器
            self._touched = False
            self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_confirm()


TREE_FILTERS = ("default", "no-tools", "user-only", "labeled-only", "all")
"""`/tree` 的过滤模式(对齐 pi 的 app.tree.filter.*:ctrl+d/t/u/l/a + ctrl+o 循环)。"""

TREE_FILTER_HINTS = {
    "default": "默认(隐藏状态类)",
    "no-tools": "隐藏工具结果",
    "user-only": "只看用户消息",
    "labeled-only": "只看有标签",
    "all": "全部条目",
}


class TreeRow(NamedTuple):
    """树里的一行:`text` 是展示文本,`label` 是原标签(供编辑时回填)。"""

    id: str
    text: str
    label: str = ""


def entry_passes_tree_filter(entry: dict, mode: str) -> bool:
    """pi 的 tree filter 语义(对齐 applyFilter 的 switch)。

    默认视图隐藏「设置类」entry(`model_change` / `thinking_level_change`):它们是
    时间线上的事实(供还原与诊断),但在树里每一行都显示一遍"模型: x/y"只会淹没对话。
    `all` 视图仍能看到它们 —— pi 的 `isSettingsEntry` 也是这个口径。
    """
    kind = entry.get("type")
    if mode == "user-only":
        return kind == "message" and entry.get("role") == "user"
    if mode == "labeled-only":
        return bool(str(entry.get("label") or "").strip())
    if mode == "no-tools":
        return kind not in ("state", "tool")
    if mode == "all":
        return True
    return kind not in ("state", "model_change", "thinking_level_change")


class TreeSelector(EditorSlotPanel):
    """`/tree` 的会话树选择器(对齐 pi 的 tree filter 与标签键位)。

    返回要跳到的 entry id;取消返回 None。标签直接在环上改(回调负责落盘),
    改完重新 `provider()` 取一遍行。
    """

    BOX_ID = "session-box"
    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("ctrl+d", "set_filter('default')", "默认视图", priority=True, show=False),
        Binding("ctrl+t", "set_filter('no-tools')", "隐藏工具结果", priority=True, show=False),
        Binding("ctrl+u", "set_filter('user-only')", "只看用户消息", priority=True, show=False),
        Binding("ctrl+l", "set_filter('labeled-only')", "只看有标签", priority=True, show=False),
        Binding("ctrl+a", "set_filter('all')", "全部条目", priority=True, show=False),
        Binding("ctrl+o", "cycle_filter", "循环过滤", priority=True, show=False),
        Binding("shift+ctrl+o", "cycle_filter_back", "反向循环", priority=True, show=False),
        Binding("shift+l", "edit_label", "编辑标签", priority=True, show=False),
        Binding("shift+t", "toggle_label_time", "标签时间戳", priority=True, show=False),
    ]

    def __init__(self, provider, *, on_label, current: str | None = None) -> None:
        super().__init__()
        self._provider = provider      # (mode, query, show_label_time) -> list[TreeRow]
        self._on_label = on_label
        self._current = current
        self._mode = "default"
        self._show_label_time = False
        self._editing: str | None = None

    def title_text(self) -> str:
        return self._hint()

    def _refresh_title(self) -> None:
        self.query_one("#model-hint", Static).update(self.title_text())

    def compose_body(self) -> ComposeResult:
        yield prompt_row("session-filter", placeholder="输入搜索节点(空格分词)")
        yield OptionList(id="tree-list")

    def on_panel_ready(self) -> None:
        self.query_one("#session-filter", Input).focus()
        self._refresh()

    def _input(self) -> Input:
        return self.query_one("#session-filter", Input)

    def _hint(self) -> str:
        if self._editing is not None:
            return "编辑标签:输入后 enter 保存(留空 = 清除),escape 取消"
        return (f"会话树 · 过滤={TREE_FILTER_HINTS.get(self._mode, self._mode)}"
                + (" · 标签带时间" if self._show_label_time else "")
                + "   ↑↓ 选择 · enter 跳转 · ctrl+d/t/u/l/a 过滤 · ctrl+o 循环 · "
                  "shift+l 标签 · shift+t 标签时间 · escape 取消")

    def _rows(self) -> list[TreeRow]:
        return self._provider(self._mode, self._input().value.strip(), self._show_label_time)

    def _refresh(self) -> None:
        listing = self.query_one("#tree-list", OptionList)
        listing.clear_options()
        rows = self._rows()
        for row in rows:
            listing.add_option(Option(row.text, id=row.id))
        if rows:
            listing.highlighted = 0
        self._refresh_title()

    def _highlighted_id(self) -> str | None:
        listing = self.query_one("#tree-list", OptionList)
        if not listing.option_count:
            return None
        index = listing.highlighted if listing.highlighted is not None else 0
        return str(listing.get_option_at_index(index).id)

    # -- 键位 ---------------------------------------------------------
    def action_cancel(self) -> None:
        if self._editing is not None:
            self._end_edit()
            return
        self.dismiss(None)

    def action_set_filter(self, mode: str) -> None:
        if mode in TREE_FILTERS:
            self._mode = mode
            self._refresh()

    def action_cycle_filter(self) -> None:
        self._step_filter(1)

    def action_cycle_filter_back(self) -> None:
        self._step_filter(-1)

    def _step_filter(self, step: int) -> None:
        index = (TREE_FILTERS.index(self._mode) + step) % len(TREE_FILTERS)
        self._mode = TREE_FILTERS[index]
        self._refresh()

    def action_toggle_label_time(self) -> None:
        self._show_label_time = not self._show_label_time
        self._refresh()

    def action_edit_label(self) -> None:
        if self._editing is not None:
            self._commit_label()
            return
        entry_id = self._highlighted_id()
        if entry_id is None:
            return
        current = next((row.label for row in self._rows() if row.id == entry_id), "")
        self._editing = entry_id
        box = self._input()
        box.value = current
        box.cursor_position = len(box.value)
        self._refresh_title()

    def _end_edit(self) -> None:
        self._editing = None
        self._input().value = ""
        self._refresh()

    def _commit_label(self) -> None:
        entry_id, self._editing = self._editing, None
        label = self._input().value.strip()
        if entry_id:
            self._on_label(entry_id, label)
        self._end_edit()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._editing is None:
            self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._editing is not None:
            self._commit_label()
            return
        entry_id = self._highlighted_id()
        if entry_id is not None:
            self.dismiss(entry_id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))


class _TuiUi:
    """`ctx.ui` 的 TUI 后端(把扩展的交互请求接到 qi 的模态框上)。

    为什么可以直接 `await`:扩展 handler 跑在 TUI 进程的 worker 里(**同一个事件循环**),
    所以“弹模态 + 等结果”就是一个 future。web 不行(前端在浏览器里),那边要经 AG-UI 的
    Custom 事件往返 —— 归 P-E5。

    escape 关掉模态 = “没回答”,一律回落到调用方给的 `default`,与无界面时的语义一致 ——
    否则“取消”在 TUI 里和 `-p` 里会得到不同结果,而扩展没办法同时处理两种。
    """

    def __init__(self, app: Any) -> None:
        # `app` 只鸭子用到两样:`_note(text, tone)` 与 `await_screen(screen)`。
        # 不写成 `QiTui`:测试用 stub app 时不渲染,而且用 Protocol 声明一个只为了
        # 写注解的接口, lint 与类型检查器都不认(试过两个写法都报)。
        self._app = app

    def notify(self, message: str, *, level: str = "info") -> None:
        # 扩展的 notify 等级 → 色调:pi 的 `info` 在提示块里用 `dim`(比正文轻)
        tone = {"error": "error", "warning": "warning", "warn": "warning"}.get(level, "dim")
        self._app._note(message, tone)

    async def confirm(self, message: str, *, title: str | None = None,
                      default: bool = False) -> bool:
        picked = await self._app.await_screen(PickerScreen(
            f"{title or '确认'}:{message}",
            [("yes", "是(默认)" if default else "是"),
             ("no", "否(默认)" if not default else "否")]))
        if picked is None:
            return default
        return picked == "yes"

    async def select(self, message: str, options: list[str], *,
                     title: str | None = None, default: str | None = None) -> str | None:
        picked = await self._app.await_screen(PickerScreen(
            f"{title or '选择'}:{message}", [(o, o) for o in options], current=default))
        return None if picked is None else str(picked)

    async def input(self, message: str, *, title: str | None = None,
                    default: str | None = None, secret: bool = False) -> str | None:
        return await self._app.await_screen(PromptScreen(
            f"{title or '输入'}:{message}", default=default or "", secret=secret))

    # ── 数据层:多行编辑 / 状态 / 外观(pi 的 ctx.ui 数据层)──
    async def editor(self, prefill: str = "", *, title: str | None = None) -> str | None:
        return await self._app.await_screen(EditorScreen(title or "编辑", prefill))

    def set_status(self, key: str, text: str | None) -> None:
        self._app.set_extension_status(key, text)

    def set_title(self, title: str) -> None:
        self._app.set_terminal_title(title)

    def set_working_message(self, message: str | None = None) -> None:
        self._app.set_working_message(message)

    def set_working_visible(self, visible: bool) -> None:
        self._app.set_working_visible(visible)

    def set_working_indicator(self, options: dict | None = None) -> None:
        self._app.set_working_indicator(options or {})

    def set_hidden_thinking_label(self, label: str | None = None) -> None:
        self._app.set_hidden_thinking_label(label)

    def get_tools_expanded(self) -> bool:
        return bool(getattr(self._app, "_expanded", False))

    def set_tools_expanded(self, expanded: bool) -> None:
        self._app.set_tools_expanded(bool(expanded))

    @property
    def theme(self) -> Any:
        return getattr(self._app, "_palette", None)

    def get_all_themes(self) -> list[dict]:
        return self._app.all_themes()

    def get_theme(self, name: str) -> Any:
        return self._app.theme_by_name(name)

    def set_theme(self, theme: Any) -> dict:
        return self._app.apply_theme(theme)

    def paste_to_editor(self, text: str) -> None:
        current = self._app.get_editor_text()
        self._app.set_editor_text(current + str(text))

    def set_editor_text(self, text: str) -> None:
        self._app.set_editor_text(text)

    def get_editor_text(self) -> str:
        return self._app.get_editor_text()

    # ── 组件层(TUI-only;pi 的 setWidget / custom / setFooter / setHeader)──
    def set_widget(self, key: str, content: Any,
                   options: dict | None = None) -> None:
        self._app.set_extension_widget(key, content, options or {})

    async def custom(self, factory: Any, options: dict | None = None) -> Any:
        return await self._app.await_custom(factory, options or {})

    def set_footer(self, factory: Any) -> None:
        self._app.set_extension_footer(factory)

    def set_header(self, factory: Any) -> None:
        self._app.set_extension_header(factory)

    def set_editor_component(self, factory: Any) -> None:
        self._app.set_editor_component_impl(factory)

    def get_editor_component(self) -> Any:
        return self._app.get_editor_component_impl()

    def add_autocomplete_provider(self, factory: Any) -> Any:
        return self._app.add_autocomplete_provider_impl(factory)

    def on_terminal_input(self, handler: Any) -> Any:
        return self._app.add_terminal_input_handler(handler)


class QiTui(App):
    TITLE = "qi"
    SUB_TITLE = "单 agent · 扩展"

    CSS = """
    /* 两种模式共用:pi 没有滚动条/边框 chrome,只剩内容本身 */
    Screen { height: 100%; background: transparent; scrollbar-size: 0 0; }
    #body { height: 100%; background: transparent; scrollbar-size: 0 0; }
    /* fullscreen:qi 拥有视口 —— transcript 用 1fr 占满剩余空间并在**内部**滚动,
       输入框 / footer 固定在底部(滚轮只滚 transcript,翻不到 shell 历史)。 */
    #log { height: 1fr; background: transparent; scrollbar-size: 0 0; }
    /* regular(inline):不占全屏、不进备用屏 —— Screen 高度随内容,transcript 的高度
       由 `_sync_log_height()` 按终端高算(不能写 `1fr`:inline 下没有“屏幕高度”可分配)。 */
    Screen.regular { height: auto; max-height: 100%; }
    Screen.regular #body { height: auto; }
    Screen.regular #log { height: auto; }
    /* 扩展挂件的两个槽:空时**不占行**(Textual 的 Vertical 默认 `height: 1fr`,
       不压下去会在输入框与 footer 之间各撑出一块空白)。 */
    #ext-widgets-above, #ext-widgets-below { height: auto; background: transparent; }
    .blank { height: 1; background: transparent; }
    .msg { width: 1fr; height: auto; }
    #border-top, #border-bottom { height: 1; background: transparent; }
    .compaction-label, .compaction-body { background: transparent; height: auto; }
    #editor { border: none; height: auto; max-height: 8; padding: 0 1; background: transparent; }
    #editor .text-area--cursor-line { background: transparent; }
    /* `/` 与 `@` 补全面板(pi 的 autocomplete):默认隐藏,有候选才显示。
       版式由 `CompletionPanel` 自己画 —— 这里只管显隐/宽度/缩进,不给底色与边框。 */
    #completions { display: none; width: 1fr; height: auto;
                   padding: 0 1; background: transparent; }
    #completions.visible { display: block; }
    #footer { height: auto; width: 1fr; background: transparent; scrollbar-size: 0 0; }
    /* 命令选择器 / 输入面板:pi 的 `showSelector()` 是把**编辑器那一格**的内容换掉
       (`editorContainer.clear()`),所以外形与编辑器一致 —— 全宽、贴底(footer 之上)、
       上下 `─`(pi 的 DynamicBorder)、**没有圆角也没有遮罩**。
       底色取 `$background`(= 探测到的终端底色)⇒ 看不出“填色”但又盖住底下的编辑器。 */
    ModalScreen { align-vertical: bottom; background: transparent; }
    #model-box, #session-box {
        width: 1fr;
        height: auto;
        max-height: 80%;
        background: $background;
        border: none;
        border-top: solid $secondary;
        border-bottom: solid $secondary;
        padding: 0 1;
    }
    /* pi 的面板标题是 accent bold;键位提示行是 dim(非粗体:行内用 `[not bold]` 退回) */
    #model-hint { color: $primary; text-style: bold; }
    #panel-hints { color: $text-muted; }
    .panel-gap { height: 1; }
    #model-list, #scoped-list { background: transparent; }
    /* 会话选择器的列表是**自己画的一列文本**(`Static`):pi 的列表没有边框、没有底色,
       选中行是整行 selectedBg。Textual 的 `OptionList` 自带 `tall $border-blurred`
       边框与整行高亮底 —— 那圈框就是以前 `/resume` 与 `/model` 不一致的来源。 */
    #session-list { background: transparent; height: auto; max-height: 11; width: 1fr; }
    /* OptionList(/tree 与 scoped 列表)去掉自带的 `tall` 边框、`$surface` 底与整行高亮底,
       只把选中行提亮成 accent —— pi 的列表没有边框也没有行底色。 */
    #tree-list, #scoped-list { border: none; padding: 0; height: auto; max-height: 50%;
                               background: transparent; }
    #scoped-list > .option-list--option-highlighted,
    #scoped-list:focus > .option-list--option-highlighted,
    #tree-list > .option-list--option-highlighted,
    #tree-list:focus > .option-list--option-highlighted {
        background: transparent;
        color: $primary;
        text-style: bold;
    }
    /* pi-tui 的 `Input` = `prompt + value`:一行纯文本,没有框与底色 */
    .input-row { height: 1; }
    .input-prompt { width: 2; }
    #prompt-input, #session-filter {
        border: none;
        background: transparent;
        padding: 0;
        height: 1;
        width: 1fr;
    }
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

    # 模态(选择器)打开时关掉的应用级快捷键。
    #
    # 为什么必须关:Textual 的 priority 绑定是**从 App 往下**检查的
    # (`App._check_bindings` 用 `reversed(screen._binding_chain)`),所以 App 级
    # ctrl+d/o/t/l/p/x/c 会盖掉模态里同名的键 —— 而 pi 的会话/树选择器恰好全用
    # 这些键(删除 / 过滤 / 跳转)。`check_action` 返回 False 后 Textual 会继续
    # 往下找模态自己的 priority 绑定。
    MODAL_BLOCKED_ACTIONS = frozenset({
        "interrupt", "clear_or_exit", "exit_or_delete", "toggle_expand",
        "toggle_thinking", "copy_answer", "external_editor", "select_model",
        "cycle_model", "cycle_model_back", "suspend_process",
    })

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if len(self.screen_stack) > 1 and action in self.MODAL_BLOCKED_ACTIONS:
            return False
        return True

    def __init__(self, runtime: QiRuntime | None = None, initial_prompt: str | None = None,
                 palette: Palette | None = None, session_id: str | None = None,
                 cont: bool = False, fork_id: str | None = None,
                 no_session: bool = False, name: str | None = None,
                 approve_project: bool | None = None,
                 extension_flags: list[str] | None = None,
                 extra_extension_paths: list[Path] | None = None,
                 tools: str | None = None, exclude_tools: str | None = None,
                 no_tools: bool = False, no_builtin_tools: bool = False,
                 append_system_prompt: list[str] | None = None,
                 resume: bool = False, exact_session_id: str | None = None,
                 base_prompt_override: str | None = None,
                 no_extensions: bool = False, no_context_files: bool = False,
                 session_dir_path: str | None = None,
                 model_override: str | None = None, api_key: str | None = None,
                 scoped_models: list[str] | None = None,
                 tui_mode: str | None = None):
        super().__init__()
        # TUI 模式(pi 同名键;`--tui-mode` 压过 settings)。**qi 默认 fullscreen** ——
        # 为什么不是 pi 的 regular:见 `docs/tui.md` §1。
        self._tui_mode = tui_mode if tui_mode in TUI_MODES else DEFAULT_TUI_MODE
        self._fullscreen = self._tui_mode == "fullscreen"
        self._rt = runtime
        # 项目信任的三态(None = 看 settings.defaultProjectTrust);`qi -a` / `-na` 透传到这里
        self._approve_project = approve_project
        # `qi --ext name=value` 原样带下去 —— 解析要等扩展声明完(在 QiRuntime 里)
        self._extension_flags = list(extension_flags or [])
        # `qi -e <dir>`:一次性试用目录(scope=temporary)。两个构造点都要带上,
        # 否则 `/reload` 之后试用的扩展会静静消失。
        self._extra_extension_paths = list(extra_extension_paths or [])
        # `qi --append-system-prompt`:与 `-e` / 工具旗标同理 —— 两个构造点都要带上,
        # 否则 `/reload` 之后追加的那段会静谧消失。
        self._append_system_prompt = list(append_system_prompt or [])
        # `-r/--resume` 与 `--session-id`:会话选择参数(与 headless 路径同义)
        self._resume = resume
        self._exact_session_id = exact_session_id
        # 只在**构造 QiRuntime** 时用到的参数 —— 打成一包,两个构造点都带下去。
        # 为什么必须两个点都带:否则 `/reload` 之后这些"本次运行的覆盖"会静静失效
        # (工具集、基座、模型、密钥、会话目录全变回默认,而用户以为还是他给的那套)。
        self._runtime_extra: dict[str, Any] = {
            "tools": tools, "exclude_tools": exclude_tools,
            "no_tools": no_tools, "no_builtin_tools": no_builtin_tools,
            "append_system_prompt": self._append_system_prompt,
            "base_prompt_override": base_prompt_override,
            "no_extensions": no_extensions, "no_context_files": no_context_files,
            "session_dir_path": session_dir_path,
            "model_override": model_override, "api_key": api_key,
            "scoped_models": scoped_models,
        }
        # `qi -t/-xt/-nt/-nbt`:工具收窄的四个旗标。与 `-e` 同理 —— 两个构造点都要带上,
        # 否则 `/reload` 之后工具集又变回完整的(而用户以为收窄还生效)。
        self._tools = tools
        self._exclude_tools = exclude_tools
        self._no_tools = no_tools
        self._no_builtin_tools = no_builtin_tools
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
        self._shown_name = "?"
        self._working = False
        self._frame = 0
        self._frame_timer = None
        self._abort: AbortSignal | None = None   # 当前回合的中断信号(escape 用)
        self._expanded = False
        self._live: AssistantMessage | None = None
        # (块, 工具名, 输出)。块可能是内置 `ToolBlock`,也可能是扩展渲染的
        # `ExtensionToolBlock` —— 两者都提供 set_state / set_output。
        self._tool_blocks: list[tuple[Any, str, str]] = []
        #: 正在跑的工具块(**按 `tool_call_id` 配对** —— 并发执行时会有多条同时在途)
        self._current_tools: dict[str, Any] = {}
        self._model: ResolvedModel | None = None
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0}
        # 消息队列(pi 的 steer / follow-up):回合进行中提交的消息不并发跑,而是排队。
        # 必须在 _default_status() 之前初始化 —— 它会把排队数写进状态行。
        self._pending_steer: list[str] = []
        self._pending_follow: list[str] = []
        # 扩展写的状态行片段:必须在 `_default_status()` 之前初始化(它会拼进状态行)
        self._ext_statuses: dict[str, str] = {}
        self._status = self._default_status()
        self.footer_text = Text("")
        self._branch: str | None = None
        self._last_answer = ""
        self._exit_armed = False
        self._last_escape = 0.0              # 双击 escape(pi 的 doubleEscapeAction)
        # settings 驱动的界面参数(on_mount 里按实际设置覆盖)
        self._output_pad = 1
        self._completion_rows = COMPLETION_ROWS
        self._quiet_startup = False
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
        # ── 扩展 UI(ctx.ui 的数据层 + setWidget)────────────────
        #: `ctx.ui.set_status` 写的状态行片段(`_ext_statuses` 在上面、`_default_status()` 之前)
        #: `ctx.ui.set_working_message` / `set_working_visible` / `set_working_indicator`
        self._working_message: str | None = None
        self._working_visible = True
        self._working_frames: list[str] = list(SPINNER_FRAMES)
        self._working_interval = SPINNER_INTERVAL
        #: `ctx.ui.set_hidden_thinking_label`
        self._hidden_thinking_label: str | None = None
        #: `ctx.ui.set_widget` 挂的常驻 widget(`key -> widget`)
        self._ext_widgets: dict[str, Any] = {}
        #: `ctx.ui.set_editor_component` 的工厂(`None` = 内置 Editor)
        self._editor_component_factory: Any = None
        #: `ctx.ui.add_autocomplete_provider` 的补全提供者
        self._autocomplete_providers: list[Any] = []
        #: async 补全的回填(`((当时的文本, 光标偏移), 候选)`)—— 文本变了就作废
        self._async_completion: tuple[tuple[str, int], list[Candidate]] | None = None
        #: `ctx.ui.set_footer` / `set_header` 的工厂(挂出的组件带 `.ext-footer` / `.ext-header`)
        self._ext_footer: Any = None
        self._ext_header: Any = None
        #: `ctx.ui.on_terminal_input` 的处理器(按注册顺序逐个试)
        self._terminal_input_handlers: list[Any] = []

    # -- 布局 -----------------------------------------------------------
    def get_default_screen(self) -> Screen:
        """默认屏幕:regular 模式打上 `.regular` 类,让 CSS 选到 inline 布局。

        必须在**首屏创建时**就带上 —— 若放到 `on_mount` 里再加,inline 启动的第一帧会按
        fullscreen 规则把 Screen 撑到终端高,内联区域会先空掉一屏再缩回去。
        """
        return Screen(id="_default", classes="" if self._fullscreen else "regular")

    def compose(self) -> ComposeResult:
        with Vertical(id="body"):
            yield Transcript(id="log")
            yield Vertical(id="ext-widgets-above")
            yield Static("", id="border-top")
            yield Editor(id="editor")
            # 补全面板在编辑器**下边框之下**(pi 的 editor 把 SelectList 画在
            # renderBottomBorder() 之后),不是输入框上方。
            yield Static("", id="border-bottom")
            yield CompletionPanel(self._palette, id="completions")
            yield Vertical(id="ext-widgets-below")
            yield Static("", id="footer")

    def on_mount(self) -> None:
        self.console.push_theme(rich_theme(self._palette))
        try:
            if self._rt is None:
                # `ui_frontend` 在这里装上:`ctx.ui.confirm/select/input` 才真会问人
                self._rt = QiRuntime(has_ui=True, approve_project=self._approve_project,
                                     ui_frontend=_TuiUi(self),
                                     extension_flags=self._extension_flags,
                                     extra_extension_paths=self._extra_extension_paths,
                                     **self._runtime_extra)
            self._bind_extension_shortcuts()
            self._renderer = TuiRenderer(self._palette, self._rt.cwd)
            self._select_session()
            if self._resume:
                # `-r/--resume`:起来就开选择器(与 `/resume` 同一条路)。放到 on_mount
                # 之后一拍再弹 —— 挂载中途 push_screen 会让内联布局还没量完尺寸。
                self.call_after_refresh(self._show_session_selector)
            # 模型/级别**先取 runtime 当前的那一份**:`_select_session()` 已经 bind 过会话,
            # 所以这里拿到的可能是会话里记的旧值,而不是 settings 默认(pi 的 restoredModel)。
            # 命令行显式给了 `--model` 时 runtime 不会还原,两边自然一致。
            self._sync_model_from_runtime()
            if self._model is None:
                try:
                    self._model = resolve_default_model(self._rt.cfg, self._rt.cwd)
                except ConfigError:
                    self._model = None
            self._apply_ui_settings()
            # P-E4c:技能是 core 的能力(顶层六层来源),不再从 agent 汇总;
            # agent 列表那一段 banner 里不再有(core 没有角色概念了)。
            # 资源清单对齐 pi:紧凑态只列名字,`[Extensions]` 与 `[Skills]` 并列。
            skills = sorted({s.name for s in self._rt.top_skills})
            extensions = sorted({str(name) for name in
                                 (getattr(self._rt, "extensions", None) or ())})
            banner = None if self._quiet_startup else self._renderer.banner(
                _version(), skills, extensions)
            if self._session is not None and self._session.branch():
                self._replay_branch(self._session, banner=banner)   # 恢复历史(banner 在最上)
            elif banner is not None:
                self._append(Static(banner, classes="msg"))
            if self._startup_note:
                tone = "warning" if "不存在" in self._startup_note else "dim"
                self._note(self._startup_note, tone)
            # runtime 攒下的启动提示(未信任跳过项目级扩展、旧 plugins/ 目录残留…)。
            # runtime 自己不打印(不做 IO),所以展示归前端。
            for note in getattr(self._rt, "notes", []):
                self._note(note, "warning")
            # `--ext` 打错属致命:tui 没法给退出码,至少用红色说清楚旗标没生效
            for problem in getattr(self._rt, "flag_errors", []):
                self._note(problem, "error")
        except (LoadError, ConfigError) as exc:
            self._append(Static(Text(f"启动失败: {exc}", style=self._palette.hex("error")),
                                classes="msg"))
            self._rt = None
        self._repaint_borders()
        self._refresh_footer()
        self._sync_log_height()
        self.query_one("#editor", TextArea).focus()
        if self._initial_prompt and self._rt is not None:
            self.call_after_refresh(self._submit, self._initial_prompt)

    # -- 基础操作 -------------------------------------------------------
    def _apply_ui_settings(self) -> None:
        """把 settings 里已接的界面参数落到运行中的控件上(pi 的 settings-manager 同名 getter)。"""
        settings = getattr(self._rt, "settings", None)

        def number(name: str, default: int) -> int:
            try:
                value = int(getattr(settings, name, default))
            except (TypeError, ValueError):
                return default
            return max(0, value)

        self._quiet_startup = bool(getattr(settings, "quietStartup", False))
        self._show_thinking = not bool(getattr(settings, "hideThinkingBlock", False))
        self._output_pad = number("outputPad", 1)
        self._completion_rows = max(1, number("autocompleteMaxVisible", COMPLETION_ROWS))
        editor_pad = number("editorPaddingX", 1)
        editor = self.query_one("#editor", TextArea)
        editor.styles.padding = (0, editor_pad)
        # 补全面板与编辑器同缩进(pi 用同一个 `paddingX` 画 SelectList)
        self.query_one("#completions", CompletionPanel).set_padding(editor_pad)

    # -- 渲染回调(pi 的 renderCall / renderResult / register*Renderer / markdown)------
    def _renderers(self) -> Any:
        rt = self._rt
        return getattr(rt, "renderers", None) if rt is not None else None

    def _ext_ctx(self) -> Any:
        # 测试里的 FakeRuntime 只有 UI 那几样 —— 缺 `extension_ctx` 时当“没上下文”处理,
        # 而不是让渲染路径因为一个 AttributeError 把工具卡弄没。
        if self._rt is None:
            return None
        factory = getattr(self._rt, "extension_ctx", None)
        return factory() if callable(factory) else None

    def _tool_def(self, name: str) -> Any:
        """取工具定义(没有 catalog 的宿主返回 None)。"""
        catalog = getattr(self._rt, "catalog", None) if self._rt is not None else None
        getter = getattr(catalog, "get", None)
        return getter(name) if callable(getter) else None

    def _transform_markdown(self, text: str) -> str:
        """`register_markdown_transformer`:渲染前链式改写(user / assistant 的最终文本)。"""
        renderers = self._renderers()
        if renderers is None or renderers.is_empty:
            return text
        return renderers.apply_markdown(text, self._ext_ctx())

    def _extension_widget(self, fn: Any, payload: Any) -> Any:
        """调一个渲染回调并返回组件。按形参个数喂 `(payload, ctx)` —— pi 的三参
        `(x, theme, context)` 写法会拿到 `(payload, ctx, None)`,不报错。"""
        if not callable(fn):
            return None
        try:
            arity = len(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            arity = 0
        args = (payload, self._ext_ctx(), None)[:max(0, arity)]
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 扩展的渲染回调坏不该把卡片弄没
            self._note(f"扩展渲染回调失败: {type(exc).__name__}: {exc}", "warning")
            return None

    def _tool_block(self, name: str, args: dict, *, state: str = "pending") -> Any:
        """建工具块:有 `render_call` 就用扩展的组件,否则用内置 `ToolBlock`。"""
        tool = self._tool_def(name)
        render_call = getattr(tool, "render_call", None) if tool is not None else None
        if render_call is not None:
            widget = self._extension_widget(render_call, args)
            if widget is not None:
                shell = str(getattr(tool, "render_shell", "") or "") != "self"
                block = ExtensionToolBlock(self._palette, shell=shell)
                block.set_state(state)
                try:
                    block.show(widget)
                    return block
                except Exception as exc:  # noqa: BLE001 挂载失败 → 退回内置
                    self._note(f"扩展工具渲染失败({name}): {exc}", "warning")
        block = ToolBlock(self._renderer.tool_title(name, args), self._palette)
        block.set_state(state)
        return block

    def _tool_result_widget(self, name: str, result: str) -> Any:
        """`tool_end` 时:有 `render_result` 就让扩展自己画结果,否则返回 None(走内置)。"""
        tool = self._tool_def(name)
        render_result = getattr(tool, "render_result", None) if tool is not None else None
        if render_result is None:
            return None
        return self._extension_widget(render_result, result)

    # -- 编辑器(含自定义组件)-------------------------------------
    def _reset_editor(self, editor: TextArea) -> None:
        """清空编辑器。

        内置 `Editor.reset()` 会**先退出历史浏览**再清空;自定义组件只有个 `TextArea` 时
        没这个语义,退回 `load_text("")` —— 这就是“自定义编辑器组件只需要是个 TextArea”
        的边界:qi 的调用点不会要求超出 TextArea 的接口。
        """
        reset = getattr(editor, "reset", None)
        if callable(reset):
            reset()
            return
        editor.load_text("")

    def set_editor_component_impl(self, factory: Any) -> None:
        """`ctx.ui.set_editor_component`:把输入框换成扩展给的组件(`None` = 恢复内置)。

        要求组件是 **`TextArea`(或它的子类)**。这不是偷懒:qi 的编辑器是承重的 ——
        历史环 / kill-ring / 补全面板 / 光标定位都挂在它身上,而它们全部建立在 TextArea
        的接口之上。把边界画在“必须是个 TextArea”上,15 处调用点因此不需要各自做兼容。
        """
        if factory is None:
            if self._editor_component_factory is None:
                return
            self._editor_component_factory = None
            self._replace_editor(Editor(id="editor"))
            return
        widget = self._build_extension_component(factory, "editor")
        if widget is None:
            return
        if not isinstance(widget, TextArea):
            self._note(
                "set_editor_component 需要返回一个 TextArea(或它的子类);"
                f"收到 {type(widget).__name__} —— 已忽略", "warning")
            return
        self._editor_component_factory = factory
        widget.id = "editor"
        self._replace_editor(widget)

    def _replace_editor(self, widget: TextArea) -> None:
        """把 `#editor` 换成新组件。

        两条 Textual 的事实决定了这里的形状(都是实测出来的):
        * widget 的 `id` **一旦设过就不能改**,所以不能给旧的改名腾位;
        * `remove()` 的**实际生效是延迟的**(要到消息周期里才从 `_nodes` 摘掉),而在它
          生效之前挂同名 id 会撞 `DuplicateIds`。先试过 `call_after_refresh`,**不够** ——
          它等的是“下一帧”,而摘除消息可能还在队列后面,于是偶发地把编辑器弄丢。

        所以用 `remove()` 的**可 await 形式**:等它真的摘完再挂。`run_worker` 只是为了让
        这个 await 能从一个同步入口发起(TUI 的 `/reload` 之类也是同一套办法)。
        挂载点直接用 `#body`(编辑器在它下面、`#border-bottom` 前面)。
        """
        try:
            old = self.query_one("#editor")
        except NoMatches:
            return

        async def _swap() -> None:
            try:
                await old.remove()
            except Exception as exc:  # noqa: BLE001 移除失败就没法换 —— 要看得见
                self._note(f"替换编辑器失败(移除旧组件): {exc}", "warning")
                return
            try:
                self.query_one("#body").mount(
                    widget, before=self.query_one("#border-bottom"))
            except Exception as exc:  # noqa: BLE001 挂不上必须说出来 —— 否则编辑器就没了
                self._note(f"替换编辑器失败(挂载新组件): {type(exc).__name__}: {exc}",
                           "warning")
                return
            self._sync_log_height()
            try:
                widget.focus()
            except Exception:  # noqa: BLE001 不支持 focus 的组件也能用
                return

        self.run_worker(_swap(), exclusive=False, exit_on_error=False)

    def get_editor_component_impl(self) -> Any:
        return self._editor_component_factory

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
        """transcript 最多占 终端高 - (编辑器实际行数 + 上下边框 + footer),超出内部滚动。

        只管 regular(inline):fullscreen 下高度由 `#log { height: 1fr }` 分配,
        这里再塞 inline style 反而会盖掉它。
        """
        if self._fullscreen:
            return
        try:
            editor = self.query_one("#editor", TextArea)
            log = self.query_one("#log")
        except NoMatches:  # pragma: no cover - 挂载前/卸载后的调用
            return
        reserved = self._editor_rows(editor) + 2 + self._footer_rows()
        if self._completions_open:
            reserved += min(len(self._completions), self._completion_rows)
        log.styles.max_height = max(3, self.size.height - reserved)

    def _editor_slot_offset(self) -> int:
        """编辑器那一格底边到屏幕底的距离(footer 行数)—— 选择器贴底时要让开。

        pi 的选择器占的是编辑器容器那一格,footer 仍在原位;Textual 这边是 ModalScreen
        覆盖层,所以用 `margin-bottom` 把面板底边对齐到同一条线上。
        """
        return self._footer_rows()

    def _footer_rows(self) -> int:
        """footer 当前真占几行（第三行只在有内容时出现；上限 `FOOTER_LINES` 兜底）。"""
        return max(2, min(FOOTER_LINES, len(self.footer_text.plain.splitlines()) or 2))

    def _editor_rows(self, editor: TextArea) -> int:
        """编辑器会占几行(含软换行估算)。

        transcript 的上限必须在布局前算出来,不能反查 `editor.size.height`(循环依赖),
        所以按终端宽估算包裹行数;估多了只是 transcript 少一行,估少了会把输入框挤掉。
        同时受终端高限制(矮终端下不能让编辑器把屏幕吃光)。
        """
        width = max(8, self.size.width - 2)
        rows = 0
        for line in editor.text.split("\n"):
            rows += max(1, -(-len(line) // width))
        # 终端高 - (上下边框 2 + footer + transcript 至少 3) 才是编辑器的安全上限
        ceiling = max(1, min(MAX_EDITOR_ROWS, self.size.height - 5 - self._footer_rows()))
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
        frames = self._working_frames
        if not self._working or not self._working_visible or not frames:
            return Text("─" * width, style=border)
        frame = frames[self._frame % len(frames)]
        text = self._working_message or "Working"
        label = f" {frame} {text} "
        head = "── "
        rest = "─" * max(0, width - len(head) - len(label))
        line = Text(head, style=Style(color=p.hex("border")))
        line.append(frame, style=Style(color=p.hex("accent")))
        line.append(f" {text} ", style=Style(color=p.hex("muted")))
        line.append(rest, style=Style(color=p.hex("border")))
        return line

    def _editor_color_key(self) -> str:
        """边框色:`!` = bashMode(绿),`!!` = dim,否则 border。"""
        try:
            text = self.query_one("#editor", TextArea).text.lstrip()
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
            self._frame_timer = self.set_interval(self._working_interval, self._tick)
        elif not working and self._frame_timer is not None:
            self._frame_timer.stop()
            self._frame_timer = None
        self._repaint_borders()

    def _tick(self) -> None:
        frames = self._working_frames
        self._frame = (self._frame + 1) % max(1, len(frames))
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

        # 状态行是**有条件**的(pi 同款:没内容就不占行)。先洗掉换行/制表/连续空格
        # (pi 的 `sanitizeStatusText`)—— 否则扩展写的多行状态会把 footer 撑成 4 行,
        # 预留行数失真 → inline 区域把输入框挤掉。
        status = " ".join(self._status.split())
        third = Text(status, style=Style(color=p.hex("muted")))
        first.truncate(width, overflow="ellipsis")
        lines = [first, second]
        if third.plain:
            third.truncate(width, overflow="ellipsis")
            lines.append(third)
        self.footer_text = Text("\n").join(lines)
        try:
            footer = self.query_one("#footer", Static)
        except NoMatches:      # App 正在卸载
            return
        footer.update(self.footer_text)

    # -- 输入 -----------------------------------------------------------
    def on_editor_submitted(self, event: Editor.Submitted) -> None:
        text = event.value.strip()
        editor = self.query_one("#editor", TextArea)
        self._reset_editor(editor)
        self._sync_log_height()
        if not text:
            return
        # pi 只把对话与 bash 记进输入历史,内置命令不入 —— 否则 ↑ 全被 /tree 之类古满
        if not text.startswith("/"):
            # 自定义编辑器组件可以没有 qi 的历史环(只要求是个 TextArea)
            add_history = getattr(editor, "add_to_history", None)
            if callable(add_history):
                add_history(text)
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
        editor = self.query_one("#editor", TextArea)
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
        editor = self.query_one("#editor", TextArea)
        text = editor.text.strip()
        if not text:
            return
        self._reset_editor(editor)
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
        """编辑器变高/变矮、进入/退出 bash 模式时,同步布局与边框色。

        `#editor` 可能**暂时不存在**:`set_editor_component` 换组件时旧编辑器的
        `Changed` 消息可能还在路上(实测撞过)。所以这里宽一点,不假设它一定在。
        """
        if event.text_area.id != "editor":
            return
        try:
            editor = self.query_one("#editor", TextArea)
        except (NoMatches, ScreenStackError):
            return
        self._repaint_borders()
        self._async_completion = None      # 文本变了:async 补全的回填作废
        if getattr(editor, "history_browsing", False):
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
        self._append(UserMessage(self._transform_markdown(text), self._renderer, self._palette))
        if self._session is None:
            # 懒建:到这里才真有话要说,现在建会话(普通启动=落文件,`--no-session`=内存)。
            self._session = self._lazy_session()
        # 会话是这一刻才建出来的(或早就绑过)→ 通知 runtime 绑定它,否则这一轮里的 `/model`
        # 找不到"该往哪个文件记"(设置类 entry 见 docs/session-format.md §6.1)。
        # 幂等,随手调一次没关系。
        bind = getattr(rt, "bind_session", None)
        if callable(bind):
            bind(self._session)
        self.run_worker(self._run(text), exclusive=False, exit_on_error=False)

    # -- 事件循环 -------------------------------------------------------
    async def _run(self, text: str) -> None:
        if self._rt is None or self._session is None:      # 防御:worker 可能在切换会话后跑
            return
        runtime, session = self._rt, self._session
        self._live = None
        renderer = self._renderer
        # 中断信号按**回合**建:工具层靠它杀进程,收尾后清掉(不会串到下个回合)
        abort = AbortSignal()
        self._abort = abort
        self._set_working(True)
        interrupted = False
        try:
            async for ev in runtime.stream(text, session, abort=abort):
                if ev.kind == "compaction_start":
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
                        self._live = AssistantMessage(self._palette, self._output_pad)
                        self._append(self._live)
                    self._live.append_delta(ev.text, renderer)
                    self._scroll_end()
                elif ev.kind == "assistant_message":
                    if self._live is None:
                        self._live = AssistantMessage(self._palette, self._output_pad)
                        self._append(self._live)
                    self._live.set_text(self._transform_markdown(ev.text), renderer)
                    self._live = None
                    self._live_thinking = None      # 收束思考块(保留可 ctrl+t 切换)
                    # 最终回答(不带工具调用的那条)才供 /copy 使用
                    if ev.text.strip() and not ev.data.get("tool_calls"):
                        self._last_answer = ev.text
                elif ev.kind == "tool_start":
                    block = self._tool_block(ev.tool or "?", ev.data.get("args") or {})
                    # 并发执行时会有多条工具同时在途 —— 按 `tool_call_id` 配对
                    self._current_tools[str(ev.data.get("tool_call_id") or "")] = block
                    self._append(block)
                elif ev.kind == "tool_end":
                    call_id = str(ev.data.get("tool_call_id") or "")
                    block = self._current_tools.pop(call_id, None)
                    if block is None and len(self._current_tools) == 1:
                        # 兼容不给 id 的来源:只有一条在途时能确定是哪一条
                        _, block = self._current_tools.popitem()
                    if block is None:
                        block = self._tool_block(ev.tool or "?", {})
                        self._append(block)
                    status = "ok" if ev.data.get("status") == "ok" else "error"
                    block.set_state(status)
                    name = ev.tool or "?"
                    is_error = status != "ok"
                    widget = self._tool_result_widget(name, ev.text or "")
                    if widget is not None and isinstance(block, ExtensionToolBlock):
                        with contextlib.suppress(Exception):
                            block.show(widget)
                    else:
                        block.set_output(renderer.tool_body(name, ev.text or "",
                                                            expanded=self._expanded,
                                                            is_error=is_error))
                    self._tool_blocks.append((block, name, ev.text or ""))
                    if status == "error" and ev.data.get("error"):
                        block.set_output(Text("\n" + str(ev.data["error"]),
                                              style=Style(color=self._palette.hex("error"))))
                    self._scroll_end()
                elif ev.kind == "error":
                    self._append(Static(Text(ev.text, style=Style(color=self._palette.hex("error"))),
                                        classes="msg"))
                elif ev.kind == "agent_end":
                    usage = ev.data.get("usage") or {}
                    self._usage["prompt_tokens"] += _as_int(usage.get("prompt_tokens"))
                    self._usage["completion_tokens"] += _as_int(usage.get("completion_tokens"))
                    self._refresh_footer()
        except Exception as exc:  # noqa: BLE001 回合失败只报错:TUI 不能被一轮带崩
            # 接的是 LLM(凭证/网络/流中断)、分派、落盘等**没被就地处理**的异常。
            # 往上抛 = Textual 按 exit_on_error 直接退出 TUI,用户连再试一次的机会都没有。
            self._note(f"回合失败:{type(exc).__name__}: {exc}", "error")
        finally:
            interrupted = abort.aborted
            if self._abort is abort:
                self._abort = None
            self._set_working(False)
            self._live = None
            self._live_thinking = None
            self._report_reasoning_dropped()
            self._scroll_end()
            if interrupted:
                self._flash("已中断")
            # 回合结束再抽队列(排队消息不并发跑,避免两个回合互踩同一会话)
            self.call_after_refresh(self._drain_queue)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """worker 兜底:没被就地处理的异常只提示,不让 Textual 退出 TUI。

        四个 run_worker 都传了 `exit_on_error=False` 且各自 try/except;这里是防
        “以后新增的 worker 忘了兜”——宁可多一行提示,也不要整屏消失。
        """
        if event.state != WorkerState.ERROR:
            return
        error = getattr(event.worker, "error", None)
        if error is not None:
            self._note(f"后台任务失败:{type(error).__name__}: {error}", "error")
            self._scroll_end()

    # -- 命令 -----------------------------------------------------------
    def _note(self, text: str, tone: str = "dim") -> None:
        """命令行输出:统一消息块(无底色,padding 0,1)。

        `tone` 经 `NOTE_TONES` 归一(pi 的 `info` 当普通正文),**不认识的一律当 `dim`** ——
        这里只是提示的配色,不该因为一个词把命令 / worker 打断。
        """
        key = NOTE_TONES.get(tone, "dim")
        self._append(Static(Text(text, style=Style(color=self._palette.hex(key))), classes="msg"))

    def _command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        raw = parts[0]                      # 保留原大小写:技能名是大小写敏感的
        cmd = raw.lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        # /quit 与 /hotkeys /help 不依赖 runtime;其余需要一个可用的运行期
        if self._rt is None and cmd not in ("/quit", "/hotkeys"):
            self._note("运行时不可用。", "error")
            self._scroll_end()
            return
        rt = self._rt
        store = self._session_store()

        # `/skill:<名> [参数]`:强制加载并执行一个技能(pi 的 skills register as `/skill:name`)。
        # 放在扩展命令**之前**:技能名不会被扩展命令抢占(前缀不会撞)。
        if raw.startswith("/skill:"):
            self._run_skill_command(raw[len("/skill:"):], arg)
            return

        # 扩展命令**优先于内置**(pi 的顺序:扩展先认领,认领了就跳过内置分发),但
        # 保留 `RESERVED_COMMANDS` —— 顶掉 `/quit` 等于把用户锁在界面里,
        # 那不是扩展该有的权力(而且出问题时用户已经没法用这个界面改回来了)。
        if rt is not None and cmd not in RESERVED_COMMANDS:
            ext = rt.commands.find(cmd)
            if ext is not None:
                self.run_worker(self._run_extension_command(ext, arg), exclusive=False,
                                exit_on_error=False)
                self._scroll_end()
                return

        if cmd == "/hotkeys":
            self._note(HOTKEYS_TEXT, "text")
        elif cmd == "/quit":
            self.exit()
        # ── 会话 ──────────────────────────────────────────────
        elif cmd == "/new":
            if rt is None:
                return
            # 新会话**不带标题**:标题由自动命名在首次提问的回合末尾补上
            # (写死一个默认名会让 `has_title()` 认为"已经有名字了",自动命名就永远不跑)
            # 且**不建文件**:与裸 `qi` 一样走预留 —— 连点两次 `/new` 不该堆两个空会话
            # (web 端 §18.28 就是为这个改成懒创建的,pi 的 `/new` 同样不落文件)。
            self._run_guarded(self._switch_guarded(
                self._lazy_session(), reason="new", note="已开新会话(auto)"))
        elif cmd == "/resume":
            if arg:
                s = store.get(arg)
                if s is None:
                    self._note(f"会话不存在 {arg}", "error")
                else:
                    self._run_guarded(self._switch_guarded(
                        s, reason="resume",
                        note=f"已恢复 {s.id}(分支 {s.message_count} 条消息)"))
            else:
                self._show_session_selector()          # 模态选择器(对齐 pi)
                self._scroll_end()
                return
        elif cmd == "/settings":
            self._open_settings_panel()
        elif cmd == "/share":
            self._share_session()
        elif cmd == "/trust":
            self._set_trust(arg)
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
                        self._run_guarded(self._fork_guarded(session, target))
                else:
                    self.push_screen(
                        PickerScreen("从哪条用户消息 fork(选中后重新提问)", options),
                        lambda value: self._run_guarded(self._fork_guarded(session, value))
                        if value else None)
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
                self.run_worker(self._compact_worker(arg), exclusive=False,
                                exit_on_error=False)
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
                # pi 的口径:无参**也是开选择器**(`showModelSelector`),不是打印一张表
                self.action_select_model()
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
        elif cmd == "/scoped-models":
            self.action_scoped_models()
        elif cmd == "/thinking":
            if not arg:
                # pi 的 `showThinkingSelector`:`✓ ` 标当前、带说明、`default` 那档缀 ` · default`
                self.push_screen(
                    ThinkingSelector(self._thinking_level, self._default_thinking_level()),
                    lambda level: self._set_thinking_level(str(level)) if level else None)
                self._scroll_end()
                return
            if arg.lower() not in THINKING_LEVELS:
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
                # 交给 runtime:标题落盘(内存 + header + 文件)+ `session_info_changed`
                # 事件都只此一处 —— TUI 不再自己走那三步。
                if rt is not None and hasattr(rt, "set_session_title"):
                    rt.set_session_title(session, arg, source="user")
                else:
                    session.title = arg
                self._refresh_footer()
                self._note(f"会话名已设为 {arg}")
        elif cmd == "/export":
            session = self._session
            if session is None:
                self._note("当前没有会话", "warning")
                self._scroll_end()
                return
            if not session.entries or (session.unflushed and not session.path.exists()):
                # 还没落盘(刚起会话、一句都还没聊,或 `--no-session`)→ 说清楚,
                # 而不是让 `copy` 抛一个 `[Errno 2] No such file`(用户看不到所以然)
                self._note("这个会话还没落盘,没有可导出的文件(先聊一轮)", "warning")
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
            # 对话框里采集 key(不进 transcript):worker 里 await 模态,与 `/compact` 同一形状
            self.run_worker(self._login_flow(arg), exclusive=False, exit_on_error=False)
            self._scroll_end()
            return
        elif cmd == "/logout":
            self.run_worker(self._logout_flow(arg), exclusive=False, exit_on_error=False)
            self._scroll_end()
            return
        elif cmd == "/changelog":
            self._note(self._changelog())

        elif cmd in PLANNED_COMMANDS:
            self._note(f"{cmd} 计划中(需先给后端加能力)", "warning")
        else:
            # pi 没有 `/help`:命令靠 `/` 补全自己被发现(补全项带描述)
            self._note(f"未知命令 {cmd};输入 / 看全部命令", "warning")
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
        # 与 `_switch_session` 同一条约定:换了当前会话就要通知 runtime(设置类 entry 写哪)
        bind = getattr(self._rt, "bind_session", None) if self._rt is not None else None
        if callable(bind):
            bind(session)
        self._sync_model_from_runtime()
        self._refresh_footer()
        self._note(f"已导入并切换到 {sid}(消息 {session.message_count} 条)")

    async def _run_extension_command(self, command, arg: str) -> None:
        """跑一条扩展命令。异常只提示,不把 TUI 打崩(扩展是第三方代码)。"""
        runtime = self._rt
        if runtime is None:
            return
        try:
            await command.handler(arg, runtime.extension_ctx())
        except Exception as exc:  # noqa: BLE001 第三方代码
            self._note(f"/{command.invocable} 执行失败: {type(exc).__name__}: {exc}",
                       "error")

    def _bind_extension_shortcuts(self) -> None:
        """把扩展注册的快捷键接到 textual 的动态 `bind()` 上。

        单个键绑失败(名字写法不对)只提示,不让其余快捷键陪绑 —— 而且**不让启动失败**:
        一个手滑的扩展不该把整个界面拦住。
        """
        if self._rt is None:
            return
        for shortcut in self._rt.commands.shortcuts():
            try:
                self.bind(shortcut.key, f"ext_shortcut('{shortcut.key}')",
                          description=shortcut.description or f"扩展快捷键 {shortcut.key}")
            except Exception as exc:  # noqa: BLE001 坏键名/保留键
                self._note(f"快捷键 {shortcut.key} 注册失败: {exc}", "warning")

    def action_ext_shortcut(self, key: str) -> None:
        """所有扩展快捷键共用的入口(textual 的 action 只能带参数,不能动态加方法)。"""
        runtime = self._rt
        if runtime is None:
            return
        shortcut = next((s for s in runtime.commands.shortcuts() if s.key == key), None)
        if shortcut is None:
            return

        async def _run() -> None:
            try:
                # `runtime` 在闭包外已经收窄好(不用 assert:`-O` 会把 assert 剥掉,
                # 那正好是“生产环境少一层保护”的写法)。
                await shortcut.handler(runtime.extension_ctx())
            except Exception as exc:  # noqa: BLE001 第三方代码
                self._note(f"快捷键 {key} 执行失败: {type(exc).__name__}: {exc}", "error")

        self.run_worker(_run(), exclusive=False, exit_on_error=False)

    def _reload_runtime(self) -> None:
        """重载 agents / extensions / 配置(会话不变)。"""
        try:
            runtime = QiRuntime(has_ui=True, approve_project=self._approve_project,
                                ui_frontend=_TuiUi(self),
                                extension_flags=self._extension_flags,
                                extra_extension_paths=self._extra_extension_paths,
                                **self._runtime_extra)
        except (LoadError, ConfigError) as exc:
            self._note(f"重载失败: {exc}", "error")
            return
        self._rt = runtime
        self._renderer = TuiRenderer(self._palette, runtime.cwd)
        self._bind_extension_shortcuts()
        for note in runtime.notes:
            self._note(note, "warning")
        if self._session is not None:
            # 新 runtime 的“已发过”记账是空的 → 这里会再发一次(扩展据此重开资源)
            self.run_worker(runtime.start_session(self._session, reason="reload"),
                            exclusive=False, exit_on_error=False)
        self._branch = git_branch(str(runtime.cwd))
        # 重载换了 runtime 对象 → 重新绑定会话并同步模型/级别(否则 footer 会退回默认值,
        # 而请求用的是会话里记的那个)
        bind = getattr(runtime, "bind_session", None)
        if self._session is not None and callable(bind):
            bind(self._session)
        self._sync_model_from_runtime()
        if self._model is None:
            try:
                self._model = resolve_default_model(runtime.cfg, runtime.cwd)
            except ConfigError:
                self._model = None
        self._refresh_footer()
        self._note(f"已重载:{len(runtime.extensions)} 个扩展。"
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
        self._set_thinking_level(THINKING_LEVELS[(index + 1) % len(THINKING_LEVELS)],
                                 source="cycle")

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

    def _default_thinking_level(self) -> str:
        """生效的默认档(`settings.defaultThinkingLevel` → `DEFAULT_THINKING_LEVEL`)。

        `/thinking` 的选择器用它标 ` · default`(pi 的 `ThinkingSelectorComponent` 同形)。
        """
        settings = getattr(self._rt, "settings", None)
        raw = str(getattr(settings, "defaultThinkingLevel", None) or "").strip().lower()
        return raw if raw in THINKING_LEVELS else DEFAULT_THINKING_LEVEL

    def _set_thinking_level(self, level: str, source: str = "set") -> None:
        """设置级别:交给 runtime(它才是唯一入口,`thinking_level_select` 从那里发)。"""
        rt = self._rt
        if rt is not None and hasattr(rt, "set_thinking_level"):
            self._thinking_level = rt.set_thinking_level(level, source=source)
        else:
            self._thinking_level = normalize_thinking_level(level)
        self._refresh_footer()
        notice = ""
        if self._model is not None and not self._model.reasoning:
            notice = "(当前模型未声明 reasoning,不会随请求发送)"
        self._flash(f"思考级别: {self._thinking_level}{notice}")

    def action_interrupt(self) -> None:
        """escape:中断当前回合。

        第一次按是**协作式**(对齐 pi 的 app.interrupt):置位信号,runner 走到正常收尾
        —— 未执行的工具补上“已中断”结果、半截回答照常落盘。宽限期内没收尾(工具不理信号)
        则强制终止;再按一次也是立即强制。
        """
        if not self._working:
            return
        signal = self._abort
        if signal is not None and not signal.aborted:
            signal.abort()
            if self._queue_count():
                self._restore_queue()
            self._flash("正在中断…(再按 escape 强制终止)")
            grace = INTERRUPT_GRACE_S
            self.set_timer(grace, functools.partial(self._force_cancel, signal))
            return
        self._force_cancel(signal)

    def _force_cancel(self, signal: AbortSignal | None) -> None:
        """兜底硬取消:工具不理会中断信号时用(宽限期到点,或用户再按一次 escape)。"""
        if self._abort is not signal or not self._working:
            return                     # 已经收尾,或已经是另一个回合
        self.workers.cancel_all()
        self._set_working(False)
        self._flash("已强制终止")

    def action_clear_or_exit(self) -> None:
        """ctrl+c:清空编辑器;连按两次退出 —— 对齐 pi 的 app.clear/app.exit。"""
        editor = self.query_one("#editor", TextArea)
        if editor.text:
            self._reset_editor(editor)
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
        editor = self.query_one("#editor", TextArea)
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
        editor = self.query_one("#editor", TextArea)
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
        """可选模型(`/model` / ctrl+l / ctrl+p / `/scoped-models` 共用)。

        清单本身来自 **core** 的 `selectable_models()`(web 端的模型菜单用同一份 ——
        两处各写一份口径,迟早一个改了另一个没改):**预置兜底那批只有"能解析出凭证"
        时才列出来**,而 `models.json` 里**显式写过**的 provider 不受这条限制。

        这里只做一件 TUI 自己的事:把"当前是哪个"标出来(`is_current`)。
        """
        rt = self._rt
        if rt is None:
            return []
        current = self._model
        cfg = getattr(rt, "cfg", None)
        if cfg is None:
            return []
        return [(provider, model,
                 bool(current and current.provider == provider and current.model == model))
                for provider, model in selectable_models(cfg, store=self._auth_store())]

    def _auth_store(self) -> Any:
        """凭证库:优先用 runtime 身上那个(它带着 `--api-key` 的覆盖);拿不到就用真的。"""
        rt = self._rt
        store = getattr(rt, "_auth", None) if rt is not None else None
        if store is None:
            from .auth import AuthStore

            store = AuthStore()
        return store

    def _switch_model(self, provider: str, model: str, source: str = "set") -> None:
        """运行期换模型:**交给 runtime**(它才是唯一入口,`model_select` 从那里发),
        这里只做界面反馈。"""
        rt = self._rt
        if rt is None:
            return
        try:
            resolved = rt.set_model(provider, model, source=source)
        except Exception as exc:  # 配置/凭证异常不该把 TUI 弄崩
            self._note(f"切换模型失败: {exc}", "error")
            self._scroll_end()
            return
        self._model = resolved
        self._refresh_footer()
        self._flash(f"模型: {resolved.label}")

    def action_select_model(self) -> None:
        """ctrl+l(以及 `/model` 无参):模型选择器(对齐 pi 的 `showModelSelector`)。"""
        options = self._model_options()
        if not options:
            self._flash("models.json 里没有可选模型")
            return

        def picked(value: str | None) -> None:
            if value:
                provider, _, model = value.partition("\x00")
                self._switch_model(provider, model)

        self.push_screen(ModelSelector(options, default=self._default_model_ref()), picked)

    def _default_model_ref(self) -> tuple[str, str] | None:
        """`settings` 里配的默认模型(选择器用它缀 ` · default`,pi 同形)。"""
        settings = getattr(self._rt, "settings", None)
        provider = str(getattr(settings, "defaultProvider", None) or "").strip()
        model = str(getattr(settings, "defaultModel", None) or "").strip()
        return (provider, model) if provider and model else None

    def action_cycle_model(self) -> None:
        self._cycle_model(1)

    def action_cycle_model_back(self) -> None:
        self._cycle_model(-1)

    def _enabled_models(self) -> list[str]:
        """Ctrl+P 轮换的清单:`settings.enabledModels`(空 = 不限,即全部)。

        CLI 的 `--models` 给过就用它 —— 那是**本次运行**的清单,不该被 settings 盖掉
        (也不写回 settings:命令行给的东西不静默落盘)。
        """
        override = getattr(self._rt, "scoped_models", None)
        if override is not None:
            return [str(value) for value in override]
        raw = getattr(getattr(self._rt, "settings", None), "enabledModels", None) or []
        return [str(value) for value in raw]

    def _cycle_options(self) -> list[tuple[str, str, bool]]:
        """Ctrl+P 轮换用的清单:`enabledModels` 非空时只在这些模型里转(pi 的 scopedModels)。"""
        options = self._model_options()
        enabled = set(self._enabled_models())
        if not enabled:
            return options
        scoped = [item for item in options if f"{item[0]}/{item[1]}" in enabled]
        return scoped or options          # 配置过时的空集:回退到全部,别把轮换卡死

    def _cycle_labels(self) -> list[str]:
        """Ctrl+P 会轮换到的模型标签(`provider/model`)。"""
        return [f"{provider}/{model}" for provider, model, _ in self._cycle_options()]

    def action_scoped_models(self) -> None:
        """/scoped-models:挑 Ctrl+P 轮换哪些模型(对齐 pi 的 `/scoped-models`)。"""
        options = [(f"{provider}/{model}", provider)
                   for provider, model, _ in self._model_options()]
        if not options:
            self._flash("models.json 里没有可选模型")
            return

        def picked(values: list[str] | None) -> None:
            if values is not None:
                self._save_scoped_models(values)

        self.push_screen(ScopedModelsSelector(options, self._enabled_models()), picked)

    def _save_scoped_models(self, values: list[str]) -> None:
        """写回 `settings.enabledModels`(全局设置,对齐 pi 的 app.models.save)。"""
        if self._rt is None:
            return
        try:
            set_value("user", "enabledModels", values, self._rt.cwd)
        except (OSError, SettingsError) as exc:
            self._note(f"保存失败: {exc}", "error")
            self._scroll_end()
            return
        self._rt.settings.enabledModels = values
        self._flash("Ctrl+P 轮换全部模型" if not values
                    else f"Ctrl+P 在 {len(values)} 个模型里轮换")

    def _cycle_model(self, step: int) -> None:
        options = self._cycle_options()
        if not options:
            self._flash("models.json 里没有可选模型")
            return
        flat = [(provider, model) for provider, model, _ in options]
        current = (self._model.provider, self._model.model) if self._model else None
        index = flat.index(current) if current in flat else -1
        provider, model = flat[(index + step) % len(flat)]
        self._switch_model(provider, model, source="cycle")

    # -- 会话树(pi 的 /tree /fork /clone)--------------------
    def _session_store(self) -> SessionStore:
        """用 runtime 的 store(`sessionDir` 设置才会生效),而不是自己 new 一个。"""
        if self._rt is not None:
            return self._rt.sessions
        return SessionStore()

    # -- 会话选择器(/resume;对齐 pi 的会话选择器键位)----------
    def _show_session_selector(self) -> None:
        store = self._session_store()
        if not store.list():
            self._flash("没有历史会话")
            return

        def picked(session_id: str | None) -> None:
            if not session_id:
                return
            session = store.get(session_id)
            if session is None:
                self._flash("会话不存在")
                return
            # 与 `/resume <id>` 走**同一道闸门**(pi 的 `session_before_switch`):
            # 选择器这条路以前绕过事件,于是“扩展能拦命令行、拦不住选择器”。
            self._run_guarded(self._switch_guarded(
                session, reason="resume", note=f"已恢复 {session.id}"))

        self.push_screen(
            SessionSelector(store.list, on_rename=self._rename_session,
                            on_delete=self._delete_session,
                            current=self._session.id if self._session else None,
                            current_cwd=(str(self._rt.cwd) if self._rt is not None
                                         else str(Path.cwd()))),
            picked)

    def _rename_session(self, session_id: str, name: str) -> None:
        """改名走 runtime 的 `set_session_title`(内存 + header + 落盘 + 事件一处做完)。"""
        store = self._session_store()
        session = store.get(session_id)
        if session is None:
            return
        setter = getattr(self._rt, "set_session_title", None) if self._rt is not None else None
        try:
            if callable(setter):
                setter(session, name, source="user")
            else:
                store.set_title(session, name)
        except OSError as exc:
            self._note(f"改名失败: {exc}", "error")
            return
        if self._session is not None and self._session.id == session_id:
            self._session.title = name
            self._refresh_footer()

    def _delete_session(self, session_id: str) -> None:
        if not self._session_store().delete(session_id):
            self._note(f"删除失败: {session_id}", "error")
            return
        if self._session is not None and self._session.id == session_id:
            self._session = None          # 删的是当前会话:下一条消息会自动新建
            # 解绑:否则"下一条消息"新建会话前的 `/model` 会把设置写进一个已删掉的文件
            bind = getattr(self._rt, "bind_session", None) if self._rt is not None else None
            if callable(bind):
                bind(None)

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
        if kind == "model_change":
            return f"模型: {entry.get('provider')}/{entry.get('model_id')}"
        if kind == "thinking_level_change":
            return f"思考级别: {entry.get('thinking_level')}"
        if kind == "state":
            return f"状态: {entry.get('key')} = {entry.get('value')}"
        return str(kind)

    def _tree_rows(self, session, mode: str, query: str,
                   show_label_time: bool) -> list[TreeRow]:
        """整棵树 → 行(按 `mode` 过滤、按 `query` 搜索);`●` 当前节点,`│` 当前分支。

        父节点被过滤掉时子节点仍然继续遍历 —— 缩进保留,与 pi 一致。
        """
        on_branch = {str(e.get("id")) for e in session.branch()}
        node = session.current
        tokens = [token for token in query.lower().split() if token]
        out: list[TreeRow] = []

        def walk(parent: str | None, depth: int) -> None:
            for child in session.children(parent):
                child_id = str(child.get("id"))
                label = str(child.get("label") or "").strip()
                mark = "●" if child_id == node else ("│" if child_id in on_branch else "·")
                text = "  " * depth + f"{mark} "
                if label:
                    text += f"[{label}] "
                    if show_label_time and child.get("labelTimestamp"):
                        text += f"({child['labelTimestamp']}) "
                text += self._entry_label(child)
                if entry_passes_tree_filter(child, mode) \
                        and all(token in text.lower() for token in tokens):
                    out.append(TreeRow(child_id, text, label))
                walk(child_id, depth + 1)

        walk(None, 0)
        return out

    def _set_entry_label(self, entry_id: str, label: str) -> None:
        """给 entry 打/清标签(直接落在会话文件里,对齐 pi 的标签编辑)。"""
        session = self._session
        if session is None:
            return
        entry = next((e for e in session.entries if str(e.get("id")) == entry_id), None)
        if entry is None:
            return
        if label:
            entry["label"] = label
            entry["labelTimestamp"] = datetime.now().isoformat(timespec="seconds")
        else:
            entry.pop("label", None)
            entry.pop("labelTimestamp", None)
        self._session_store().save(session)
        self._flash(f"标签: {label}" if label else f"已清除标签 {entry_id}")

    def _run_skill_command(self, name: str, args: str) -> None:
        """`/skill:<名> [参数]`:读全文 SKILL.md 并**执行**(pi 的 "Load and execute the skill")。"""
        rt = self._rt
        if rt is None:
            self._note("运行时不可用。", "error")
            return
        known = {label[len("/skill:"):]: label
                 for label, _detail in skill_command_candidates(rt)}
        if not known:
            self._note("未知命令(技能命令被 settings.enableSkillCommands 关了)。", "warning")
            return
        match = next((s for s in getattr(rt, "top_skills", None) or []
                      if s.name.lower() == name.lower()), None)
        if match is None:
            self._note(f"没有技能 {name};输入 `/skill:` 看补全", "warning")
            return
        try:
            payload = skill_invocation(match, args)
        except OSError as exc:
            self._note(f"技能读不了:{exc}", "error")
            return
        self._submit(payload)
        self._note(f"已加载技能 {match.name}({match.path})", "info")

    def _share_session(self) -> None:
        """`/share`:把当前会话传成**私有** GitHub gist,并把链接复制到剪贴板。

        网络调用**走线程**(`asyncio.to_thread`),否则几百毫秒的 HTTP 会冻住界面 ——
        pi 是全程异步的,这边用 worker + to_thread 达到同样的"不卡住"。
        """
        rt = self._rt
        if rt is None or self._session is None:
            self._note("当前没有会话可分享。", "warning")
            return
        self._note("正在上传成私有 gist…", "info")
        self.run_worker(self._do_share(self._session), exclusive=False, exit_on_error=False)

    async def _do_share(self, session: Any) -> None:
        import asyncio

        from .share import ShareError, resolve_token, share_session

        if resolve_token() is None:
            self._note("没有 GitHub token:设 `GITHUB_TOKEN`(需 gist 权限),"
                       "或 `qi auth login github`", "error")
            return
        try:
            url = await asyncio.to_thread(share_session, session)
        except ShareError as exc:
            self._note(str(exc), "error")
            return
        except Exception as exc:      # noqa: BLE001 兜底:别让一次分享把界面打崩
            self._note(f"分享失败:{type(exc).__name__}: {exc}", "error")
            return
        try:
            self.copy_to_clipboard(url)          # 顺手复制;失败不影响已成功的分享
            copied = "(已复制到剪贴板)"
        except Exception:      # noqa: BLE001 无剪贴板(远程终端等)
            copied = ""
        self._note(f"已分享(私有 gist):{url} {copied}", "info")

    def _open_settings_panel(self) -> None:
        """`/settings`:偏好面板。保存后写**用户级** settings,并把新值应用到当前界面。"""
        from .settings import setting_choices, set_value

        rt = self._rt
        if rt is None:
            self._note("运行时不可用。", "error")
            return
        try:
            from .tui import run_settings_panel   # 同模块:留着是为了与其它面板同一写法
        except Exception as exc:      # noqa: BLE001 textual 依赖问题 → 报清楚,不留死命令
            self._note(f"面板不可用:{exc}", "error")
            return
        changed = run_settings_panel(setting_choices(rt.settings))
        if not changed:
            self._note("没有改动。", "info")
            return
        for key, value in changed.items():
            try:
                set_value("user", key, parse_value(value), rt.cwd)
            except SettingsError as exc:
                self._note(f"{key} 没写进去:{exc}", "error")
                continue
            setattr(rt.settings, key, parse_value(value))
            self._note(f"已保存 {key} = {value}(用户级 settings)", "info")
        self._apply_ui_settings()

    # -- 凭证(`/login` `/logout`)------------------------------------------
    def _config_providers(self) -> list[str]:
        """可登录的 provider:models.json 里的 + **预置兜底**那批(config.load_config 补的)。"""
        cfg = getattr(self._rt, "cfg", None)
        return sorted(getattr(cfg, "providers", None) or {})

    def _preset_providers(self) -> set[str]:
        """哪些 provider 来自预置表(models.json 里没写)—— 只用来做标记。"""
        cfg = getattr(self._rt, "cfg", None)
        return set(getattr(cfg, "presetProviders", None) or ())

    async def _login_flow(self, arg: str) -> None:
        """`/login [provider]`:选 provider → 输入 API key → 写 `auth.json`(对齐 pi)。

        三处与 pi 同口径:
          · 不给 provider 先弹选择器,给了就直奔输入(pi `handleLoginCommand`);
          · 登录后重建客户端 —— 不然新 key 不生效(`LiteLLMClient` 构造时就解析好了);
          · key 只进 auth store,**不进 transcript**(pi 那个对话框也是这个目的)。

        与 pi 的差异(已落档,见 docs/tui.md):pi 的 `/login` 主要在做**订阅登录**
        (provider 声明 `auth.oauth`,跑 device-code / PKCE),qi 的凭证层只有 api_key;
        另外 key 输入在 qi 里是**遮罩**的(pi 的对话框明文回显)。
        """
        from .auth import AuthStore

        store = AuthStore()
        provider = (arg or "").strip().lower()
        providers = self._config_providers()
        if not provider:
            if not providers:
                self._note("models.json 里还没有 provider;先 `qi init --preset deepseek`"
                           "(看全部:`qi init --list-presets`),或手写 models.json。", "warning")
                return
            saved = set(store.providers())
            presets = self._preset_providers()

            def label(name: str) -> str:
                # `(预置)` = 来自 qi 的预置表(models.json 里没写);登录后就能直接用它
                mark = "已存凭证" if name in saved else "无凭证"
                return f"{name}    ({mark}{' · 预置' if name in presets else ''})"

            options = [(name, label(name)) for name in providers]
            provider = str(await self.await_screen(PickerScreen(
                "登录哪个 provider(↑↓ 选择 · enter 确认 · escape 取消)", options)) or "")
            provider = provider.strip().lower()
            if not provider:
                self._note("已取消登录。", "info")
                return
        known = provider in providers
        title = f"{provider} API key" + ("" if known else "(该 provider 不在 models.json 里)")
        key = str(await self.await_screen(PromptScreen(title, secret=True)) or "").strip()
        if not key:
            self._note("已取消登录(没输入 key)。", "info")
            return
        try:
            store.set_key(provider, key)
        except OSError as exc:                 # 读/写盘失败(权限、只读 home…)
            self._note(f"写凭证失败: {exc}", "error")
            return
        self._note(f"已保存 {provider} 的 API key → {store.path}(0600)", "info")
        if provider in self._preset_providers():
            self._note(f"{provider} 来自**预置**(models.json 里没写,qi 的内置表兜底);"
                       f"要改 baseUrl / 模型就 `qi init --preset {provider}` 物化出来再改。",
                       "dim")
        if not known:
            self._note(f"注意:{provider} 不在 models.json 里 —— 补上 provider 段才能用它。",
                       "warning")
            return
        self._after_credential_change(provider)

    def _after_credential_change(self, provider: str) -> None:
        """登录/登出后:重建客户端(新 key 马上生效);**当前没模型**时顺带选一个。

        “当前没模型”才自动选 —— 不把用户正在用的模型洗掉(pi 也只在 previousModel
        未知时才自动选)。
        """
        rt = self._rt
        if rt is not None and hasattr(rt, "reload_credentials"):
            rt.reload_credentials()             # 失败也只是保持旧客户端(不挡登录本身)
        if self._model is not None:
            self._note(f"{provider} 已就绪;当前模型不变(要换用 /model)。", "info")
            return
        models = [model for name, model, _ in self._model_options() if name == provider]
        if not models:
            self._note(f"{provider} 在 models.json 里没有模型;加一个再用 /model 选。", "warning")
            return
        self._switch_model(provider, models[0])

    async def _logout_flow(self, arg: str) -> None:
        """`/logout [provider]`:删 `auth.json` 里的凭证(无参先给选择器,对齐 pi)。"""
        from .auth import AuthStore

        store = AuthStore()
        provider = (arg or "").strip().lower()
        if not provider:
            saved = store.providers()
            if not saved:
                self._note(f"auth store 为空({store.path});没有可退的登录。", "info")
                return
            provider = str(await self.await_screen(PickerScreen(
                "退出哪个 provider(↑↓ 选择 · enter 确认 · escape 取消)",
                [(name, name) for name in saved])) or "").strip().lower()
            if not provider:
                return
        if store.remove(provider):
            self._note(f"已删除 {provider} 的凭证;环境变量与 models.json 里的 apiKey 不受影响",
                       "info")
            # 下一回合重新解析:删掉 auth.json 那条后可能落到 env / models.json
            rt = self._rt
            if rt is not None and hasattr(rt, "reload_credentials"):
                rt.reload_credentials()
        else:
            self._note(f"{provider} 没有已存凭证(环境变量与 models.json 里的 apiKey 不受影响)",
                       "warning")

    def _set_trust(self, arg: str) -> None:
        """`/trust [yes|no|forget]`:把信任决定写进 `~/.qi/agent/trust.json`(对齐 pi)。

        三条 pi 的行为照抄:

        * **连带记住上一层目录** —— 同一条路径下的平级项目一起生效;
        * **只写用户 home 的信任库**,不碰仓库里任何文件(仓库不能为自己背书);
        * **当前会话不重载** —— 项目级资源已按旧决定加载/跳过,重启后才按新的来。
        """
        from .trust import TrustStore

        store = TrustStore()
        cwd = self._rt.cwd if self._rt is not None else Path.cwd()
        choice = (arg or "yes").strip().lower()
        if choice in ("forget", "clear", "none"):
            removed = store.forget(cwd)
            self._note(f"{cwd} 的信任决定{'已忘掉' if removed else '本来就没有'}"
                       "(上层若还有决定,它照样生效)", "info")
            return
        if choice not in ("yes", "no"):
            self._note("用法:/trust [yes|no|forget]", "warning")
            return
        path = store.set(cwd, choice == "yes", parent=True)
        verdict = "信任" if choice == "yes" else "不信任"
        self._note(f"已记住:{cwd}(及其上一层)以后都{verdict};写在 {path}。"
                   "**当前会话不重载,重启 qi 后生效**", "info")

    def action_show_tree(self) -> None:
        """/tree:跳转当前会话的任意节点(同一文件内的分支导航 + pi 的过滤/标签键)。"""
        session = self._session
        if session is None:
            return
        if not self._tree_rows(session, "all", "", False):
            self._flash("会话还是空的")
            return

        def picked(value: str | None) -> None:
            if value:
                self._run_guarded(self._jump_guarded(value))

        self.push_screen(TreeSelector(
            lambda mode, query, show_ts: self._tree_rows(session, mode, query, show_ts),
            on_label=self._set_entry_label,
            current=session.current), picked)

    def _user_message_options(self, session) -> list[tuple[str, str]]:
        """当前分支上的用户消息(fork 的可选点)。"""
        return [(str(e.get("id")), self._entry_label(e))
                for e in session.branch()
                if e.get("type") == "message" and e.get("role") == "user"]

    # -- 会话操作的事件闸门(pi 的 session_before_* / session_tree)----
    #
    # 为什么要这一层:`/fork` / `/tree` / `/new` / `/resume` 原本直接调 SessionStore,
    # 于是扩展的 `session_before_*` 只能拦住**扩展自己发起**的操作,拦不住用户按的键 ——
    # 闸门名不副实。`before_session_op` 把这个判断收进 runtime(那里才有总线),
    # TUI 只负责在动手前 await 一次。
    async def _session_gate_async(self, event: str, payload: dict) -> bool:
        """跑一次可取消的会话事件。返回 True = 放行。"""
        rt = self._rt
        gate = getattr(rt, "before_session_op", None) if rt is not None else None
        if not callable(gate):
            return True
        try:
            pending: Any = gate(event, payload)
            verdict = await pending if inspect.isawaitable(pending) else pending
        except Exception as exc:  # noqa: BLE001 闸门坏了不该把会话操作卡死
            self._note(f"会话事件 {event} 派发失败: {exc}", "warning")
            return True
        if verdict and verdict.get("cancel"):
            self._flash("已被扩展取消")
            return False
        return True

    async def _fork_guarded(self, session: Any, entry_id: str) -> None:
        if await self._session_gate_async("session_before_fork", {"entryId": entry_id}):
            self._fork_from(session, entry_id)

    async def _jump_guarded(self, entry_id: str) -> None:
        if not await self._session_gate_async("session_before_tree", {"targetId": entry_id}):
            return
        self._jump_to(entry_id)
        rt = self._rt
        notify = getattr(rt, "notify_session_tree", None) if rt is not None else None
        if callable(notify):
            notify(entry_id)

    async def _switch_guarded(self, session: Any, *, reason: str, note: str) -> None:
        """`/new` / `/resume` 的统一入口:先过 `session_before_switch`,再真正切。"""
        if not await self._session_gate_async("session_before_switch", {"reason": reason}):
            return
        rt = self._rt
        emit = getattr(rt, "emit_session_shutdown", None) if rt is not None else None
        old = self._session
        if callable(emit) and old is not None and old.id != getattr(session, "id", None):
            with contextlib.suppress(Exception):
                result: Any = emit(reason)
                if inspect.isawaitable(result):
                    await result
        self._switch_session(session, note=note)

    def _run_guarded(self, coro: Any) -> None:
        """把闸门+操作放进 worker(它们是 async,而 `_command` 是同步的)。"""
        self.run_worker(coro, exclusive=False, exit_on_error=False)

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
                            exclusive=False, exit_on_error=False)

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
        title = f"{session.display_label} @fork" if session.display_label else "fork"
        forked = self._session_store().fork_at(session, entry.get("parentId"), title=title)
        text = str(entry.get("content") or "")
        self._switch_session(forked, note=f"已 fork 出新会话 {forked.id}(那条消息已放回编辑器)")
        editor = self.query_one("#editor", TextArea)
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
            session, session.current, title=title or f"{session.display_label} 副本")
        self._switch_session(cloned, note=f"已 clone 到新会话 {cloned.id}(分支已复制)")

    def _switch_session(self, session, note: str = "") -> None:
        """切到另一个会话:重放它的当前分支,清掉属于上一个会话的临时状态。"""
        self._session = session
        # 通知 runtime 把「当前会话」换掉:换模型/换级别要记进**这个**文件,
        # 而且会话里记的模型/级别要在这个时机恢复(pi 的 `getSessionContextSettings`)。
        # 防御式取用:与 `notify_session_tree` 同一条约定 —— 测试替身不必实现每个可选方法。
        rt = self._rt
        bind = getattr(rt, "bind_session", None) if rt is not None else None
        if callable(bind):
            bind(session)
        self._sync_model_from_runtime()
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

    def _sync_model_from_runtime(self) -> None:
        """把界面缓存的模型/思考级别对齐到 runtime 当前真正在用的那一份。

        为什么必须显式同步:两处都存了同一件事 —— `bind_session` 会按会话里记的
        `model_change` **还原模型**(可能不是 settings 的默认值),而 `self._model` /
        `self._thinking_level` 是界面自己的一份缓存。不同步就会出现"footer 说 deepseek、
        请求发的是 glm"(footer 是用户唯一能看见的真相,它错了最难查)。
        """
        rt = self._rt
        spec = getattr(getattr(rt, "llm_exec", None), "spec", None) if rt is not None else None
        if spec is not None:
            self._model = spec
        if rt is not None:
            self._thinking_level = normalize_thinking_level(
                getattr(rt, "thinking_level", None))

    async def await_screen(self, screen: ModalScreen[Any]) -> Any:
        """弹一个模态并**等**它的结果。

        textual 的 `push_screen` 是回调式的,所以这里包一层 future —— 扩展的 `ctx.ui`
        是 await 风格(它本来就在 async handler 里),回调式会让每个调用点都写成回调地狱。

        界面不在跑(构造了 App 但没 `run`)时直接返回 None:没人可问,让调用方走 default,
        而不是在这里挂死一个永远不会被解析的 future。
        """
        if not self.is_running:
            return None
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def done(value: Any) -> None:
            if not future.done():
                future.set_result(value)

        self.push_screen(screen, done)
        return await future

    def _select_session(self) -> None:
        """按 CLI 传来的意图选/建会话,并把“会话已绑定”告知扩展。

        以前 TUI 无视这些参数、每次都新建一个名叫 `tui` 的会话 —— 文档里写了 `-c` 支持,
        但进 TUI 就失效了。现在与 headless 路径用同一套规则。
        """
        self._pick_session()
        self._bind_current_session()
        self._notify_session_start()

    def _bind_current_session(self) -> None:
        """把刚选定的会话**同步**告诉 runtime(换模型/换级别要记进这个文件)。

        必须与 `_notify_session_start()` 分开、且**先于**它:`start_session` 走
        `run_worker`(异步),而紧随 `_select_session()` 的 `_sync_model_from_runtime()`
        是同步的 —— 若只靠 `start_session` 里的 bind,续会话时 footer 会读到 restore
        **之前**的旧模型(于是 footer 显示 settings 默认、请求却发会话里记的那个,
        这类"看得见的与发出去的不一致"最难查)。`/resume` / `/fork` / `/import` 走的是
        `_switch_session` / `_import_session`,它们各自 bind —— 这里补的是**启动路径**。
        """
        rt = self._rt
        bind = getattr(rt, "bind_session", None) if rt is not None else None
        if self._session is not None and callable(bind):
            bind(self._session)

    def _notify_session_start(self) -> None:
        """派发 `session_start`(扩展的会话级初始化)。

        走 worker 而不是 `await`:选会话有多个**同步**调用点(on_mount / /new / /resume /
        /fork / /import),把它们全改成 async 的收益不抵风险。runtime 自己是幂等的
        (同一会话至多一次),所以这里“可能多调度一次”不会让扩展把资源开两遍。
        """
        if self._rt is None or self._session is None:
            return
        self.run_worker(self._rt.start_session(self._session), exclusive=False,
                        exit_on_error=False)

    def _pick_session(self) -> None:
        """按 CLI 意图选会话。**无事发生时不自作主张建文件** —— 见 `_lazy_session()`。

        懒建(对齐 web 端 §18.28 的同一条结论):"点了就落文件"会让 TUI 每次启动都留一个
        空会话,`--no-session` 更是宣称不落盘却落了盘。所以:
          - 显式给了会话意图(`--session` / `--fork` / `--session-id` / `-c` / `--name`)→ 照做;
          - 什么都没给 → **不建**,等第一句话真发出去时再建(`_submit` → `_lazy_session()`);
          - `--no-session` → 建**内存会话**(entries 照常,永不写盘)。
        """
        store = self._session_store()
        cwd = self._rt.cwd if self._rt is not None else Path.cwd()
        name = (self._want_name or "").strip()

        if self._want_no_session:
            # 真·不落盘:以前走 `create()`,于是 `--no-session` 照样留文件
            self._session = store.ephemeral(name or "ephemeral", cwd=cwd)
            return
        if self._want_fork_id:
            source = _open_session_ref(store, self._want_fork_id)
            if source is None:
                self._session = store.create(name, cwd=cwd)
                self._startup_note = f"会话不存在: {self._want_fork_id}(已新建)"
                return
            # 分叉出来的会话**自带** `源名 @fork`:它有名字(所以自动命名不会覆盖),
            # 而且来源一眼可见。源会话没名字时用它的第一句话当名字。
            title = name or (f"{source.display_label} @fork" if source.display_label else "fork")
            self._session = store.fork_at(source, source.current, title=title)
            self._startup_note = f"已从 {source.id} 分叉出新会话 {self._session.id}"
            return
        if self._want_session_id:
            found = _open_session_ref(store, self._want_session_id)
            if found is None:
                self._session = store.create(name, cwd=cwd)
                self._startup_note = f"会话不存在: {self._want_session_id}(已新建)"
            else:
                self._session = found
                if name:
                    store.set_title(self._session, name)
            return
        if self._exact_session_id:
            # `--session-id <id>`:精确 id,**不存在则建**(pi 同名旗标)
            # 这是**显式**要一个特定 id 的会话(脚本/CI 拿它当句柄),所以照建 —— 不懒。
            found = store.get(self._exact_session_id)
            self._session = found or store.create(name, cwd=cwd,
                                                 session_id=self._exact_session_id)
            return
        if self._want_cont:
            self._session = store.latest() or store.create(name, cwd=cwd)
            return
        if name:
            # `qi -n <名字>`:显式要一个带名字的新会话 → 建(否则一个没文件的名字没处安放)
            self._session = store.create(name, cwd=cwd)
            return
        # 裸 `qi`:**预留**一个会话,但**不建文件** —— 文件推迟到第一条助手回答。
        #
        # 为什么不干脆留空(`self._session = None`):pi 的 `SessionManager` 构造时就
        # `newSession()`(只算路径、`flushed=false`),于是"当前会话"这个不变量在 pi 里
        # 始终成立 —— 历史、用量、扩展的 `ctx.session_manager`、`/session` 信息行都照常工作。
        # 留空则要在下游每一处判 None,而那些判空迟早漏一个。所以:**对象照建,只懒文件**。
        self._session = store.reserve(name, cwd=cwd)

    def _lazy_session(self):
        """第一句话真发出去时把会话**预留**出来(懒建的唯一收口)。

        注意"预留"不是"建文件":`store.reserve()` 只定下 id 与路径,文件推迟到第一条
        **assistant** 回答(`session.py` 的 `_persist`)。所以"问一句就被打断"也不留文件。

        为什么值得为它单独一个方法:`--no-session` 要内存会话、普通启动要预留,
        而这层差别不该散到 `_submit` / `/new` / 删掉当前会话后继续说话……每个调用点各写一遍,
        迟早有一处忘了(那处就会重新开始漏空会话)。
        """
        store = self._session_store()
        cwd = self._rt.cwd if self._rt is not None else Path.cwd()
        if self._want_no_session:
            return store.ephemeral(self._want_name or "ephemeral", cwd=cwd)
        return store.reserve(self._want_name, cwd=cwd)

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
        renderers = self._renderers()
        for entry in session.branch():
            kind = entry.get("type")
            custom_type = entry.get("custom_type")
            # ① 扩展的渲染器优先:custom entry 用 entry renderer,custom message 用
            # message renderer(pi 的 registerEntryRenderer / registerMessageRenderer)。
            # 回调返回 None 或抛错 → 退回下面的内置渲染(绝不白屏)。
            if renderers is not None and custom_type:
                fn = (renderers.message_renderer(custom_type) if kind == "message"
                      else renderers.entry_renderer(custom_type))
                widget = self._extension_widget(fn, entry)
                if widget is not None:
                    self._append(widget)
                    continue
            if kind == "message" and entry.get("role") == "user":
                self._append(UserMessage(self._transform_markdown(str(entry.get("content") or "")),
                                         renderer, self._palette))
            elif kind == "message" and entry.get("role") == "assistant":
                message = AssistantMessage(self._palette, self._output_pad)
                message.set_text(self._transform_markdown(str(entry.get("content") or "")), renderer)
                self._append(message)
            elif kind == "custom" and entry.get("custom_type") == "assistant_narration":
                message = AssistantMessage(self._palette, self._output_pad)
                message.set_text(self._transform_markdown(str(entry.get("content") or "")), renderer)
                self._append(message)
            elif kind == "dispatch":
                self._append(Static(renderer.dispatch_line(entry.get("agent"), entry),
                                    classes="msg"))
            elif kind == "tool":
                name = str(entry.get("tool") or "?")
                status = "ok" if entry.get("status") == "ok" else "error"
                result = str(entry.get("result") or "")
                block = self._tool_block(name, entry.get("args") or {}, state=status)
                widget = self._tool_result_widget(name, result)
                if widget is not None and isinstance(block, ExtensionToolBlock):
                    with contextlib.suppress(Exception):
                        block.show(widget)
                else:
                    block.set_output(renderer.tool_body(name, result, expanded=self._expanded,
                                                        is_error=status != "ok"))
                self._tool_blocks.append((block, name, result))
                self._append(block)
            elif kind in ("compaction", "branch_summary"):
                self._append_compaction(entry)
            elif kind == "custom":
                # 未注册渲染器的 custom entry:**退回原始显示,绡不丢弃**
                # (docs/extensions.md §8.2 的旧会话回放兼容)。以前这里什么都不画,
                # 于是“扩展存的状态”在回放里凭空消失 —— 那是很难查的一类不一致。
                self._append(Static(
                    f"[{custom_type or 'custom'}] "
                    f"{json.dumps(entry.get('data') or {}, ensure_ascii=False)}",
                    classes="msg"))
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
        """内置候选 + **扩展补全提供者**(`ctx.ui.add_autocomplete_provider`)。

        合并规则(写清楚,因为它是 qi 自己的契约 —— pi 传的是 pi-tui 的 provider 对象):
        provider 收到 `(text, cursor_offset)`,返回 `list[str | {value, label, description}]`。
        * 内置也有候选 → 扩展项插在**前面**,与内置项共用同一个替换区间;
        * 内置没有候选 → 用**当前词**(到上一个空白)作区间 —— 所以“打 `#12`,选 `#1234`”
          会正确地把 `#12` 换掉。
        """
        try:
            items, start, end = self._builtin_candidates()
        except (NoMatches, ScreenStackError):
            return [], 0, 0
        text, offset = self._editor_text_offset()
        extra = self._extension_completion_items(text, offset)
        # async 补全的回填:只有文本与光标**都没动**时才采用(否则候选对不上现在的 token)
        if self._async_completion is not None:
            where, pending_items = self._async_completion
            if where == (text, offset):
                extra = [*extra, *pending_items]
        if not extra:
            return items, start, end
        if items:
            return [*extra, *items], start, end
        return extra, self._word_start(text, offset), offset

    def _extension_completion_items(self, text: str, offset: int) -> list[Candidate]:
        """问所有注册的补全提供者要候选(单个抛错不拖垮内置补全)。

        **async 返回值不支持** —— 每次按键都要算,补全路径是同步的。
        """
        if not self._autocomplete_providers:
            return []
        items: list[Candidate] = []
        for provider in list(self._autocomplete_providers):
            try:
                raw: Any = provider(text, offset)
            except Exception as exc:  # noqa: BLE001 扩展的 provider 坏不该把输入框弄挂
                self._note(f"扩展补全提供者失败: {type(exc).__name__}: {exc}", "warning")
                continue
            if inspect.isawaitable(raw):
                # **async 补全支持**:排一个 worker 等结果,回来后再刷一次面板。
                # 不能直接 note 掉 —— pi 的 `getArgumentCompletions` 允许返回 Promise。
                self._schedule_async_completion(raw)
                continue
            items.extend(_candidate_items(raw))
        return items[:MAX_COMPLETION_ITEMS]

    def _schedule_async_completion(self, pending: Any) -> None:
        """async 补全回调:排一个 worker 等结果,回来后重刷面板。

        结果按“当时的光标位置”存下来(`_async_completion`),只有文本与光标都没变时
        才被采用 —— 否则用户已经接着打字了,一堆旧候选会比没有还糟。文本一变就丢。
        """
        text, offset = self._editor_text_offset()

        async def _run() -> None:
            try:
                raw = await pending
            except Exception as exc:  # noqa: BLE001 扩展的补全坏不该把输入框弄挂
                self._note(f"async 补全失败: {type(exc).__name__}: {exc}", "warning")
                return
            items = _candidate_items(raw)
            if not items:
                return
            self._async_completion = ((text, offset), items)
            self._refresh_completions()

        try:
            self.run_worker(_run(), exclusive=False, exit_on_error=False)
        except Exception:  # noqa: BLE001 界面未在跑:丢掉这个 coroutine 免得 "never awaited"
            close = getattr(pending, "close", None)
            if callable(close):
                close()

    def _editor_text_offset(self) -> tuple[str, int]:
        """编辑器全文 + 光标的字符偏移(提供者拿到的是这个,不是 (row, col))。"""
        try:
            editor = self.query_one("#editor", TextArea)
        except (NoMatches, ScreenStackError):
            return "", 0
        text = editor.text
        row, col = editor.cursor_location
        lines = text.split("\n")
        if row >= len(lines):
            return text, len(text)
        return text, min(len("\n".join(lines[:row])) + (row > 0) + col, len(text))

    @staticmethod
    def _word_start(text: str, offset: int) -> int:
        """光标前那个“词”的起点(空白分隔)—— 没有内置候选时用作替换区间。"""
        start = offset
        while start > 0 and text[start - 1] not in " \t\n":
            start -= 1
        return start

    def _builtin_candidates(self) -> tuple[list[Candidate], int, int]:
        """根据光标前的 token 给出候选。

        返回 `(candidates, start, end)`,start/end 是要被替换的区间。规则对齐 pi:
          · 行首的 `/xxx`(不含第二个 `/`)= 命令名补全
          · `/cmd <前缀>` = 参数补全(`/model` `/thinking` `/login`,pi 的 getArgumentCompletions)
          · 当前 token 以 `@` 开头 = 相对路径补全
        """
        editor = self.query_one("#editor", TextArea)
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
            # **扩展注册的命令也算**(pi 的模型:extensions can register custom commands,
            # 而它们靠 `/` 补全被发现)。以前这里只遍历内置表 —— 配上 `/help` 被删掉,
            # 扩展命令就彻底不可见了(只能去读那个扩展的 README)。
            extra = [(f"/{c.invocable}", c.description or "")
                     for c in (self._rt.commands.all() if self._rt is not None else [])]
            # 技能也注册成命令(pi 的 `enableSkillCommands`):`/skill:<名>`
            extra += skill_command_candidates(self._rt)
            items = [Candidate(name, name, detail)
                     for name, detail in sorted([*TUI_COMMANDS.items(), *extra])
                     if name.startswith(before)]
            if len(items) == 1 and items[0].value == before:
                items = []          # 已完整匹配,不必再提示
            return items, row_start, row_start + col

        # 1b) 参数补全:`/cmd <前缀>`(还没输入第二个参数)
        #     内置表 + **扩展命令的 `getArgumentCompletions`**(pi 的 registerCommand 选项)
        if before.startswith("/") and " " in before:
            cmd, _, rest = before.partition(" ")
            if " " not in rest:
                key = cmd.lower()
                items = (self._argument_candidates(key, rest)
                         if key in ARG_COMPLETION_COMMANDS else [])
                items = [*items, *self._extension_argument_candidates(cmd, rest)]
                if items:
                    start = row_start + len(cmd) + 1
                    return items, start, row_start + col

        # 2) 文件补全:`@路径` 或 `@"带空格的路径"`(pi 的 PATH_DELIMITERS / 引号规则)
        match = _at_prefix(before)
        if match is None:
            return [], 0, 0
        start, raw, quoted = match
        items = self._file_candidates(raw, quoted=quoted)
        return items, row_start + start, row_start + col

    def _extension_argument_candidates(self, cmd: str, prefix: str) -> list[Candidate]:
        """扩展命令的参数补全(pi 的 `getArgumentCompletions(prefix)`)。

        收 `list[str]` 或 `list[{value|label, description}]`。**async 返回值不支持** ——
        补全路径是同步的;遇到 coroutine 会记一条 note 并忽略(不静默)。
        """
        if self._rt is None:
            return []
        command = self._rt.commands.find(cmd.lstrip("/"))
        fn = getattr(command, "get_argument_completions", None) if command is not None else None
        if not callable(fn):
            return []
        try:
            raw: Any = fn(prefix)
        except Exception as exc:  # noqa: BLE001 扩展的补全坏不该把输入框弄挂
            self._note(f"扩展参数补全失败({cmd}): {exc}", "warning")
            return []
        if inspect.isawaitable(raw):
            # 同上:async 也能用,只是结果晚一拍到(worker 回来后重刷面板)
            self._schedule_async_completion(raw)
            return []
        return _candidate_items(raw)

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
        return out[:MAX_COMPLETION_ITEMS]

    def _file_candidates(self, query: str, *, quoted: bool = False) -> list[Candidate]:
        """`@` 后的路径候选:优先 fd 全树搜索(pi 同款),没装 fd 就扫当前目录一层。

        `quoted` = 已在 `@"…"` 里:候选也补上成对引号,带空格的路径同理。
        """
        base = Path(self._rt.cwd) if self._rt is not None else Path.cwd()
        found = _fd_candidates(base, query) or _scan_candidates(base, query)
        out: list[Candidate] = []
        for display, is_dir in found[:MAX_COMPLETION_ITEMS]:
            name = display.rsplit("/", 1)[-1]
            label = name + ("/" if is_dir else "")
            out.append(Candidate(_completion_value(display, is_dir, quoted), label,
                                 "" if display == name else display))
        return out

    def _refresh_completions(self) -> None:
        """重算候选并同步面板显隐(不改文本,只负责菜单)。"""
        try:
            panel = self.query_one("#completions", CompletionPanel)
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
        # 面板自己按 pi 的 SelectList 版式画(箭头 + 列对齐 + muted 说明,
        # 无底色);行数上限也由它拿(`autocompleteMaxVisible`)。
        panel.set_items(candidates, self._completion_rows)
        panel.highlighted = 0
        panel.add_class("visible")
        self._completions_open = True
        self._sync_log_height()

    def _move_completion(self, step: int) -> None:
        panel = self.query_one("#completions", CompletionPanel)
        count = panel.option_count
        if not count:
            return
        current = panel.highlighted
        panel.highlighted = (current + step) % count

    def _close_completions(self) -> None:
        self._completions_open = False
        self._completions = []
        try:
            panel = self.query_one("#completions", CompletionPanel)
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
        panel = self.query_one("#completions", CompletionPanel)
        index = panel.highlighted
        picked = candidates[min(index, len(candidates) - 1)]
        value, label = picked.value, picked.label
        editor = self.query_one("#editor", TextArea)
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
        self.run_worker(self._exec_bash(command, excluded, block), exclusive=False,
                        exit_on_error=False)

    async def _exec_bash(self, command: str, excluded: bool, block: BashBlock) -> None:
        """执行用户手敲的命令;`!` 会把「命令 + 输出」落成一条 user 消息供后续回合参考。"""
        rt = self._rt
        cwd = str(rt.cwd) if rt is not None else str(Path.cwd())
        # `user_bash`(pi):`!` / `!!` 命令执行**前**通知。契约照 pi:
        # `{operations: {command}}` 可换掉要跑的命令,`{result: "…"}` 则直接给结果不执行。
        if rt is not None and getattr(getattr(rt, "bus", None), "has", lambda _e: False)(
                "user_bash"):
            verdict = await rt.bus.emit(
                "user_bash",
                {"command": command, "excludeFromContext": bool(excluded), "cwd": cwd},
                ctx=rt.extension_ctx(self._abort))
            for src, exc in verdict.errors:
                rt.notes.append(f"扩展 {src} 的 user_bash 处理失败: {exc}")
            operations = verdict.payload.get("operations")
            if isinstance(operations, dict) and operations.get("command"):
                command = str(operations["command"])
            given = verdict.payload.get("result")
            if isinstance(given, str):
                block.set_result(given, 0)
                self._scroll_end()
                return
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
        editor = self.query_one("#editor", TextArea)
        current = editor.text.rstrip("\n")
        restored = "\n".join([current, *queued]) if current else "\n".join(queued)
        editor.load_text(restored)
        editor.move_cursor(self._offset_to_location(restored, len(restored)))
        self._sync_log_height()
        self._restore_status()
        self._flash(f"已取回 {len(queued)} 条排队消息")

    # -- 底部状态行的瞬时提示(pi 的状态区,不加额外 chrome)----------
    def _default_status(self) -> str:
        # P-E4c:"分派模式"不再存在(core 是单 agent,auto 取消) —— 以前那行 `qi · auto`
        # 是模式指示,现在没有可显的东西就**整行不出现**(同 pi:第三行只放扩展状态)。
        # 角色(qi-agents)要显示自己的东西就走 `ctx.ui.set_status`。
        parts: list[str] = []
        if self._queue_count():
            parts.append(f"排队 {self._queue_count()}")
        # 扩展写的状态片段:按 key 字典序(渲染顺序稳定,不因插入次序而抳)
        for key in sorted(self._ext_statuses):
            parts.append(self._ext_statuses[key])
        return " · ".join(parts)

    def _flash(self, message: str, seconds: float = 2.0) -> None:
        self._status = message
        self._refresh_footer()
        self.set_timer(seconds, self._restore_status)

    def _restore_status(self) -> None:
        self._status = self._default_status()
        self._refresh_footer()

    # -- 扩展 UI 后端(ctx.ui 的方法委托到这里)----------------
    def set_extension_status(self, key: str, text: str | None) -> None:
        """`ctx.ui.set_status`:footer 状态行里的一条(`text=None` 清除)。"""
        if text is None:
            self._ext_statuses.pop(key, None)
        else:
            self._ext_statuses[str(key)] = str(text)
        self._status = self._default_status()
        self._refresh_footer()

    def set_terminal_title(self, title: str) -> None:
        """`ctx.ui.set_title`:终端窗口/标签页标题(Textual `App.title`)。"""
        self.title = str(title)

    def set_working_message(self, message: str | None) -> None:
        self._working_message = str(message) if message else None
        self._repaint_borders()

    def set_working_visible(self, visible: bool) -> None:
        self._working_visible = bool(visible)
        self._repaint_borders()

    def set_working_indicator(self, options: dict) -> None:
        """`ctx.ui.set_working_indicator`:`{"frames": [...], "intervalMs": int}`。"""
        frames = options.get("frames")
        if isinstance(frames, list):
            self._working_frames = [str(f) for f in frames]
            self._frame = 0
        raw = options.get("intervalMs") or options.get("interval_ms")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            try:
                interval = float(raw) / 1000.0
            except (TypeError, ValueError):
                interval = 0.0
            if interval > 0:
                self._working_interval = interval
                if self._frame_timer is not None:
                    self._frame_timer.stop()
                    self._frame_timer = self.set_interval(self._working_interval, self._tick)
        self._repaint_borders()

    def set_hidden_thinking_label(self, label: str | None) -> None:
        self._hidden_thinking_label = str(label) if label else None

    def set_tools_expanded(self, expanded: bool) -> None:
        """`ctx.ui.set_tools_expanded`:等同内置的“展开工具输出”。"""
        self._expanded = bool(expanded)
        for block in [*self._bash_blocks, *self._compaction_blocks]:
            setter = getattr(block, "set_expanded", None)
            if callable(setter):
                setter(self._expanded)
        self._refresh_footer()

    def get_editor_text(self) -> str:
        try:
            return self.query_one("#editor", TextArea).text
        except (NoMatches, ScreenStackError):
            return ""

    def set_editor_text(self, text: str) -> None:
        try:
            self.query_one("#editor", TextArea).text = str(text)
        except (NoMatches, ScreenStackError):
            return

    # -- 主题(ctx.ui.getAllThemes / getTheme / setTheme)--------
    def all_themes(self) -> list[dict]:
        from . import theme as theme_mod
        return [{"name": name, "path": str(theme_mod.THEMES_DIR / f"{name}.json")}
                for name in theme_mod.THEME_CHOICES]

    def theme_by_name(self, name: str) -> Any:
        from .theme import Palette, ThemeError, load_palette
        try:
            palette: Palette = load_palette(str(name))
        except ThemeError:
            return None
        return palette

    def apply_theme(self, theme: Any) -> dict:
        """`ctx.ui.set_theme`:切主题(名字或 Palette)。返回 `{success, error?}`。"""
        from .theme import Palette, ThemeError, load_palette
        try:
            palette = theme if isinstance(theme, Palette) else load_palette(str(theme))
        except ThemeError as exc:
            return {"success": False, "error": str(exc)}
        self._palette = palette
        try:
            textual = textual_theme(palette)
            self.register_theme(textual)
            self.theme = textual.name
            self.console.push_theme(rich_theme(palette))
            self.refresh()
        except Exception as exc:  # noqa: BLE001 主题切换失败不该把应用弄崩
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"success": True}

    # -- setWidget / custom ----------------------------------------
    def set_extension_widget(self, key: str, content: Any, options: dict) -> None:
        """`ctx.ui.set_widget`:在编辑器上/下方挂一个常驻 widget。"""
        existing = self._ext_widgets.pop(key, None)
        if existing is not None:
            try:
                existing.remove()
            except Exception as exc:  # noqa: BLE001 移除失败不该把命令打断,但要看得见
                self._note(f"移除扩展 widget {key!r} 失败: {exc}", "warning")
        if content is None:
            return
        placement = str(options.get("placement") or "aboveEditor")
        slot_id = "ext-widgets-below" if placement == "belowEditor" else "ext-widgets-above"
        try:
            slot = self.query_one(f"#{slot_id}")
        except (NoMatches, ScreenStackError):
            self._note(f"扩展 widget 槽位不存在({slot_id})", "warning")
            return
        ctx = self._rt.extension_ctx() if self._rt is not None else None
        widget: Any = content(ctx) if callable(content) else None
        if widget is None:
            text = ("\n".join(str(x) for x in content)
                    if isinstance(content, (list, tuple)) else str(content))
            widget = Static(text)
        try:
            widget.id = f"ext-widget-{key}"
            slot.mount(widget)
        except Exception as exc:  # noqa: BLE001 挂载失败要看得见(否则组件“静静消失”)
            self._note(f"挂载扩展 widget {key!r} 失败: {exc}", "warning")
            return
        self._ext_widgets[key] = widget

    async def await_custom(self, factory: Any, options: dict) -> Any:
        """`ctx.ui.custom`:把扩展给的组件放进模态,等它调 `done(value)` 或 escape。

        factory 可以收 `(ctx, done)` / `(ctx)` / 无参 —— 按形参个数适配(pi 的 factory
        拿到的是 `(tui, theme, keybindings, done)`,qi 没有 tui 对象,所以给 ctx)。
        """
        box: dict = {"widget": None}
        screen = CustomScreen(box, options.get("title"))

        def done(value: Any = None) -> None:
            with contextlib.suppress(Exception):
                screen.dismiss(value)

        if callable(factory):
            try:
                arity = len(inspect.signature(factory).parameters)
            except (TypeError, ValueError):
                arity = 0
            ctx = self._rt.extension_ctx() if self._rt is not None else None
            if arity >= 2:
                box["widget"] = factory(ctx, done)
            elif arity == 1:
                box["widget"] = factory(ctx)
            else:
                box["widget"] = factory()
        else:
            box["widget"] = factory
        if box["widget"] is None:
            return None
        return await self.await_screen(screen)

    # -- setFooter / setHeader ------------------------------------
    def _build_extension_component(self, factory: Any, what: str) -> Any:
        """把扩展给的组件工厂变成 widget(收 `(ctx) -> Widget`,也直接收 Widget)。"""
        widget = None
        if factory is not None:
            if callable(factory):
                ctx = self._rt.extension_ctx() if self._rt is not None else None
                try:
                    arity = len(inspect.signature(factory).parameters)
                except (TypeError, ValueError):
                    arity = 0
                widget = factory(ctx) if arity else factory()
            else:
                widget = factory
        if widget is None:
            self._note(f"扩展的 {what} 工厂没有返回组件(已忽略)", "warning")
        return widget

    def set_extension_footer(self, factory: Any) -> None:
        """`ctx.ui.set_footer`:整个替换 footer(`None` = 恢复内置)。"""
        for old in list(self.query(".ext-footer")):
            old.remove()
        self._ext_footer = factory
        try:
            builtin = self.query_one("#footer")
        except NoMatches:
            return
        if factory is None:
            builtin.styles.display = "block"
            self._refresh_footer()
            return
        widget = self._build_extension_component(factory, "footer")
        if widget is None:
            return
        # 内置 footer 只是**藏起来**,不卸载 —— `_refresh_footer` 仍按原样写它,
        # 而扩展把 footer 撤回去时立即就能恢复(否则得重建一个 widget)。
        builtin.styles.display = "none"
        widget.add_class("ext-footer")
        try:
            self.query_one("#body").mount(widget, before=builtin)
        except Exception as exc:  # noqa: BLE001 挂载失败要看得见
            self._note(f"挂载扩展 footer 失败: {exc}", "warning")

    def set_extension_header(self, factory: Any) -> None:
        """`ctx.ui.set_header`:在对话区上方放一个 header(`None` = 移除)。"""
        for old in list(self.query(".ext-header")):
            old.remove()
        self._ext_header = factory
        if factory is None:
            return
        widget = self._build_extension_component(factory, "header")
        if widget is None:
            return
        widget.add_class("ext-header")
        try:
            self.query_one("#body").mount(widget, before=self.query_one("#log"))
        except Exception as exc:  # noqa: BLE001
            self._note(f"挂载扩展 header 失败: {exc}", "warning")

    # -- onTerminalInput / addAutocompleteProvider -----------------
    def add_autocomplete_provider_impl(self, provider: Any) -> Any:
        """`ctx.ui.add_autocomplete_provider`:注册补全提供者,返回**退订函数**。

        契约:`provider(text, cursor_offset) -> list[str | {value, label, description}]`
        (与命令参数补全同一套元素形状 —— 两种补全只学一次)。
        """
        if not callable(provider):
            return lambda: None
        self._autocomplete_providers.append(provider)

        def _unsubscribe() -> None:
            try:
                self._autocomplete_providers.remove(provider)
            except ValueError:
                return
        return _unsubscribe

    def add_terminal_input_handler(self, handler: Any) -> Any:
        """`ctx.ui.on_terminal_input`:注册一个原始按键处理器,返回**退订函数**。

        handler 返回 `{"consume": True}` = 这个键不再往下走(pi 的 `consume` 语义)。
        pi 的 `data`(改写按键)qi 不支持 —— 返回它也不会有第二次分派,所以不假装支持。
        """
        if not callable(handler):
            return lambda: None
        self._terminal_input_handlers.append(handler)

        def _unsubscribe() -> None:
            try:
                self._terminal_input_handlers.remove(handler)
            except ValueError:
                return
        return _unsubscribe

    def on_key(self, event: events.Key) -> None:
        """给扩展一次看按键的机会(未被编辑器与 priority 绑定吃掉的键会冒泡到这里)。"""
        for handler in list(self._terminal_input_handlers):
            try:
                result = handler(event.key)
            except Exception as exc:  # noqa: BLE001 第三方代码
                self._note(f"扩展终端输入处理器失败: {exc}", "warning")
                continue
            if isinstance(result, dict) and result.get("consume"):
                event.stop()
                return

    async def on_unmount(self) -> None:
        """退出前发 `session_shutdown`(pi 的 `quit` 语义)。

        扩展用它做收尾(提交、落盘、关连接)—— 所以幂等由这个标志兼管:Textual 卸载
        与显式 exit 都可能走到这里。
        """
        rt = self._rt
        if rt is None or getattr(self, "_session_shutdown_sent", False):
            return
        bus = getattr(rt, "bus", None)
        emit = getattr(rt, "emit_session_shutdown", None)
        if bus is None or not callable(emit) or not bus.has("session_shutdown"):
            return
        self._session_shutdown_sent = True
        try:
            result: Any = emit("quit")
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 收尾失败不该阻止退出
            return


def _version() -> str:
    from . import __version__

    return __version__


# 关掉鼠标上报 | 清掉可能残留的状态
#
# 1000 = 按键/释放、1003 = 任意移动、1015 = urxvt 编码、1006 = SGR 编码。
# `mouse=False` 时 Textual 不会写这串(它的 `_disable_mouse_support` 直接 return),
# 所以上一次**崩溃**留下的上报模式会在终端里一直活着:鼠标一动就发 X10 报文。
_MOUSE_OFF = "\x1b[?1000l\x1b[?1003l\x1b[?1015l\x1b[?1006l"


def _reset_mouse_reporting() -> None:
    """进界面前主动关掉鼠标上报(包括上一次崩溃没还原的残留)。

    残留开着时,鼠标移动会让终端不停发旧式 X10 报文(`ESC [ M` + 原始坐标字节);
    即使已经加固了 UTF-8 解码,那些字节也只能变成一堆 U+FFFD 打进输入框。
    驱动写的是 `sys.__stderr__`,这里保持一致。
    """
    with contextlib.suppress(Exception):  # 没有终端 / 已关闭:不是致命问题
        stream = sys.__stderr__ or sys.stderr
        stream.write(_MOUSE_OFF)
        stream.flush()


def _harden_inline_input() -> None:
    """让 inline 驱动对非法 UTF-8 字节免疫:一个坏字节不该打死整个会话。

    Textual 的 `LinuxInlineDriver` 用**严格** UTF-8 增量解码器读 stdin
    (`getincrementaldecoder("utf-8")()`,不带 `errors=`);而终端在**旧式 X10 鼠标
    编码**下发的报文是 `ESC [ M Cb Cx Cy`,后三个字节是原始坐标 —— 只要某个坐标
    ≥ 0x80,整段就不是合法 UTF-8,解码抛 `UnicodeDecodeError` → 输入线程死掉 →
    `App.panic` 打出栈并把 TUI 一起带走(报错就长这样:
    `'utf-8' codec can't decode byte 0x85 in position 4`)。

    终端不支持 SGR(1006)时报文就会退回 X10:macOS Terminal.app、部分 tmux/ssh,
    以及终端被重建(如 tmux detach/reattach)都会踩到;上游仍未修
    (textualize/textual#6456)。这里只换掉**该驱动模块**里那个工厂:坏字节变
    U+FFFD,`XTermParser` 照常把它当报文/字符吞掉,界面继续跑。

    幂等:重复进 TUI 只打一次补丁。
    """
    try:
        import codecs

        from textual.drivers import linux_inline_driver as driver_module
    except Exception:  # 驱动改名/缺失也只是少一层加固,不能挡住进界面
        return
    if getattr(driver_module, "_qi_lenient_decoder", False):
        return

    def lenient(encoding: str, errors: str = "strict") -> Any:
        # 驱动按 `factory("utf-8")()` 调用:交回一个默认 errors=replace 的类
        decoder_cls = codecs.getincrementaldecoder(encoding)
        return functools.partial(decoder_cls, errors="replace")

    driver_module.getincrementaldecoder = lenient  # type: ignore[assignment]
    driver_module._qi_lenient_decoder = True  # type: ignore[attr-defined]


def _open_session_ref(store: Any, ref: str) -> Any:
    """`--session` / `--fork` 的 `path|id` 形态:先当**文件路径**,再当 id / 前缀。

    与 CLI 侧 `_open_session` 同义 —— `get()` 只按 header id / 文件名 stem 前缀匹配,
    路径永远不命中,而帮助文字里写的是 `path|id`。
    """
    maybe = Path(ref).expanduser()
    if maybe.is_file():
        opened = store.open_file(maybe)
        if opened is not None:
            return opened
    return store.get(ref)


class SettingsPanel(App[Any]):
    """`/settings`:偏好面板(对齐 pi 的 `/settings`)。

    只放**值域有限且已接线**的键(见 `settings.SETTING_CHOICES`)。数字/路径类不放 ——
    在 TUI 里敲数字与路径的体验比 `qi config --set` 差,让人去那边改更诚实。

    enter/space **循环到下一个候选值**,`ctrl+s` 保存(写**用户级** settings,与 pi 的
    `/settings` 一致 —— 它管的是"以后的默认"),`escape` 取消。面板不碰盘:它只交回
    `{键: 新值}`,写盘由调用方做,所以取消时没有副作用。
    """

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("ctrl+s", "save", "保存"),
        Binding("enter", "pick", "下一个值", show=False),
        Binding("space", "pick", "下一个值", show=False),
    ]

    CSS = "#settings-box { padding: 1 2; height: auto; }"

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        super().__init__()
        self._rows = rows                       # (键, 当前值)
        self._original = list(rows)         # 只交回**改过的**
        self._chosen = {key: value for key, value in rows}

    def compose(self) -> ComposeResult:
        from textual.widgets import SelectionList
        from textual.widgets.selection_list import Selection

        selections = [Selection(f"{key} = {value}", index, False)
                      for index, (key, value) in enumerate(self._rows)]
        with Vertical(id="settings-box"):
            yield Static("设置:enter/space 换下一个值 · ctrl+s 保存(写用户级 settings) · "
                         "escape 取消", id="settings-hint")
            yield SelectionList[int](*selections, id="settings-list")

    def on_selection_list_selected_changed(
            self, event: "SelectionList.SelectedChanged[int]") -> None:
        del event                                # 面板不用勾选:enter 是"换值"

    def action_pick(self) -> None:
        from textual.widgets import SelectionList

        widget = self.query_one("#settings-list", SelectionList)
        index = widget.highlighted
        if index is None or index >= len(self._rows):
            return
        key, _current = self._rows[index]
        self._chosen[key] = next_choice(key, self._chosen[key])
        label = f"{key} = {self._chosen[key]}"
        self._rows[index] = (key, self._chosen[key])
        widget.replace_option_prompt_at_index(index, label)

    def action_save(self) -> None:
        changed = {key: value for key, value in self._chosen.items()
                   if dict(self._original).get(key) != value}
        self.exit(changed or None)

    def action_cancel(self) -> None:
        self.exit(None)


class ResourcePanel(App[Any]):
    """`qi config` 的资源启停面板(对齐 pi 的 `pi config`:space 勾选 / ctrl+s 保存)。

    它**不碰 settings** —— 只把“哪些保持启用”(下标集合)交回去,写声明由
    `packages.apply_resource_selection` 做。这样面板能被单独驱动测试,
    取消(escape)时也不会留下任何副作用。

    **已关闭的项照样列出来**(只是不勾)—— 那是“状态被记住”换来的能力,
    也是 pi 的 `pi config -l` 能把继承项置灰的同一件事。
    """

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("ctrl+s", "save", "保存"),
        Binding("ctrl+a", "pick_all", "全选", show=False),
        Binding("ctrl+x", "pick_none", "全不选", show=False),
    ]

    CSS = "#resource-box { padding: 1 2; height: auto; }"

    def __init__(self, items: list[tuple[str, str, str, bool]]) -> None:
        """`items` = `(名字, 类型, 作用域, 启用)`。"""
        super().__init__()
        self._items = items

    def compose(self) -> ComposeResult:
        from textual.widgets import SelectionList
        from textual.widgets.selection_list import Selection

        selections = [
            Selection(f"{name}  [{kind} · {scope}]", index, enabled)
            for index, (name, kind, scope, enabled) in enumerate(self._items)
        ]
        with Vertical(id="resource-box"):
            yield Static("资源:space 勾选 · ctrl+s 保存 · ctrl+a 全选 · ctrl+x 全不选 · "
                         "escape 取消(不勾 = 关掉那个资源)", id="resource-hint")
            yield SelectionList[int](*selections, id="resource-list")

    def action_save(self) -> None:
        self.exit(self._chosen())

    def action_cancel(self) -> None:
        self.exit(None)

    def _chosen(self) -> set[int]:
        from textual.widgets import SelectionList

        return set(self.query_one("#resource-list", SelectionList).selected)

    def action_pick_all(self) -> None:
        from textual.widgets import SelectionList

        self.query_one("#resource-list", SelectionList).select_all()

    def action_pick_none(self) -> None:
        from textual.widgets import SelectionList

        self.query_one("#resource-list", SelectionList).deselect_all()


def skill_command_candidates(rt: Any) -> list[tuple[str, str]]:
    """`/skill:<名>` 那批命令(空列表 = 关掉了或没有技能)。

    纯函数便于测:技能平时**渐进披露**(提示词里只有描述),而 `/skill:<名>` 是**强制加载**
    的入口 —— pi 的 `docs/skills.md` 原话:"use prompting or `/skill:name` to force it"。
    `settings.enableSkillCommands`(默认 true,pi 同名)关掉时这批命令不存在。
    """
    if rt is None or not getattr(getattr(rt, "settings", None), "enableSkillCommands", True):
        return []
    return [(f"/skill:{skill.name}", skill.description or "")
            for skill in (getattr(rt, "top_skills", None) or [])]


def skill_invocation(skill: Any, args: str) -> str:
    """`/skill:<名> [参数]` 要提交给模型的那段文本 —— 技能全文 + 一句“按它执行”。

    pi 的语义是 "Load and execute the skill",而参数是技能的入参
    (`/skill:pdf-tools extract`)。效果与用户手贴 SKILL.md 等价,只是不用手抄。
    """
    head = f"按技能 `{skill.name}` 执行" + (f":{args}" if args else "。")
    return f"{head}\n\n{skill.path.read_text(encoding='utf-8')}"


def run_settings_panel(rows: list[tuple[str, str]]) -> dict[str, str] | None:
    """起偏好面板;返回**改过的** `{键: 新值}`(取消或无改动 → None)。

    inline 小 App(与资源面板同理,不跟随主界面的 tuiMode)。
    """
    _reset_mouse_reporting()
    _harden_inline_input()
    return SettingsPanel(rows).run(inline=True, inline_no_clear=True, mouse=False)


def run_resource_panel(items: list[tuple[str, str, str, bool]]) -> set[int] | None:
    """起面板;返回**保持启用**的下标集合(取消返回 None)。

    用 inline 小 App:它是从主 TUI 里弹出来的短暂覆盖层,不跟随主界面的 tuiMode。
    """
    _reset_mouse_reporting()
    _harden_inline_input()
    return ResourcePanel(items).run(inline=True, inline_no_clear=True, mouse=False)


def run_tui(initial_prompt: str | None = None, *, session_id: str | None = None,
            cont: bool = False, fork_id: str | None = None,
            no_session: bool = False, name: str | None = None,
            approve_project: bool | None = None,
            extension_flags: list[str] | None = None,
            extra_extension_paths: list[Path] | None = None,
            tools: str | None = None, exclude_tools: str | None = None,
            no_tools: bool = False, no_builtin_tools: bool = False,
            append_system_prompt: list[str] | None = None,
            resume: bool = False, exact_session_id: str | None = None,
            base_prompt_override: str | None = None,
            no_extensions: bool = False, no_context_files: bool = False,
            session_dir_path: str | None = None,
            model_override: str | None = None, api_key: str | None = None,
            scoped_models: list[str] | None = None,
            tui_mode: str | None = None) -> None:
    """启动 TUI;`initial_prompt` 非空时进界面即提交(来自 `qi "问题"`)。

    会话选择参数与 headless 路径同义:`qi -c` / `--session` / `--fork` / `-n` / `--no-session`。

    渲染模式:`--tui-mode` 压过 `settings.tuiMode`,都没给就是 `fullscreen`(qi 的默认,
    pi 的默认是 regular —— 取舍见 `docs/tui.md` §1)。
    """
    settings = None
    try:
        from .settings import load_settings

        settings = load_settings()[0]
    except Exception:  # settings 坏了不该挡住进界面
        settings = None
    setting = getattr(settings, "theme", None)
    mode = tui_mode if tui_mode in TUI_MODES else resolve_tui_mode(settings)
    _reset_mouse_reporting()
    _harden_inline_input()
    palette = resolve_theme(setting, probe=True)
    app = QiTui(initial_prompt=initial_prompt, palette=palette, session_id=session_id,
                cont=cont, fork_id=fork_id, no_session=no_session, name=name,
                approve_project=approve_project,
                extension_flags=extension_flags,
                extra_extension_paths=extra_extension_paths,
                tools=tools, exclude_tools=exclude_tools,
                no_tools=no_tools, no_builtin_tools=no_builtin_tools,
                append_system_prompt=append_system_prompt,
                resume=resume, exact_session_id=exact_session_id,
                base_prompt_override=base_prompt_override,
                no_extensions=no_extensions, no_context_files=no_context_files,
                session_dir_path=session_dir_path, model_override=model_override,
                api_key=api_key, scoped_models=scoped_models, tui_mode=mode)
    if app._fullscreen:
        # fullscreen:进备用屏(qi 拥有视口)。**鼠标开着**:滚轮/拖动喂给 qi 自己的
        # transcript,而不是终端回滚缓冲 —— 这正是“滚动只在本界面内”的来源。
        app.run()
        return
    # regular(inline):不占全屏、不进备用屏,把滚动交给终端。
    # mouse=False:qi 的界面没有必须的鼠标交互,而上报鼠标会让不支持 SGR(1006) 的终端
    # 退回旧式 X10 报文 —— 正是 `_harden_inline_input` 里那类崩溃的来源;关掉还顺带把
    # 原生文本选择/复制还给终端。
    app.run(inline=True, inline_no_clear=True, mouse=False)
