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


# 会话环境变量(对齐 pi 的 `resolveSpawnContext`):被**删掉再填**,不是简单覆盖 ——
# 免得嵌在里面的子进程(子 agent 再跑 bash)继承上一层的值,那是很难查的一类串味。
SESSION_ENV_KEYS = ("QI_SESSION_ID", "QI_SESSION_FILE", "QI_PROVIDER", "QI_MODEL",
                    "QI_REASONING_LEVEL")


def session_env(session_id: str | None, session_file: str | None,
                provider: str | None, model: str | None,
                reasoning_level: str | None) -> dict[str, str]:
    """构造这一回合的会话环境变量(pi 的 `exposeSessionEnvironment`)。

    给 bash / powershell 的运行时环境:`env | grep QI_` 就能自查"当前是哪个模型、
    哪个会话"。**为什么必须走环境变量**:换模型是界面状态,不进对话上下文,
    所以工具是 agent 唯一能**自证**"现在跑的是什么"的通道(见 docs/environment-variables.md)。

    取不到的值**不设**(而不是设成空串):空串会被读成"设过了,值是空的"。
    """
    pairs = {
        "QI_SESSION_ID": session_id,
        "QI_SESSION_FILE": session_file,
        "QI_PROVIDER": provider,
        "QI_MODEL": model,
        "QI_REASONING_LEVEL": reasoning_level,
    }
    return {key: str(value) for key, value in pairs.items()
            if value is not None and str(value) != ""}


def merged_env(extra: dict[str, str] | None) -> dict[str, str]:
    """当前进程环境,先把 `SESSION_ENV_KEYS` 摘掉再叠 `extra`(pi 同款顺序)。"""
    env = dict(os.environ)
    for key in SESSION_ENV_KEYS:
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """超时要连子进程一起杀,否则 `sleep 100 &` 之类会逃逸成孤儿。"""
    if not is_windows():
        # start_new_session=True 时 shell 自己就是进程组组长
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
    proc.kill()


# 在跑的子进程组(pid → 进程)。信号处理器里必须**同步**回收,所以用模块级登记表
# (pi 的 `trackDetachedChildPids` 同形);asyncio 单线程,普通 dict 就够,不需要锁。
_LIVE_CHILDREN: dict[int, asyncio.subprocess.Process] = {}


def kill_live_children() -> None:
    """杀掉当前所有在跑的子进程组 —— 供**同步**调用者用(信号处理器不能 await)。"""
    for pid, proc in list(_LIVE_CHILDREN.items()):
        _LIVE_CHILDREN.pop(pid, None)      # 先除名:pid 可能被复用,别误杀别人
        _kill_tree(proc)


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
                    timeout: float, abort: AbortSignal | None = None,
                    env: dict[str, str] | None = None) -> ShellResult:
    """执行一条命令,返回 `(输出文本, 退出码)`。

    命令以**参数**或 **stdin** 交给解析出的 shell 二进制,不走 `shell=True`,
    所以执行器不会随平台漂移到 cmd.exe。stdin 用 DEVNULL:命令读不到 TUI 的按键。

    `abort` 置位时不等命令自己退出:立即按**进程组**回收并返回 `aborted=True`。
    没有这一步,一个 `sleep 300` 就会把 escape 拖成好几分钟的“没反应”。

    `env` 是这一回合的**会话环境变量**(`QI_MODEL` 等,见 `session_env`);不给就照旧
    继承进程环境(但会把 `SESSION_ENV_KEYS` 摘掉 —— 不许串味)。

    异常/被取消时也**不会留孤儿**:收尾在 `finally` 里做(tools 的调用者被
    `Task.cancel()`、SIGINT 让 asyncio 取消主任务,都会走到这里)。
    """
    if config.command_via_stdin:
        argv = [config.shell, *config.args]
        stdin = asyncio.subprocess.PIPE
    else:
        argv = [config.shell, *config.args, command]
        stdin = asyncio.subprocess.DEVNULL

    popen_kwargs: dict = {"env": merged_env(env)}
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

    _LIVE_CHILDREN[proc.pid] = proc
    comm = asyncio.ensure_future(proc.communicate())
    watch = asyncio.ensure_future(abort.wait()) if abort is not None else None
    try:
        waiters = {comm} if watch is None else {comm, watch}
        done, _pending = await asyncio.wait(waiters, timeout=timeout,
                                           return_when=asyncio.FIRST_COMPLETED)
        if not done:                                    # 超时:拿不到退出码
            return ShellResult(text="", exit_code=None, timed_out=True)
        if watch is not None and watch in done and comm not in done:
            return ShellResult(text="", exit_code=None, aborted=True)   # 中断
        out, _ = comm.result()
    finally:
        # 还在跑 = 超时 / 中断 / **被取消** / 异常。不能只处理前两者:调用方的任务被
        # cancel(escape 强制终止、Ctrl-C 让 asyncio 取消主任务)时不会有人回来杀它。
        # `_kill_tree` 是同步的,所以即使下面的 await 又被取消,进程也已经死了。
        if proc.returncode is None:
            await _teardown(proc, comm)
        _LIVE_CHILDREN.pop(proc.pid, None)
        if watch is not None:
            watch.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await watch

    return ShellResult(text=out.decode("utf-8", errors="replace"), exit_code=proc.returncode)
