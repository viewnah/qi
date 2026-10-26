"""qi-assistants：应用助手目录 + 两个只读工具。

`register` 保持薄：只把工具挂上。真正的读写在 `catalog.py`，召回在 `matching.py`。
"""

from __future__ import annotations

from typing import Any

from .tools import build_recommend_tool, build_search_tool

__all__ = ["register"]


def register(api: Any) -> None:
    api.register_tool(build_search_tool())
    api.register_tool(build_recommend_tool())
