"""把源码树里的宿主与扩展放进 `sys.path`（与其他扩展的测试同款）。"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

for _rel in ("src", "extensions/qi-assistants", "extensions/qi-agents"):
    _path = REPO / _rel
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
