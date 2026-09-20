"""entry point 的两种形态 + **不再静默跳过**。

这条是从一个真 bug 来的:三个官方扩展的 entry point 都写成 `包:register`(`qi_mcp:register`),
于是 `ep.load()` 返回的是**函数**而不是模块;而 loader 用 `getattr(module, "register", None)`
找入口,拿不到就 `continue` —— **静默跳过**。结果是"装了官方扩展却什么也不发生",
而这在测试里看不见:**之前的假 EP 都返回模块**,所以一直没暴露。

它最终是被 `qi doctor` 抓出来的(三个扩展装着,扩展一节却是空的)。
"""

from __future__ import annotations

import importlib.metadata as metadata
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from qi_agent.extensions import ExtensionBus  # noqa: E402

# 这个文件测的就是 entry point 本身 → 退出"默认不装载真扩展"的夹具
pytestmark = pytest.mark.real_extensions
from qi_agent.registry import CapabilityRegistry, ToolCatalog, discover_extensions  # noqa: E402

BODY = '''
from qi_agent.extensions import Tool


async def _noop(args, ctx):
    return "ok"


def register(api):
    api.registerTool(Tool("probe", "探针", {"type": "object", "properties": {}}, _noop))
'''


def _module_ep(tmp_path: Path, name: str = "probe"):
    """形态一:entry point 指向一个**模块**(模块里有 register)。"""
    path = tmp_path / f"{name}.py"
    path.write_text(BODY, encoding="utf-8")
    module = type(sys)(name)
    module.__file__ = str(path)
    exec(compile(BODY, str(path), "exec"), module.__dict__)
    ep = type("EP", (), {"name": name, "load": staticmethod(lambda: module),
                         "dist": None})()
    return ep


def _callable_ep(name: str = "probe"):
    """形态二(**官方扩展用的**):entry point 指向 `包:register`,返回的是函数本身。"""
    module = type(sys)(name)
    exec(compile(BODY, "<ep>", "exec"), module.__dict__)
    ep = type("EP", (), {"name": name, "load": staticmethod(lambda: module.register),
                         "dist": None})()
    return ep


def _discover(monkeypatch, eps, tmp_path):
    monkeypatch.setattr(metadata, "entry_points", lambda **kw: eps if kw.get("group") else [])
    catalog = ToolCatalog()
    names = discover_extensions(catalog, CapabilityRegistry(), tmp_path, bus=ExtensionBus(),
                                project_trusted=False)
    return names, catalog


def test_module_form_is_loaded(monkeypatch, tmp_path):
    names, catalog = _discover(monkeypatch, [_module_ep(tmp_path)], tmp_path)
    assert names == ["probe"] and "probe" in catalog.names


def test_callable_form_is_loaded(monkeypatch, tmp_path):
    """**官方扩展就是这一种** —— 修 bug 前它被静默跳过。"""
    names, catalog = _discover(monkeypatch, [_callable_ep()], tmp_path)
    assert names == ["probe"], "可调用入口被跳过了(这正是那个 bug)"
    assert "probe" in catalog.names


def test_an_entry_point_without_register_is_loud(monkeypatch, tmp_path):
    """既不是模块也不带 register → **报出来**,而不是安静地什么都不做。"""
    ep = type("EP", (), {"name": "broken", "load": staticmethod(lambda: 42), "dist": None})()
    with pytest.raises(RuntimeError, match="register"):
        _discover(monkeypatch, [ep], tmp_path)


def test_the_real_extensions_use_the_callable_form():
    """**守约定**:三个官方扩展的 entry point 必须能被 load 成可调用对象。

    这条断言的是**打包约定**本身 —— 它能提前抓住"entry point 写成了别的形状"。
    """
    available = {ep.name: ep for ep in metadata.entry_points(group="qi.extensions")}
    ours = {k: v for k, v in available.items() if k in ("agents", "mcp", "web")}
    if not ours:
        pytest.skip("三个官方扩展没装成 editable(本机未安装时跳过)")
    for name, ep in ours.items():
        loaded = ep.load()
        assert callable(loaded), f"{name} 的 entry point 加载出来的不是可调用对象:{loaded!r}"
