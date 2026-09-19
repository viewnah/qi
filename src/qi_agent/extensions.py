"""扩展宿主:事件总线 + 扩展上下文 + 工具定义 + 传给 `register(api)` 的接口。

对应 pi 的 `ExtensionAPI` / `ExtensionContext` / 事件订阅 / `registerTool`
(docs/extensions.md §3、§4)。

**公开面白名单**(§5.5 —— pi 的 `## Available Imports` 对应物)。承诺的只有本模块:

    ExtensionBus / ExtensionApi / ExtensionContext / EmitResult
    Tool / ToolError / ToolExecutor / ToolOutcome / register_tool

P-E2a 把 `Tool` 从 `registry.py` 搬了过来(那里只留 `ToolCatalog`):扩展要写工具
就必然要这个类型,让它住在“注册表”里等于把内部结构当公开面。

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

import asyncio
import contextlib
import inspect
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .abort import AbortSignal
from .models import ToolOutcome

#: `handler(payload, ctx) -> dict | None | Awaitable[...]`
#: `payload` 是本次派发的那一个 dict(原地改 = 对后续 handler 可见)。
ExtensionHandler = Callable[[dict, "ExtensionContext"], Any | Awaitable[Any]]

#: 工具执行函数签名:`async def execute(args, ctx) -> str | ToolOutcome`
#: 返回 `str` 即视为成功;需要上报 status/exit_code 的工具返回 `ToolOutcome`。
ToolExecutor = Callable[[dict, Any], Awaitable[str | ToolOutcome]]


class ToolError(Exception):
    """工具执行错误:以结果文本返回给模型,**不中断会话**(runner 会包成 error 结果)。"""


@dataclass
class Tool:
    """一个可被模型调用的工具。

    `description` 与 `prompt_snippet` 是**两个不同的面向**(pi 同款区分):

    * `description` 进 tool schema —— 模型在“要不要调这个工具”时看的说明,可以长一些;
    * `prompt_snippet` 进系统提示词「可用工具」那一行(省略则回落到 `description`)。

    合并写会让 schema 里塞进一整段散文 / 提示词里又缺一行摘要。`prompt_guidelines`
    是该工具被启用时才追加的指南 bullet(pi 的 `promptGuidelines`),**必须自带工具名**:
    指南是平铺追加的,写“用这个工具…”模型分不清“这个”指谁。
    """

    name: str
    description: str
    parameters: dict                        # JSON Schema
    execute: ToolExecutor                   # async def execute(args, ctx) -> str | ToolOutcome
    keywords: list[str] = field(default_factory=list)
    prompt_snippet: str = ""
    prompt_guidelines: list[str] = field(default_factory=list)
    #: 谁装的(由 `register_tool` 盖):`{source, path, scope, origin}`。
    #: `getAllTools()` 按它过滤/诊断 —— 扩展工具与内置工具靠这个区分。
    source_info: dict | None = None

    @property
    def prompt_line(self) -> str:
        """系统提示词「可用工具」里那一行。回落规则只此一处。"""
        return self.prompt_snippet or self.description

    def to_llm_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def register_tool(catalog: Any, tool: Tool, *, source: str = "", path: str = "",
                  scope: str = "", origin: str = "") -> None:
    """把工具注册进 catalog,**并盖上来源**(`source_info`)。

    盖章只在这一处:让每个调用点自己拼一个 dict,迟早会漏字段,而 `source_info`
    是 `getAllTools()` 过滤和“这个工具到底谁装的”唯一依据。键名照 pi 的 `sourceInfo`。

    已带 `source_info` 的工具不覆盖(工具可以自己声明更精确的来源)。
    """
    if tool.source_info is None:
        tool.source_info = {"source": source or "unknown", "path": path,
                           "scope": scope or "temporary", "origin": origin or "top-level"}
    catalog.register(tool)


def tool_info(tool: Tool) -> dict:
    """工具的公开元数据(`getAllTools()` 的元素)。

    字段用 snake_case(qi 自己的 Python 数据);方法的**名字**照 pi 保留 camelCase,
    因为那是扩展作者要背的那部分。
    """
    return {"name": tool.name, "description": tool.description,
            "prompt_snippet": tool.prompt_snippet,
            "prompt_guidelines": list(tool.prompt_guidelines),
            "parameters": tool.parameters,
            "source_info": dict(tool.source_info or {})}


def _note(host: Any, text: str) -> None:
    """往宿主的 notes 通道丢一条可读提示(宿主可能没有这个属性)。"""
    notes = getattr(host, "notes", None)
    if isinstance(notes, list):
        notes.append(text)


#: 单个子进程 stdout/stderr 的捕获上限(与工具输出同档)
MAX_EXEC_OUTPUT = 50_000


@dataclass
class ExecResult:
    """`api.exec()` 的结果(pi 的 `pi.exec` 同形)。"""

    command: str
    args: list[str]
    code: int | None            # None = 被信号杀掉(含超时/中断)
    stdout: str
    stderr: str
    killed: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.code == 0


async def exec_command(command: str, args: Sequence[str] | None = None, *,
                       cwd: str | Path | None = None, timeout: float | None = None,
                       signal: AbortSignal | None = None,
                       env: dict[str, str] | None = None) -> ExecResult:
    """起一个**不经 shell** 的子进程(对齐 pi 的 `pi.exec`)。

    不经 shell 是刻意的:`args` 原样进 argv,扩展不必自己拼引号 —— 也就不会因为少转义
    一个空格而执行了别的命令。要 shell 语义就显式 `bash -c`。

    `timeout` 与 `signal` 任一命中都**杀进程**并把 `killed` 置真(`code` 为 None):
    协作式取消在这里体现为`kill()`,因为子进程不会自己检查 Python 的 flag。
    """
    argv = [command, *[str(a) for a in (args or ())]]
    started = time.perf_counter_ns()

    def elapsed() -> int:
        return (time.perf_counter_ns() - started) // 1_000_000

    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd) if cwd else None,
        env={**os.environ, **env} if env else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    comm = asyncio.ensure_future(proc.communicate())
    watcher = asyncio.ensure_future(signal.wait()) if signal is not None else None
    killed = False
    try:
        waiters: set[asyncio.Future] = {comm}
        if watcher is not None:
            waiters.add(watcher)
        done, _pending = await asyncio.wait(waiters, timeout=timeout,
                                            return_when=asyncio.FIRST_COMPLETED)
        if comm not in done:
            # 超时或中断 —— 子进程不会看 Python 的中断标志,所以只能杀
            killed = True
            proc.kill()
        out, err = await comm
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await watcher

    return ExecResult(
        command=command, args=argv[1:],
        code=None if killed else proc.returncode,
        stdout=_decode(out), stderr=_decode(err),
        killed=killed, duration_ms=elapsed())


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_EXEC_OUTPUT:
        return text[:MAX_EXEC_OUTPUT] + "\n…(输出已截断)"
    return text


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
    #: 来源信息(`source_info` 的四个键;由发现阶段告诉 api 它是从哪来的)
    _path: str = ""
    _scope: str = "temporary"      # user | project | temporary
    _origin: str = "top-level"     # top-level | package
    #: 宿主(runtime)提供的工具集读写面 —— 鸭子类型,只要有两个方法:
    #:   `tool_names() -> list[str]`(本回合实际启用的工具名)
    #:   `set_tool_names(names) -> None`(覆盖,对后续回合生效)
    #: 用鸭子类型而不是 Protocol:`extensions` 不能 import runtime(成环),而协议
    #: 在这里只起文档作用 —— 类型检查器验不到实现方,不如把契约写在这里。
    #: 没给 host 时 `getActiveTools` 退回“catalog 里的全部”,`setActiveTools` 报错。
    _host: Any = None

    # ── 工具 ──
    def registerTool(self, tool: Tool) -> None:      # noqa: N802 pi 的方法名,保持同形
        """注册工具(进 ToolCatalog),并盖上本扩展的来源。

        **装载后也能调**(事件里、命令里):catalog 是活的对象,而 `tools: ["*"]` 的
        agent 每回合**当场重算**工具集,所以新工具下一轮就能调,不需要 `/reload`。
        """
        register_tool(self.catalog, tool, source=self._name, path=self._path,
                      scope=self._scope, origin=self._origin)

    def add_tool(self, tool: Tool) -> None:
        """v1 旧名;`registerTool` 是正式名(对齐 pi)。"""
        self.registerTool(tool)

    def getAllTools(self) -> list[dict]:             # noqa: N802
        """所有**已注册**工具的元数据(含 `source_info`)。

        纯 catalog 查询,不需要宿主 —— 所以装载阶段就能用(例如扩展想知道
        自己是不是唯一的 `grep` 提供者)。
        """
        return [tool_info(t) for t in self.catalog.all()]

    def getActiveTools(self) -> list[str]:           # noqa: N802
        """本回合实际启用的工具名。宿主没给工具面时退回“catalog 里的全部”。"""
        if self._host is None:
            return sorted(self.catalog.names)
        return list(self._host.tool_names())

    def setActiveTools(self, names: Sequence[str]) -> None:   # noqa: N802
        """改运行时的工具集(plan-mode / 只读角色那种需求)。

        **未知名字被过滤**而不是报错:pi 允许先把名字放进集合、工具随后才动态注册。
        但静默丢弃也不行(打错一个字等于悄悄改了权限),所以过虑掉的会写进宿主的
        `notes` —— 看得见。
        """
        if self._host is None:
            raise RuntimeError("宿主没有提供工具集接口:setActiveTools 不可用")
        wanted = [str(n) for n in names]
        unknown = sorted({n for n in wanted if n not in self.catalog.names})
        if unknown:
            _note(self._host, f"setActiveTools 收到未注册的工具名(已忽略): {unknown}")
        self._host.set_tool_names(wanted)

    async def exec(self, command: str, args: Sequence[str] | None = None, *,
                   cwd: str | Path | None = None, timeout: float | None = None,
                   signal: AbortSignal | None = None) -> ExecResult:
        """起一个不经 shell 的子进程(pi 的 `pi.exec`)。

        异步场景**必须**把 `ctx.signal` 传进来,否则用户按 Esc 取消不掉它。
        """
        return await exec_command(command, args, cwd=cwd, timeout=timeout, signal=signal)

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
    "ExecResult",
    "ExtensionApi",
    "ExtensionBus",
    "ExtensionContext",
    "ExtensionHandler",
    "Tool",
    "ToolError",
    "ToolExecutor",
    "ToolOutcome",
    "exec_command",
    "register_tool",
    "tool_info",
]
