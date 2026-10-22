"""知识库 kind：目录来自 `/api/rag/kb/available`。

安装不是下载文档，而是把「账号 + KB 引用」写进 qi-kb 自己的 `kb.json` / `kb-auth.json`。
真正的写入委托给 qi-kb（配置格式只有一份实现）；本模块只负责投影目录。
"""

from __future__ import annotations

from ..catalog import StoreItem, content_sig
from ..client import StoreClient
from .base import PlanContext
from ..errors import UpstreamError, UsageError

KIND = "kb"


def fetch(client: StoreClient) -> list[StoreItem]:
    source = client.source
    try:
        data = client.get("/rag/kb/available")
    except UpstreamError as exc:
        # 老部署没有 RAG 路由时返回 404；当成“这一类为空”，不要让整个缓存失效。
        if getattr(exc, "status", None) == 404:
            return []
        raise
    if isinstance(data, dict):
        kbs = data.get("knowledge_bases") or data.get("items") or []
    elif isinstance(data, list):
        kbs = data
    else:
        kbs = []
    return [_item(raw, source.name) for raw in kbs if isinstance(raw, dict)]


def _item(raw: dict, server: str) -> StoreItem:
    kb_id = str(raw.get("kb_id") or raw.get("id") or "")
    name = str(raw.get("name") or kb_id)
    entry = {
        "id": kb_id,
        "name": name,
        "description": str(raw.get("description") or ""),
        "embeddingModel": str(raw.get("embedding_model") or ""),
        "visibility": str(raw.get("visibility") or "private"),
        "docCount": int(raw.get("doc_count") or 0),
        "chunkCount": int(raw.get("chunk_count") or 0),
        "updatedAt": str(raw.get("updated_at") or ""),
        "icon": str(raw.get("icon") or ""),
    }
    return StoreItem(
        id=f"kb:{kb_id}",
        kind=KIND,
        name=name,
        description=entry["description"],
        version=content_sig(kb_id, entry["name"], entry["description"],
                            entry["docCount"], entry["chunkCount"],
                            entry["updatedAt"], entry["embeddingModel"],
                            entry["visibility"]),
        updated_at=entry["updatedAt"],
        requires_config=False,
        badges={
            "docs": entry["docCount"],
            "chunks": entry["chunkCount"],
            "visibility": entry["visibility"],
            "owner": str(raw.get("owner_username") or ""),
            "embedding": entry["embeddingModel"],
        },
        meta={"kb_id": kb_id, "server": server, "entry": entry,
              "source_name": str(raw.get("name") or "")},
    )


def plan(item: StoreItem, ctx: PlanContext):
    """`kb` 的安装走 qi-kb 的配置写入器；planner 不在这里生成文件动作。"""
    raise UsageError(
        "知识库安装由 qi-kb 负责",
        hint="用 `qi kb install <id> --provider <名>`，或 `qi store install kb:<id>`",
    )
