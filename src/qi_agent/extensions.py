"""扩展宿主:事件总线 + 扩展上下文 + 传给 `register(api)` 的接口。

对应 pi 的 `ExtensionAPI` / `ExtensionContext` / 事件订阅(docs/extensions.md §3、§4)。

**公开面白名单**(§5.5 —— pi 的 `## Available Imports` 对应物)。当前承诺:

    ExtensionBus / ExtensionApi / ExtensionContext / EmitResult    ← 本模块
    Tool / ToolError / ToolOutcome                                ← qi_agent.registry / qi_agent.models

`Tool` 暂时仍住在 `registry.py`(本模块被 registry 反向导入,不能成环),P-E6 收口时搬过来。

## 三条硬规则(§4,由本模块**实现**而不只是文档)

1. **顺序 = 装载顺序**,且是**快照遍历** —— handler 里再 `on()` 不改变本次派发。
   否则"注册一个自己"会让派发越跑越多(中间件风格的自注册很常见)。
2. **patch 链** —— handler 返回 `dict` 就浅合并进 payload,后面的 handler 看到前面改过的值;
   payload 是**同一个 dict**,所以原地改字段同样对后续可见(对齐 pi 的 `event.input` 可原地改)。
3. **失败隔离** —— 单个 handler 抛异常:记进 `EmitResult.errors` 并**继续链**,不中断会话。
   安全类事件(`tool_call` 这类"拦截器")可以给 `on_error_result` 做 fail-safe:
   **闸门自己崩了就拦住,而不是放行**(放行等于"装了权限闸门反而更不安全")。

不认识的返回值一律忽略(不报错):扩展可能比宿主新,而"插件发了东西但没人看见"
比"多一个无害的返回值"难诊断得多。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .abort import AbortSignal
from .models import ToolOutcome

#: `handler(payload, ctx) -> dict | None | Awaitable[...]`
#: `payload` 是本次派发的那一个 dict(原地改 = 对后续 handler 可见)。
ExtensionHandler = Callable[[dict, "ExtensionContext"], Any | Awaitable[Any]]


def _handler_name(handler: Any) -> str:
    """handler 的可读名字(诊断用):优先模块.qualname。"""
    module = getattr(handler, "__module__", "") or ""
    qual = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", "") or "?"
    return f"{module}.{qual}" if module else str(qual)


def _is_verdict(returned: dict, stop_keys: tuple[str, ...] | None,
                stop_values: dict[str, tuple[str, ...]] | None) -> bool:
    """这次返回算不算“裁决”(要停链)?两种形状的差别见 `emit_until`。"""
    if stop_keys and any(returned.get(k) for k in stop_keys):
        return True
    for key, allowed in (stop_values or {}).items():
        if returned.get(key) in allowed:
            return True
    return False


@dataclass(frozen=True)
class ExtensionContext:
    """`ctx` —— 传给每个 handler 的只读上下文(P-E1d 是最小集,见 §3.3)。

    P-E3/P-E4 会往上加 `ui` / `sessionManager` / `compact` / `model` 对象等;
    现在先只放"任何 handler 都可能要用"的那几个。

    `notes` 是**故意可变**的:扩展也能往启动提示里加话(它是宿主给前端的唯一提示通道,
    runtime 自己不打印)。frozen 只管字段重绑定,不管列表内容。
    """

    cwd: Path
    model: str | None = None            # "provider/model"(与 pi 的 ctx.model 同形)
    thinking_level: str = "off"
    signal: AbortSignal | None = None   # 协作式中断:扩展做异步**必须**传它,否则 Esc 取消不掉
    has_ui: bool = False
    project_trusted: bool = True
    notes: list[str] = field(default_factory=list)

    def is_project_trusted(self) -> bool:
        """pi 是 `ctx.isProjectTrusted()`;保持方法形状,方便 qi-agents 直接照搬。"""
        return self.project_trusted


@dataclass
class EmitResult:
    """一次派发的结果。

    `payload` 是链结束后那一个 dict(patch 链的最终值);
    `returns` 保留每个 handler 的原始返回值(诊断);
    `errors` 是 `(来源, 异常)` —— 有内容不代表派发失败,只代表那些 handler 没生效。
    """

    payload: dict
    returns: list[Any] = field(default_factory=list)
    errors: list[tuple[str, Exception]] = field(default_factory=list)
    result: dict | None = None          # `emit_until` 命中裁决时,那个 handler 的返回
    stopped_by: str | None = None       # 谁终止了链(None = 全跑完)

    @property
    def ok(self) -> bool:
        return not self.errors


class ExtensionBus:
    """扩展事件总线:按装载顺序链式派发(§4)。"""
    def __init__(self) -> None:
        # event -> [(来源, handler)]。来源 = 扩展名(诊断时能指到具体哪个扩展)
        self._handlers: dict[str, list[tuple[str, ExtensionHandler]]] = {}

    # ── 注册 ──
    def on(self, event: str, handler: ExtensionHandler, *, source: str = "") -> None:
        """订阅事件。`source` 缺省用 handler 的模块名(库内调用方应显式给扩展名)。"""
        self._handlers.setdefault(event, []).append((source or _handler_name(handler), handler))

    @property
    def events(self) -> list[str]:
        """已有人订阅的事件名(升序)。"""
        return sorted(self._handlers)

    def handler_count(self, event: str) -> int:
        return len(self._handlers.get(event, ()))

    @property
    def is_empty(self) -> bool:
        """一个 handler 都没有 —— 宿主可以据此跳过整条派发路径(零扩展时的常见情形)。"""
        return not self._handlers

    # ── 派发 ──
    async def emit(self, event: str, payload: dict | None = None, *,
                   ctx: ExtensionContext) -> EmitResult:
        """通知 / patch 链:所有 handler 都跑,返回值里的 dict 被并进 payload。"""
        return await self._dispatch(event, payload, ctx, stop_keys=None,
                                    stop_values=None, on_error_result=None)

    async def emit_until(self, event: str, payload: dict | None = None, *,
                         ctx: ExtensionContext,
                         stop_keys: tuple[str, ...] = (),
                         stop_values: dict[str, tuple[str, ...]] | None = None,
                         on_error_result: dict | None = None) -> EmitResult:
        """裁决型派发:**第一个**命中裁决的 handler 胜,链立即停(§3.1)。

        裁决有**两种形状**,都是 pi 实际在用的,所以两种都要支持 ——
        差别是真实的,不是风格:

        * `stop_keys` —— 键的**真值**即裁决:tool_call 的 `{{"block": True}}`。
          `{{block: False}}` 不算(不能用一个假值把后面的闸门短路掉)。
        * `stop_values` —— 键的**取值落在集合里**即裁决:input 的 `{{"action": "handled"}}`。
          这里必须按值判:同一个 `action` 键下 `transform` 是**链式**的(要接着往下改),
          把 `action` 当键真值判会让多级改写变成"第一级就定案"。

        `on_error_result` 是 fail-safe:handler 抛异常时用它当裁决(不给 = 只记错继续链)。
        """
        return await self._dispatch(event, payload, ctx, stop_keys=stop_keys,
                                    stop_values=stop_values,
                                    on_error_result=on_error_result)

    async def _dispatch(self, event: str, payload: dict | None,
                        ctx: ExtensionContext, *,
                        stop_keys: tuple[str, ...] | None,
                        stop_values: dict[str, tuple[str, ...]] | None,
                        on_error_result: dict | None) -> EmitResult:
        # payload 始终是**同一个** dict:handler 原地改就对后续可见(patch 链规则 2)
        base: dict = dict(payload or {})
        out = EmitResult(payload=base)
        # 快照遍历(规则 1):handler 里再 on() 不影响本次派发
        for source, handler in list(self._handlers.get(event, ())):
            try:
                returned = handler(base, ctx)
                if inspect.isawaitable(returned):
                    returned = await returned
            except Exception as exc:  # noqa: BLE001 规则 3:一个 handler 坏不拖垮整条链
                out.errors.append((source, exc))
                if on_error_result is not None:
                    # fail-safe:闸门崩了就拦(status quo 偏向"不执行")
                    out.result = dict(on_error_result)
                    out.stopped_by = source
                    break
                continue
            out.returns.append(returned)
            if isinstance(returned, dict):
                base.update(returned)
                if _is_verdict(returned, stop_keys, stop_values):
                    out.result = dict(returned)
                    out.stopped_by = source
                    break
            # 非 dict 返回值:忽略(不报错)—— 扩展可能比宿主新
        return out


@dataclass
class ExtensionApi:
    """传给扩展 `register(api)` 的接口。

    刻意**不 import** `ToolCatalog`(只鸭子类型用 `register`):`registry` 反向导入本模块
    来构造这个对象,直接 import 会成环。
    """

    catalog: Any
    bus: ExtensionBus
    _config_kinds: set[str] = field(default_factory=set)
    _types: dict[str, set[str]] = field(default_factory=dict)
    _name: str = ""

    # ── 工具 ──
    def add_tool(self, tool: Any) -> None:
        """注册工具(进 ToolCatalog)。`registerTool` 是 P-E2 的正式名,届时成为别名入口。"""
        self.catalog.register(tool)

    # ── 事件 ──
    def on(self, event: str, handler: ExtensionHandler) -> None:
        """订阅事件;来源自动带上本扩展名(诊断时能指到是谁)。"""
        self.bus.on(event, handler, source=self._name)

    # ── 消费型配置(v1 机制,原样继承)──
    def provides_config(self, kind: str, types: list[str] | None = None) -> None:
        """声明消费的 agent 配置种类;types 为该种类支持的 type 值(如数据源 mysql/…)。"""
        self._config_kinds.add(kind)
        if types:
            self._types.setdefault(kind, set()).update(types)

    @property
    def config_kinds(self) -> set[str]:
        return self._config_kinds


__all__ = [
    "EmitResult",
    "ExtensionApi",
    "ExtensionBus",
    "ExtensionContext",
    "ExtensionHandler",
    "ToolOutcome",
]
