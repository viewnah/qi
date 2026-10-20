"""qi-a2a 测试的基座:把扩展与宿主放进 `sys.path`,并兜住两件不靠纪律的事。

与 `tests/conftest.py` 同源(精简到 qi-a2a 所需),但独立一份 —— pytest 的 conftest 按
**目录**生效,测试搬进 `extensions/qi-a2a/tests/` 之后根目录那份就覆盖不到了;少了它:

1. 不 stub entry point → 本机 `pip install -e extensions/qi-a2a` 装过的话,真实扩展会被
   每个其它测试的 runtime 装上,测试结果随"这台机器装了什么"漂移;
2. 不隔离 `~/.qi` → 测试会往开发机真实用户状态里写。

本扩展自己的测试**故意**用 `monkeypatch.setenv(QI_AGENT_HOME, …)` 指定两层配置的全局目录,
那是夹具之后发生的事,所以隔离夹具照常工作。
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]   # tests/ → qi-a2a/ → extensions/ → 仓库根

# 扩展是**独立发行包**(不装进 venv),连同宿主 `src/` 一起放进 path 才能 import 源码树
for _rel in ("extensions/qi-a2a", "src"):
    _path = REPO / _rel
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


# ── 测试的确定性:默认不装载"本机真装的扩展" ──────────────────────

_ORIGINAL_ENTRY_POINTS = importlib.metadata.entry_points


@pytest.fixture(autouse=True)
def _no_ambient_extensions(request, monkeypatch):
    """只 stub `qi.extensions` 这一组;别的 `entry_points()` 调用(查 dist 版本等)原样放行。"""
    if request.node.get_closest_marker("real_extensions"):
        return

    def _only_what_the_test_asks_for(*, group=None, **kwargs):
        if group == "qi.extensions":
            return []
        return (_ORIGINAL_ENTRY_POINTS(group=group, **kwargs) if group
                else _ORIGINAL_ENTRY_POINTS(**kwargs))

    monkeypatch.setattr(importlib.metadata, "entry_points", _only_what_the_test_asks_for)


# ── 测试绝不碰真实用户状态(~/.qi)──────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path_factory, monkeypatch):
    """把名字空间根钉进临时目录 —— 任何 `~/.qi` 写入都落在那里,不会污染开发机。

    用 `QI_CONFIG_DIR` 而不是 `QI_AGENT_HOME`:后者是"显式指定、别做迁移"的语义,
    而本扩展的不少用例要**自己**指定全局 home。
    """
    root = tmp_path_factory.mktemp("qi-home")
    monkeypatch.setenv("QI_CONFIG_DIR", str(root))
    monkeypatch.delenv("QI_AGENT_HOME", raising=False)
    monkeypatch.delenv("QI_AGENT_CONFIG", raising=False)
    return root
