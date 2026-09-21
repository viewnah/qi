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

#: `deliver_as` 的两种写法归一:qi 的 snake_case 与 pi 的驼峰(值本身也是 pi 的约定)。
_DELIVER_AS = {
    "steer": "steer",
    "follow_up": "follow_up", "followUp": "follow_up", "followup": "follow_up",
    "next_turn": "next_turn", "nextTurn": "next_turn", "nextturn": "next_turn",
}


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
    #: pi 的 `ToolDefinition.label` —— 界面上显示的名字(TUI 工具卡片标题)。
    #: 空 = 回落到 `name`(不是所有工具都需要一个好看的名字)。
    label: str = ""
    #: pi 的 `prepareArguments`:拿到**原始**工具调用参数后、执行前的整理钩子。
    #: 返回的 dict 才是真正执行用的那一份(也进 `tool_call` 事件的 `input`)。
    #: qi **不做** schema 校验,所以它的位置就是“执行前最后一次整理”。
    prepare_arguments: Callable[[Any], Any] | None = None
    #: pi 的 `renderCall` / `renderResult` —— 扩展自己画工具卡片(TUI-only,见 §5.1)。
    #: 签名 `(args|outcome, ctx) -> 组件`;不认识的返回值由前端退回默认渲染。
    render_call: Callable[..., Any] | None = None
    render_result: Callable[..., Any] | None = None

    @property
    def prompt_line(self) -> str:
        """系统提示词「可用工具」里那一行。回落规则只此一处。"""
        return self.prompt_snippet or self.description

    @property
    def display_label(self) -> str:
        """界面显示名(`label` 缺省回落到 `name`)。"""
        return self.label or self.name

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


def _coerce_int(value: Any, default: int = 0) -> int:
    """把配置里可能写成字符串/None 的数字收成 int(不因一个坏字段把 ctx 构造搞崩)。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _adapt_execute(fn: Any) -> ToolExecutor:
    """pi 的 execute 是 `(toolCallId, params, signal, onUpdate, ctx)`,qi 是 `(args, ctx)`。

    按**形参个数**判:pi 形状的包一层(第 1 个参数给 `ctx.tool_call_id`,第 4 个给
    `ctx.on_update`)。拿不到签名就原样用 —— 猜错比不猜更糟。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn
    if len(params) < 4:
        return fn

    async def wrapper(args: dict, ctx: Any) -> Any:
        result = fn(getattr(ctx, "tool_call_id", "") or "", args,
                    getattr(ctx, "abort", None), getattr(ctx, "on_update", None), ctx)
        return await result if inspect.isawaitable(result) else result

    return wrapper  # type: ignore[return-value]


def _coerce_tool(tool: Any) -> Tool:
    """把 pi 形状的 dict 工具定义收成 `Tool`(已经是 `Tool` 的原样返回)。"""
    if isinstance(tool, Tool):
        return tool
    if not isinstance(tool, dict):
        raise TypeError(f"register_tool 需要 Tool 或 dict,收到 {type(tool).__name__}")

    def pick(*names: str) -> Any:
        for key in names:
            if tool.get(key) is not None:
                return tool[key]
        return None

    name = pick("name")
    execute = pick("execute")
    if not name or not callable(execute):
        raise TypeError("register_tool 的 dict 必须带 name 与 execute")
    return Tool(
        name=str(name),
        description=str(pick("description") or ""),
        parameters=pick("parameters") or {"type": "object", "properties": {}},
        execute=_adapt_execute(execute),
        prompt_snippet=str(pick("promptSnippet", "prompt_snippet") or ""),
        prompt_guidelines=list(pick("promptGuidelines", "prompt_guidelines") or []),
        label=str(pick("label") or ""),
        prepare_arguments=pick("prepareArguments", "prepare_arguments"),
        render_call=pick("renderCall", "render_call"),
        render_result=pick("renderResult", "render_result"),
    )


def tool_info(tool: Tool) -> dict:
    """工具的公开元数据(`getAllTools()` 的元素)。

    字段用 snake_case(qi 自己的 Python 数据);方法的**名字**照 pi 保留 camelCase,
    因为那是扩展作者要背的那部分。
    """
    info = {"name": tool.name, "label": tool.display_label,
            "description": tool.description,
            "prompt_snippet": tool.prompt_snippet,
            "prompt_guidelines": list(tool.prompt_guidelines),
            "parameters": tool.parameters,
            "source_info": dict(tool.source_info or {})}
    # pi 的键名(驼峰)也放一份:qi 自己的数据用 snake_case,但照 pi 写的扩展
    # (`t.promptGuidelines` / `t.sourceInfo`)也要能直接跑 —— 两套键指同一个值。
    info["promptGuidelines"] = info["prompt_guidelines"]
    info["sourceInfo"] = info["source_info"]
    return info


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
#: 能力解析器(E20 的产物):拿到一个**作用域**(`scope`),交回该作用域下这个种类的工具。
#: (MCP 已改走按值注入 E25,不再用它;完整论证见 design/extensions-design.md §13。)
#: 返回 `list[Tool]` 或 awaitable。`scope` 的形态由提供方与消费方约定 —— qi 里通常是
#: 一个 agent 目录(§7.4)。故意用 `...` 而不是固定签名:作用域这个参数到底长什么样,
#: 是提供方与消费方之间的事,core 不应当插进来定它。
ResolverFn = Callable[..., Any | Awaitable[Any]]
#: CLI 子命令处理器:收**命令名之后的原始 argv**(如 `["--port", "30142"]`),回退出码或 None。
#: 故意不接 typer/click 的解析结果:选项表是静态的,让扩展自己解析自己的参数是唯一稳的
#: 形状(E19 实测:动态改 typer 的选项表会把用户打错的选项变成别的东西)。
CliHandler = Callable[[list[str]], int | None]


@dataclass
class CliCommand:
    """一个扩展注册的 CLI 子命令(`qi <name> …`)。"""

    name: str
    handler: CliHandler
    description: str = ""
    source: str = ""


class CliCommandRegistry:
    """扩展 CLI 子命令的登记处(与 `CommandRegistry` 并列的宿主侧汇合点)。

    **重名不编号、也不静默丢** —— 与斜杠命令的处理不同,因为 CLI 名字是用户敲进终端的
    第一个词,`qi web:1` 这种东西没法用。所以撞名是**错误**而不是排队:两个扩展都想叫
    `web` 是用户必须知道并解决的事。
    """

    def __init__(self) -> None:
        self._commands: dict[str, CliCommand] = {}

    def add_command(self, name: str, handler: CliHandler, *,
                    description: str = "", source: str = "") -> bool:
        """返回 `False` = 这个名字已被占(调用方据此报错,而不是默默丢掉一个)。"""
        if name in self._commands:
            return False
        self._commands[name] = CliCommand(name=name, handler=handler,
                                          description=description, source=source)
        return True

    def find(self, name: str) -> CliCommand | None:
        return self._commands.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._commands)

    def all(self) -> list[CliCommand]:
        return [self._commands[n] for n in self.names]


@dataclass
class ExtensionCommand:
    """一条扩展斜杠命令。`invocable` 是**实际能输入的**名字(重名时带 `:1`/`:2`)。"""

    name: str              # 原始名(扩展声明时的)
    invocable: str         # 加上后缀后的可输入名
    description: str
    handler: CommandHandler
    source: str            # 哪个扩展注册的(诊断/展示)
    #: pi 的 `getArgumentCompletions(prefix) -> list | None`(参数补全)。qi 暂未消费,
    #: 但**收下来** —— 注册了不生效比报错难查,所以 `get_commands()` 会把它带出去。
    get_argument_completions: Any = None


@dataclass
class ExtensionShortcut:
    """一条扩展快捷键。`key` 用 textual 的写法(如 `ctrl+shift+p`)。"""

    key: str
    description: str
    handler: ShortcutHandler
    source: str


@dataclass
class FlagSpec:
    """扩展声明的一个 CLI 旗标(design/extensions-design.md §11.8 选定的 b 方案)。

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
                    description: str = "", source: str = "",
                    get_argument_completions: Any = None) -> None:
        self._commands.setdefault(name, []).append(ExtensionCommand(
            name=name, invocable=name, description=description,
            handler=handler, source=source,
            get_argument_completions=get_argument_completions))
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


class RendererRegistry:
    """渲染回调的登记处(pi 的 `registerMessageRenderer` / `registerEntryRenderer` /
    `registerMarkdownTransformer`)。

    **TUI-only**:回调返回的是前端组件,所以非 TUI 前端退回默认渲染(与 pi 的
    `ctx.mode` 门控同一口径)。这里只负责**登记** —— 什么时候调由前端决定。

    同名后注册者胜(pi 的行为):一个扩展重注册自己的渲染器是常见写法。
    """

    def __init__(self) -> None:
        self._messages: dict[str, Any] = {}
        self._entries: dict[str, Any] = {}
        self._markdown: list[Any] = []

    def add_message(self, custom_type: str, renderer: Any, *, source: str = "") -> None:
        self._messages[str(custom_type)] = (renderer, source)

    def add_entry(self, custom_type: str, renderer: Any, *, source: str = "") -> None:
        self._entries[str(custom_type)] = (renderer, source)

    def add_markdown(self, transformer: Any, *, source: str = "") -> None:
        self._markdown.append((transformer, source))

    def message_renderer(self, custom_type: str) -> Any | None:
        found = self._messages.get(str(custom_type))
        return found[0] if found else None

    def entry_renderer(self, custom_type: str) -> Any | None:
        found = self._entries.get(str(custom_type))
        return found[0] if found else None

    def transformers(self) -> list[Any]:
        return [fn for fn, _ in self._markdown]

    def apply_markdown(self, markdown: str, ctx: Any = None) -> str:
        """按注册顺序链式跑 markdown transformer(单个抛错只跳过它)。"""
        text = markdown
        for fn, source in self._markdown:
            try:
                text = str(fn(text, ctx) if ctx is not None else fn(text))
            except Exception:  # noqa: BLE001 渲染器坏不该把消息弄丢
                continue
        return text

    @property
    def is_empty(self) -> bool:
        return not self._messages and not self._entries and not self._markdown


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

    分两层(与 pi 一致):

    * **数据层** —— 问答(`confirm`/`select`/`input`/`editor`)与状态(`notify`/
      `set_status`/`set_title`/`set_working_*`/`set_hidden_thinking_label`/
      `get_set_tools_expanded`/主题)。跨前端:TUI 与 web 都该实现;没前端时退回确定值。
    * **组件层** —— `set_widget` / `set_footer` / `set_header` / `custom` /
      `set_editor_component` / `add_autocomplete_provider` / `on_terminal_input`。
      **TUI-only**(pi 同样只在 `ctx.mode === "tui"` 下有意义):其他前端下 `custom()`
      返回 None、注册类调用记一条 note 后 no-op —— 不静默。

    两条硬规则(§4 同级的契约,有测试钉住):

    1. **没有后端时每个问答方法返回调用方给的 `default`**,于是“交互”退化成“按事先
       声明好的策略走”,而**永远不会挂住**。`confirm` 的 `default` 默认 **False**
       (拒绝是安全边);`select`/`input`/`editor` 默认 None(取消)。默认值由**调用方**
       给 —— 只有它知道“这里是拒绝安全还是继续安全”。
    2. `notify` 没有后端时**落进 `notes`**,不丢弃(否则“扩展说了一句话”就凭空消失)。

    后端自己抛异常也走 `default`(记一条 note):交互是辅助手段,不该成为新的失败点。
    """

    def __init__(self, frontend: Any = None, notes: list[str] | None = None,
                 mode: str = "print", bus: Any = None, ctx_factory: Any = None) -> None:
        self._frontend = frontend
        self._notes = notes if notes is not None else []
        self.mode = mode
        #: `ui_prompt_start` / `ui_prompt_end` 的派发面(pi 用来观察“有人在等交互”)。
        #: 没给总线就只有交互,不发事件。
        self._bus = bus
        self._ctx_factory = ctx_factory

    async def _emit_prompt(self, phase: str, kind: str, title: str | None) -> None:
        """发 `ui_prompt_start` / `ui_prompt_end`(失败不影响交互本身)。"""
        event = f"ui_prompt_{phase}"
        if self._bus is None or not self._bus.has(event):
            return
        ctx = self._ctx_factory() if callable(self._ctx_factory) else None
        if ctx is None:
            return
        try:
            await self._bus.emit(event,
                                 {"reason": "ui_prompt", "kind": kind, "title": title},
                                 ctx=ctx)
        except Exception:  # noqa: BLE001 通知失败不该影响交互
            return

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

    def _forward(self, method: str, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
        """转给前端。返回 `(拿到值了吗, 值)` —— 前端没有这个方法就是 `(False, None)`。"""
        fn = getattr(self._frontend, method, None)
        if not callable(fn):
            return False, None
        try:
            return True, fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 界面坏不等于回合该失败
            self._report(method, exc)
            return True, None

    def _component_unavailable(self, method: str) -> None:
        """组件层在当前前端上不可用时的统一处理:**不静默**(组件被丢了是功能损失)。"""
        self._notes.append(
            f"ui.{method} 需要 TUI 前端且该前端实现了它(ctx.mode={self.mode!r});已忽略")

    def _forward_component(self, method: str, *args: Any) -> bool:
        """组件层转发:前端没实现这个方法时**记一条 note**并返回 False。"""
        if self._frontend is None or self.mode != "tui":
            self._component_unavailable(method)
            return False
        found, _ = self._forward(method, *args)
        if not found:
            self._component_unavailable(method)
            return False
        return True

    # ── 数据层:问答 ──
    async def confirm(self, message: str, *, title: str | None = None,
                      default: bool = False) -> bool:
        """问一个是/否。**默认 False** —— “拿不准就不做”是安全边。

        `title` 与 `message` 的顺序照 qi 的写法(message 必填);pi 是
        `confirm(title, message)` —— 两个都收(见下 `confirm_title_first` 说明)。
        """
        if self._frontend is None:
            return default
        await self._emit_prompt("start", "confirm", title)
        try:
            value = await self._frontend.confirm(message, title=title, default=default)
        except Exception as exc:  # noqa: BLE001 界面坏不等于回合该失败
            self._report("confirm", exc)
            return default
        finally:
            await self._emit_prompt("end", "confirm", title)
        return default if value is None else bool(value)

    async def select(self, message: str, options: Sequence[str], *,
                     title: str | None = None, default: str | None = None) -> str | None:
        """让用户从 `options` 里选一个。取消 → None。"""
        if self._frontend is None:
            return default
        await self._emit_prompt("start", "select", title)
        try:
            value = await self._frontend.select(message, list(options),
                                               title=title, default=default)
        except Exception as exc:  # noqa: BLE001
            self._report("select", exc)
            return default
        finally:
            await self._emit_prompt("end", "select", title)
        return default if value is None else str(value)

    async def input(self, message: str, *, title: str | None = None,
                    default: str | None = None, secret: bool = False) -> str | None:
        """要一行文本。取消 → None(注意:空字符串是“用户按了回车”,不是取消)。"""
        if self._frontend is None:
            return default
        await self._emit_prompt("start", "input", title)
        try:
            value = await self._frontend.input(message, title=title, default=default,
                                              secret=secret)
        except Exception as exc:  # noqa: BLE001
            self._report("input", exc)
            return default
        finally:
            await self._emit_prompt("end", "input", title)
        return default if value is None else str(value)

    async def editor(self, prefill: str = "", *, title: str | None = None) -> str | None:
        """多行编辑(pi 的 `ui.editor(title, prefill)`)。无前端 → None。"""
        if self._frontend is None:
            return None
        await self._emit_prompt("start", "editor", title)
        try:
            found, value = self._forward("editor", prefill, title=title)
            if not found:                       # 前端没实现多行 → 退回单行 input
                return await self.input(title or "编辑", default=prefill)
            return None if value is None else str(value)
        finally:
            await self._emit_prompt("end", "editor", title)

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

    # ── 数据层:状态与外观 ──
    def set_status(self, key: str, text: str | None) -> None:
        """footer/状态栏里的一条常驻状态(`text=None` 清除)。"""
        if self._frontend is None:
            return
        self._forward("set_status", key, text)

    def set_title(self, title: str) -> None:
        """终端窗口/标签页标题。"""
        if self._frontend is None:
            return
        self._forward("set_title", title)

    def set_working_message(self, message: str | None = None) -> None:
        """流式过程中那一行“正在做事”的文案(不传 = 恢复默认)。"""
        if self._frontend is None:
            return
        self._forward("set_working_message", message)

    def set_working_visible(self, visible: bool) -> None:
        if self._frontend is None:
            return
        self._forward("set_working_visible", visible)

    def set_working_indicator(self, options: dict | None = None) -> None:
        """动画帧/间隔配置(`{"frames": [...], "intervalMs": int}`)。"""
        if self._frontend is None:
            return
        self._forward("set_working_indicator", options)

    def set_hidden_thinking_label(self, label: str | None = None) -> None:
        """折叠思考块显示的那个短标签。"""
        if self._frontend is None:
            return
        self._forward("set_hidden_thinking_label", label)

    def get_tools_expanded(self) -> bool:
        return bool(self._forward("get_tools_expanded")[1]) if self._frontend else False

    def set_tools_expanded(self, expanded: bool) -> None:
        if self._frontend is None:
            return
        self._forward("set_tools_expanded", expanded)

    @property
    def theme(self) -> Any:
        """当前主题(无前端 / 无该能力 → None)。

        注意走 `getattr` 而不是 `_forward`:pi 的 `ui.theme` 是**属性**,前端的也是属性
        (取到的是 Palette,不可调用) —— 拿 `_forward` 会被 `callable` 判掉而永远返回 None。
        """
        if self._frontend is None:
            return None
        return getattr(self._frontend, "theme", None)

    def get_all_themes(self) -> list[dict]:
        value = self._forward("get_all_themes")[1] if self._frontend else None
        return list(value) if isinstance(value, list) else []

    def get_theme(self, name: str) -> Any:
        return self._forward("get_theme", name)[1] if self._frontend else None

    def set_theme(self, theme: Any) -> dict:
        """切主题。返回 `{success, error?}`(pi 同形)。"""
        if self._frontend is None:
            return {"success": False, "error": "没有前端"}
        found, value = self._forward("set_theme", theme)
        if not found:
            return {"success": False, "error": "前端不支持切主题"}
        return dict(value) if isinstance(value, dict) else {"success": bool(value)}

    # ── 数据层:编辑器内容 ──
    def paste_to_editor(self, text: str) -> None:
        if self._frontend is None:
            return
        self._forward("paste_to_editor", text)

    def set_editor_text(self, text: str) -> None:
        if self._frontend is None:
            return
        self._forward("set_editor_text", text)

    def get_editor_text(self) -> str:
        value = self._forward("get_editor_text")[1] if self._frontend else None
        return "" if value is None else str(value)

    # ── 组件层(TUI-only)──
    def set_widget(self, key: str, content: Any,
                   options: dict | None = None) -> None:
        """在编辑器上/下方挂一个常驻 widget(pi 的 `setWidget`;`content=None` 移除)。

        `content` 是字符串行列表,或一个 `(ctx) -> 组件` 的工厂。
        """
        self._forward_component("set_widget", key, content, options)

    def set_footer(self, factory: Any) -> None:
        """整个替换 footer(`None` = 恢复内置)。"""
        self._forward_component("set_footer", factory)

    def set_header(self, factory: Any) -> None:
        """整个替换启动 header(`None` = 恢复内置)。"""
        self._forward_component("set_header", factory)

    async def custom(self, factory: Any, options: dict | None = None) -> Any:
        """弹一个扩展自己的组件(可带键盘焦点 / overlay)。无 TUI → None(pi 同形)。"""
        if self._frontend is None or self.mode != "tui":
            return None
        found, value = self._forward("custom", factory, options)
        if not found:
            self._component_unavailable("custom")
            return None
        await self._emit_prompt("start", "custom", None)
        try:
            return await value if inspect.isawaitable(value) else value
        finally:
            await self._emit_prompt("end", "custom", None)

    def set_editor_component(self, factory: Any) -> None:
        self._forward_component("set_editor_component", factory)

    def get_editor_component(self) -> Any:
        return self._forward("get_editor_component")[1] if self._frontend else None

    def add_autocomplete_provider(self, factory: Any) -> None:
        self._forward_component("add_autocomplete_provider", factory)

    def on_terminal_input(self, handler: Any) -> Any:
        """听原始终端输入。返回**退订函数**(pi 同形);不可用时返回 no-op 函数。

        不可用不是静默的 —— 读键盘是功能性的(不是装饰),丢了该看得见。
        """
        if self._frontend is None or self.mode != "tui":
            self._component_unavailable("on_terminal_input")
            return lambda: None
        found, value = self._forward("on_terminal_input", handler)
        if not found:
            self._component_unavailable("on_terminal_input")
            return lambda: None
        return value if callable(value) else (lambda: None)

    # ── pi 的驼峰别名 ──
    def setStatus(self, key: str, text: str | None) -> None:            # noqa: N802
        self.set_status(key, text)

    def setTitle(self, title: str) -> None:                             # noqa: N802
        self.set_title(title)

    def setWorkingMessage(self, message: str | None = None) -> None:    # noqa: N802
        self.set_working_message(message)

    def setWorkingVisible(self, visible: bool) -> None:                 # noqa: N802
        self.set_working_visible(visible)

    def setWorkingIndicator(self, options: dict | None = None) -> None:  # noqa: N802
        self.set_working_indicator(options)

    def setHiddenThinkingLabel(self, label: str | None = None) -> None:  # noqa: N802
        self.set_hidden_thinking_label(label)

    def getToolsExpanded(self) -> bool:                                 # noqa: N802
        return self.get_tools_expanded()

    def setToolsExpanded(self, expanded: bool) -> None:                 # noqa: N802
        self.set_tools_expanded(expanded)

    def getAllThemes(self) -> list[dict]:                               # noqa: N802
        return self.get_all_themes()

    def getTheme(self, name: str) -> Any:                               # noqa: N802
        return self.get_theme(name)

    def setTheme(self, theme: Any) -> dict:                             # noqa: N802
        return self.set_theme(theme)

    def pasteToEditor(self, text: str) -> None:                         # noqa: N802
        self.paste_to_editor(text)

    def setEditorText(self, text: str) -> None:                         # noqa: N802
        self.set_editor_text(text)

    def getEditorText(self) -> str:                                     # noqa: N802
        return self.get_editor_text()

    def setWidget(self, key: str, content: Any, options: dict | None = None) -> None:  # noqa: N802
        self.set_widget(key, content, options)

    def setFooter(self, factory: Any) -> None:                          # noqa: N802
        self.set_footer(factory)

    def setHeader(self, factory: Any) -> None:                          # noqa: N802
        self.set_header(factory)

    def setEditorComponent(self, factory: Any) -> None:                 # noqa: N802
        self.set_editor_component(factory)

    def getEditorComponent(self) -> Any:                                # noqa: N802
        return self.get_editor_component()

    def addAutocompleteProvider(self, factory: Any) -> None:            # noqa: N802
        self.add_autocomplete_provider(factory)

    def onTerminalInput(self, handler: Any) -> Any:                     # noqa: N802
        return self.on_terminal_input(handler)


class ModelView(str):
    """`ctx.model` —— pi 那边是一个 `Model` 对象(qi 内部是 `ResolvedModel`)。

    做成 `str` 的子类是刻意的:qi 既有用法(`ctx.model == "provider/model"`、直接
    进 JSON / 进提示词)一字不变,而 pi 的字段访问(`ctx.model.id` /
    `ctx.model.contextWindow`)也成立 —— 两边的语义同时满足,而不是二选一。
    """

    # 类级注解:str 子类不能用 `__slots__`,所以用注解把动态属性告诉类型检查器
    provider: str
    id: str
    name: str
    api: str
    base_url: str | None
    reasoning: bool
    context_window: int
    max_tokens: int

    def __new__(cls, provider: str, model_id: str, *, api: str = "",
                base_url: str | None = None, reasoning: bool = False,
                context_window: int = 0, max_tokens: int = 0, name: str = "") -> "ModelView":
        obj = super().__new__(cls, f"{provider}/{model_id}")
        obj.provider = provider
        obj.id = model_id                       # pi 的 `model.id`
        obj.name = name or model_id
        obj.api = api
        obj.base_url = base_url
        obj.reasoning = reasoning
        obj.context_window = context_window
        obj.max_tokens = max_tokens
        return obj

    @classmethod
    def from_resolved(cls, resolved: Any) -> "ModelView":
        """从内部的 `ResolvedModel` 造一个(str 子类,所以 `== "p/m"` 也真)。"""
        if resolved is None:
            raise ValueError("from_resolved 需要 ResolvedModel,收到 None")
        entry = getattr(resolved, "entry", None)
        return cls(getattr(resolved, "provider", "") or "",
                   getattr(resolved, "model", "") or "",
                   api=getattr(resolved, "api", "") or "",
                   base_url=getattr(resolved, "base_url", None),
                   reasoning=bool(getattr(resolved, "reasoning", False)),
                   context_window=_coerce_int(getattr(resolved, "context_window", 0)),
                   max_tokens=_coerce_int(getattr(resolved, "max_tokens", 0)),
                   name=str(getattr(entry, "name", "") or ""))

    # ── pi 的字段名(驼峰)──
    @property
    def model(self) -> str:
        return self.id

    @property
    def contextWindow(self) -> int:              # noqa: N802
        return self.context_window

    @property
    def maxTokens(self) -> int:                  # noqa: N802
        return self.max_tokens

    @property
    def baseUrl(self) -> str | None:             # noqa: N802
        return self.base_url

    @property
    def label(self) -> str:
        return str(self)


class SessionView:
    """`ctx.session_manager` —— 当前会话的**只读**视图。

    为什么不让扩展直接拿 `Session` / `SessionStore`:
    * 读——扩展要的只是“我说过什么、会话叫什么、分到哪个文件”,不需要知道 entry 形状;
    * 写——写口只有 `api.append_entry` **一个**(章由宿主盖:agent 归属、source、落盘时机)。
      两个写口迟早写出两种 entry 形状。

    `entries()` 返回**当前分支**(不是整个文件):会话是树,扩展没理由看到别的分支。
    没有活动会话时读返回空/None —— 写由 `api.append_entry` 报错(不静默丢弃)。

    方法名两面都有:qi 的 snake_case 是正式名,pi 的驼峰是别名(`getEntry` /
    `getBranch` / …),所以照 pi 写的扩展能直接跑。
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

    # ── pi ReadonlySessionManager 对应面 ──
    def get_entries(self) -> list[dict]:
        return self.entries()

    def get_entry(self, entry_id: str) -> dict | None:
        for entry in getattr(self._session, "entries", []) or []:
            if str(entry.get("id")) == str(entry_id):
                return dict(entry)
        return None

    def get_leaf_id(self) -> str | None:
        return getattr(self._session, "leaf", None)

    def get_leaf_entry(self) -> dict | None:
        leaf = self.get_leaf_id()
        return self.get_entry(leaf) if leaf else None

    def get_branch(self, leaf: str | None = None) -> list[dict]:
        if self._session is None:
            return []
        try:
            return [dict(e) for e in self._session.branch(leaf)]
        except TypeError:            # 测试替身可能不收参数
            return self.entries()

    def build_context_entries(self) -> list[dict]:
        """进 LLM 上下文的那一串(= 当前分支)。"""
        return self.entries()

    def get_header(self) -> dict | None:
        entries = getattr(self._session, "entries", []) or []
        if entries and entries[0].get("type") == "session":
            return dict(entries[0])
        return None

    def get_cwd(self) -> str | None:
        cwd = getattr(self._session, "cwd", None)
        return str(cwd) if cwd is not None else None

    def get_session_dir(self) -> str | None:
        p = getattr(self._session, "path", None)
        return str(Path(p).parent) if p is not None else None

    def get_session_file(self) -> str | None:
        return self.path

    def get_session_name(self) -> str | None:
        return self.title or None

    def get_label(self, entry_id: str) -> str | None:
        """某条 entry 的 label(label entry 盖在 targetId 上,后写者胜)。"""
        found: str | None = None
        for entry in getattr(self._session, "entries", []) or []:
            if entry.get("type") == "label" and str(entry.get("targetId")) == str(entry_id):
                found = entry.get("label")
        return found

    def get_tree(self) -> list[dict]:
        """嵌套树(`[{entry, children, label}]`),给 `/tree` 那类渲染用。"""
        session = self._session
        if session is None:
            return []
        nodes = list(getattr(session, "tree_entries", []))
        children: dict[str | None, list[dict]] = {}
        for entry in nodes:
            children.setdefault(entry.get("parentId") or None, []).append(entry)

        def build(entry: dict) -> dict:
            node = {"entry": dict(entry), "children": []}
            label = self.get_label(str(entry.get("id")))
            if label is not None:
                node["label"] = label
            node["children"] = [build(c) for c in children.get(str(entry.get("id")), [])]
            return node

        return [build(e) for e in children.get(None, [])]

    # ── pi 的驼峰别名 ──
    def getSessionId(self) -> str | None:        # noqa: N802
        return self.session_id

    def getSessionFile(self) -> str | None:      # noqa: N802
        return self.path

    def getSessionName(self) -> str | None:      # noqa: N802
        return self.get_session_name()

    def getCwd(self) -> str | None:              # noqa: N802
        return self.get_cwd()

    def getEntries(self) -> list[dict]:          # noqa: N802
        return self.entries()

    def getEntry(self, entry_id: str) -> dict | None:      # noqa: N802
        return self.get_entry(entry_id)

    def getLeafId(self) -> str | None:           # noqa: N802
        return self.get_leaf_id()

    def getLeafEntry(self) -> dict | None:       # noqa: N802
        return self.get_leaf_entry()

    def getBranch(self, leaf: str | None = None) -> list[dict]:   # noqa: N802
        return self.get_branch(leaf)

    def buildContextEntries(self) -> list[dict]:  # noqa: N802
        return self.build_context_entries()

    def getHeader(self) -> dict | None:          # noqa: N802
        return self.get_header()

    def getLabel(self, entry_id: str) -> str | None:      # noqa: N802
        return self.get_label(entry_id)

    def getTree(self) -> list[dict]:             # noqa: N802
        return self.get_tree()

    def getSessionDir(self) -> str | None:       # noqa: N802
        return self.get_session_dir()


class ModelRegistryView:
    """`ctx.model_registry` —— 模型目录的只读面 + provider 注册(pi 的 `ModelRegistry`)。

    只做扩展真正会用的那几个(列全部/可用、按 provider+id 查、鉴权状态、注册/注销
    provider),不做完整的解析与补全 —— 那属于宿主内部。
    """

    def __init__(self, host: Any = None) -> None:
        self._host = host

    def _call(self, name: str, default: Any, *args: Any, **kwargs: Any) -> Any:
        if self._host is None:
            return default
        fn = getattr(self._host, name, None)
        if not callable(fn):
            return default
        return fn(*args, **kwargs)

    def get_all(self) -> list[Any]:
        return list(self._call("model_catalog", []))

    def get_available(self) -> list[Any]:
        return list(self._call("available_models", []) or self.get_all())

    def find(self, provider: str, model_id: str) -> Any | None:
        for model in self.get_all():
            if model.provider == provider and model.id == model_id:
                return model
        return None

    def has_configured_auth(self, model: Any) -> bool:
        return bool(self._call("has_configured_auth", False, model))

    def get_provider_display_name(self, provider: str) -> str:
        return str(self._call("provider_display_name", provider, provider))

    def register_provider(self, name: str, config: dict | None = None) -> None:
        if self._host is None:
            raise RuntimeError("宿主没有提供 provider 注册接口:register_provider / registerProvider 不可用")
        self._host.register_provider(name, dict(config or {}), "extension")

    def unregister_provider(self, name: str) -> None:
        if self._host is None:
            raise RuntimeError("宿主没有提供 provider 注销接口:unregister_provider / unregisterProvider 不可用")
        self._host.unregister_provider(name)

    # ── pi 的驼峰别名 ──
    def getAll(self) -> list[Any]:               # noqa: N802
        return self.get_all()

    def getAvailable(self) -> list[Any]:         # noqa: N802
        return self.get_available()

    def hasConfiguredAuth(self, model: Any) -> bool:      # noqa: N802
        return self.has_configured_auth(model)

    def getProviderDisplayName(self, provider: str) -> str:   # noqa: N802
        return self.get_provider_display_name(provider)

    def registerProvider(self, name: str, config: dict | None = None) -> None:   # noqa: N802
        self.register_provider(name, config)

    def unregisterProvider(self, name: str) -> None:      # noqa: N802
        self.unregister_provider(name)



@dataclass(frozen=True)
class ExtensionContext:
    """`ctx` —— 传给每个 handler 的上下文(见 docs/extensions.md §3.3)。

    字段是**只读值**(frozen),但方法(`abort()` / `compact()` / `getContextUsage()` /
    命令上下文的那几个)要回宿主去问 —— 所以带一个 `host` 后向引用(鸭子类型;
    `extensions` 不能 import `runtime`,会成环)。没给 host 时读方法退回确定值、
    写方法报错(与 `ctx.ui` 没前端时的口径一致:**不静默**)。

    `notes` 是**故意可变**的:扩展也能往启动提示里加话(它是宿主给前端的唯一提示通道,
    runtime 自己不打印)。frozen 只管字段重绑定,不管列表内容。

    命名:qi 的 snake_case 是正式名,pi 的驼峰 (`isProjectTrusted` / `getSystemPrompt` /
    `thinkingLevel` / `sessionManager` / …) 是别名。
    """

    cwd: Path
    #: 当前模型。`ModelView` 是 `str` 子类:既有的 `== "provider/model"` 用法不变,
    #: pi 的 `ctx.model.id` / `ctx.model.contextWindow` 也成立。
    model: ModelView | None = None
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
    #: 运行模式:`tui` | `rpc` | `json` | `print`(pi 的 `ExtensionMode`)。
    #: 组件层 UI(`ctx.ui.custom` / widget / footer)只在 `tui` 里有意义 ——
    #: 扩展该用 `ctx.mode == "tui"` 做门控(pi 的写法)。
    mode: str = "print"
    #: 本回合绑定的会话对象(命令上下文要它来切会话)。**不是**公开面 —— 扩展读用
    #: `ctx.session_manager`。
    session: Any = field(default=None, repr=False, compare=False)
    #: `--models` / `enabledModels` 圈定的模型(空 = 不限制)。
    scoped_models: tuple[str, ...] = ()
    #: 宿主后向引用(runtime)。读方法缺它时给确定值,写方法报错。
    host: Any = field(default=None, repr=False, compare=False)
    #: `ctx.model_registry`(pi 的 `ModelRegistry` 只读面)。
    model_registry: ModelRegistryView = field(default_factory=ModelRegistryView)

    # ── pi 的驼峰属性别名 ──
    @property
    def hasUI(self) -> bool:                     # noqa: N802
        return self.has_ui

    @property
    def thinkingLevel(self) -> str:              # noqa: N802
        return self.thinking_level

    @property
    def sessionManager(self) -> SessionView:     # noqa: N802
        return self.session_manager

    @property
    def scopedModels(self) -> tuple[str, ...]:   # noqa: N802
        return self.scoped_models

    @property
    def modelRegistry(self) -> ModelRegistryView:   # noqa: N802
        return self.model_registry

    # ── 信任 ──
    def is_project_trusted(self) -> bool:
        """pi 的 `ctx.isProjectTrusted()`。"""
        return self.project_trusted

    def isProjectTrusted(self) -> bool:          # noqa: N802
        return self.project_trusted

    # ── 运行控制(pi 的 ExtensionContext 同名方法)──
    def abort(self) -> None:
        """中断当前回合(pi 的 `ctx.abort()`)。没有回合在跑时是 no-op。"""
        if self.signal is not None:
            self.signal.abort()
        host_abort = getattr(self.host, "abort", None)
        if callable(host_abort) and self.signal is None:
            host_abort()

    def is_idle(self) -> bool:
        """agent 空闲吗(没在流式)(pi 的 `ctx.isIdle()`)。"""
        fn = getattr(self.host, "is_idle", None)
        return bool(fn()) if callable(fn) else True

    def isIdle(self) -> bool:                    # noqa: N802
        return self.is_idle()

    def has_pending_messages(self) -> bool:
        """有没有排队等送达的扩展消息(pi 的 `ctx.hasPendingMessages()`)。"""
        fn = getattr(self.host, "has_pending_messages", None)
        return bool(fn()) if callable(fn) else False

    def hasPendingMessages(self) -> bool:        # noqa: N802
        return self.has_pending_messages()

    def shutdown(self) -> None:
        """优雅退出 qi(pi 的 `ctx.shutdown()`;只在 TUI/CLI 下有意义)。"""
        fn = getattr(self.host, "shutdown", None)
        if callable(fn):
            fn()

    def get_context_usage(self) -> Any:
        """当前上下文占用(pi 的 `ContextUsage`:`{tokens, contextWindow, percent}`)。"""
        fn = getattr(self.host, "context_usage", None)
        return fn() if callable(fn) else None

    def getContextUsage(self) -> Any:            # noqa: N802
        return self.get_context_usage()

    def compact(self, options: dict | None = None) -> None:
        """触发一次压缩,**不等**它完成(pi 的 `ctx.compact()` 同语义)。"""
        fn = getattr(self.host, "compact", None)
        if callable(fn):
            fn(options)

    def get_system_prompt(self) -> str:
        """当前生效的 system prompt 全文(pi 的 `ctx.getSystemPrompt()`)。"""
        fn = getattr(self.host, "current_system_prompt", None)
        return str(fn()) if callable(fn) else ""

    def getSystemPrompt(self) -> str:            # noqa: N802
        return self.get_system_prompt()

    def get_system_prompt_options(self) -> dict:
        """造 system prompt 用的结构化选项(pi 的 `ctx.getSystemPromptOptions()`)。"""
        fn: Any = getattr(self.host, "system_prompt_options", None)
        if not callable(fn):
            return {}
        value = fn()
        return dict(value) if isinstance(value, dict) else {}

    def getSystemPromptOptions(self) -> dict:    # noqa: N802
        return self.get_system_prompt_options()

    # ── 会话操作(pi 的 ExtensionCommandContext:只在命令/事件处理器里安全)──
    async def wait_for_idle(self) -> None:
        fn: Any = getattr(self.host, "wait_for_idle", None)
        if callable(fn):
            result: Any = fn()
            if inspect.isawaitable(result):
                await result

    async def waitForIdle(self) -> None:         # noqa: N802
        await self.wait_for_idle()

    async def new_session(self, options: dict | None = None) -> dict:
        """开一个新会话。返回 `{cancelled: bool}`(pi 同形)。"""
        return await self._session_op("extension_new_session", options)

    async def newSession(self, options: dict | None = None) -> dict:   # noqa: N802
        return await self.new_session(options)

    async def fork(self, entry_id: str, options: dict | None = None) -> dict:
        return await self._session_op("extension_fork", entry_id, options)

    async def navigate_tree(self, target_id: str, options: dict | None = None) -> dict:
        return await self._session_op("extension_navigate_tree", target_id, options)

    async def navigateTree(self, target_id: str, options: dict | None = None) -> dict:   # noqa: N802
        return await self.navigate_tree(target_id, options)

    async def switch_session(self, session_path: str, options: dict | None = None) -> dict:
        return await self._session_op("extension_switch_session", session_path, options)

    async def switchSession(self, session_path: str, options: dict | None = None) -> dict:   # noqa: N802
        return await self.switch_session(session_path, options)

    async def reload(self) -> None:
        """重载扩展与资源(pi 的 `ctx.reload()`)。"""
        fn = getattr(self.host, "reload", None)
        if callable(fn):
            result = fn()
            if inspect.isawaitable(result):
                await result

    async def _session_op(self, name: str, *args: Any) -> dict:
        fn: Any = getattr(self.host, name, None)
        if not callable(fn):
            raise RuntimeError(f"宿主没有提供会话操作:{name} 不可用(要宿主拥有会话控制权)")
        result = fn(self, *args)
        if inspect.isawaitable(result):
            return await result
        return dict(result) if isinstance(result, dict) else {}


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

    def on(self, name: str, handler: Callable[[Any], Any]) -> Callable[[], None]:
        """订阅一条 peer 消息。**返回退订函数**(pi 的 `EventBus.on` 同形)。"""
        self._bus.on_message(name, handler, source=self._source)

        def _unsubscribe() -> None:
            self._bus.remove_message(name, handler)

        return _unsubscribe

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

    def remove_message(self, name: str, handler: Callable[[Any], Any]) -> bool:
        """退订一条 peer 消息(按 handler 身份匹配)。返回是否真的移除了。"""
        listeners = self._messages.get(name)
        if not listeners:
            return False
        for index, (_, existing) in enumerate(listeners):
            if existing is handler:
                listeners.pop(index)
                return True
        return False

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
    #: CLI 子命令的登记处(同上);没给则 `registerCliCommand` 报错。
    _cli_commands: CliCommandRegistry | None = None
    #: 能力交接的汇合点(宿主侧,发现阶段注入 —— 类型写 `Any` 是因为 `CapabilityRegistry`
    #: 住在 registry.py,而它反过来 import 本模块,写成具体类型会成环)。
    #: 与 `_host` / `catalog` 一样是鸭子类型:只需要 `add_resolver` / `resolve_tools`。
    _capabilities: Any = None
    #: 渲染回调的登记处(pi 的 `registerMessageRenderer` / `registerEntryRenderer` /
    #: `registerMarkdownTransformer`)。没给则那三个方法记一条 note(不静默)。
    _renderers: Any = None

    # ── 工具 ──
    def register_tool(self, tool: Tool | dict) -> None:
        """注册工具(进 ToolCatalog),并盖上本扩展的来源。

        **装载后也能调**(事件里、命令里):catalog 是活的对象,而 `tools: ["*"]` 的
        agent 每回合**当场重算**工具集,所以新工具下一轮就能调,不需要 `/reload`。

        收 `Tool`,也收 pi 形状的 dict(`{name, label, description, promptSnippet,
        promptGuidelines, parameters, execute}`;snake_case 键也认)。
        """
        register_tool(self.catalog, _coerce_tool(tool), source=self._name,
                      path=self._path, scope=self._scope, origin=self._origin)

    def add_tool(self, tool: Tool) -> None:
        """v1 旧名;`registerTool` 是正式名(对齐 pi)。"""
        self.register_tool(tool)

    def get_all_tools(self) -> list[dict]:             # noqa: N802
        """所有**已注册**工具的元数据(含 `source_info`)。

        纯 catalog 查询,不需要宿主 —— 所以装载阶段就能用(例如扩展想知道
        自己是不是唯一的 `grep` 提供者)。
        """
        return [tool_info(t) for t in self.catalog.all()]

    def get_active_tools(self) -> list[str]:           # noqa: N802
        """本回合实际启用的工具名。宿主没给工具面时退回“catalog 里的全部”。"""
        if self._host is None:
            return sorted(self.catalog.names)
        return list(self._host.tool_names())

    def set_active_tools(self, names: Sequence[str]) -> None:   # noqa: N802
        """改运行时的工具集(plan-mode / 只读角色那种需求)。

        **未知名字被过滤**而不是报错:pi 允许先把名字放进集合、工具随后才动态注册。
        但静默丢弃也不行(打错一个字等于悄悄改了权限),所以过虑掉的会写进宿主的
        `notes` —— 看得见。
        """
        if self._host is None:
            raise RuntimeError("宿主没有提供工具集接口:set_active_tools / setActiveTools 不可用")
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
    def register_command(self, name: str, handler: CommandHandler | dict | None = None, *,
                         description: str = "",
                         get_argument_completions: Any = None) -> None:
        """注册一条斜杠命令(pi 的 `registerCommand`)。handler 收 `(args, ctx)`。

        两种写法都收:
        * qi 形状:`register_command("deploy", handler, description="…")`
        * pi 形状:`register_command("deploy", {"handler": h, "description": "…",
          "getArgumentCompletions": fn})`

        重名不覆盖:两条都留着并变成 `name:1` / `name:2`(见 `CommandRegistry`)。
        """
        options = handler if isinstance(handler, dict) else None
        if options is not None:
            handler = options.get("handler")
            description = description or str(options.get("description") or "")
            get_argument_completions = (get_argument_completions
                                        or options.get("getArgumentCompletions")
                                        or options.get("get_argument_completions"))
        if not callable(handler):
            raise TypeError("register_command 需要 handler(位置参数或 options 里的 'handler')")
        if self._commands is None:
            raise RuntimeError("宿主没有提供命令登记处:register_command / registerCommand 不可用")
        self._commands.add_command(name.strip().lstrip("/"), handler,
                                   description=description, source=self._name,
                                   get_argument_completions=get_argument_completions)

    def register_shortcut(self, key: str, handler: ShortcutHandler | dict | None = None, *,
                          description: str = "") -> None:
        """注册一个快捷键(pi 的 `registerShortcut`)。key 用 textual 的写法。

        兼容 pi 的 options 对象写法(`{"handler": h, "description": "…"}`)。
        """
        options = handler if isinstance(handler, dict) else None
        if options is not None:
            handler = options.get("handler")
            description = description or str(options.get("description") or "")
        if not callable(handler):
            raise TypeError("register_shortcut 需要 handler(位置参数或 options 里的 'handler')")
        if self._commands is None:
            raise RuntimeError("宿主没有提供命令登记处:register_shortcut / registerShortcut 不可用")
        self._commands.add_shortcut(key, handler, description=description,
                                    source=self._name)

    def get_commands(self) -> list[dict]:
        """当前可输入的命令清单(给自动补全 / 帮助用)。

        元素形状照 pi 的 `SlashCommandInfo`(name/description/source/sourceInfo),
        另带 `has_argument_completions`(qi 自己的诊断字段)。
        """
        if self._commands is None:
            return []
        return [{"name": c.invocable, "description": c.description, "source": c.source,
                 "source_info": {"source": c.source},
                 "sourceInfo": {"source": c.source},
                 "has_argument_completions": callable(c.get_argument_completions)}
                for c in self._commands.all()]

    async def run_agent(self, spec: Any, task: str, *, abort: AbortSignal | None = None,
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
            raise RuntimeError("宿主没有提供子运行接口:run_agent / runAgent 不可用")
        return await self._host.run_agent(spec, task, abort=abort, on_event=on_event)

    # ── CLI 旗标 ──
    def register_flag(self, name: str, options: dict | None = None, *,
                      type: str = "boolean", default: Any = False,
                      description: str = "") -> None:
        """声明一个 CLI 旗标(pi 的 `registerFlag`)。

        **值不走 `--plan` 这种短形式**,而是 `qi --ext plan=true` —— 原因见 §11.8:
        typer 的选项表是静态的,为动态旗标放宽 `ignore_unknown_options` 会把用户
        打错的选项变成一句 prompt。`--ext` 显式、可 grep、不可能和笔误混淆。

        兼容 pi 的 options 对象写法(`register_flag("plan", {"type": "boolean"})`)。
        """
        if isinstance(options, dict):
            type = str(options.get("type") or type)
            default = options.get("default", default)
            description = description or str(options.get("description") or "")
        if self._flags is None:
            raise RuntimeError("宿主没有提供旗标登记处:register_flag / registerFlag 不可用")
        self._flags.add(name, type=type, default=default,
                        description=description, source=self._name)

    def get_flag(self, name: str) -> Any:                     # noqa: N802
        """读旗标当前值(`--ext` 给的优先,否则声明的 default)。"""
        if self._flags is None:
            return None
        return self._flags.value(name)

    # ── CLI 子命令 ──
    def register_cli_command(self, name: str, handler: CliHandler, *,    # noqa: N802
                           description: str = "") -> None:
        """注册一个 CLI 子命令:`qi <name> [参数…]`。

        `handler(argv)` 收到**命令名之后的原始 argv**(如 `["--port", "30142"]`),返回
        退出码或 None。**故意不接 typer/click 的解析结果**:选项表是静态的,动态改它会把
        用户打错的选项变成别的东西(E19 实测),所以各扩展自己解析自己的参数。

        宿主会在 typer 之前认出这个名字(`cli._dispatch_extension_command`)—— 扩展不必知道
        typer 的存在。**与 core 子命令同名时 core 优先**;两个扩展撞名则是错误(CLI 名字是用户
        敲的第一个词,`qi web:1` 这种没法用,所以不编号)。
        """
        if self._cli_commands is None:
            # **没登记处就是 no-op**:CLI 子命令只在"由 CLI 入口装载"的那个宿主里能被派发,
            # 而宿主有很多种(runtime / doctor / 测试里的轻量发现 / 第三方)。让这里报错等于
            # "凡是注册 CLI 子命令的扩展,在任何不关心 CLI 的宿主里都装不上" —— 而 qi-web 就是
            # 这种扩展。**重名仍然报错**(那是真冲突),只是"这儿的宿主不收"不算错。
            return
        if not self._cli_commands.add_command(name, handler, description=description,
                                              source=self._name):
            raise RuntimeError(f"CLI 子命令 `{name}` 已被占用(改个名字,或确认没有装两个))")

    # ── 扩展之间 ──
    @property
    def events(self) -> ExtensionEvents:
        """`api.events` —— 扩展之间的消息频道(见 `ExtensionEvents`)。"""
        return ExtensionEvents(self.bus, source=self._name)

    # ── provider ──
    def register_provider(self, name: str, config: dict | None = None) -> None:   # noqa: N802
        """动态注册/覆盖一个 provider(代理、自定义端点、团队模型配置)。

        **只改内存,不写 `models.json`** —— 注册的 provider 活在这个进程里。
        pi 允许扩展持久化目录元数据(带 generation 校验),那是另一套机制,qi 没做。

        覆盖同名 provider 是允许的(代理正是这个用途),但会记一条 note:
        “我明明配了 models.json,却被别人改了” 很难查。
        """
        if self._host is None or not callable(getattr(self._host, "register_provider", None)):
            raise RuntimeError("宿主没有提供 provider 注册接口:register_provider / registerProvider 不可用")
        self._host.register_provider(name, dict(config or {}), self._name)

    def unregister_provider(self, name: str) -> None:
        """注销一个先前注册的 provider(pi 的 `unregisterProvider`)。

        找不到就无操作(pi 同义)—— “本来就没了”与“刚被删了”在调用点无法区分,
        报错只会让清理逻辑多一层无谓的异常处理。
        """
        host = getattr(self._host, "unregister_provider", None)
        if not callable(host):
            raise RuntimeError("宿主没有提供 provider 注销接口:unregister_provider / unregisterProvider 不可用")
        host(name)

    # ── 会话 ──
    def append_entry(self, custom_type: str, data: dict | None = None) -> None:   # noqa: N802
        """落一条扩展自定义 entry(进会话文件,**不进 LLM 上下文**)。

        这才是持久化扩展状态的正确位置:它不会污染对话上下文,但刷新/重开后还在
        (`ctx.session_manager.custom_entries(name)` 读回来)。

        回合外没有活动会话时**报错** —— 静默丢掉意味着“我存了但重启后没了”。
        """
        if self._host is None or not callable(
                getattr(self._host, "append_extension_entry", None)):
            raise RuntimeError("宿主没有提供会话写口:append_entry / appendEntry 不可用")
        self._host.append_extension_entry(custom_type, dict(data or {}), self._name)

    # ── 会话名与 label(pi 的 setSessionName / getSessionName / setLabel)──
    def set_session_name(self, name: str) -> None:
        host = getattr(self._host, "set_session_name", None)
        if not callable(host):
            raise RuntimeError("宿主没有提供会话改名接口:set_session_name / setSessionName 不可用")
        host(name)

    def get_session_name(self) -> str | None:
        host = getattr(self._host, "get_session_name", None)
        if not callable(host):
            return None
        value = host()
        return None if value is None else str(value)

    def set_label(self, entry_id: str, label: str | None) -> None:
        """给某条 entry 打/清 label(label 是用户可见的书签,pi 同义)。"""
        host = getattr(self._host, "set_entry_label", None)
        if not callable(host):
            raise RuntimeError("宿主没有提供 label 写口:set_label / setLabel 不可用")
        host(entry_id, label)

    # ── 模型与思考级别(pi 的 setModel / get-setThinkingLevel)──
    def set_model(self, model: Any) -> bool:
        """切本会话的模型(不动配置默认值)。接受 `"provider/model"` / `ModelView` /
        `{"provider": …, "id"/"model": …}`。返回是否成功(pi 同形)。"""
        host = getattr(self._host, "set_extension_model", None)
        if not callable(host):
            raise RuntimeError("宿主没有提供模型切换接口:set_model / setModel 不可用")
        return bool(host(model))

    def get_thinking_level(self) -> str:
        value = getattr(self._host, "thinking_level", None)
        return str(value) if value else "off"

    def set_thinking_level(self, level: str) -> None:
        host = getattr(self._host, "set_thinking_level", None)
        if not callable(host):
            raise RuntimeError("宿主没有提供思考级别接口:set_thinking_level / setThinkingLevel 不可用")
        host(level, source="extension")

    def send_message(self, message: str | dict, *, deliver_as: str | None = None,
                     trigger_turn: bool = False) -> None:
        """往当前对话插一条消息(**进** LLM 上下文;与 `append_entry` 相反)。

        `deliver_as` 决定**什么时候**送达(照搬 pi 的三档,pi 的驼峰写法也认):

        * `steer`(默认)—— 本轮的**下一次 LLM 调用**之前(即当前这轮工具跑完之后)。
        * `follow_up`(`followUp`)—— 等 agent **本该收工**时才送:有排队消息就不收工。
        * `next_turn`(`nextTurn`)—— 不打断本轮,留到**下一次用户输入**。

        收字符串,也收 pi 形状的字典(`{customType, content, display, details}`)。
        `trigger_turn=True` = 空闲时也开一轮(由前端在命令处理完后领取)。
        """
        custom_type = display = details = None
        if isinstance(message, str):
            text = message
        else:
            text = str(message.get("content") or "")
            custom_type = message.get("customType") or message.get("custom_type")
            display = message.get("display")
            details = message.get("details")
        self._queue_message(text, deliver_as or "steer", "sendMessage",
                            custom_type=custom_type, display=display, details=details,
                            trigger_turn=trigger_turn)

    def send_user_message(self, content: str, *, deliver_as: str | None = None,
                          trigger_turn: bool = True,
                          expand_prompt_templates: bool = False) -> None:
        """插一条**用户**消息(pi 的 `sendUserMessage`)。

        `trigger_turn` 缺省 **True**(pi 同义:空闲时会开一轮)。qi 的做法是把“要开一轮”
        记在宿主身上,由前端在本轮/命令处理完后领取(`take_turn_request()`)——
        因为“在 handler 里嵌套跑一轮”在流式架构里是另一件事(§11.9)。

        `expand_prompt_templates=True` 时交给宿主的展开器(命令 / 技能 / 提示词模板);
        宿主不支持则记一条 note 后**原样**入队(不静默假装展开了)。
        """
        text = content
        if expand_prompt_templates:
            expand = getattr(self._host, "expand_prompt_text", None)
            if callable(expand):
                text = str(expand(text))
            else:
                _note(self._host, "宿主不支持 expand_prompt_templates(...):原样入队")
        self._queue_message(text, deliver_as or "steer", "sendUserMessage",
                            trigger_turn=trigger_turn)

    def _queue_message(self, text: str, deliver_as: str, kind: str, *, custom_type: Any = None,
                       display: Any = None, details: Any = None,
                       trigger_turn: bool = False) -> None:
        body = (text or "").strip()
        if not body:
            return                                   # 空白不入队(与 message 注入一致)
        normalized = _DELIVER_AS.get(str(deliver_as))
        if normalized is None:
            raise ValueError(
                f"deliver_as 只能是 steer / follow_up / next_turn(pi 写法 followUp / "
                f"nextTurn 也认),收到 {deliver_as!r}")
        if self._host is None or not callable(
                getattr(self._host, "queue_extension_message", None)):
            raise RuntimeError("宿主没有提供消息队列:send_message / sendMessage 不可用")
        self._host.queue_extension_message(body, normalized, self._name, kind,
                                          custom_type=custom_type, display=display,
                                          details=details, trigger_turn=bool(trigger_turn))

    # ── 渲染(TUI-only,见 §5.1)──
    def register_message_renderer(self, custom_type: str, renderer: Any) -> None:
        """给某个 custom 消息类型注册渲染器(pi 的 `registerMessageRenderer`)。"""
        if self._renderers is None:
            _note(self._host, "宿主没有渲染登记处:register_message_renderer / registerMessageRenderer 未生效")
            return
        self._renderers.add_message(custom_type, renderer, source=self._name)

    def register_entry_renderer(self, custom_type: str, renderer: Any) -> None:
        """给某个 custom entry 类型注册渲染器(pi 的 `registerEntryRenderer`)。"""
        if self._renderers is None:
            _note(self._host, "宿主没有渲染登记处:register_entry_renderer / registerEntryRenderer 未生效")
            return
        self._renderers.add_entry(custom_type, renderer, source=self._name)

    def register_markdown_transformer(self, transformer: Any) -> None:
        """注册一个 markdown 渲染前的改写器(pi 的 `registerMarkdownTransformer`)。"""
        if self._renderers is None:
            _note(self._host, "宿主没有渲染登记处:register_markdown_transformer / registerMarkdownTransformer 未生效")
            return
        self._renderers.add_markdown(transformer, source=self._name)

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

    # ── 能力交接(提供方声明 + 消费方取用,P-E5 ② / E20)──
    def register_resolver(self, kind: str, resolver: ResolverFn) -> None:      # noqa: N802
        """登记“这个种类的配置怎么变成能用的东西”。

        `resolver(scope=…)` 返回 `list[Tool]`(或 awaitable);`scope` 是**作用域**(§7.4:
        qi 里最常见的作用域 = 一个 agent 目录)。同时隐含 `provides_config(kind)` ——
        能解析就等于在管这个种类,不必再声明一次。

        这是 qi-mcp 那一侧:它声明 `mcp_servers` 怎么从三层 mcp.json 变成真的工具;
        消费方(qi-agents / qi-web)只调 `resolveTools`,**不知道 MCP 存在**。
        """
        if self._capabilities is None:
            raise RuntimeError("宿主没有提供能力汇合点:register_resolver / registerResolver 不可用")
        self._capabilities.add_resolver(kind, self._name, resolver)
        self._config_kinds.add(kind)

    async def resolve_tools(self, kind: str, *, scope: Any = None) -> list[Any]:   # noqa: N802
        """问所有提供者要 `kind` 的工具,合并返回。

        **没人提供 → 返回 `[]`(优雅降级)** —— 这是 E20 选“能力交接”而不是直接 import 的
        全部理由:只装 qi-agents 时角色照跑,只是拿不到 MCP 工具,而不是装不上。
        """
        if self._capabilities is None:
            return []
        return await self._capabilities.resolve_tools(kind, scope=scope)

    # ── pi 的驼峰别名(qi 的正式名是 snake_case)──
    registerTool = register_tool
    getAllTools = get_all_tools
    getActiveTools = get_active_tools
    setActiveTools = set_active_tools
    registerCommand = register_command
    registerShortcut = register_shortcut
    getCommands = get_commands
    runAgent = run_agent
    registerFlag = register_flag
    getFlag = get_flag
    registerCliCommand = register_cli_command
    registerProvider = register_provider
    unregisterProvider = unregister_provider
    appendEntry = append_entry
    sendMessage = send_message
    sendUserMessage = send_user_message
    registerMessageRenderer = register_message_renderer
    registerEntryRenderer = register_entry_renderer
    registerMarkdownTransformer = register_markdown_transformer
    setSessionName = set_session_name
    getSessionName = get_session_name
    setLabel = set_label
    setModel = set_model
    getThinkingLevel = get_thinking_level
    setThinkingLevel = set_thinking_level
    registerResolver = register_resolver
    resolveTools = resolve_tools


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
    "ModelRegistryView",
    "ModelView",
    "RendererRegistry",
    "SessionView",
    "Tool",
    "ToolError",
    "ToolExecutor",
    "ToolOutcome",
    "exec_command",
    "register_tool",
    "tool_info",
]
