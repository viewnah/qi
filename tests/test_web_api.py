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
from qi_agent.web.app import create_app
from qi_agent.web.schemas import CONTRACT_VERSION
from qi_agent.web.security import is_loopback, mask_key, require_safe_config
from qi_agent.web.state import WebState


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
                         session_store=sessions, llm=llm or StreamingStub(),
                         disable_router=True)

    state = WebState(tmp_path, runtime_factory=factory)
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
    test_contract_p0 的 test_plugin_details_reach_the_event),这里确认 AG-UI
    映射层没有把它丢掉 —— 两处都通,插件才真的能渲染。
    """
    from qi_agent.models import AgentEvent, TOOL_OK
    from qi_agent.web.agui import AguiTranslator

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

    # 分派是 qi 独有 → 走 CUSTOM,不污染标准事件
    assert any(e.get("type") == "CUSTOM" and e.get("name") == "qi.dispatch" for e in events)


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
    assert "dispatch" in types
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
    from qi_agent.web.state import RunBusy, WebState

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
async def test_agents_endpoint_lists_general(client):
    body = (await client.get("/api/agents")).json()
    assert any(a["name"] == "general" for a in body["agents"])
    general = next(a for a in body["agents"] if a["name"] == "general")
    assert general["tools"]            # tools=["*"] 解析后的实际清单
    assert general["source"] == "builtin"


@pytest.mark.asyncio
async def test_skills_and_plugins_endpoints(client):
    """设置页要的两块数据:顶层技能清单与已装载插件名。

    插件只回名字是**故意的**:`discover_plugins()` 的职责是装载而非描述,
    它拿不到版本/作者就不编造。
    """
    skills = (await client.get("/api/skills")).json()
    assert isinstance(skills["skills"], list)
    for skill in skills["skills"]:
        assert set(skill) == {"name", "description", "source", "path"}

    plugins = (await client.get("/api/plugins")).json()
    assert isinstance(plugins["plugins"], list)
    assert all(isinstance(name, str) for name in plugins["plugins"])


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

    from qi_agent.cli import _port_free

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy_port = sock.getsockname()[1]
        assert _port_free("127.0.0.1", busy_port) is False
    # 关掉后同一端口应可绑定(证明上面 False 不是因为参数写错)
    assert _port_free("127.0.0.1", busy_port) is True
