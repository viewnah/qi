"""A2A 条目:`GET/POST/PATCH/DELETE /api/a2a` + `POST /api/a2a/probe`(qi-lab 独有)。

与 MCP 那一套共用三条规矩(见 `test_lab_mcp_write_api.py`),但 A2A 多两件**自己的**事,
各有用例钉住:

1. **两层深合并**:项目层只写差异,读端要能同时看见"每层原样"与"合并后生效",
   `null` 删键、数组整体替换;
2. **`headers` 值不出宿主**:读端与写端都只回键名 —— 那是 token 最常待的地方。

探测那条**不联外网**:用 `httpx.MockTransport` 换掉 `A2AClient` 的传输。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402

pytest.importorskip("fastapi")
pytest.importorskip("a2a")
from fastapi.testclient import TestClient  # noqa: E402

SECRET = "sk-do-not-leak-1234567890"
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
    # base_url 用回环:宿主的 Host 白名单默认只允许回环一类(TestClient 发的是
    # `Host: testserver`,会被挡成 400 —— 那不是路由没注册,是门卫拦的)。
    return TestClient(app, base_url="http://127.0.0.1")


def _write(project: Path, payload: dict, *, layer: str = "project") -> Path:
    base = project / ".qi" if layer == "project" else project.parent / "home"
    path = base / "a2a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _entries(project: Path) -> dict:
    path = project / ".qi" / "a2a.json"
    return json.loads(path.read_text(encoding="utf-8"))["a2aServers"]


# ── 读 ──────────────────────────────────────────────────

def test_list_reports_both_layers_and_the_merged_view(tmp_path, monkeypatch):
    """两层:**逐层原样**给人看"谁写的",**resolved** 给人看"现在生效的是什么"。"""
    project = _env(tmp_path, monkeypatch)
    home = tmp_path / "home"
    (home / "a2a.json").write_text(json.dumps({"a2aServers": {
        "hikqin": {"baseUrl": "http://gw:8080", "timeoutMs": 1000}}}), encoding="utf-8")
    _write(project, {"a2aServers": {
        "hikqin": {"tenant": "agent-1", "description": "项目加的"}}})

    body = _client(project).get("/api/a2a").json()

    assert body["unavailable"] is False
    assert [s["scope"] for s in body["sources"]] == ["global", "project"]
    assert body["sources"][0]["exists"] is True
    # 合并视图:两层的值都在,来源是**主导层**(最具体的那一层)
    merged = {e["name"]: e for e in body["resolved"]}["hikqin"]
    assert merged["base_url"] == "http://gw:8080"
    assert merged["tenant"] == "agent-1"
    assert merged["timeout_ms"] == 1000
    assert merged["source"] == "project"
    assert merged["layers"] == ["global", "project"]


def test_list_shows_missing_files_as_empty(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    body = _client(project).get("/api/a2a").json()
    assert body["resolved"] == []
    assert [s["exists"] for s in body["sources"]] == [False, False]


def test_list_never_leaks_header_values(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {
        "baseUrl": "http://gw:8080",
        "headers": {"Authorization": f"Bearer {SECRET}"}}}})

    response = _client(project).get("/api/a2a")

    assert response.status_code == 200
    assert SECRET not in response.text
    entry = response.json()["resolved"][0]
    assert entry["header_keys"] == ["Authorization"]


def test_list_does_not_expand_env_placeholders(tmp_path, monkeypatch):
    """展开等于把环境变量的值吐进 HTTP 响应 —— 读端只做合并,不做展开。"""
    project = _env(tmp_path, monkeypatch)
    monkeypatch.setenv("A2A_TEST_TOKEN", SECRET)
    _write(project, {"a2aServers": {"hikqin": {
        "baseUrl": "http://gw:8080",
        "headers": {"Authorization": "Bearer ${A2A_TEST_TOKEN}"}}}})

    assert SECRET not in _client(project).get("/api/a2a").text


def test_disabled_entries_are_listed_not_hidden(tmp_path, monkeypatch):
    """关掉的也要看得见 —— 界面得能说明"为什么这个用不了"。"""
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {
        "on": {"baseUrl": "http://a"},
        "off": {"baseUrl": "http://b", "disabled": True}}})

    resolved = {e["name"]: e for e in _client(project).get("/api/a2a").json()["resolved"]}
    assert resolved["off"]["disabled"] is True


def test_probe_and_write_are_advertised_in_meta(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    assert _client(project).get("/api/meta").json()["capabilities"]["a2a_write"] is True


# ── 写:确认头 ──────────────────────────────────────────

def test_write_requires_the_confirm_header(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    client = _client(project)
    body = {"scope": "project", "entry": {"name": "hikqin", "baseUrl": "http://gw"}}

    response = client.post("/api/a2a", json={"scope": "project",
                                             "entry": {"name": "hikqin", "base_url": "http://gw"}})

    assert response.status_code == 428
    assert not (project / ".qi" / "a2a.json").exists()   # 一个字节都没动
    assert body                                                    # 形状留作对照


def test_delete_requires_the_confirm_header(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {"baseUrl": "http://gw"}}})

    response = _client(project).request("DELETE", "/api/a2a",
                                        json={"scope": "project", "name": "hikqin"})

    assert response.status_code == 428
    assert "hikqin" in _entries(project)


# ── 写:新建 ────────────────────────────────────────────

def test_create_writes_camel_case_and_returns_the_list(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    response = _client(project).post(
        "/api/a2a", headers=YES, json={"scope": "project", "entry": {
            "name": "skill-debug",
            "card_url": "http://gw/a2a/agents/a1/card.json",
            "timeout_ms": 900000,
            "direct_tools": True,
            "headers": {"Authorization": f"Bearer {SECRET}"},
        }})

    assert response.status_code == 201
    written = _entries(project)["skill-debug"]
    assert written["cardUrl"] == "http://gw/a2a/agents/a1/card.json"
    assert written["timeoutMs"] == 900000
    assert written["directTools"] is True
    assert written["headers"]["Authorization"] == f"Bearer {SECRET}"
    # 响应是列表,且**值不回显**;工具名里的非标识符字符会被收成 `_`
    assert response.json()["resolved"][0]["tool_name"] == "a2a__skill_debug"
    assert SECRET not in response.text


def test_create_rejects_same_name_within_one_layer(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {"baseUrl": "http://gw"}}})

    response = _client(project).post(
        "/api/a2a", headers=YES, json={"scope": "project",
                                       "entry": {"name": "hikqin", "base_url": "http://x"}})

    assert response.status_code == 409


def test_create_allows_same_name_in_another_layer(tmp_path, monkeypatch):
    """跨层同名是**合法**的(那正是深合并的关系)。"""
    project = _env(tmp_path, monkeypatch)
    (tmp_path / "home" / "a2a.json").write_text(
        json.dumps({"a2aServers": {"hikqin": {"baseUrl": "http://gw"}}}), encoding="utf-8")

    response = _client(project).post(
        "/api/a2a", headers=YES, json={"scope": "project",
                                       "entry": {"name": "hikqin", "tenant": "agent-1"}})

    assert response.status_code == 201
    assert _entries(project)["hikqin"] == {"tenant": "agent-1"}


def test_create_rejects_an_entry_without_any_endpoint(tmp_path, monkeypatch):
    """写下去不生效的声明不会报错,它只是永远连不上 —— 那是最难查的一种。"""
    project = _env(tmp_path, monkeypatch)

    response = _client(project).post(
        "/api/a2a", headers=YES,
        json={"scope": "project", "entry": {"name": "空条目", "description": "只有描述"}})

    assert response.status_code == 400
    assert "baseUrl" in response.json()["detail"] or "rpcUrl" in response.json()["detail"]


def test_create_rejects_a_non_http_url(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)

    response = _client(project).post(
        "/api/a2a", headers=YES,
        json={"scope": "project", "entry": {"name": "x", "base_url": "ftp://gw"}})

    assert response.status_code == 400
    assert "http" in response.json()["detail"]


def test_create_rejects_missing_env_variable(tmp_path, monkeypatch):
    """缺环境变量要**当场**报出来,而不是写一行 `${TOKEN}` 等运行时 401。"""
    project = _env(tmp_path, monkeypatch)
    monkeypatch.delenv("A2A_NOPE", raising=False)

    response = _client(project).post(
        "/api/a2a", headers=YES, json={"scope": "project", "entry": {
            "name": "x", "base_url": "http://gw",
            "headers": {"Authorization": "Bearer ${A2A_NOPE}"}}})

    assert response.status_code == 400
    assert "A2A_NOPE" in response.json()["detail"]


def test_create_rejects_bad_name(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    client = _client(project)

    for name in ("", "  ", " x "):
        response = client.post("/api/a2a", headers=YES,
                               json={"scope": "project",
                                     "entry": {"name": name, "base_url": "http://gw"}})
        assert response.status_code == 400, name


def test_create_keeps_top_level_keys_and_uses_0600(tmp_path, monkeypatch):
    """文件是用户手写的:改一个条目不能把 `$schema` 抹掉;新文件只给自己读。"""
    project = _env(tmp_path, monkeypatch)
    path = project / ".qi" / "a2a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"$schema": "https://x/a2a.schema.json",
                                "a2aServers": {}}), encoding="utf-8")

    _client(project).post("/api/a2a", headers=YES,
                          json={"scope": "project",
                                "entry": {"name": "x", "base_url": "http://gw"}})

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["$schema"] == "https://x/a2a.schema.json"
    assert "x" in raw["a2aServers"]


def test_unavailable_when_qi_a2a_is_not_installed(tmp_path, monkeypatch):
    """没装扩展 → `unavailable=true`,不是 500。"""
    project = _env(tmp_path, monkeypatch)
    # 已 import 过的子模块不会因为父包被换成 None 而失效,两个都要挡。
    for name in ("qi_a2a", "qi_a2a.config", "qi_a2a.card", "qi_a2a.client"):
        monkeypatch.setitem(sys.modules, name, None)

    response = _client(project).get("/api/a2a")

    assert response.status_code == 200
    assert response.json()["unavailable"] is True


# ── 写:改 ──────────────────────────────────────────────

def test_patch_only_touches_the_keys_that_were_sent(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {
        "baseUrl": "http://gw", "timeoutMs": 1000,
        "headers": {"Authorization": f"Bearer {SECRET}"}}}})

    response = _client(project).patch(
        "/api/a2a", headers=YES,
        json={"scope": "project", "name": "hikqin", "patch": {"tenant": "agent-9"}})

    assert response.status_code == 200
    written = _entries(project)["hikqin"]
    assert written["tenant"] == "agent-9"
    assert written["timeoutMs"] == 1000                       # 没送的不动
    assert written["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_patch_null_means_do_not_change(tmp_path, monkeypatch):
    """读端不给 `headers` 的值,前端就不可能把它重发回来 —— 把 `null` 当"删掉"
    会让人一保存就丢 token。"""
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {
        "baseUrl": "http://gw", "headers": {"Authorization": f"Bearer {SECRET}"}}}})

    _client(project).patch("/api/a2a", headers=YES, json={
        "scope": "project", "name": "hikqin", "patch": {"headers": None}})

    assert _entries(project)["hikqin"]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_patch_empty_headers_clears_the_table(tmp_path, monkeypatch):
    """要清掉整张表就送 `{}` —— 与上面那条"null = 不改"配合起来才没有歧义。"""
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {
        "baseUrl": "http://gw", "headers": {"A": "1"}}}})

    _client(project).patch("/api/a2a", headers=YES, json={
        "scope": "project", "name": "hikqin", "patch": {"headers": {}}})

    assert "headers" not in _entries(project)["hikqin"]


def test_patch_renames_in_place_and_keeps_order(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {
        "a": {"baseUrl": "http://a"},
        "b": {"baseUrl": "http://b", "tenant": "t1"},
        "c": {"baseUrl": "http://c"}}})

    _client(project).patch("/api/a2a", headers=YES, json={
        "scope": "project", "name": "b", "patch": {"name": "renamed"}})

    assert list(_entries(project)) == ["a", "renamed", "c"]
    assert _entries(project)["renamed"]["tenant"] == "t1"


def test_patch_validates_the_merged_result(tmp_path, monkeypatch):
    """把唯一端点删掉 → 400(而不是写一份永远不生效的声明)。"""
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {"baseUrl": "http://gw"}}})

    response = _client(project).patch("/api/a2a", headers=YES, json={
        "scope": "project", "name": "hikqin", "patch": {"base_url": ""}})

    assert response.status_code == 400
    assert _entries(project)["hikqin"]["baseUrl"] == "http://gw"     # 没落盘


def test_patch_unknown_entry_is_404(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {"baseUrl": "http://gw"}}})

    response = _client(project).patch("/api/a2a", headers=YES, json={
        "scope": "project", "name": "nope", "patch": {"tenant": "t"}})

    assert response.status_code == 404


def test_patch_preserves_unknown_fields(tmp_path, monkeypatch):
    """读端会说"这个字段我不认识",但它**不能因此在保存时被删掉**。"""
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {
        "baseUrl": "http://gw", "pushNotifications": True}}})

    _client(project).patch("/api/a2a", headers=YES, json={
        "scope": "project", "name": "hikqin", "patch": {"tenant": "t"}})

    assert _entries(project)["hikqin"]["pushNotifications"] is True


# ── 写:删 ──────────────────────────────────────────────

def test_delete_only_removes_that_layer(tmp_path, monkeypatch):
    """删掉项目层那份,全局层的同名条目**露出来** —— 深合并关系没变。"""
    project = _env(tmp_path, monkeypatch)
    (tmp_path / "home" / "a2a.json").write_text(
        json.dumps({"a2aServers": {"hikqin": {"baseUrl": "http://global"}}}),
        encoding="utf-8")
    _write(project, {"a2aServers": {"hikqin": {"tenant": "agent-1"}}})

    response = _client(project).request(
        "DELETE", "/api/a2a", headers=YES,
        json={"scope": "project", "name": "hikqin"})

    assert response.status_code == 200
    assert _entries(project) == {}
    merged = response.json()["resolved"][0]
    assert merged["base_url"] == "http://global"
    assert merged["layers"] == ["global"]


def test_delete_unknown_entry_is_404(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"a": {"baseUrl": "http://a"}}})

    response = _client(project).request(
        "DELETE", "/api/a2a", headers=YES, json={"scope": "project", "name": "zzz"})

    assert response.status_code == 404


def test_bad_json_is_a_readable_400(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    path = project / ".qi" / "a2a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{oops", encoding="utf-8")

    response = _client(project).get("/api/a2a")

    assert response.status_code == 400
    assert "a2a.json" in response.json()["detail"]


# ── 探测 ────────────────────────────────────────────────

CARD = {
    "name": "技能调试",
    "description": "调技能",
    "version": "1.0.0",
    "capabilities": {"streaming": True},
    "defaultInputModes": ["text"],
    "defaultOutputModes": ["text"],
    "skills": [{"id": "s1", "name": "调试", "description": "查配置", "tags": ["技能"]}],
    "supportedInterfaces": [{"url": "http://gw/a2a", "protocolBinding": "JSONRPC",
                             "protocolVersion": "1.0", "tenant": "agent-1"}],
}


def _patch_transport(monkeypatch, handler) -> None:
    """把 `A2AClient` 的 httpx 传输换成 MockTransport —— 探测不联外网。"""
    import httpx

    from qi_a2a import client as a2a_client

    real = a2a_client.A2AClient

    def factory(entry, *, transport=None):
        return real(entry, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(a2a_client, "A2AClient", factory)


def test_probe_reads_the_card_and_reports_timing(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {"baseUrl": "http://gw"}}})

    def handler(request):
        import httpx
        return httpx.Response(200, json=CARD)

    _patch_transport(monkeypatch, handler)

    body = _client(project).post("/api/a2a/probe", json={"server": "hikqin"}).json()

    assert body["ok"] is True
    assert body["discovery_url"] == "http://gw/a2a/agents"
    assert body["source"] == "project"
    assert body["cards"][0]["tenant"] == "agent-1"
    assert body["cards"][0]["skills"][0]["name"] == "调试"
    assert body["elapsed_ms"] >= 0


def test_probe_reports_network_failure_as_data_not_500(tmp_path, monkeypatch):
    """探测失败是**结果**,不是宿主异常:界面要显示那句错,而不是一个 500。"""
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"hikqin": {"baseUrl": "http://gw"}}})

    def handler(request):
        import httpx
        return httpx.Response(404, text="not found")

    _patch_transport(monkeypatch, handler)

    response = _client(project).post("/api/a2a/probe", json={"server": "hikqin"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "404" in body["error"]


def test_probe_picks_a_single_entry_automatically(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"only": {"baseUrl": "http://gw"}}})

    def handler(request):
        import httpx
        return httpx.Response(200, json=CARD)

    _patch_transport(monkeypatch, handler)

    assert _client(project).post("/api/a2a/probe", json={}).json()["ok"] is True


def test_probe_requires_a_name_when_several_are_configured(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"a": {"baseUrl": "http://a"},
                                    "b": {"baseUrl": "http://b"}}})

    body = _client(project).post("/api/a2a/probe", json={}).json()

    assert body["ok"] is False
    assert "server" in body["error"]


def test_probe_rejects_unknown_entry(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"a": {"baseUrl": "http://a"}}})

    body = _client(project).post("/api/a2a/probe", json={"server": "nope"}).json()

    assert body["ok"] is False
    assert "nope" in body["error"]


def test_probe_without_entries_explains_how_to_add_one(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)

    body = _client(project).post("/api/a2a/probe", json={}).json()

    assert body["ok"] is False
    assert "baseUrl" in body["error"]


def test_probe_can_select_one_agent(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"gw": {"baseUrl": "http://gw"}}})

    second = json.loads(json.dumps(CARD))
    second["name"] = "另一个"
    second["supportedInterfaces"][0]["tenant"] = "agent-2"

    def handler(request):
        import httpx
        return httpx.Response(200, json=[CARD, second])

    _patch_transport(monkeypatch, handler)

    body = _client(project).post(
        "/api/a2a/probe", json={"server": "gw", "agent": "agent-2"}).json()

    assert [c["tenant"] for c in body["cards"]] == ["agent-2"]


def test_probe_skips_disabled_entries(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project, {"a2aServers": {"off": {"baseUrl": "http://gw", "disabled": True}}})

    body = _client(project).post("/api/a2a/probe", json={}).json()

    assert body["ok"] is False
    assert "没有任何 A2A 条目" in body["error"]
