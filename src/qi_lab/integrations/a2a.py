"""`qi-lab → qi-a2a` 的唯一 import 面。"""

from __future__ import annotations

from typing import Any


def available() -> bool:
    try:
        import qi_a2a.config  # noqa: F401
    except ImportError:
        return False
    return True


def __getattr__(name: str) -> Any:
    from qi_a2a import card, client, config, remote

    modules = {"config": config, "card": card, "client": client, "remote": remote}
    if name in modules:
        return modules[name]
    for module in modules.values():
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(name)
