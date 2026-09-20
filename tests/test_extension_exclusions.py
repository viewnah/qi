"""`settings.extensions[]` 的**排除项**(`!pat` / `-path`)必须真的生效。

这与技能是同一套机制(`split_resource_values` + `settings_exclude_paths`,语法照 pi),
但扩展这边一直**只接了一半**:排除项被解析出来、然后被丢掉 —— 于是

    "extensions": ["-~/.qi/agent/extensions/foo"]

是个静默空操作(扩展照旧加载)。这个文件钉住它真的生效,并且:

* 排除项作用于**整个发现集** —— 内建目录(`~/.qi/agent/extensions/`)里扫出来的也能关;
* **`-e` 显式给的不过滤** —— 显式意图胜过配置;
* 关掉的是**一个**扩展,同一个父目录下的其它扩展不受影响。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.extensions import ExtensionBus  # noqa: E402
from qi_agent.registry import CapabilityRegistry, ToolCatalog, discover_extensions  # noqa: E402
from qi_agent.settings import SettingsError  # noqa: E402

_EXT = """
from qi_agent.extensions import Tool


def _run(args, ctx):
    return "ok"


def register(api):
    api.registerTool(Tool({name!r}, "探针", {{"type": "object", "properties": {{}}}}, _run))
"""


def _env(tmp_path: Path, monkeypatch, packages: list[str] | None = None) -> Path:
    home = tmp_path / "home"
    (home / paths.EXTENSIONS_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (home / paths.SETTINGS_FILE_NAME).write_text(
        json.dumps({"extensions": packages or []}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    return home


def _install(root: Path, name: str) -> Path:
    d = root / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "extension.py").write_text(_EXT.format(name=name), encoding="utf-8")
    return d


def _load(cwd: Path, **kw) -> list[str]:
    return discover_extensions(ToolCatalog(), CapabilityRegistry(), cwd,
                               bus=ExtensionBus(), project_trusted=False, **kw)


def _settings(home: Path, values: list[str]) -> None:
    (home / paths.SETTINGS_FILE_NAME).write_text(
        json.dumps({"extensions": values}), encoding="utf-8")


def test_baseline_without_exclusion_both_load(tmp_path, monkeypatch):
    home = _env(tmp_path, monkeypatch)
    _install(home, "probe")
    _install(home, "other")
    assert set(_load(tmp_path / "proj")) == {"probe", "other"}


def test_dash_prefix_excludes_a_builtin_directory_extension(tmp_path, monkeypatch):
    """核心用例:内建目录里扫出来的扩展,用 `-<路径>` 关掉。"""
    home = _env(tmp_path, monkeypatch)
    probe = _install(home, "probe")
    _install(home, "other")
    _settings(home, [f"-{probe}"])

    loaded = _load(tmp_path / "proj")
    assert "probe" not in loaded, "排除项没生效(这正是修掉的静默空操作)"
    assert "other" in loaded, "不该把同目录下的其它扩展一起关掉"


def test_bang_prefix_excludes_by_glob(tmp_path, monkeypatch):
    """`!pat` 是 pi 的另一种写法(与 `-path` 等价),支持 glob。

    glob **相对该条所在的 settings.json 目录**解析(文档语义,也是 pathlib 的要求 ——
    它不支持绝对 glob 模式),所以这里写 `extensions/probe-*`。
    """
    home = _env(tmp_path, monkeypatch)
    _install(home, "probe-one")
    _install(home, "probe-two")
    _install(home, "keep")
    _settings(home, [f"!{paths.EXTENSIONS_DIR_NAME}/probe-*"])

    loaded = _load(tmp_path / "proj")
    assert loaded == ["keep"], loaded


def test_exclusion_applies_to_include_from_settings_too(tmp_path, monkeypatch):
    """纳入与排除可以同时写在数组里(pi 的用法:整体纳入、个别关掉)。"""
    home = _env(tmp_path, monkeypatch)
    outside = _install(tmp_path / "外", "probe")
    _settings(home, [str(tmp_path / "外"), f"-{outside}"])

    assert _load(tmp_path / "proj") == []


def test_explicit_extra_dir_wins_over_exclusion(tmp_path, monkeypatch):
    """`-e` 是显式意图,排除项不管它。"""
    home = _env(tmp_path, monkeypatch)
    probe = _install(home, "probe")
    _settings(home, [f"-{probe}"])

    loaded = _load(tmp_path / "proj", extra_dirs=[probe])
    assert "probe" in loaded, "显式给的路径不该被配置里的排除项干掉"


def test_broken_settings_do_not_break_discovery(tmp_path, monkeypatch):
    """settings.json 坏了在别处会报;这里不能因此把扩展发现整个搞挂。"""
    home = _env(tmp_path, monkeypatch)
    _install(home, "probe")
    (home / paths.SETTINGS_FILE_NAME).write_text("{ 坏 json", encoding="utf-8")

    assert _load(tmp_path / "proj") == ["probe"]
    with pytest.raises(SettingsError):
        from qi_agent.settings import load_settings_by_scope

        load_settings_by_scope(tmp_path / "proj")
