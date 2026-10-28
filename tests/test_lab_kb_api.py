"""知识库面板的 API（`/api/kb/*`）：转发、确认头、降级与错误映射。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from qi_lab.app import create_app  # noqa: E402

YES = {"X-Qi-Confirm": "yes"}


def _client(project: Path) -> TestClient:
    app = create_app(cwd=project, password=None, allowed_hosts=[], bind_host="127.0.0.1")
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def _seed_kb(project: Path) -> None:
    from qi_kb import auth, providers

    providers.add_provider(project, "alice", base_url="http://hik:7091")
    auth.AuthStore().set("alice", {
        "login": "alice", "role": "user", "access": "a1", "refresh": "r1",
        "exp": 9999999999,
    })
    providers.install_kb(project, "alice", {"id": "kb1", "name": "测试库",
                                           "docCount": 1, "chunkCount": 2})


def test_status_and_provider_add(project):
    with _client(project) as client:
        assert client.get("/api/meta").json()["capabilities"]["kb_write"] is True
        assert client.get("/api/kb/status").json()["available"] is True
        body = client.post("/api/kb/providers", headers=YES, json={
            "name": "alice", "baseUrl": "http://hik:7091",
        }).json()
        assert body["providers"][0]["name"] == "alice"


def test_provider_login_writes_auth(project, monkeypatch):
    from qi_kb import auth

    def fake_login(cfg, name, login, password, **kwargs):
        entry = {"login": login, "role": "user", "access": "a1",
                 "refresh": "r1", "exp": 9999999999}
        auth.AuthStore().set(name, entry)
        return entry

    monkeypatch.setattr(auth, "login", fake_login)
    with _client(project) as client:
        client.post("/api/kb/providers", headers=YES, json={
            "name": "alice", "baseUrl": "http://hik:7091",
        })
        body = client.post("/api/kb/providers/login", headers=YES, json={
            "name": "alice", "login": "alice", "password": "pw",
        }).json()
    assert body["providers"][0]["loggedIn"] is True


def test_provider_remove_conflict_and_cascade(project):
    _seed_kb(project)
    with _client(project) as client:
        assert client.request("DELETE", "/api/kb/providers", headers=YES,
                              json={"name": "alice"}).status_code == 409
        body = client.request("DELETE", "/api/kb/providers", headers=YES,
                              json={"name": "alice", "cascade": True}).json()
    assert body["providers"] == []


def test_available_and_bound_only(project, monkeypatch):
    _seed_kb(project)

    def fake_available(cwd=None, **kwargs):
        return {"available": True, "items": [{
            "id": "kb1", "provider": "alice", "name": "测试库",
            "docCount": 1, "chunkCount": 2, "bound": True,
        }], "total": 1, "warnings": [], "error": ""}

    from qi_kb import service
    monkeypatch.setattr(service, "available", fake_available)
    with _client(project) as client:
        body = client.get("/api/kb/available").json()
        bound = client.get("/api/kb/available?boundOnly=true").json()
    assert body["items"][0]["id"] == "kb1"
    assert bound["total"] == 1


def test_install_requires_confirm_and_conflict_maps_409(project, monkeypatch):
    _seed_kb(project)
    from qi_kb import service
    from qi_kb.errors import Conflict

    def fake_install(*args, **kwargs):
        if not kwargs.get("rebind"):
            raise Conflict("已由 alice 安装", existing_provider="alice",
                           target_provider="bob")
        return {"action": "rebind", "kbId": "kb1", "provider": "bob",
                "scope": "global", "kb": {"name": "测试库"}}

    monkeypatch.setattr(service, "install", fake_install)
    with _client(project) as client:
        assert client.post("/api/kb/install", json={
            "kbId": "kb1", "provider": "bob"}).status_code == 428
        assert client.post("/api/kb/install", headers=YES, json={
            "kbId": "kb1", "provider": "bob"}).status_code == 409
        body = client.post("/api/kb/install", headers=YES, json={
            "kbId": "kb1", "provider": "bob", "rebind": True}).json()
    assert body["action"] == "rebind"


def test_search_and_ask(project, monkeypatch):
    _seed_kb(project)
    from qi_kb import service

    monkeypatch.setattr(service, "search", lambda *a, **k: {
        "provider": "alice", "query": "问题", "results": [{"content": "片段",
        "score": 0.5, "provider": "alice"}], "total": 1, "strategy": "hybrid",
        "rerankMethod": None, "kbIds": ["kb1"], "warnings": [],
    })
    monkeypatch.setattr(service, "ask", lambda *a, **k: {
        "provider": "alice", "question": "问题", "answer": "答案",
        "sources": [], "kbIdsUsed": ["kb1"], "evidenceSufficient": False,
        "warnings": [],
    })
    with _client(project) as client:
        found = client.post("/api/kb/search", json={"query": "问题"}).json()
        asked = client.post("/api/kb/ask", json={"question": "问题"}).json()
    assert found["results"][0]["content"] == "片段"
    assert asked["answer"] == "答案"


def test_defaults_requires_confirm(project):
    with _client(project) as client:
        assert client.patch("/api/kb/defaults", json={"defaults": {"topK": 3}}
                            ).status_code == 428
        body = client.patch("/api/kb/defaults", headers=YES,
                            json={"defaults": {"topK": 3}}).json()
    assert body["defaults"]["topK"] == 3


def test_agui_kb_ids_are_parsed():
    from qi_lab import agui

    inp = agui.RunAgentInput.parse({
        "threadId": "s1",
        "messages": [{"role": "user", "content": "hi"}],
        "forwardedProps": {"kbIds": ["kb1", "kb2"], "kbProvider": "alice"},
    })
    assert inp.kb_ids == ["kb1", "kb2"]
    assert inp.kb_provider == "alice"


def test_ask_stream(project, monkeypatch):
    _seed_kb(project)
    from qi_kb import service

    def fake_stream(*args, **kwargs):
        yield {"type": "token", "content": "你", "provider": "alice"}
        yield {"type": "token", "content": "好", "provider": "alice"}
        yield {"type": "sources", "content": [{
            "index": 1, "doc_name": "a.pdf", "filename": "a.pdf",
            "file_ref": "kb://alice/kb1/file1", "provider": "alice",
        }], "provider": "alice"}
        yield {"type": "done", "provider": "alice", "kbIds": ["kb1"]}

    monkeypatch.setattr(service, "ask_stream", fake_stream)
    with _client(project) as client:
        res = client.post("/api/kb/ask/stream", json={"question": "问题"})
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]
    assert '"content": "你"' in res.text
    assert '"type": "done"' in res.text
    assert '"url":' in res.text


def test_links_are_stable_and_download_works(project, monkeypatch):
    _seed_kb(project)
    from qi_kb import service

    monkeypatch.setattr(service, "search", lambda *a, **k: {
        "provider": "alice", "query": "q", "results": [{
            "content": "片段", "score": 0.5, "provider": "alice",
            "kb_id": "kb1", "doc_id": "d1", "file_id": "file1",
            "filename": "a.pdf", "file_ref": "kb://alice/kb1/file1",
        }], "total": 1, "strategy": "hybrid", "rerankMethod": None,
        "kbIds": ["kb1"], "warnings": [],
    })
    monkeypatch.setattr(service, "download_file", lambda *a, **k: {
        "content": b"PDFDATA", "filename": "a.pdf",
        "mediaType": "application/pdf",
    })
    with _client(project) as client:
        body = client.post("/api/kb/search", json={"query": "q"}).json()
        url = body["results"][0]["url"]
        assert url == "/api/kb/files/alice/kb1/file1"
        assert "?" not in url
        assert client.get(url).content == b"PDFDATA"


def test_ask_answer_links_are_rewritten(project, monkeypatch):
    _seed_kb(project)
    from qi_kb import service

    monkeypatch.setattr(service, "ask", lambda *a, **k: {
        "provider": "alice", "question": "q", "answer": "答案 [来源1]",
        "sources": [{"index": 1, "doc_name": "a.pdf", "filename": "a.pdf",
                     "provider": "alice", "file_ref": "kb://alice/kb1/file1"}],
        "kbIdsUsed": ["kb1"], "evidenceSufficient": True, "warnings": [],
    })
    with _client(project) as client:
        body = client.post("/api/kb/ask", json={"question": "q"}).json()
    assert "[来源1](" in body["answer"]
    assert "/api/kb/files/alice/kb1/file1" in body["answer"]
