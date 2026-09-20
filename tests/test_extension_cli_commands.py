"""P-E5 ③ 的新面:扩展注册 CLI 子命令(`api.registerCliCommand`)。

这是 qi-web 搬出 core 的前置件 —— 用户选的是"core 一点不留",所以 `qi web` 这个入口必须由
扩展提供,而 core 要有一个"让扩展注册子命令"的面。

三条不变量:

1. **core 的子命令名优先** —— 扩展撞名要被看见,不能悄悄接管;
2. **分派在 typer 之前**(E19:typer 的选项表是静态的,动态加会踩坑)—— 所以这里测的是
   `_dispatch_extension_command`,不是 typer 的行为;
3. **未信任的项目目录不扫**(§5.3):CLI 子命令会在 typer 之前执行扩展代码,而项目目录是
   仓库控制的 —— 这层要 fail-closed 并有用例钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from qi_agent import cli, paths  # noqa: E402
from qi_agent.extensions import (  # noqa: E402
    CliCommandRegistry,
    ExtensionApi,
    ExtensionBus,
)
from qi_agent.registry import ToolCatalog  # noqa: E402

ENTRY = "extension.py"          # E9:一个目录一个扩展,固定入口


def _api(registry: CliCommandRegistry | None, name: str = "probe") -> ExtensionApi:
    return ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(), _name=name,
                        _cli_commands=registry)


def _install(root: Path, name: str, body: str) -> None:
    d = root / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / ENTRY).write_text(body, encoding="utf-8")


# ── 注册面 ──────────────────────────────────────────────

def test_register_and_find():
    registry = CliCommandRegistry()
    calls: list[list[str]] = []
    _api(registry, "qi-web").registerCliCommand(
        "web", lambda argv: calls.append(argv) or 0, description="起 web 界面")

    command = registry.find("web")
    assert command is not None
    assert command.description == "起 web 界面"
    assert command.source == "qi-web"                    # 诊断要指得到人
    assert registry.names == ["web"]
    assert command.handler(["--port", "30142"]) == 0
    assert calls == [["--port", "30142"]]                # 收**原始 argv**,不解析


def test_duplicate_name_is_an_error_not_a_silent_drop():
    """CLI 名字是用户敲的第一个词,`qi web:1` 没法用 —— 所以撞名是错误而不是排队。"""
    registry = CliCommandRegistry()
    _api(registry, "first").registerCliCommand("web", lambda argv: 0)
    with pytest.raises(RuntimeError, match="已被占用"):
        _api(registry, "second").registerCliCommand("web", lambda argv: 0)


def test_without_a_host_registry_it_raises():
    """与 `registerCommand` / `registerFlag` / `registerResolver` 同一套规则。"""
    with pytest.raises(RuntimeError, match="CLI 命令登记处"):
        _api(None).registerCliCommand("web", lambda argv: 0)


# ── 分派 ────────────────────────────────────────────────

def test_dispatch_runs_the_handler_with_the_remaining_argv(monkeypatch):
    seen: list[list[str]] = []
    registry = CliCommandRegistry()
    registry.add_command("hello", lambda argv: seen.append(argv) or 7)

    monkeypatch.setattr(cli, "_discover_cli_commands", lambda cwd=None: registry)
    with pytest.raises(SystemExit) as exc:
        cli._dispatch_extension_command(["hello", "--x", "1"])
    assert exc.value.code == 7                           # 退出码透传
    assert seen == [["--x", "1"]]                        # 命令名之后的原样 argv


def test_dispatch_returns_false_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(cli, "_discover_cli_commands", lambda cwd=None: CliCommandRegistry())
    assert cli._dispatch_extension_command(["不存在的命令"]) is False
    assert cli._dispatch_extension_command([]) is False
    assert cli._dispatch_extension_command(["--help"]) is False      # 旗标开头 → 交给 typer


def test_core_subcommand_names_win(monkeypatch):
    """core 自己的名字优先 —— 装了 qi-web 也不该把 `qi doctor` 变成别的东西。"""
    called: list[str] = []
    registry = CliCommandRegistry()
    registry.add_command("doctor", lambda argv: called.append("ext") or 0)
    monkeypatch.setattr(cli, "_discover_cli_commands", lambda cwd=None: registry)

    assert cli._dispatch_extension_command(["doctor"]) is False
    assert called == []


def test_core_subcommand_names_are_actually_known():
    """`_core_subcommand_names()` 得真的拿到东西,否则"core 优先"是空话。"""
    names = cli._core_subcommand_names()
    assert {"doctor", "models", "sessions"} <= names, f"core 子命令名没取全: {names}"


# ── 发现(轻量 + 信任门控)────────────────────────────

def _home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return home


def test_global_extension_cli_command_is_discovered(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _install(home, "qi-web-dev",
             "def register(api):\n    api.registerCliCommand('hello', lambda argv: 0)\n")

    registry = cli._discover_cli_commands(tmp_path / "proj")
    assert "hello" in registry.names


def test_project_extension_cli_command_is_NOT_discovered_when_untrusted(tmp_path, monkeypatch):
    """fail-closed:项目目录是仓库控制的,而 CLI 子命令会在 typer 之前执行它的代码。"""
    _home(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _install(project / ".qi", "from-repo",
             "def register(api):\n    api.registerCliCommand('sneaky', lambda argv: 0)\n")

    registry = cli._discover_cli_commands(project)
    assert "sneaky" not in registry.names


def test_broken_extension_does_not_kill_the_cli(tmp_path, monkeypatch, capsys):
    """一个装坏的扩展只该让 CLI 少几条命令,不该让 `qi` 完全不可用。"""
    home = _home(tmp_path, monkeypatch)
    _install(home, "broken", "def register(api):\n    raise RuntimeError('装坏了')\n")

    registry = cli._discover_cli_commands(tmp_path / "proj")
    assert registry.names == []


# ── 与 qi-web 的真实约定(qi-web 侧长这样调用)───────────

def test_the_contract_qi_web_will_use(tmp_path, monkeypatch):
    """把 qi-web 将要写的那两行钉住:注册 `web` + 自己解析 `--port`。"""
    home = _home(tmp_path, monkeypatch)
    _install(home, "qi-web", (
        "def register(api):\n"
        "    def serve(argv):\n"
        "        port = int(argv[argv.index('--port') + 1]) if '--port' in argv else 30142\n"
        "        api.events  # 宿主面可用\n"
        "        return port\n"
        "    api.registerCliCommand('web', serve, description='起 web 界面')\n"))

    registry = cli._discover_cli_commands(tmp_path / "proj")
    command = registry.find("web")
    assert command is not None
    assert command.handler(["--port", "3456"]) == 3456    # 扩展自己解析自己的参数
    assert command.handler([]) == 30142                   # 缺省也由它自己定


def _unused(_: Any) -> None:      # 保留:下面若加类型化夹具用得上
    return None
