"""会话导入:`POST /api/sessions/import`。

与导出配对(`GET /api/sessions/{sid}/export` 原样发 JSONL)。要钉住的是导入时
**必须改写**的几处,以及“坏文件不落盘”:

1. **新 id**:导入是新增一份,不是覆盖;也不信文件里那个可能是 `../` 的串;
2. **cwd 改成宿主固定工作目录**:`GET /api/sessions` 只列这个目录下的会话,不改写
   就会出现“导进来了但左栏看不见”;
3. **entry 的 id/parentId 原样保留**(v1 线性文件补链),历史仍可回放;
4. **坏文件 400,一个文件都不建** —— 别留下只有 header 的孤儿会话。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.workspaces import WorkspaceStore  # noqa: E402
from qi_lab import app as labapp  # noqa: E402
from qi_lab.app import create_app  # noqa: E402


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def client(project: Path, tmp_path: Path, minimal_config) -> TestClient:
    app = create_app(cwd=project, password=None, allowed_hosts=[], bind_host="127.0.0.1",
                     workspace_store=WorkspaceStore(tmp_path / "workspaces.json"))
    return TestClient(app, base_url="http://127.0.0.1")


def jsonl(entries: list[Any]) -> str:
    return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n"


def sessions_root() -> Path:
    return paths.global_home() / "sessions"


def stored() -> list[Path]:
    root = sessions_root()
    return sorted(root.glob("*.jsonl")) if root.is_dir() else []


HEADER = {
    "type": "session", "version": 2, "id": "old_id_0001", "title": "旧会话",
    "created_at": "2026-01-02T03:04:05", "cwd": "/some/other/machine/proj",
}
MESSAGES = [
    {"type": "message", "role": "user", "content": "你好",
     "id": "u1", "parentId": None},
    {"type": "message", "role": "assistant", "content": "在",
     "id": "a1", "parentId": "u1"},
]


def test_import_creates_a_new_session_with_the_host_cwd(client, project):
    """导出的 JSONL 能导回来:新 id、固定 cwd、标题与 entry 原样保留。"""
    response = client.post(
        "/api/sessions/import",
        json={"content": jsonl([HEADER, *MESSAGES])},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["id"] != HEADER["id"], "导入必须分配新 id,不能覆盖/复用文件里的 id"
    assert body["title"] == "旧会话"
    assert body["cwd"] == str(project.resolve())
    assert body["message_count"] == 2
    assert body["running"] is False

    detail = client.get(f"/api/sessions/{body['id']}").json()
    header = detail["entries"][0]
    assert header["id"] == body["id"]
    assert header["cwd"] == str(project.resolve())
    assert header["created_at"] == HEADER["created_at"]
    assert [entry.get("content") for entry in detail["entries"][1:]] == ["你好", "在"]
    assert detail["entries"][2]["parentId"] == "u1"

    listed = client.get("/api/sessions").json()["sessions"]
    assert body["id"] in [row["id"] for row in listed]


def test_import_v1_linear_file_is_migrated(client):
    """v1(没有 id/parentId)按文件顺序补链,不是把历史搞丢。"""
    header = {"type": "session", "version": 1, "id": "oldv1", "title": "线性",
              "created_at": "2026-01-02T03:04:05", "cwd": "/old"}
    content = jsonl([
        header,
        {"type": "message", "role": "user", "content": "第一句"},
        {"type": "message", "role": "assistant", "content": "第一答"},
    ])
    response = client.post("/api/sessions/import", json={"content": content})
    assert response.status_code == 201, response.text
    sid = response.json()["id"]

    entries = client.get(f"/api/sessions/{sid}").json()["entries"]
    assert entries[0]["version"] == 2
    first, second = entries[1], entries[2]
    assert first["id"] and second["id"]
    assert first["parentId"] is None
    assert second["parentId"] == first["id"]


def test_import_title_override(client):
    """`title` 非空 = 覆盖文件 header 的标题(同一个导出导多份时用得上)。"""
    response = client.post(
        "/api/sessions/import",
        json={"content": jsonl([HEADER, *MESSAGES]), "title": "导入副本"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["title"] == "导入副本"


@pytest.mark.parametrize(
    "content",
    [
        "not json\n",
        jsonl([{"type": "message", "role": "user", "content": "没有头"}]),
        jsonl([HEADER, HEADER]),
        jsonl([HEADER, "不是对象"]),
        jsonl([HEADER]) + "{坏行\n",
    ],
)
def test_bad_files_are_refused_and_nothing_is_written(client, content):
    before = stored()
    response = client.post("/api/sessions/import", json={"content": content})
    assert response.status_code == 400, response.text
    assert stored() == before, "400 时不能留下半个会话文件"


def test_empty_content_is_422(client):
    assert client.post("/api/sessions/import", json={"content": ""}).status_code == 422


def test_import_size_limit(client, monkeypatch):
    """上限是“网络输入”的防线:超了 400,而不是把内存吃满。"""
    monkeypatch.setattr(labapp, "MAX_IMPORT_BYTES", 32)
    response = client.post(
        "/api/sessions/import",
        json={"content": jsonl([HEADER, *MESSAGES])},
    )
    assert response.status_code == 400
    assert "太大" in response.json()["detail"]
