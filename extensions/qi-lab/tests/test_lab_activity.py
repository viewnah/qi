"""委派直播(design/web-lab.md 二期):qi-agents → `tool_execution_update` → `ACTIVITY_SNAPSHOT`。

三层各钉一层:

1. **内容提取**(纯函数):认 `qi` 信封,不认识的进度事件一律 `None` —— 同一条事件上
   还跑着别的扩展的 `on_update`,过滤不严就会把工具进度当成委派直播;
2. **翻译与混流**(纯函数):一个子 run 一条**稳定**活动消息(`replace: true` 覆盖更新);
   队列 FIFO 是"卡先到、活动后到"的保证;
3. **端到端**:真 app、真流 —— LLM 开跑时往 handler 塞一帧,`data:` 行里必须出现
   `ACTIVITY_SNAPSHOT`(订阅、广播、消费、组帧全链路)。

注入点刻意选在 `astream` 里:那才是事件循环线程。从测试线程 `put_nowait` 到
`asyncio.Queue` 上是跨线程唤醒 getter —— 不安全,而且"测试里偶发才坏"最贵。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from qi_lab import activity  # noqa: E402
from qi_lab.agui import AguiTranslator, mixed_frames  # noqa: E402
from qi_lab.state import WebState  # noqa: E402

# qi-agents 真发的信封(`subagent._web`)加 runner 的外层(见 runner._on_update)
PAYLOAD = {
    "tool_call_id": "call_1",
    "tool_name": "subagent",
    "args": {"tasks": []},
    "partial_result": {"qi": {"kind": "subagent", "phase": "act", "run": "scout#1",
                              "role": "scout", "parent": "qi", "depth": 1,
                              "task": "读一遍 sources.py", "line": '· read {"path": "a.py"}'}},
}


# ── 1. 内容提取(过滤)─────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    None, "不是 dict", [], 42,                       # 根本不是信封
    {},                                              # 没有 partial_result
    {"partial_result": "字符串"},                     # partial_result 不是 dict
    {"partial_result": {"text": "别的扩展的进度"}},     # 没有 qi 信封
    {"partial_result": {"qi": {"kind": "mcp", "phase": "act"}}},   # kind 不是 subagent
    {"partial_result": {"qi": {"kind": "subagent", "phase": "think"}}},   # 没有 run
    {"partial_result": {"qi": {"kind": "subagent", "phase": "weird", "run": "x"}}},  # 未知阶段
    {"partial_result": {"qi": {"kind": "subagent", "phase": "act", "run": ""}}},      # 空 run
    {"partial_result": {"qi": {"kind": "subagent", "phase": "act", "run": 7}}},       # run 非字符串
])
def test_activity_content_filters_everything_not_ours(payload):
    """与委派无关 / 形状不对的进度一律 None —— 过滤器是这层的全部职责。"""
    assert activity.activity_content(payload) is None


def test_activity_content_carries_the_envelope_and_call_id():
    """正常信封:原样留字段 + 把 `tool_call_id` 抬进 content(前端归卡的主键)。"""
    content = activity.activity_content(PAYLOAD)
    assert content is not None
    assert content["run"] == "scout#1" and content["phase"] == "act"
    assert content["depth"] == 1 and content["line"] == '· read {"path": "a.py"}'
    assert content["toolCallId"] == "call_1"
    assert content["kind"] == "subagent", "信封的 kind 留着:前端还要再认一次"


def test_activity_content_without_call_id_still_works():
    """没有 `tool_call_id`(非工具路径)也给 content:帧照出,只是前端归不了卡。"""
    content = activity.activity_content(
        {"partial_result": PAYLOAD["partial_result"]})
    assert content is not None and "toolCallId" not in content


# ── 2. 翻译与混流 ────────────────────────────────────────────

def test_feed_activity_shape_is_stable_per_run():
    """一个 run 一条稳定活动消息:messageId 只由 run 名决定,`replace` 覆盖更新。"""
    tr = AguiTranslator(thread_id="t1", run_id="r1")
    content = activity.activity_content(PAYLOAD)
    frames = tr.feed_activity(content)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["type"] == "ACTIVITY_SNAPSHOT"
    assert frame["messageId"] == "qi-act-scout#1", "同一 run 的多帧必须是同一条消息"
    assert frame["activityType"] == activity.ACTIVITY_TYPE == "qi.subagent"
    assert frame["subagentRunId"] == "scout#1"
    assert frame["replace"] is True, "帧是该 run 活动的最新全量,不是追加"
    assert "threadId" not in frame and "runId" not in frame, \
        "与 qi 其余流帧同形:threadId/runId 只在 RUN_STARTED 上(见 translator.start)"
    assert frame["content"] == content


def test_feed_activity_without_run_emits_nothing():
    """没 run 名就没有消息身份 —— 宁可不发,也不发一条客户端无法覆盖的帧。"""
    tr = AguiTranslator(thread_id="t", run_id="r")
    assert tr.feed_activity({"phase": "act"}) == []
    assert tr.feed_activity({"run": ""}) == []


def _qi_content(**over: object) -> dict:
    """qi-agents 真发的 content(缺省 = scout 的一条活动帧)。"""
    c: dict = {"kind": "subagent", "run": "scout#1", "role": "scout",
               "parent": "qi", "depth": 1, "task": "读一遍",
               "toolCallId": "call_1", "phase": "act"}
    c.update(over)
    return c


def _envelope(qi: dict) -> dict:
    """qi-agents 推送 + runner 外层的完整 payload 形状。"""
    return {"tool_call_id": "call_1", "partial_result": {"qi": qi}}


def test_start_becomes_a_native_subagent_started():
    """生:**AG-UI 原生的 `SUBAGENT_STARTED`**(不是我们自造的类型)。"""
    tr = AguiTranslator(thread_id="t", run_id="r")
    (frame,) = tr.feed_activity(_qi_content(phase="start"))
    assert frame["type"] == "SUBAGENT_STARTED"
    assert frame["subagentRunId"] == "scout#1" and frame["name"] == "scout"
    assert frame["description"] == "读一遍", "子任务进 description"
    assert frame["parentToolCallId"] == "call_1", "指回发起它的那张工具卡"
    assert frame["metadata"] == {"toolCallId": "call_1", "depth": 1,
                                 "parent": "qi"}, "深度与父:展示要用"


def test_only_nested_runs_carry_a_parent_run_id():
    """顶层由**主 agent** 派出 —— 主 agent 不是子运行,不发 parentSubagentRunId。"""
    tr = AguiTranslator(thread_id="t", run_id="r")
    (top,) = tr.feed_activity(_qi_content(phase="start", depth=1))
    assert "parentSubagentRunId" not in top
    (nested,) = tr.feed_activity(
        _qi_content(phase="start", depth=2, parent="coordinator"))
    assert nested["parentSubagentRunId"] == "coordinator"


def test_done_becomes_finished_or_error():
    """死:成功 `SUBAGENT_FINISHED`,失败 `SUBAGENT_ERROR` —— 都拿 metadata 归卡。"""
    tr = AguiTranslator(thread_id="t", run_id="r")
    (ok,) = tr.feed_activity(_qi_content(phase="done", ok=True))
    assert ok["type"] == "SUBAGENT_FINISHED" and ok["outcome"] == {"type": "success"}
    assert ok["metadata"]["toolCallId"] == "call_1", \
        "FINISHED 在协议里没有 parentToolCallId:归卡靠 metadata"

    (bad,) = tr.feed_activity(
        _qi_content(phase="done", ok=False, error="子运行失败"))
    assert bad["type"] == "SUBAGENT_ERROR"
    assert bad["message"] == "子运行失败" and bad["code"] == "subagent_failed"
    assert bad["metadata"]["toolCallId"] == "call_1"


class _Feed:
    """假队列:与 `WebState` 的消息形状一致 `(kind, payload)`。"""

    def __init__(self, items):
        self._items = list(items)

    async def get(self):
        return self._items.pop(0)


@pytest.mark.asyncio
async def test_mixed_frames_translates_activity_and_keeps_fifo_order():
    """混流:直播内容翻译成帧,主流帧原样过,顺序 = 入队顺序。"""
    tr = AguiTranslator(thread_id="t", run_id="r")
    content = activity.activity_content(PAYLOAD)
    feed = _Feed([
        (activity.KIND_FRAME, {"type": "TOOL_CALL_START", "toolCallId": "call_1"}),
        (activity.KIND_ACTIVITY, content),
        (activity.KIND_FRAME, {"type": "TOOL_CALL_RESULT", "toolCallId": "call_1"}),
        (activity.KIND_END, None),
    ])
    out = [frame async for frame in mixed_frames(feed, tr)]
    assert [f["type"] for f in out] == [
        "TOOL_CALL_START", "ACTIVITY_SNAPSHOT", "TOOL_CALL_RESULT"], \
        "卡帧在前、活动在后 —— 顺序错了客户端就找不到归卡的行"
    assert out[1]["content"]["run"] == "scout#1"


@pytest.mark.asyncio
async def test_mixed_frames_ends_at_the_sentinel():
    """哨兵之后一帧都不出(pump 收工)。"""
    tr = AguiTranslator(thread_id="t", run_id="r")
    feed = _Feed([(activity.KIND_END, None),
                  (activity.KIND_ACTIVITY, {"run": "late"})])
    assert [f async for f in mixed_frames(feed, tr)] == []


# ── 3. 状态层:广播与订阅 ─────────────────────────────────────

def test_broadcast_reaches_only_open_queues():
    """开两条队列都收到;关掉的那条不再收(泄漏的队列会越推越多)。"""
    state = WebState(Path("/tmp"), runtime_factory=lambda cwd: None)
    kept, closed = state.open_activity(), state.open_activity()
    state._on_activity(PAYLOAD, None)
    assert kept.get_nowait() == (activity.KIND_ACTIVITY,
                                 activity.activity_content(PAYLOAD))
    assert closed.get_nowait()[0] == activity.KIND_ACTIVITY
    state.close_activity(closed)
    state.close_activity(closed)          # 幂等:重复收不炸
    state._on_activity(PAYLOAD, None)
    assert kept.get_nowait()[0] == activity.KIND_ACTIVITY
    assert closed.empty(), "已收掉的队列不该再收到"


def test_broadcast_skips_unrelated_payload_and_silent_state():
    """没有活跃流时不构造内容;无关进度也不投。"""
    state = WebState(Path("/tmp"), runtime_factory=lambda cwd: None)
    state._on_activity(PAYLOAD, None)                    # 没开队列:静默跳过
    queue = state.open_activity()
    state._on_activity({"partial_result": {"text": "别人家的进度"}}, None)
    assert queue.empty()


class _FakeRuntime:
    def __init__(self):
        from qi_agent.extensions import ExtensionBus

        self.bus = ExtensionBus()


def test_runtime_subscribes_activity_handler_exactly_once():
    """每个 runtime 只挂一次订阅(总线没有 `off`,重复挂 = 每帧翻倍)。"""
    created = []

    def factory(cwd):
        runtime = _FakeRuntime()
        created.append(runtime)
        return runtime

    state = WebState(Path("/tmp"), runtime_factory=factory)
    first = state.runtime_for("/tmp/a")
    again = state.runtime_for("/tmp/a")
    other = state.runtime_for("/tmp/b")
    assert first is again
    assert first.bus._handlers["tool_execution_update"] == [
        ("qi-lab", state._on_activity)], "同一 runtime 挂两次会把帧翻倍"
    assert other.bus._handlers["tool_execution_update"] == [
        ("qi-lab", state._on_activity)], "换个 cwd 的 runtime 也要有自己的订阅"


# ── 4. 端到端:帧真的出现在 AG-UI 流里 ─────────────────────────

class _ActivityLLM:
    """最小流式 LLM:开跑时经**事件循环线程**把直播内容塞进 handler。

    第一次 `astream` 时注入 —— 那一刻 gen 已经 `open_activity()`(首帧之前),
    所以注入一定落在活跃队列上。
    """

    def __init__(self) -> None:
        self.inject = None            # 由测试延迟绑定(app 建好之后)

    async def astream(self, messages, tools=None, temperature=None):
        from qi_agent.llm import LLMDelta

        if self.inject is not None:
            self.inject, injected = None, self.inject
            injected()
        yield LLMDelta(text="好")
        yield LLMDelta(finished=True, usage={"total_tokens": 1})


def test_activity_snapshot_arrives_in_the_stream(tmp_path, monkeypatch):
    """全链路:订阅挂上 → 流进行中的一帧 → `data:` 行里出现 ACTIVITY_SNAPSHOT。"""
    from qi_agent import paths
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore
    from qi_lab.app import create_app
    from qi_lab.state import WebState

    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (tmp_path / "models.json").write_text(
        '{"providers": {"ollama": {"api": "openai-completions", '
        '"models": [{"id": "x"}]}}}', encoding="utf-8")
    (home / "settings.json").write_text(
        '{"defaultProvider": "ollama", "defaultModel": "x"}', encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))

    llm = _ActivityLLM()
    store = SessionStore(root=tmp_path / "sessions")
    state = WebState(project, runtime_factory=lambda cwd: QiRuntime(
        cwd=Path(cwd), runtime_cfg=RuntimeConfig(workdir=Path(cwd)),
        session_store=store, llm=llm))
    app = create_app(cwd=project, state=state, allowed_hosts=[], bind_host="127.0.0.1")
    # runtime_for 在 gen 开头被调 → 订阅那时已挂(这里再确认一次,不用等流)
    assert state.runtime_for(project).bus.has("tool_execution_update")
    llm.inject = lambda: [
        state._on_activity(p, None)
        for p in (_envelope(_qi_content(phase="start")),
                  PAYLOAD,
                  _envelope(_qi_content(phase="done", ok=True)))
    ]

    with TestClient(app, base_url="http://127.0.0.1") as client:
        sid = client.post("/api/sessions", json={"title": "t",
                                                 "cwd": str(project)}).json()["id"]
        frames = []
        with client.stream("POST", "/api/ag-ui", json={
                "threadId": sid, "runId": "r1",
                "messages": [{"role": "user", "content": "你好"}]}) as resp:
            assert resp.status_code == 200, resp.read()
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[len("data: "):]))

    kinds = [f.get("type") for f in frames]
    assert "RUN_STARTED" in kinds and "RUN_FINISHED" in kinds, kinds
    for want in ("SUBAGENT_STARTED", "ACTIVITY_SNAPSHOT", "SUBAGENT_FINISHED"):
        assert want in kinds, kinds
        assert kinds.index("RUN_STARTED") < kinds.index(want) < kinds.index("RUN_FINISHED"), \
            "直播帧必须落在流里(既不是流外,也不是流后)"
    assert (kinds.index("SUBAGENT_STARTED") < kinds.index("ACTIVITY_SNAPSHOT")
            < kinds.index("SUBAGENT_FINISHED")), "生 → 内容 → 死:单队列 FIFO 保证"
    start = frames[kinds.index("SUBAGENT_STARTED")]
    assert start["name"] == "scout" and start["parentToolCallId"] == "call_1"
    finish = frames[kinds.index("SUBAGENT_FINISHED")]
    assert finish["subagentRunId"] == "scout#1" and finish["outcome"] == {"type": "success"}
    snap = frames[kinds.index("ACTIVITY_SNAPSHOT")]
    assert snap["activityType"] == "qi.subagent"
    assert snap["content"]["run"] == "scout#1" and snap["content"]["toolCallId"] == "call_1"
    assert snap["messageId"] == "qi-act-scout#1" and snap["replace"] is True
    assert not state._activity, "流结束后队列必须收掉(泄漏会让广播越推越多)"
