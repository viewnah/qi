"""在对话页面上钉一个**远端 A2A 角色**(`pin_role` 处理不了的那一支)。

背景:`pin_role` 的做法是把角色正文拼进系统提示词。远端角色**没有本地正文**
(`role.prompt == ""`),所以以前选了它等于什么都没做 —— chip 显示着 `skill-debug`,
实际跑的却是基座模型。那是一个**会说谎的控件**。

这里的用例钉住分流与它的三个副作用:
1. 整轮转发给远端(不是拼提示词);
2. user / assistant 两条消息都进会话文件(否则刷新后看不到这轮);
3. 帧仍然是 `TextMessageStart→Content→End + RUN_FINISHED`(前端不需要写特例)。
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

YES = {"X-Qi-Confirm": "yes"}

CARD = {
    "name": "技能调试",
    "description": "调技能",
    "version": "1.0.0",
    "capabilities": {"streaming": True},
    "defaultInputModes": ["text"],
    "defaultOutputModes": ["text"],
    "skills": [],
    "supportedInterfaces": [{"url": "http://gw/a2a", "protocolBinding": "JSONRPC",
                             "protocolVersion": "1.0", "tenant": "agent-1"}],
}


def _env(tmp_path: Path, monkeypatch) -> Path:
    """项目目录 + 一份**可用的**全局配置(建会话要默认模型)。"""
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    return project


def _a2a(home: Path) -> None:
    (home / "a2a.json").write_text(json.dumps({
        "a2aServers": {
            "remote-one": {"cardUrl": "http://gw/a2a/agents/a1/card.json",
                           "description": "远端的一个角色"},
            "gw": {"baseUrl": "http://gw"},          # 网关:不是角色
        }
    }, ensure_ascii=False), encoding="utf-8")


def _client(project: Path) -> TestClient:
    from qi_lab.app import create_app

    app = create_app(cwd=project, password=None, allowed_hosts=[], bind_host="127.0.0.1")
    return TestClient(app, base_url="http://127.0.0.1")


# ── 分流判据 ────────────────────────────────────────────

def test_remote_role_is_recognised(tmp_path, monkeypatch, minimal_config):
    project = _env(tmp_path, monkeypatch)
    _a2a(minimal_config)
    from qi_lab.serve import remote_role_for

    found = remote_role_for(project, "remote-one")
    assert found is not None
    assert found.name == "remote-one"


def test_gateway_and_unknown_names_are_not_remote_roles(tmp_path, monkeypatch,
                                                        minimal_config):
    project = _env(tmp_path, monkeypatch)
    _a2a(minimal_config)
    from qi_lab.serve import remote_role_for

    assert remote_role_for(project, "gw") is None        # 网关不是角色
    assert remote_role_for(project, "nope") is None
    assert remote_role_for(project, "") is None


def test_cwd_may_be_a_string(tmp_path, monkeypatch, minimal_config):
    """`session.cwd` 存的是字符串 —— 角色发现那一层要 `Path`。"""
    project = _env(tmp_path, monkeypatch)
    _a2a(minimal_config)
    from qi_lab.serve import remote_role_for

    assert remote_role_for(str(project), "remote-one") is not None


def test_agents_endpoint_lists_the_remote_role(tmp_path, monkeypatch, minimal_config):
    """界面能选它,是这条路径存在的前提 —— 两边必须是同一份数据。"""
    project = _env(tmp_path, monkeypatch)
    _a2a(minimal_config)
    agents = _client(project).get("/api/agents").json()["agents"]
    remote = [a for a in agents if a["source"] == "a2a"]
    assert [a["name"] for a in remote] == ["remote-one"]


# ── 一整轮 ──────────────────────────────────────────────

def _fake_remote(monkeypatch, *, text: str = "", ok: bool = True,
                 error: str = "", steps: list[str] | None = None) -> None:
    """把 `qi_a2a.remote.run_remote` 换成假的:这里测的是**会话与帧**,
    不是 A2A 协议(那在 qi-a2a 自己的用例里)。"""
    from qi_a2a import remote as remote_mod

    class Run:
        def __init__(self) -> None:
            self.ok = ok
            self.text = text
            self.error = error
            self.steps = steps or ["· 假远端"]
            self.task_id = "t1"
            self.context_id = "c1"

    async def fake(_agent, _task, **_kwargs):
        return Run()

    monkeypatch.setattr(remote_mod, "run_remote", fake)


def _session(client: TestClient, project: Path) -> dict:
    """建一个会话并返回它的摘要(测试用:直接打接口,不碰内部结构)。"""
    response = client.post("/api/sessions", json={"title": "远端测试",
                                                  "cwd": str(project)})
    assert response.status_code == 201, response.text
    return response.json()


def _stream(client: TestClient, session_id: str, agent: str) -> list[dict]:
    """跑一轮,把 SSE 帧解回 dict 列表。"""
    response = client.post("/api/ag-ui", json={
        "threadId": session_id, "runId": "r1",
        "messages": [{"role": "user", "content": "帮我看看"}],
        "forwardedProps": {"agent": agent},
    })
    assert response.status_code == 200, response.text
    frames = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: "):]))
    return frames


def test_remote_turn_streams_a_complete_message(tmp_path, monkeypatch, minimal_config):
    project = _env(tmp_path, monkeypatch)
    _a2a(minimal_config)
    _fake_remote(monkeypatch, text="远端答的")
    client = _client(project)
    session = _session(client, project)

    frames = _stream(client, session["id"], "remote-one")
    types = [f["type"] for f in frames]

    assert "TEXT_MESSAGE_START" in types
    assert "TEXT_MESSAGE_CONTENT" in types
    assert "TEXT_MESSAGE_END" in types
    assert types[-1] == "RUN_FINISHED"
    content = next(f for f in frames if f["type"] == "TEXT_MESSAGE_CONTENT")
    assert content["delta"] == "远端答的"
    assert next(f for f in frames if f["type"] == "RUN_FINISHED")["result"] == "远端答的"


def test_remote_turn_persists_both_messages(tmp_path, monkeypatch, minimal_config):
    """user 与 assistant 都要落盘:否则刷新后这一轮只剩"我说了什么"或整个消失。"""
    project = _env(tmp_path, monkeypatch)
    _a2a(minimal_config)
    _fake_remote(monkeypatch, text="远端答的")
    client = _client(project)
    session = _session(client, project)

    _stream(client, session["id"], "remote-one")

    detail = client.get(f"/api/sessions/{session['id']}").json()
    roles = [(e.get("role"), e.get("content"), e.get("agent_id"))
             for e in detail["entries"]
             if e.get("type") == "message"]
    assert ("user", "帮我看看", "remote-one") in roles
    assert ("assistant", "远端答的", "remote-one") in roles


def test_remote_failure_becomes_run_error(tmp_path, monkeypatch, minimal_config):
    """远端连不上 → `RUN_ERROR` 帧,而不是 500 / 一个空回合。"""
    project = _env(tmp_path, monkeypatch)
    _a2a(minimal_config)
    _fake_remote(monkeypatch, ok=False, error="条目 `remote-one`:连不上")
    client = _client(project)
    session = _session(client, project)

    frames = _stream(client, session["id"], "remote-one")
    errors = [f for f in frames if f["type"] == "RUN_ERROR"]

    assert errors, [f["type"] for f in frames]
    assert "连不上" in errors[0]["message"]


def test_local_role_still_goes_through_pin_role(tmp_path, monkeypatch, minimal_config):
    """本地角色那条路不受影响:它不会被认成远端。

    **只验判据,不跑整轮** —— 本地角色要真起 runner 打 LLM,那是 `pin_role` 与
    `test_lab_*` 既有用例的事;这里只需要证明"同一份角色表里,本地的那条不会
    被分流到远端"。跑整轮会让这个用例依赖网络与模型配置(实测 ~50s 的失败重试)。
    """
    _env(tmp_path, monkeypatch)
    _a2a(minimal_config)
    role_dir = minimal_config / "agents" / "local-one"
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "agent.md").write_text(
        "---\nname: local-one\ndescription: 本地角色\n---\n你是 local-one。",
        encoding="utf-8")

    from qi_lab.serve import remote_role_for

    assert remote_role_for(minimal_config, "local-one") is None
    assert remote_role_for(minimal_config, "remote-one") is not None
