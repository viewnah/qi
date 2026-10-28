"""把检索/问答结果里的 `doc_id` 补成可下载的文件引用。

上游 ask/search 只给 `doc_id`（没有 `file_id`）；下载端点要的是
`/rag/kb/{kb_id}/files/{file_id}/download`。所以这里查一次
`GET /rag/kb/{kb_id}/documents` 建 `doc_id -> file_id/filename` 的缓存。

URL 是稳定相对路径 `/api/kb/files/<provider>/<kb_id>/<file_id>`（qi-lab 代理端点）。
不再带签名 / 过期参数，所以可以安全地落进历史消息；真正的 hikqin 凭证仍然只存在
qi-lab 进程内。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from .client import KbClient

CACHE_TTL = 300.0
_DOC_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, dict[str, str]]]] = {}


def proxy_url(provider: str, kb_id: str, file_id: str) -> str:
    return (f"/api/kb/files/{quote(str(provider), safe='')}/"
            f"{quote(str(kb_id), safe='')}/{quote(str(file_id), safe='')}")


def _doc_map(client: KbClient, kb_id: str, *, clock=time.time) -> dict[str, dict[str, str]]:
    provider = client.provider_name
    base = str(client.provider.get("baseUrl") or "")
    key = (provider, base, kb_id)
    now = clock()
    cached = _DOC_CACHE.get(key)
    if cached is not None and now - cached[0] < CACHE_TTL:
        return cached[1]
    mapping: dict[str, dict[str, str]] = {}
    try:
        for doc in client.documents(kb_id):
            doc_id = str(doc.get("doc_id") or doc.get("id") or "")
            if not doc_id:
                continue
            mapping[doc_id] = {
                "file_id": str(doc.get("file_id") or ""),
                "filename": str(doc.get("filename") or ""),
            }
    except Exception:  # noqa: BLE001 拿不到文件表不该让问答失败
        mapping = {}
    _DOC_CACHE[key] = (now, mapping)
    return mapping


def enrich(client: Any, items: list[dict[str, Any]], *,
           clock=time.time) -> list[dict[str, Any]]:
    """给 sources / search results 补 `file_id` / `filename` / `file_ref`。

    没有对应文件时保留条目，但 `file_ref=""` 并带 `link_error`。
    """
    if not items:
        return items
    by_kb: dict[str, dict[str, dict[str, str]]] = {}
    for item in items:
        kb_id = str(item.get("kb_id") or item.get("kbId") or "")
        if not kb_id or kb_id in by_kb:
            continue
        by_kb[kb_id] = _doc_map(client, kb_id, clock=clock)
    for item in items:
        kb_id = str(item.get("kb_id") or item.get("kbId") or "")
        doc_id = str(item.get("doc_id") or item.get("docId") or "")
        doc = by_kb.get(kb_id, {}).get(doc_id)
        if doc and doc.get("file_id"):
            item["file_id"] = doc["file_id"]
            item["filename"] = doc.get("filename") or item.get("doc_name") or ""
            item["file_ref"] = f"kb://{client.provider_name}/{kb_id}/{doc['file_id']}"
            item["url"] = proxy_url(client.provider_name, kb_id, doc["file_id"])
        else:
            item.setdefault("file_id", "")
            item.setdefault("filename", item.get("doc_name") or "")
            item.setdefault("file_ref", "")
            item.setdefault("url", "")
            item["link_error"] = item.get("link_error") or "源文件不存在或已清理"
    return items


def clear_cache() -> None:
    _DOC_CACHE.clear()
