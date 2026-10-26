from __future__ import annotations

from qi_kb import links


class FakeClient:
    provider_name = "alice"
    provider = {"baseUrl": "http://hik:7091"}

    def __init__(self):
        self.calls = 0

    def documents(self, kb_id):
        self.calls += 1
        return [{"doc_id": "d1", "file_id": "f1", "filename": "a.pdf"}]


def test_enrich_sources_adds_file_ref_and_caches():
    links.clear_cache()
    client = FakeClient()
    items = [{"kb_id": "kb1", "doc_id": "d1", "doc_name": "a.pdf"}]
    links.enrich(client, items)
    assert items[0]["file_id"] == "f1"
    assert items[0]["file_ref"] == "kb://alice/kb1/f1"
    assert items[0]["url"] == "/api/kb/files/alice/kb1/f1"
    # 第二次命中缓存，不再查 documents
    links.enrich(client, [{"kb_id": "kb1", "doc_id": "d1"}])
    assert client.calls == 1


def test_enrich_marks_missing_file():
    links.clear_cache()
    items = [{"kb_id": "kb1", "doc_id": "missing"}]
    links.enrich(FakeClient(), items)
    assert items[0]["file_ref"] == ""
    assert items[0]["url"] == ""
    assert "源文件不存在" in items[0]["link_error"]


def test_rescore_zero_uses_retrieval_score():
    from qi_kb import service

    class SearchClient:
        def search(self, *args, **kwargs):
            return {"results": [
                {"doc_id": "d1", "score": 0.42, "metadata": {"chunk_index": 1}},
            ]}

    items = [{"doc_id": "d1", "chunk_index": 1, "score": 0.0}]
    service._rescore_zero(SearchClient(), "q", ["kb1"], items, {
        "top_k": 5, "strategy": "hybrid", "hybrid_alpha": 0.7, "rerank_top_k": 3,
    })
    assert items[0]["score"] == 0.42
