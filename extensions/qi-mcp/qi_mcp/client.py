"""真的 MCP 客户端:stdio 与 Streamable HTTP 两种传输(`mcp` 2.x SDK)。

## 这个文件里唯一难的地方:会话必须归**一个长驻 task** 所有

SDK 的两套传输都是 **task 亲和**的(anyio cancel scope 的进出必须同一 task):

    Attempted to exit cancel scope in a different task than it was entered in

也就是说,不能用"调用时进入 `async with`、下一个调用再进一次" —— 而我们的 `ServerManager`
恰好是"连一次,之后多次调用"。所以给**每个 server 一个长驻 asyncio task**,所有 MCP 调用
排进队列、在**那个 task 里**执行。传输差异被收进 `opener`(一个返回 async ctx manager 的
可调用),`SessionClient` 本身对两种传输一视同仁。

(替代方案是每次调用重连:省掉 task,但每次调用都要重付一次进程启动/握手 —— stdio 起
`npx` 可能好几秒,HTTP 多一次 TLS 与 initialize,那样 lazy 复用就没意义了,pi 也保留空闲连接。)

## 传输口径

- **stdio**:`env` 默认**全量继承宿主环境**(对齐 pi 的 `inheritEnv: true`),再叠加声明里的 `env`;
- **http**:`headers` 与 `bearerToken` 支持 `${VAR}` 展开(**缺失即报错,在发请求之前**),
  `auth: "bearer"` + `bearerToken` / `bearerTokenEnv` 给 `Authorization` 头。
  **不做 OAuth/凭据存储**(已明确留到后续版本),所以这里不会假装支持 `auth: "oauth"`;
- 超时:`requestTimeoutMs` → 单次调用 `read_timeout_seconds`(值写坏就回缺省,不抛)。

结果渲染:文本片段拼起来;`is_error` 明确标出来(模型要能看出"工具自己报错了")。
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from .config import ServerSpec
from .servers import Client, ManagerError

DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 60.0

#: `Opener`:返回一个 async context manager,yield `(read, write)` 两条流。
Opener = Callable[[], Any]


# ── 字段/结果兼容(mcp 1.x 驼形 ↔ 2.x 蛇形)──────────────

def _field(obj: Any, *names: str) -> Any:
    """取第一个存在的字段。**两套拼写都试** —— `mcp` 2.x 把模型字段改成了蛇形
    (`is_error` / `input_schema`),1.x 是驼形(`isError` / `inputSchema`)。
    写死一种会让另一半静默拿不到值(比如"工具报错"根本没标出来),所以这里宽容,
    而**具体哪种生效由端到端测试盯着**(v2 = 蛇形,已验)。
    """
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _render_call_result(result: Any) -> str:
    """把 `CallToolResult` 变成文本。非文本内容(图片等)只标类型 —— 不假装能渲染它。"""
    parts: list[str] = []
    for item in _field(result, "content") or []:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else f"[{type(item).__name__}]")
    text = "\n".join(parts) if parts else "(空结果)"
    return f"[工具报告错误] {text}" if _field(result, "is_error", "isError") else text


# ── 开场:两种传输各自的 opener ───────────────────────────

def _params(spec: ServerSpec) -> Any:
    from mcp import StdioServerParameters

    cfg = spec.config
    env: dict[str, str] | None = None
    extra = cfg.get("env")
    if cfg.get("inheritEnv", True):
        env = {**os.environ, **{str(k): str(v) for k, v in (extra or {}).items()}}
    elif isinstance(extra, dict):
        env = {str(k): str(v) for k, v in extra.items()}
    return StdioServerParameters(
        command=str(cfg.get("command", "")),
        args=[str(a) for a in (cfg.get("args") or [])],
        env=env,
        cwd=str(cfg["cwd"]) if cfg.get("cwd") else None,
    )


def _stdio_opener(spec: ServerSpec) -> Opener:
    def open_() -> Any:
        from mcp.client.stdio import stdio_client

        return stdio_client(_params(spec))

    return open_


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: str, *, what: str, server: str) -> str:
    """展开 `${VAR}`。**缺失直接报错** —— 带着字面 `${TOKEN}` 去发请求,现场只会看到
    一个 401,而不知道是环境变量没设(pi 也是这个口径:缺变量在发请求前失败)。"""

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ManagerError(
                f"MCP server `{server}` 的 {what} 引用了环境变量 {name},但它没有设置")
        return os.environ[name]

    return _ENV_REF.sub(sub, value)


def _headers(spec: ServerSpec) -> dict[str, str]:
    """组装 HTTP 头:`headers` + `auth: bearer` 的 `Authorization`。"""
    raw = spec.config.get("headers")
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        out.update({str(k): _expand(str(v), what="headers", server=spec.name)
                    for k, v in raw.items()})
    if spec.config.get("auth") == "bearer":
        token = spec.config.get("bearerToken")
        env_name = spec.config.get("bearerTokenEnv")
        if not token and env_name:
            if str(env_name) not in os.environ:
                raise ManagerError(
                    f"MCP server `{spec.name}` 的 bearerTokenEnv={env_name} 指向的环境变量没有设置")
            token = os.environ[str(env_name)]
        if token:
            out.setdefault("Authorization",
                           f"Bearer {_expand(str(token), what='bearerToken', server=spec.name)}")
    return out


def _http_client_factory() -> Any:
    """拿到“建 http client”的那个工厂(它才能收 headers)。

    为什么要运行时查:`mcp` 2.x 把它从公开的 `mcp.client.streamable_http` **不再导出**
    (还在 `dir()` 里,但类型检查判 “not exported”),真身份在**私有**模块
    `mcp.shared._httpx_utils`。而 `streamable_http_client(url, *, http_client=…)`
    又不收 headers —— 要发自定义头就只能拿到这个工厂。

    所以先试公开名(将来可能重新导出),再退私有名;两者都没有就给一句能读懂的话。
    静态 `from … import …` 做不到这种“先公开后私有”的降级,而且会在公开名上被类型检查拦下。
    """
    import importlib

    for module_name in ("mcp.client.streamable_http", "mcp.shared._httpx_utils"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:                    # 换版本时模块可能搬家
            continue
        factory = getattr(module, "create_mcp_http_client", None)
        if factory is not None:
            return factory
    raise ManagerError("这个 mcp 版本没有可用的 http client 工厂(create_mcp_http_client)")


def _http_opener(spec: ServerSpec) -> Opener:
    url = str(spec.config.get("url", ""))
    if not url:
        raise ManagerError(f"MCP server `{spec.name}`:声明里没有 url")

    @asynccontextmanager
    async def open_() -> AsyncIterator[Any]:
        # v2 的 headers 不是 streamable_http_client 的参数,而是“先建 http client”。
        # http client 由我们持有 → 也要由我们关掉(否则每次重连都漏一个连接池)。
        from mcp.client.streamable_http import streamable_http_client

        factory = _http_client_factory()
        http_client = factory(headers=_headers(spec) or None)
        try:
            async with streamable_http_client(url, http_client=http_client) as streams:
                yield streams
        finally:
            await http_client.aclose()

    return open_


# ── 会话客户端(两种传输共用)─────────────────────────────

class SessionClient:
    """一个 server 的连接:**长驻 task 持有会话**,调用经队列进去执行。"""

    def __init__(self, spec: ServerSpec, opener: Opener, *,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> None:
        self.spec = spec
        self._open = opener
        self._connect_timeout = connect_timeout
        self._ops: asyncio.Queue[tuple[Callable[..., Any], tuple, asyncio.Future] | None] = (
            asyncio.Queue())
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._error: BaseException | None = None
        self._closed = False

    def _timeout(self) -> float:
        """读声明里的 `requestTimeoutMs`(pi 字段);缺省 30s。

        值可能是任何东西(手写 JSON)→ **解析失败就用缺省,不抛**:一个写错的超时值
        不该让整个 server 不可用。
        """
        raw = self.spec.config.get("requestTimeoutMs")
        if not isinstance(raw, (int, float, str)):      # 显式收窄:bool 是 int 的子类,也认
            return DEFAULT_READ_TIMEOUT_SECONDS
        try:
            seconds = float(raw) / 1000.0
        except (TypeError, ValueError):
            return DEFAULT_READ_TIMEOUT_SECONDS
        return seconds if seconds > 0 else DEFAULT_READ_TIMEOUT_SECONDS

    # ── 启动:失败必须在**第一次调用**时就暴露(由 manager 记成 note)──
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._ready.clear()
        self._error = None
        self._task = asyncio.create_task(self._serve(), name=f"mcp:{self.spec.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._connect_timeout)
        except asyncio.TimeoutError:
            await self.aclose()
            raise ManagerError(
                f"MCP server `{self.spec.name}` 连接超时({self._connect_timeout:g}s)") from None
        if self._error is not None:
            raise self._error

    async def _serve(self) -> None:
        from mcp import ClientSession

        try:
            async with self._open() as streams:
                read, write = streams[0], streams[1]     # 有的是 (read, write[, get_id])
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # **初始化完成就要放行 `start()`** —— 这条曾经只在 finally 里(那要等到
                    # 会话结束才执行),于是正常启动路径上永远不 set,调用方白等到连接超时。
                    self._ready.set()
                    await self._loop(session)
        except BaseException as exc:           # 含 CancelledError:都要让等待方醒来
            self._error = exc
            raise
        finally:
            self._ready.set()                  # 启动失败/提前退出时也要放行 start()
            self._fail_pending(self._error or ManagerError("连接已结束"))

    async def _loop(self, session: Any) -> None:
        while True:
            item = await self._ops.get()
            if item is None:                   # 关闭信号 → 退出 with 块(顺带收掉子进程/连接)
                return
            fn, args, fut = item
            if fut.cancelled():
                continue
            try:
                fut.set_result(await fn(session, *args))
            except BaseException as exc:       # noqa: BLE001 失败要如实传回调用方
                fut.set_exception(exc)

    def _fail_pending(self, exc: BaseException) -> None:
        """会话结束时,把还排着队的请求**立刻**失败掉 —— 别让调用方等到超时。"""
        while not self._ops.empty():
            item = self._ops.get_nowait()
            if item is not None and not item[2].done():
                item[2].set_exception(exc)

    async def _submit(self, fn: Callable[..., Any], *args: Any) -> Any:
        if self._closed:
            raise ManagerError(f"MCP server `{self.spec.name}` 的连接已关闭")
        await self.start()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._ops.put((fn, args, fut))
        return await fut

    # ── `Client` 协议 ──
    async def list_tools(self) -> list[tuple[str, str, dict]]:
        async def _list(session: Any) -> list[tuple[str, str, dict]]:
            result = await session.list_tools()
            return [(str(_field(t, "name") or ""),
                     str(_field(t, "description") or ""),
                     _field(t, "input_schema", "inputSchema") or {})
                    for t in _field(result, "tools") or []]

        return await self._submit(_list)

    async def call_tool(self, name: str, args: dict) -> str:
        async def _call(session: Any) -> str:
            result = await session.call_tool(name, arguments=args,
                                             read_timeout_seconds=self._timeout())
            return _render_call_result(result)

        return await self._submit(_call)

    async def aclose(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is None or task.done():
            return
        await self._ops.put(None)              # 让 _loop 返回 → with 块正常退出 → 收资源
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):   # noqa: B014
            task.cancel()


# ── 连接器 ──────────────────────────────────────────────

async def connect_stdio(spec: ServerSpec) -> Client:
    """stdio 传输。连不上就抛 —— 由 `ServerManager` 记成 note。"""
    client = SessionClient(spec, _stdio_opener(spec))
    await client.start()
    return client


async def connect_http(spec: ServerSpec) -> Client:
    """Streamable HTTP 传输(E21 的另一半)。"""
    opener = _http_opener(spec)                # 声明不完整在这里就报错(url 缺/变量缺)
    client = SessionClient(spec, opener)
    await client.start()
    return client


async def connect(spec: ServerSpec) -> Client:
    """按声明里的传输选连接器。socket(`rmcp-mux`)不做 —— pi 有,我们用不到。"""
    if spec.transport == "stdio":
        return await connect_stdio(spec)
    if spec.transport == "http":
        return await connect_http(spec)
    raise ManagerError(
        f"MCP server `{spec.name}`:传输 `{spec.transport}` 尚未实现(目前支持 stdio 与 http)")
