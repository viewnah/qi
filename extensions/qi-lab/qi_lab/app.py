"""qi-lab HTTP 宿主(design/web.md §3):REST + SSE + 静态托管。

同进程直连 `QiRuntime.stream()` —— web 是 CLI/TUI 之外的**第三个 consumer**,零协议桥。
契约版本见 `schemas.CONTRACT_VERSION`;前端从 `/api/meta` 读取并据此降级。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from qi_agent import __version__
from qi_agent.auth import AuthStore, resolve_key
from qi_agent.config import (
    ConfigError,
    load_config,
    resolve_default_model,
    resolve_router_model,
    selectable_models,
)
from qi_agent.llm import THINKING_LEVELS
from qi_agent.loader import LoadError
from qi_agent.session import usage_summary
from qi_agent.workspaces import WorkspaceStore, normalize
from . import (activity, agui, agents as agentapi, browse, files as fileapi,
               schemas, skills as skillapi)
from .security import check_credentials, check_host, mask_key
from .serve import pin_role, remote_role_for
from .state import RunBusy, WebState

STATIC_DIR = Path(__file__).parent / "static"
#: 手写页面目录。**不是** Vite 的 outDir(`static/`) —— 那里 `emptyOutDir: true`,
#: 下一次 `npm run build` 会把手写的文件一起清掉。
PAGES_DIR = Path(__file__).parent / "pages"
DEFAULT_WINDOW = 200          # 会话明细默认窗口(渐进恢复:老会话不因细节太多而卡住)

#: SSE 心跳间隔(秒):队列空闲这么久就发一行注释,顶开“攒够字节/时间才转发”的中间层
#: (内网穿透 / 远程端口映射 / 反代)。太小会白占带宽,太大则中间层的阈值先到 —— 10s 是
#: 常见的 keep-alive 量级。
STREAM_HEARTBEAT_S = 10.0


def _int_field(obj: object, name: str) -> int:
    """取一个 int 字段(缺省 0)。**字段写坏不该把整页打成 500** —— `/api/models` 是个
    只读页面,为一个手写配置里写歪的 `contextWindow` 报 500 得不偿失。

    与 `llm.py` / `runtime.py` / `session.py` / `tui.py` 里的 `_as_int` 同一口径。
    """
    try:
        return int(getattr(obj, name, 0) or 0)
    except (TypeError, ValueError):
        return 0

_PLACEHOLDER = """<!doctype html><meta charset="utf-8"><title>qi lab</title>
<style>body{{font:14px/1.7 -apple-system,"PingFang SC",sans-serif;max-width:640px;
margin:72px auto;padding:0 24px;color:#0F1115;background:#F5F6F7}}
code{{background:#fff;padding:2px 6px;border-radius:4px;border:1px solid #E5E5E5}}
h1{{font-size:18px}}</style>
<h1>qi-lab 宿主已启动,但前端产物不存在</h1>
<p>API 可直接使用(<code>/api/meta</code>、<code>/api/sessions</code>、SSE 事件流)。</p>
<p>构建前端:<code>cd extensions/qi-lab/ui &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>构建产物会被放进 <code>extensions/qi-lab/qi_lab/static/</code>,刷新本页即可。</p>"""


def _store_available() -> bool:
    """`qi-store` 装了吗?(只 import,不发请求)。

    面板据此决定摆不摆"安装 / 卸载"按钮 —— 与 `mcp_write` 同一条降级规矩:
    少一排按钮比点了才吃 404 好。
    """
    try:
        import qi_store.service  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _kb_available() -> bool:
    """`qi-kb` 装了吗?(只 import,不发请求)。"""
    try:
        import qi_kb.service  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


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
            raise HTTPException(status_code=400, detail="Host 头不被允许(见 QI_LAB_ALLOWED_HOSTS)")
        auth = request.headers.get("authorization")
        if not check_credentials(auth, password):
            raise HTTPException(status_code=401, detail="需要凭证",
                                headers={"WWW-Authenticate": 'Basic realm="qi-lab"'})

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
                          # 模型面:v1 只有 `/api/config` 的只读回声 —— 界面能看见模型、
                          # 却换不了。这一版补上清单与切换(`/api/models` + `/api/model`),
                          # 老前端不认识这两个开关时照旧只显示,不影响它自己运行。
                          "model_switch": True,
                          # MCP 面:写端点(增删改 `/api/mcp`)。老宿主没有它 →
                          # 界面把那一节当只读(写请求会 404/405),不影响读。
                          "mcp_write": True,
                          # 技能面:写端点(添加 / 删除 / 导入 `/api/skills*`)。
                          # 同上:没有它 → 技能那一节只读展示(读端点一直在)。
                          "skill_write": True,
                          # 角色面:删除端点(`DELETE /api/agents`)。同上:没有它 →
                          # Agent 那一节只读展示(读端点一直在),不摆一个点了才 404 的键。
                          "agent_write": True,
                          # A2A 面:条目增删改(`/api/a2a`)与名片探测(`/api/a2a/probe`)。
                          # 同上:没有它 → A2A 那一节只读展示(读端点一直在)。
                          "a2a_write": True,
                          # 商店面:**真的去 import 一次** qi-store,而不是写死 True ——
                          # 没装这个扩展时那一节必须自己说清楚,而不是摆一排点了就 404 的键。
                          "store_write": _store_available(),
                          # 知识库面：qi-kb 的 provider / KB / 检索问答。
                          "kb_write": _kb_available(),
                          # 扩展面:插件据这两个开关决定要不要发结构化 UI。
                          # `ag_ui` = 事件形状是 AG-UI;`ui_v1` = 认 details["ui"] 的词汇表
                          # (见 design/web.md §16.2)。缺哪个都退回"折叠显示原始 JSON"。
                          "ag_ui": True, "ui_v1": True,
                          # 直播面:委派过程的 `ACTIVITY_SNAPSHOT` 帧(qi-agents → 本端,
                          # 见 design/web-lab.md 二期)。没有它 = 运行中看不到子运行在干什么
                          # (结束后照旧走 details + result 分段)。
                          "activity": True},
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

    # ── 角色与 MCP:走扩展的真实数据(P-E5 ③ 切片 3c,用户选 A)──
    @app.get("/api/agents", response_model=schemas.AgentList, dependencies=[Depends(guard)])
    async def list_agents() -> schemas.AgentList:
        """角色列表 —— 数据来自 **qi-agents**(`discover()`)。

        qi-agents 没装 → `unavailable=True`(界面说清楚),而不是 500:Web 界面不该因为
        少装一个扩展就整页报错。
        """
        try:
            from qi_agents.discovery import discover
        except ImportError:
            return schemas.AgentList(unavailable=True)
        roles = discover(web.default_cwd, "both")
        return schemas.AgentList(agents=[
            schemas.AgentInfo(name=r.name, description=r.description, source=r.source,
                              tools=list(r.tools or []), model=r.model or "",
                              path=str(r.path or ""),
                              mcp=_role_mcp_count(r.path))
            for r in sorted(roles.values(), key=lambda r: r.name)])

    @app.get("/api/mcp", response_model=schemas.McpList, dependencies=[Depends(guard)])
    async def list_mcp() -> schemas.McpList:
        """MCP 声明表:qi 两层 + 各角色私有(装了 qi-agents 才枚举得出角色那层)。

        **值一律不出宿主**(只有键名;stdio 的 command/args 也不给)—— 由测试钉住。
        """
        try:
            from qi_mcp.config import load_layers, read_file
        except ImportError:
            return schemas.McpList(unavailable=True)
        sources: list[schemas.McpSource] = []
        for scope, path, servers in load_layers(web.default_cwd):
            sources.append(schemas.McpSource(
                scope=scope, path=str(path), exists=path.is_file(),
                servers=[_mcp_server_info(n, c, scope) for n, c in servers.items()]))
        for role_path in _role_dirs(web.default_cwd):
            declared = _role_mcp_file(role_path)
            if declared is None:
                continue
            path, servers = declared
            sources.append(schemas.McpSource(
                scope="role", path=str(path), exists=True, role=role_path.name,
                servers=[_mcp_server_info(n, c, "role") for n, c in servers.items()]))
        return schemas.McpList(sources=sources)

    # ── MCP 声明的写操作(增删改)────────────────────
    # 三件事必须做对:
    #   1. **确认头**(与写凭证同一道闸):`.qi/mcp.json` 跟着仓库走,一次误写会被提交出去;
    #   2. **写下去就得连得上**:传输在闭集里、载荷字段齐全,否则 400 —— 连不上的声明
    #      不会报错,它只是永远不生效,那是最难查的一种;
    #   3. **响应回结构、不回值**:三个端点都回 `McpList`,界面一次往返就刷新完。

    @app.post("/api/mcp", status_code=201, response_model=schemas.McpList,
              dependencies=[Depends(guard)])
    async def create_mcp(body: schemas.McpWrite,
                         confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> schemas.McpList:
        """在 `scope` 那层新增一个 server(文件不存在就建)。"""
        confirm_gate(confirm)
        name = _check_mcp_name(body.server.name)
        path = _mcp_target(body.scope, body.role, web.default_cwd)
        servers = _read_mcp_servers(path)
        if name in servers:
            raise HTTPException(status_code=409, detail=f"该层已有同名 server: {name}")
        config = _build_mcp_config(body.server)
        _validate_mcp_config(config)
        servers[name] = config
        _write_mcp_servers(path, servers)
        return await list_mcp()

    @app.patch("/api/mcp", response_model=schemas.McpList, dependencies=[Depends(guard)])
    async def patch_mcp(body: schemas.McpPatch,
                        confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> schemas.McpList:
        """改一个 server:**只动送来的键**(见 `McpServerPatch`),没送的原样留着。

        换传输时会清掉旧传输的载荷字段(command/args/cwd/url/socket)—— 留着的话文件里
        会躺着一份“按 type 走 http、却还带 command”的残骸,读端按结构猜传输时会被误导。
        """
        confirm_gate(confirm)
        target = _check_mcp_name(body.name)
        path = _mcp_target(body.scope, body.role, web.default_cwd)
        servers = _read_mcp_servers(path)
        if target not in servers:
            raise HTTPException(status_code=404, detail=f"该层没有这个 server: {target}")
        config = _apply_mcp_patch(servers[target], body.patch)
        _validate_mcp_config(config)
        new_name = target
        if "name" in body.patch.model_fields_set and body.patch.name is not None:
            new_name = _check_mcp_name(body.patch.name)
            if new_name != target and new_name in servers:
                raise HTTPException(status_code=409, detail=f"该层已有同名 server: {new_name}")
        # 按**原顺序**重建:改名只是换键,不该把这一层的排列搅乱
        updated = {new_name if key == target else key: (config if key == target else value)
                   for key, value in servers.items()}
        _write_mcp_servers(path, updated)
        return await list_mcp()

    @app.delete("/api/mcp", response_model=schemas.McpList, dependencies=[Depends(guard)])
    async def delete_mcp(body: schemas.McpDelete,
                         confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> schemas.McpList:
        """删一个 server。**只删这一层的键**:同名的下层声明会露出来(覆盖关系没变)。"""
        confirm_gate(confirm)
        name = _check_mcp_name(body.name)
        path = _mcp_target(body.scope, body.role, web.default_cwd)
        servers = _read_mcp_servers(path)
        if name not in servers:
            raise HTTPException(status_code=404, detail=f"该层没有这个 server: {name}")
        servers.pop(name)
        _write_mcp_servers(path, servers)
        return await list_mcp()

    # ── A2A 条目(两层 a2a.json):读 + 增删改 + 探测 ──────
    # 与 MCP 同一套规矩(确认头、只写读端指的那一层、值只进不回),两处不同:
    #   · a2a.json 只有两层(global / project),**没有**角色私有那层;
    #   · 合并是**同名深合并**,所以读端除了逐层原样,还回一份 `resolved` 生效视图 ——
    #     否则界面上看到的永远是"某层写了什么",而不是"现在到底连的是哪"。

    @app.get("/api/a2a", response_model=schemas.A2AList, dependencies=[Depends(guard)])
    async def list_a2a() -> schemas.A2AList:
        return _a2a_list(web.default_cwd)

    @app.post("/api/a2a", status_code=201, response_model=schemas.A2AList,
              dependencies=[Depends(guard)])
    async def create_a2a(body: schemas.A2AWrite,
                         confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> schemas.A2AList:
        """在 `scope` 那层新增一个条目(文件不存在就建)。"""
        confirm_gate(confirm)
        name = _check_a2a_name(body.entry.name)
        path = _a2a_target(body.scope, web.default_cwd)
        entries = _read_a2a_entries(path)
        if name in entries:
            raise HTTPException(status_code=409, detail=f"该层已有同名条目: {name}")
        config = _build_a2a_config(body.entry)
        _validate_a2a_config(name, config, scope=body.scope, cwd=web.default_cwd)
        entries[name] = config
        _write_a2a_entries(path, entries)
        return _a2a_list(web.default_cwd)

    @app.patch("/api/a2a", response_model=schemas.A2AList, dependencies=[Depends(guard)])
    async def patch_a2a(body: schemas.A2APatch,
                        confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> schemas.A2AList:
        """改一个条目:**只动送来的键**,没送的原样留着(见 `A2AEntryPatch`)。"""
        confirm_gate(confirm)
        target = _check_a2a_name(body.name)
        path = _a2a_target(body.scope, web.default_cwd)
        entries = _read_a2a_entries(path)
        if target not in entries:
            raise HTTPException(status_code=404, detail=f"该层没有这个条目: {target}")
        config = _apply_a2a_patch(entries[target], body.patch)
        new_name = target
        if "name" in body.patch.model_fields_set and body.patch.name is not None:
            new_name = _check_a2a_name(body.patch.name)
            if new_name != target and new_name in entries:
                raise HTTPException(status_code=409, detail=f"该层已有同名条目: {new_name}")
        # 校验用的是**合并后**的那份:把 baseUrl 删掉却什么都没有,必须当场报错,
        # 而不是写进文件里等运行时才发现。
        _validate_a2a_config(new_name, config, scope=body.scope, cwd=web.default_cwd)
        updated = {new_name if key == target else key: (config if key == target else value)
                   for key, value in entries.items()}
        _write_a2a_entries(path, updated)
        return _a2a_list(web.default_cwd)

    @app.delete("/api/a2a", response_model=schemas.A2AList, dependencies=[Depends(guard)])
    async def delete_a2a(body: schemas.A2ADelete,
                         confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> schemas.A2AList:
        """删一个条目。**只删这一层的键**:同名的下层声明会露出来(深合并关系没变)。"""
        confirm_gate(confirm)
        name = _check_a2a_name(body.name)
        path = _a2a_target(body.scope, web.default_cwd)
        entries = _read_a2a_entries(path)
        if name not in entries:
            raise HTTPException(status_code=404, detail=f"该层没有这个条目: {name}")
        entries.pop(name)
        _write_a2a_entries(path, entries)
        return _a2a_list(web.default_cwd)

    @app.post("/api/a2a/probe", response_model=schemas.A2AProbeResult,
              dependencies=[Depends(guard)])
    async def probe_a2a(body: schemas.A2AProbeRequest) -> schemas.A2AProbeResult:
        """去真实端点拉一次名片,回答"我配的对不对"。

        **只探测已配置的条目**(不收任意 URL)—— 否则这个端点就变成了内网探测器。
        走的是 qi-a2a 工具的同一条代码路径(`fetch_cards`),所以这里能看到什么,
        模型调用时就能看到什么。
        """
        return await _probe_a2a(web.default_cwd, body.server, body.agent)

    # ── 角色的删除(只删,不新建)────────────────────────
    # 为什么只有删除:角色的**内容**是一段提示词 + frontmatter 的工具/模型限制,它的
    # 写入格式由 qi-agents 定义与校验(`_read_role` 决定一份 agent.md 算不算角色)——
    # 这个宿主不替它发明一个“看起来能写”的格式。而“删掉一个放错了/不再用的角色”是
    # 边界清楚的文件操作:两层都是 qi 体系写出来的目录,删完读模型立刻少一条。
    # 三件事必须做对:
    #   1. **确认头**(与写凭证、写 MCP、写技能同一道闸):角色目录里还可能带着自己的
    #      `mcp.json`,一次误删连私有声明一起没;
    #   2. **名字是查找键,不拼路径**:目标由 `qi_agents.discovery.discover()` 给,与
    #      `GET /api/agents` 同一条代码路径 —— 界面显示什么就删什么(见 `qi_lab.agents`);
    #   3. **回新清单**:回 `AgentList`,界面一次往返就刷新完(读端点不回值 —— 角色
    #      的提示词正文从来没出过这个宿主)。
    # 与技能不同,这里**没有** `web.refresh_skills()` 的等价物:runtime 缓存的是
    # `top_skills`,角色是每次派单现 `discover()` 的,删完下一次调用就看不见它了。

    @app.delete("/api/agents", response_model=schemas.AgentList, dependencies=[Depends(guard)])
    async def delete_agent(body: schemas.AgentDelete,
                           confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> schemas.AgentList:
        """按读模型的名字删掉**该层**的一个角色(整目录)。

        只删这一层:项目层同名覆盖用户层,删用户层那份不会把项目层的也带走。
        qi-agents 没装 → 503(与读端的 `unavailable=True` 同一件事的两种说法:
        读可以降级成“没角色”,删没有可删的对象,不能装作删成功了)。
        """
        confirm_gate(confirm)
        try:
            agentapi.remove(web.default_cwd, body.scope, body.name)
        except agentapi.AgentError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return await list_agents()

    # ── 技能的写操作:添加 / 删除 / 导入 ──────────────────
    # 三件事必须做对:
    #   1. **确认头**(与写凭证、写 MCP 同一道闸):技能是**代码** —— SKILL.md 会被
    #      原样读进提示词,一次误写等于往模型上下文里塞东西,而它比 mcp.json 更容易
    #      被 `git add .` 带走(项目层就在 `.qi/skills/`);
    #   2. **写完重扫 runtime**:runtime 按 cwd 缓存 `top_skills`,不重扫的话会出现
    #      “GET 说它在、模型却用不上”;`web.refresh_skills()` 就是干这个的;
    #   3. **回新清单**:三个端点都回 `SkillList`,界面一次往返就刷新完。
    #      (读端点不回值 —— 技能没有需要遮的东西,`path` 本来就在读端给。)
    # 为什么只有两个 scope:`global` = `~/.qi/agent/skills`,`project` = `<cwd>/.qi/skills`。
    # `.agents/skills`(别的工具的目录)与 settings 的 `skills[]`(用户手指定的路径)
    # 不可写 —— 边界写在 `qi_lab.skills` 的模块说明里。

    def _skill_error(exc: skillapi.SkillError) -> HTTPException:
        """`SkillError.status` 就是它要回的 4xx(400/404/409)。"""
        return HTTPException(status_code=exc.status, detail=str(exc))

    @app.post("/api/skills", status_code=201, response_model=schemas.SkillList,
              dependencies=[Depends(guard)])
    async def create_skill(body: schemas.SkillWrite,
                           confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> schemas.SkillList:
        """新建一个技能:`<scope 那层>/<name>/SKILL.md`(frontmatter 带 name/description)。"""
        confirm_gate(confirm)
        try:
            root = skillapi.skill_root(body.scope, web.default_cwd)
            name = skillapi.check_name(body.name)
            skillapi.create(root, name, body.description, body.body)
        except skillapi.SkillError as exc:
            raise _skill_error(exc) from exc
        web.refresh_skills()
        return await list_skills()

    @app.delete("/api/skills", response_model=schemas.SkillList,
                dependencies=[Depends(guard)])
    async def delete_skill(body: schemas.SkillDelete,
                           confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> schemas.SkillList:
        """按**显示名**删掉该层的一个技能:整目录(`SKILL.md` + references/ 之类)。

        只删这一层 —— 跨层同名是覆盖关系,下层同名技能会露出来而不是被一起删掉。
        """
        confirm_gate(confirm)
        try:
            root = skillapi.skill_root(body.scope, web.default_cwd)
            skillapi.remove(root, skillapi.check_name(body.name))
        except skillapi.SkillError as exc:
            raise _skill_error(exc) from exc
        web.refresh_skills()
        return await list_skills()

    @app.post("/api/skills/import", status_code=201, response_model=schemas.SkillList,
              dependencies=[Depends(guard)])
    async def import_skill(body: schemas.SkillImport,
                           confirm: str | None = Header(None, alias="X-Qi-Confirm")) -> schemas.SkillList:
        """把本机路径上的一个技能导进 `scope` 那层。

        `source` 可以是技能目录、单份 `.md`、或 `.zip`(里面要能定位到唯一的 SKILL.md)。
        **不接收上传体**:这个宿主跑在本机,导入指的是“从磁盘上拿”,不是把字节
        推上来(推字节那条路要 multipart,属于另一件事)。
        """
        confirm_gate(confirm)
        try:
            root = skillapi.skill_root(body.scope, web.default_cwd)
            skillapi.import_skill(root, body.source, body.name)
        except skillapi.SkillError as exc:
            raise _skill_error(exc) from exc
        web.refresh_skills()
        return await list_skills()

    @app.post("/api/skills/reload", response_model=schemas.SkillList,
              dependencies=[Depends(guard)])
    async def reload_skills(confirm: str | None = Header(None, alias="X-Qi-Confirm")
                            ) -> schemas.SkillList:
        """重扫技能目录。**不改任何文件,只让缓存跟上磁盘。**

        为什么需要它:

        * `/api/skills` 读的是 `runtime.top_skills`(按 cwd 缓存的快照);
        * 缓存只在**写端点**里重扫 —— 于是一段"没经过本宿主"的变更(手工拷目录、
          另一个进程/CLI 装的、编辑器改了 frontmatter)在页面上永远看不见;
        * TUI 有 `/reload`,web 以前只能重启进程。

        仍然要确认头:不是为了防误操作(它不写文件),而是为了保住
        **"所有 `POST /api/skills*` 一律带确认头"** 这条一眼可记的规矩 ——
        以后新增端点的人不需要判断"这个算不算写"。
        """
        confirm_gate(confirm)
        web.refresh_skills()
        return await list_skills()

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
        # 改标题走 store 的方法:内存 + header entry + 落盘三处一起改(与自动命名共用)。
        web.sessions.set_title(session, body.title)
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
    # 在某个目录下新建会话就是新建工作区。三种方法的语义见 design/web.md §18.3。

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

    # ── 会话目录内的文件(右侧文件面板)──────────────────────────
    #
    # **边界与文件工具同一条**(`files.guard` 抄自 `ToolContext.guard`):路径必须落在
    # 会话 cwd 内,软链也跟随后判定。同一份数据不该有两个边界 —— 工具读不到的,界面也不读。
    #
    # 三个端点各管一件事:列一层 / 读文本 / 读原字节。原字节那条**只给白名单类型**,
    # 而且对 HTML 加 `sandbox` 响应头(见 `files.py` 的模块注释)。

    def _file_root(session: str | None) -> tuple[Path, str]:
        """取这次请求的根目录:会话的 cwd(没有会话 → 默认 cwd)。"""
        if session:
            found = web.sessions.get(session)
            if found is None:
                raise HTTPException(status_code=404, detail="会话不存在")
            return web.session_cwd(found), session
        return web.default_cwd, ""

    @app.get("/api/files", response_model=schemas.FileListing,
             dependencies=[Depends(guard)])
    async def list_files(path: str = Query(""),
                         session: str | None = Query(None)) -> schemas.FileListing:
        """列**一层**目录(前端懒展开)。"""
        root, _sid = _file_root(session)
        try:
            absolute, entries = fileapi.list_dir(root, path)
        except fileapi.FileAccessError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="目录不存在") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="没有权限读取这个目录") from None
        parent = Path(path).parent.as_posix() if path else None
        if parent == ".":
            parent = ""
        return schemas.FileListing(
            root=str(root.resolve(strict=False)), path=absolute, parent=parent,
            entries=[schemas.FileEntry(name=e.name, path=e.path, kind=e.kind,
                                       size=e.size, mtime=e.mtime, raw=e.raw)
                     for e in entries],
        )

    @app.get("/api/files/content", response_model=schemas.FileContent,
             dependencies=[Depends(guard)])
    async def file_content(path: str = Query(...),
                           session: str | None = Query(None)) -> schemas.FileContent:
        """文本预览。二进制 / 太大**不算错误**:回 kind 说明,前端显示一行提示。"""
        root, _sid = _file_root(session)
        try:
            read = fileapi.read_text(root, path)
        except fileapi.FileAccessError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="文件不存在") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="没有权限读取这个文件") from None
        return schemas.FileContent(
            path=path, kind=read.kind, size=read.size, truncated=read.truncated,
            # 二进制不给语言提示(它不预览)
            lang=fileapi.lang_of(path) if read.kind == "text" else "",
            text=read.text,
        )

    @app.get("/api/files/raw", dependencies=[Depends(guard)])
    async def file_raw(path: str = Query(...),
                       session: str | None = Query(None)) -> Response:
        """原字节预览(图片 / PDF / HTML)。

        两条响应头是安全设计的一部分,不是可选项:
          · `X-Content-Type-Options: nosniff` —— 别让浏览器把回的东西当别的类型解释;
          · `Content-Security-Policy: sandbox`(只对 HTML)—— 把预览的 HTML 放进一个
            **不透明源**:脚本不执行、拿不到本站的 cookie/凭证。前端那头还会再加一层
            `<iframe sandbox>`(双保险)。
        """
        root, _sid = _file_root(session)
        try:
            blob, media = fileapi.read_bytes(root, path)
        except fileapi.FileAccessError as exc:
            code = 415 if "不支持原样预览" in str(exc) else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="文件不存在") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="没有权限读取这个文件") from None
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
            "Cache-Control": "no-store",
        }
        if media == "text/html":
            headers["Content-Security-Policy"] = "sandbox"
        return Response(content=blob, media_type=media, headers=headers)

    # ── AG-UI:单次 POST,响应即流 ──────────────────────────────
    #
    # 这是 design/web.md 记的**破坏性替换**:旧的 `POST /turn`(202)+ `POST /cancel`
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
            runtime = web.runtime_for(web.session_cwd(session))
            # 直播队列与主流**同一个**(design/web-lab.md §5.2 通道 A):子运行期间
            # 主流是静默的(LLM 调用不产事件),活动帧如果走旁路就会抢在工具卡帧
            # 之前到 —— 同队列 FIFO 把顺序钉死。
            feed = web.open_activity()
            pump: asyncio.Task[None] | None = None
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
                # **绑定会话**:web 是"一个 runtime 服务同一 cwd 下的多个会话",而
                # `_active_session` 只在一个回合内有效 —— 不显式绑定,两件事同时坏掉:
                #   ① 换模型/级别的落盘(`model_change` / `thinking_level_change`)写不进
                #      "哪个文件"(web 端此前没有任何 `/model` 落盘点);
                #   ② 续会话不还原上次用的模型(runtime 用一个模型发请求、界面上写着另一个)。
                # `bind_session` 同时做还原,所以每轮**开跑前**调一次(它幂等:同一会话的
                # 还原只做一次 —— `_restored_session` 记的是"上一轮绑的那个 id")。
                await web.bind(runtime, session, reason="resume")
                # 「智能体 chip」的落点。**不能**靠 `stream(agent_override=…)`:那是 P-E4c
                # 之前的接口,core 收窄成单 agent 之后流水线里已经没人读它(传了不报错、
                # 只是没生效)。角色现在是 qi-agents 的 `before_agent_start`,
                # 所以这里把钉住值交给它(详见 `serve.pin_role`)。
                #
                # 会话级知识库选择：qi-lab 只把 forwardedProps.kbIds 记进 qi-kb 的进程内
                # scope；before_agent_start 与本轮的工具调用都按它过滤。空 = 用全局绑定。
                try:
                    kb_service, _kb_reason = _kb()
                    if kb_service is not None and (inp.kb_ids or inp.kb_provider):
                        kb_service.set_session_scope(session.id, inp.kb_ids, inp.kb_provider)
                except Exception:  # noqa: BLE001 没装 qi-kb / 配置坏了不影响正常对话
                    pass

                # **远端角色是另一条路**:它没有本地提示词,拼提示词等于没拼 ——
                # 所以整轮转发给远端(`serve.remote_role_for` 判,与 `/api/agents` 同一份数据)。
                remote = remote_role_for(session.cwd or web.default_cwd, inp.agent)
                if remote is not None:
                    async for frame in _remote_turn(remote, text, session, runtime,
                                                     translator, inp.run_id):
                        yield frame
                    return
                pin_role(runtime, inp.agent)

                async def pump_stream() -> None:
                    """把主流灌进队列(翻译、不直接 yield)。

                    直播帧由 bus handler 直接进同一个队列,所以"子运行的静默期"
                    不必等下一个主流事件。异常在这里转成 `RUN_ERROR` 帧再收尾:
                    这个 task 的异常不会冒到外层,不转帧外层就会永远等下去。
                    """
                    try:
                        async for ev in runtime.stream(text, session):
                            for frame in translator.feed(ev):
                                feed.put_nowait((activity.KIND_FRAME, frame))
                    except asyncio.CancelledError:
                        raise                 # 取消:外层 finally 会 cancel 本 task
                    except Exception as exc:  # noqa: BLE001 一律转 RUN_ERROR,不断连
                        feed.put_nowait((activity.KIND_FRAME, translator.run_error(
                            f"{type(exc).__name__}: {exc}", code=type(exc).__name__)))
                    else:
                        # 流正常结束:补收尾(万一最后一帧还开着文本/思考)
                        for frame in translator.finish_open():
                            feed.put_nowait((activity.KIND_FRAME, frame))
                    finally:
                        feed.put_nowait((activity.KIND_END, None))

                pump = asyncio.create_task(pump_stream())
                async for frame in agui.mixed_frames(feed, translator,
                                                     heartbeat_s=STREAM_HEARTBEAT_S):
                    if frame is agui.HEARTBEAT:
                        # SSE 注释行(`:` 开头):客户端忽略,但支够顶开“攒够字节/时间才转发”
                        # 的中间层 —— 内网穿透 / 远程端口映射 / 反代都可能这么干,表现就是
                        # “直播要等整个回合结束才一次到齐”。
                        yield ": ping\n\n"
                        continue
                    yield agui.encode(frame)
            except asyncio.CancelledError:
                # 客户端 abort(Stop 按钮)/ 掉线。**不能吞**:要让 ASGI 看见取消,
                # 否则连接不释放。收尾帧也发不出去(连接已断),不再往里 yield;
                # pump 在 finally 里被 cancel → `runtime.stream` 被同一把取消打断
                # (与改造前"直接打断生成器"等价)。
                raise
            except Exception as exc:  # noqa: BLE001 一律转 RUN_ERROR,不断连
                yield agui.encode(translator.run_error(
                    f"{type(exc).__name__}: {exc}", code=type(exc).__name__))
            finally:
                if pump is not None:
                    pump.cancel()        # 不 cancel 会把流留在后台跑到进程结束
                web.close_activity(feed)  # 泄漏的队列会让广播越推越多
                try:
                    kb_service, _kb_reason = _kb()
                    if kb_service is not None:
                        kb_service.clear_session_scope(session.id)
                except Exception:  # noqa: BLE001
                    pass
                web.end(session.id)       # 必须在 finally:否则一次异常就永久卡住

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    # ── agent / 配置(只读) + 凭证(写需二次确认) ──────────
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
        # 只管“在用”的 provider:models.json 里写过的 + 默认/路由模型那个。
        # 预置兜底那批只是目录(见 qi_agent.presets),没配也不算缺。
        #
        # 用 getattr 而不是模块级 import:qi-lab 是**独立安装**的扩展(`qi install`),
        # 用户的 qi-agent 可能还是没有这个符号的旧版本 —— 那种情况下不能把整个扩展顶崩,
        # 退回旧口径(全部 provider 都算在用)即可。
        from qi_agent import config as qi_config

        hook = getattr(qi_config, "in_play_providers", None)
        in_play = (hook(cfg, default_provider=default.provider, router_provider=router.provider)
                   if callable(hook) else set(cfg.providers))
        missing = [p.name for p in providers if p.name in in_play and not p.credential_ok]
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

    # ── 模型:清单 + 切换(会话级)──────────────────────────
    #
    # 为什么需要这一节:web 端此前**只有 `/api/config` 的只读回声** —— 界面上那个模型名
    # 标着"换模型在设置里",而设置页只有 provider 卡片与凭证,没有换模型的入口。
    # 于是 web 能看见模型、却换不了(TUI 有 `/model` / ctrl+l / ctrl+p)。
    #
    # 三个决定:
    #   · **清单口径与 TUI 的 `/model` 完全一致** —— 都走 core 的 `selectable_models()`:
    #     **解析得出凭证的 provider 才列**(`models.json` 里显式写过的也没有豁免,
    #     写了 `$ENV` 却没导出就是“用不了”)。两处各写一份清单,迟早一个改了另一个没改;
    #   · **换模型走 `runtime.set_model()` / `set_thinking_level()`** —— 那是唯一入口,
    #     `model_select` / `thinking_level_select` 事件与 `model_change` /
    #     `thinking_level_change` 的落盘都在里面。界面层不自己拼 entry;
    #   · **带 `session` 时先绑定** —— 一个 runtime 服务同一 cwd 下的多个会话,
    #     不绑定就"不知道该写进哪个文件"(这正是本扩展此前漏掉会话绑定的那一类)。
    #     绑定同时会按会话里记的 entry 还原模型,所以先绑再设,顺序不能反。

    def _session_for(sid: str | None):
        """按 id 取会话(不给则 None)。不存在 → 404(不是静默回落到别的会话)。"""
        if sid is None:
            return None
        session = web.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {sid}")
        return session

    def _current_model_label(runtime) -> str:
        spec = getattr(getattr(runtime, "llm_exec", None), "spec", None)
        if spec is None:
            return ""
        return f"{spec.provider}/{spec.model}"

    @app.get("/api/models", response_model=schemas.ModelCatalog,
             dependencies=[Depends(guard)])
    async def list_models(session: str | None = Query(None)) -> schemas.ModelCatalog:
        """可选模型清单 + 当前值(与 TUI 的 `/model` 同一份口径)。

        `session` 给了就先绑定它 —— 当前值要反映**那条会话**的还原结果,而不是
        runtime 上残留的上一轮状态(一个 runtime 服务多个会话)。
        """
        target = _session_for(session)
        runtime = web.runtime_for(web.session_cwd(target) if target else web.default_cwd)
        if target is not None:
            web.bind_session(runtime, target)
        store = getattr(runtime, "_auth", None)
        options: list[schemas.ModelOption] = []
        for provider, model_id in selectable_models(runtime.cfg, store=store):
            prov = (runtime.cfg.providers or {}).get(provider)
            entry = next((m for m in (prov.models if prov else []) if m.id == model_id), None)
            options.append(schemas.ModelOption(
                provider=provider, id=model_id,
                context_window=_int_field(entry, "contextWindow"),
            ))
        return schemas.ModelCatalog(
            models=options,
            currently=_current_model_label(runtime),
            thinking_level=str(getattr(runtime, "thinking_level", "") or ""),
            thinking_levels=list(THINKING_LEVELS),
        )

    @app.post("/api/model", response_model=schemas.ModelCatalog,
              dependencies=[Depends(guard)])
    async def set_model(body: schemas.ModelSelect) -> schemas.ModelCatalog:
        """换模型 / 换思考级别(下一回合生效),并返回更新后的当前值。

        返回新的清单而不只是 204:界面要在同一次往返里把 chip 上的字与菜单里的 ✓ 一起
        更新掉,否则它会显示"已经切了"而实际没切(或反过来)—— 复用 GET 的那份装配。
        """
        if (body.provider is None) != (body.model is None):
            raise HTTPException(status_code=422,
                                detail="provider 与 model 必须成对给(只给一个会切到半个模型)")
        if body.provider is None and body.thinking_level is None:
            raise HTTPException(status_code=422,
                                detail="至少要给 model 或 thinking_level 之一")

        target = _session_for(body.session)
        runtime = web.runtime_for(web.session_cwd(target) if target else web.default_cwd)
        if target is not None:
            # 先绑:① 落盘要知道写哪个文件;② 绑定会按会话里的 entry 还原,
            # 于是"这次没传的字段"保持会话里的值,而不是被上一次绑定的残留顶掉。
            web.bind_session(runtime, target)
        if body.provider is not None:
            try:
                runtime.set_model(str(body.provider), str(body.model), source="set")
            except Exception as exc:      # noqa: BLE001 配置/凭证问题 → 400,不是 500
                raise HTTPException(
                    status_code=400,
                    detail=f"{type(exc).__name__}: {exc}") from exc
        if body.thinking_level is not None:
            level = str(body.thinking_level).strip().lower()
            if level not in THINKING_LEVELS:
                raise HTTPException(status_code=422,
                                    detail=f"未知思考级别: {body.thinking_level}"
                                           f"(可选 {'/'.join(THINKING_LEVELS)})")
            runtime.set_thinking_level(level, source="set")
        return await list_models(body.session)

    @app.get("/api/skills", response_model=schemas.SkillList, dependencies=[Depends(guard)])
    async def list_skills() -> schemas.SkillList:
        """顶层技能(六层来源 + settings 追加),供设置页展示。"""
        runtime = web.runtime_for(web.default_cwd)
        return schemas.SkillList(skills=[
            schemas.SkillInfo(name=skill.name, description=skill.description,
                              source=skill.source, path=str(skill.path))
            for skill in runtime.top_skills
        ])

    @app.get("/api/extensions", response_model=schemas.ExtensionList, dependencies=[Depends(guard)])
    async def list_extensions() -> schemas.ExtensionList:
        runtime = web.runtime_for(web.default_cwd)
        return schemas.ExtensionList(extensions=list(runtime.extensions))

    # ── 商店(qi-store)──────────────────────────────────
    #
    # 消费方式与 `qi_mcp.config` / `qi_agents.discovery` 一致:**进程内懒 import**。
    # qi-store 没装 → 回 `available=false`,不让整页 500。
    #
    # 三条规矩:
    # 1. **所有网络调用走 `asyncio.to_thread`**:qi_store 是同步客户端(httpx.Client),
    #    直接在事件循环里调会把整页卡住;两套客户端也不值得为面板再写一份;
    # 2. **写操作过 `confirm_gate`**(`X-Qi-Confirm: yes`),与 `/api/mcp` `/api/a2a` 同一条;
    # 3. **没有 login 端点**:密码不经手浏览器 —— 面板只读 `status`,登录去终端 `qi store login`。

    def _store():
        """拿到 qi_store 模块;没装/坏了 → None + 原因(调用方回 available=false)。"""
        try:
            from qi_store import service  # noqa: F401
            return service, ""
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    def _store_error(exc: Exception) -> HTTPException:
        """`StoreError` 的 `exit_code` → HTTP 状态码。

        映射是**语义**的,不是数字对齐:2=你给错了(400)、3=该登录(401)、
        4=没这个条目(404)、5=撞了(409)、其余算上游/磁盘问题(502)。
        """
        from qi_store.errors import (EXIT_AUTH, EXIT_CONFLICT, EXIT_NOT_FOUND,
                                     EXIT_USAGE, StoreError)

        if not isinstance(exc, StoreError):
            return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
        status = {EXIT_USAGE: 400, EXIT_AUTH: 401, EXIT_NOT_FOUND: 404,
                  EXIT_CONFLICT: 409}.get(exc.exit_code, 502)
        detail = str(exc) + (f"（{exc.hint}）" if exc.hint else "")
        return HTTPException(status_code=status, detail=detail)

    async def _in_store(fn, /, **kwargs):
        """在线程里跑一次 qi_store 调用,并把 `StoreError` 映射成 HTTP 错误。"""
        try:
            return await asyncio.to_thread(fn, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise _store_error(exc) from exc

    @app.get("/api/store/status", response_model=schemas.StoreStatus,
             dependencies=[Depends(guard)])
    async def store_status() -> schemas.StoreStatus:
        service, reason = _store()
        if service is None:
            return schemas.StoreStatus(available=False, error=reason)
        try:
            data = await asyncio.to_thread(service.status, web.default_cwd)
        except Exception as exc:  # noqa: BLE001
            # 配置坏了也不报 500:面板要能画出来并告诉你哪一步坏了
            return schemas.StoreStatus(available=False, error=str(exc))
        return schemas.StoreStatus(**data)

    # ── 登录 / 退出 ────────────────────────────────────────
    #
    # **密码只在这里过一次**:service.login 把它交给上游就丢,响应是 StoreStatus
    # (只有登录态),令牌不出宿主。写凭证 = 写操作 → 与其它写端点同一条确认闸。

    @app.post("/api/store/login", response_model=schemas.StoreStatus,
              dependencies=[Depends(guard)])
    async def store_login(body: schemas.StoreLoginRequest,
                          confirm: str | None = Header(None, alias="X-Qi-Confirm")
                          ) -> schemas.StoreStatus:
        confirm_gate(confirm)
        service, reason = _store()
        if service is None:
            return schemas.StoreStatus(available=False, error=reason)
        try:
            data = await asyncio.to_thread(
                service.login, web.default_cwd,
                login=body.login.strip(), password=body.password,
                base_url=body.baseUrl.strip() or None, store=body.store.strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise _store_error(exc) from exc
        return schemas.StoreStatus(**data)

    @app.post("/api/store/logout", response_model=schemas.StoreStatus,
              dependencies=[Depends(guard)])
    async def store_logout(body: schemas.StoreLogoutRequest,
                           confirm: str | None = Header(None, alias="X-Qi-Confirm")
                           ) -> schemas.StoreStatus:
        confirm_gate(confirm)
        service, reason = _store()
        if service is None:
            return schemas.StoreStatus(available=False, error=reason)
        data = await _in_store(service.logout, cwd=web.default_cwd,
                               store=body.store.strip() or None, all_stores=body.all)
        return schemas.StoreStatus(**data)

    @app.get("/api/store/search", response_model=schemas.StoreList,
             dependencies=[Depends(guard)])
    async def store_search(q: str | None = Query(None),
                           kind: str | None = Query(None),
                           limit: int = Query(0, ge=0, le=200),
                           offline: bool = Query(False)) -> schemas.StoreList:
        service, reason = _store()
        if service is None:
            return schemas.StoreList(available=False, error=reason)
        data = await _in_store(service.search, cwd=web.default_cwd, q=q, kind=kind,
                               limit=limit, offline=offline)
        return schemas.StoreList(**data)

    @app.get("/api/store/installed", response_model=schemas.StoreInstalled,
             dependencies=[Depends(guard)])
    async def store_installed() -> schemas.StoreInstalled:
        service, reason = _store()
        if service is None:
            return schemas.StoreInstalled(available=False, error=reason)
        data = await _in_store(service.installed, cwd=web.default_cwd)
        return schemas.StoreInstalled(**data)

    @app.get("/api/store/check", response_model=schemas.StoreCheck,
             dependencies=[Depends(guard)])
    async def store_check(offline: bool = Query(False)) -> schemas.StoreCheck:
        service, reason = _store()
        if service is None:
            return schemas.StoreCheck(available=False, error=reason)
        data = await _in_store(service.check, cwd=web.default_cwd, offline=offline)
        return schemas.StoreCheck(**data)

    @app.post("/api/store/plan", response_model=schemas.StorePlan,
              dependencies=[Depends(guard)])
    async def store_plan(body: schemas.StorePlanRequest) -> schemas.StorePlan:
        """**只算不写**。前端装配前必须先看这个(写哪里/会不会撞/要补什么)。"""
        if not body.ids:
            raise HTTPException(status_code=400, detail="ids 不能为空")
        service, reason = _store()
        if service is None:
            return schemas.StorePlan(available=False, error=reason)
        data = await _in_store(service.plan, cwd=web.default_cwd, ids=body.ids,
                               scope=body.scope, force=body.force)
        return schemas.StorePlan(**data)

    @app.post("/api/store/install", response_model=schemas.StoreInstallResult,
              dependencies=[Depends(guard)])
    async def store_install(body: schemas.StoreInstallRequest,
                            confirm: str | None = Header(None, alias="X-Qi-Confirm")
                            ) -> schemas.StoreInstallResult:
        confirm_gate(confirm)
        if not body.ids:
            raise HTTPException(status_code=400, detail="ids 不能为空")
        service, reason = _store()
        if service is None:
            return schemas.StoreInstallResult(available=False, error=reason)
        data = await _in_store(service.install, cwd=web.default_cwd, ids=body.ids,
                               scope=body.scope, force=body.force,
                               allow_unsafe=body.allowUnsafe)
        if any(item.get("kind") == "skill" for item in data["installed"]):
            # 技能落在磁盘上还不够:**runtime 按 cwd 缓存 `top_skills`**,不重扫就会出现
            # "GET /api/skills 说它在、模型却用不上"。与 `POST /api/skills` 同一条处置。
            web.refresh_skills()
        return schemas.StoreInstallResult(**data)

    @app.post("/api/store/uninstall", response_model=schemas.StoreUninstallResult,
              dependencies=[Depends(guard)])
    async def store_uninstall(body: schemas.StoreUninstallRequest,
                              confirm: str | None = Header(None, alias="X-Qi-Confirm")
                              ) -> schemas.StoreUninstallResult:
        confirm_gate(confirm)
        if not body.id:
            raise HTTPException(status_code=400, detail="id 不能为空")
        service, reason = _store()
        if service is None:
            return schemas.StoreUninstallResult(available=False, error=reason)
        data = await _in_store(service.uninstall, cwd=web.default_cwd, id=body.id,
                               scope=body.scope)
        if data.get("kind") == "skill":
            web.refresh_skills()      # 卸掉的技能也要从缓存里消失,否则模型还能用
        return schemas.StoreUninstallResult(**data)

    # ── 知识库（qi-kb）──────────────────────────────────
    #
    # 与商店同一套消费方式：进程内懒 import `qi_kb.service`，网络调用走
    # `asyncio.to_thread`，写操作过确认头，没装就 available=false。

    def _kb():
        try:
            from qi_kb import service  # noqa: F401
            return service, ""
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    def _kb_error(exc: Exception) -> HTTPException:
        from qi_kb.errors import (EXIT_AUTH, EXIT_CONFLICT, EXIT_NOT_FOUND,
                                  EXIT_USAGE, KbError)

        if not isinstance(exc, KbError):
            return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
        status = {EXIT_USAGE: 400, EXIT_AUTH: 401, EXIT_NOT_FOUND: 404,
                  EXIT_CONFLICT: 409}.get(exc.exit_code, 502)
        detail = str(exc) + (f"（{exc.hint}）" if exc.hint else "")
        return HTTPException(status_code=status, detail=detail)

    async def _in_kb(fn, /, **kwargs):
        try:
            return await asyncio.to_thread(fn, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise _kb_error(exc) from exc

    @app.get("/api/kb/status", response_model=schemas.KbStatus,
             dependencies=[Depends(guard)])
    async def kb_status() -> schemas.KbStatus:
        service, reason = _kb()
        if service is None:
            return schemas.KbStatus(available=False, error=reason)
        try:
            data = await asyncio.to_thread(service.status, web.default_cwd)
        except Exception as exc:  # noqa: BLE001
            return schemas.KbStatus(available=False, error=str(exc))
        return schemas.KbStatus(**data)

    @app.get("/api/kb/providers", response_model=schemas.KbProviders,
             dependencies=[Depends(guard)])
    async def kb_providers() -> schemas.KbProviders:
        service, reason = _kb()
        if service is None:
            return schemas.KbProviders(error=reason)
        data = await _in_kb(service.providers_list, cwd=web.default_cwd)
        return schemas.KbProviders(**data)

    @app.post("/api/kb/providers", response_model=schemas.KbStatus,
              dependencies=[Depends(guard)])
    async def kb_provider_add(body: schemas.KbProviderAddRequest,
                              confirm: str | None = Header(None, alias="X-Qi-Confirm")
                              ) -> schemas.KbStatus:
        confirm_gate(confirm)
        service, reason = _kb()
        if service is None:
            return schemas.KbStatus(available=False, error=reason)
        await _in_kb(service.provider_add, cwd=web.default_cwd,
                     name=body.name, base_url=body.baseUrl,
                     api_prefix=body.apiPrefix, login=body.login or None,
                     role=body.role or None, instance=body.instance or None,
                     scope=body.scope)
        return await kb_status()

    @app.post("/api/kb/providers/login", response_model=schemas.KbStatus,
              dependencies=[Depends(guard)])
    async def kb_provider_login(body: schemas.KbProviderLoginRequest,
                                confirm: str | None = Header(None, alias="X-Qi-Confirm")
                                ) -> schemas.KbStatus:
        confirm_gate(confirm)
        service, reason = _kb()
        if service is None:
            return schemas.KbStatus(available=False, error=reason)
        await _in_kb(service.provider_login, cwd=web.default_cwd,
                     name=body.name, login=body.login or body.name,
                     password=body.password, base_url=body.baseUrl or None,
                     api_prefix=body.apiPrefix, scope=body.scope,
                     instance=body.instance or None)
        return await kb_status()

    @app.delete("/api/kb/providers", response_model=schemas.KbStatus,
                dependencies=[Depends(guard)])
    async def kb_provider_remove(body: schemas.KbProviderRemoveRequest,
                                 confirm: str | None = Header(None, alias="X-Qi-Confirm")
                                 ) -> schemas.KbStatus:
        confirm_gate(confirm)
        service, reason = _kb()
        if service is None:
            return schemas.KbStatus(available=False, error=reason)
        await _in_kb(service.provider_remove, cwd=web.default_cwd,
                     name=body.name, cascade=body.cascade)
        return await kb_status()

    @app.post("/api/kb/plan", response_model=schemas.KbPlan,
              dependencies=[Depends(guard)])
    async def kb_plan(body: schemas.KbPlanRequest) -> schemas.KbPlan:
        service, reason = _kb()
        if service is None:
            return schemas.KbPlan(available=False, error=reason)
        data = await _in_kb(service.plan_install, cwd=web.default_cwd,
                            kb=body.kbId, provider=body.provider,
                            scope=body.scope, rebind=body.rebind)
        return schemas.KbPlan(
            available=True, action=str(data.get("action") or ""),
            kbId=str(data.get("kbId") or body.kbId),
            provider=str(data.get("provider") or body.provider),
            existingProvider=data.get("existingProvider"),
            targetProvider=str(data.get("provider") or body.provider),
            blocked=bool(data.get("blocked")), warnings=[],
        )

    @app.get("/api/kb/available", response_model=schemas.KbList,
             dependencies=[Depends(guard)])
    async def kb_available(provider: str | None = Query(None),
                           q: str | None = Query(None),
                           boundOnly: bool = Query(False)) -> schemas.KbList:
        service, reason = _kb()
        if service is None:
            return schemas.KbList(available=False, error=reason)
        data = await _in_kb(service.available, cwd=web.default_cwd,
                            provider=provider or None, q=q or None,
                            bound_only=boundOnly)
        return schemas.KbList(**data)

    @app.get("/api/kb/docs", response_model=schemas.KbDocs,
             dependencies=[Depends(guard)])
    async def kb_docs(kbId: str = Query(...), provider: str | None = Query(None)
                      ) -> schemas.KbDocs:
        service, reason = _kb()
        if service is None:
            return schemas.KbDocs(available=False, error=reason)
        data = await _in_kb(service.documents, cwd=web.default_cwd,
                            kb=kbId, provider=provider or None)
        return schemas.KbDocs(**data)

    @app.post("/api/kb/search", response_model=schemas.KbSearchResult,
              dependencies=[Depends(guard)])
    async def kb_search(body: schemas.KbSearchRequest) -> schemas.KbSearchResult:
        service, reason = _kb()
        if service is None:
            return schemas.KbSearchResult(available=False, error=reason)
        data = await _in_kb(service.search, cwd=web.default_cwd, query=body.query,
                            kb_ids=body.kbIds or None, provider=body.provider or None,
                            top_k=body.topK, strategy=body.strategy,
                            use_rerank=body.useRerank, rerank_top_k=body.rerankTopK)
        return schemas.KbSearchResult(available=True, **data)

    @app.post("/api/kb/ask", response_model=schemas.KbAskResult,
              dependencies=[Depends(guard)])
    async def kb_ask(body: schemas.KbAskRequest) -> schemas.KbAskResult:
        service, reason = _kb()
        if service is None:
            return schemas.KbAskResult(available=False, error=reason)
        data = await _in_kb(service.ask, cwd=web.default_cwd, question=body.question,
                            kb_ids=body.kbIds or None, provider=body.provider or None,
                            top_k=body.topK, strategy=body.strategy,
                            use_rerank=body.useRerank, rerank_top_k=body.rerankTopK)
        return schemas.KbAskResult(available=True, **data)

    @app.post("/api/kb/ask/stream", dependencies=[Depends(guard)])
    async def kb_ask_stream(body: schemas.KbAskRequest) -> StreamingResponse:
        """流式问答（SSE）：逐 token 推，最后 sources / done。"""
        service, reason = _kb()
        if service is None:
            async def empty():
                yield ("data: " + json.dumps(
                    {"type": "error", "message": reason}, ensure_ascii=False) + "\n\n")
            return StreamingResponse(empty(), media_type="text/event-stream")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def worker() -> None:
            try:
                for event in service.ask_stream(
                    web.default_cwd, body.question,
                    kb_ids=body.kbIds or None, provider=body.provider or None,
                    top_k=body.topK, strategy=body.strategy,
                    use_rerank=body.useRerank, rerank_top_k=body.rerankTopK,
                ):
                    if stop.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:  # noqa: BLE001 错误也走 SSE，不弄断连接
                message = str(exc)
                if hasattr(exc, "hint") and getattr(exc, "hint"):
                    message += f"（{exc.hint}）"
                loop.call_soon_threadsafe(queue.put_nowait,
                                          {"type": "error", "message": message})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        async def gen():
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield ("data: " + json.dumps(item, ensure_ascii=False) + "\n\n")
            finally:
                stop.set()

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        })

    @app.post("/api/kb/install", response_model=schemas.KbInstallResult,
              dependencies=[Depends(guard)])
    async def kb_install(body: schemas.KbInstallRequest,
                         confirm: str | None = Header(None, alias="X-Qi-Confirm")
                         ) -> schemas.KbInstallResult:
        confirm_gate(confirm)
        service, reason = _kb()
        if service is None:
            return schemas.KbInstallResult(available=False, error=reason)
        data = await _in_kb(service.install, cwd=web.default_cwd, kb=body.kbId,
                            provider=body.provider, scope=body.scope,
                            rebind=body.rebind)
        entry = data.get("kb") or {}
        return schemas.KbInstallResult(
            available=True, action=str(data.get("action") or ""),
            kbId=str(data.get("kbId") or body.kbId),
            provider=str(data.get("provider") or body.provider),
            scope=str(data.get("scope") or body.scope),
            name=str(entry.get("name") or ""),
        )

    @app.post("/api/kb/uninstall", response_model=schemas.KbUninstallResult,
              dependencies=[Depends(guard)])
    async def kb_uninstall(body: schemas.KbUninstallRequest,
                           confirm: str | None = Header(None, alias="X-Qi-Confirm")
                           ) -> schemas.KbUninstallResult:
        confirm_gate(confirm)
        service, reason = _kb()
        if service is None:
            return schemas.KbUninstallResult(available=False, error=reason)
        data = await _in_kb(service.uninstall, cwd=web.default_cwd, kb=body.kbId)
        return schemas.KbUninstallResult(available=True, **data)

    @app.patch("/api/kb/defaults", response_model=schemas.KbStatus,
               dependencies=[Depends(guard)])
    async def kb_defaults(body: schemas.KbDefaultsRequest,
                          confirm: str | None = Header(None, alias="X-Qi-Confirm")
                          ) -> schemas.KbStatus:
        confirm_gate(confirm)
        service, reason = _kb()
        if service is None:
            return schemas.KbStatus(available=False, error=reason)
        await _in_kb(service.set_defaults, cwd=web.default_cwd,
                     values=body.defaults, scope=body.scope)
        return await kb_status()

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

    # ── 手写的页面(不经过 Vite 构建)────────────────────
    # `static/` 是构建产物,一次 `npm run build` 就会把它清掉(emptyOutDir);所以
    # 不需要构建的页面放在 `pages/`,由这里直接读文件下发。改完刷新即可生效。
    @app.get("/a2a", response_class=HTMLResponse, include_in_schema=False)
    async def a2a_page() -> str:
        """A2A 条目的独立页面(原生 JS,不依赖前端构建产物)。"""
        page = PAGES_DIR / "a2a.html"
        if not page.is_file():
            return (_PLACEHOLDER if not STATIC_DIR.is_dir() else
                    '<!doctype html><meta charset="utf-8"><title>404</title>'
                    '<p>页面文件不见了:qi_lab/pages/a2a.html</p>')
        return page.read_text(encoding="utf-8")

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
    raise SystemExit("请用 `qi lab` 启动(见 qi_agent.cli)")


async def _remote_turn(remote: Any, text: str, session: Any, runtime: Any,
                       translator: Any, run_id: str) -> Any:
    """**远端角色的一整轮**:把用户这句话转给远端,**边收边发**。

    与本地那条路的三处逐一对应(不这么做就会有"界面上说了、会话文件里没有"):

    1. **落 user 消息**:本地由 `runtime.stream` 在开跑前写 —— 这里得自己写,
       否则刷新后看不到自己说了什么;
    2. **落 assistant 消息**:同理,远端的回答也要进会话文件,不然回放是空的;
       两份都标 `agent_id=<远端名>`,会话文件里才看得出这轮是谁答的;
    3. **帧**:远端的 `artifactUpdate` 本来就是增量的,所以这里**边收边发**
       `TEXT_MESSAGE_CONTENT` —— 等整轮跑完再一次性贴上,长回答上就表现为
       "界面空着十几秒,然后突然出现一大段"。

    **桥接**:`run_remote` 是协程(它内部把整个 A2A 流跑完),而这里是 async generator
    (要边 yield 边等)。两者之间用一条 `asyncio.Queue`:回调往里塞,生成器从里取。
    队列里放**哨兵化的元组**(`("end", None)`)而不是裸值 —— 内容本身也可能是空串,
    用 `None` 表示结束迟早会和"空增量"撞上。

    失败**不抛**:转成 `RUN_ERROR` 帧后再结束(与本地流的异常处理同一条规矩)。
    审批中断(`input-required`)按**成功**处理 —— 远端把话带回来了,正文里写着
    "没执行"与回退方式。
    """
    from qi_a2a.remote import run_remote

    # 1) user 消息先落盘(与本地同序:先落 user,再跑)。
    session_store = getattr(runtime, "sessions", None)
    name = str(getattr(remote, "name", "") or "a2a")
    if session_store is not None:
        session_store.append(session, {"type": "message", "role": "user",
                                       "content": text, "agent_id": name})

    queue: asyncio.Queue = asyncio.Queue()
    streamed = False          # 远端吐过增量吗(没吐就用 `run.text` 兜底整段发)

    def on_text(delta: str, *, replace: bool = False) -> None:
        queue.put_nowait(("text", (delta, replace)))

    async def drive() -> None:
        try:
            run = await run_remote(remote, text, on_text=on_text)
        except Exception as exc:        # noqa: BLE001 桥的一侧只负责转交
            queue.put_nowait(("failed", f"{type(exc).__name__}: {exc}"))
        else:
            queue.put_nowait(("done", run))
        finally:
            queue.put_nowait(("end", None))

    task = asyncio.create_task(drive())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "end":
                break
            if kind == "text":
                delta, replace = payload
                if not delta:
                    continue
                if replace and streamed:
                    # 远端重来了一段(`append=false`):把已发的那条收尾,下一段开新的
                    for frame in translator.remote_reset():
                        yield agui.encode(frame)
                    streamed = False
                for frame in translator.remote_delta(delta):
                    yield agui.encode(frame)
                streamed = True
                continue
            if kind == "failed":
                for frame in translator.remote_reset():
                    yield agui.encode(frame)
                yield agui.encode(translator.run_error(str(payload), code="A2AError"))
                return
            if kind == "done":
                run = payload
                if not run.ok:
                    for frame in translator.remote_reset():
                        yield agui.encode(frame)
                    yield agui.encode(translator.run_error(
                        run.error or "远端调用失败", code="A2AError"))
                    return
                body = run.text or "(远端没有返回文本)"
                if not streamed:
                    # 非流式远端(或流里一个增量都没有):兜底整段发一次
                    for frame in translator.remote_message(body):
                        yield agui.encode(frame)
                else:
                    # 流式已经发过正文:这里只补收尾,不重复发内容
                    for frame in translator.remote_reset():
                        yield agui.encode(frame)
                # 2) assistant 消息落盘 —— 用**聚合后的全文**,不是拼接的 delta:
                #    两者在 `append=false` 时不一致,而会话文件该记最终那一份。
                if session_store is not None:
                    session_store.append(session, {"type": "message", "role": "assistant",
                                                   "content": body, "agent_id": name})
                # 3) 收尾帧
                yield agui.encode(translator.remote_finished(body))
                return
    finally:
        # 取消(客户端 abort / 掉线):把后台那个协程收掉 —— 否则它会继续读到
        # 进程结束,期间的输出没人接。
        if not task.done():
            task.cancel()


# ── 3c 的辅助:角色目录与 MCP 结构(模块级,纯读)────────────────

def _role_dirs(cwd) -> list:
    """各角色的目录(装了 qi-agents 才有)。读不到就当没有 —— 界面那头会显示 unavailable。"""
    try:
        from qi_agents.discovery import discover
    except ImportError:
        return []
    return [r.path.parent for r in discover(cwd, "both").values() if r.path]


def _role_mcp_file(role_dir):
    """角色目录里的 mcp.json → `(路径, {名: 声明})`;没有/读坏返回 None。"""
    try:
        from qi_mcp.config import MCP_FILE, read_file
    except ImportError:
        return None
    path = role_dir / MCP_FILE
    if not path.is_file():
        return None
    try:
        return path, read_file(path)
    except Exception:          # noqa: BLE001 一个角色写坏了不该让整页 500
        return None


def _role_mcp_count(agent_md) -> int:
    found = _role_mcp_file(agent_md.parent) if agent_md else None
    return len(found[1]) if found else 0


def _mcp_server_info(name, config, scope):
    """把一份 server 声明变成**只有结构**的视图(键名可以给,值一律不给)。"""
    from .schemas import McpServerInfo

    env = config.get("env") if isinstance(config.get("env"), dict) else {}
    headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}
    try:
        # 传输判定**只有一处**:走 qi-mcp 的 `ServerSpec`(它认 Agent Plugins 的 `type`,
        # 也在说“这一块归 qi-mcp 管”)。以前这里自己又抄了一份 `command`/`url`/`socket`
        # 三连推断 —— 那意味着 `type: streamable-http` 的声明在两个面里得到不同答案。
        from qi_mcp.config import ServerSpec, unknown_fields

        spec = ServerSpec(name=name, config=config)
        unknown = unknown_fields(spec)
        transport = spec.transport_label     # 展示用:写了 type 就照它显示(如 streamable-http)
        kind = spec.transport                # 判定用:stdio | http | socket | sse | unknown
    except ImportError:
        unknown = []
        kind = ("stdio" if config.get("command") else
                "http" if config.get("url") else
                "socket" if config.get("socket") else "unknown")
        transport = kind
    direct = config.get("directTools", False)
    # `disabled` 是布尔开关:**只认真正的 JSON true**(`"true"` / `1` 都不算)。
    # 等价于 `config.get("disabled") is True`,但用 isinstance 门写 —— 与下面 direct_tools
    # 同一惯用法,且避开“identity comparison with literal”的静态检查。
    disabled = config.get("disabled")
    return McpServerInfo(
        name=name, transport=transport,
        # `url` 只在 HTTP 家族(streamable-http / sse)才吐 —— stdio 的 `command`/`args`
        # 是最容易直接写进明文密钥的地方,一律不出。
        url=str(config.get("url", "")) if kind in ("http", "sse") else "",
        # socket 路径与 url 同档:结构、不是密钥(不给的话那种 server 改不了)
        socket=str(config.get("socket", "")) if kind == "socket" else "",
        env_keys=sorted(str(k) for k in env),
        header_keys=sorted(str(k) for k in headers),
        disabled=isinstance(disabled, bool) and disabled,
        direct_tools=direct if isinstance(direct, (bool, list)) else False,
        unknown_fields=unknown, source=scope)


# ── MCP 声明的写操作:路径 / 读 / 写 / 校验(模块级,纯函数)──────────


def _mcp_target(scope: str, role: str, cwd) -> Path:
    """这一处声明表落在哪个文件。

    **路径只有一处算法**:读端走 `qi_mcp.config.scope_files()`,写端也必须走它 ——
    读 A 写 B 是“改了却没生效”里最贵的一种。`scope=role` 那层不在 qi-mcp 里
    (它只有两层,第三层归 qi-agents),所以按角色目录名找。
    """
    try:
        from qi_mcp.config import MCP_FILE, scope_files
    except ImportError:
        raise HTTPException(status_code=400, detail="没装 qi-mcp 扩展,不能改写 MCP 声明")
    if scope in ("global", "project"):
        for layer, path in scope_files(cwd):
            if layer == scope:
                return path
        raise HTTPException(status_code=400, detail=f"不认识的 scope: {scope}")
    if not role.strip():
        raise HTTPException(status_code=400, detail="scope=role 必须给角色名 role")
    try:
        import qi_agents.discovery                      # noqa: F401  探可用性
    except ImportError:
        raise HTTPException(status_code=400, detail="没装 qi-agents 扩展,没有角色层可写")
    for directory in _role_dirs(cwd):
        if directory.name == role:
            return directory / MCP_FILE
    raise HTTPException(status_code=404, detail=f"没有这个角色: {role}")


def _check_mcp_name(name: str) -> str:
    """server 名的规矩:**不许带首尾空白**(一个手滑的空格会造出两个“同名” server,
    而界面上看起来一模一样),长度给个上限。"""
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="server 名不能为空")
    if name != name.strip():
        raise HTTPException(status_code=400, detail="server 名不能带首尾空白")
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="server 名太长(最多 200 字)")
    return name


def _read_mcp_servers(path: Path) -> dict:
    """读一份声明表 → `{名: 声明}`;文件不存在 = **空表**(没配 ≠ 配错了)。"""
    from qi_mcp.config import read_file
    if not path.is_file():
        return {}
    try:
        return dict(read_file(path))
    except Exception as exc:            # noqa: BLE001 坏文件要说清是哪个文件
        raise HTTPException(status_code=400, detail=f"{path}: 读不了({exc})")


def _write_mcp_servers(path: Path, servers: dict) -> None:
    """把 `servers` 写回 `path`。

    · **顶层其它键原样保留**(`$schema` 这类):文件是用户手写的,不能因为改一个
      server 就把别的键抹掉;
    · **原子写**(临时文件 + replace):留下半份 JSON,会让“我配了但没生效”变成
      没法诊断的事;
    · 新文件 **0600**:声明表里可能直接写着 `env` / `headers` 的明文密钥。
    """
    raw: dict = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"{path}: JSON 解析失败 {exc}")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"{path}: 顶层必须是对象")
    raw["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else 0o600
    os.chmod(tmp, mode)
    tmp.replace(path)


def _build_mcp_config(server) -> dict:
    """表单 → 一份声明。**写 `type`**(Agent Plugins 1.0 写法):读端优先认它,于是
    “界面上选的传输”与“文件里的词”永远是同一个,拿着文件核对也对得上。

    这里**不**检查载荷齐不齐 —— 统一交给 `_validate_mcp_config`,免得两处规则各说各话。
    """
    transport = server.transport.strip()
    if transport not in schemas.MCP_TRANSPORTS:
        raise HTTPException(
            status_code=400,
            detail=f"不认识的传输 `{server.transport}`(可写:{' / '.join(schemas.MCP_TRANSPORTS)})")
    config: dict = {"type": transport}
    if transport == "stdio":
        if server.command.strip():
            config["command"] = server.command
        if server.args:
            config["args"] = list(server.args)
        if server.cwd.strip():
            config["cwd"] = server.cwd
    elif transport in ("streamable-http", "sse", "http"):
        config["url"] = server.url.strip()
    else:                              # socket
        config["socket"] = server.socket.strip()
    if server.env:
        config["env"] = dict(server.env)
    if server.headers:
        config["headers"] = dict(server.headers)
    if server.disabled:
        config["disabled"] = True
    if isinstance(server.direct_tools, list):
        if server.direct_tools:
            config["directTools"] = list(server.direct_tools)
    elif server.direct_tools:
        config["directTools"] = True
    return config


def _validate_mcp_config(config: dict) -> None:
    """落盘前判一遍:**不完整的声明连不上,而它不会报错** —— 它只是永远不生效。"""
    from qi_mcp.config import ServerSpec

    spec = ServerSpec(name="", config=config)
    declared = spec.declared_type
    if declared is not None and declared not in schemas.MCP_TRANSPORTS:
        raise HTTPException(status_code=400,
                            detail=f"不支持的传输 `{declared}`"
                                   f"(可写:{' / '.join(schemas.MCP_TRANSPORTS)})")
    transport = spec.transport
    if transport == "unknown":
        raise HTTPException(status_code=400,
                            detail="声明不完整:stdio 要 command,streamable-http/sse 要 url,"
                                   "socket 要套接字路径")
    if transport == "stdio" and not str(config.get("command", "")).strip():
        raise HTTPException(status_code=400, detail="stdio 必须给 command")
    if transport in ("http", "sse") and not str(config.get("url", "")).strip():
        raise HTTPException(status_code=400, detail=f"{spec.transport_label} 必须给 url")
    if transport == "socket" and not str(config.get("socket", "")).strip():
        raise HTTPException(status_code=400, detail="socket 必须给套接字路径")


def _apply_mcp_patch(config: dict, patch) -> dict:
    """把 `patch` 里**出现过**的键改到 `config` 的副本上;没出现的键一个都不动。

    这正是“值看不见也改得动”的实现:读端不给值,前端就不送键,旧值原样留着。
    """
    from qi_mcp.config import TYPE_TRANSPORTS, ServerSpec

    out = dict(config)
    if "transport" in patch.model_fields_set and patch.transport is not None:
        transport = patch.transport.strip()
        if transport not in schemas.MCP_TRANSPORTS:
            raise HTTPException(
                status_code=400,
                detail=f"不认识的传输 `{patch.transport}`"
                       f"(可写:{' / '.join(schemas.MCP_TRANSPORTS)})")
        # 换传输 = 换载荷。按 **kind** 判而不是按字面:老声明没写 `type`,而
        # `http` 与 `streamable-http` 是同一种东西,不该因此把 url 清掉。
        if TYPE_TRANSPORTS.get(transport, transport) != ServerSpec(name="", config=out).transport:
            for key in ("command", "args", "cwd", "url", "socket"):
                out.pop(key, None)
        out["type"] = transport

    for field in ("url", "command", "cwd", "socket"):
        if field in patch.model_fields_set:
            value = getattr(patch, field)
            _set_or_drop(out, field, value.strip() if isinstance(value, str) else value)

    for field, key in (("args", "args"), ("env", "env"), ("headers", "headers")):
        if field in patch.model_fields_set and getattr(patch, field) is not None:
            value = getattr(patch, field)
            if value:
                out[key] = dict(value) if isinstance(value, dict) else list(value)
            else:
                out.pop(key, None)

    if "disabled" in patch.model_fields_set and patch.disabled is not None:
        # 空键比 `disabled: false` 更诚实(读端只认真 true,两者等价)。
        if patch.disabled:
            out["disabled"] = True
        else:
            out.pop("disabled", None)
    if "direct_tools" in patch.model_fields_set and patch.direct_tools is not None:
        value = patch.direct_tools
        if isinstance(value, list) and value:
            out["directTools"] = list(value)
        elif value is True:
            out["directTools"] = True
        else:
            out.pop("directTools", None)
    return out


def _set_or_drop(config: dict, key: str, value: str | None) -> None:
    """空字符串 = **删掉键**,不写空值:`{'url': ''}` 在界面上显示成“未声明”,
    却没人看得出那是“被改没了”。"""
    if value:
        config[key] = value
    else:
        config.pop(key, None)


# ── A2A 条目:路径 / 读 / 写 / 校验 / 探测(模块级,纯函数)──────────

#: 表单字段 → a2a.json 的键。**只有这一张表**:读端与写端都从它来,
#: 于是"界面上叫 base_url、文件里叫 baseUrl"这种错位没有第二处可发生。
A2A_FILE_KEYS = (
    ("card_url", "cardUrl"), ("base_url", "baseUrl"), ("rpc_url", "rpcUrl"),
    ("tenant", "tenant"), ("description", "description"),
    ("discovery", "discovery"), ("tool_prefix", "toolPrefix"),
)


def _a2a_target(scope: str, cwd) -> Path:
    """这一层 a2a.json 落在哪个文件。

    **路径只有一处算法**:读端走 `qi_a2a.config.scope_files()`,写端也必须走它 ——
    读 A 写 B 是"改了却没生效"里最贵的一种。
    """
    try:
        from qi_a2a.config import scope_files
    except ImportError:
        raise HTTPException(status_code=400, detail="没装 qi-a2a 扩展,不能改写 A2A 声明")
    for layer, path in scope_files(cwd):
        if layer == scope:
            return path
    raise HTTPException(status_code=400,
                        detail=f"不认识的 scope: {scope}(可写:{' / '.join(schemas.A2A_SCOPES)})")


def _check_a2a_name(name: str) -> str:
    """条目名的规矩与 MCP 那条完全一样:**不许带首尾空白**(一个手滑的空格会造出两个
    “同名”条目,而界面上看起来一模一样),长度给个上限。"""
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="条目名不能为空")
    if name != name.strip():
        raise HTTPException(status_code=400, detail="条目名不能带首尾空白")
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="条目名太长(最多 200 字)")
    return name


def _read_a2a_entries(path: Path) -> dict:
    """读一份 a2a.json → `{名: 声明}`;文件不存在 = **空表**(没配 ≠ 配错了)。"""
    from qi_a2a.config import read_file
    if not path.is_file():
        return {}
    try:
        return dict(read_file(path))
    except Exception as exc:            # noqa: BLE001 坏文件要说清是哪个文件
        raise HTTPException(status_code=400, detail=f"{path}: 读不了({exc})")


def _write_a2a_entries(path: Path, entries: dict) -> None:
    """把 `entries` 写回 `path`。

    · **顶层其它键原样保留**(`$schema` 这类):文件是用户手写的,不能因为改一个条目
      就把别的键抹掉;
    · **原子写**(临时文件 + replace):留下半份 JSON,会让"我配了但没生效"变成
      没法诊断的事;
    · 新文件 **0600**:`headers` 里可能直接写着明文 token。
    """
    from qi_a2a.config import SERVERS_KEY
    raw: dict = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"{path}: JSON 解析失败 {exc}")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"{path}: 顶层必须是对象")
    raw[SERVERS_KEY] = entries
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else 0o600
    os.chmod(tmp, mode)
    tmp.replace(path)


def _build_a2a_config(edit) -> dict:
    """表单 → 一份声明。**只写非空值**:默认值(600s / 流式 / 代理)由 qi-a2a 自己补,
    抄一份到这里只会让"改了默认值"变成两处不同步。"""
    config: dict = {}
    for field, key in A2A_FILE_KEYS:
        value = getattr(edit, field).strip()
        if value:
            config[key] = value
    if edit.headers:
        config["headers"] = dict(edit.headers)
    if edit.timeout_ms is not None:
        config["timeoutMs"] = edit.timeout_ms
    if edit.streaming is not None:
        config["streaming"] = edit.streaming
    _apply_a2a_direct(config, edit.direct_tools)
    if edit.disabled:
        config["disabled"] = True
    return config


def _apply_a2a_direct(config: dict, value) -> None:
    """`directTools` 的三种取值:`true` / 名字数组 / 关(删键)。"""
    if isinstance(value, list) and value:
        config["directTools"] = list(value)
    elif value is True:
        config["directTools"] = True
    else:
        config.pop("directTools", None)


def _apply_a2a_patch(config: dict, patch) -> dict:
    """把 `patch` 里**出现过**的键改到 `config` 的副本上;没出现的键一个都不动。

    这正是"值看不见也改得动"的实现:读端不给 `headers` 的值,前端就不送这个键,
    旧值原样留着。**送了 `null` 也读作不改**(`is not None` 那几道判断),否则界面
    一保存就把 token 抹了。
    """
    out = dict(config)
    for field, key in A2A_FILE_KEYS:
        if field in patch.model_fields_set:
            value = getattr(patch, field)
            if value is not None:
                _set_or_drop(out, key, value.strip())
    if "headers" in patch.model_fields_set and patch.headers is not None:
        if patch.headers:
            out["headers"] = dict(patch.headers)
        else:
            out.pop("headers", None)      # 空表 = 清掉整张表
    if "timeout_ms" in patch.model_fields_set and patch.timeout_ms is not None:
        out["timeoutMs"] = patch.timeout_ms
    if "streaming" in patch.model_fields_set and patch.streaming is not None:
        out["streaming"] = patch.streaming
    if "direct_tools" in patch.model_fields_set and patch.direct_tools is not None:
        _apply_a2a_direct(out, patch.direct_tools)
    if "disabled" in patch.model_fields_set and patch.disabled is not None:
        # 空键比 `disabled: false` 更诚实(读端只认真 true,两者等价)。
        if patch.disabled:
            out["disabled"] = True
        else:
            out.pop("disabled", None)
    return out


def _validate_a2a_config(name: str, config: dict, *, scope: str, cwd) -> None:
    """落盘前判一遍:**不完整的声明不会报错,它只是永远不生效** —— 那是最难查的一种。

    判的是**合并后的生效结果**,不是这一层自己那份:深合并下项目层只写 `{"tenant": …}`
    是**合法**的(端点由全局层给)。只验这一层会把这种写法误判成“缺端点”,
    而那正是这套配置最常用的形态。

    校验本身走 qi-a2a 的 `validate_entry`(结构与 URL 的规矩只有那一份);它会把 `${ENV}`
    展开后再查,所以"环境变量没设"会当场变成一个 400,而不是文件里的一行占位符。
    """
    from qi_a2a.config import ConfigError, validate_entry
    try:
        validate_entry(name, _a2a_effective_config(cwd, scope, name, config))
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _a2a_effective_config(cwd, scope: str, name: str, layer_config: dict) -> dict:
    """这一层落盘之后,**合并出来**的那份条目(校验用)。

    只叠**更低**的层:高层的同一条目不该影响“这一层写完结果长什么样”。
    只有两层,所以“更低”就是 global(写 global 时没有下层)。
    """
    from qi_a2a.config import load_layers, merge_config

    lower: dict = {}
    for layer, _path, entries in load_layers(cwd):
        if layer == scope:
            break
        if name in entries:
            lower = merge_config(lower, entries[name])
    return merge_config(lower, layer_config) if lower else layer_config


def _a2a_entry_info(name: str, config: dict, scope: str,
                    layers: list[str] | None = None) -> schemas.A2AEntryInfo:
    """把一份声明变成**只含结构**的视图(键名可以给,`headers` 的值一律不给)。

    默认值与直连工具名都走 `qi_a2a.config.A2AEntry` —— 界面显示的超时/流式/禁用
    必须和模型真正拿到的一致,自己再抄一遍默认值迟早会差一格。
    """
    from qi_a2a.config import A2AEntry, entry_tool_name, unknown_fields

    entry = A2AEntry(name=name, config=config, scope=scope)   # type: ignore[arg-type]
    return schemas.A2AEntryInfo(
        name=name,
        card_url=entry.card_url,
        base_url=entry.base_url,
        rpc_url=entry.rpc_url,
        tenant=entry.tenant,
        description=entry.description,
        header_keys=sorted(entry.headers),
        timeout_ms=entry.timeout_ms,
        streaming=entry.streaming,
        discovery=entry.discovery,
        direct_tools=entry.direct_tools,
        tool_name=entry_tool_name(entry) if entry.wants_direct() else "",
        disabled=entry.disabled,
        unknown_fields=unknown_fields(entry),
        source=scope,
        layers=list(layers or []),
    )


def _a2a_list(cwd) -> schemas.A2AList:
    """两层原样 + 深合并后的生效视图。

    读端**不展开 `${ENV}`**:展开等于把环境变量的值吐到 HTTP 响应里。所以这里走
    `load_layers()`(原样)与 `resolve_layers()`(只合并不展开),两者都不校验 ——
    一个写坏的条目不该让整个设置页 500,它的毛病会以 `unknown_fields` / 空端点显示出来。
    """
    try:
        from qi_a2a.config import ConfigError, load_layers, resolve_layers
    except ImportError:
        return schemas.A2AList(unavailable=True)

    try:
        sources: list[schemas.A2ASource] = []
        for scope, path, entries in load_layers(cwd):
            sources.append(schemas.A2ASource(
                scope=scope, path=str(path), exists=path.is_file(),
                entries=[_a2a_entry_info(name, config, scope)
                         for name, config in entries.items()]))

        resolved = [
            _a2a_entry_info(name, config, scope, layers)
            for name, (config, layers, scope) in sorted(resolve_layers(cwd).items())
        ]
    except ConfigError as exc:
        # 坏文件是**用户能修**的那种错:说清是哪个文件,别 500。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return schemas.A2AList(sources=sources, resolved=resolved)


async def _probe_a2a(cwd, server: str, agent: str) -> schemas.A2AProbeResult:
    """去真实端点拉一次名片。

    用 `resolve()`(展开 + 校验)而不是 `resolve_layers()`:探测要的就是"模型调用时
    会看到什么",那一路是展开过的。缺环境变量之类的问题在这里变成可读的 `error`。
    """
    try:
        from qi_a2a.card import CardError, cards_from_payload, discovery_url, select_card
        from qi_a2a.client import A2AClient, RpcError
        from qi_a2a.config import ConfigError, active
    except ImportError:
        return schemas.A2AProbeResult(server=server, error="没装 qi-a2a 扩展,无法探测")

    try:
        entries = active(cwd)
    except ConfigError as exc:
        return schemas.A2AProbeResult(server=server, error=f"a2a.json 有问题:{exc}")

    if not entries:
        return schemas.A2AProbeResult(
            server=server,
            error='没有任何 A2A 条目:先加一条(例如 {"baseUrl": "http://127.0.0.1:8080"})')
    if server:
        entry = entries.get(server)
        if entry is None:
            return schemas.A2AProbeResult(
                server=server,
                error=f"没有这个条目: {server};可选:{', '.join(sorted(entries))}")
    elif len(entries) == 1:
        entry = next(iter(entries.values()))
    else:
        return schemas.A2AProbeResult(
            server=server,
            error=f"配置了多个条目,请指定 server;可选:{', '.join(sorted(entries))}")

    result = schemas.A2AProbeResult(server=entry.name, source=entry.scope)
    url = discovery_url(entry)
    result.discovery_url = url
    started = time.monotonic()
    try:
        async with A2AClient(entry) as client:
            cards = await _probe_cards(client, entry, url)
        if agent:
            cards = [select_card(cards, agent)]
    except (CardError, RpcError) as exc:
        result.error = str(exc)
        return result
    finally:
        result.elapsed_ms = int((time.monotonic() - started) * 1000)

    result.ok = True
    result.cards = [
        schemas.A2AProbeCard(
            name=card.name, description=card.description, tenant=card.tenant,
            rpc_url=card.rpc_url,
            skills=[schemas.A2ASkill(**skill) for skill in card.skills])
        for card in cards
    ]
    return result


async def _probe_cards(client, entry, url):
    """一条路径:**有发现地址就走它,没有就用配置合成卡**。

    与 qi-a2a 工具的 `fetch_cards()` 同形(那边多一层"没有 url"的分支);这里显式
    重复一次是为了不在 qi-lab 里 import 工具层 —— 探测不该把整个工具面拖进来。
    """
    from qi_a2a.card import card_from_entry, cards_from_payload

    if not url:
        return [card_from_entry(entry)]
    return cards_from_payload(await client.get_json(url), entry)
