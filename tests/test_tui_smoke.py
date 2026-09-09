"""P9:回归集结构校验 + TUI 冒烟(stub 运行时,无网络)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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


@pytest.mark.asyncio
async def test_tui_smoke_import(tmp_path, monkeypatch):
    from qi_agent import paths

    (tmp_path / "qi_agent.toml").write_text('[models.default]\nprovider="ollama"\nmodel="x"\n')
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "qi_agent.toml"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    from qi_agent.tui import QiTui

    app = QiTui()
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app.query_one("#input").value = "/quit"
        await app.query_one("#input").action_submit()
        await pilot.pause(0.05)
    assert True  # 冒烟:能启动并退出
