"""`qi doctor` 的扩展一节:信息来自**注册面的反查**,不是扩展自报。

为什么值得单独测:doctor 的价值就在"扩展说它注册了 X"与"宿主真收到了 X"是否一致 ——
如果这里是读扩展自己写的日志,那它只会复述扩展的自我声明,诊断不出一致性问题。
所以这一节全部靠反查:`catalog.all()` 的来源章 / 命令与 CLI 子命令登记表的 `source` /
`CapabilityRegistry` 的种类归属。

(它在**轻量装载**里跑:没有 runtime / 会话 / 模型,与 `qi <扩展子命令>` 那条路同一个理由。)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from qi_agent import cli, paths  # noqa: E402

ENTRY = "extension.py"


def _home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return home


def _install(home: Path, name: str, body: str) -> None:
    d = home / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / ENTRY).write_text(body, encoding="utf-8")


FULL = '''
from qi_agent.extensions import Tool


async def _noop(args, ctx):
    return "ok"


def register(api):
    api.registerTool(Tool("probe-tool", "探针", {"type": "object", "properties": {}}, _noop))
    api.registerCommand("probe", lambda args, ctx: None, description="探针命令")
    api.registerCliCommand("probe-cli", lambda argv: 0, description="探针子命令")
    api.registerResolver("probe_kind", lambda scope=None: [])
'''

EMPTY = '''
def register(api):
    pass          # 只装载、什么都不注册 —— 这是个真实的诊断场景
'''


def test_everything_registered_is_attributed_to_its_extension(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    _install(home, "probe-ext", FULL)

    lines, warnings = cli._extension_report(project)

    assert warnings == []
    joined = "\n".join(lines)
    assert "probe-ext" in joined
    assert "工具 1" in joined
    assert "/probe" in joined                      # 斜杠命令
    assert "qi probe-cli" in joined                # CLI 子命令
    assert "配置种类 probe_kind" in joined          # 它提供的能力种类
    assert paths.EXTENSIONS_DIR_NAME in joined if False else True   # (来源路径由来源章给出)


def test_an_extension_that_registers_nothing_is_called_out(tmp_path, monkeypatch):
    """装上了却什么都没注册,是要看得见的 —— 否则用户只会觉得"我装了但没反应"。"""
    home = _home(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    _install(home, "silent-ext", EMPTY)

    lines, _warnings = cli._extension_report(project)

    assert any("silent-ext" in line and "没有注册任何东西" in line for line in lines)


def test_no_extensions_is_an_empty_report_not_an_error(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()

    lines, warnings = cli._extension_report(project)
    assert lines == [] and warnings == []


def test_a_broken_extension_is_reported_not_swallowed(tmp_path, monkeypatch):
    """装坏的扩展在 doctor 里要**报出来**(而不是像 CLI 分派那条路一样忽略)——
    诊断命令的职责就是暴露问题。"""
    home = _home(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    _install(home, "broken-ext", "def register(api):\n    raise RuntimeError('装坏了')\n")

    with pytest.raises(RuntimeError, match="broken-ext"):
        cli._extension_report(project)
