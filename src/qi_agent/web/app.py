"""qi-web HTTP 宿主(docs/web.md §3):REST + SSE + 静态托管。

同进程直连 `QiRuntime.stream()` —— web 是 CLI/TUI 之外的**第三个 consumer**,零协议桥。
契约版本见 `schemas.CONTRACT_VERSION`;前端从 `/api/meta` 读取并据此降级。
"""

from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..auth import AuthStore, resolve_key
from ..config import ConfigError, load_config, resolve_default_model, resolve_router_model
from ..loader import LoadError, load_mcp_scopes
from ..registry import AgentRegistry
from ..session import usage_summary
from ..workspaces import WorkspaceStore, normalize
from . import agui, browse, schemas
from .security import check_credentials, check_host, mask_key
from .state import RunBusy, WebState

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
               state: WebState | None = None,
               workspace_store: WorkspaceStore | None = None) -> FastAPI:
    app = FastAPI(title=schemas.APP_NAME, version=__version__,
                  docs_url=None, redoc_url=None)
    web = state or WebState(Path(cwd).resolve() if cwd else Path.cwd())
    app.state.web = web
    #: 工作区的两个「人工决定」(显示名 / 不再分组)。默认落在 `~/.qi/agent/`,
    #: 测试传一个 tmp 路径的 store,不去碰真实 home。
    workspaces = workspace_store or WorkspaceStore()
    app.state.workspaces = workspaces
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
                          "auth_write": True, "config_read": True, "trajectory": True,
                          # 扩展面:插件据这两个开关决定要不要发结构化 UI。
                          # `ag_ui` = 事件形状是 AG-UI;`ui_v1` = 认 details["ui"] 的词汇表
                          # (见 docs/web.md §16.2)。缺哪个都退回"折叠显示原始 JSON"。
                          "ag_ui": True, "ui_v1": True},
        )

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "app": schemas.APP_NAME, "contract": schemas.CONTRACT_VERSION}

    # ── 会话 ──────────────────────────────────────────────
    def touched_at(session) -> str:
        """会话文件最后一次改动的时刻(本地时间,与 `session._now()` 同格式)。

        左栏的「15分钟」显示的是**最近活跃**,不是创建时间:列表顺序本来就是文件
        mtime(`SessionStore._files()`),用创建时间会让"排在最前却写着 3天前"
        这种自相矛盾的行出现。文件被外部删掉时回落 `created_at`,不抛。
        """
        try:
            mtime = session.path.stat().st_mtime
        except OSError:
            return session.created_at
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime))

    def summary(session) -> schemas.SessionSummary:
        return schemas.SessionSummary(
            id=session.id, title=session.title, created_at=session.created_at,
            updated_at=touched_at(session),
            cwd=session.cwd, message_count=session.message_count,
            # `is_busy` 已是 bool —— 早先写成 `is not None` 是错的(永远为 True)
            running=web.is_busy(session.id), path=str(session.path),
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
            running=web.is_busy(session.id),
            total_entries=len(entries), entries=entries[start:end], skipped=start,
            # 用量汇总统计的是**整条分支**(与这个窗口无关) —— 所以它在后端算:
            # 前端只拿到窗口,长会话自己求和会静默少算。
            usage=schemas.UsageSummary(**usage_summary(entries)),
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
        if web.is_busy(sid):
            raise HTTPException(status_code=409, detail="会话正在运行,先停止再删除")
        if not web.sessions.delete(sid):
            raise HTTPException(status_code=404, detail="会话不存在")

    @app.delete("/api/sessions", response_model=schemas.DeletedSessions,
                dependencies=[Depends(guard)])
    async def delete_sessions(scope: Literal["ungrouped"] = Query(...)) -> schemas.DeletedSessions:
        """批量删除:目前只有一种作用域 —— 左栏「未分组」桶里的会话。

        「未分组」= **没有 cwd 的旧会话**(qi 早先版本的 header 里没有 cwd,无从判断
        它在哪个项目里)。它们既不能被"删除工作区"带走(那个目录名下的会话根本不含它们),
        也只能一个一个删 —— 所以给这一桶一个批量出口。

        与单条删除同一套护栏:**任一条在运行中就整体拒绝**(409),否则会删掉正在写的文件。
        """
        doomed = [
            s.id for s in web.sessions.list()
            if s.cwd is None or s.cwd == ""
        ]
        busy = [sid for sid in doomed if web.is_busy(sid)]
        if busy:
            raise HTTPException(
                status_code=409,
                detail="未分组里有会话正在运行,先停止再清除",
            )
        return schemas.DeletedSessions(
            ids=[sid for sid in doomed if web.sessions.delete(sid)],
        )

    @app.get("/api/sessions/{sid}/export", dependencies=[Depends(guard)])
    async def export_session(sid: str) -> Response:
        """导出会话 JSONL(= TUI 的 `/export`)。**原样发文件**:会话文件本身就是 JSONL,
        再序列化一遍只会引入第二份真相。文件名给成 `<标题>-<id>.jsonl`,浏览器据此命名。"""
        session = web.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        try:
            text = session.path.read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(status_code=404, detail="会话文件不存在") from None
        stem = "".join(c for c in (session.title or "session") if c not in '/\\:*?"<>|')[:40]
        # HTTP 头是 latin-1:标题里只要有中文,直接写进 `filename=` 就会抛
        # UnicodeEncodeError(这个端点会 500)。所以按 RFC 5987 双写:ASCII 兜底名
        # + `filename*=UTF-8''<百分号编码>`,浏览器优先用后者。
        ascii_name = f"qi-{sid}.jsonl"
        utf8_name = quote(f"{stem or 'session'}-{sid}.jsonl", safe="")
        return Response(
            content=text,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{ascii_name}"; '
                    f"filename*=UTF-8''{utf8_name}"
                ),
                # 前端要能读文件名(同源也默认不给跨不出去的响应头)
                "Access-Control-Expose-Headers": "Content-Disposition",
            },
        )

    @app.post("/api/sessions/{sid}/compact", response_model=schemas.CompactionResult,
              dependencies=[Depends(guard)])
    async def compact_session(sid: str) -> schemas.CompactionResult:
        """手动压缩上下文(= TUI 的 `/compact`,把旧消息摘要掉)。

        **不能在运行中压缩**:压缩要"读整条分支 → 摘要 → 追加一条 entry",
        与会话正在追加的那一轮会打架(和重命名/删除同一类冲突),所以占用中就 409。
        `compacted=False` 表示没什么可压(消息太少/没超阈值),不是错误。
        """
        session = web.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if web.is_busy(sid):
            raise HTTPException(status_code=409, detail="会话正在运行,先停止再压缩")
        runtime = web.runtime_for(web.session_cwd(session))
        entry = await runtime.compact_session(session)
        return schemas.CompactionResult(compacted=entry is not None)

    @app.post("/api/sessions/{sid}/fork", response_model=schemas.SessionSummary,
              status_code=201, dependencies=[Depends(guard)])
    async def fork_session(sid: str, body: schemas.SessionFork | None = None) -> schemas.SessionSummary:
        """把一个会话的**当前分支**复制成新会话(pi 的 fork:新文件,不是同一个文件里开叉)。

        语义与 CLI/TUI 一致(`store.fork_at(source, source.current, title=f"{title} @fork")`):
        副标题带 `@fork`、cwd 继承、entry 的 id/parentId 原样拷过去(链在新文件里自洽)。
        运行中不允许分叉:文件正在被追加,拷到的可能是个半截回合。
        """
        session = web.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if web.is_busy(sid):
            raise HTTPException(status_code=409, detail="会话正在运行,先停止再分叉")
        at = session.current if body is None or body.at is None else body.at
        if at is not None and at not in {str(e.get("id")) for e in session.tree_entries}:
            raise HTTPException(status_code=400, detail=f"找不到分叉点: {at}")
        title = f"{session.title} @fork" if session.title else "fork"
        return summary(web.sessions.fork_at(session, at, title=title))

    # ── 工作区:只有"改过的显示名"这一份偏好 ────────────────
    # 项目列表本身是派生的(会话 cwd),所以这里没有"列表"端点,也没有"新建工作区"——
    # 在某个目录下新建会话就是新建工作区。三种方法的语义见 docs/web.md §18.3。

    @app.get("/api/workspaces", response_model=schemas.WorkspaceNames,
             dependencies=[Depends(guard)])
    async def list_workspaces() -> schemas.WorkspaceNames:
        return schemas.WorkspaceNames(names=workspaces.load().names)

    @app.patch("/api/workspaces", response_model=schemas.WorkspaceNames,
               dependencies=[Depends(guard)])
    async def rename_workspace(body: schemas.WorkspaceRename) -> schemas.WorkspaceNames:
        """改工作区的**显示名**。目录不动:项目就是目录,改目录名会影响会话的 cwd。"""
        return schemas.WorkspaceNames(names=workspaces.rename(body.cwd, body.name).names)

    @app.delete("/api/workspaces", response_model=schemas.WorkspaceDeleted,
                dependencies=[Depends(guard)])
    async def delete_workspace(cwd: str = Query(..., min_length=1)) -> schemas.WorkspaceDeleted:
        """删除工作区:**连同它名下的会话一起删**(不可恢复)。目录本身不删。

        三件事必须做对:
          · 会话集合按**规范化后的 cwd** 取(老会话的 header 可能没规范化过,
            macOS 上 `/tmp` 与 `/private/tmp` 是同一个目录两种写法);
          · 任一会话在运行中就整体拒绝(409)—— 删掉正在写的文件会毁掉那一轮;
          · 最后忘掉这个目录的显示名,不留悬空的覆盖项。
        """
        target = normalize(cwd)
        doomed = [
            s.id for s in web.sessions.list()
            if s.cwd is not None and normalize(s.cwd) == target
        ]
        busy = [sid for sid in doomed if web.is_busy(sid)]
        if busy:
            raise HTTPException(
                status_code=409,
                detail="该工作区有会话正在运行,先停止再删除工作区",
            )
        deleted = [sid for sid in doomed if web.sessions.delete(sid)]
        return schemas.WorkspaceDeleted(
            ids=deleted, names=workspaces.forget(cwd).names,
        )

    # ── 服务器目录浏览(「添加工作区」的选择器)─────────────────
    # 形状照 pi-web 的 `GET /api/cwd/browse`(见 `web/browse.py` 的文件头)。
    # 只读、只列目录:权限面比已有的 bash 工具小得多,所以沿用同一道 guard。

    @app.get("/api/fs/dirs", response_model=schemas.DirectoryListing,
             dependencies=[Depends(guard)])
    async def browse_dirs(path: str = Query("")) -> schemas.DirectoryListing:
        """列某个目录下的子目录;`path` 省略 = 主目录(前端首屏就这样)。"""
        try:
            listing = browse.list_directories(path)
        # 三条 except 各自可达:FileNotFoundError / NotADirectoryError / PermissionError
        # 是 OSError 的三个**兄弟**(实测 MRO:NotADirectoryError → OSError → Exception),
        # 互不为子类。pi-lens 的 unreachable-except 在这里是误报。
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="目录不存在") from None
        except NotADirectoryError:
            raise HTTPException(status_code=400, detail="这不是一个目录") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="没有权限读取这个目录") from None
        root = browse.resolve_dir(path)
        return schemas.DirectoryListing(
            path=str(root),
            parent=browse.parent_of(root),
            home=str(browse.home_dir()),
            roots=[schemas.DirEntry(**d) for d in browse.windows_drives()],
            entries=[schemas.DirEntry(**e) for e in listing],
        )

    # ── AG-UI:单次 POST,响应即流 ──────────────────────────────
    #
    # 这是 docs/web.md 记的**破坏性替换**:旧的 `POST /turn`(202)+ `POST /cancel`
    # + `GET /events`(两跳、带 `event:`/`id:`、`snapshot` 收尾)已全部移除。
    #
    # 三条硬约束来自**官方编码器**(`@ag-ui/encoder@0.0.59` 的 `encodeSSE`),
    # 不是文档转述:只有 `data:` 行、没有 `event:`、没有 `id:`;类型在 JSON 的 `type` 里。
    #
    # 于是三个东西一起没了(取舍记录在文档里):
    #   · `?run_id=` 重放已完成轮 —— 流就是这次响应,没有"晚连上来"的客户端;
    #   · `?from=` 断线续传 —— 没有可续的流;AG-UI 的答案本是"重发 RunAgentInput";
    #   · `id:` 游标 —— 没有任何消费者了(实测过原生 EventSource 会回传
    #     `Last-Event-ID` 而 qi 不读它,留着只会误导)。
    #
    # 取消也不再需要单独的端点:客户端 abort 请求 → ASGI 取消生成器 →
    # `runtime.stream()` 被 CancelledError 打断。
    @app.post("/api/ag-ui", dependencies=[Depends(guard)])
    async def ag_ui(request: Request, body: dict = Body(...)) -> StreamingResponse:
        """AG-UI 协议端点:POST `RunAgentInput` → `text/event-stream`。

        `threadId` = qi 的 session id(qi 的 fork 会**新建会话文件**,所以每个分支
        天然是一条独立 thread,与 AG-UI 的线性 thread 模型不冲突);
        `runId` 由客户端提供,原样回显在 `RUN_STARTED` 里。
        """
        try:
            inp = agui.RunAgentInput.parse(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not inp.thread_id:
            raise HTTPException(status_code=422, detail="RunAgentInput.threadId 不能为空")
        session = web.sessions.get(inp.thread_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {inp.thread_id}")
        text = inp.last_user_text()
        if not text.strip():
            raise HTTPException(status_code=422, detail="messages 里没有 user 内容")
        try:
            web.begin(session.id)               # 会话级串行:两个 POST 不得往同一文件追加
        except RunBusy as busy:
            raise HTTPException(status_code=409, detail="该会话已有活跃 run") from busy

        run_id = inp.run_id or uuid.uuid4().hex[:12]
        translator = agui.AguiTranslator(thread_id=session.id, run_id=run_id)
        entries = session.visible_entries()

        async def gen():
            try:
                # RUN_STARTED 必须是流的第一帧(AG-UI 生命周期边界),
                # 紧接着把历史与状态作为权威起点推给客户端 —— 这是 qi 自己的决定:
                # AG-UI 不强制快照,但 qi 的 UI 靠它重建整屏。
                for frame in translator.start():
                    yield agui.encode(frame)
                yield agui.encode(agui.messages_snapshot(entries))
                yield agui.encode(agui.state_snapshot({
                    "cwd": session.cwd, "title": session.title,
                }))
                # qi 自己的完整 entry 列表:MESSAGES_SNAPSHOT 装不下 dispatch/tool/叙述,
                # 不给这一份,刷新后轨迹就只剩对话(直播与回放不一致)。
                yield agui.encode(agui.history_snapshot(entries))
                async for ev in web.runtime_for(web.session_cwd(session)).stream(
                        text, session, agent_override=inp.agent):
                    for frame in translator.feed(ev):
                        yield agui.encode(frame)
                # 流正常结束:补收尾(万一最后一帧还开着文本/思考)
                for frame in translator.finish_open():
                    yield agui.encode(frame)
            except asyncio.CancelledError:
                # 客户端 abort(Stop 按钮)/ 掉线。**不能吞**:要让 ASGI 看见取消,
                # 否则连接不释放。收尾帧也发不出去(连接已断),所以只记一句日志。
                for frame in translator.finish_open():
                    yield agui.encode(frame)
                raise
            except Exception as exc:  # noqa: BLE001 一律转 RUN_ERROR,不断连
                yield agui.encode(translator.run_error(
                    f"{type(exc).__name__}: {exc}", code=type(exc).__name__))
            finally:
                web.end(session.id)             # 必须在 finally:否则一次异常就永久卡住

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
            default_model_name=default.model,
            default_model_context_window=default.context_window,
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

    def _mcp_info(spec, bound_by: list[str]) -> schemas.McpServerInfo:
        """把一条 McpServerSpec 投成**只有结构、没有值**的形状。

        三道不算计的脱敏,写在最靠近数据的地方:
          · `env` / `headers` 只取**键名**;
          · `stdio` 的 `command` / `args` 不回(它们是最容易直接塞明文密钥的字段:
            `npx -y xx-mcp --token=sk-…`)—— 只回 "stdio" 这个事实;
          · 非 dict 的 `config`(手改了 mcp.json)不猜,按空处理。
        """
        cfg = spec.config if isinstance(spec.config, dict) else {}
        env = cfg.get("env")
        headers = cfg.get("headers")
        url = str(cfg.get("url", "") or "")
        transport = str(cfg.get("type", "") or "")
        is_stdio = bool(cfg.get("command")) or transport == "stdio"
        return schemas.McpServerInfo(
            name=spec.name,
            transport=transport,
            target=url or ("stdio" if is_stdio else ""),
            env_keys=sorted(env) if isinstance(env, dict) else [],
            header_keys=sorted(headers) if isinstance(headers, dict) else [],
            bound_by=sorted(bound_by),
        )

    @app.get("/api/mcp", response_model=schemas.McpList, dependencies=[Depends(guard)])
    async def list_mcp() -> schemas.McpList:
        """MCP 声明的三处来源:全局 / 项目 / 各 agent 私有,供设置页展示。

        `bound_by` 是**门控的结果**:全局/项目里的 server 只有在某个 agent 的
        `mcp_servers` 里被声明才算"绑上了"(凭证敏感 → 默认无、显式声明)。
        没有任何"已连接"字段 —— v1 没有 MCP client,那种状态不存在。
        """
        runtime = web.runtime_for(web.default_cwd)
        units = runtime.registry.all()
        bound: dict[str, list[str]] = {}
        for unit in units:
            for spec in unit.mcp_declared:
                bound.setdefault(spec.name, []).append(unit.name)
        sources: list[schemas.McpSource] = []
        for scope, path, servers in load_mcp_scopes(web.default_cwd):
            sources.append(schemas.McpSource(
                scope=scope, path=str(path), exists=path.is_file(),
                servers=[_mcp_info(s, bound.get(s.name, [])) for s in servers],
            ))
        for unit in units:
            if not unit.mcp_private:
                continue
            f = unit.path / "mcp.json"
            sources.append(schemas.McpSource(
                scope="agent", owner=unit.name, path=str(f), exists=f.is_file(),
                servers=[_mcp_info(s, [unit.name]) for s in unit.mcp_private],
            ))
        return schemas.McpList(sources=sources)

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
