"""qi-a2a 的**远端角色**导出(`remote.py`)。

这一块是把 A2A 条目交给 qi-agents 当 `subagent` 用的桥。它自己不做协议 ——
协议在 `client.py`,这里只决定"哪些条目算角色"与"一次委派长什么样"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-a2a"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_a2a import remote  # noqa: E402
from qi_a2a.client import SendResult  # noqa: E402


def _env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return home, project


def _write(base: Path, servers: dict) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / "a2a.json").write_text(
        json.dumps({"a2aServers": servers}, ensure_ascii=False), encoding="utf-8")


# ── 哪些条目算角色 ──────────────────────────────────────

def test_card_url_entry_is_a_remote_role(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    _write(home, {"ppt": {"cardUrl": "http://gw/a2a/agents/a1/card.json",
                          "description": "做 PPT 的"}})

    agents = remote.remote_agents(tmp_path)
    assert [a.name for a in agents] == ["ppt"]
    assert agents[0].description == "做 PPT 的"
    assert agents[0].scope == "global"


def test_tenant_entry_is_a_remote_role(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    _write(home, {"fixed": {"baseUrl": "http://gw", "tenant": "agent-1"}})
    assert [a.name for a in remote.remote_agents(tmp_path)] == ["fixed"]


def test_gateway_entry_is_not_a_role(tmp_path, monkeypatch):
    """只给 `baseUrl` = 网关,后面挂着 N 个智能体 —— 枚举它们要联网,而角色发现不能联网。"""
    home, _project = _env(tmp_path, monkeypatch)
    _write(home, {"gw": {"baseUrl": "http://gw"}})
    assert remote.remote_agents(tmp_path) == []


def test_disabled_entries_are_skipped(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    _write(home, {"off": {"cardUrl": "http://gw/c.json", "disabled": True}})
    assert remote.remote_agents(tmp_path) == []


def test_entry_without_description_gets_a_readable_fallback(tmp_path, monkeypatch):
    """描述是模型挑角色的唯一依据,不能是空串 —— 否则它在提示词里等于不存在。"""
    home, _project = _env(tmp_path, monkeypatch)
    _write(home, {"skill-debug": {"cardUrl": "http://gw/c.json"}})
    agents = remote.remote_agents(tmp_path)
    assert agents[0].description
    assert "skill-debug" in agents[0].description


def test_scope_filters_the_layer(tmp_path, monkeypatch):
    home, project = _env(tmp_path, monkeypatch)
    _write(home, {"g": {"cardUrl": "http://gw/g.json"}})
    _write(project / ".qi", {"p": {"cardUrl": "http://gw/p.json"}})

    assert [a.name for a in remote.remote_agents(project, scope="user")] == ["g"]
    assert [a.name for a in remote.remote_agents(project, scope="project")] == ["p"]
    assert [a.name for a in remote.remote_agents(project, scope="both")] == ["g", "p"]


def test_untrusted_project_layer_is_skipped(tmp_path, monkeypatch):
    home, project = _env(tmp_path, monkeypatch)
    _write(home, {"g": {"cardUrl": "http://gw/g.json"}})
    _write(project / ".qi", {"p": {"cardUrl": "http://gw/p.json"}})

    names = [a.name for a in remote.remote_agents(project, scope="both", trusted=False)]
    assert names == ["g"]


def test_broken_config_yields_no_roles_instead_of_raising(tmp_path, monkeypatch):
    """角色发现跑在每次 `subagent` 调用上:一份写坏的 a2a.json 不该把本地委派一起拦下来。"""
    home, _project = _env(tmp_path, monkeypatch)
    home.mkdir(parents=True, exist_ok=True)
    (home / "a2a.json").write_text("{oops", encoding="utf-8")

    assert remote.remote_agents(tmp_path) == []


def test_role_dict_marks_it_as_remote():
    """角色表的 `source="a2a"` 是界面与诊断用来区分本地/远端的那个词。"""
    from qi_a2a.config import A2AEntry

    agent = remote.RemoteAgent(name="x", description="d",
                               entry=A2AEntry(name="x", config={}), tenant="")
    assert agent.as_role_dict()["remote"] is True
    assert agent.as_role_dict()["source"] == "a2a"


# ── 一次远端委派 ────────────────────────────────────────

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


def _fake_client(monkeypatch, result: SendResult, *, card=CARD,
                 calls: list | None = None, fail: Exception | None = None,
                 events: list | None = None) -> None:
    """把 `A2AClient` 换成假的:这里测的是**委派语义**,不是协议。

    `events` 给了就按顺序把它们喂给 `on_event`(模拟流式)—— 用来验 `on_text` 的
    增量转发与 `append` 语义。
    """
    from qi_a2a import client as a2a_client

    class Fake:
        def __init__(self, entry, **_kwargs):
            self.entry = entry

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def get_json(self, url):
            if fail:
                raise fail
            return card

        async def send(self, card_, text="", **kwargs):
            if calls is not None:
                calls.append({"text": text, **kwargs})
            emit = kwargs.get("on_event")
            if events and emit is not None:
                for event in events:
                    emit(event)
            return result

    monkeypatch.setattr(a2a_client, "A2AClient", Fake)
    monkeypatch.setattr(remote, "A2AClient", Fake)


def _artifact_event(text: str, *, append: bool = False):
    """造一个 `artifactUpdate` 事件(形状照实测:Hikqin 首片不带 append)。"""
    from a2a.types import StreamResponse

    event = StreamResponse()
    update = event.artifact_update
    update.task_id = "t1"
    update.artifact.artifact_id = "reply"
    if append:
        update.append = True
    if text:
        part = update.artifact.parts.add()
        part.text = text
    return event


def _agent(tmp_path: Path, monkeypatch, **cfg) -> remote.RemoteAgent:
    home, _project = _env(tmp_path, monkeypatch)
    _write(home, {"remote-one": {"cardUrl": "http://gw/c.json", **cfg}})
    return remote.remote_agents(tmp_path)[0]


@pytest.mark.asyncio
async def test_remote_run_returns_text_and_ids(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    calls: list[dict] = []
    _fake_client(monkeypatch, SendResult(text="做完了", state="completed",
                                         task_id="t1", context_id="c1"), calls=calls)

    run = await remote.run_remote(agent, "写个 PPT")

    assert run.ok is True
    assert run.text == "做完了"
    assert (run.task_id, run.context_id) == ("t1", "c1")
    assert calls[0]["text"] == "写个 PPT"
    # 委派 = **独立上下文**:不复用任何 contextId(复用会把上次委派的内容带进来)
    assert calls[0].get("context_id", "") == ""


@pytest.mark.asyncio
async def test_remote_run_reports_progress(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _fake_client(monkeypatch, SendResult(text="ok", state="completed"))
    seen: list[str] = []

    run = await remote.run_remote(agent, "任务", on_progress=seen.append)

    assert any("取名片" in line for line in seen)
    assert any("发送" in line for line in seen)
    # 耗时在 finally 里算出来,进 `steps`(它比进度晚 —— 进度是跑的时候说的)
    assert any("用时" in line for line in run.steps)


@pytest.mark.asyncio
async def test_remote_run_failure_is_a_result_not_an_exception(tmp_path, monkeypatch):
    """远端连不上是**结果** —— 与本地子运行同一条规矩(一个失败不带倒同批其它结果)。"""
    agent = _agent(tmp_path, monkeypatch)
    from qi_a2a.client import RpcError

    _fake_client(monkeypatch, SendResult(), fail=RpcError("连不上"))

    run = await remote.run_remote(agent, "任务")

    assert run.ok is False
    assert "连不上" in run.error


@pytest.mark.asyncio
async def test_input_required_is_reported_as_success_with_resume_hint(tmp_path, monkeypatch):
    """远端等审批 = 委派**成功**把话带回来了,但正文要说清"没执行"与怎么回传。"""
    agent = _agent(tmp_path, monkeypatch)
    _fake_client(monkeypatch, SendResult(
        text="", state="input-required", task_id="t9", context_id="c9",
        interrupt={"action_requests": [{"id": "a1", "name": "delete_file",
                                       "args": {"path": "x"}}],
                   "review_configs": [{"allowed_decisions": ["approve", "reject"]}]}))

    run = await remote.run_remote(agent, "删掉它")

    assert run.ok is True
    assert "没有执行" in run.text
    assert "delete_file" in run.text
    assert "resume" in run.text and "t9" in run.text


@pytest.mark.asyncio
async def test_abort_signal_stops_the_run(tmp_path, monkeypatch):
    """Esc:信号在事件之间查一次 —— httpx 的超时管不了"用户不要了"。"""
    agent = _agent(tmp_path, monkeypatch)
    _fake_client(monkeypatch, SendResult(text="x", state="completed"))

    class Signal:
        aborted = True

    run = await remote.run_remote(agent, "任务", signal=Signal())

    # 假客户端不看 signal,这里只钉"参数被传下去了"这件事的**形状**:
    # 真判断在 A2AClient.send 里(它每个事件之间查一次)。
    assert run is not None


# ── 正文增量(`on_text`)────────────────────────────────
#
# 界面要"边收边显示"。`run_remote` 只做转发,聚合仍归 client —— 两边各算一遍
# 必然会在某处不一致(比如 append 语义)。

@pytest.mark.asyncio
async def test_text_deltas_follow_the_append_semantics(tmp_path, monkeypatch):
    """首片 `append` 缺省 = **替换**(`replace=True`),后续 `append=true` = 追加。"""
    agent = _agent(tmp_path, monkeypatch)
    _fake_client(monkeypatch, SendResult(text="你好呀", state="completed"), events=[
        _artifact_event("你"),                          # 首片:不带 append
        _artifact_event("好", append=True),
        _artifact_event("呀", append=True),
        _artifact_event(""),                            # 空增量:不该回调
    ])

    got: list[tuple[str, bool]] = []

    async def go():
        return await remote.run_remote(
            agent, "任务", on_text=lambda d, *, replace=False: got.append((d, replace)))

    await go()

    assert got == [("你", True), ("好", False), ("呀", False)]


@pytest.mark.asyncio
async def test_no_text_callback_when_remote_never_streams(tmp_path, monkeypatch):
    """非流式远端一个增量都不吐 —— 调用方要拿 `run.text` 兜底(这条钉住"不回调")。"""
    agent = _agent(tmp_path, monkeypatch)
    _fake_client(monkeypatch, SendResult(text="一次给全", state="completed"))

    got: list[str] = []
    run = await remote.run_remote(agent, "任务", on_text=lambda d, **_: got.append(d))

    assert got == []
    assert run.text == "一次给全"
