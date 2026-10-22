"""qi-kb 测试基座。

1. 把扩展目录与宿主 `src/` 放进 `sys.path`（扩展是独立发行包，不装进 venv）；
2. 每个测试把 `QI_AGENT_HOME` 指到 tmp，避免写开发机真实的 `~/.qi/agent/kb*.json`；
3. 清掉 `QI_KB_TOKEN*`，免得 CI 环境污染。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]      # tests/ → qi-kb/ → extensions/ → 仓库根
for _rel in ("extensions/qi-kb", "src"):
    _path = REPO / _rel
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


@pytest.fixture(autouse=True)
def kb_home(tmp_path, monkeypatch):
    home = tmp_path / "agent"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    monkeypatch.delenv("QI_CONFIG_DIR", raising=False)
    for name in list(__import__("os").environ):
        if name.startswith("QI_KB_TOKEN"):
            monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def project(tmp_path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path
