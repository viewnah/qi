"""pi 主题移植(TUI 视觉基线)。

`themes/dark.json` / `themes/light.json` 逐字取自 pi
(`@earendil-works/pi-coding-agent` 的 `dist/modes/interactive/theme/`),
键名与语义保持一致 —— 这样 qi 的配色和 pi 是同一份数据,日后可直接 diff 上游。

对外只暴露三件事:
  - `load_palette(name)`:读调色板(vars → colors 解析)
  - `resolve_theme(setting)`:settings/env 决定 dark|light,`auto` 时探测终端背景
  - 若干格式化纯函数(footer 用),与 pi `footer.js` 的行为一致
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import select
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

THEMES_DIR = Path(__file__).resolve().parent / "themes"

THEME_CHOICES = ("dark", "light")
"""内置主题名(与 pi 的两个 JSON 一一对应)。"""

AUTO_THEME = "auto"
"""跟随终端背景(对齐 pi 的默认行为)。"""


class ThemeError(Exception):
    """主题名未知 / 调色板损坏。"""


@dataclass(frozen=True)
class Palette:
    """一份 pi 调色板(semantic key → #rrggbb)。

    `terminal_bg` 是探测到的终端背景色:Textual 的 inline 区域必须有不透明底色,
    把它设成终端背景色才能在视觉上“不填色”(pi 本身不画底色)。
    """

    name: str
    colors: dict[str, str]
    page_bg: str = "#18181e"
    terminal_bg: str | None = None

    @property
    def is_dark(self) -> bool:
        return self.name != "light"

    def hex(self, key: str) -> str:
        """取语义色;未知名直接报错(拼错颜色名时不要静默变白)。"""
        try:
            return self.colors[key]
        except KeyError as exc:  # pragma: no cover - 只在配置/代码写错时触发
            raise ThemeError(f"未知主题色: {key}") from exc

    def fg(self, key: str, text: str) -> str:
        """Rich markup 前景色片段(hex 直写,无需注册)。"""
        return f"[{self.hex(key)}]{text}[/]"

    def bg_markup(self, key: str, text: str) -> str:
        return f"[on {self.hex(key)}]{text}[/]"

    def style(self, key: str, *, bold: bool = False, italic: bool = False,
              dim: bool = False, bg: bool = False) -> str:
        """Rich style 字符串:`#rrggbb`(+on #rrggbb),可选 bold/italic/dim。"""
        color = f"on {self.hex(key)}" if bg else self.hex(key)
        parts = [color]
        if bold:
            parts.append("bold")
        if italic:
            parts.append("italic")
        if dim:
            parts.append("dim")
        return " ".join(parts)


def load_palette(name: str) -> Palette:
    """读内置调色板;`colors` 里的 var 名按 pi 规则解析成 hex。"""
    if name not in THEME_CHOICES:
        raise ThemeError(f"未知主题 {name!r};内置:{', '.join(THEME_CHOICES)}")
    path = THEMES_DIR / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise ThemeError(f"主题文件不可用: {path}") from exc
    variables: dict[str, str] = data.get("vars") or {}
    raw: dict[str, str] = data.get("colors") or {}
    # 统一小写:pi 的 JSON 里大小写混用(#2D2838 / #343541)。归一后才可预期 ——
    # 注意 Textual 的 `Color.hex` 会**原样保留**传入字符串的大小写,所以别指望它帮你归一。
    colors = {
        key: (value if value.startswith("#") else variables.get(value, value)).lower()
        for key, value in raw.items()
    }
    page_bg = ((data.get("export") or {}).get("pageBg") or "#18181e").lower()
    return Palette(name=name, colors=colors, page_bg=page_bg)


# ── 终端背景探测(OSC 11)────────────────────────────────


def _luminance(r: int, g: int, b: int) -> float:
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def parse_background_color(reply: str) -> str | None:
    """从 OSC 11 回复里取出背景色 `#rrggbb`(支持 8/16 位分量与 #RRGGBB)。"""
    match = re.search(r"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", reply)
    if match:
        channels = []
        for part in match.groups():
            value = int(part, 16)
            channels.append(value >> 8 if len(part) >= 3 else value)
        return "#{:02x}{:02x}{:02x}".format(*[min(255, c) for c in channels])
    match = re.search(r"#([0-9a-fA-F]{6})", reply)
    if match:
        return "#" + match.group(1).lower()
    return None


def parse_osc11(reply: str) -> str | None:
    """把终端的 OSC 11 回复解析成 dark|light(解析不出返回 None)。"""
    color = parse_background_color(reply)
    if color is None:
        return None
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "light" if _luminance(*rgb) > 0.5 else "dark"


def _query_osc11(timeout: float) -> str | None:
    """向终端发 OSC 11 查询并读回复(必须在 Textual 接管 stdin 之前调用)。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except (termios.error, ValueError, OSError):
        return None
    raw = copy.deepcopy(saved)
    raw[3] &= ~(termios.ICANON | termios.ECHO)
    raw[6][termios.VMIN] = 0
    raw[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        os.write(sys.stdout.fileno(), b"\x1b]11;?\x07")
        sys.stdout.flush()
        deadline = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(fd, 64)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            if "\x07" in buf or "\x1b\\" in buf:
                break
        return buf or None
    except OSError:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)


def detect_terminal_background(timeout: float = 0.15) -> str | None:
    """终端背景色 `#rrggbb`;不支持 OSC 11 / 超时返回 None。"""
    reply = _query_osc11(timeout)
    return parse_background_color(reply) if reply else None


def detect_terminal_theme(timeout: float = 0.15) -> str | None:
    """终端背景明暗(dark|light);探测失败返回 None。"""
    bg = detect_terminal_background(timeout)
    if bg is None:
        return None
    rgb = tuple(int(bg[i:i + 2], 16) for i in (1, 3, 5))
    return "light" if _luminance(*rgb) > 0.5 else "dark"


def theme_from_env() -> str | None:
    """`QI_THEME` 显式覆盖(取值 dark|light|auto)。"""
    value = (os.environ.get("QI_THEME") or "").strip().lower()
    return value or None


def resolve_theme(setting: str | None = None, *, probe: bool = True) -> Palette:
    """决定用哪份调色板。

    优先级:`QI_THEME` > `settings.theme` > 探测终端背景 > dark。
    `auto`(或未设)走探测;探测不出来对齐 pi 落 dark。探测到的背景色
    一并带在 `Palette.terminal_bg` 上(Textual inline 区域用色)。
    """
    choice = (theme_from_env() or setting or AUTO_THEME).strip().lower()
    background = detect_terminal_background() if probe else None
    detected = parse_osc11(background or "")
    if choice in THEME_CHOICES:
        palette = load_palette(choice)
    elif choice in (AUTO_THEME, "", "default", "none"):
        palette = load_palette(detected or "dark")
    else:
        # 未知名字:不静默,交给调用方决定怎么提示;这里退回 dark 更稳妥
        palette = load_palette("dark")
    return dataclasses.replace(palette, terminal_bg=background)


# ── footer 格式化(逐条对齐 pi footer.js)──────────────


def format_tokens(count: int) -> str:
    """紧凑 token 数:`1234` → `1.2k`(与 pi `formatTokens` 一致)。"""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    if count < 1_000_000:
        return f"{round(count / 1000)}k"
    if count < 10_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{round(count / 1_000_000)}M"


def shorten_home(path: str, home: str | None = None) -> str:
    """把家目录缩成 `~`(pi `formatCwdForFooter` 的最小版本)。"""
    home = home if home is not None else str(Path.home())
    if home and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def format_cwd_line(cwd: str, *, home: str | None = None, branch: str | None = None,
                    session_name: str | None = None) -> str:
    """footer 第一行:`~/repo (main) • 会话名`。"""
    text = shorten_home(cwd, home)
    if branch:
        text = f"{text} ({branch})"
    if session_name:
        text = f"{text} • {session_name}"
    return text


# ── Rich 渲染桥接(markdown / 代码高亮)──────────────


def rich_theme(palette: Palette):
    """Rich 的主题名 → pi 语义色。

    Rich 的 markdown 渲染走 `console.get_style("markdown.h1")` 这类名字,
    所以把这套名字映射到 pi 的 md* 键,词法高亮之外的 markdown 视觉才一致。
    """
    from rich.style import Style
    from rich.theme import Theme as RichTheme

    c = palette.colors
    heading = Style(color=c["mdHeading"], bold=True)
    return RichTheme(
        {
            "markdown.h1": heading,
            "markdown.h2": heading,
            "markdown.h3": heading,
            "markdown.h4": heading,
            "markdown.h5": heading,
            "markdown.h6": heading,
            "markdown.paragraph": Style(color=c["text"]),
            "markdown.item": Style(color=c["text"]),
            "markdown.item.bullet": Style(color=c["mdListBullet"]),
            "markdown.item.number": Style(color=c["mdListBullet"]),
            "markdown.hr": Style(color=c["mdHr"]),
            "markdown.link": Style(color=c["mdLink"]),
            "markdown.link_url": Style(color=c["mdLinkUrl"]),
            "markdown.block_quote": Style(color=c["mdQuote"]),
            "markdown.code": Style(color=c["mdCode"]),
            "markdown.code_block": Style(color=c["mdCodeBlock"]),
            "markdown.kbd": Style(bold=True),
        },
        inherit=True,
    )


def syntax_theme(palette: Palette):
    """pi `syntax*` 键 → pygments token 的 Rich 代码高亮主题。

    pi 用 highlight.js,qi 用 pygments;这里按**语义**对齐(注释/关键字/函数/
    字符串/数字/类型/运算符…),颜色取同一份调色板,所以两种实现看起来是一套。
    背景保持终端默认(pi 的代码块没有底色)。
    """
    from pygments.style import Style as PygStyle
    from pygments.token import (
        Comment,
        Generic,
        Keyword,
        Name,
        Number,
        Operator,
        Punctuation,
        String,
        Text as PygText,
        Token,
    )
    from rich.syntax import PygmentsSyntaxTheme

    c = palette.colors

    class QiPygmentsStyle(PygStyle):
        # None = 不画底色(pi 的代码块没有背景,只有 mdCodeBlock 前景色)。
        # pygments 上游把它注解成 str,这里用 Any 声明才能写 None。
        background_color: Any = None
        styles = {
            Token: c["text"],
            PygText: c["text"],
            Comment: c["syntaxComment"],
            Keyword: c["syntaxKeyword"],
            Name: c["text"],
            Name.Function: c["syntaxFunction"],
            Name.Builtin: c["syntaxType"],
            Name.Class: c["syntaxType"],
            Name.Variable: c["syntaxVariable"],
            Name.Attribute: c["syntaxVariable"],
            String: c["syntaxString"],
            Number: c["syntaxNumber"],
            Operator: c["syntaxOperator"],
            Punctuation: c["syntaxPunctuation"],
            Generic.Deleted: c["toolDiffRemoved"],
            Generic.Inserted: c["toolDiffAdded"],
        }

    return PygmentsSyntaxTheme(QiPygmentsStyle)


def textual_theme(palette: Palette):
    """pi 调色板 → Textual 主题。

    background 取探测到的终端背景色:Textual inline 区域总会有一个不透明底色,
    只有与终端一致才看不出“被填色”。另外把 markdown 的 md* 色挂成 CSS 变量,
    供 App.CSS 里覆盖 MarkdownFence / MarkdownH1 等组件默认样式。
    """
    from textual.theme import Theme as TuiTheme

    c = palette.colors
    return TuiTheme(
        name=f"qi-{palette.name}",
        dark=palette.is_dark,
        primary=c["accent"],
        secondary=c["border"],
        accent=c["borderAccent"],
        foreground=c["text"],
        background=palette.terminal_bg or palette.page_bg,
        success=c["success"],
        warning=c["warning"],
        error=c["error"],
        surface=c["userMessageBg"],
        panel=c["toolPendingBg"],
        variables={
            "md-code": c["mdCode"],
            "md-code-block": c["mdCodeBlock"],
            "md-code-block-border": c["mdCodeBlockBorder"],
            "md-quote": c["mdQuote"],
            "md-hr": c["mdHr"],
            "md-list-bullet": c["mdListBullet"],
            "md-link": c["mdLink"],
            "md-link-url": c["mdLinkUrl"],
            "md-heading": c["mdHeading"],
        },
    )


def git_branch(cwd: str | os.PathLike[str]) -> str | None:
    """当前 git 分支;非仓库/无 git 时 None(不打印错误)。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd), capture_output=True, text=True, timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    branch = out.stdout.strip()
    return branch if branch and branch != "HEAD" else None
