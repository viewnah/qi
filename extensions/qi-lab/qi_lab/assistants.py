"""应用助手目录在 qi-lab 侧的唯一适配层。

真正的读写在 `qi_assistants.catalog`（独立扩展）；这里只做三件事：

1. **可选依赖**：没装扩展时给出人能看懂的 503，而不是 ImportError 冒到 500；
2. **错误映射**：`CatalogError.status` 就是 HTTP 状态码；
3. **视图转换**：把 `Catalog` 收成 `{path, exists, assistants}`。

所有函数都是同步的（文件 IO 很小），路由里用 `asyncio.to_thread` 包一层，
与 `/api/kb/*` 的口径一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class AssistantError(Exception):
    """目录层错误。`status` 是要回给 HTTP 的状态码。"""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


def probe() -> tuple[bool, str]:
    """`(装没装, 原因)`。只 import，不读文件、不发请求。"""
    try:
        import qi_assistants.catalog  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def _catalog():
    try:
        from qi_assistants import catalog
    except Exception as exc:  # noqa: BLE001
        raise AssistantError(
            "没装 qi-assistants 扩展，应用助手目录不可用"
            f"（{type(exc).__name__}: {exc}）",
            status=503,
        ) from exc
    return catalog


def _view(data) -> dict[str, Any]:
    return {
        "path": str(data.path),
        "exists": data.exists,
        "assistants": [assistant.to_dict() for assistant in data.assistants],
    }


def _call(fn, /, *args, **kwargs):
    catalog = _catalog()
    try:
        return fn(*args, **kwargs)
    except catalog.CatalogError as exc:
        raise AssistantError(exc.detail, exc.status) from exc


def read(cwd: Path | str | None) -> dict[str, Any]:
    return _view(_call(_catalog().load, cwd))


def create(cwd: Path | str | None, payload: dict) -> dict[str, Any]:
    return _view(_call(_catalog().create, cwd, payload))


def update(cwd: Path | str | None, assistant_id: str, patch: dict) -> dict[str, Any]:
    return _view(_call(_catalog().update, cwd, assistant_id, patch))


def delete(cwd: Path | str | None, assistant_id: str) -> dict[str, Any]:
    return _view(_call(_catalog().delete, cwd, assistant_id))


def import_seed(cwd: Path | str | None, mode: str = "merge") -> dict[str, Any]:
    catalog = _catalog()
    try:
        data, created, skipped = catalog.import_seed(cwd, mode=mode)
    except catalog.CatalogError as exc:
        raise AssistantError(exc.detail, exc.status) from exc
    view = _view(data)
    view["created"] = created
    view["skipped"] = skipped
    return view
