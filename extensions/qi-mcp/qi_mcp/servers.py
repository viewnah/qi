"""server 生命周期 + 工具元数据缓存(**lazy**:真用才连)。

与 pi-mcp-adapter 同款语义:

- **lazy**:`search` / `call` 之前不连任何 server(构造这个对象不等于启动进程);
- **连一次就复用**;连不上就记一条 note 并把那个 server 标为失败 —— **但下次还会再试**,
  因为临时的网络/权限问题不该让一次会话永久失去一个 server;
- 元数据缓存:列一次工具就记住。**与 pi 的差异**:pi 有持久缓存(跨会话),
   ours 是进程内 —— 所以第一次 `search` 会为拿元数据而连接全部 active server。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import ServerSpec


def qualified(server: str, tool: str) -> str:
    """E22 的命名:`mcp__<server>__<tool>`(代理与切片 3 的直连共用同一套名字)。"""
    return f"mcp__{server}__{tool}"


def split_qualified(name: str) -> tuple[str, str] | None:
    """`mcp__srv__tool` → `(srv, tool)`;不是这个形状回 `None`(与裸工具名区分开)。"""
    if not name.startswith("mcp__"):
        return None
    server, sep, tool = name[len("mcp__"):].partition("__")
    return (server, tool) if sep and server and tool else None


@dataclass(frozen=True)
class ToolInfo:
    """一个 server 的一个工具(以及它属于谁 —— 平铺进工具表时要靠 `server` 消歧)。"""

    server: str
    name: str                                   # server 自己声明的名字(不带前缀)
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        return qualified(self.server, self.name)

    def matches(self, query: str) -> bool:
        q = query.lower()
        return q in self.name.lower() or q in self.description.lower()


class ManagerError(Exception):
    """给模型看的可读错误(与 `ConfigError` 区分:那个是配置写坏了)。"""


class Client(Protocol):
    """一个已连接的 MCP server 的最小面。真实现走 SDK(`client.py`),测试注入假的。"""

    async def list_tools(self) -> list[tuple[str, str, dict]]:
        """→ `[(名字, 描述, inputSchema)]`。"""
        ...

    async def call_tool(self, name: str, args: dict) -> str:
        """调一个工具,回它给的文本。"""
        ...

    async def aclose(self) -> None: ...

    def alive(self) -> bool:
        """连接还活着吗?**同步**回答 —— 不 await。

        它是“要不要重连”的判据。没有它的话,server 中途崩掉后 `_clients` 里那个死
        client 会一直被返回,于是**整条会话**都得到一个永远失败的 server(症状是
        `连接已关闭`,而没有任何重试机会)。
        """
        ...


Connector = Callable[[ServerSpec], Awaitable[Client]]


class ServerManager:
    def __init__(self, specs: dict[str, ServerSpec], connector: Connector, *,
                 on_note: Callable[[str], None] | None = None) -> None:
        self._specs = dict(specs)
        self._connect = connector
        self._on_note = on_note
        self._clients: dict[str, Client] = {}
        self._failed: dict[str, str] = {}
        self._tools: dict[str, list[ToolInfo]] = {}
        self._notes: list[str] = []
        #: **每个 server 一把**连接锁(以前是一把全局锁,而且跨 `await connect` 持有)。
        self._locks: dict[str, asyncio.Lock] = {}

    def set_on_note(self, on_note: Callable[[str], None] | None) -> None:
        """换掉诊断出口 —— **后补也要生效**。

        manager 是“谁先要谁建”的:代理工具那条路没有 ui,可能先把 manager 建出来;
        此后 `session_start` / `/mcp tools` 再带 `notify=True` 想接上诊断就已经晚了。
        不补的话,连接失败、列工具失败这些一律**静默丢掉** —— 而那正是用户最需要看到的东西。
        """
        self._on_note = on_note

    # ── 诊断面 ──
    @property
    def notes(self) -> list[str]:
        return list(self._notes)

    @property
    def failed(self) -> dict[str, str]:
        return dict(self._failed)

    @property
    def specs(self) -> dict[str, ServerSpec]:
        return dict(self._specs)

    def connected(self) -> list[str]:
        return sorted(self._clients)

    def _note(self, text: str) -> None:
        self._notes.append(text)
        if self._on_note is not None:
            self._on_note(text)

    # ── 生命周期 ──
    def _usable(self, name: str) -> bool:
        """缓存里有一个**还活着**的连接吗?"""
        client = self._clients.get(name)
        return client is not None and client.alive()

    def _conn_lock(self, name: str) -> asyncio.Lock:
        """取某个 server 的连接锁(`setdefault` 原子 —— 中间没有 await)。

        锁只保护“**谁能建这条连接**”,不保护网络本身:同一 server 不重复连(第二个
        等锁的人拿到的是同一份),不同 server 互不阻塞。
        """
        lock = self._locks.get(name)
        if lock is None:
            lock = self._locks[name] = asyncio.Lock()
        return lock

    async def client(self, name: str) -> Client | None:
        """拿到(必要时建立或**重连**)某个 server 的连接。`None` = 不可用。

        两处以前的毛病在这里收掉:
        * 一把全局锁跨 `await self._connect()` 持有 → 一个慢 server 挡住其余全部,
          最坏等满它的连接超时(60s)。现在锁是**每 server 一把**。
        * 只看 `name in self._clients`，**不查存活** → server 中途崩掉后那条会话
          永久拿到一个死连接。现在先 `_usable()`,死的丢掉重连。
        """
        spec = self._specs.get(name)
        if spec is None or spec.disabled:
            return None
        if self._usable(name):
            return self._clients[name]
        async with self._conn_lock(name):
            if self._usable(name):            # 等锁期间别人可能已经连上了
                return self._clients[name]
            if name in self._clients:
                self._note(f"MCP server `{name}` 的连接已断,重连")
            self._clients.pop(name, None)
            self._tools.pop(name, None)       # 重连后工具集可能变了 → 下次重新列
            try:
                client = await self._connect(spec)
            except Exception as exc:                      # 一个 server 起不来不该毁整体
                self._failed[name] = f"{type(exc).__name__}: {exc}"
                self._note(f"MCP server `{name}` 连接失败:{self._failed[name]}")
                return None
            self._clients[name] = client
            self._failed.pop(name, None)
            return client

    async def _ensure_tools(self, name: str, *, refresh: bool = False) -> None:
        """确保**这一个** server 的工具元数据在缓存里。失败只记 note(不抛)。"""
        client = await self.client(name)
        if client is None or (not refresh and name in self._tools):
            return
        try:
            listed = await client.list_tools()
        except Exception as exc:
            self._note(f"MCP server `{name}` 列工具失败:{type(exc).__name__}: {exc}")
            return
        # `includeTools` / `excludeTools` 在这里生效 —— **对代理也一样**:
        # 它决定的是这个 server 的**可见工具集**。否则过滤就是装饰:代理照样搜得到、
        # 调得到被过滤掉的工具。
        # 延迟导入:direct 要用本模块的 ToolInfo/qualified,顶层 import 会成环。
        from .direct import select_tools

        self._tools[name] = select_tools(
            [ToolInfo(server=name, name=str(t_name),
                      description=str(t_desc or ""), schema=t_schema or {})
             for t_name, t_desc, t_schema in listed],
            self._specs[name])

    async def tools(self, *, refresh: bool = False) -> list[ToolInfo]:
        """全部 active server 的工具。

        **并发**连 + 列:串行的话 N 个 server 的启动时间会相加,而且一个连不上的会把
        后面全部拖住(它的连接超时）。顺序按声明表固定 —— 输出稳定比“谁先返回”重要。
        """
        names = [n for n, spec in self._specs.items() if not spec.disabled]
        await asyncio.gather(*(self._ensure_tools(n, refresh=refresh) for n in names))
        return [info for n in names for info in self._tools.get(n, [])]

    async def tools_of(self, name: str) -> list[ToolInfo]:
        """单个 server 的可见工具(过滤后)。

        **只连那一个**:直连注册是一个 server 一个 server 问的,以前这里走 `tools()`
        会把**全部** server 都拉起来(给 3 个 server 开 `directTools` = 启动时连全部)。
        """
        await self._ensure_tools(name)
        return list(self._tools.get(name, []))

    async def call(self, server: str, tool: str, args: dict) -> str:
        """调一个工具。不可用时抛 `ManagerError`(可读 + 带上失败原因)。"""
        client = await self.client(server)
        if client is None:
            why = self._failed.get(server, "server 不在声明表里或已被 disabled")
            raise ManagerError(f"MCP server `{server}` 不可用:{why}")
        try:
            return await client.call_tool(tool, args)
        except Exception as exc:
            raise ManagerError(f"`{server}` 的 `{tool}` 调用失败:{type(exc).__name__}: {exc}") from exc

    async def aclose(self) -> None:
        """关掉**全部**连接。先取出再清表 —— 清完就没人能再拿到它们(与 `role.py` 同一写法)。"""
        clients = list(self._clients.items())
        self._clients.clear()
        for name, client in clients:
            try:
                await client.aclose()
            except Exception as exc:                       # 关闭失败只记,不抛(已在收尾)
                self._note(f"MCP server `{name}` 关闭失败:{type(exc).__name__}: {exc}")
