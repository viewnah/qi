"""P-E2d:依赖契约 —— 扩展不得把宿主写进 `dependencies`(docs/extensions.md §5.5 / E11)。

pi 用 `peerDependencies` + `"*"` 表达“宿主提供、别自己打包”;Python **没有** peer 这个概念。
于是扩展一旦写 `qi-agent==0.1.0`,pip 就会在解析时把 qi 自己降级 —— **宿主被自己的扩展
踢掉**,而故障现场(某个 API 不存在)和根因(pyproject 里一行依赖)隔得很远。所以要在
装载那一刻就把这句话说出来。

**只报告不拒绝**也是刻意的:声明本身不危险(危险的是被 pip 解成一棵冲突的树),
拒载会让一个本来能跑的扩展直接不可用 —— 而用户此刻需要的是“知道并去改 pyproject”。
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.extensions import ExtensionBus  # noqa: E402
from qi_agent.registry import (  # noqa: E402
    CapabilityRegistry,
    ToolCatalog,
    discover_extensions,
    installed_host_version,
    warn_host_dependency,
)
from qi_agent.tools import register_builtin_tools  # noqa: E402


class _Dist:
    """假 distribution:只关心 `requires`。"""

    def __init__(self, requires: list[str] | None) -> None:
        self.requires = requires


def _warn(requires: list[str] | None) -> list[str]:
    out: list[str] = []
    warn_host_dependency("probe", _Dist(requires), out.append)
    return out


# ── 单元:判定与措辞 ─────────────────────────────────────

def test_reports_when_host_is_declared():
    msgs = _warn(["qi-agent==0.1.0", "pymysql>=1.1"])
    assert len(msgs) == 1
    assert "qi-agent==0.1.0" in msgs[0]
    assert "从 pyproject 里去掉" in msgs[0]           # 报告要给出可执行的动作


def test_silent_for_unrelated_dependencies():
    assert _warn(["pymysql>=1.1", "mcp>=1.26"]) == []


def test_silent_without_requirements_or_dist_or_report():
    assert _warn(None) == []
    assert _warn([]) == []
    assert _warn(["qi-agent"]) != []                 # 裸名字也算(没版本约束同样是声明)
    warn_host_dependency("probe", None, [].append)   # 没有 dist → 不炸
    warn_host_dependency("probe", _Dist(["qi-agent"]), None)   # 没有报告通道 → 不炸


def test_name_normalization_catches_spelling_variants():
    """PEP 503:`Qi.Agent` / `qi_agent` / `QI-AGENT` 都是同一个 distribution。"""
    for spelling in ("Qi.Agent>=0.1", "qi_agent", "QI-AGENT[x]>=0.1"):
        assert _warn([spelling]), f"没认出来: {spelling}"


def test_does_not_mistake_a_lookalike_package_for_the_host():
    """`qi-agent-extra` 是另一个包 —— 不能因为前缀相同就误报。"""
    assert _warn(["qi-agent-extra>=1.0", "qi-agents"]) == []


def test_reports_version_mismatch_explicitly():
    """版本对不上时要说清“不满足”,而不只是把两个字符串摆在一起。"""
    msgs = _warn(["qi-agent>=999.0"])
    assert len(msgs) == 1
    assert "999.0" in msgs[0]
    if installed_host_version() is not None:
        assert "不满足" in msgs[0]


# ── 集成:装载时走到报告通道,而且**不拒绝装载** ──────────

def _fake_entry_point(requires: list[str] | None, name: str = "from-pip"):
    module = SimpleNamespace(register=lambda api: None)
    return SimpleNamespace(name=name, load=lambda: module, dist=_Dist(requires))


@pytest.fixture
def entry_point_channel(monkeypatch):
    """把 entry point 通道替成一条假记录(目录通道不受影响)。"""
    def install(requires: list[str] | None, name: str = "from-pip"):
        records = [_fake_entry_point(requires, name)]
        monkeypatch.setattr(importlib.metadata, "entry_points",
                            lambda *, group=None, **kw: records if group == "qi.extensions" else [])
    return install


def _catalog() -> tuple[ToolCatalog, CapabilityRegistry]:
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    return catalog, CapabilityRegistry()


def test_discovery_warns_but_still_loads(tmp_path, monkeypatch, entry_point_channel):
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    entry_point_channel(["qi-agent==0.1.0"])
    warnings: list[str] = []

    catalog, caps = _catalog()
    loaded = discover_extensions(catalog, caps, tmp_path, bus=ExtensionBus(),
                                project_trusted=False, on_warning=warnings.append)

    assert loaded == ["from-pip"]                    # **装上了**
    assert len(warnings) == 1 and "from-pip" in warnings[0]


def test_discovery_is_quiet_for_a_clean_extension(tmp_path, monkeypatch, entry_point_channel):
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    entry_point_channel(["pymysql>=1.1"])
    warnings: list[str] = []

    catalog, caps = _catalog()
    discover_extensions(catalog, caps, tmp_path, bus=ExtensionBus(),
                        project_trusted=False, on_warning=warnings.append)
    assert warnings == []


@pytest.mark.asyncio
async def test_runtime_surfaces_the_warning_in_notes(tmp_path, monkeypatch, entry_point_channel):
    """到了 runtime 一级,报告要落进 `notes`(界面上看得见)—— 否则等于没报。

    目录通道的扩展**没有** dist 元数据,所以这条检查只对 pip 通道生效
    (目录通道的 PEP 723 声明解析是 P-E6)。
    """
    import json

    (tmp_path / "models.json").write_text(json.dumps({
        "providers": {"ollama": {"api": "openai-completions",
                                 "baseUrl": "http://127.0.0.1:11434/v1",
                                 "models": [{"id": "x"}]}}}), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x"}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    entry_point_channel(["qi-agent>=0.0.1"])

    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=tmp_path)
    assert runtime.extensions == ["from-pip"]
    assert any("qi-agent" in n and "dependencies" in n for n in runtime.notes)
