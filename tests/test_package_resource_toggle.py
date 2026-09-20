"""pip 包的启停:pi 的 `packages` **对象形态**(`{"source": X, "extensions": []}`)。

目录扩展能用列表里的否定项关(`-path` / `!glob`,见 test_extension_exclusions.py),
但 **entry point 装进来的包没有路径可否定** —— pi 的表示法是对象形态里把那一类资源
写成空数组:`docs/settings.md` 的 packages 一节说 "Object form filters which resources
to load",示例里 `"extensions": []` 就是"这个包不带扩展"。

qi 落成:

* 键**缺失** = 不过滤(全部加载)—— pi 的语义,也是"没写就是都要";
* `[]` = 不贡献扩展(包可能还在贡献别的东西,所以**不算没装**);
* 值写歪了(不是列表/字符串)= 按不过滤处理 —— 不因为一个键写错就把包判死;
* 比对不受影响:它仍然"声明了且装了",`qi list` 不会报 "声明了没装"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.extensions import ExtensionBus  # noqa: E402
from qi_agent.packages import parse_declaration  # noqa: E402
from qi_agent.registry import CapabilityRegistry, ToolCatalog, discover_extensions  # noqa: E402

_EXT = """
from qi_agent.extensions import Tool


def _run(args, ctx):
    return "ok"


def register(api):
    api.registerTool(Tool("probe", "探针", {"type": "object", "properties": {}}, _run))
"""


# ── 解析(纯函数) ─────────────────────────────────────────────────


def _decl(entry, source: str = "s"):
    """`parse_declaration` 包一层:断言不是 None,省得每个用例都写一遍。"""
    decl = parse_declaration(entry, source)
    assert decl is not None, entry
    return decl


def test_object_form_with_empty_extensions_means_no_extensions():
    decl = _decl({"source": "qi-mcp", "extensions": []}, "settings:user")
    assert decl.name == "qi-mcp"
    assert decl.channel == "pip"
    assert decl.contributes_extensions is False


def test_missing_key_means_no_filter():
    """键缺失 = 不过滤(pi:没提的种类不过滤)。"""
    assert _decl({"source": "qi-mcp"}).contributes_extensions is True
    assert _decl({"source": "qi-mcp", "skills": ["a"]}).contributes_extensions is True
    assert _decl("qi-mcp").contributes_extensions is True


def test_nonempty_extensions_list_means_enabled():
    assert _decl({"source": "qi-mcp", "extensions": ["mcp"]}).contributes_extensions is True


def test_malformed_value_is_treated_as_no_filter():
    """写歪了(`extensions: 12`)不该把包判死 —— 按不过滤处理。"""
    assert _decl({"source": "qi-mcp", "extensions": 12}).contributes_extensions is True


# ── 装载(端到端) ─────────────────────────────────────────────────


def _env(tmp_path: Path, monkeypatch, packages: list) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / paths.SETTINGS_FILE_NAME).write_text(
        json.dumps({"packages": packages}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    # 目录通道的探针(证明"只关掉那个包",不是把一切都关掉)
    probe_dir = home / paths.EXTENSIONS_DIR_NAME / "probe-dir"
    probe_dir.mkdir(parents=True, exist_ok=True)
    (probe_dir / "extension.py").write_text(_EXT, encoding="utf-8")
    return home


def _fake_dist(monkeypatch, dist_name: str = "probe-dist") -> None:
    """把一个假的 pip 扩展装上(entry point 组 `qi.extensions`)。"""
    import importlib.metadata as metadata
    import types

    module = types.ModuleType(f"probe_dist_{dist_name.replace('-', '_')}")
    # 换个工具名:目录通道那个探针也叫 `probe`,而 catalog 会拒绝重复注册
    # (两边的探针同时在时,那声报错是**对的**)
    exec(_EXT.replace('"probe"', '"probe_dist"'), module.__dict__)
    dist = type("Dist", (), {"name": dist_name, "_path": "/tmp/fake"})()
    ep = type("EP", (), {"name": dist_name, "load": staticmethod(lambda: module),
                         "dist": dist})()

    def fake_entry_points(*, group=None, **kw):
        return [ep] if group == "qi.extensions" else []

    monkeypatch.setattr(metadata, "entry_points", fake_entry_points)


def _load(cwd: Path) -> list[str]:
    return discover_extensions(ToolCatalog(), CapabilityRegistry(), cwd,
                               bus=ExtensionBus(), project_trusted=False)


def test_package_contributes_by_default(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, packages=["probe-dist"])
    _fake_dist(monkeypatch)
    assert set(_load(tmp_path / "proj")) == {"probe-dist", "probe-dir"}


def test_object_form_switches_the_package_off(tmp_path, monkeypatch):
    """`extensions: []` → 这个包的 entry point 不加载;目录扩展不受影响。"""
    _env(tmp_path, monkeypatch, packages=[{"source": "probe-dist", "extensions": []}])
    _fake_dist(monkeypatch)
    loaded = _load(tmp_path / "proj")
    assert "probe-dist" not in loaded, "对象形态的空数组没生效"
    assert "probe-dir" in loaded, "不该把目录通道的扩展一起关掉"


def test_object_form_with_other_kind_still_loads_extensions(tmp_path, monkeypatch):
    """只有 `skills` 有选择时,扩展仍然加载(那份选择不属于扩展这一类)。"""
    _env(tmp_path, monkeypatch, packages=[{"source": "probe-dist", "skills": ["x"]}])
    _fake_dist(monkeypatch)
    assert "probe-dist" in _load(tmp_path / "proj")


def test_project_declaration_overrides_user(tmp_path, monkeypatch):
    """项目那份说关,就关(与 `discover_declared` 的"近者胜"一致)。"""
    home = _env(tmp_path, monkeypatch, packages=["probe-dist"])
    project_settings = tmp_path / "proj" / ".qi"
    project_settings.mkdir(parents=True, exist_ok=True)
    (project_settings / paths.SETTINGS_FILE_NAME).write_text(
        json.dumps({"packages": [{"source": "probe-dist", "extensions": []}]}),
        encoding="utf-8")
    _fake_dist(monkeypatch)

    from qi_agent.settings import load_settings_by_scope  # noqa: F401  确保 import 可用

    loaded = discover_extensions(ToolCatalog(), CapabilityRegistry(), tmp_path / "proj",
                                 bus=ExtensionBus(), project_trusted=True)
    assert "probe-dist" not in loaded
    assert (home / paths.EXTENSIONS_DIR_NAME / "probe-dir").is_dir(), "探针还在(只是没被加载)"
