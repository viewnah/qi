"""把三个扩展包放进 `sys.path` —— 它们是**独立发行包**(不装进 venv),测试要能 import 源码。

放一处而不是每个测试文件各写一遍:本轮就踩到了这个坑 —— 逐文件插入时漏掉三个文件,
直接变成 collection error(而报错信息只说 "No module named 'qi_web'",不指路)。

生产环境里这三个包是经 entry point 发现的(装上去的);这里只是让**源码树**里的测试能跑。
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

for _name in ("qi-agents", "qi-mcp", "qi-web"):
    _path = REPO / "extensions" / _name
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# `src/`(宿主)也放这里,免得个别测试忘了插
_SRC = REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── 测试的确定性:默认不装载"本机真装的扩展" ──────────────────────
#
# 为什么必须这样:三个官方扩展一旦 `pip install -e` 装上(开发机上很常见),entry point 就会
# 在每个测试的 runtime 里被装载 —— 于是**测试结果取决于这台机器上装了什么**。那种漂移最难查:
# 同一条测试在你这里是绿的、在 CI 上也是绿的,只是因为它们测的不是同一件事。
#
# 要测 entry point 行为本身(谁被装载、入口形态对不对)的文件,用
# `pytestmark = pytest.mark.real_extensions` 退出这个夹具 —— 显式退出比隐式依赖清楚。

_ORIGINAL_ENTRY_POINTS = importlib.metadata.entry_points


@pytest.fixture(autouse=True)
def _no_ambient_extensions(request, monkeypatch):
    """只 stub `qi.extensions` 这一组;别的 `entry_points()` 调用(查 dist 版本等)原样放行。"""
    if request.node.get_closest_marker("real_extensions"):
        return

    def _only_what_the_test_asks_for(*, group=None, **kwargs):
        if group == "qi.extensions":
            return []
        return _ORIGINAL_ENTRY_POINTS(group=group, **kwargs) if group else _ORIGINAL_ENTRY_POINTS(**kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", _only_what_the_test_asks_for)
