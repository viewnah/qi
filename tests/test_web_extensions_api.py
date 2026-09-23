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


def test_mcp_endpoint_shows_agent_plugins_type_verbatim(tmp_path, monkeypatch):
    """`.qi/mcp.json` 用的是 Agent Plugins 1.0 写法(顶层 `$schema` + `type`)。

    网页上显示的值要**和配置文件对得上**(`streamable-http` 而不是被推断出来的 `http`),
    而 `type` 也不该被当成“未识别字段”报警。
    """
    project = _env(tmp_path, monkeypatch)
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "mcp.json").write_text(json.dumps({
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {
            "docs": {"type": "streamable-http", "url": "https://mcp.example.com/mcp"},
            "legacy": {"command": "npx", "args": ["-y", "x"]},      # 没 `type` 的旧声明
        },
    }), encoding="utf-8")

    with _client(project) as client:
        sources = {s["scope"]: s for s in client.get("/api/mcp").json()["sources"]}
    servers = {s["name"]: s for s in sources["project"]["servers"]}

    assert servers["docs"]["transport"] == "streamable-http"      # 照 `type` 显示
    assert servers["docs"]["url"] == "https://mcp.example.com/mcp"
    assert servers["docs"]["unknown_fields"] == []                 # `type` / `$schema` 都不报
    assert servers["legacy"]["transport"] == "stdio"              # 字段推断仍然生效


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


# ── 「智能体 chip」真的影响系统提示词(回归)─────────────────
#
# 这一段此前是**静默失效**的:前端把 `forwardedProps.agent` 送上来,后端转手交给
# `runtime.stream(agent_override=…)` —— 而那是 P-E4c 之前的接口,core 收窄成单 agent
# 之后流水线里已经没有任何地方读它。传了不报错、界面照常显示,只是角色提示词从没生效。
# 角色现在是 qi-agents 的 `before_agent_start`,所以这里钉住"钉住值真的到达提示词"。

class _RecordingLLM:
    """记下每次请求的 system prompt。

    runner 走的是**流式**路径(`stream_llm` 有 `astream` 就用它),所以这里实现
    `astream`;只实现 `chat` 的话那个协程根本不会被 await(实测的 `never awaited` 警告)。
    """

    def __init__(self) -> None:
        self.systems: list[str] = []

    async def chat(self, messages, tools=None, temperature=None):  # pragma: no cover
        raise AssertionError("有 astream 时不应调用 chat")

    async def astream(self, messages, tools=None, temperature=None):
        from qi_agent.llm import LLMDelta

        self.systems.append(str(messages[0].content))
        yield LLMDelta(text="好")
        yield LLMDelta(finished=True, usage={"total_tokens": 1})


def _shared_runtime_app(project: Path, tmp_path: Path, monkeypatch, llm, *, approve=None):
    """一个 runtime 服务一个 cwd —— 与 WebState 的真实形状一致。"""
    from qi_agent import paths
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore
    from qi_web.app import create_app
    from qi_web.state import WebState

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (tmp_path / "models.json").write_text(
        '{"providers": {"ollama": {"api": "openai-completions", "models": [{"id": "x"}]}}}',
        encoding="utf-8")
    (home / "settings.json").write_text(
        '{"defaultProvider": "ollama", "defaultModel": "x"}', encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    store = SessionStore(root=tmp_path / "sessions")
    state = WebState(project, runtime_factory=lambda cwd: QiRuntime(
        cwd=Path(cwd), runtime_cfg=RuntimeConfig(workdir=Path(cwd)),
        session_store=store, llm=llm, approve_project=approve))
    return create_app(cwd=project, state=state, allowed_hosts=[], bind_host="127.0.0.1")


def _run_turn(client, sid: str, agent: str | None, run_id: str) -> None:
    with client.stream("POST", "/api/ag-ui", json={
        "threadId": sid, "runId": run_id,
        "messages": [{"role": "user", "content": "你好"}],
        "forwardedProps": {"agent": agent},
    }) as resp:
        assert resp.status_code == 200, resp.read()
        for _line in resp.iter_lines():
            pass


def _write_role(root: Path, name: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(
        f"---\nname: {name}\ndescription: 做这个的\n---\n{body}", encoding="utf-8")


def test_pinned_agent_reaches_the_system_prompt(tmp_path, monkeypatch):
    """钉住 `scout` → 它的正文进 system prompt;切回 auto → 不再进。

    用的是**用户级**角色(`~/.qi/agent/agents/`):项目级那条另有信任闸门(见下一个用例)。
    """
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    llm = _RecordingLLM()
    app = _shared_runtime_app(project, tmp_path, monkeypatch, llm)
    _write_role(tmp_path / "home" / "agents", "scout", "MARKER-侦察员。")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        sid = client.post("/api/sessions", json={"title": "t", "cwd": str(project)}).json()["id"]
        _run_turn(client, sid, "scout", "r1")
        _run_turn(client, sid, None, "r2")

    assert "MARKER-侦察员。" in llm.systems[0], "钉住的角色没进系统提示词"
    assert "MARKER-侦察员。" not in llm.systems[1], "切回 auto 之后角色还在(状态没被更新)"


def test_pinned_project_agent_needs_trust(tmp_path, monkeypatch):
    """项目级角色 = 仓库控制的提示词 → 项目**未被信任**时不注入。

    与 qi-agents 里 `--ext agent=<项目角色>` 的那条闸门是同一条规矩;
    web 的"钉住"是本前端自己的入口,所以这条闸门也得在这一侧有。
    """
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    llm = _RecordingLLM()
    app = _shared_runtime_app(project, tmp_path, monkeypatch, llm, approve=False)
    _write_role(project / ".qi" / "agents", "evil", "MARKER-项目角色。")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        sid = client.post("/api/sessions", json={"title": "t", "cwd": str(project)}).json()["id"]
        _run_turn(client, sid, "evil", "r1")

    assert llm.systems, "这一轮没跑起来"
    assert "MARKER-项目角色。" not in llm.systems[0]


def test_pinned_project_agent_is_used_once_trusted(tmp_path, monkeypatch):
    """信任之后就照常生效 —— 闸门不能反过来把正常用法也挡掉。"""
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    llm = _RecordingLLM()
    app = _shared_runtime_app(project, tmp_path, monkeypatch, llm, approve=True)
    _write_role(project / ".qi" / "agents", "reviewer", "MARKER-项目审查员。")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        sid = client.post("/api/sessions", json={"title": "t", "cwd": str(project)}).json()["id"]
        _run_turn(client, sid, "reviewer", "r1")

    assert "MARKER-项目审查员。" in llm.systems[0]
