"""应用助手目录:`/api/assistants` 的 CRUD + 导入示例。

钉住四件事:

1. **只有项目级一层** —— 文件固定在 `<cwd>/.qi/assistants.json`；
2. **写操作都要确认头** —— 缺了是 428,不是“悄悄写进去”；
3. **读端回整份视图** —— 写端点也回同一形状,界面一次往返刷新完；
4. **URL 是外链** —— 只接受 http(s) 绝对地址。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402

pytest.importorskip("fastapi")
pytest.importorskip("qi_assistants")
from fastapi.testclient import TestClient  # noqa: E402

YES = {"X-Qi-Confirm": "yes"}


def _env(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return project


def _client(project: Path) -> TestClient:
    from qi_lab.app import create_app

    app = create_app(cwd=project, password=None, allowed_hosts=[], bind_host="127.0.0.1")
    # base_url 用回环:Host 白名单默认拦 `testserver`(那是门卫,不是路由没注册)。
    return TestClient(app, base_url="http://127.0.0.1")


def _payload(**overrides) -> dict:
    body = {
        "name": "评估方案生成助手",
        "description": "意图分析、评估要素确认，输出评估方案。",
        "tasks": ["意图分析", "分部分生成"],
        "outputs": ["评估方案文档"],
        "url": "https://eval.example.com/plan",
    }
    body.update(overrides)
    return body


def _file(project: Path) -> Path:
    return project / ".qi" / "assistants.json"


def test_list_missing_file_is_empty_but_not_unavailable(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    body = _client(project).get("/api/assistants").json()
    assert body["unavailable"] is False
    assert body["exists"] is False
    assert body["assistants"] == []
    assert body["path"] == str(_file(project))


def test_capability_bit_is_on_when_extension_is_installed(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    meta = _client(project).get("/api/meta").json()
    assert meta["capabilities"]["assistant_write"] is True


def test_write_requires_confirm_header(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    response = _client(project).post("/api/assistants", json=_payload())
    assert response.status_code == 428
    assert not _file(project).exists()


def test_create_writes_project_file_and_returns_full_view(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    response = _client(project).post("/api/assistants", json=_payload(), headers=YES)
    assert response.status_code == 201

    body = response.json()
    assert body["exists"] is True
    (assistant,) = body["assistants"]
    assert assistant["id"].startswith("as_")
    assert assistant["enabled"] is True
    assert assistant["url"] == "https://eval.example.com/plan"

    raw = json.loads(_file(project).read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["assistants"][0]["id"] == assistant["id"]


def test_schema_rejects_id_and_unknown_fields(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    client = _client(project)
    with_id = client.post("/api/assistants", json=_payload(id="as_00000000"), headers=YES)
    assert with_id.status_code == 422
    with_extra = client.post("/api/assistants", json=_payload(stage="方案"), headers=YES)
    assert with_extra.status_code == 422


def test_patch_rejects_unknown_keys(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    client = _client(project)
    assistant = client.post("/api/assistants", json=_payload(), headers=YES).json()["assistants"][0]
    response = client.patch("/api/assistants",
                            json={"id": assistant["id"], "entry_url": "https://x"},
                            headers=YES)
    assert response.status_code == 422


def test_duplicate_name_is_409(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    client = _client(project)
    assert client.post("/api/assistants", json=_payload(), headers=YES).status_code == 201
    response = client.post("/api/assistants",
                           json=_payload(name="评估方案生成助手"), headers=YES)
    assert response.status_code == 409


def test_patch_only_changes_sent_keys(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    client = _client(project)
    created = client.post("/api/assistants", json=_payload(), headers=YES).json()
    assistant = created["assistants"][0]

    response = client.patch("/api/assistants",
                            json={"id": assistant["id"], "name": "改名了"}, headers=YES)
    assert response.status_code == 200
    updated = response.json()["assistants"][0]
    assert updated["name"] == "改名了"
    assert updated["description"] == assistant["description"]
    assert updated["tasks"] == assistant["tasks"]
    assert updated["id"] == assistant["id"]


def test_patch_can_clear_keywords(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    client = _client(project)
    created = client.post("/api/assistants", json=_payload(keywords=["方案"]),
                          headers=YES).json()
    assistant = created["assistants"][0]
    body = client.patch("/api/assistants",
                        json={"id": assistant["id"], "keywords": []}, headers=YES).json()
    assert body["assistants"][0]["keywords"] == []


def test_patch_can_disable_with_false(tmp_path, monkeypatch):
    """`enabled=false` 不能被 `exclude_none` 顺手丢掉 —— False 是值，不是“没送”。"""
    project = _env(tmp_path, monkeypatch)
    client = _client(project)
    assistant = client.post("/api/assistants", json=_payload(), headers=YES).json()["assistants"][0]
    body = client.patch("/api/assistants",
                        json={"id": assistant["id"], "enabled": False}, headers=YES).json()
    assert body["assistants"][0]["enabled"] is False


def test_delete_is_hard_and_404_after(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    client = _client(project)
    created = client.post("/api/assistants", json=_payload(), headers=YES).json()
    assistant = created["assistants"][0]

    response = client.request("DELETE", "/api/assistants",
                              json={"id": assistant["id"]}, headers=YES)
    assert response.status_code == 200
    assert response.json()["assistants"] == []

    again = client.request("DELETE", "/api/assistants",
                           json={"id": assistant["id"]}, headers=YES)
    assert again.status_code == 404


def test_unknown_id_is_404(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    response = _client(project).patch(
        "/api/assistants", json={"id": "as_deadbeef", "name": "x"}, headers=YES)
    assert response.status_code == 404


def test_url_validation_reaches_the_api(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    response = _client(project).post(
        "/api/assistants", json=_payload(url="javascript:alert(1)"), headers=YES)
    assert response.status_code == 400
    assert "http" in response.json()["detail"]


def test_import_seed_is_idempotent_and_reports_counts(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    client = _client(project)
    first = client.post("/api/assistants/import", json={}, headers=YES)
    assert first.status_code == 200
    assert first.json()["created"] == 3
    assert len(first.json()["assistants"]) == 3

    second = client.post("/api/assistants/import", json={"mode": "merge"}, headers=YES)
    assert second.json()["created"] == 0
    assert second.json()["skipped"] == 3


def test_import_replace_clears_existing(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    client = _client(project)
    client.post("/api/assistants", json=_payload(name="手工条目"), headers=YES)
    body = client.post("/api/assistants/import", json={"mode": "replace"},
                       headers=YES).json()
    names = {item["name"] for item in body["assistants"]}
    assert "手工条目" not in names
    assert len(names) == 3


def test_corrupt_file_is_503_with_index(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    path = _file(project)
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    response = _client(project).get("/api/assistants")
    assert response.status_code == 503
    assert "JSON" in response.json()["detail"]


def test_unavailable_extension_degrades_to_read_only(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    from qi_lab import assistants as assistantapi

    monkeypatch.setattr(assistantapi, "probe", lambda: (False, "no qi-assistants"))
    client = _client(project)
    body = client.get("/api/assistants").json()
    assert body["unavailable"] is True
    assert body["assistants"] == []
    assert client.get("/api/meta").json()["capabilities"]["assistant_write"] is False


def test_unavailable_extension_writes_503(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    import qi_assistants  # noqa: F401

    monkeypatch.setitem(sys.modules, "qi_assistants", None)
    response = _client(project).post("/api/assistants", json=_payload(), headers=YES)
    assert response.status_code == 503
