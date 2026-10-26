from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest

from qi_kb import auth, client, config


def jwt(exp: int | None = None, **claims) -> str:
    payload = {"username": "alice", "role": "user",
               "exp": exp or int(time.time()) + 3600, **claims}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{raw}.sig"


def write_kb(home: Path) -> None:
    (home / "kb.json").write_text(json.dumps({
        "providers": {
            "alice": {
                "baseUrl": "http://hik:7091",
                "apiPrefix": "/api",
                "knowledgeBases": {
                    "kb1": {"id": "kb1", "name": "测试库", "docCount": 1},
                },
            }
        }
    }, ensure_ascii=False), encoding="utf-8")


def envelope(data: dict) -> dict:
    return {"code": 200, "message": "success", "data": data}


def test_resolve_env_token(kb_home, monkeypatch):
    write_kb(kb_home)
    cfg = config.load()
    monkeypatch.setenv("QI_KB_TOKEN_ALICE", jwt())
    info = auth.resolve_access(cfg, "alice")
    assert info.source == "env:QI_KB_TOKEN_ALICE"


def test_resolve_kb_auth(kb_home):
    write_kb(kb_home)
    cfg = config.load()
    auth.AuthStore().set("alice", {"access": jwt(), "exp": int(time.time()) + 3600})
    info = auth.resolve_access(cfg, "alice")
    assert info.source == "kb-auth"


def test_client_list_search_ask(kb_home):
    write_kb(kb_home)
    cfg = config.load()
    store = auth.AuthStore()
    store.set("alice", {"access": jwt(), "exp": int(time.time()) + 3600})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/rag/kb/available"):
            return httpx.Response(200, json=envelope({"knowledge_bases": [
                {"kb_id": "kb1", "name": "测试库", "doc_count": 1, "chunk_count": 2},
            ]}))
        if request.url.path.endswith("/rag/qa/retrieval/search"):
            body = json.loads(request.content)
            assert body["top_k"] == 3
            assert body["rerank_top_k"] == 3  # 被夹到 <= top_k
            return httpx.Response(200, json=envelope({"results": [
                {"content": "片段", "score": 0.5, "doc_id": "d1", "metadata": {"filename": "a.pdf"}},
            ], "total": 1}))
        if request.url.path.endswith("/rag/qa/ask"):
            return httpx.Response(200, json=envelope({
                "answer": "答案", "sources": [{"doc_name": "a.pdf", "score": 0.1}],
                "kb_ids_used": ["kb1"],
            }))
        return httpx.Response(404, json={"message": "no"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with client.KbClient(cfg, "alice", auth_store=store, http=http) as c:
        assert c.list_available()[0]["kb_id"] == "kb1"
        data = c.search("问题", ["kb1"], top_k=3, rerank_top_k=20)
        assert data["results"][0]["content"] == "片段"
        answer = c.ask("问题", ["kb1"])
        assert answer["answer"] == "答案"
    assert any(path.endswith("/rag/kb/available") for path in seen)


def test_client_refreshes_expired_access(kb_home):
    write_kb(kb_home)
    cfg = config.load()
    store = auth.AuthStore()
    store.set("alice", {"access": jwt(int(time.time()) - 10), "refresh": "r1",
                        "exp": int(time.time()) - 10})
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/refresh"):
            return httpx.Response(200, json=envelope({
                "access_token": jwt(int(time.time()) + 3600), "refresh_token": "r2"}))
        if request.url.path.endswith("/rag/kb/available"):
            return httpx.Response(200, json=envelope({"knowledge_bases": []}))
        return httpx.Response(404, json={"message": "no"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with client.KbClient(cfg, "alice", auth_store=store, http=http) as c:
        assert c.list_available() == []
    assert calls[0].endswith("/auth/refresh")
    assert store.get("alice")["refresh"] == "r2"


def test_client_401_refreshes_once(kb_home):
    write_kb(kb_home)
    cfg = config.load()
    store = auth.AuthStore()
    store.set("alice", {"access": jwt(), "refresh": "r1", "exp": int(time.time()) + 3600})
    available_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal available_calls
        if request.url.path.endswith("/auth/refresh"):
            return httpx.Response(200, json=envelope({
                "access_token": jwt(int(time.time()) + 3600), "refresh_token": "r2"}))
        if request.url.path.endswith("/rag/kb/available"):
            available_calls += 1
            if available_calls == 1:
                return httpx.Response(401, json={"message": "无效的认证凭据"})
            return httpx.Response(200, json=envelope({"knowledge_bases": []}))
        return httpx.Response(404, json={"message": "no"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with client.KbClient(cfg, "alice", auth_store=store, http=http) as c:
        assert c.list_available() == []
    assert available_calls == 2
    assert store.get("alice")["refresh"] == "r2"


def test_client_rejects_unknown_path(kb_home):
    write_kb(kb_home)
    cfg = config.load()
    store = auth.AuthStore()
    store.set("alice", {"access": jwt(), "exp": int(time.time()) + 3600})
    http = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=envelope({}))))
    with client.KbClient(cfg, "alice", auth_store=store, http=http) as c:
        with pytest.raises(Exception):
            c._request("POST", "/rag/kb/create", payload={})


def test_client_ask_stream(kb_home):
    write_kb(kb_home)
    cfg = config.load()
    store = auth.AuthStore()
    store.set("alice", {"access": jwt(), "exp": int(time.time()) + 3600})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rag/qa/ask/stream")
        body = "\n\n".join([
            'data: {"type":"token","content":"你"}',
            'data: {"type":"token","content":"好"}',
            'data: {"type":"sources","content":[{"doc_name":"a.pdf"}]}',
            'data: {"type":"done"}',
            '',
        ])
        return httpx.Response(200, content=body.encode(),
                              headers={"Content-Type": "text/event-stream"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with client.KbClient(cfg, "alice", auth_store=store, http=http) as c:
        events = list(c.ask_stream("问题", ["kb1"]))
    assert [e["type"] for e in events] == ["token", "token", "sources", "done"]
    assert events[0]["content"] == "你"


def test_client_download(kb_home):
    write_kb(kb_home)
    cfg = config.load()
    store = auth.AuthStore()
    store.set("alice", {"access": jwt(), "exp": int(time.time()) + 3600})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rag/kb/kb1/files/f1/download")
        return httpx.Response(
            200, content=b"PDFDATA",
            headers={"Content-Type": "application/pdf",
                     "Content-Disposition": "attachment; filename*=UTF-8''a.pdf"},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with client.KbClient(cfg, "alice", auth_store=store, http=http) as c:
        data = c.download("kb1", "f1")
    assert data["content"] == b"PDFDATA"
    assert data["filename"] == "a.pdf"
    assert data["mediaType"] == "application/pdf"
