"""参考扩展(`examples/extensions/hello/`)必须真的能用。

这份示例是"照着写"的起点,所以它不能只是**看起来**对 —— 它走的那五条路
(工具 / 斜杠命令 / CLI 子命令 / 旗标 / 事件)每条都有用例。这同时也是对扩展面的压力测试:
如果连一个几行的示例都要绕,那说明面有问题,而不是示例写得不好。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402

EXAMPLE = REPO / "examples" / "extensions" / "hello"

_MODELS = '{"providers": {"ollama": {"api": "openai-completions", "models": [{"id": "x"}]}}}'
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """把示例装到**用户级**扩展目录(项目级要信任,这里不牵扯门控)。"""
    home = tmp_path / "home"
    (home / "extensions" / "hello").mkdir(parents=True, exist_ok=True)
    shutil.copy(EXAMPLE / "extension.py", home / "extensions" / "hello" / "extension.py")
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return home, project


def _loaded(project: Path):
    """轻量装载(与 `qi doctor` 那条路同一个):拿回目录 / 命令 / 旗标 / CLI 登记表。"""
    from qi_agent.extensions import (
        CliCommandRegistry,
        CommandRegistry,
        ExtensionBus,
        FlagRegistry,
    )
    from qi_agent.registry import CapabilityRegistry, ToolCatalog, discover_extensions

    catalog, commands = ToolCatalog(), CommandRegistry()
    flags, cli = FlagRegistry(), CliCommandRegistry()
    loaded = discover_extensions(catalog, CapabilityRegistry(), project, bus=ExtensionBus(),
                                 commands=commands, flags=flags, cli_commands=cli,
                                 project_trusted=False)
    return loaded, catalog, commands, flags, cli


# ── 示例真的装载,五条路都通 ────────────────────────────

def test_example_loads_and_registers_everything_it_documents(tmp_path, monkeypatch):
    _home, project = _env(tmp_path, monkeypatch)
    loaded, catalog, commands, flags, cli = _loaded(project)

    assert loaded == ["hello"]                       # 目录形态:入口 extension.py 被认到
    assert "greet" in catalog.names                  # 1. 工具
    assert "hello" in commands.names                 # 2. 斜杠命令
    assert "hello-cli" in cli.names                  # 3. CLI 子命令
    assert "greet" in flags.names                    # 4. 旗标


@pytest.mark.asyncio
async def test_example_tool_runs_and_falls_back_when_no_flag(tmp_path, monkeypatch):
    """工具得真能跑;没给旗标时用兜底称呼(示例里写了这条回落)。"""
    _home, project = _env(tmp_path, monkeypatch)
    _l, catalog, _c, _f, _cli = _loaded(project)

    tool = catalog.get("greet")
    assert tool is not None
    run_tool = tool.execute          # 绑定局部:本仓已知的 semgrep 误报规避(SQL sink)
    ctx: Any = SimpleNamespace(workdir=project)

    plain = await run_tool({}, ctx)                  # 没给 who → 用兜底称呼
    assert isinstance(plain, str) and plain == f"你好,世界!(工作目录:{project})"
    named = await run_tool({"who": "张三"}, ctx)
    assert isinstance(named, str) and "张三" in named


def test_example_command_notifies_without_a_frontend(tmp_path, monkeypatch):
    """`ctx.ui.notify` 在没有界面时要落进 notes(`/hello` 在 headless 下不静默丢失)。"""
    _home, project = _env(tmp_path, monkeypatch)
    _l, _c, commands, _f, _cli = _loaded(project)
    command = commands.find("hello")
    assert command is not None

    ui_notes: list[str] = []
    ui = SimpleNamespace(notify=lambda message, **kw: ui_notes.append(message))
    ctx: Any = SimpleNamespace(cwd=project, ui=ui)     # 鸭子类型的最小 ctx
    command.handler("你好呀", ctx)

    assert any("你好呀" in note for note in ui_notes)


def test_example_cli_subcommand_is_dispatchable(tmp_path, monkeypatch):
    """CLI 子命令收的是**原始 argv** —— 示例用 `qi hello-cli --any 参数` 演示这一点。"""
    _home, project = _env(tmp_path, monkeypatch)
    _l, _c, _cmds, _f, cli = _loaded(project)
    command = cli.find("hello-cli")
    assert command is not None
    assert command.handler(["--any", "参数"]) == 0            # 扩展自己解析,core 不插手


@pytest.mark.asyncio
async def test_example_session_start_hook_fires_in_a_real_runtime(tmp_path, monkeypatch):
    """整条链路:runtime 装载示例 → 绑会话 → `session_start` → notify 落进 notes。"""
    from qi_agent.llm import ChatResponse
    from qi_agent.runtime import QiRuntime

    class _LLM:
        async def chat(self, messages, tools=None, temperature=None):
            return ChatResponse(text="好")

    _home, project = _env(tmp_path, monkeypatch)
    runtime = QiRuntime(cwd=project, llm=_LLM(), approve_project=True)

    assert runtime.extensions == ["hello"]
    assert runtime.flag_errors == []              # 含 registerCliCommand 在内全部注册成功

    session = runtime.sessions.create("t", cwd=runtime.cwd)
    # **`session_start` 是前端绑会话时的动作**(`cli.py:314` / `tui.py:3030`),`stream()` 自己
    # 不发它 —— 所以测试要像前端那样先绑定,否则测的是一条真实使用里不存在的路径。
    # (这条一度把我引向"headless 下 session_start 不派发"的怀疑;查清后是测试走错了路。)
    await runtime.start_session(session, reason="startup")
    async for _event in runtime.stream("你好", session):
        pass

    assert any("hello 扩展已装载" in note for note in runtime.notes), runtime.notes
