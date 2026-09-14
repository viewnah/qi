"""qi-web HTTP 宿主(docs/web.md §3):REST + SSE + 静态托管。

同进程直连 `QiRuntime.stream()` —— web 是 CLI/TUI 之外的**第三个 consumer**,零协议桥。
契约版本见 `schemas.CONTRACT_VERSION`;前端从 `/api/meta` 读取并据此降级。
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..auth import AuthStore, resolve_key
from ..config import ConfigError, load_config, resolve_default_model, resolve_router_model
from ..loader import LoadError
from ..registry import AgentRegistry
from . import schemas
from .security import check_credentials, check_host, mask_key
from .state import HEARTBEAT_S, RunBusy, WebState, comment, frame

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_WINDOW = 200          # 会话明细默认窗口(渐进恢复:老会话不因细节太多而卡住)

_PLACEHOLDER = """<!doctype html><meta charset="utf-8"><title>qi web</title>
<style>body{{font:14px/1.7 -apple-system,"PingFang SC",sans-serif;max-width:640px;
margin:72px auto;padding:0 24px;color:#0F1115;background:#F5F6F7}}
code{{background:#fff;padding:2px 6px;border-radius:4px;border:1px solid #E5E5E5}}
h1{{font-size:18px}}</style>
<h1>qi-web 宿主已启动,但前端产物不存在</h1>
<p>API 可直接使用(<code>/api/meta</code>、<code>/api/sessions</code>、SSE 事件流)。</p>
<p>构建前端:<code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>构建产物会被放进 <code>src/qi_agent/web/static/</code>,刷新本页即可。</p>"""


def create_app(cwd: Path | str | None = None, password: str | None = None,
               allowed_hosts: list[str] | None = None, bind_host: str = "127.0.0.1",
               state: WebState | None = None) -> FastAPI:
    app = FastAPI(title=schemas.APP_NAME, version=__version__,
                  docs_url=None, redoc_url=None)
    web = state or WebState(Path(cwd).resolve() if cwd else Path.cwd())
    app.state.web = web
    app.state.password = password
    allowed = list(allowed_hosts or [])

    # ── 通用守卫 ──────────────────────────────────────────
    def guard(request: Request) -> None:
        if not check_host(allowed, request.headers.get("host"), bind_host):
            raise HTTPException(status_code=400, detail="Host 头不被允许(见 QI_WEB_ALLOWED_HOSTS)")
        auth = request.headers.get("authorization")
        if not check_credentials(auth, password):
            raise HTTPException(status_code=401, detail="需要凭证",
                                headers={"WWW-Authenticate": 'Basic realm="qi-web"'})

    def confirm_gate(confirm: str | None) -> None:
        """写操作的第二道闸:必须显式带确认头(前端弹层确认后才带上)。"""
        if not secrets.compare_digest((confirm or "").strip().lower(), "yes"):
            raise HTTPException(
                status_code=428,
                detail="写操作需要二次确认:带上请求头 X-Qi-Confirm: yes",
            )

    # ── 元信息 ────────────────────────────────────────────
    @app.get("/api/meta", response_model=schemas.Meta, dependencies=[Depends(guard)])
    async def meta() -> schemas.Meta:
        return schemas.Meta(
            version=__version__,
            default_cwd=str(web.default_cwd),
            auth_required=bool(password),
            static_ready=STATIC_DIR.is_dir(),
            capabilities={"sse": True, "streaming": True, "cancel": True,
                          "auth_write": True, "config_read": True, "trajectory": True},
        )

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "app": schemas.APP_NAME, "contract": schemas.CONTRACT_VERSION}

    # ── 会话 ──────────────────────────────────────────────
    def summary(session) -> schemas.SessionSummary:
        return schemas.SessionSummary(
            id=session.id, title=session.title, created_at=session.created_at,
            cwd=session.cwd, message_count=session.message_count,
            running=web.active_run(session.id) is not None, path=str(session.path),
        )

    def detail(session, limit: int, before: int | None) -> schemas.SessionDetail:
        # 只暴露**当前分支**(+ header,契约里 entries[0] 一直是 header):
        # 树里其它分支不属于这段对话的历史
        entries = session.visible_entries()
        end = len(entries) if before is None else max(0, min(before, len(entries)))
        start = max(0, end - max(1, limit))
        return schemas.SessionDetail(
            id=session.id, title=session.title, created_at=session.created_at,
            cwd=session.cwd, path=str(session.path),
            running=web.active_run(session.id) is not None,
            total_entries=len(entries), entries=entries[start:end], skipped=start,
        )

    @app.get("/api/sessions", response_model=schemas.SessionList, dependencies=[Depends(guard)])
    async def list_sessions() -> schemas.SessionList:
        return schemas.SessionList(sessions=[summary(s) for s in web.sessions.list()])

    @app.post("/api/sessions", response_model=schemas.SessionSummary, status_code=201,
              dependencies=[Depends(guard)])
    async def create_session(body: schemas.SessionCreate) -> schemas.SessionSummary:
        cwd = Path(body.cwd).expanduser() if body.cwd else web.default_cwd
        if not cwd.is_dir():
            raise HTTPException(status_code=400, detail=f"工作目录不存在: {cwd}")
        return summary(web.sessions.create(body.title, cwd=cwd))

    @app.get("/api/sessions/{sid}", response_model=schemas.SessionDetail,
             dependencies=[Depends(guard)])
    async def get_session(sid: str, limit: int = Query(DEFAULT_WINDOW, ge=1, le=2000),
                          before: int | None = Query(None, ge=0)) -> schemas.SessionDetail:
        session = web.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return detail(session, limit, before)

    @app.patch("/api/sessions/{sid}", response_model=schemas.SessionSummary,
               dependencies=[Depends(guard)])
    async def rename_session(sid: str, body: schemas.SessionRename) -> schemas.SessionSummary:
        session = web.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        session.title = body.title
        if session.entries and session.entries[0].get("type") == "session":
            session.entries[0]["title"] = body.title
        web.sessions.save(session)
        return summary(session)

    @app.delete("/api/sessions/{sid}", status_code=204, dependencies=[Depends(guard)])
    async def delete_session(sid: str) -> None:
        if web.active_run(sid) is not None:
            raise HTTPException(status_code=409, detail="会话正在运行,先停止再删除")
        if not web.sessions.delete(sid):
            raise HTTPException(status_code=404, detail="会话不存在")

    # ── 执行一轮 + SSE ────────────────────────────────────
    @app.post("/api/sessions/{sid}/turn", response_model=schemas.TurnAccepted,
              status_code=202, dependencies=[Depends(guard)])
    async def post_turn(sid: str, body: schemas.TurnRequest) -> schemas.TurnAccepted:
        session = web.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        try:
            run = web.start_run(session, body.text, body.agent)
        except RunBusy as busy:
            raise HTTPException(status_code=409, detail={
                "detail": "该会话已有活跃 run", "run_id": busy.run_id}) from busy
        return schemas.TurnAccepted(run_id=run.run_id, session_id=sid, from_seq=0)
        # from_seq 恒为 0:客户端随后 GET /events?run_id=… 重放**整轮**。
        # 不做"从当前 seq 开始":run 可能在 POST 返回前就发了几条事件,那几条会丢。

    @app.post("/api/sessions/{sid}/cancel", status_code=202, dependencies=[Depends(guard)])
    async def cancel_turn(sid: str) -> dict:
        if not web.cancel_run(sid):
            raise HTTPException(status_code=409, detail="没有正在运行的 run")
        return {"cancelled": True, "session_id": sid}

    @app.get("/api/sessions/{sid}/events", dependencies=[Depends(guard)])
    async def events(sid: str, request: Request, run_id: str | None = Query(None),
                     from_seq: int = Query(0, alias="from", ge=0)) -> StreamingResponse:
        """SSE 事件流。

        每代以 **snapshot** 开头(客户端的权威起点,不带 `id:` 以免与 seq 撞号),
        其后是目标 run 的事件;run 结束后发 `run.finished` 并**关闭**连接——
        客户端重连会拿到包含落盘结果的新快照。这就是 docs/web.md §8.2 记的
        "每代 opening snapshot 原子替换"。

        `?run_id=` 可指向**已完成**的 run 并把它重放一遍:否则客户端晚一步连上来
        (异步任务很快就跑完)就只能看到快照,拿不到事件流。
        """
        session = web.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        target = web.run_by_id(run_id) if run_id else web.active_run(sid)

        async def gen():
            yield comment("qi-web sse open")
            yield frame("snapshot", detail(session, DEFAULT_WINDOW, None).model_dump())
            run = target
            if run is None:
                while True:                        # 空闲:只发心跳,保持连接可用
                    if await request.is_disconnected():
                        return
                    yield comment("idle")
                    await asyncio.sleep(HEARTBEAT_S)
            seen = max(from_seq, run.first_seq)
            while True:
                if await request.is_disconnected():
                    return
                for item in run.items_from(seen):
                    seen = item["seq"] + 1
                    yield frame(item["kind"], item["payload"], item["seq"])
                if run.done:
                    return
                if not await run.wait_more(seen):
                    yield comment("ping")

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    # ── agent / 配置(只读) + 凭证(写需二次确认) ──────────
    @app.get("/api/agents", response_model=schemas.AgentList, dependencies=[Depends(guard)])
    async def list_agents() -> schemas.AgentList:
        runtime = web.runtime_for(web.default_cwd)
        registry: AgentRegistry = runtime.registry
        agents = []
        for unit in registry.all():
            cfg = unit.config
            agents.append(schemas.AgentInfo(
                name=unit.name, display_name=(cfg.display_name or "").strip(),
                description=cfg.description, source=unit.source, tools=unit.tools,
                keywords=cfg.keywords, skills=len(unit.skills),
                data_sources=len(unit.data_sources), mcp_private=len(unit.mcp_private),
            ))
        return schemas.AgentList(agents=agents)

    @app.get("/api/config", response_model=schemas.ConfigView, dependencies=[Depends(guard)])
    async def get_config() -> schemas.ConfigView:
        runtime = web.runtime_for(web.default_cwd)
        cfg = runtime.cfg
        store = AuthStore()
        providers = []
        for name, prov in cfg.providers.items():
            resolved = resolve_key(name, prov.apiKey, store)
            providers.append(schemas.ProviderInfo(
                name=name, base_url=prov.baseUrl, api=prov.api,
                models=len(prov.models), credential_ok=resolved.ok,
                credential_source=resolved.source, credential_masked=mask_key(resolved.key),
            ))
        default = resolve_default_model(cfg, web.default_cwd)
        router = resolve_router_model(cfg, web.default_cwd)
        missing = [p.name for p in providers if not p.credential_ok]
        checks = [
            {"name": "models.json", "ok": bool(runtime.config_files),
             "detail": ", ".join(str(p) for p in runtime.config_files) or "未找到"},
            {"name": "默认模型", "ok": bool(default.model), "detail": default.label},
            {"name": "凭证", "ok": not missing, "detail": "缺失: " + ", ".join(missing) if missing else "全部就绪"},
        ]
        return schemas.ConfigView(
            providers=providers, default_model=default.label,
            default_model_source="settings" if default.model else "",
            router_model=(router.label if router.label != default.label else None),
            settings_files=[str(p) for p in runtime.settings_files],
            config_files=[str(p) for p in runtime.config_files],
            session_dir=str(runtime.sessions.root),
            # 二次确认弹层要显示"改的是哪个文件",所以把凭证文件路径一并回给前端
            auth_file=str(AuthStore().path),
            checks=checks,
        )

    @app.get("/api/skills", response_model=schemas.SkillList, dependencies=[Depends(guard)])
    async def list_skills() -> schemas.SkillList:
        """顶层技能(六层来源 + settings 追加),供设置页展示。"""
        runtime = web.runtime_for(web.default_cwd)
        return schemas.SkillList(skills=[
            schemas.SkillInfo(name=skill.name, description=skill.description,
                              source=skill.source, path=str(skill.path))
            for skill in runtime.top_skills
        ])

    @app.get("/api/plugins", response_model=schemas.PluginList, dependencies=[Depends(guard)])
    async def list_plugins() -> schemas.PluginList:
        runtime = web.runtime_for(web.default_cwd)
        return schemas.PluginList(plugins=list(runtime.plugins))

    @app.post("/api/auth/{provider}", status_code=204, dependencies=[Depends(guard)])
    async def set_auth(provider: str, body: schemas.AuthWrite,
                       confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> None:
        confirm_gate(confirm)
        AuthStore().set_key(provider, body.key)

    @app.delete("/api/auth/{provider}", status_code=204, dependencies=[Depends(guard)])
    async def delete_auth(provider: str,
                          confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> None:
        confirm_gate(confirm)
        if not AuthStore().remove(provider):
            raise HTTPException(status_code=404, detail=f"凭证不存在: {provider}")

    # ── 配置/装载错误 → 可读响应(而不是 500 栈) ──────────
    @app.exception_handler(ConfigError)
    @app.exception_handler(LoadError)
    async def _config_error(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=503,
                            content={"detail": f"{type(exc).__name__}: {exc}"})

    # ── 静态前端(必须最后挂:先匹配 /api) ─────────────────
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    else:
        @app.get("/", response_class=HTMLResponse)
        async def placeholder() -> str:
            return _PLACEHOLDER

    return app


def main() -> None:  # pragma: no cover - 入口由 CLI 调用
    raise SystemExit("请用 `qi web` 启动(见 qi_agent.cli)")
