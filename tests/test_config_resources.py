"""`qi config` 的资源清单与开关(pi 的资源面板的数据层)。

“关”是**写声明**而不是删条目 —— 两种表示法都照 pi:

* 目录扩展 → `settings.extensions[]` 里一条否定项 `-<路径>`;
* pip 包   → `settings.packages` 里那一条换成对象形态 `{"source": X, "extensions": []}`。

所以状态是**记住的**:能区分“没配过”与“主动关了”。这个文件钉住:
列表的启用状态、开关写回哪个作用域、以及**关掉之后装载路径真的不加载**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.cli import app  # noqa: E402
from qi_agent.extensions import ExtensionBus  # noqa: E402
from qi_agent.packages import list_resources, set_resource_enabled  # noqa: E402
from qi_agent.registry import CapabilityRegistry, ToolCatalog, discover_extensions  # noqa: E402

runner = CliRunner()

_EXT = ("from qi_agent.extensions import Tool\n"
        "def _run(args, ctx):\n    return 'ok'\n"
        "def register(api):\n"
        "    api.registerTool(Tool('probe', '探针', {'type': 'object', 'properties': {}}, _run))\n")


def _env(tmp_path: Path, monkeypatch, settings: dict | None = None) -> Path:
    home = tmp_path / "home"
    (home / paths.EXTENSIONS_DIR_NAME / "probe").mkdir(parents=True, exist_ok=True)
    (home / paths.EXTENSIONS_DIR_NAME / "probe" / "extension.py").write_text(_EXT, encoding="utf-8")
    (home / paths.SETTINGS_FILE_NAME).write_text(
        json.dumps(settings or {}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    return home


def _settings(home: Path) -> dict:
    return json.loads((home / paths.SETTINGS_FILE_NAME).read_text(encoding="utf-8"))


def _loaded(cwd: Path) -> list[str]:
    return discover_extensions(ToolCatalog(), CapabilityRegistry(), cwd,
                               bus=ExtensionBus(), project_trusted=False)


# ── 清单 ─────────────────────────────────────────────────────────────


def test_lists_builtin_directory_extension_as_enabled(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    items = list_resources(tmp_path / "proj")
    probe = next(i for i in items if i.kind == "extension" and i.name == "probe")
    assert probe.enabled is True and probe.scope == "user"


def test_negation_shows_up_as_disabled(tmp_path, monkeypatch):
    home = _env(tmp_path, monkeypatch)
    probe = home / paths.EXTENSIONS_DIR_NAME / "probe"
    (home / paths.SETTINGS_FILE_NAME).write_text(
        json.dumps({"extensions": [f"-{probe}"]}), encoding="utf-8")

    item = next(i for i in list_resources(tmp_path / "proj") if i.name == "probe")
    assert item.enabled is False, "否定项没被读成“已关闭”"


def test_declared_package_is_listed_with_its_state(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, {"packages": ["qi-mcp",
                                              {"source": "qi-agents", "extensions": []}]})
    items = {i.name: i for i in list_resources(tmp_path / "proj") if i.kind == "package"}
    assert items["qi-mcp"].enabled is True
    assert items["qi-agents"].enabled is False


# ── 开关 ─────────────────────────────────────────────────────────────


def test_turning_an_extension_off_writes_a_negation(tmp_path, monkeypatch):
    home = _env(tmp_path, monkeypatch)
    item = next(i for i in list_resources(tmp_path / "proj") if i.name == "probe")

    note = set_resource_enabled(item, False, tmp_path / "proj")
    assert "关闭" in note
    assert _settings(home)["extensions"] == [f"-{item.detail}"]
    # 关键:装载路径真的不加载了(不是只改了个数字)
    assert _loaded(tmp_path / "proj") == []


def test_turning_it_back_on_removes_the_negation(tmp_path, monkeypatch):
    home = _env(tmp_path, monkeypatch)
    item = next(i for i in list_resources(tmp_path / "proj") if i.name == "probe")
    set_resource_enabled(item, False, tmp_path / "proj")

    again = next(i for i in list_resources(tmp_path / "proj") if i.name == "probe")
    assert again.enabled is False
    set_resource_enabled(again, True, tmp_path / "proj")
    assert _settings(home)["extensions"] == []
    assert _loaded(tmp_path / "proj") == ["probe"]


def test_turning_a_package_off_writes_the_object_form(tmp_path, monkeypatch):
    home = _env(tmp_path, monkeypatch, {"packages": ["qi-mcp"]})
    item = next(i for i in list_resources(tmp_path / "proj") if i.kind == "package")

    set_resource_enabled(item, False, tmp_path / "proj")
    assert _settings(home)["packages"] == [{"source": "qi-mcp", "extensions": []}]
    # 再列一次:状态是记住的
    assert next(i for i in list_resources(tmp_path / "proj")
                if i.kind == "package").enabled is False
    # 写回“开”:回到一行声明
    set_resource_enabled(item, True, tmp_path / "proj")
    assert _settings(home)["packages"] == ["qi-mcp"]


def test_toggle_only_touches_that_scope(tmp_path, monkeypatch):
    """“项目关掉、全局还开着”必须表达得出来 —— 这是 pi 的 global/project 叠加。"""
    home = _env(tmp_path, monkeypatch)
    project_settings = tmp_path / "proj" / ".qi"
    project_settings.mkdir(parents=True, exist_ok=True)
    (project_settings / paths.SETTINGS_FILE_NAME).write_text("{}", encoding="utf-8")

    items = [i for i in list_resources(tmp_path / "proj", trusted=True)
             if i.scope == "project" and i.kind == "extension"]
    if not items:      # 项目目录里没扩展时:用 user 的那条,但写成 project 作用域
        from dataclasses import replace

        base = next(i for i in list_resources(tmp_path / "proj") if i.name == "probe")
        items = [replace(base, scope="project")]
    set_resource_enabled(items[0], False, tmp_path / "proj")

    assert _settings(home).get("extensions", []) == [], "不该动 user 那份"
    project_json = json.loads((project_settings / paths.SETTINGS_FILE_NAME).read_text("utf-8"))
    assert project_json["extensions"] == [f"-{items[0].detail}"]


# ── CLI ──────────────────────────────────────────────────────────────


def test_config_without_flags_prints_the_table(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, {"packages": ["qi-mcp"]})
    res = runner.invoke(app, ["config"])
    assert res.exit_code == 0, res.output
    assert "probe" in res.output and "qi-mcp" in res.output
    assert "启用" in res.output


def test_config_get_reads_that_scope(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, {"theme": "light"})
    res = runner.invoke(app, ["config", "--get", "theme"])
    assert res.exit_code == 0, res.output
    assert "light" in res.output


def test_config_get_missing_key_is_nonzero(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["config", "--get", "nope"])
    assert res.exit_code == 1


# ── 面板(契约层:UI 只回勾选,写声明在纯函数里) ──────────────────────


def test_apply_selection_only_writes_the_changes(tmp_path, monkeypatch):
    from qi_agent.packages import apply_resource_selection

    home = _env(tmp_path, monkeypatch)
    items = list_resources(tmp_path / "proj")
    notes = apply_resource_selection(items, set(range(len(items))), tmp_path / "proj")
    assert notes == [], "全保持原状时不该动 settings:" + str(_settings(home))

    off = {i for i in range(len(items)) if items[i].name != "probe"}
    notes = apply_resource_selection(items, off, tmp_path / "proj")
    assert len(notes) == 1 and "关闭" in notes[0]


def test_panel_contract_save_and_cancel(monkeypatch):
    """面板的契约:`ctrl+s` 交回**保持启用**的下标,`escape` 交回 None。"""
    from qi_agent.tui import ResourcePanel

    app = ResourcePanel([("a", "扩展", "user", True), ("b", "包", "user", False)])
    seen: dict = {}
    monkeypatch.setattr(app, "exit", lambda value=None: seen.update(value=value))
    monkeypatch.setattr(app, "_chosen", lambda: {1})

    app.action_save()
    assert seen["value"] == {1}
    app.action_cancel()
    assert seen["value"] is None
