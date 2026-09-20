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
        self._lock = asyncio.Lock()

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
    async def client(self, name: str) -> Client | None:
        """拿到(必要时建立)某个 server 的连接。`None` = 不可用(没有/关了/连不上)。"""
        async with self._lock:
            if name in self._clients:
                return self._clients[name]
            spec = self._specs.get(name)
            if spec is None or spec.disabled:
                return None
            try:
                client = await self._connect(spec)
            except Exception as exc:                      # 一个 server 起不来不该毁整体
                self._failed[name] = f"{type(exc).__name__}: {exc}"
                self._note(f"MCP server `{name}` 连接失败:{self._failed[name]}")
                return None
            self._clients[name] = client
            self._failed.pop(name, None)
            return client

    async def tools(self, *, refresh: bool = False) -> list[ToolInfo]:
        """全部 active server 的工具(逐个 lazy 连接;列过的记住)。"""
        out: list[ToolInfo] = []
        for name, spec in self._specs.items():
            if spec.disabled:
                continue
            client = await self.client(name)
            if client is None:
                continue
            if refresh or name not in self._tools:
                try:
                    listed = await client.list_tools()
                except Exception as exc:
                    self._note(f"MCP server `{name}` 列工具失败:{type(exc).__name__}: {exc}")
                    continue
                self._tools[name] = [
                    ToolInfo(server=name, name=str(t_name),
                             description=str(t_desc or ""), schema=t_schema or {})
                    for t_name, t_desc, t_schema in listed]
            out.extend(self._tools[name])
        return out

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
        for name, client in list(self._clients.items()):
            try:
                await client.aclose()
            except Exception as exc:                       # 关闭失败只记,不抛(已在收尾)
                self._note(f"MCP server `{name}` 关闭失败:{type(exc).__name__}: {exc}")
        self._clients.clear()
