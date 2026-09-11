"""交互式提示:上下键选择 + 文本输入(样式对齐 QwenPaw 的 questionary 体验)。

TTY 下用原始终端模式实现方向键菜单,渲染形如:

    ? 选择 provider
      ❯ my-llm [✓]
        ＋ 新建 provider

选择后收敛为一行 ``? 选择 provider my-llm [✓]``。
非 TTY(管道 / 测试)退回编号输入。文本输入用 typer.prompt(行模式)。
"""

from __future__ import annotations

import os
import sys

import typer
from rich.console import Console

console = Console()

_ESC = "\x1b"
_UP = ("\x1b[A", "\x1bOA")
_DOWN = ("\x1b[B", "\x1bOB")
_ENTER = ("\r", "\n")
_QUESTION = "\x1b[36m?\x1b[0m"   # 青色 ?
_POINTER = "\x1b[36m❯\x1b[0m"    # 青色指针


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # pragma: no cover - 极端环境
        return False


def _read_key() -> str:
    """读一个按键(TTY)。方向键返回带 ESC 的转义序列。"""
    fd = sys.stdin.fileno()
    try:
        import termios
        import tty
    except ImportError:  # Windows
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            nxt = msvcrt.getwch()
            return _ESC + {"H": "A", "P": "B"}.get(nxt, nxt)
        return ch

    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1).decode("utf-8", "ignore")
        if ch == _ESC:
            nxt = os.read(fd, 1).decode("utf-8", "ignore")
            if nxt in ("[", "O"):
                nxt += os.read(fd, 1).decode("utf-8", "ignore")
            return ch + nxt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _clear_lines(count: int) -> None:
    sys.stdout.write(f"\x1b[{count}A")
    for _ in range(count):
        sys.stdout.write("\r\x1b[2K\n")
    sys.stdout.write(f"\x1b[{count}A")
    sys.stdout.flush()


def select(message: str, options: list[str], default: int = 0) -> int:
    """上下键选择;返回选中索引。样式对齐 questionary。"""
    if not options:
        raise ValueError("options 不能为空")
    default = max(0, min(default, len(options) - 1))
    if not _is_tty():
        return _numbered(message, options, default)

    idx = default
    n = len(options)
    sys.stdout.write(f"{_QUESTION} {message}\n")

    def render() -> None:
        for i, opt in enumerate(options):
            if i == idx:
                sys.stdout.write(f"\r\x1b[2K  {_POINTER} {opt}\n")
            else:
                sys.stdout.write(f"\r\x1b[2K    {opt}\n")
        sys.stdout.flush()

    render()
    while True:
        key = _read_key()
        if key in _UP or key == "k":
            idx = (idx - 1) % n
        elif key in _DOWN or key == "j":
            idx = (idx + 1) % n
        elif key in _ENTER:
            _clear_lines(n + 1)
            sys.stdout.write(f"{_QUESTION} {message} \x1b[36m{options[idx]}\x1b[0m\n")
            sys.stdout.flush()
            return idx
        elif key in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
            raise KeyboardInterrupt
        else:
            continue
        sys.stdout.write(f"\x1b[{n}A")
        render()


def _numbered(message: str, options: list[str], default: int) -> int:
    for i, opt in enumerate(options, 1):
        pointer = "[cyan]❯[/cyan]" if i - 1 == default else " "
        console.print(f"  {pointer} [cyan]{i}[/cyan]) {opt}")
    raw = typer.prompt(message, default=str(default + 1), show_default=True).strip()
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return int(raw) - 1
    if raw in options:
        return options.index(raw)
    return default


def confirm(message: str, default: bool = True) -> bool:
    """是/否选择(上下键)。"""
    return select(message, ["是", "否"], default=0 if default else 1) == 0


def text(message: str, default: str | None = None, required: bool = False,
         suffix: str = "") -> str:
    """文本输入;required=True 时不允许空值。"""
    label = message + suffix
    while True:
        raw = typer.prompt(label, default=default or "", show_default=bool(default)).strip()
        if raw or not required:
            return raw
        console.print("[yellow]该项必填。[/yellow]")


def integer(message: str, default: int) -> int:
    """整数输入,默认值可直接回车。"""
    raw = typer.prompt(message, default=default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        console.print("[yellow]请输入整数。[/yellow]")
        return integer(message, default)
