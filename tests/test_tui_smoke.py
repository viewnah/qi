"""P9:回归集结构校验 + TUI 冒烟(stub 运行时,无网络)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_MODELS = (
    '{"providers": {"ollama": {"api": "openai-completions", '
    '"models": [{"id": "x"}]}}}'
)
# 默认模型属于 settings.json(对齐 pi)
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def test_router_cases_yaml_structure():
    import yaml

    cases = yaml.safe_load(Path(__file__).parent.joinpath("router_cases.yaml").read_text(encoding="utf-8"))
    assert isinstance(cases, list) and len(cases) >= 7
    for c in cases:
        assert "input" in c and "expect" in c
    # 样例 agent 覆盖回归集用到的名字
    agents_dir = Path(__file__).resolve().parents[1] / "examples" / "agents"
    have = {p.name for p in agents_dir.iterdir() if (p / "agent.md").exists()}
    for c in cases:
        if not str(c["expect"]).startswith("@"):
            assert c["expect"] in have, f"回归集期望 {c['expect']} 但样例里没有(have={have})"


def _tui_env(tmp_path, monkeypatch) -> None:
    """最小可启动环境:一份 models.json + 空的 QI_AGENT_HOME。"""
    from qi_agent import paths

    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))


@pytest.mark.asyncio
async def test_tui_smoke_import(tmp_path, monkeypatch):
    _tui_env(tmp_path, monkeypatch)
    from textual.widgets import Input

    from qi_agent.tui import QiTui

    app = QiTui()
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app.query_one("#input", Input).value = "/quit"
        await app.query_one("#input", Input).action_submit()
        await pilot.pause(0.05)
    assert True  # 冒烟:能启动并退出


def test_tui_quit_action_is_textual_builtin():
    """不再自定义 action_quit:复用基类,避免与 async 基类方法签名不兼容。"""
    from textual.binding import Binding

    from qi_agent.tui import QiTui

    # 基类(App)提供 action_quit,qi 不再覆盖
    assert getattr(QiTui.action_quit, "__qualname__", "").startswith("App.")
    # 且绑定表把 ctrl+c 映射到 quit
    actions: dict[str, str] = {}
    for b in QiTui.BINDINGS:
        if isinstance(b, Binding):
            actions[b.key] = b.action
        else:  # BindingType 的元组形态 (key, action, description)
            actions[b[0]] = b[1]
    assert actions.get("ctrl+c") == "quit"


@pytest.mark.asyncio
async def test_tui_ctrl_c_quits(tmp_path, monkeypatch):
    """ctrl+c → "quit" → Textual 内置 async `App.action_quit` → `self.exit()`。

    回归:qi 曾自己实现同步 `action_quit`,而 Textual 8.x 的基类方法是 async,
    覆盖签名不兼容(且属重复实现)。本测试固定「ctrl+c 仍能退出」这一行为。
    """
    _tui_env(tmp_path, monkeypatch)
    from qi_agent.tui import QiTui

    app = QiTui()
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert app._exit is False
        await pilot.press("ctrl+c")
        await pilot.pause(0.05)
        assert app._exit is True, "ctrl+c 未触发退出"
