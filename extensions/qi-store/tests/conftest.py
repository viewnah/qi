"""qi-store 测试的基座。

与 `extensions/qi-a2a/tests/conftest.py` 同源(精简到本扩展所需):

1. 把扩展目录与宿主 `src/` 放进 `sys.path` —— 扩展是**独立发行包**,不装进 venv;
2. 兜住 entry point,免得这台机器上真装过什么影响测试结果;
3. **每个测试都把 `QI_AGENT_HOME` 指到 tmp**,否则测试会往开发机真实的
   `~/.qi/agent/store.json` 里写凭证。这条不是洁癖:那个文件现在可能真的存着令牌。
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]      # tests/ → qi-store/ → extensions/ → 仓库根
FIXTURES = Path(__file__).resolve().parent / "fixtures"

for _rel in ("extensions/qi-store", "extensions/qi-kb", "src"):
    _path = REPO / _rel
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


_ORIGINAL_ENTRY_POINTS = importlib.metadata.entry_points


@pytest.fixture(autouse=True)
def _no_ambient_extensions(request, monkeypatch):
    """只 stub `qi.extensions` 这一组;别的 `entry_points()` 调用(查 dist 版本等)原样放行。"""
    if request.node.get_closest_marker("real_extensions"):
        return

    def fake_entry_points(**kwargs):
        group = kwargs.get("group")
        if group == "qi.extensions":
            return []
        return _ORIGINAL_ENTRY_POINTS(**kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)


@pytest.fixture(autouse=True)
def store_home(tmp_path, monkeypatch):
    """每个测试一个干净的 `~/.qi/agent`,并清掉可能干扰的环境变量。"""
    home = tmp_path / "agent"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    for name in ("QI_STORE_URL", "QI_STORE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def project(tmp_path) -> Path:
    """一个干净的项目目录(项目级配置落在 `<project>/.qi/`)。"""
    path = tmp_path / "proj"
    path.mkdir()
    return path


def fixture_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def kb_home(store_home):
    """qi-kb 的测试也会用这个名字；在 store 测试里复用它。"""
    return store_home
