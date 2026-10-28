from __future__ import annotations

import json
from pathlib import Path

import httpx

from qi_store import config, service
from qi_store.catalog import Catalog
from qi_store.client import StoreClient
from qi_store.config import Source, StoreConfig
from qi_store.kinds import kb as kbkind


def fixture() -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / "rag_kb_available.json")
                      .read_text(encoding="utf-8"))


def source() -> Source:
    return Source(
        name="hikqin",
        base_url="http://hik:7091",
        auth={"login": "alice", "role": "user", "access": "a1", "refresh": "r1",
              "exp": 9999999999},
    )


def test_fetch_projects_kb() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/rag/kb/available"
        return httpx.Response(200, json=fixture())

    client = StoreClient(source(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    items = kbkind.fetch(client)
    assert [item.id for item in items] == [
        "kb:54aa228821324031876bc06d94b538e9",
        "kb:9d80f0cecb8a43d699c36a40acf298a0",
    ]
    assert items[0].badges["docs"] == 2
    assert items[0].meta["entry"]["name"] == "智能体平台相关案例材料"


def test_install_copies_account_to_qi_kb(monkeypatch, kb_home, project):
    item = kbkind._item(fixture()["data"]["knowledge_bases"][0], "hikqin")
    src = source()
    cfg = StoreConfig(sources={"hikqin": src}, default_store="hikqin")
    catalog = Catalog("hikqin", [item])
    monkeypatch.setattr(service, "_cfg_and_source",
                        lambda cwd, store=None: (cfg, src))
    monkeypatch.setattr(service, "_catalog",
                        lambda cwd, kind=None, offline=False, store=None: (catalog, src))

    store_file = kb_home / "store.json"
    store_file.write_text('{"stores":{"hikqin":{"baseUrl":"http://hik:7091"}}}',
                          encoding="utf-8")
    before = store_file.read_text(encoding="utf-8")

    result = service.install(project, ids=[item.id], scope="global")
    assert result["installed"][0]["kind"] == "kb"

    from qi_kb import auth as kb_auth
    from qi_kb import config as kb_config
    kb_cfg = kb_config.load(project)
    assert "54aa228821324031876bc06d94b538e9" in kb_cfg.providers["hikqin"]["knowledgeBases"]
    kb_entry = kb_auth.AuthStore().get("hikqin")
    assert kb_entry is not None and kb_entry["access"] == "a1"
    assert store_file.read_text(encoding="utf-8") == before

    installed = service.installed(project)["items"]
    assert any(row["id"] == item.id and row["kind"] == "kb" for row in installed)

    uninstalled = service.uninstall(project, id=item.id)
    assert uninstalled["kind"] == "kb"
    assert kb_config.load(project).providers["hikqin"]["knowledgeBases"] == {}
