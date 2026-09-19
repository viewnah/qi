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


# ── 命令与快捷键(扩展向 TUI/CLI 注册入口)──────────────────

#: 命令 handler:`async def handler(args: str, ctx) -> None`
CommandHandler = Callable[[str, "ExtensionContext"], Any | Awaitable[Any]]
#: 快捷键 handler:`async def handler(ctx) -> None`
ShortcutHandler = Callable[["ExtensionContext"], Any | Awaitable[Any]]


@dataclass
class ExtensionCommand:
    """一条扩展斜杠命令。`invocable` 是**实际能输入的**名字(重名时带 `:1`/`:2`)。"""

    name: str              # 原始名(扩展声明时的)
    invocable: str         # 加上后缀后的可输入名
    description: str
    handler: CommandHandler
    source: str            # 哪个扩展注册的(诊断/展示)


@dataclass
class ExtensionShortcut:
    """一条扩展快捷键。`key` 用 textual 的写法(如 `ctrl+shift+p`)。"""

    key: str
    description: str
    handler: ShortcutHandler
    source: str


@dataclass
class FlagSpec:
    """扩展声明的一个 CLI 旗标(docs/extensions.md §11.8 选定的 b 方案)。

    qi **不往 typer 的选项表里动态加东西**(实测:typer 的 `get_command` 每次重建,
    往 `TyperGroup` 里塞裸 `click.Option` 会崩在它自己的内置属性上)。core 只静态声明
    一个 `--ext name=value`(可重复),扩展旗标走它。
    """

    name: str
    type: str = "boolean"          # boolean | string
    default: Any = False
    description: str = ""
    source: str = ""


#: `--ext` 里接受的真/假写法。未列全的一律当“值非法”报出来,不静默取默认值。
_TRUTHY = ("true", "1", "yes", "on")
_FALSY = ("false", "0", "no", "off")


class FlagRegistry:
    """扩展旗标的登记处 + `--ext` 传来的值的解析。

    两条设计:
    * **未知旗标名会被拒绝**(`provide` 返回 False)—— 这是 `--ext` 方案保留
      “打错就报错”那一层保护的地方:没有它,`--ext agnet=reviewer` 会静静地什么也不做。
    * **值非法也报出来**(不静默取默认值):`--ext plan=maybe` 应该是错,
      而不是“用户以为开了、其实没开”。
    """

    def __init__(self) -> None:
        self._specs: dict[str, FlagSpec] = {}
        self._values: dict[str, Any] = {}
        #: “提供了但没用上 / 用不对”的说明(调用方转成 notes 让人看见)
        self.problems: list[str] = []

    # ── 声明(扩展侧)──
    def add(self, name: str, *, type: str = "boolean", default: Any = False,
            description: str = "", source: str = "") -> None:
        """重名时**第一条胜**,后来的记一条 problem(不静默丢掉,也不覆盖它)。"""
        key = name.strip().lstrip("-")
        if key in self._specs:
            self.problems.append(
                f"旗标 --{key} 已被 {self._specs[key].source} 声明"
                f"({source} 的重复声明被忽略)")
            return
        self._specs[key] = FlagSpec(name=key, type=type, default=default,
                                    description=description, source=source)

    # ── 取值(扩展侧)──
    def value(self, name: str) -> Any:
        """旗标当前值:`--ext` 给的优先,否则声明的 default;未声明的 → None。"""
        key = name.strip().lstrip("-")
        if key in self._values:
            return self._values[key]
        spec = self._specs.get(key)
        return spec.default if spec is not None else None

    # ── 喂值(宿主侧,来自 `--ext`)──
    def provide(self, pair: str) -> str | None:
        """`name=value`(或裸 `name` = 布尔真)。**返回错误说明**(None = 没问题)。

        返回字符串而不是布尔值:调用方需要把这句话**原样报给用户**,自己再拼一遍
        容易和这里的判断漂开(新增一种非法写法时只改了一处)。
        """
        raw = pair.strip()
        if not raw:
            return None
        name, sep, value = raw.partition("=")
        key = name.strip().lstrip("-")
        spec = self._specs.get(key)
        if spec is None:
            return (f"--ext {name} 没有对应的扩展旗标(是不是打错了?)"
                    + (f";已注册的是: {', '.join(self.names)}" if self._specs else
                       ";当前没有任何扩展声明旗标"))
        if spec.type != "boolean":
            if not sep:
                # pi 同款(它报 `Extension flag "--x" requires a value`):字符串旗标缺值
                # 要报错 —— 静默变成空串会表现为“我传了角色名,但它没生效”。
                return (f"旗标 {key} 需要值(写成 --{key}=值 或 --ext {key}=值)")
            self._values[key] = value
            return None
        if not sep:                                  # `--ext plan` = 打开
            self._values[key] = True
            return None
        lowered = value.strip().lower()
        if lowered in _TRUTHY:
            self._values[key] = True
            return None
        if lowered in _FALSY:
            self._values[key] = False
            return None
        return (f"--ext {key}={value} 不是布尔值"
                f"(接受 {'/'.join(_TRUTHY + _FALSY)};写 --ext {key} 即打开)")

    def specs(self) -> list[FlagSpec]:
        return [self._specs[k] for k in sorted(self._specs)]

    @property
    def names(self) -> list[str]:
        return sorted(self._specs)

    @property
    def is_empty(self) -> bool:
        return not self._specs


class CommandRegistry:
    """扩展命令与快捷键的登记处(与 `CapabilityRegistry` 并列的一个宿主侧汇合点)。

    **重名不覆盖,而是都留着并加序号**(`/review:1`、`/review:2`,对齐 pi):
    命令是用户显式输入的东西,静默丢掉一个会变成“我装了但打不出来”—— 而那种
    问题在现场是无法区分“没装”与“被覆盖”的。
    """

    def __init__(self) -> None:
        self._commands: dict[str, list[ExtensionCommand]] = {}
        self._shortcuts: list[ExtensionShortcut] = []

    # ── 注册 ──
    def add_command(self, name: str, handler: CommandHandler, *,
                    description: str = "", source: str = "") -> None:
        self._commands.setdefault(name, []).append(ExtensionCommand(
            name=name, invocable=name, description=description,
            handler=handler, source=source))
        self._renumber(name)

    def add_shortcut(self, key: str, handler: ShortcutHandler, *,
                     description: str = "", source: str = "") -> None:
        self._shortcuts.append(ExtensionShortcut(
            key=key, description=description, handler=handler, source=source))

    def _renumber(self, name: str) -> None:
        """同名命令一律带序号(`:1`、`:2`…)—— 只有一个时不加。"""
        holders = self._commands.get(name, [])
        for index, command in enumerate(holders, start=1):
            command.invocable = name if len(holders) == 1 else f"{name}:{index}"

    # ── 查询 ──
    def all(self) -> list[ExtensionCommand]:
        """全部命令(按可输入名排序)。"""
        out = [c for holders in self._commands.values() for c in holders]
        return sorted(out, key=lambda c: c.invocable)

    def find(self, invocable: str) -> ExtensionCommand | None:
        """按**可输入名**查(`review:2` 也认;`review` 在重名时查不到,要写全)。"""
        key = invocable.strip().lstrip("/")
        return next((c for c in self.all() if c.invocable == key), None)

    @property
    def names(self) -> list[str]:
        return [c.invocable for c in self.all()]

    def shortcuts(self) -> list[ExtensionShortcut]:
        return list(self._shortcuts)

    @property
    def is_empty(self) -> bool:
        return not self._commands and not self._shortcuts


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


class ExtensionUi:
    """`ctx.ui` —— 扩展向前端要交互的唯一入口(docs/extensions.md §5.1)。

    前端(TUI / web)提供一个鸭子类型的 backend,四个方法:`confirm` / `select` /
    `input`(异步,有返回值)与 `notify`(同步)。**backend 可选** —— 拿不到就走下面两条硬规则。

    1. **没有 backend 时必须有确定结果**:每个方法返回调用方给的 `default`。
       `confirm` 的 `default` 默认 **False**(拒绝是安全边),`select`/`input` 默认 None(取消)。
       “默认值由调用方给”是刻意的 —— 只有它知道“这里是拒绝安全还是继续安全”;
       而一个确定的结果就是 `-p` 永远不会卡在这里的原因。
    2. `notify` 在没有 backend 时**不丢弃**,落进宿主 `notes`。否则“扩展说了一句话”
       就凭空消失 —— 事情发生了但没人看见,是最难诊断的一类。

    backend 抛异常也走 `default`(记一条 note):交互是辅助手段,不该成为新的失败点。
    """

    def __init__(self, frontend: Any = None, notes: list[str] | None = None) -> None:
        self._frontend = frontend
        self._notes = notes if notes is not None else []

    @property
    def frontend(self) -> Any:
        return self._frontend

    @frontend.setter
    def frontend(self, value: Any) -> None:
        self._frontend = value

    @property
    def has_frontend(self) -> bool:
        """有人能弹交互吗(与 `ctx.has_ui` 不同:后者是“有人在看”)。"""
        return self._frontend is not None

    def _report(self, method: str, exc: Exception) -> None:
        self._notes.append(f"扩展的 ui.{method} 前端处理失败: {type(exc).__name__}: {exc}")

    async def confirm(self, message: str, *, title: str | None = None,
                      default: bool = False) -> bool:
        """问一个是/否。**默认 False** —— “拿不准就不做”是安全边。"""
        if self._frontend is None:
            return default
        try:
            value = await self._frontend.confirm(message, title=title, default=default)
        except Exception as exc:  # noqa: BLE001 界面坏不等于回合该失败
            self._report("confirm", exc)
            return default
        return default if value is None else bool(value)

    async def select(self, message: str, options: Sequence[str], *,
                     title: str | None = None, default: str | None = None) -> str | None:
        """让用户从 `options` 里选一个。取消 → None。"""
        if self._frontend is None:
            return default
        try:
            value = await self._frontend.select(message, list(options),
                                                title=title, default=default)
        except Exception as exc:  # noqa: BLE001
            self._report("select", exc)
            return default
        return default if value is None else str(value)

    async def input(self, message: str, *, title: str | None = None,
                    default: str | None = None, secret: bool = False) -> str | None:
        """要一行文本。取消 → None(注意:空字符串是“用户按了回车”,不是取消)。"""
        if self._frontend is None:
            return default
        try:
            value = await self._frontend.input(message, title=title, default=default,
                                               secret=secret)
        except Exception as exc:  # noqa: BLE001
            self._report("input", exc)
            return default
        return default if value is None else str(value)

    def notify(self, message: str, *, level: str = "info") -> None:
        """说一句不需要回答的话。无界面时进 `notes`,**不丢弃**。"""
        if self._frontend is None:
            self._notes.append(message)
            return
        try:
            self._frontend.notify(message, level=level)
        except Exception as exc:  # noqa: BLE001
            self._report("notify", exc)
            self._notes.append(message)


class SessionView:
    """`ctx.session_manager` —— 当前会话的**只读**视图。

    为什么不让扩展直接拿 `Session` / `SessionStore`:
    * 读——扩展要的只是“我说过什么、会话叫什么、分到哪个文件”,不需要知道 entry 形状;
    * 写——写口只有 `api.appendEntry` **一个**(章由宿主盖:agent 归属、source、落盘时机)。
      两个写口迟早写出两种 entry 形状。

    `entries()` 返回**当前分支**(不是整个文件):会话是树,扩展没理由看到别的分支。
    没有活动会话时读返回空 —— 写由 `api.appendEntry` 报错(不静默丢弃)。
    """

    def __init__(self, session: Any = None) -> None:
        self._session = session

    @property
    def available(self) -> bool:
        """有活动会话吗(回合外没有)。"""
        return self._session is not None

    @property
    def session_id(self) -> str | None:
        return getattr(self._session, "id", None)

    @property
    def path(self) -> str | None:
        p = getattr(self._session, "path", None)
        return str(p) if p is not None else None

    @property
    def title(self) -> str:
        return str(getattr(self._session, "title", "") or "")

    def entries(self) -> list[dict]:
        """当前分支的全部 entry(原样,宿主不裁剪) —— 扩展自己按 `type` 过滤。"""
        if self._session is None:
            return []
        return [dict(e) for e in self._session.branch()]

    def custom_entries(self, custom_type: str | None = None) -> list[dict]:
        """只取 `custom` entry(可再按 `custom_type` 过滤)—— 扩展存状态的常规做法。"""
        out = [e for e in self.entries() if e.get("type") == "custom"]
        if custom_type is not None:
            out = [e for e in out if e.get("custom_type") == custom_type]
        return out


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
    #: `ctx.ui` —— **总是存在**(不是 Optional):没前端时它按 `default` 回答,
    #: 所以扩展不必到处写 `if ctx.ui is not None`。要靠它做分支就查 `ctx.has_ui`。
    ui: ExtensionUi = field(default_factory=ExtensionUi)
    #: `ctx.session_manager` —— 当前会话的只读视图(见 `SessionView`)。回合外 `available` 为 False。
    session_manager: SessionView = field(default_factory=SessionView)

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


class ExtensionEvents:
    """`api.events` —— **扩展之间**的消息频道(不是宿主事件)。

    与 `api.on`(宿主事件)的分工:宿主事件是“qi 在通知你”,有链式返回值与裁决语义;
    这里只是“扩展之间说一声”。分开的意义在于两者**语义完全不同** —— 混成一件事的话,
    迟早有人给宿主事件 emit 一条自定义消息(然后奇怪为什么没人理),或者拿 peer 消息
    去拦工具调用(拦不住 —— 拦截必须走 `tool_call`)。

    `emit` **不等待**:pi 的 `pi.events.emit` 也是同步的。要拿结果就用宿主事件 +
    `sendMessage`,而不是把这里改成 async(那会让所有调用点都跟着改)。
    """

    def __init__(self, bus: "ExtensionBus", source: str = "") -> None:
        self._bus = bus
        self._source = source

    def on(self, name: str, handler: Callable[[Any], Any]) -> None:
        self._bus.on_message(name, handler, source=self._source)

    def emit(self, name: str, data: Any = None) -> None:
        self._bus.send_message(name, data, source=self._source)

    @property
    def names(self) -> list[str]:
        """已被订阅的 peer 消息名(诊断用)。"""
        return self._bus.message_names


class ExtensionBus:
    """扩展事件总线:按装载顺序链式派发(§4)。

    同一个对象上还挂着**扩展之间**的消息频道(`api.events`)。共用对象是刻意的:
    总线本来就是“所有扩展共享的那一个东西”(而且它是必填参数),另起一个对象
    迟早会出现“两个扩展各拿一个总线、消息静默发不到”.
    """

    def __init__(self, notes: list[str] | None = None) -> None:
        # event -> [(来源, handler)]。来源 = 扩展名(诊断时能指到具体哪个扩展)
        self._handlers: dict[str, list[tuple[str, ExtensionHandler]]] = {}
        #: peer 消息频道:`name -> [(来源, handler)]`。与上面的 `_handlers`(宿主事件)
        #: 是**两张分开的表** —— 所以同名的宿主事件与 peer 消息互不干扰。
        self._messages: dict[str, list[tuple[str, Callable[[Any], Any]]]] = {}
        #: handler 抛异常的去处(宿主传自己的 notes;没传就自己攒着,可从 `problems` 读)
        self._notes = notes if notes is not None else []

    # ── 注册 ──
    def on(self, event: str, handler: ExtensionHandler, *, source: str = "") -> None:
        """订阅事件。`source` 缺省用 handler 的模块名(库内调用方应显式给扩展名)。"""
        self._handlers.setdefault(event, []).append((source or _handler_name(handler), handler))

    # ── 扩展之间(peer messaging)──
    def on_message(self, name: str, handler: Callable[[Any], Any], *,
                   source: str = "") -> None:
        """订阅一条 peer 消息。handler 只收 `data`(没有 ctx —— peer 消息不该需要宿主上下文)。"""
        self._messages.setdefault(name, []).append(
            (source or _handler_name(handler), handler))

    def send_message(self, name: str, data: Any = None, *, source: str = "") -> None:
        """发一条 peer 消息:同步 handler 立即调,async handler 排成后台任务。

        **不等待**、**没有返回值**。单个 handler 抛异常 → 记进宿主的 notes 并继续
        (与宿主事件同一条规则:一个扩展坏掉不拖垮别的),
        """
        for listener, handler in list(self._messages.get(name, ())):
            try:
                result = handler(data)
            except Exception as exc:  # noqa: BLE001 第三方代码
                self._notes.append(f"扩展 {listener} 的 events.on({name!r}) 处理失败: {exc}")
                continue
            if inspect.isawaitable(result):
                self._schedule(result, listener, name)

    def _schedule(self, awaitable: Any, listener: str, name: str) -> None:
        """把 async handler 排成后台任务。没在事件循环里就丢掉(与 `_emit_notice` 同一条取舍)。"""
        async def _run() -> None:
            try:
                await awaitable
            except Exception as exc:  # noqa: BLE001 第三方代码
                self._notes.append(f"扩展 {listener} 的 events.on({name!r}) 处理失败: {exc}")

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            return

    @property
    def message_names(self) -> list[str]:
        return sorted(self._messages)

    @property
    def problems(self) -> list[str]:
        """handler 异常的记录(宿主没传 notes 时用这个读)。"""
        return list(self._notes)

    @property
    def events(self) -> list[str]:
        """已有人订阅的事件名(升序)。"""
        return sorted(self._handlers)

    def handler_count(self, event: str) -> int:
        return len(self._handlers.get(event, ()))

    def has(self, event: str) -> bool:
        """有人订阅这个事件吗?—— 宿主用它做**逐事件的开销护栏**:

        没有订阅就不构造 payload、不建 `EmitResult` 直接跳过。所以“零扩展时零开销”
        不是承诺,而是写在各派发点上的一行判断。
        """
        return bool(self._handlers.get(event))

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
    #: 以及会话写口:`append_extension_entry(custom_type, data, source) -> None`。
    #: 与消息队列:`queue_extension_message(text, deliver_as, source, kind) -> None`。
    #: 用鸭子类型而不是 Protocol:`extensions` 不能 import runtime(成环),而协议
    #: 在这里只起文档作用 —— 类型检查器验不到实现方,不如把契约写在这里。
    #: 没给 host 时 `getActiveTools` 退回“catalog 里的全部”,`setActiveTools` 报错。
    _host: Any = None
    #: 命令与快捷键的登记处(由发现阶段注入);没给则 `registerCommand` 报错。
    _commands: CommandRegistry | None = None
    #: CLI 旗标的登记处(同上);没给则 `registerFlag` 报错。
    _flags: FlagRegistry | None = None

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

    # ── 命令与快捷键 ──
    def registerCommand(self, name: str, handler: CommandHandler, *,
                        description: str = "") -> None:      # noqa: N802
        """注册一条斜杠命令(pi 的 `registerCommand`)。handler 收 `(args, ctx)`。

        重名不覆盖:两条都留着并变成 `name:1` / `name:2`(见 `CommandRegistry`)。
        """
        if self._commands is None:
            raise RuntimeError("宿主没有提供命令登记处:registerCommand 不可用")
        self._commands.add_command(name.strip().lstrip("/"), handler,
                                   description=description, source=self._name)

    def registerShortcut(self, key: str, handler: ShortcutHandler, *,
                         description: str = "") -> None:     # noqa: N802
        """注册一个快捷键(pi 的 `registerShortcut`)。key 用 textual 的写法。"""
        if self._commands is None:
            raise RuntimeError("宿主没有提供命令登记处:registerShortcut 不可用")
        self._commands.add_shortcut(key, handler, description=description,
                                    source=self._name)

    def getCommands(self) -> list[dict]:                     # noqa: N802
        """当前可输入的命令清单(给自动补全 / 帮助用)。"""
        if self._commands is None:
            return []
        return [{"name": c.invocable, "description": c.description, "source": c.source}
                for c in self._commands.all()]

    async def runAgent(self, spec: Any, task: str, *, abort: AbortSignal | None = None,
                       on_event: Any = None) -> str:      # noqa: N802
        """在宿主内起一个**受管的子运行**:独立上下文、自己的工具集与模型。

        这是 qi-agents(以及任何“把一个任务交给另一个角色”的需求)的基石。
        `spec` 是 dict:`system_prompt`(必填)/ `tools`(缺省**继承父**的当前集合)/
        `model`(缺省继承父)/ `name`。

        与 `sendMessage` 的区别:那个是“往当前对话里插一句话”,这个是“另起一个**不碰
        会话**的运行” —— 子运行不落盘、不分派,但扩展事件照常派发(所以闸门对它也生效)。

        `on_event` 给进度用(子运行的工具调用能实时上报给父界面)。
        """
        if self._host is None or not callable(getattr(self._host, "run_agent", None)):
            raise RuntimeError("宿主没有提供子运行接口:runAgent 不可用")
        return await self._host.run_agent(spec, task, abort=abort, on_event=on_event)

    # ── CLI 旗标 ──
    def registerFlag(self, name: str, *, type: str = "boolean", default: Any = False,
                     description: str = "") -> None:        # noqa: N802
        """声明一个 CLI 旗标(pi 的 `registerFlag`)。

        **值不走 `--plan` 这种短形式**,而是 `qi --ext plan=true` —— 原因见 §11.8:
        typer 的选项表是静态的,为动态旗标放宽 `ignore_unknown_options` 会把用户
        打错的选项变成一句 prompt。`--ext` 显式、可 grep、不可能和笔误混淆。
        """
        if self._flags is None:
            raise RuntimeError("宿主没有提供旗标登记处:registerFlag 不可用")
        self._flags.add(name, type=type, default=default,
                        description=description, source=self._name)

    def getFlag(self, name: str) -> Any:                     # noqa: N802
        """读旗标当前值(`--ext` 给的优先,否则声明的 default)。"""
        if self._flags is None:
            return None
        return self._flags.value(name)

    # ── 扩展之间 ──
    @property
    def events(self) -> ExtensionEvents:
        """`api.events` —— 扩展之间的消息频道(见 `ExtensionEvents`)。"""
        return ExtensionEvents(self.bus, source=self._name)

    # ── provider ──
    def registerProvider(self, name: str, config: dict | None = None) -> None:   # noqa: N802
        """动态注册/覆盖一个 provider(代理、自定义端点、团队模型配置)。

        **只改内存,不写 `models.json`** —— 注册的 provider 活在这个进程里。
        pi 允许扩展持久化目录元数据(带 generation 校验),那是另一套机制,qi 没做。

        覆盖同名 provider 是允许的(代理正是这个用途),但会记一条 note:
        “我明明配了 models.json,却被别人改了” 很难查。
        """
        if self._host is None or not callable(getattr(self._host, "register_provider", None)):
            raise RuntimeError("宿主没有提供 provider 注册接口:registerProvider 不可用")
        self._host.register_provider(name, dict(config or {}), self._name)

    # ── 会话 ──
    def appendEntry(self, custom_type: str, data: dict | None = None) -> None:   # noqa: N802
        """落一条扩展自定义 entry(进会话文件,**不进 LLM 上下文**)。

        这才是持久化扩展状态的正确位置:它不会污染对话上下文,但刷新/重开后还在
        (`ctx.session_manager.custom_entries(name)` 读回来)。

        回合外没有活动会话时**报错** —— 静默丢掉意味着“我存了但重启后没了”。
        """
        if self._host is None or not callable(
                getattr(self._host, "append_extension_entry", None)):
            raise RuntimeError("宿主没有提供会话写口:appendEntry 不可用")
        self._host.append_extension_entry(custom_type, dict(data or {}), self._name)

    def sendMessage(self, message: str | dict, *, deliver_as: str = "steer") -> None:  # noqa: N802
        """往当前对话插一条消息(**进** LLM 上下文;与 `appendEntry` 相反)。

        `deliver_as` 决定**什么时候**送达(照搬 pi 的三档):

        * `steer`(默认)—— 本轮的**下一次 LLM 调用**之前(即当前这轮工具跑完之后)。
          适合“工具结果里发现了个事,先告诉模型”。
        * `follow_up` —— 等 agent **本该收工**时才送:有排队消息就不收工,继续跑一轮。
          适合“顺手再做一件事”。
        * `next_turn` —— 不打断本轮,留到**下一次用户输入**。

        收字符串或 `{"content": …}`(与 `before_agent_start` 的 `message` 同形)。
        """
        text = message if isinstance(message, str) else str(message.get("content") or "")
        self._queue_message(text, deliver_as, "sendMessage")

    def sendUserMessage(self, content: str, *, deliver_as: str = "steer") -> None:   # noqa: N802
        """插一条**用户**消息(送达时机与 `sendMessage` 相同,只是语义上是“用户说的”)。

        **qi 不自动开一轮**:pi 的 `sendUserMessage` 在 agent 空闲时会`triggerTurn`,
        而那需要“在处理器里嵌套跑一轮”的能力(嵌套流式)。qi 现在的做法是**排队**,
        由前端决定要不要因此开一轮 —— 这条差别写在 §11.9。
        """
        self._queue_message(content, deliver_as, "sendUserMessage")

    def _queue_message(self, text: str, deliver_as: str, kind: str) -> None:
        body = (text or "").strip()
        if not body:
            return                                   # 空白不入队(与 message 注入一致)
        if deliver_as not in ("steer", "follow_up", "next_turn"):
            raise ValueError(f"deliver_as 只能是 steer / follow_up / next_turn,收到 {deliver_as!r}")
        if self._host is None or not callable(
                getattr(self._host, "queue_extension_message", None)):
            raise RuntimeError("宿主没有提供消息队列:sendMessage 不可用")
        self._host.queue_extension_message(body, deliver_as, self._name, kind)

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
    "CommandRegistry",
    "EmitResult",
    "ExecResult",
    "ExtensionApi",
    "ExtensionBus",
    "ExtensionCommand",
    "ExtensionContext",
    "ExtensionEvents",
    "ExtensionHandler",
    "ExtensionShortcut",
    "ExtensionUi",
    "FlagRegistry",
    "FlagSpec",
    "SessionView",
    "Tool",
    "ToolError",
    "ToolExecutor",
    "ToolOutcome",
    "exec_command",
    "register_tool",
    "tool_info",
]
