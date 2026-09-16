"""shell 解析与命令执行:bash 必须是**真 bash**,powershell 是独立的 Windows 通道。

覆盖(对齐 pi 的 utils/shell.js + core/tools/bash.js):
  1. bash 解析优先级:shellPath → Windows Git Bash 已知路径 → PATH → /bin/bash → sh
  2. 老版 WSL 的 bash.exe 只能喂 stdin(不接受 -c)
  3. PowerShell:仅 Windows;pwsh.exe 优先,参数与 UTF-8 前缀照搬 pi
  4. 真执行:bash-only 语法能跑、stdin 不被继承、退出码/超时/结果文本形态与旧版一致
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from qi_agent.registry import ToolCatalog, ToolError
from qi_agent.tools import (
    ToolContext,
    _bash,
    _powershell,
    _shell_outcome,
    register_builtin_tools,
)
from qi_agent.tools import shell as shell_mod
from qi_agent.tools.shell import (
    POWERSHELL_ARGS,
    POWERSHELL_UTF8_PREFIX,
    ShellConfig,
    ShellError,
    ShellResult,
    _bash_config,
    _is_legacy_wsl_bash,
    resolve_powershell_config,
    resolve_shell_config,
)

WINDOWS = sys.platform == "win32"


def _catalog() -> ToolCatalog:
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    return catalog


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(agent_name="t", workdir=tmp_path)


# ── 1. bash 解析优先级 ───────────────────────────────────

def test_explicit_shell_path_wins(tmp_path):
    """settings.shellPath 优先于任何平台探测(对齐 pi 的第 1 步)。"""
    fake = tmp_path / "mybash"
    fake.write_text("#!/bin/sh\nexec /bin/sh \"$@\"\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    config = resolve_shell_config(str(fake))
    assert config.shell == str(fake)
    assert config.args in (("-c",), ("-s",))
    assert config.command_via_stdin is False


def test_missing_shell_path_is_an_actionable_error(tmp_path):
    """指了不存在的 shell 就报错,不静默退回默认 —— 否则用户以为自己的配置生效了。"""
    with pytest.raises(ShellError, match="shellPath"):
        resolve_shell_config(str(tmp_path / "nope"))


@pytest.mark.skipif(WINDOWS, reason="Unix 分支")
def test_unix_resolves_real_bash_not_default_shell():
    """关键契约:解析结果必须是 bash 系,而不是 sh/系统默认 shell。"""
    config = resolve_shell_config()
    assert config.shell.endswith(("bash", "sh")), config.shell
    assert config.args == ("-c",)
    if Path("/bin/bash").exists():
        assert config.shell.endswith("bash")


@pytest.mark.skipif(WINDOWS, reason="Unix 分支")
def test_unix_falls_back_to_sh_when_no_bash(monkeypatch):
    """两个 bash 都找不到才退 sh(pi 的最后一档)。"""
    monkeypatch.setattr(shell_mod.shutil, "which", lambda name: None)
    assert resolve_shell_config() == ShellConfig(shell="sh", args=("-c",))


def test_windows_prefers_git_bash_known_paths(monkeypatch, tmp_path):
    """Windows:先看 `%ProgramFiles%\\Git\\bin\\bash.exe`,再看 PATH,都没有才报错。"""
    git_bash = tmp_path / "bash.exe"
    git_bash.write_text("", encoding="utf-8")
    monkeypatch.setattr(shell_mod, "is_windows", lambda: True)
    monkeypatch.setattr(shell_mod, "_git_bash_candidates", lambda: [str(git_bash)])
    assert resolve_shell_config().shell == str(git_bash)

    # 已知路径都没有 → 退 PATH
    monkeypatch.setattr(shell_mod, "_git_bash_candidates", lambda: [])
    monkeypatch.setattr(shell_mod.shutil, "which",
                        lambda name: "C:/msys64/usr/bin/bash.exe" if "bash" in name else None)
    assert resolve_shell_config().shell == "C:/msys64/usr/bin/bash.exe"

    # 都没有 → 报错要给出可照做的三条解法
    monkeypatch.setattr(shell_mod.shutil, "which", lambda name: None)
    with pytest.raises(ShellError) as excinfo:
        resolve_shell_config()
    message = str(excinfo.value)
    assert "Git for Windows" in message and "shellPath" in message


# ── 2. 老版 WSL bash:只能喂 stdin ─────────────────────────

@pytest.mark.parametrize("path", [
    r"C:\Windows\System32\bash.exe",
    r"c:\windows\system32\bash.exe",
    "C:/Windows/System32/bash.exe",
    r"C:\Windows\SysNative\bash.exe",
])
def test_legacy_wsl_bash_detected(path):
    assert _is_legacy_wsl_bash(path) is True


@pytest.mark.parametrize("path", [
    r"C:\Program Files\Git\bin\bash.exe",
    "/bin/bash",
    "/usr/bin/bash",
])
def test_normal_bash_not_treated_as_wsl(path):
    assert _is_legacy_wsl_bash(path) is False


def test_legacy_wsl_bash_uses_stdin_transport():
    """`-s` + stdin(pi 的 commandTransport="stdin"):老 launcher 不认 `-c`。"""
    config = _bash_config(r"C:\Windows\System32\bash.exe")
    assert config.args == ("-s",)
    assert config.command_via_stdin is True

    assert _bash_config("/bin/bash") == ShellConfig(shell="/bin/bash", args=("-c",))


# ── 3. PowerShell ───────────────────────────────────────

def test_powershell_prefers_pwsh(monkeypatch):
    monkeypatch.setattr(shell_mod, "is_windows", lambda: True)
    monkeypatch.setattr(shell_mod.shutil, "which",
                        lambda name: {"pwsh.exe": "C:/pwsh.exe"}.get(name))
    config = resolve_powershell_config()
    assert config.shell == "C:/pwsh.exe"
    assert config.args == POWERSHELL_ARGS
    # 参数照搬 pi:不加载 profile、非交互、绕过执行策略、-Command
    assert config.args[:4] == ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass")
    assert config.command_via_stdin is False


def test_powershell_falls_back_to_windows_powershell(monkeypatch):
    monkeypatch.setattr(shell_mod, "is_windows", lambda: True)
    monkeypatch.setattr(shell_mod.shutil, "which",
                        lambda name: "C:/powershell.exe" if name == "powershell.exe" else None)
    assert resolve_powershell_config().shell == "C:/powershell.exe"


def test_powershell_missing_on_windows_is_actionable(monkeypatch):
    monkeypatch.setattr(shell_mod, "is_windows", lambda: True)
    monkeypatch.setattr(shell_mod.shutil, "which", lambda name: None)
    with pytest.raises(ShellError, match="PowerShell"):
        resolve_powershell_config()


@pytest.mark.skipif(WINDOWS, reason="非 Windows 分支")
def test_powershell_off_windows_says_so():
    """不在 Windows 就给一条能照做的错误(而不是假称命令跑过了)。"""
    with pytest.raises(ShellError, match="仅 Windows"):
        resolve_powershell_config()


@pytest.mark.skipif(WINDOWS, reason="非 Windows 分支")
@pytest.mark.asyncio
async def test_powershell_tool_errors_off_windows(tmp_path):
    """工具层同样不静默降级:拿到的是能照做的 ToolError(模型可读,会话不中断)。"""
    with pytest.raises(ToolError, match="仅 Windows"):
        await _powershell({"command": "Get-Location"}, _ctx(tmp_path))


# ── 4. 注册与结果形态 ────────────────────────────────────

def test_powershell_registered_with_platform_note():
    """pi 的第 8 个工具在 qi 里也在册;描述里点明仅 Windows(描述就是 prompt 里的 snippet)。"""
    catalog = _catalog()
    assert "powershell" in catalog.names
    assert "bash" in catalog.names
    tool = catalog.get("powershell")
    assert tool is not None
    assert "Windows" in tool.description
    assert "command" in tool.parameters["properties"]
    assert tool.parameters["required"] == ["command"]


def test_shell_outcome_text_is_byte_identical_to_old_format():
    """结果文本形态是契约(CLI/TUI/Web 与历史会话都按它渲染):成功、失败、超时。"""
    from qi_agent.models import TOOL_ERROR, TOOL_OK

    ok = _shell_outcome(ShellResult(text="hi\n", exit_code=0), 5.0)
    assert ok.status == TOOL_OK and ok.exit_code == 0 and ok.result == "hi\n"
    assert ok.error is None

    bad = _shell_outcome(ShellResult(text="boom\n", exit_code=2), 5.0)
    assert bad.status == TOOL_ERROR and bad.exit_code == 2
    assert bad.result == "exit=2\nboom\n"
    assert bad.error is None            # 有 exit_code 时不再重复归类

    slow = _shell_outcome(ShellResult(text="", exit_code=None, timed_out=True), 3.0)
    assert slow.status == TOOL_ERROR and slow.exit_code is None
    assert slow.error == "timeout"
    assert slow.result == "命令超时(>3s),已终止"


# ── 5. 真执行(跑到解析出的 bash 上) ───────────────────────

@pytest.mark.skipif(WINDOWS, reason="bash 语义")
@pytest.mark.asyncio
async def test_bash_actually_runs_bash(tmp_path):
    """名不虚传:bash-only 的东西必须能跑。

    旧实现走 `create_subprocess_shell`,在 Unix 上是 `/bin/sh`(dash 系):
    `${BASH_VERSION}` 会是空、`[[ ]]` 会语法错。这条测试就是钉这一点。
    """
    version = await _bash({"command": "echo ${BASH_VERSION:-sh}"}, _ctx(tmp_path))
    assert version.exit_code == 0
    assert version.result.strip() != "sh", "解析到的不是 bash"

    bracket = await _bash({"command": '[[ -n "x" ]] && echo bash-ok'}, _ctx(tmp_path))
    assert bracket.exit_code == 0
    assert "bash-ok" in bracket.result


@pytest.mark.asyncio
async def test_bash_stdin_is_not_inherited(tmp_path):
    """stdin 走 DEVNULL:命令读不到 TUI 的按键,`cat` 立刻拿到 EOF 而不是挂住。"""
    out = await _bash({"command": "cat", "timeout": 5}, _ctx(tmp_path))
    assert out.exit_code == 0
    assert out.result.strip() == ""


@pytest.mark.asyncio
async def test_bash_timeout_kills_and_tags(tmp_path):
    """超时:不给退出码,用 error 字段给机器可读原因(与旧版一致)。"""
    out = await _bash({"command": "tail -f /dev/null", "timeout": 0.3}, _ctx(tmp_path))
    assert out.error == "timeout"
    assert out.exit_code is None
    assert "命令超时" in out.result


def test_powershell_utf8_prefix_matches_pi():
    """UTF-8 前缀照搬 pi:少了它,中文/emoji 经管道回来会变问号。"""
    assert POWERSHELL_UTF8_PREFIX.startswith("try { [Console]::OutputEncoding=")
    assert POWERSHELL_UTF8_PREFIX.endswith("\n")
