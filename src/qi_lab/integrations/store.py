"""`qi-lab → qi-store` 的唯一 import 面。"""

from __future__ import annotations

from typing import Any


def available() -> bool:
    try:
        import qi_store.service  # noqa: F401
    except ImportError:
        return False
    return True


def __getattr__(name: str) -> Any:
    from qi_store import errors, service

    if name == "service":
        return service
    if name == "errors":
        return errors
    for module in (errors, service):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(name)
