"""shell 解析与执行(对齐 pi 的 `dist/utils/shell.js` + `dist/core/tools/bash.js`)。

pi 的 `bash` 工具**不会**把命令丢给系统默认 shell:它解析一个**真正的 bash 二进制**
(settings.shellPath → Windows 上 Git Bash 的已知路径 → PATH 上的 bash → `/bin/bash` → `sh`),
而 Windows 原生命令走**另一条** `powershell` 工具。

qi v1 早期用 `asyncio.create_subprocess_shell` —— 在 Windows 上那等于 `cmd.exe`:
工具名叫 bash,跑的却是 cmd,模型按 bash 语法写 `&&` / `$(...)` / `ls -la` 会静默出错。
这里按 pi 的顺序解析真 shell,并且**从不用 `shell=True`**。

有意的差异:
- pi 手写 `where` / `which` 探测,qi 用 `shutil.which`(语义等价、跨平台更稳)。
- pi 对二进制输出还做一次控制字符清洗(`sanitizeBinaryOutput`);qi 只做 UTF-8 替换解码。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from ..abort import AbortSignal

# PowerShell 调用参数(照搬 pi):不加载 profile、非交互、绕过执行策略
POWERSHELL_ARGS = ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command")

# 让 PowerShell 用 UTF-8 输出(照搬 pi):否则中文/emoji 经管道回来会变问号
POWERSHELL_UTF8_PREFIX = "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}\n"


class ShellError(RuntimeError):
    """解析不到可用 shell —— 配置/环境问题。信息要能让人直接照做。"""


@dataclass(frozen=True)
class ShellConfig:
    shell: str
    args: tuple[str, ...]
    command_via_stdin: bool = False   # 老版 WSL 的 bash.exe 不认 -c,只能喂 stdin


@dataclass
class ShellResult:
    text: str
    exit_code: int | None = None
    timed_out: bool = False
    aborted: bool = False      # 用户中断(进程组已回收),与超时分开报


def is_windows() -> bool:
    return sys.platform == "win32"


def _is_legacy_wsl_bash(path: str) -> bool:
    """`C:\\Windows\\System32\\bash.exe`(老 WSL launcher)只认 stdin,不接受 `-c`。"""
    normalized = path.replace("/", "\\").lower()
    return normalized.startswith(("c:\\windows\\system32\\bash.exe",
                                  "c:\\windows\\sysnative\\bash.exe"))


def _bash_config(shell: str) -> ShellConfig:
    if _is_legacy_wsl_bash(shell):
        return ShellConfig(shell=shell, args=("-s",), command_via_stdin=True)
    return ShellConfig(shell=shell, args=("-c",))


def _git_bash_candidates() -> list[str]:
    """Windows 上 Git for Windows 的常见安装位置(顺序同 pi)。"""
    out: list[str] = []
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            out.append(str(Path(base) / "Git" / "bin" / "bash.exe"))
    return out


def resolve_shell_config(shell_path: str | None = None) -> ShellConfig:
    """按 pi 的顺序解析 bash。

    1. 显式 `shellPath`(settings.json)
    2. Windows:Git Bash 已知路径 → PATH 上的 `bash.exe`
    3. Unix:`/bin/bash` → PATH 上的 `bash` → 兜底 `sh`
    """
    if shell_path:
        path = Path(shell_path).expanduser()
        if not path.is_file():
            raise ShellError(f"settings.shellPath 指向的文件不存在: {path}")
        return _bash_config(str(path))

    if is_windows():
        candidates = _git_bash_candidates()
        for candidate in candidates:
            if Path(candidate).is_file():
                return _bash_config(candidate)
        found = shutil.which("bash.exe") or shutil.which("bash")
        if found:
            return _bash_config(found)
        searched = ("\n已查过 Git Bash 路径:\n" + "\n".join(f"  {c}" for c in candidates)
                    if candidates else "")
        raise ShellError(
            "找不到 bash。三种解法:\n"
            "  1. 装 Git for Windows: https://git-scm.com/download/win\n"
            "  2. 把已有的 bash(Cygwin / MSYS2 / WSL)加进 PATH\n"
            "  3. 在 settings.json 里设 shellPath" + searched
        )

    # `which` 也认绝对路径:存在且可执行才返回它 —— 于是 /bin/bash → PATH bash 一步到位
    found = shutil.which("/bin/bash") or shutil.which("bash")
    if found:
        return _bash_config(found)
    return ShellConfig(shell="sh", args=("-c",))


def resolve_powershell_config() -> ShellConfig:
    """Windows 上优先 PowerShell 7(`pwsh.exe`),退回内置 `powershell.exe`。

    非 Windows 直接报错(与 pi 的 "only available on Windows" 同义),给出可照做的替代路径。
    """
    if not is_windows():
        raise ShellError(
            f"powershell 工具仅 Windows 可用(当前平台 {sys.platform})。"
            "类 Unix 上请用 bash;确需 pwsh 就用 bash 调 `pwsh -Command ...`。"
        )
    shell = shutil.which("pwsh.exe") or shutil.which("pwsh") or shutil.which("powershell.exe")
    if shell is None:
        raise ShellError("找不到 PowerShell:装 PowerShell 7(pwsh.exe),或把 powershell.exe 加进 PATH。")
    return ShellConfig(shell=shell, args=POWERSHELL_ARGS)


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """超时要连子进程一起杀,否则 `sleep 100 &` 之类会逃逸成孤儿。"""
    if not is_windows():
        # start_new_session=True 时 shell 自己就是进程组组长
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
    proc.kill()


async def _teardown(proc: asyncio.subprocess.Process, comm: asyncio.Future) -> None:
    """超时/中断后的回收:先停读取、再按进程组杀、最后 wait。

    顺序同旧实现(取消 communicate → kill → wait):只 kill 不 wait 的话,transport
    会拖到事件循环关闭后才被 GC,触发 "Event loop is closed" 的 unraisable 异常。
    """
    if not comm.done():
        comm.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await comm
    _kill_tree(proc)
    with contextlib.suppress(Exception):
        await proc.wait()


async def run_shell(config: ShellConfig, command: str, *, cwd: Path,
                    timeout: float, abort: AbortSignal | None = None) -> ShellResult:
    """执行一条命令,返回 `(输出文本, 退出码)`。

    命令以**参数**或 **stdin** 交给解析出的 shell 二进制,不走 `shell=True`,
    所以执行器不会随平台漂移到 cmd.exe。stdin 用 DEVNULL:命令读不到 TUI 的按键。

    `abort` 置位时不等命令自己退出:立即按**进程组**回收并返回 `aborted=True`。
    没有这一步,一个 `sleep 300` 就会把 escape 拖成好几分钟的“没反应”。
    """
    if config.command_via_stdin:
        argv = [config.shell, *config.args]
        stdin = asyncio.subprocess.PIPE
    else:
        argv = [config.shell, *config.args, command]
        stdin = asyncio.subprocess.DEVNULL

    popen_kwargs: dict = {}
    if not is_windows():
        popen_kwargs["start_new_session"] = True   # 独立进程组,便于整组回收

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **popen_kwargs,
    )
    if config.command_via_stdin and proc.stdin is not None:
        proc.stdin.write(command.encode("utf-8"))
        with contextlib.suppress(Exception):
            await proc.stdin.drain()
        proc.stdin.close()

    comm = asyncio.ensure_future(proc.communicate())
    watch = asyncio.ensure_future(abort.wait()) if abort is not None else None
    try:
        waiters = {comm} if watch is None else {comm, watch}
        done, _pending = await asyncio.wait(waiters, timeout=timeout,
                                           return_when=asyncio.FIRST_COMPLETED)
        if not done:                                    # 超时:拿不到退出码
            await _teardown(proc, comm)
            return ShellResult(text="", exit_code=None, timed_out=True)
        if watch is not None and watch in done and comm not in done:
            await _teardown(proc, comm)                 # 中断:同样整组回收
            return ShellResult(text="", exit_code=None, aborted=True)
        out, _ = comm.result()
    finally:
        if watch is not None:
            watch.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await watch

    return ShellResult(text=out.decode("utf-8", errors="replace"), exit_code=proc.returncode)
