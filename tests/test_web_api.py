"""qi-web API 契约测试(离线:stub LLM + 临时 QI_AGENT_HOME,不起真服务)。

用 `httpx.ASGITransport` 在进程内直连 ASGI app,因此:
  * 不需要端口、不受本机其他服务影响;
  * SSE 也走真实路径(StreamingResponse),不是伪造的字节流。

覆盖:契约号 / 会话增删改查 / 一轮执行 + SSE 事件序列 / 并发 409 / 取消 /
凭证写入的二次确认闸门 / 口令保护 / Host 允许表 / 只读配置掩码。
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


async def _sse_frames(client, sid: str, run_id: str | None = None,
                      timeout: float = 10.0) -> list[tuple[str, dict]]:
    """读完一条 SSE 流(在 run.finished 后服务端会关闭)。返回 (event, payload) 列表。"""
    import asyncio

    url = f"/api/sessions/{sid}/events"
    if run_id:
        url += f"?run_id={run_id}"
    frames: list[tuple[str, dict]] = []
    async with asyncio.timeout(timeout):
        async with client.stream("GET", url) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            event_name, data = None, None
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    event_name = line[7:].strip()
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                elif line == "" and event_name is not None:
                    frames.append((event_name, data or {}))
                    if event_name == "run.finished":
                        break
                    event_name, data = None, None
    return frames


# ── 元信息 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_meta_exposes_contract_and_capabilities(client, tmp_path):
    """契约号必须可读:前端据此判断能否与宿主协作(docs/web.md §3)。"""
    resp = await client.get("/api/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contract"] == CONTRACT_VERSION
    assert body["default_cwd"] == str(tmp_path)
    assert body["auth_required"] is False
    assert body["capabilities"]["sse"] is True and body["capabilities"]["streaming"] is True
    assert (await client.get("/api/health")).json()["ok"] is True


# ── 会话增删改查 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_lifecycle(client, tmp_path):
    created = await client.post("/api/sessions", json={"title": "第一个"})
    assert created.status_code == 201
    sid = created.json()["id"]
    assert created.json()["cwd"] == str(tmp_path)      # 会话头带 cwd
    assert created.json()["running"] is False

    listed = (await client.get("/api/sessions")).json()["sessions"]
    assert [s["id"] for s in listed] == [sid]

    detail = (await client.get(f"/api/sessions/{sid}")).json()
    assert detail["total_entries"] == 1 and detail["entries"][0]["type"] == "session"
    assert detail["skipped"] == 0

    renamed = await client.patch(f"/api/sessions/{sid}", json={"title": "改名了"})
    assert renamed.status_code == 200 and renamed.json()["title"] == "改名了"
    # 改名落到 header entry 上(回放时读到的就是新名字)
    again = (await client.get(f"/api/sessions/{sid}")).json()
    assert again["entries"][0]["title"] == "改名了"

    assert (await client.delete(f"/api/sessions/{sid}")).status_code == 204
    assert (await client.get(f"/api/sessions/{sid}")).status_code == 404
    assert (await client.delete(f"/api/sessions/{sid}")).status_code == 404


@pytest.mark.asyncio
async def test_create_session_rejects_missing_cwd(client):
    resp = await client.post("/api/sessions", json={"cwd": "/nonexistent/xyz"})
    assert resp.status_code == 400 and "工作目录不存在" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_detail_window_paginates(client):
    """长会话只回一个窗口(渐进恢复):limit/before 必须真的按窗口切。"""
    web = _web(client)
    sid = (await client.post("/api/sessions", json={"title": "长"})).json()["id"]
    session = web.sessions.get(sid)
    assert session is not None
    for i in range(4):                          # header + 4 = 5 条
        web.sessions.append(session, {"type": "message", "role": "user", "content": f"m{i}"})

    full = (await client.get(f"/api/sessions/{sid}?limit=50")).json()
    assert full["total_entries"] == 5 and full["skipped"] == 0

    tail = (await client.get(f"/api/sessions/{sid}?limit=2")).json()
    assert [e.get("content") for e in tail["entries"]] == ["m2", "m3"]
    assert tail["skipped"] == 3                 # 窗口之前还有 3 条可继续向更早翻

    older = (await client.get(f"/api/sessions/{sid}?limit=2&before=3")).json()
    assert [e.get("content") for e in older["entries"]] == ["m0", "m1"]
    assert older["skipped"] == 1


# ── 一轮执行 + SSE ────────────────────────────────────────

@pytest.mark.asyncio
async def test_turn_streams_full_event_sequence(client):
    """一轮执行的事件序列:snapshot → dispatch → … → tool_* → text → agent_end → run.finished。"""
    sid = (await client.post("/api/sessions", json={"title": "跑一轮"})).json()["id"]
    accepted = await client.post(f"/api/sessions/{sid}/turn", json={"text": "看下目录"})
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]
    assert accepted.json()["from_seq"] == 0        # 客户端重放整轮

    frames = await _sse_frames(client, sid, run_id)
    kinds = [k for k, _ in frames]
    assert kinds[0] == "snapshot"                   # 每代以快照开头
    for expected in ("dispatch", "agent_start", "tool_start", "tool_end",
                     "text", "agent_end", "run.finished"):
        assert expected in kinds, f"缺少事件 {expected}:{kinds}"

    # 逐字流式:文本增量确实在流里,且拼接 == 最终 text
    deltas = "".join(p["text"] for k, p in frames if k == "text_delta")
    assert deltas == "我先看一下完成"
    final = [p["text"] for k, p in frames if k == "text"][-1]
    assert final == "完成"

    # 工具事件带结构化字段(前端工具行的依据)
    tool_end = next(p for k, p in frames if k == "tool_end")
    assert tool_end["data"]["status"] == "ok"
    assert isinstance(tool_end["data"]["duration_ms"], int)

    # usage 透出(状态栏)
    usage = next(p for k, p in frames if k == "agent_end")["data"]["usage"]
    assert usage["total_tokens"] == 12 and usage["llm_calls"] == 2

    assert next(p for k, p in frames if k == "run.finished")["status"] == "ok"


@pytest.mark.asyncio
async def test_turn_result_is_replayable_from_session(client):
    """SSE 之外,结果必须已落盘:刷新后靠会话快照也能重建(含叙述与工具卡)。"""
    sid = (await client.post("/api/sessions", json={"title": "回放"})).json()["id"]
    await client.post(f"/api/sessions/{sid}/turn", json={"text": "看下目录"})
    await _sse_frames(client, sid, (await _last_run(client, sid)))

    entries = (await client.get(f"/api/sessions/{sid}?limit=50")).json()["entries"]
    shaped = [(e.get("type"), e.get("custom_type")) for e in entries]
    assert ("custom", "assistant_narration") in shaped          # 工具前的叙述
    assert ("tool", None) in shaped                             # 工具往返
    assert entries[-1]["role"] == "assistant"                   # 最终回答
    assert shaped.index(("custom", "assistant_narration")) < shaped.index(("tool", None))


async def _last_run(client, sid: str) -> str:
    """取该会话最近一次 run(含已完成的)。"""
    run = _web(client).active_run(sid) or _web(client).recent_run(sid)
    if run is None:
        raise AssertionError("没有找到 run")
    return run.run_id


@pytest.mark.asyncio
async def test_second_turn_while_running_gets_409(tmp_path, monkeypatch):
    """同一会话串行:并发提交返回 409(而不是静默交错写 JSONL)。"""
    app = _app(tmp_path, monkeypatch, llm=SlowStub())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        sid = (await client.post("/api/sessions", json={"title": "并发"})).json()["id"]
        first = await client.post(f"/api/sessions/{sid}/turn", json={"text": "a"})
        assert first.status_code == 202
        second = await client.post(f"/api/sessions/{sid}/turn", json={"text": "b"})
        assert second.status_code == 409
        assert second.json()["detail"]["run_id"] == first.json()["run_id"]
        # 运行中不允许删除(否则会删掉正在写的文件)
        assert (await client.delete(f"/api/sessions/{sid}")).status_code == 409
        assert (await client.post(f"/api/sessions/{sid}/cancel")).status_code == 202


@pytest.mark.asyncio
async def test_cancel_without_run_is_409(client):
    sid = (await client.post("/api/sessions", json={"title": "空"})).json()["id"]
    assert (await client.post(f"/api/sessions/{sid}/cancel")).status_code == 409


class _FakeRequest:
    """只为 idle 路径提供 `is_disconnected()`:不经过 HTTP,直接驱动生成器。"""

    def __init__(self, disconnect_after: int = 0) -> None:
        self.calls = 0
        self._after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return bool(self._after) and self.calls > self._after


@pytest.mark.asyncio
async def test_idle_stream_opens_with_snapshot(app):
    """没有活跃 run 时也要能连上:先给快照,再靠心跳保活。

    这里**直接驱动生成器**而不是走 httpx:httpx 的 ASGITransport 会缓冲整个响应体,
    对"永不结束"的空闲流会一直等下去。增量投递(真实分块到达)由真机 e2e
    (uvicorn + curl)覆盖,见 tests 之外的验收记录。
    """
    web: WebState = app.state.web
    sid = web.sessions.create("空闲", cwd=web.default_cwd).id
    route = next(r for r in app.routes
                 if getattr(r, "path", "") == "/api/sessions/{sid}/events")
    resp = await route.endpoint(sid=sid, request=_FakeRequest(), run_id=None, from_seq=0)
    agen = resp.body_iterator
    opening = await agen.__anext__()
    snapshot = await agen.__anext__()
    await agen.aclose()

    assert opening.startswith(": ")                    # 开场注释帧
    assert snapshot.startswith("event: snapshot")      # 快照
    assert "id: " not in snapshot.split("\n", 1)[0]   # 快照不带 id,避免与 seq 0 撞号
    assert json.loads(snapshot.split("data: ", 1)[1])["id"] == sid


# ── 凭证写入:二次确认闸门 ────────────────────────────────

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
