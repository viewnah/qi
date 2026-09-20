"""P-E5 ③ 切片 3c:Web 端点的角色与 MCP 走**扩展的真实数据**(用户选 A)。

那两节 UI 原本指向 P-E4c 删掉的 `/api/agents` 与 `/api/mcp`;这里按"接真实面"重建 ——
数据分别来自 **qi-agents**(`discover()`)与 **qi-mcp**(两层 + 各角色私有)。

三个不变量,每条都由用例钉住:

1. **值一律不出宿主**(从 v1 继承的那条安全规矩):只给 `env`/`headers` 的**键名**,
   stdio 的 `command`/`args` 也不给 —— 那是最容易把密钥直接写进去的地方;
2. **没装扩展不是 500**:`unavailable=True`,界面据此说清楚(少装一个扩展不该让整页报错);
3. **`exists=false` 也回**:前端据此区分"没这个文件"与"文件在但没写 server"。
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
from fastapi.testclient import TestClient  # noqa: E402

SECRET = "sk-do-not-leak-1234567890"


def _env(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return project


def _role(project: Path, name: str, *, description="做这个的", tools='["read"]',
          mcp: dict | None = None) -> Path:
    d = project / ".qi" / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ntools: {tools}\n---\n你是 {name}。",
        encoding="utf-8")
    if mcp is not None:
        (d / "mcp.json").write_text(json.dumps({"mcpServers": mcp}), encoding="utf-8")
    return d


def _client(project: Path) -> TestClient:
    from qi_web.app import create_app

    app = create_app(cwd=project, password=None, allowed_hosts=[], bind_host="127.0.0.1")
    # base_url 用回环:宿主的 Host 白名单默认只允许回环一类,而 TestClient 默认发
    # `Host: testserver` —— 那会被挡成 400(不是路由没注册,是门卫拦的)。
    return TestClient(app, base_url="http://127.0.0.1")


# ── /api/agents:数据来自 qi-agents ──────────────────────

def test_agents_endpoint_reads_qi_agents(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _role(project, "reviewer", description="只读审查", tools='["read", "grep"]')
    _role(project, "scout", mcp={"gh": {"command": "npx", "args": ["-y", "x"]}})

    with _client(project) as client:
        body = client.get("/api/agents").json()

    assert body["unavailable"] is False
    agents = {a["name"]: a for a in body["agents"]}
    assert set(agents) == {"reviewer", "scout"}
    assert agents["reviewer"]["description"] == "只读审查"
    assert agents["reviewer"]["source"] == "project"
    assert agents["reviewer"]["tools"] == ["read", "grep"]
    assert agents["scout"]["mcp"] == 1              # 角色私有 mcp.json 里的 server 数


def test_agents_endpoint_says_unavailable_without_qi_agents(tmp_path, monkeypatch):
    """少装 qi-agents 不该让整页 500 —— 说清楚"没有"比报错有用。"""
    project = _env(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "qi_agents", None)          # import 会失败
    monkeypatch.setitem(sys.modules, "qi_agents.discovery", None)

    with _client(project) as client:
        response = client.get("/api/agents")
    assert response.status_code == 200
    assert response.json() == {"agents": [], "unavailable": True}


# ── /api/mcp:数据来自 qi-mcp ────────────────────────────

def test_mcp_endpoint_lists_layers_and_role_private(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    home = Path(str(tmp_path / "home"))
    (home / "mcp.json").write_text(
        json.dumps({"mcpServers": {"global-one": {"command": "npx", "args": ["-y", "g"]}}}),
        encoding="utf-8")
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"docs": {"url": "https://mcp.example.com/mcp"}}}),
        encoding="utf-8")
    _role(project, "scout", mcp={"priv": {"command": "p"}})

    with _client(project) as client:
        body = client.get("/api/mcp").json()

    assert body["unavailable"] is False
    by_scope = {s["scope"]: s for s in body["sources"]}
    assert set(by_scope) == {"global", "project", "role"}
    assert by_scope["global"]["exists"] is True
    assert by_scope["global"]["servers"][0]["name"] == "global-one"
    assert by_scope["project"]["servers"][0]["transport"] == "http"
    assert by_scope["project"]["servers"][0]["url"] == "https://mcp.example.com/mcp"
    assert by_scope["role"]["role"] == "scout"
    assert by_scope["role"]["servers"][0]["name"] == "priv"


def test_mcp_endpoint_never_leaks_values(tmp_path, monkeypatch):
    """**这条是从 v1 继承的安全规矩**,P-E4c 删端点时一起删过 —— 现在重新钉住。

    只给 `env` / `headers` 的键名;值与 stdio 的 `command`/`args` 一律不出宿主。
    """
    project = _env(tmp_path, monkeypatch)
    home = Path(str(tmp_path / "home"))
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"gh": {
        "command": "npx",
        "args": ["-y", "server", f"--token={SECRET}"],       # 明文密钥的典型落点
        "env": {"GH_TOKEN": SECRET},
        "headers": {"Authorization": f"Bearer {SECRET}"},
        "disabled": True,
        "directTools": True,
        "某不认识的字段": 1,
    }}}), encoding="utf-8")

    with _client(project) as client:
        raw = client.get("/api/mcp").text

    assert SECRET not in raw                       # 值一个都不能出现
    assert "npx" not in raw                        # stdio 的 command 也不给
    assert "--token" not in raw                    # args 更不给
    server = client.get("/api/mcp").json()["sources"][0]["servers"][0]
    assert server["env_keys"] == ["GH_TOKEN"]      # 键名可以给
    assert server["header_keys"] == ["Authorization"]
    assert server["transport"] == "stdio"
    assert server["disabled"] is True
    assert server["direct_tools"] is True
    assert server["unknown_fields"] == ["某不认识的字段"]   # 不认识的字段只报名字


def test_mcp_endpoint_reports_missing_files_as_empty(tmp_path, monkeypatch):
    """`exists=false` 也回 —— 前端据此区分"没这个文件"与"文件在但没写 server"。"""
    project = _env(tmp_path, monkeypatch)

    with _client(project) as client:
        body = client.get("/api/mcp").json()

    scopes = {s["scope"]: s for s in body["sources"]}
    assert scopes["global"]["exists"] is False and scopes["global"]["servers"] == []
    assert scopes["project"]["exists"] is False


def test_mcp_endpoint_says_unavailable_without_qi_mcp(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "qi_mcp", None)
    monkeypatch.setitem(sys.modules, "qi_mcp.config", None)

    with _client(project) as client:
        body = client.get("/api/mcp").json()
    assert body == {"sources": [], "unavailable": True}


def test_broken_role_mcp_json_does_not_break_the_page(tmp_path, monkeypatch):
    """一个角色把 mcp.json 写坏 → 那一层不出现,而不是整页 500。"""
    project = _env(tmp_path, monkeypatch)
    d = _role(project, "broken")
    (d / "mcp.json").write_text("{ 这不是 JSON", encoding="utf-8")

    with _client(project) as client:
        response = client.get("/api/mcp")
    assert response.status_code == 200
    assert all(s["scope"] != "role" for s in response.json()["sources"])
