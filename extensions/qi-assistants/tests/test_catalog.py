"""assistants.json：读、校验、原子写、导入示例。

这一层的错误必须**当场报**：一份写坏的目录如果被静默跳过，推荐会变少，
但不会有任何报错 —— 那是最难查的一种。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qi_assistants import catalog


def _payload(**overrides) -> dict:
    payload = {
        "name": "评估方案生成助手",
        "description": "意图分析、评估要素确认，输出评估方案。",
        "tasks": ["意图分析", "分部分生成"],
        "outputs": ["评估方案文档"],
        "url": "https://eval.example.com/plan",
        "keywords": ["方案"],
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def _path(tmp_path: Path) -> Path:
    return tmp_path / ".qi" / "assistants.json"


def test_missing_file_is_an_empty_catalog(tmp_path):
    data = catalog.load(tmp_path)
    assert data.exists is False
    assert data.assistants == ()
    assert data.path == _path(tmp_path)


def test_create_writes_id_and_roundtrips(tmp_path):
    data = catalog.create(tmp_path, _payload())
    assert data.exists is True
    (assistant,) = data.assistants
    assert assistant.id.startswith("as_") and len(assistant.id) == 11
    assert assistant.name == "评估方案生成助手"
    assert assistant.enabled is True

    raw = json.loads(_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["version"] == catalog.SCHEMA_VERSION
    assert raw["assistants"][0]["id"] == assistant.id
    assert raw["assistants"][0]["tasks"] == ["意图分析", "分部分生成"]


def test_id_is_server_generated(tmp_path):
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.create(tmp_path, _payload(id="as_00000000"))
    assert "服务端生成" in str(exc.value)


def test_duplicate_name_is_409_even_with_different_case(tmp_path):
    catalog.create(tmp_path, _payload(name="Plan Bot"))
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.create(tmp_path, _payload(name="plan bot"))
    assert exc.value.status == 409


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "data:text/html,x",
    "file:///etc/passwd",
    "/relative/path",
    "https://user:pass@example.com/x",
    "https://example.com/a b",
    "https://example.com:bad/x",
])
def test_rejects_unsafe_urls(tmp_path, url):
    with pytest.raises(catalog.CatalogError):
        catalog.create(tmp_path, _payload(url=url))


def test_accepts_http_and_https(tmp_path):
    catalog.create(tmp_path, _payload(name="a", url="http://127.0.0.1:8080/a"))
    catalog.create(tmp_path, _payload(name="b", url="https://example.com/b?x=1#y"))


def test_tasks_must_be_an_array_not_a_string(tmp_path):
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.create(tmp_path, _payload(tasks="意图分析,分部分生成"))
    assert "必须是数组" in str(exc.value)


def test_tasks_are_trimmed_deduped_and_capped(tmp_path):
    data = catalog.create(tmp_path, _payload(tasks=[" 意图分析 ", "意图分析", "生成"]))
    assert data.assistants[0].tasks == ("意图分析", "生成")
    with pytest.raises(catalog.CatalogError):
        catalog.create(tmp_path, _payload(name="x", tasks=[f"t{i}" for i in range(9)]))


def test_unknown_fields_are_rejected(tmp_path):
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.create(tmp_path, _payload(entry_url="https://x"))
    assert "不认识的字段" in str(exc.value)


def test_update_only_changes_sent_keys(tmp_path):
    created = catalog.create(tmp_path, _payload()).assistants[0]
    data = catalog.update(tmp_path, created.id, {"name": "新名字"})
    updated = data.find(created.id)
    assert updated is not None
    assert updated.name == "新名字"
    assert updated.description == created.description
    assert updated.url == created.url
    assert updated.tasks == created.tasks


def test_update_rejects_null(tmp_path):
    created = catalog.create(tmp_path, _payload()).assistants[0]
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.update(tmp_path, created.id, {"description": None})
    assert "null" in str(exc.value)


def test_update_can_clear_keywords_with_empty_array(tmp_path):
    created = catalog.create(tmp_path, _payload()).assistants[0]
    updated = catalog.update(tmp_path, created.id, {"keywords": []}).find(created.id)
    assert updated is not None and updated.keywords == ()


def test_update_keeps_own_name_but_rejects_others(tmp_path):
    first = catalog.create(tmp_path, _payload(name="A")).assistants[0]
    second = catalog.create(tmp_path, _payload(name="B")).assistants[-1]
    assert catalog.update(tmp_path, first.id, {"name": "A"}).find(first.id) is not None
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.update(tmp_path, second.id, {"name": "A"})
    assert exc.value.status == 409


def test_delete_is_hard_delete(tmp_path):
    created = catalog.create(tmp_path, _payload()).assistants[0]
    assert catalog.delete(tmp_path, created.id).assistants == ()
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.delete(tmp_path, created.id)
    assert exc.value.status == 404


def test_import_seed_is_idempotent(tmp_path):
    data, created, skipped = catalog.import_seed(tmp_path)
    assert created == 3 and skipped == 0
    assert len(data.assistants) == 3
    assert all(a.id.startswith("as_") for a in data.assistants)

    again, created, skipped = catalog.import_seed(tmp_path)
    assert created == 0 and skipped == 3
    assert len(again.assistants) == 3


def test_import_seed_replace_clears_first(tmp_path):
    catalog.create(tmp_path, _payload(name="手工条目"))
    data, created, _ = catalog.import_seed(tmp_path, mode="replace")
    assert created == 3
    assert "手工条目" not in {a.name for a in data.assistants}


def test_malformed_json_is_503_not_silently_empty(tmp_path):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.load(tmp_path)
    assert exc.value.status == 503


def test_bad_entry_is_reported_with_its_index(tmp_path):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "assistants": [
        {"id": "as_00000000", "name": "ok", "description": "d",
         "tasks": ["t"], "outputs": ["o"], "url": "javascript:x"}]},
        ensure_ascii=False), encoding="utf-8")
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.load(tmp_path)
    assert "第 1 条" in str(exc.value)
    assert exc.value.status == 503


def test_unknown_schema_version_is_refused(tmp_path):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 99, "assistants": []}), encoding="utf-8")
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.load(tmp_path)
    assert exc.value.status == 503


def test_active_filters_disabled(tmp_path):
    catalog.create(tmp_path, _payload(name="a", enabled=False))
    catalog.create(tmp_path, _payload(name="b", enabled=False))
    catalog.create(tmp_path, _payload(name="c"))
    assert [a.name for a in catalog.load(tmp_path).active()] == ["c"]


def test_legacy_status_is_read_compatible(tmp_path):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "assistants": [
        {"id": "as_00000001", "name": "p", "description": "d", "tasks": ["t"],
         "outputs": ["o"], "url": "https://example.com/p", "status": "published"},
        {"id": "as_00000002", "name": "d", "description": "d", "tasks": ["t"],
         "outputs": ["o"], "url": "https://example.com/d", "status": "draft"},
        {"id": "as_00000003", "name": "a", "description": "d", "tasks": ["t"],
         "outputs": ["o"], "url": "https://example.com/a", "status": "archived"},
    ]}, ensure_ascii=False), encoding="utf-8")
    loaded = {a.name: a.enabled for a in catalog.load(tmp_path).assistants}
    assert loaded == {"p": True, "d": False, "a": False}

    # 下一次写入就落成新形状，旧字段不再回写。
    catalog.update(tmp_path, "as_00000001", {"description": "x"})
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "status" not in raw["assistants"][0]
    assert raw["assistants"][0]["enabled"] is True
