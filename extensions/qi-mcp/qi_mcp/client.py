"""真的 MCP 客户端:stdio 传输(`mcp` SDK)。

## 这个文件里唯一难的地方:会话必须归**一个长驻 task** 所有

`mcp` SDK 的传输(以及它内部的 anyio task group)是 **task 亲和**的:

    Attempted to exit cancel scope in a different task than it was entered in

也就是说,不能用"调用时进入 `async with`、下一个调用再进一次"的写法 —— 而我们的
`ServerManager` 恰好是"连一次,之后多次调用"。所以这里给**每个 server 一个长驻 asyncio task**,
所有 MCP 调用都排进队列、在**那个 task 里**执行。这是唯一能让"连接复用"与 SDK 的
task 亲和要求同时成立的形状。

(替代方案是**每次调用重连**:省掉 task,但每次工具调用都要重付一次进程启动 ——
`npx` 起一个 server 可能要好几秒,那样 lazy 复用就没意义了,pi 也明确保留空闲连接。)

## 其他口径

- `env`:**默认全量继承宿主环境**(对齐 pi 的 `inheritEnv: true`),再叠加声明里的 `env`;
- 超时:读声明里的 `requestTimeoutMs`(pi 字段),缺省 30s;
- 结果渲染:文本片段拼起来;`isError` 明确标出来(模型要能看出"工具自己报错了")。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from .config import ServerSpec
from .servers import Client, ManagerError

DEFAULT_READ_TIMEOUT_SECONDS = 30.0


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


def _field(obj: Any, *names: str) -> Any:
    """取第一个存在的字段。**两套拼写都试** —— `mcp` 2.x 把模型字段改成了蛇形
    (`is_error` / `input_schema`),1.x 是驼形(`isError` / `inputSchema`)。
    写死一种会让另一半静默拿不到值(比如“工具报错”根本没标出来),所以这里宽容,
    而**具体哪种生效由端到端测试盯着**。
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


class StdioClient:
    """一个 stdio server 的连接:**长驻 task 持有会话**,调用经队列进去执行。"""

    def __init__(self, spec: ServerSpec, *, connect_timeout: float = 60.0) -> None:
        self.spec = spec
        self._connect_timeout = connect_timeout
        self._ops: asyncio.Queue[tuple[Callable[..., Any], tuple, asyncio.Future] | None] = (
            asyncio.Queue())
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._error: BaseException | None = None
        self._closed = False

    def _timeout(self) -> float:
        """读声明里的 `requestTimeoutMs`(pi 字段);缺省 30s。

        值可能是任何东西(手写 JSON)→ **解析失败就用缺省**,不抛:一个写错的超时值
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
        self._task = asyncio.create_task(self._serve(), name=f"mcp-stdio:{self.spec.name}")
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
        from mcp.client.stdio import stdio_client

        try:
            async with stdio_client(_params(self.spec)) as (read, write):
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
            if item is None:                   # 关闭信号 → 退出 with 块(顺带收掉子进程)
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
            return [(str(_field(t, "name", "name") or ""),
                     str(_field(t, "description", "description") or ""),
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
        await self._ops.put(None)              # 让 _loop 返回 → with 块正常退出 → 收子进程
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):   # noqa: B014
            task.cancel()


async def connect_stdio(spec: ServerSpec) -> Client:
    """`Connector`:建一个 stdio 连接。连不上就抛 —— 由 `ServerManager` 记成 note。"""
    client = StdioClient(spec)
    await client.start()
    return client


async def connect(spec: ServerSpec) -> Client:
    """按声明里的传输选连接器。**只实现了 stdio** —— 其余给可读错误而不是假装。"""
    if spec.transport == "stdio":
        return await connect_stdio(spec)
    raise ManagerError(
        f"MCP server `{spec.name}`:传输 `{spec.transport}` 尚未实现"
        f"(目前只支持 stdio;Streamable HTTP 是切片 2c)")
