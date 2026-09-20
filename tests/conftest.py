"""把三个扩展包放进 `sys.path` —— 它们是**独立发行包**(不装进 venv),测试要能 import 源码。

放一处而不是每个测试文件各写一遍:本轮就踩到了这个坑 —— 逐文件插入时漏掉三个文件,
直接变成 collection error(而报错信息只说 "No module named 'qi_web'",不指路)。

生产环境里这三个包是经 entry point 发现的(装上去的);这里只是让**源码树**里的测试能跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

for _name in ("qi-agents", "qi-mcp", "qi-web"):
    _path = REPO / "extensions" / _name
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# `src/`(宿主)也放这里,免得个别测试忘了插
_SRC = REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
