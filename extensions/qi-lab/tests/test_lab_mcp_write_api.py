"""MCP 声明的**写**面:`POST/PATCH/DELETE /api/mcp`(qi-lab 独有,qi-web 没有)。

读那面有一条从 v1 继承的规矩(见 `test_lab_extensions_api.py`):值不出宿主。写这面
在它之上多三条,每条由用例钉住:

1. **过确认闸**:没有 `X-Qi-Confirm: yes` 就 428,文件一个字节都不动 ——
   `.qi/mcp.json` 跟着仓库走,一次误写会被提交出去;
2. **写下去就得连得上**:传输在闭集里、载荷字段齐全,否则 400。连不上的声明不会报错,
   它只是永远不生效,那是最难查的一种;
3. **值只进不回**:`env` / `command` / `args` 的值必须写得进文件,但**响应里一个字都不给**
   (与读侧同一条安全规矩)。
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
from fastapi.testclient import TestClient  # noqa: E402

SECRET = "sk-do-not-leak-1234567890"
YES = {"X-Qi-Confirm": "yes"}


def _env(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return project


def _role(project: Path, name: str) -> Path:
    d = project / ".qi" / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(
        f"---\nname: {name}\ndescription: 做这个的\n---\n你是 {name}。",
        encoding="utf-8")
    return d


def _client(project: Path) -> TestClient:
    from qi_lab.app import create_app

    app = create_app(cwd=project, password=None, allowed_hosts=[], bind_host="127.0.0.1")
    # base_url 用回环:宿主的 Host 白名单默认只允许回环一类(TestClient 发的是
    # `Host: testserver`,会被挡成 400 —— 那不是路由没注册,是门卫拦的)。
    return TestClient(app, base_url="http://127.0.0.1")


def _project_file(project: Path) -> Path:
    return project / ".qi" / "mcp.json"


def _write(path: Path, payload: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8")


def _servers(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


# ── 新增 ────────────────────────────────────────────────

def test_create_writes_the_file_and_lists_it(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)

    with _client(project) as client:
        response = client.post("/api/mcp", headers=YES, json={
            "scope": "project",
            "server": {"name": "docs", "transport": "streamable-http",
                       "url": "https://mcp.example.com/mcp"},
        })

    assert response.status_code == 201
    assert {s["scope"]: s for s in response.json()["sources"]}["project"]["servers"] == [
        {"name": "docs", "transport": "streamable-http", "url": "https://mcp.example.com/mcp",
         "socket": "", "env_keys": [], "header_keys": [], "disabled": False,
         "direct_tools": False, "unknown_fields": [], "source": "project"}]
    written = json.loads(_project_file(project).read_text(encoding="utf-8"))
    assert written["mcpServers"] == {"docs": {"type": "streamable-http",
                                              "url": "https://mcp.example.com/mcp"}}


def test_create_keeps_the_other_top_level_keys(tmp_path, monkeypatch):
    """文件是用户手写的:`$schema` 这类顶层键**不能因为改一个 server 被抹掉**。"""
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {"old": {"command": "npx", "args": ["-y", "x"]}},
    })

    with _client(project) as client:
        response = client.post("/api/mcp", headers=YES, json={
            "scope": "project",
            "server": {"name": "docs", "transport": "streamable-http",
                       "url": "https://mcp.example.com/mcp"},
        })

    assert response.status_code == 201
    written = json.loads(_project_file(project).read_text(encoding="utf-8"))
    assert written["$schema"].endswith("mcp.schema.json")
    assert set(written["mcpServers"]) == {"old", "docs"}


def test_create_without_confirm_is_refused(tmp_path, monkeypatch):
    """没带确认头 → 428,且**文件不建**(不能“报错了但其实写了一半”)。"""
    project = _env(tmp_path, monkeypatch)

    with _client(project) as client:
        response = client.post("/api/mcp", json={
            "scope": "project",
            "server": {"name": "docs", "transport": "streamable-http",
                       "url": "https://mcp.example.com/mcp"},
        })

    assert response.status_code == 428
    assert not _project_file(project).exists()


def test_create_incomplete_transport_is_refused(tmp_path, monkeypatch):
    """stdio 没给 command → 400:连不上的声明**不会报错**,它只是永远不生效。"""
    project = _env(tmp_path, monkeypatch)

    with _client(project) as client:
        response = client.post("/api/mcp", headers=YES, json={
            "scope": "project", "server": {"name": "broken", "transport": "stdio"}})

    assert response.status_code == 400
    assert "command" in response.json()["detail"]
    assert not _project_file(project).exists()


def test_create_rejects_a_transport_nothing_can_connect_with(tmp_path, monkeypatch):
    """传输是**闭集**:往配置里写 `websocket` 这种连不上的词,比写坏 JSON 更难查。"""
    project = _env(tmp_path, monkeypatch)

    with _client(project) as client:
        response = client.post("/api/mcp", headers=YES, json={
            "scope": "project",
            "server": {"name": "ws", "transport": "websocket", "url": "ws://x"}})

    assert response.status_code == 400
    assert "websocket" in response.json()["detail"]
    assert not _project_file(project).exists()


def test_create_duplicate_name_is_a_conflict(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), {"mcpServers": {"docs": {"command": "npx"}}})

    with _client(project) as client:
        response = client.post("/api/mcp", headers=YES, json={
            "scope": "project",
            "server": {"name": "docs", "transport": "streamable-http", "url": "https://x/mcp"}})

    assert response.status_code == 409
    assert _servers(_project_file(project))["docs"] == {"command": "npx"}   # 原样没动


def test_create_new_global_file_is_private(tmp_path, monkeypatch):
    """声明表里可能直接写着明文密钥(`env`)—— 新文件 0600,已有的保留原权限。"""
    project = _env(tmp_path, monkeypatch)
    home = Path(str(tmp_path / "home"))

    with _client(project) as client:
        response = client.post("/api/mcp", headers=YES, json={
            "scope": "global",
            "server": {"name": "gh", "transport": "stdio", "command": "npx",
                       "env": {"GH_TOKEN": SECRET}}})

    assert response.status_code == 201
    mode = (home / "mcp.json").stat().st_mode & 0o777
    assert mode == 0o600


# ── 修改 ────────────────────────────────────────────────

def test_patch_only_touches_the_keys_it_was_given(tmp_path, monkeypatch):
    """**没送的键一个都不动** —— “值看不见也能改”的前提就是前端不送那个键。"""
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), {"mcpServers": {"gh": {
        "type": "stdio", "command": "npx",
        "args": ["-y", "gh", f"--token={SECRET}"],
        "env": {"GH_TOKEN": SECRET},
        "headers": {"Authorization": f"Bearer {SECRET}"},
        "disabled": True,
    }}})

    with _client(project) as client:
        response = client.patch("/api/mcp", headers=YES, json={
            "scope": "project", "name": "gh", "patch": {"direct_tools": True}})

    assert response.status_code == 200
    got = _servers(_project_file(project))["gh"]
    assert got["args"] == ["-y", "gh", f"--token={SECRET}"]       # 原样
    assert got["env"] == {"GH_TOKEN": SECRET}                     # 原样
    assert got["headers"]["Authorization"] == f"Bearer {SECRET}"  # 原样
    assert got["disabled"] is True                                # 原样
    assert got["directTools"] is True                             # 只有这个变了


def test_patch_rejects_an_incomplete_result(tmp_path, monkeypatch):
    """把 url 清空的 streamable-http → 400,**文件原样**(校验在写盘之前)。"""
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), {"mcpServers": {"docs": {
        "type": "streamable-http", "url": "https://mcp.example.com/mcp"}}})

    with _client(project) as client:
        response = client.patch("/api/mcp", headers=YES, json={
            "scope": "project", "name": "docs", "patch": {"url": ""}})

    assert response.status_code == 400
    assert "url" in response.json()["detail"]
    assert _servers(_project_file(project))["docs"]["url"] == "https://mcp.example.com/mcp"


def test_patch_transport_change_clears_the_old_carrier_fields(tmp_path, monkeypatch):
    """换传输 = 换载荷:留着 `command` 会在文件里躺成一份没人看得懂的残骸。"""
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), {"mcpServers": {"x": {
        "type": "stdio", "command": "npx", "args": ["-y", "y"]}}})

    with _client(project) as client:
        response = client.patch("/api/mcp", headers=YES, json={
            "scope": "project", "name": "x",
            "patch": {"transport": "streamable-http", "url": "https://mcp.example.com/mcp"}})

    assert response.status_code == 200
    assert _servers(_project_file(project))["x"] == {
        "type": "streamable-http", "url": "https://mcp.example.com/mcp"}


def test_patch_can_rename(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), {"mcpServers": {
        "one": {"command": "a"}, "two": {"command": "b"}}})

    with _client(project) as client:
        response = client.patch("/api/mcp", headers=YES, json={
            "scope": "project", "name": "one", "patch": {"name": "uno"}})

    assert response.status_code == 200
    assert list(_servers(_project_file(project))) == ["uno", "two"]   # 顺序也没乱


def test_patch_rename_onto_an_existing_name_is_a_conflict(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), {"mcpServers": {"one": {"command": "a"},
                                                   "two": {"command": "b"}}})

    with _client(project) as client:
        response = client.patch("/api/mcp", headers=YES, json={
            "scope": "project", "name": "one", "patch": {"name": "two"}})

    assert response.status_code == 409
    assert set(_servers(_project_file(project))) == {"one", "two"}


def test_patch_unknown_server_is_a_404(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), {"mcpServers": {"a": {"command": "x"}}})

    with _client(project) as client:
        response = client.patch("/api/mcp", headers=YES, json={
            "scope": "project", "name": "nope", "patch": {"disabled": True}})

    assert response.status_code == 404


# ── 删除 ────────────────────────────────────────────────

def test_delete_only_removes_that_layer(tmp_path, monkeypatch):
    """项目层删掉一个同名 server → **全局那个露出来**(覆盖关系没变)。"""
    project = _env(tmp_path, monkeypatch)
    home = Path(str(tmp_path / "home"))
    _write(home / "mcp.json", {"mcpServers": {"gh": {"command": "global-version"}}})
    _write(_project_file(project), {"mcpServers": {"gh": {"command": "project-version"}}})

    with _client(project) as client:
        response = client.request("DELETE", "/api/mcp", headers=YES,
                                 json={"scope": "project", "name": "gh"})
        after = client.get("/api/mcp").json()

    assert response.status_code == 200
    assert _servers(_project_file(project)) == {}
    by_scope = {s["scope"]: s for s in after["sources"]}
    assert [s["name"] for s in by_scope["global"]["servers"]] == ["gh"]
    assert by_scope["project"]["servers"] == []
    assert _servers(home / "mcp.json") == {"gh": {"command": "global-version"}}


def test_delete_unknown_name_is_a_404(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), {"mcpServers": {"a": {"command": "x"}}})

    with _client(project) as client:
        response = client.request("DELETE", "/api/mcp", headers=YES,
                                 json={"scope": "project", "name": "nope"})

    assert response.status_code == 404
    assert set(_servers(_project_file(project))) == {"a"}


def test_delete_without_confirm_leaves_the_file_alone(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), {"mcpServers": {"a": {"command": "x"}}})

    with _client(project) as client:
        response = client.request("DELETE", "/api/mcp", json={"scope": "project", "name": "a"})

    assert response.status_code == 428
    assert set(_servers(_project_file(project))) == {"a"}


# ── 角色那一层 ──────────────────────────────────────────

def test_role_scope_writes_that_roles_own_file(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    role = _role(project, "scout")

    with _client(project) as client:
        created = client.post("/api/mcp", headers=YES, json={
            "scope": "role", "role": "scout",
            "server": {"name": "priv", "transport": "stdio", "command": "p"}})
        listed = {s["scope"]: s for s in client.get("/api/mcp").json()["sources"]}

    assert created.status_code == 201
    assert _servers(role / "mcp.json") == {"priv": {"type": "stdio", "command": "p"}}
    assert listed["role"]["role"] == "scout"
    assert [s["name"] for s in listed["role"]["servers"]] == ["priv"]


def test_role_scope_needs_the_role_name_and_the_role_itself(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _role(project, "scout")

    with _client(project) as client:
        missing_role = client.post("/api/mcp", headers=YES, json={
            "scope": "role",
            "server": {"name": "p", "transport": "stdio", "command": "p"}})
        unknown_role = client.post("/api/mcp", headers=YES, json={
            "scope": "role", "role": "ghost",
            "server": {"name": "p", "transport": "stdio", "command": "p"}})

    assert missing_role.status_code == 400
    assert unknown_role.status_code == 404


def test_socket_server_round_trips(tmp_path, monkeypatch):
    """socket 那种 server **读得回来才改得动** —— 所以套接字路径进结构(它不是密钥)。

    url 与 socket 是同一档:不出宿主的是值(env / headers / command / args),不是路径。
    """
    project = _env(tmp_path, monkeypatch)

    with _client(project) as client:
        created = client.post("/api/mcp", headers=YES, json={
            "scope": "project",
            "server": {"name": "local", "transport": "socket",
                       "socket": "/tmp/mcp.sock"}})
        read = {s["scope"]: s for s in client.get("/api/mcp").json()["sources"]}
        patched = client.patch("/api/mcp", headers=YES, json={
            "scope": "project", "name": "local", "patch": {"socket": "/tmp/other.sock"}})

    assert created.status_code == 201 and patched.status_code == 200
    assert read["project"]["servers"][0]["transport"] == "socket"
    assert read["project"]["servers"][0]["socket"] == "/tmp/mcp.sock"
    assert _servers(_project_file(project))["local"]["socket"] == "/tmp/other.sock"


# ── 安全:值只进不回 ────────────────────────────────────

def test_write_never_echoes_values(tmp_path, monkeypatch):
    """**与读侧同一条规矩**:值写得进去,但响应里一个字都不给。

    症状要是“保存成功 + 页面上把密钥打了出来”,那这条规矩就白立了。
    """
    project = _env(tmp_path, monkeypatch)

    with _client(project) as client:
        response = client.post("/api/mcp", headers=YES, json={
            "scope": "project",
            "server": {"name": "gh", "transport": "stdio", "command": "npx",
                       "args": ["-y", "gh", f"--token={SECRET}"],
                       "env": {"GH_TOKEN": SECRET},
                       "headers": {"Authorization": f"Bearer {SECRET}"}}})
        patched = client.patch("/api/mcp", headers=YES, json={
            "scope": "project", "name": "gh",
            "patch": {"env": {"NEW_TOKEN": SECRET}}})

    assert response.status_code == 201 and patched.status_code == 200
    for body in (response.text, patched.text):
        assert SECRET not in body
        assert "npx" not in body                 # stdio 的 command 也不给
        assert "--token" not in body             # args 更不给
    # …而文件里是真写进去了(写端点不是摆设)
    got = _servers(_project_file(project))["gh"]
    assert got["env"] == {"NEW_TOKEN": SECRET}
    assert got["args"] == ["-y", "gh", f"--token={SECRET}"]


# ── 坏状态:说清楚,别 500 ──────────────────────────────

def test_broken_json_is_reported_as_a_read_error(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(_project_file(project), "{ 这不是 JSON")

    with _client(project) as client:
        response = client.post("/api/mcp", headers=YES, json={
            "scope": "project", "server": {"name": "x", "transport": "stdio",
                                           "command": "npx"}})

    assert response.status_code == 400
    assert "mcp.json" in response.json()["detail"]      # 带上路径,现场只看 JSON 报错没法修


def test_write_without_qi_mcp_says_so(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "qi_mcp", None)
    monkeypatch.setitem(sys.modules, "qi_mcp.config", None)

    with _client(project) as client:
        response = client.post("/api/mcp", headers=YES, json={
            "scope": "project", "server": {"name": "x", "transport": "stdio",
                                           "command": "npx"}})

    assert response.status_code == 400
    assert "qi-mcp" in response.json()["detail"]


def test_meta_advertises_the_write_capability(tmp_path, monkeypatch):
    """界面据 `/api/meta` 的 `mcp_write` 决定要不要出“添加/编辑/删除”。

    老宿主没有这个开关 → 那一节当只读,而不是点了才吃 404。
    """
    project = _env(tmp_path, monkeypatch)

    with _client(project) as client:
        caps = client.get("/api/meta").json()["capabilities"]

    assert caps.get("mcp_write") is True
