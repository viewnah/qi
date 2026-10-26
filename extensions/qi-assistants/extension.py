"""目录通道的开发入口（与 qi-kb / qi-lab 同款）。

pip 通道走 `pyproject.toml` 的 entry point（`assistants = "qi_assistants:register"`）；
这一份只给把目录软链/复制到 `~/.qi/agent/extensions/qi-assistants/` 的开发场景用。
"""

from __future__ import annotations

from qi_assistants import register

__all__ = ["register"]
