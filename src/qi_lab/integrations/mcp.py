"""`qi-lab → qi-mcp` 的唯一 import 面。"""

from __future__ import annotations

from typing import Any


def available() -> bool:
    try:
        import qi_mcp.config  # noqa: F401
    except ImportError:
        return False
    return True


def __getattr__(name: str) -> Any:
    from qi_mcp import config

    return getattr(config, name)
