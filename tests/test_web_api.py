"""qi-web API 契约测试(离线:stub LLM + 临时 QI_AGENT_HOME,不起真服务)。

用 `httpx.ASGITransport` 在进程内直连 ASGI app,因此:
  * 不需要端口、不受本机其他服务影响;
  * SSE 也走真实路径(StreamingResponse),不是伪造的字节流。

覆盖:契约号 / 会话增删改查 / **AG-UI 线格式 + 事件序列 + 落盘回放** / 并发 409 /
输入校验与旧路由已移除 / 凭证写入的二次确认闸门 / 口令保护 / Host 允许表 / 只读配置掩码。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from qi_agent import paths
from qi_agent.llm import LLMDelta, ToolCallOut
from qi_agent.runtime import QiRuntime, RuntimeConfig
from qi_agent.session import SessionStore
from qi_web.app import create_app
from qi_web.schemas import CONTRACT_VERSION
from qi_web.security import is_loopback, mask_key, require_safe_config
from qi_web.state import WebState
from qi_agent.workspaces import WorkspaceStore


class StreamingStub:
    """第 1 轮:边说边调一个工具;第 2 轮:给结论。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=None):  # pragma: no cover
        raise AssertionError("有 astream 时不应调用 chat")

    async def astream(self, messages, tools=None, temperature=None):
        self.calls += 1
        if self.calls == 1:
            yield LLMDelta(text="我先")
            yield LLMDelta(text="看一下")
            yield LLMDelta(finished=True,
                           tool_calls=[ToolCallOut(id="c1", name="ls", args={"path": "."})],
                           usage={"total_tokens": 5})
            return
        yield LLMDelta(text="完成")
        yield LLMDelta(finished=True, usage={"total_tokens": 7})


class SlowStub(StreamingStub):
    """慢到足以让第二个请求撞上"已有活跃 run"。"""

    async def astream(self, messages, tools=None, temperature=None):
        import asyncio

        await asyncio.sleep(1.5)
        async for delta in super().astream(messages, tools, temperature):
            yield delta


def _minimal_config(base: Path, monkeypatch, provider: str = "ollama") -> None:
    """最小配置。provider 默认 ollama——它属 KEYLESS_PROVIDERS(免密钥),
    大多数用例因此不需要凭证;需要"缺凭证"场景时传 deepseek。"""
    (base / "models.json").write_text(
        json.dumps({"providers": {provider: {"api": "openai-completions",
                                              "models": [{"id": "x"}]}}}), encoding="utf-8")
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": provider, "defaultModel": "x"}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(base / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))


def _web(client) -> WebState:
    """测试内省:拿到 app.state.web(生产前端不需要这个)。"""
    return client._transport.app.state.web          # type: ignore[attr-defined]


def _app(tmp_path: Path, monkeypatch, llm=None, provider: str = "ollama", **kwargs):
    """建 app(不传 password 时无需凭证)。"""
    _minimal_config(tmp_path, monkeypatch, provider=provider)
    sessions = SessionStore(root=tmp_path / "sessions")

    def factory(cwd):
        return QiRuntime(cwd=Path(cwd), runtime_cfg=RuntimeConfig(workdir=Path(cwd)),
                         session_store=sessions, llm=llm or StreamingStub())

    state = WebState(tmp_path, runtime_factory=factory)
    # 工作区偏好落在 tmp:不传的话默认会写进真实 `~/.qi/agent/workspaces.json`
    kwargs.setdefault("workspace_store", WorkspaceStore(tmp_path / "workspaces.json"))
    return create_app(cwd=tmp_path, state=state, **kwargs)


@pytest.fixture
def app(tmp_path, monkeypatch):
    return _app(tmp_path, monkeypatch)


@pytest.fixture
async def client(app, tmp_path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        c.workdir = tmp_path            # type: ignore[attr-defined]
        yield c


def _input(sid: str, text: str = "看下目录", run_id: str = "r1") -> dict:
    """AG-UI 的 RunAgentInput(单次 POST 的 body)。"""
    return {
        "threadId": sid,
        "runId": run_id,
        "messages": [{"role": "user", "content": text}],
        "forwardedProps": {"agent": None},
    }


async def _agui(client, sid: str, text: str = "看下目录",
                run_id: str = "r1", timeout: float = 10.0) -> tuple[list[dict], list[str]]:
    """跑一轮 AG-UI,读完流。返回 (事件序列, 原始行)。

    返回原始行是为了断言**线格式**:AG-UI 的官方编码器只写 `data:`,
    不得出现 `event:` 或 `id:`。
    """
    import asyncio

    events: list[dict] = []
    raw_lines: list[str] = []
    async with asyncio.timeout(timeout):
        async with client.stream("POST", "/api/ag-ui",
                                 json=_input(sid, text, run_id)) as resp:
            assert resp.status_code == 200, resp.text
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if line == "":
                    continue
                raw_lines.append(line)
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    return events, raw_lines


def _kinds(events: list[dict]) -> list[str]:
    return [str(e.get("type")) for e in events]


def _paired(events: list[dict], start: str, end: str, id_key: str) -> None:
    """断言 start/end 成对:每个 start 的 id 都被恰好一个 end 收掉。"""
    open_ids: list[str] = []
    for e in events:
        if e.get("type") == start:
            open_ids.append(str(e.get(id_key)))
        elif e.get("type") == end:
            assert open_ids, f"出现没有 {start} 的 {end}"
            open_ids.remove(str(e.get(id_key)))
    assert open_ids == [], f"这些 {start} 没有 {end}:{open_ids}"


# ── AG-UI:插件的 details 下行通道 ────────────────────────────

def test_tool_details_survive_the_agui_layer():
    """`tool_end.data.details` 必须原样出现在 `TOOL_CALL_RESULT.metadata["qi.tool"]`。

    这是插件 UI 通道的**最后一跳**:runner 已经把 details 放进 data(见
    test_contract_p0 的 test_extension_details_reach_the_event),这里确认 AG-UI
    映射层没有把它丢掉 —— 两处都通,插件才真的能渲染。
    """
    from qi_agent.models import AgentEvent, TOOL_OK
    from qi_web.agui import AguiTranslator

    payload = {"ui": [{"type": "list", "items": [{"label": "写实现", "state": "active"}]}]}
    t = AguiTranslator(thread_id="s", run_id="r")
    t.feed(AgentEvent(kind="tool_start", tool="todo", data={"args": {}}))
    frames = t.feed(AgentEvent(kind="tool_end", tool="todo", text="ok",
                               data={"status": TOOL_OK, "duration_ms": 3,
                                     "exit_code": None, "error": None,
                                     "details": payload}))
    result = next(f for f in frames if f["type"] == "TOOL_CALL_RESULT")
    assert result["metadata"]["qi.tool"]["details"] == payload
    # 结构化结果仍与 status/duration 同处一个 metadata 槽(不是自造新字段)
    assert result["metadata"]["qi.tool"]["status"] == TOOL_OK


# ── AG-UI:线格式 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_agui_wire_format_is_data_only(client, tmp_path):
    """**线格式**:只有 `data:` 行。

    依据是官方编码器(`@ag-ui/encoder` 的 `encodeSSE`):
        `data: ${JSON.stringify(event)}\n\n`
    所以不得出现 SSE 的 `event:` 或 `id:` 字段 —— 事件类型在 JSON 的 `type` 里。
    """
    sid = (await client.post("/api/sessions", json={"title": "线格式", "cwd": str(tmp_path)})).json()["id"]
    _, lines = await _agui(client, sid)
    assert lines, "流是空的"
    assert not [ln for ln in lines if ln.startswith("event:")], "AG-UI 不写 event: 字段"
    assert not [ln for ln in lines if ln.startswith("id:")], "AG-UI 不写 id: 字段(没有续传语义)"
    for ln in lines:
        assert ln.startswith("data: "), f"非法行:{ln!r}"


@pytest.mark.asyncio
async def test_turn_streams_full_event_sequence(client, tmp_path):
    """一轮的完整 AG-UI 事件序列:首 RUN_STARTED,末 RUN_FINISHED,三段式成对。"""
    sid = (await client.post("/api/sessions", json={"title": "序列", "cwd": str(tmp_path)})).json()["id"]
    events, _ = await _agui(client, sid, "看下目录")
    kinds = _kinds(events)

    assert kinds[0] == "RUN_STARTED", f"首帧必须是 RUN_STARTED,实际 {kinds[0]}"
    assert kinds[-1] == "RUN_FINISHED", f"末帧必须是 RUN_FINISHED,实际 {kinds[-1]}"

    # 历史与状态作为权威起点,紧跟 RUN_STARTED
    assert "MESSAGES_SNAPSHOT" in kinds
    assert "STATE_SNAPSHOT" in kinds
    # qi 自己的完整 entry 列表:没有它,刷新后轨迹只剩对话
    assert any(e.get("type") == "CUSTOM" and e.get("name") == "qi.history" for e in events)

    # 分段必须成对,否则客户端会停在"正在流式"的状态
    _paired(events, "TEXT_MESSAGE_START", "TEXT_MESSAGE_END", "messageId")
    _paired(events, "REASONING_START", "REASONING_END", "messageId")
    _paired(events, "TOOL_CALL_START", "TOOL_CALL_END", "toolCallId")

    # 工具三段式:Start 之后必须有 Args(规范要求"一个或多个"),然后是 Result
    assert "TOOL_CALL_ARGS" in kinds and "TOOL_CALL_RESULT" in kinds

    # P-E4c:core 是单 agent、不再分派 → 不再有 `qi.dispatch` 这种 CUSTOM 分派事件。
    # “宿主自定义内容走 CUSTOM、不污染标准事件”这条规矩仍由历史快照守着。
    assert any(e.get("type") == "CUSTOM" and e.get("name") == "qi.history" for e in events)


@pytest.mark.asyncio
async def test_run_finished_carries_outcome_and_usage(client, tmp_path):
    """RUN_FINISHED 必须带 outcome(AG-UI 的生命周期契约),usage 挂在 metadata。"""
    sid = (await client.post("/api/sessions", json={"title": "收尾", "cwd": str(tmp_path)})).json()["id"]
    events, _ = await _agui(client, sid)
    last = events[-1]
    assert last["type"] == "RUN_FINISHED"
    assert last.get("outcome") == {"type": "success"}
    # usage 走 metadata 的 open-by-key 槽,而不是自造字段
    assert "qi.usage" in (last.get("metadata") or {})
    assert last["metadata"]["qi.usage"]["llm_calls"] >= 1


@pytest.mark.asyncio
async def test_turn_result_is_persisted_and_replayed(client, tmp_path):
    """落盘完整,而且**下一轮的 qi.history 能把它带回来**(直播与回放一致)。

    qi 一直守着这条不变量:叙述与工具卡不能只活在直播里。
    """
    sid = (await client.post("/api/sessions", json={"title": "落盘", "cwd": str(tmp_path)})).json()["id"]
    await _agui(client, sid, "看下目录")

    detail = (await client.get(f"/api/sessions/{sid}")).json()
    types = [e.get("type") for e in detail["entries"]]
    # P-E4c:core 不再写 `dispatch` entry(单 agent,无分派)
    assert "message" in types
    assert types.count("message") >= 2          # user + assistant
    assert "tool" in types                      # 工具往返落盘(第五类 entry)
    assert "custom" in types                    # 叙述落成 custom(不进 LLM 上下文)
    assert types[-1] == "message"

    # 第二轮:qi.history 必须带着上一轮的工具卡与叙述
    events, _ = await _agui(client, sid, "再来一轮", run_id="r2")
    hist = next(e for e in events if e.get("type") == "CUSTOM" and e.get("name") == "qi.history")
    replayed = [x.get("type") for x in hist["value"]["entries"]]
    assert "tool" in replayed and "custom" in replayed


@pytest.mark.asyncio
async def test_busy_session_is_serialised(client, tmp_path):
    """同一会话串行:占用中再来一个 POST → 409;占用中不允许删除。

    **为什么在 state 层手工占用、而不是真开两条并发流**:
    单 POST 之下,"忙"的时间窗恰好等于那条响应流的存活期。而
    `httpx.ASGITransport` 会把整个响应体缓冲完再交还,拿不到"已收到响应头、
    身体仍在流"的句柄 —— 所以没法在进程内造出那个时间窗(旧协议能测,
    是因为它 POST 就返回 202、run 在后台任务里跑,窗口与响应无关)。

    这里改为直接验证两件真正要做对的事:闸门本身(同会话两次占用必须拒绝),
    以及端点把闸门翻译成 409。两者都比"靠 sleep 抢时间窗"更确定。
    """
    sid = (await client.post("/api/sessions", json={"title": "并发", "cwd": str(tmp_path)})).json()["id"]
    web = _web(client)

    web.begin(sid)                      # 手工占用 = 另有一个请求正在流
    try:
        busy = await client.post("/api/ag-ui", json=_input(sid))
        assert busy.status_code == 409
        # 运行中不允许删除(否则会删掉正在写的文件)
        assert (await client.delete(f"/api/sessions/{sid}")).status_code == 409
    finally:
        web.end(sid)

    # 释放之后必须能正常跑 —— 否则一次异常就把会话永久卡住
    events, _ = await _agui(client, sid)
    assert events[-1]["type"] == "RUN_FINISHED"


def test_webstate_gate_is_symmetric():
    """闸门语义:重复占用拒绝、释放可重复、释放后可再占用。"""
    import tempfile
    from qi_web.state import RunBusy, WebState

    web = WebState(Path(tempfile.mkdtemp()))
    assert web.is_busy("s") is False
    web.begin("s")
    assert web.is_busy("s") is True
    with pytest.raises(RunBusy):
        web.begin("s")
    web.end("s")
    web.end("s")                        # 幂等:finally 里释放两次不该炸
    assert web.is_busy("s") is False
    web.begin("s")                      # 释放后可以再占


@pytest.mark.asyncio
async def test_run_rejects_bad_input(client, tmp_path):
    """三类输入错误必须是 4xx,而且**不能**开出流。

    改造后取消不再有端点:单 POST 之下"客户端断开"就是取消语义。
    所以这里顺带断言旧路由确实没了(405),免得有人以为它还在。
    """
    sid = (await client.post("/api/sessions", json={"title": "输入", "cwd": str(tmp_path)})).json()["id"]

    empty_thread = await client.post("/api/ag-ui", json={"runId": "r", "messages": []})
    assert empty_thread.status_code == 422

    unknown = await client.post("/api/ag-ui", json=_input("does-not-exist"))
    assert unknown.status_code == 404

    no_user = await client.post("/api/ag-ui", json={
        "threadId": sid, "runId": "r", "messages": [{"role": "assistant", "content": "x"}]})
    assert no_user.status_code == 422

    # 旧的两跳路由与取消端点都已移除
    assert (await client.post(f"/api/sessions/{sid}/cancel")).status_code == 405
    assert (await client.post(f"/api/sessions/{sid}/turn", json={"text": "x"})).status_code == 405


@pytest.mark.asyncio
async def test_auth_write_requires_explicit_confirm(client, tmp_path, monkeypatch):
    """没有 X-Qi-Confirm 头不能写(428);带了才落盘,且响应不回明文。"""
    no_confirm = await client.post("/api/auth/ollama", json={"key": "secret-value-1234"})
    assert no_confirm.status_code == 428
    assert "二次确认" in no_confirm.json()["detail"]

    ok = await client.post("/api/auth/ollama", json={"key": "secret-value-1234"},
                           headers={"X-Qi-Confirm": "yes"})
    assert ok.status_code == 204
    auth_file = Path(paths.global_home()) / "auth.json"
    assert json.loads(auth_file.read_text(encoding="utf-8"))["ollama"]["key"] == "secret-value-1234"

    # 只读配置里只出现掩码
    config = (await client.get("/api/config")).json()
    entry = next(p for p in config["providers"] if p["name"] == "ollama")
    assert entry["credential_ok"] is True
    assert entry["credential_masked"] == "…1234"
    assert "secret-value-1234" not in json.dumps(config)
    # 确认弹层要显示"改的是哪个文件" → 后端必须回凭证文件路径
    assert Path(config["auth_file"]) == auth_file

    assert (await client.delete("/api/auth/ollama")).status_code == 428
    assert (await client.delete("/api/auth/ollama",
                                headers={"X-Qi-Confirm": "yes"})).status_code == 204


@pytest.mark.asyncio
async def test_config_reports_missing_credential_as_a_check(tmp_path, monkeypatch):
    """缺凭证必须作为一条 check 暴露出来(前端据此提示去配模型)。"""
    app = _app(tmp_path, monkeypatch, provider="deepseek")     # 需要密钥的 provider
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        body = (await client.get("/api/config")).json()
        check = next(c for c in body["checks"] if c["name"] == "凭证")
        assert check["ok"] is False and "deepseek" in check["detail"]
        assert body["default_model"] == "deepseek/x"


@pytest.mark.asyncio
async def test_keyless_provider_is_not_reported_as_missing(client):
    """ollama 属免密钥 provider → 不能算"缺凭证"(否则首次运行全是误报)。"""
    body = (await client.get("/api/config")).json()
    check = next(c for c in body["checks"] if c["name"] == "凭证")
    assert check["ok"] is True and check["detail"] == "全部就绪"


# ── agents ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_skills_and_extensions_endpoints(client):
    """设置页要的两块数据:顶层技能清单与已装载插件名。

    扩展只回名字是**故意的**:`discover_extensions()` 的职责是装载而非描述,
    它拿不到版本/作者就不编造。
    """
    skills = (await client.get("/api/skills")).json()
    assert isinstance(skills["skills"], list)
    for skill in skills["skills"]:
        assert set(skill) == {"name", "description", "source", "path"}

    extensions = (await client.get("/api/extensions")).json()
    assert isinstance(extensions["extensions"], list)
    assert all(isinstance(name, str) for name in extensions["extensions"])
# ── 安全 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_password_protects_all_api_routes(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, password="s3cret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        unauth = await client.get("/api/sessions")
        assert unauth.status_code == 401
        assert "Basic" in unauth.headers["www-authenticate"]

        bearer = await client.get("/api/sessions", headers={"Authorization": "Bearer s3cret-token"})
        assert bearer.status_code == 200
        basic = await client.get("/api/sessions", headers={"Authorization": "Basic cWk6czNjcmV0LXRva2Vu"})
        assert basic.status_code == 200                     # base64("qi:s3cret-token")
        wrong = await client.get("/api/sessions", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        # 健康检查不需要凭证(给反代探活用)
        assert (await client.get("/api/health")).status_code == 200


@pytest.mark.asyncio
async def test_host_header_must_be_allowed(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, allowed_hosts=["qi.example.com"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        ok = await client.get("/api/sessions", headers={"Host": "qi.example.com"})
        assert ok.status_code == 200
        blocked = await client.get("/api/sessions", headers={"Host": "evil.example.com"})
        assert blocked.status_code == 400 and "Host" in blocked.json()["detail"]


def test_non_loopback_without_password_refuses_to_start():
    """跨回环 + 无口令 → 拒启,并给出可执行的修复步骤。"""
    with pytest.raises(SystemExit) as exc:
        require_safe_config("0.0.0.0", None)
    assert "拒绝启动" in str(exc.value)
    assert "QI_WEB_PASSWORD" in str(exc.value)
    require_safe_config("0.0.0.0", "pw")        # 有口令则放行
    require_safe_config("127.0.0.1", None)      # 回环不需要口令
    assert is_loopback("localhost") and not is_loopback("0.0.0.0")


def test_mask_key_never_leaks_full_value():
    assert mask_key("sk-abcdefgh1234") == "…1234"
    assert mask_key("ab") == "…"
    assert mask_key(None) == ""


def test_port_preflight_detects_busy_port():
    """端口被占用时 `qi web` 必须**拒启**并给换个端口的建议。

    背景:先前 `qi web` 先打印 URL、后 bind,于是端口冲突表现成"启动成功但页面是旧的"
    ——真发生了一次。这条锁住"先预检、失败即退出"。
    """
    import socket

    from qi_web.serve import _port_free

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy_port = sock.getsockname()[1]
        assert _port_free("127.0.0.1", busy_port) is False
    # 关掉后同一端口应可绑定(证明上面 False 不是因为参数写错)
    assert _port_free("127.0.0.1", busy_port) is True


# ── 会话分叉 ──────────────────────────────────────────────

async def test_fork_session_copies_branch_into_new_file(client, tmp_path):
    """分叉 = 把**当前分支**复制成一个新会话文件(pi 同款:新文件,不是同一文件里开叉)。

    三条与 CLI/TUI 共用的语义:标题带 `@fork`、`cwd` 继承、原会话一个字节不动。
    """
    sid = (await client.post("/api/sessions", json={"title": "源会话", "cwd": str(tmp_path)})).json()["id"]
    await _agui(client, sid)
    before = (await client.get(f"/api/sessions/{sid}")).json()

    resp = await client.post(f"/api/sessions/{sid}/fork", json={})
    assert resp.status_code == 201, resp.text
    forked = resp.json()
    assert forked["id"] != sid
    assert forked["title"] == "源会话 @fork"
    assert forked["cwd"] == str(tmp_path)

    after = (await client.get(f"/api/sessions/{sid}")).json()
    assert after["total_entries"] == before["total_entries"]      # 源会话没被改
    copy = (await client.get(f"/api/sessions/{forked['id']}")).json()
    assert copy["total_entries"] == before["total_entries"]       # 历史逐条复制
    assert copy["entries"][0]["cwd"] == str(tmp_path)


async def test_fork_rejects_busy_and_unknown_anchor(client, tmp_path):
    """运行中不分叉(文件正在被追加,拷到的可能是半截回合);分叉点不存在要 400。"""
    sid = (await client.post("/api/sessions", json={"title": "a", "cwd": str(tmp_path)})).json()["id"]
    web = _web(client)
    web.begin(sid)
    try:
        assert (await client.post(f"/api/sessions/{sid}/fork", json={})).status_code == 409
    finally:
        web.end(sid)
    assert (await client.post(f"/api/sessions/{sid}/fork", json={"at": "nope"})).status_code == 400
    assert (await client.post("/api/sessions/none/fork", json={})).status_code == 404


# ── 工作区:显示名 + 「删除连会话一起删」─────────────────────

async def _session_in(client, cwd, title="a"):
    return (await client.post("/api/sessions", json={"title": title, "cwd": str(cwd)})).json()


async def test_workspace_rename_only_touches_the_display_name(client, tmp_path):
    """改名只改**显示名**:目录没动,会话没动,标题也没动。"""
    created = await _session_in(client, tmp_path)
    cwd = created["cwd"]
    assert (await client.get("/api/workspaces")).json() == {"names": {}}

    renamed = await client.patch("/api/workspaces", json={"cwd": cwd, "name": "我的项目"})
    assert renamed.json() == {"names": {cwd: "我的项目"}}
    assert tmp_path.is_dir()
    assert (await client.get(f"/api/sessions/{created['id']}")).json()["title"] == "a"

    # 空名 = 取消改名(恢复成目录名),不是"起个空名字"
    restored = await client.patch("/api/workspaces", json={"cwd": cwd, "name": ""})
    assert restored.json() == {"names": {}}


async def test_workspace_delete_takes_its_sessions_with_it(client, tmp_path):
    """删除工作区 = **连同它名下的会话一起删**(不可恢复);目录本身不动。"""
    other_dir = tmp_path / "keep"
    other_dir.mkdir()
    one = await _session_in(client, tmp_path, "一")
    two = await _session_in(client, tmp_path, "二")
    survivor = await _session_in(client, other_dir, "别的目录")
    cwd = one["cwd"]
    await client.patch("/api/workspaces", json={"cwd": cwd, "name": "要删的"})

    body = (await client.delete("/api/workspaces", params={"cwd": cwd})).json()
    assert sorted(body["ids"]) == sorted([one["id"], two["id"]])
    assert body["names"] == {}                                   # 显示名随之清掉

    # 会话文件真的没了(不是"换个地方显示")
    assert (await client.get(f"/api/sessions/{one['id']}")).status_code == 404
    assert (await client.get(f"/api/sessions/{two['id']}")).status_code == 404
    # 别的目录一条不少
    rest = (await client.get("/api/sessions")).json()["sessions"]
    assert [x["id"] for x in rest] == [survivor["id"]]
    # 目录本身不删 —— qi 不动用户的文件夹
    assert tmp_path.is_dir()


async def test_workspace_delete_is_scoped_by_normalized_cwd(client, tmp_path, monkeypatch):
    """按**规范化后的 cwd**圈定会话:同一个目录的两种写法(软链/尾斜杠)算同一个工作区。"""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    created = await _session_in(client, real)          # 会话写在真实路径下
    # 用软链路径删:要删掉的是同一批会话
    body = (await client.delete("/api/workspaces", params={"cwd": str(link)})).json()
    assert body["ids"] == [created["id"]]
    assert (await client.get("/api/sessions")).json()["sessions"] == []


async def test_workspace_delete_refuses_while_a_session_runs(client, tmp_path):
    """工作区里有会话在跑就整体拒绝(409):删掉正在写的文件会毁掉那一轮。"""
    created = await _session_in(client, tmp_path)
    web = _web(client)
    web.begin(created["id"])
    try:
        resp = await client.delete("/api/workspaces", params={"cwd": created["cwd"]})
        assert resp.status_code == 409
        assert "正在运行" in resp.json()["detail"]
    finally:
        web.end(created["id"])
    # 释放之后必须能删掉 —— 否则一次异常就把工作区永久卡住
    assert (await client.delete("/api/workspaces", params={"cwd": created["cwd"]})).status_code == 200
    assert (await client.get("/api/sessions")).json()["sessions"] == []


async def test_workspace_delete_of_unknown_directory_is_a_noop(client, tmp_path):
    """删一个没有会话的目录:不报错、也没有东西可删(前端可能在陈旧列表上操作)。"""
    body = (await client.delete("/api/workspaces", params={"cwd": str(tmp_path / "没有这个")})).json()
    assert body == {"ids": [], "names": {}}


async def test_workspaces_survive_a_corrupt_file(client, tmp_path):
    """偏好文件坏掉不能让 API 挂:读成空、后续写入把文件修回来。"""
    bad = tmp_path / "workspaces.json"
    bad.write_text("{ 这不是 JSON", encoding="utf-8")
    assert (await client.get("/api/workspaces")).json() == {"names": {}}
    assert (await client.patch("/api/workspaces",
                               json={"cwd": str(tmp_path), "name": "x"})).status_code == 200
    assert json.loads(bad.read_text(encoding="utf-8"))["names"]


# ── 「未分组」桶的批量清除 ────────────────────────────────────

async def test_clear_ungrouped_only_touches_cwd_less_sessions(client, tmp_path):
    """「清除会话」清的是**没有 cwd 的旧会话**;任何工作区里的会话都必须原样留着。"""
    web = _web(client)
    # 造两条"旧会话":API 建会话时总会带上 cwd,所以直接经 store 建(模拟早先版本写下的 header)
    legacy_one = web.sessions.create("旧一", cwd=None)
    legacy_two = web.sessions.create("旧二", cwd=None)
    grouped = await _session_in(client, tmp_path, "有目录的")

    listed = (await client.get("/api/sessions")).json()["sessions"]
    assert {s["id"] for s in listed} == {legacy_one.id, legacy_two.id, grouped["id"]}
    assert [s["cwd"] for s in listed if s["id"] in (legacy_one.id, legacy_two.id)] == [None, None]

    body = (await client.delete("/api/sessions", params={"scope": "ungrouped"})).json()
    assert sorted(body["ids"]) == sorted([legacy_one.id, legacy_two.id])

    rest = (await client.get("/api/sessions")).json()["sessions"]
    assert [s["id"] for s in rest] == [grouped["id"]]          # 有目录的一条不少
    assert (await client.get(f"/api/sessions/{legacy_one.id}")).status_code == 404


async def test_clear_ungrouped_refuses_while_one_runs(client, tmp_path):
    """任一条在运行中就整体拒绝(409)—— 删掉正在写的文件会毁掉那一轮。"""
    web = _web(client)
    legacy = web.sessions.create("旧", cwd=None)
    grouped = await _session_in(client, tmp_path)
    web.begin(legacy.id)
    try:
        resp = await client.delete("/api/sessions", params={"scope": "ungrouped"})
        assert resp.status_code == 409
        assert "正在运行" in resp.json()["detail"]
    finally:
        web.end(legacy.id)
    # 释放后必须能清掉 —— 否则一次异常就把这一桶永久卡住
    assert (await client.delete("/api/sessions", params={"scope": "ungrouped"})).status_code == 200
    assert [s["id"] for s in (await client.get("/api/sessions")).json()["sessions"]] == [grouped["id"]]


async def test_clear_ungrouped_rejects_other_scopes(client, tmp_path):
    """作用域是**闭集**:拼错或不给都要 422,不能悄悄变成"删全部"。"""
    assert (await client.delete("/api/sessions", params={"scope": "all"})).status_code == 422
    assert (await client.delete("/api/sessions")).status_code == 422


# ── 服务器目录浏览(「添加工作区」的选择器)────────────────────

async def test_browse_dirs_lists_directories_and_parent(client, tmp_path):
    """只列目录(文件不出现在选择器里),并给出上一级与主目录。"""
    import os

    (tmp_path / "child").mkdir()
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    body = (await client.get("/api/fs/dirs", params={"path": str(tmp_path)})).json()
    assert body["path"] == os.path.realpath(str(tmp_path))       # 归一成真实路径
    # 断言**意图**而不是精确列表:tmp_path 下还有夹具自己建的目录(home / sessions)。
    names = [e["name"] for e in body["entries"]]
    assert "child" in names
    assert "note.txt" not in names                               # 文件绝不出现在选择器里
    assert all(Path(e["path"]).is_dir() for e in body["entries"])
    child = next(e for e in body["entries"] if e["name"] == "child")
    assert child["path"] == str(Path(body["path"]) / "child")
    assert body["parent"] == os.path.realpath(str(tmp_path.parent))
    # `home` 是**真实主目录**,不是 QI_AGENT_HOME(测试里那个被指向 tmp)——
    # 选择器的"主目录"快捷键要落到用户真正想去的地方。
    assert body["home"] == os.path.realpath(str(Path.home()))
    assert body["roots"] == []                                    # 非 Windows 没有盘符


async def test_browse_dirs_defaults_to_home(client):
    """不给 `path` 就是主目录 —— 前端首屏就是这么用的。"""
    import os

    body = (await client.get("/api/fs/dirs")).json()
    assert body["path"] == os.path.realpath(str(Path.home()))


async def test_browse_dirs_error_codes(client, tmp_path):
    """三种失败分开回:不存在 404、不是目录 400、没权限 403(与 pi-web 同样的区分)。"""
    missing = await client.get("/api/fs/dirs", params={"path": str(tmp_path / "没有这个")})
    assert missing.status_code == 404
    assert missing.json()["detail"] == "目录不存在"

    file = tmp_path / "a.txt"
    file.write_text("x", encoding="utf-8")
    not_dir = await client.get("/api/fs/dirs", params={"path": str(file)})
    assert not_dir.status_code == 400
    assert not_dir.json()["detail"] == "这不是一个目录"


async def test_browse_dirs_requires_credentials(tmp_path, monkeypatch):
    """它是文件系统接口:必须与其它 /api 一样过 guard(不能因为"只读"就免鉴权)。"""
    app = _app(tmp_path, monkeypatch, password="s3cret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/api/fs/dirs")).status_code == 401
        ok = await client.get("/api/fs/dirs", headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200


async def test_config_exposes_bare_model_name_for_display(tmp_path, monkeypatch):
    """`default_model` 是 `provider/model` 标签(诊断面用),`default_model_name` 是裸模型名。

    输入卡右下只有一行的宽度:`commandcode/deepseek/deepseek-v4.1-flash` 这种三段式
    在窄窗口里必然被截断,而"哪个 provider"在设置页与遥测抽屉里都看得到。
    """
    app = _app(tmp_path, monkeypatch, provider="deepseek")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        body = (await client.get("/api/config")).json()
        assert body["default_model"] == "deepseek/x"
        assert body["default_model_name"] == "x"


# ── 手动压缩(输入卡「调用指令」里的 /compact)────────────────────

async def test_compact_endpoint_reports_nothing_to_compact(client, tmp_path):
    """没什么可压时回 `compacted=False`(而不是错误)—— 前端据此提示,不当失败。"""
    sid = (await client.post("/api/sessions", json={"title": "空", "cwd": str(tmp_path)})).json()["id"]
    resp = await client.post(f"/api/sessions/{sid}/compact")
    assert resp.status_code == 200
    assert resp.json() == {"compacted": False}


async def test_compact_rejects_unknown_and_busy(client, tmp_path):
    """不存在的会话 404;运行中 409(压缩要读整条分支并追加,与正在写的回合会打架)。"""
    sid = (await client.post("/api/sessions", json={"title": "a", "cwd": str(tmp_path)})).json()["id"]
    assert (await client.post("/api/sessions/none/compact")).status_code == 404
    web = _web(client)
    web.begin(sid)
    try:
        resp = await client.post(f"/api/sessions/{sid}/compact")
        assert resp.status_code == 409
        assert "正在运行" in resp.json()["detail"]
    finally:
        web.end(sid)


# ── 导出会话(指令 /export)──────────────────────────────────

async def test_export_serves_the_session_jsonl(client, tmp_path):
    """导出就是**原样发会话文件**(它本身就是 JSONL),文件名带标题与 id。

    标题里放中文是刻意测的:HTTP 头是 latin-1,直接写进 `filename=` 会抛
    UnicodeEncodeError,所以后端按 RFC 5987 双写(`filename` + `filename*`)。
    """
    import urllib.parse

    sid = (await client.post("/api/sessions", json={"title": "导出 我", "cwd": str(tmp_path)})).json()["id"]
    await _agui(client, sid)

    resp = await client.get(f"/api/sessions/{sid}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    disposition = resp.headers["content-disposition"]
    assert f'filename="qi-{sid}.jsonl"' in disposition
    encoded = urllib.parse.quote(f"导出 我-{sid}.jsonl", safe="")
    assert f"filename*=UTF-8''{encoded}" in disposition

    lines = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    assert lines[0]["type"] == "session" and lines[0]["id"] == sid
    assert any(entry.get("type") == "message" for entry in lines[1:])

    assert (await client.get("/api/sessions/none/export")).status_code == 404


class UsageStub(StreamingStub):
    """带 `prompt_tokens` 的两步流:用来钉住"usage 会落盘"这条链路。

    默认的 StreamingStub 只报 total_tokens,而"上下文占用"看的是 prompt 侧 ——
    没有它就断言不了 `context_tokens` 到底有没有被算出来。
    """

    async def astream(self, messages, tools=None, temperature=None):
        self.calls += 1
        if self.calls == 1:
            yield LLMDelta(text="先看一下", finished=True,
                           tool_calls=[ToolCallOut(id="c1", name="ls", args={"path": "."})],
                           usage={"prompt_tokens": 900, "completion_tokens": 10,
                                  "total_tokens": 910})
            return
        yield LLMDelta(text="完成", finished=True,
                       usage={"prompt_tokens": 1200, "completion_tokens": 30,
                              "total_tokens": 1230})


@pytest.mark.asyncio
async def test_session_usage_is_persisted_and_exposed(tmp_path, monkeypatch):
    """用量汇总:一轮跑完 → 落盘 → `/api/sessions/{id}` 报会话级数字。

    以前 usage 只活在流里(RUN_FINISHED 的 metadata),**刷新就没了** ——
    于是"这个会话花了多少 token"在界面上根本无从显示。这条用例钉住它落盘,
    并且钉住三个容易错的数:`steps` 是各轮相加、`context_tokens` 取最后一轮、
    `turns` 数的是用户消息。
    """
    app = _app(tmp_path, monkeypatch, llm=UsageStub())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        sid = (await client.post("/api/sessions",
                                 json={"title": "用量", "cwd": str(tmp_path)})).json()["id"]
        await _agui(client, sid)
        body = (await client.get(f"/api/sessions/{sid}")).json()
        usage = body["usage"]
        assert usage["turns"] == 1
        assert usage["steps"] == 2                     # 这个 stub 一轮里调了两次 LLM
        assert usage["llm_calls"] == 2
        assert usage["tools"] == 1
        assert usage["total_tokens"] == 910 + 1230
        assert usage["prompt_tokens"] == 900 + 1200
        assert usage["context_tokens"] == 1200          # **最后一轮**,不是 2100

        # 落盘断言:数字来自文件,不是流里的残留 —— 重开一个 store 读同一份 JSONL。
        fresh = SessionStore(root=_web(client).sessions.root).get(sid)
        assert fresh is not None
        assistant = [e for e in fresh.branch() if e.get("role") == "assistant"]
        assert assistant[-1]["usage"]["context_tokens"] == 1200


@pytest.mark.asyncio
async def test_config_exposes_the_default_model_context_window(client):
    """上下文窗口来自 models.json(没写 contextWindow 的模型用默认值)。

    之前 App 里硬编码 `contextWindow = 0` —— 于是界面上那条"上下文占用"永远是空的,
    而数据一直都在配置里。
    """
    body = (await client.get("/api/config")).json()
    assert body["default_model_context_window"] == 128000
