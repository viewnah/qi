"""AutoRuntime(runtime.py):manual/auto + @点名 + 每轮重新路由 + 会话 JSONL 持久化。

stream(): 一次用户输入 → 事件(CLI/TUI/HTTP 共享的 consumer 源)。
"""

from __future__ import annotations

import asyncio
import json

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import paths
from .auth import AuthStore
from .compaction import (
    DEFAULT_KEEP_RECENT_TOKENS,
    DEFAULT_RESERVE_TOKENS,
    branch_to_summarize,
    compact,
    estimate_tokens,
    messages_tokens,
    prepare_compaction,
    should_compact,
    summarize_branch,
    summary_context_message,
)
from .abort import AbortSignal
from .config import (ProviderConfig, ResolvedModel, load_config, resolve_default_model,
                     resolve_model, resolve_router_model)
from .dispatcher import Decision, Dispatcher
from .extensions import (
    CommandRegistry,
    ExtensionBus,
    ExtensionContext,
    ExtensionUi,
    FlagRegistry,
    SessionView,
)
from .llm import (
    ChatMessage,
    LiteLLMClient,
    LLMClient,
    ThinkingLLMClient,
    chat_message_from_dict,
    normalize_thinking_level,
)
from .loader import LoadError, load_all_agents, load_top_level_skills, resolve_base_prompt
from .system_prompt import build_system_prompt, default_base_prompt
from .models import AgentEvent, AgentUnit
from .registry import AgentRegistry, CapabilityRegistry, ToolCatalog, discover_extensions
from .runner import AgentRunner, RunnerSettings, RunSpec
from .session import Session, SessionStore
from .titling import suggest_title
from .settings import extension_dirs, load_settings, resolve_project_trust, session_dir
from .tools import ToolContext, register_builtin_tools


@dataclass
class RuntimeConfig:
    workdir: Path
    # 轮次与请求超时都不在这里:
    #  · 轮次是 `QiRuntime(stop_after=…)` 的谓词(对齐 pi 的 shouldStopAfterTurn);
    #  · 请求级超时/重试归 provider 层(settings.json 的 `retry.provider`,见 llm.py)。
    confidence_min: float = 0.6


# 落盘工具结果的字符上限。tools/ 内置工具已自行截断(200 行 / 50k 字符),
# 但插件工具可能不截断——落盘前再过一道上限,避免单个工具撑破会话文件。
MAX_TOOL_ENTRY_CHARS = 8000
MAX_TOOL_DETAILS_CHARS = 8000
#: 自动命名最多等这么久(秒)。等不到就把落盘挂到回调上 —— 见 `_apply_title`。
TITLE_WAIT_S = 2.0    # 与 result 同档:插件不该让会话文件无界增长

#: 扩展用 `input` 的 `handled` 接管一轮、又没给 `text` 时的占位。
#: 不给提示会让用户以为程序卡住了(`ctx.ui` 要到 P-E3 才有,现在没有别的展示通道)。
INPUT_HANDLED_PLACEHOLDER = "(这一轮由扩展处理,没有文本输出)"


def _salvaged(final_text: str, partial: str) -> str:
    """收尾用哪段文本:正常结束用 `agent_end` 的全文,被硬取消时用流式累计的半截。"""
    return final_text if final_text else partial


class QiRuntime:
    """装配好的一次性运行时(每进程一个):装载配置/插件/agents,提供 stream()。"""

    def __init__(self, cwd: Path | None = None, runtime_cfg: RuntimeConfig | None = None,
                 session_store: SessionStore | None = None,
                 llm: LLMClient | None = None,
                 router_llm: LLMClient | None = None,
                 disable_router: bool = False,
                 skills_enabled: bool = True,
                 thinking_level: str | None = None,
                 stop_after: Callable[[int], bool] | None = None,
                 extra_skill_paths: Iterable[Path] | None = None,
                 extra_extension_paths: Iterable[Path] | None = None,
                 approve_project: bool | None = None,
                 has_ui: bool = False,
                 ui_frontend: Any = None,
                 extension_flags: Iterable[str] | None = None):
        self.cwd = Path(cwd) if cwd else Path.cwd()
        # 旧版扁平布局 → ~/.qi/agent/(幂等;显式设了 QI_AGENT_HOME 时不动)
        paths.ensure_layout()
        self.cfg, self.config_files = load_config(self.cwd)
        self.settings, self.settings_files = load_settings(self.cwd)
        self._has_ui = has_ui          # `ctx.has_ui`:交互式前端=True,`-p`/脚本=False
        #: 同一 runtime 对同一会话只发一次 `session_start`(见 `start_session`)
        self._started_sessions: set[str] = set()
        self.runtime_cfg = runtime_cfg or RuntimeConfig(workdir=self.cwd)
        # 轮次政策由嵌入方给(对齐 pi 的 shouldStopAfterTurn):None = 不限
        # (qi 自己不设上限 —— 钩子留着但生产代码不传,同 pi 定义了却不实现它)
        self.stop_after = stop_after
        self.workdir = self.runtime_cfg.workdir
        # 基座:项目 .qi/SYSTEM.md > ~/.qi/agent/SYSTEM.md;都没有则空串
        # (空串 = 用 system_prompt.py 里的代码内默认基座,见 docs/system-prompt.md)
        self.base_prompt, self.base_prompt_source = resolve_base_prompt(self.cwd)
        if session_store is not None:
            self.sessions = session_store
        else:
            # sessionDir(settings.json)覆盖默认会话目录;对齐 pi 的优先级链
            override_dir = session_dir(self.settings, self.cwd)
            self.sessions = SessionStore(root=override_dir) if override_dir else SessionStore()

        self.catalog = ToolCatalog()
        register_builtin_tools(self.catalog)
        self.capabilities = CapabilityRegistry()
        #: 启动提示 + handler 异常的汇总通道(runtime 不做 IO,前端自己决定怎么展示)。
        #: 必须在 `ExtensionBus` **之前**建:总线拿它当 handler 异常的去处。
        self.notes: list[str] = []
        # 事件总线:扩展在 `register(api)` 里 `api.on(...)` 订阅的东西都落在这里。
        # 零扩展时 `is_empty` = True,宿主可以据此跳过整条派发路径。
        # 同一个对象也挂着**扩展之间**的消息频道(`api.events`)。
        self.bus = ExtensionBus(notes=self.notes)
        #: 扩展的工具集覆盖(`api.setActiveTools`)。None = 按 agent 的 `tools` 解析
        self._tool_override: set[str] | None = None
        # 信任门控(P-E1 / E16):未信任 → **不扫**项目级扩展(扩展是仓库控制的任意代码)。
        # 提示不在这里打印(runtime 不做 IO):攒进 `self.notes`,由前端决定怎么展示。
        #: 当前回合绑定的会话(`api.appendEntry` / `ctx.session_manager` 靠它读/写)。
        #: 由 `stream()` 的 wrapper 设置并在 `finally` 里清掉。
        self._active_session: Session | None = None
        #: 扩展主动发的消息(`api.sendMessage` / `sendUserMessage`),按**送达时机**分桶。
        #: 元素是 `(文本, 来源扩展, 调用方式)`。steer / follow_up 由 runner 在回合内排空;
        #: next_turn 留到下一次用户输入(见 `_drain_messages` 与 `_stream_inner` 开头)。
        self._pending_messages: dict[str, list[tuple[str, str, str]]] = {
            "steer": [], "follow_up": [], "next_turn": []}
        self.project_trusted, self.trust_reason = resolve_project_trust(
            self.settings, approve=approve_project, has_ui=has_ui)
        if not self.project_trusted and paths.project_extensions_dir(self.cwd).is_dir():
            self.notes.append(
                f"未信任项目({self.trust_reason}):`.qi/extensions/` 未加载;用 `qi -a` 信任")
        legacy_ext = paths.legacy_project_extensions_dir(self.cwd)
        if legacy_ext is not None:
            self.notes.append(
                f"检测到旧目录 `{legacy_ext.relative_to(self.cwd)}` —— 已改名为 "
                f"`extensions/`,请手动改名(不会自动改仓库内容)")
        # `ctx.ui`:前端(TUI)在构造时把自己的实现递进来;没有前端时它按调用方给的
        # default 回答,所以 `-p` 不会卡在这里。notes 共用同一个列表 —— 无界面时
        # `ui.notify` 落进来就自动变成启动提示。
        #
        # 为什么走构造参数而不是一个 `set_ui_frontend()`:后者多一个“别忘了调”的次序隐患,
        # 而且每次给假运行时加方法都会撞一遍。
        # (web 不行 —— 每个浏览器连接是一个不同的前端,那边要按回合解析,见 §5.1。)
        self.ui = ExtensionUi(frontend=ui_frontend, notes=self.notes)
        # 扩展命令/快捷键的汇合点(TUI 按它分发 `/cmd` 与按键)
        self.commands = CommandRegistry()
        # CLI 旗标(扩展用 `registerFlag` 声明;值走 core 的 `--ext name=value`)
        self.flags = FlagRegistry()
        #: `--ext` 里**不能用**的那些(名字打错 / 值非法)。与 `notes` 分开:
        #: 这个是致命的(用户打错了命令行),CLI 据此退出码 2;notes 只是告知。
        self.flag_errors: list[str] = []
        # 附加扩展目录先算好:嵌在 kwargs 里会让这次调用看不出“传了哪几样”
        extra_extensions = extension_dirs(
            self.cwd, trusted=self.project_trusted,
            extra=list(extra_extension_paths or ()))
        self.extensions = discover_extensions(
            self.catalog, self.capabilities, self.cwd,
            bus=self.bus,
            host=self,
            commands=self.commands,
            flags=self.flags,
            on_warning=self.notes.append,
            extra_dirs=extra_extensions,
            project_trusted=self.project_trusted)
        # `--ext` 的值要在**扩展声明之后**才能解析(名字与类型都在那边),所以放在这里
        for pair in extension_flags or ():
            problem = self.flags.provide(str(pair))
            if problem:
                self.flag_errors.append(problem)
        self.notes.extend(self.flags.problems)

        # 顶层技能(~/ .agents > qi 全局 > .agents 项目 > qi 项目 > settings),agent 自带者优先
        self.top_skills = load_top_level_skills(
            self.cwd, self.settings,
            list(extra_skill_paths or ()),
            enabled=skills_enabled and self.settings.skillsEnabled,
        )
        units = load_all_agents(self.cwd, self.catalog.names,
                                has_data_source_provider=self.capabilities.has_provider("data_sources"),
                                ds_types=self.capabilities.types("data_sources"),
                                extra_skills=self.top_skills)
        self.registry = AgentRegistry()
        self.registry.register_all(units)

        auth = AuthStore()
        #: 换模型时要重建客户端,所以凭证存储要留在身上(`set_model` 用)
        self._auth = auth
        default: ResolvedModel = resolve_default_model(self.cfg, self.cwd)
        # 思考级别:显式传参(CLI --thinking)> settings.defaultThinkingLevel > off
        level = thinking_level if thinking_level is not None else self.settings.defaultThinkingLevel
        self.thinking_level = normalize_thinking_level(level)
        self.llm_exec = llm or LiteLLMClient(default, auth, thinking_level=self.thinking_level,
                                            retry=self.settings.retry)
        if disable_router:
            self.router_llm = None
        else:
            router_spec = resolve_router_model(self.cfg, self.cwd)
            self.router_llm = router_llm if router_llm is not None else LiteLLMClient(
                router_spec, auth, retry=self.settings.retry)
        self.dispatcher = Dispatcher(self.registry, self.router_llm,
                                     confidence_min=self.runtime_cfg.confidence_min)

    # ── 工具上下文(每个会话独立) ──
    def _tool_ctx(self, agent_name: str, unit) -> ToolContext:
        return ToolContext(agent_name=agent_name, workdir=self.workdir,
                           data_sources=unit.data_sources,
                           ask=self._ask,
                           shell_path=self.settings.shellPath,
                           ui=self.ui)

    # ── 受管子运行(扩展的 `runAgent` / E12)──
    def _client_for(self, model_ref: str | None) -> Any:
        """子运行用的客户端:给了 `"provider/model"` 就**另建一个**,不给就用当前的。

        关键点:另建时**不碰** `self.llm_exec` —— 子运行换模型不应改变父会话的模型。
        """
        if not model_ref:
            return self.llm_exec
        provider, _, model = str(model_ref).partition("/")
        if not model:
            raise ValueError(f'模型要写成 "provider/model",收到 {model_ref!r}')
        return LiteLLMClient(resolve_model(self.cfg, provider, model), self._auth,
                            thinking_level=self.thinking_level, retry=self.settings.retry)

    async def run_agent(self, spec: Any, task: str, *, abort: AbortSignal | None = None,
                        on_event: Any = None) -> str:
        """在宿主内起一个**受管的子运行**(E12):独立上下文、自己的工具集与模型。

        **与 `stream()` 的区别**(为什么不直接嵌套 stream):子运行**不碰会话** ——
        不落盘、不派发、不改 active_agent。它是“借一次工具循环”,不是“再来一轮对话”。
        所以它不会污染父会话的上下文,也不会在历史上多出几条看不懂的消息。

        **但扩展事件照常派发**(用同一个总线与 ctx):所以权限闸门(`tool_call`)
        对子运行一样生效 —— 这是“进程内”相对“子进程”的一个真实好处(子进程里宿主看不见)。

        `spec` 是 dict:`system_prompt`(必填)/ `tools`(缺省**继承父**的当前集合,
        而不是 catalog 全部 —— 给子运行比父多出权限是提权)/ `model`(缺省继承父)/ `name`。
        递归深度由**扩展自己**管(E12):宿主不猜你允许多深。
        """
        data = spec if isinstance(spec, dict) else {
            "system_prompt": getattr(spec, "prompt", ""),
            "tools": getattr(spec, "tools", None),
            "name": getattr(spec, "name", None)}
        prompt = str(data.get("system_prompt") or "").strip()
        if not prompt:
            raise ValueError("runAgent 需要 system_prompt(子运行要有自己的提示词)")
        tools = data.get("tools")
        if tools is None:
            tools = self.tool_names()          # 继承父的当前集合(不是 catalog 全部)
        name = str(data.get("name") or "subagent")
        run_spec = RunSpec(name=name, prompt=prompt, tools=[str(t) for t in tools])
        ctx = ToolContext(agent_name=name, workdir=self.workdir, ask=self._ask,
                          shell_path=self.settings.shellPath, ui=self.ui,
                          abort=abort)
        runner = AgentRunner(run_spec, self.catalog, self._client_for(data.get("model")),
                            tool_ctx=ctx,
                            bus=self.bus,
                            extension_ctx=lambda signal: self.extension_ctx(signal),
                            report=self.notes.append)
        final = ""
        async for event in runner.run(task, abort=abort):
            if event.kind == "agent_end":
                final = event.text or final
            if on_event is not None:
                on_event(event)
        return final

    async def _ask(self, question: str) -> str | None:
        """`clarify` 工具的交互入口。

        有 UI 前端时走 `ctx.ui.input` —— 这修掉一个旧缺陷:`_ask` 以前无条件返回 None,
        所以**内置的 clarify 在 TUI 里也从没问过人**(它一直只会说“无人可问”)。
        没前端(无头/脚本)仍返回 None,保持“不吃 stdin”的行为。
        """
        if not self.ui.has_frontend:
            return None
        return await self.ui.input(question, title="澄清")

    # ── 扩展上下文与事件 ──
    def _model_label(self) -> str | None:
        """`"provider/model"`(与 pi 的 `ctx.model` 同形);拿不到 spec 就 None。"""
        spec = getattr(self.llm_exec, "spec", None)
        provider, model = getattr(spec, "provider", None), getattr(spec, "model", None)
        return f"{provider}/{model}" if provider and model else None

    def extension_ctx(self, signal: AbortSignal | None = None,
                      session: Session | None = None) -> ExtensionContext:
        """构造 handler 用的 `ctx`(§3.3 的最小集)。

        **不是**缓存单例:每回合的信号不同(`signal`),而 ctx 冻结这些值。

        `session` 缺省用回合绑定的那个(`_active_session`)—— 所以 runner 里的 handler
        拿到的 `ctx.session_manager` 就是当前会话;回合外(如 TUI 命令)拿不到时
        `available` 为 False、写口报错。
        """
        return ExtensionContext(
            cwd=self.cwd,
            model=self._model_label(),
            thinking_level=self.thinking_level,
            signal=signal,
            has_ui=self._has_ui,
            project_trusted=self.project_trusted,
            notes=self.notes,
            ui=self.ui,
            session_manager=SessionView(session if session is not None
                                        else self._active_session),
        )

    # ── provider(扩展的 `registerProvider`)──
    def register_provider(self, name: str, config: dict, source: str) -> None:
        """动态注册/覆盖一个 provider。**只改内存里的 cfg,不写 `models.json`。**

        写盘会让“跑一次带代理的扩展”永久改变用户的模型配置 —— 而那是个静默的副作用。
        所以扩展注册的 provider 只活在这个进程里(pi 允许扩展持久化目录元数据、
        带 generation 校验 —— 那是另一套机制,qi 没做)。
        """
        if name in self.cfg.providers:
            self.notes.append(f"扩展 {source} 覆盖了已有的 provider {name}")
        try:
            self.cfg.providers[name] = ProviderConfig.model_validate(config)
        except Exception as exc:  # noqa: BLE001 pydantic 校验错
            raise ValueError(f"provider {name} 配置不合法: {exc}") from exc

    def append_extension_entry(self, custom_type: str, data: dict, source: str) -> None:
        """扩展自定义 entry 的**唯一写口**(`api.appendEntry` 走这里)。

        章由宿主盖:`source`(哪个扩展写的)、`agent`(当时哪个角色在跑)。扩展自己拼 entry
        的话迟早会出现两种形状,而回放/诊断都得同时认两种。

        没有活动会话时**报错** —— 回合外的写往往是“想存但存错地方”的第一步。
        """
        session = self._active_session
        if session is None:
            raise RuntimeError("没有活动会话:appendEntry 只能在回合内调用")
        self.sessions.append(session, {
            "type": "custom", "custom_type": custom_type,
            "source": source, "agent": self._active_agent(session),
            "data": data})

    def queue_extension_message(self, text: str, deliver_as: str, source: str,
                                kind: str = "sendMessage") -> None:
        """扩展消息的唯一入队口(`api.sendMessage` / `sendUserMessage` 走这里)。

        **不在这里落盘**:送达时机在 runner 手里(每次 LLM 调用前 / 本该收工时 / 下次输入),
        而“什么时候进对话”与“什么时候进文件”必须是同一个时刻 —— 先落盘会让历史里出现一条
        还没送达的消息。落盘在 `_drain_messages` 里做,与排空同时。
        """
        bucket = self._pending_messages.get(deliver_as)
        if bucket is None:                       # `api` 侧已经挡过,这里只是双保险
            raise ValueError(f"未知的 deliver_as: {deliver_as!r}")
        bucket.append((text, source, kind))

    def _drain_messages(self, deliver_as: str) -> list[str]:
        """排空某一档的待发消息,**同时落盘**。

        落盘放这里而不是 runner 里:runner 不认识会话(它的 `msgs` 是本地上下文表),
        而“谁负责落盘”只有一个答案才是稳的。

        回合外(`_active_session` 为空)只排空不落盘 —— 那种情况只可能是 next_turn
        在输入之前被取走,而它本来就还没进对话。
        """
        bucket = self._pending_messages.get(deliver_as) or []
        if not bucket:
            return []
        texts = [text for text, _source, _kind in bucket]
        session = self._active_session
        if session is not None:
            for text, source, kind in bucket:
                self.sessions.append(session, {
                    "type": "message", "role": "user", "content": text,
                    "agent_id": self._active_agent(session),
                    "injected_by": kind, "deliver_as": deliver_as, "source": source})
        bucket.clear()
        return texts

    async def start_session(self, session: Session, reason: str = "startup") -> None:
        """告知扩展"会话已绑定"(pi 的 `session_start { reason }`)。

        **幂等**:同一 runtime 对同一会话只派发一次。前端可能会在多个时机绑定同一会话,
        重复派发会把扩展的"开一次资源"变成开 N 次 —— 宿主自己记账,扩展不必防重。

        handler 抛异常不往外传(§4 规则 3):记进 `notes`,会话照跑。扩展坏在启动时,
        用户至少能在界面上看到一行字,而不是"啥都没发生"。
        """
        if session.id in self._started_sessions:
            return
        self._started_sessions.add(session.id)
        if self.bus.is_empty:
            return
        event = await self.bus.emit(
            "session_start",
            {"reason": reason, "session": session.id, "cwd": str(self.cwd)},
            ctx=self.extension_ctx())
        for source, exc in event.errors:
            self.notes.append(f"扩展 {source} 的 session_start 处理失败: {exc}")

    # ── 工具集(扩展 `setActiveTools` / `getActiveTools` 的后端)──
    def tool_names(self, unit: AgentUnit | None = None) -> list[str]:
        """本回合实际启用的工具名。

        覆盖(`setActiveTools`)优先于 agent 的 `tools` —— plan-mode 那种“从此只读”
        需要它**跳角色生效**。没有覆盖时按 agent 解析;对 `tools: ["*"]` 的 agent
        是**当场重算**,所以运行时新注册的工具下一轮就能调(pi 的 "no reload needed")。

        覆盖里已经不在 catalog 的名字会被滤掉(`setActiveTools` 允许先写名字、
        工具随后才注册)。
        """
        if self._tool_override is not None:
            return [n for n in sorted(self._tool_override) if n in self.catalog.names]
        if unit is None:
            return sorted(self.catalog.names)
        return unit.config.resolves_tools(self.catalog.names)

    def set_tool_names(self, names: Iterable[str]) -> None:
        """覆盖工具集(扩展走的入口是 `api.setActiveTools`),对**后续回合**生效。"""
        self._tool_override = set(names)

    # ── 模型与思考级别(扩展的 `setModel` / `setThinkingLevel` 走这里)──
    def set_model(self, provider: str, model: str, *, source: str = "set") -> ResolvedModel:
        """运行期换模型(**下一回合**生效)。返回解析后的模型(前端要拿它刷新显示)。

        **唯一的换模型入口** —— UI(TUI 的 `/model`、Ctrl+P)与扩展(`api.setModel`)共用,
        所以 `model_select` 只从这一处发:两个入口各自发事件,迟早一个漏发或多发。

        重建客户端时**必须带上当前 `thinking_level` 与 `retry`**:否则切模型会默默丢掉它们
        (footer 显示 high、请求里却没有 —— 这类表现最难查)。
        """
        resolved = resolve_model(self.cfg, provider, model)
        previous = getattr(getattr(self, "llm_exec", None), "spec", None)
        self.llm_exec = LiteLLMClient(resolved, self._auth,
                                     thinking_level=self.thinking_level,
                                     retry=self.settings.retry)
        self._emit_notice("model_select", {
            "model": f"{resolved.provider}/{resolved.model}",
            "previous": (f"{previous.provider}/{previous.model}" if previous else None),
            "source": source})
        return resolved

    def set_thinking_level(self, level: str, *, source: str = "set") -> str:
        """改思考级别(下一回合生效)。返回归一后的级别。

        `source` 只进事件 payload("set" = 显式设、"cycle" = shift+tab 轮转)。
        """
        previous = self.thinking_level
        self.thinking_level = normalize_thinking_level(level)
        client = getattr(self, "llm_exec", None)
        # 可选能力:测试替身 / 第三方实现可能连 `thinking_level` 都没有
        if client is not None and isinstance(client, ThinkingLLMClient):
            client.thinking_level = self.thinking_level
        self._emit_notice("thinking_level_select", {
            "level": self.thinking_level, "previous_level": previous, "source": source})
        return self.thinking_level

    def set_session_title(self, session: Session, title: str, *, source: str = "auto") -> None:
        """改会话显示名(落盘 + 发 `session_info_changed`)。

        标题有两条来源:自动命名(`source="auto"`)与用户 `/name`(`source="user"`)。
        集中到这一处是为了**事件只发一次**,而且“空标题不覆盖”那条规则只写一遍。
        """
        cleaned = (title or "").strip()
        if not cleaned:
            return
        try:
            self.sessions.set_title(session, cleaned)
        except OSError:
            # 落盘失败不该把这一轮搞垮(与本仓其它“不因此中断主流程”的 except 同形)
            return
        self._emit_notice("session_info_changed", {"name": cleaned, "source": source})

    def _emit_notice(self, event: str, payload: dict) -> None:
        """发一个**通知型**扩展事件(同步入口,不阻塞调用方)。

        `bus.emit` 是 async,而 `set_model` 这类调用点在 UI 的同步路径上 —— 用一个
        后台任务跑完全够(通知型事件的返回值本来就被忽略),避免把 UI 路径改成 async。
        没订阅时**不建任务**(零扩展零开销)。
        """
        if not self.bus.has(event):
            return
        payload = dict(payload)
        payload["session"] = self._active_session.id if self._active_session else None

        async def _run() -> None:
            result = await self.bus.emit(event, payload,
                                         ctx=self.extension_ctx(session=self._active_session))
            for src, exc in result.errors:
                self.notes.append(f"扩展 {src} 的 {event} 处理失败: {exc}")

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            # 没在事件循环里(如脚本直接构造 runtime):通知型事件丢掉不影响主流程
            return

    # ── 主流程 ──
    def _active_agent(self, session: Session) -> str | None:
        # 只用**当前分支**:别的分支上的 state 不能影响这一条
        for e in reversed(session.branch()):
            if e.get("type") == "state" and e.get("key") == "active_agent":
                return e.get("value")
        return None

    def _set_active(self, session: Session, agent: str | None) -> None:
        self.sessions.append(session, {"type": "state", "key": "active_agent", "value": agent})

    def _opening_shown(self, session: Session, agent: str) -> bool:
        """该 agent 的开场白是否已在本会话展示过。

        必须扫描**当前分支的全部** entry:旧实现只看 `entries[-1]`,而首轮末尾已是 assistant
        消息,.get("opening_shown") 恒为 None → 开场白每轮都重复显示。
        按 agent 记(非按会话记),所以切换到另一个 agent 时会展示它自己的开场白。
        """
        return any(
            e.get("type") == "custom" and e.get("custom_type") == "opening_shown"
            and e.get("agent") == agent
            for e in session.branch()
        )

    def _history(self, session: Session) -> list:
        """当前分支的上下文 = 最近一次压缩摘要 + 压缩点之后的消息(+ 分支摘要)。

        pi 的语义:压缩后模型看到的是 `system | summary | firstKeptEntryId 起头的消息`。
        没压过时退回旧的「最近 40 条」简易窗口 —— 那时压缩还没接管窗口。
        """
        branch = session.branch()
        last_compaction = next((e for e in reversed(branch)
                                if e.get("type") == "compaction"), None)
        out: list[ChatMessage] = []
        keep_from = 0
        if last_compaction is not None:
            out.append(summary_context_message(str(last_compaction.get("summary") or "")))
            first_kept = str(last_compaction.get("firstKeptEntryId") or "")
            keep_from = next((i for i, x in enumerate(branch)
                              if str(x.get("id")) == first_kept), len(branch))
        for e in branch[keep_from:]:
            kind = e.get("type")
            if kind == "branch_summary":
                out.append(summary_context_message(str(e.get("summary") or ""), kind="branch"))
            elif kind == "message" and e.get("role") != "system":
                d = dict(e)
                d.pop("type", None); d.pop("ts", None); d.pop("agent_id", None)
                out.append(chat_message_from_dict(d))
        return out if last_compaction is not None else out[-40:]

    # ── 上下文压缩(pi 的 /compact + 自动压缩)──
    def _compaction_options(self) -> tuple[bool, int, int]:
        """读取 `settings.compaction`:`(enabled, reserveTokens, keepRecentTokens)`。"""
        raw = self.settings.compaction or {}
        enabled = bool(raw.get("enabled", True))
        reserve = raw.get("reserveTokens")
        keep = raw.get("keepRecentTokens")
        return (enabled,
                reserve if isinstance(reserve, int) and reserve > 0 else DEFAULT_RESERVE_TOKENS,
                keep if isinstance(keep, int) and keep > 0 else DEFAULT_KEEP_RECENT_TOKENS)

    def _context_window(self) -> int:
        spec = getattr(self.llm_exec, "spec", None)
        window = getattr(spec, "context_window", 0)
        return window if isinstance(window, int) else 0

    async def _maybe_auto_compact(self, session: Session):
        """开新一回合前检查上下文体积:超了就先自动压一次(pi 的 auto-compaction)。

        预算口径:分支上会进上下文的内容 + 基座提示词;阈值 = `contextWindow - reserveTokens`。
        压缩失败不应该把整轮卡死 —— 只报错,继续跑。
        """
        enabled, reserve, _keep = self._compaction_options()
        window = self._context_window()
        if not enabled or window <= 0:
            return
        # 用**重建后的上下文**估算,不是原始 entry 之和 —— 压缩过的内容不该再计入
        base = self.base_prompt or default_base_prompt()   # 无自定义基座时按代码内默认计入
        tokens = messages_tokens(self._history(session)) + estimate_tokens(base)
        if not should_compact(tokens, window, enabled=enabled, reserve_tokens=reserve):
            return
        yield AgentEvent(kind="compaction_start", text="正在自动压缩上下文…",
                         data={"auto": True, "tokens": tokens, "contextWindow": window})
        try:
            entry = await self.compact_session(session)
        except Exception as exc:  # noqa: BLE001 压缩失败不能拖垮这一轮
            yield AgentEvent(kind="error", text=f"自动压缩失败(继续本轮): {exc}")
            return
        if entry is not None:
            yield AgentEvent(kind="compaction_end", text=str(entry.get("summary") or ""),
                             data={"auto": True, "tokensBefore": entry.get("tokensBefore"),
                                   "entry": entry})

    async def compact_session(self, session: Session,
                              instructions: str | None = None) -> dict | None:
        """执行一次压缩并落盘;没什么可压时返回 None(调用方据此提示用户)。"""
        _enabled, _reserve, keep = self._compaction_options()
        prep = prepare_compaction(session.branch(), keep_recent_tokens=keep)
        if prep is None:
            return None
        entry = await compact(self.llm_exec, prep, instructions=instructions)
        self.sessions.append(session, entry)
        return entry

    async def summarize_branch_for_jump(self, session: Session, source_branch: list[dict],
                                        from_id: str | None, target_id: str | None
                                        ) -> dict | None:
        """`/tree` 跳到别的分支时,把「被放弃的那段」压成摘要挂到新位置。

        `source_branch` 必须由调用方在**移动 position 之前**取好:`/tree` 一移动 current,
        再调 `session.branch()` 拿到的就是目标分支了(摘要会静默变成空)。
        """
        target_branch = session.branch(target_id)
        entries = branch_to_summarize(source_branch, from_id, target_branch)
        if not entries:
            return None
        summary, usage = await summarize_branch(self.llm_exec, entries)
        if not summary:
            return None
        entry = {"type": "branch_summary", "summary": summary,
                 "fromId": from_id, "usage": usage}
        # 挂到跳过去的位置下(所以新 leaf 就是这条摘要)
        self.sessions.set_position(session, target_id)
        self.sessions.append(session, entry)
        return entry

    async def _emit_input(self, text: str, session: Session, source: str,
                          abort: AbortSignal | None) -> tuple[str, dict | None, str | None]:
        """派发 `input`(pi 的 input 事件):可拦截 / 改写 / 吞掉。

        返回 `(最终文本, handled 裁决, 裁决者)`;裁决非空表示扩展接管了这一轮。

        **两个键,两种含义**(不能合):
        * `text` —— **改写后的用户输入**(`transform` 用);
        * `reply` —— **扩展给用户的答复**(`handled` 用)。

        分开不是洁癖:裁决的字段会被 patch 进 payload,如果两者同名,扩展的答复就会被
        当成用户输入落盘 —— 测试里真的出现过用户消息变成 "pong" 的那种结果。
        pi 不需要区分是因为它的 `handled` 不带文本(靠 `ctx.ui` 自己展示);
        qi 的 `ctx.ui` 要到 P-E3 才有,所以这里要一个 `reply`。
        """
        if not self.bus.has("input"):
            return text, None, None
        result = await self.bus.emit_until(
            "input",
            {"text": text, "source": source, "session": session.id},
            ctx=self.extension_ctx(abort),
            stop_values={"action": ("handled",)})
        for src, exc in result.errors:
            self.notes.append(f"扩展 {src} 的 input 处理失败: {exc}")
        final = result.payload.get("text")
        return (str(final) if final is not None else text), result.result, result.stopped_by

    async def _handled_turn(self, session: Session, verdict: dict, source: str | None):
        """扩展接管了这一轮(pi 的 `action: handled`):**不跑 agent**。

        仍然**落盘助手侧**—— 否则回放里这一轮凭空消失,而“直播看得见、刷新就没了”
        是本仓反复出现过的那类不一致。用户消息由 `stream()` 先落过了(那是“用户确实
        说了这句话”的记录,与“谁处理了它”无关)。

        没给 `reply` 时给一句占位:`ctx.ui` 要到 P-E3 才有,现在扩展没有别的展示
        通道,静默吞掉会让用户以为程序卡了。
        """
        agent = self._active_agent(session) or "extension"
        reply = str(verdict.get("reply") or "").strip()
        self._persist_final(session, agent, reply or INPUT_HANDLED_PLACEHOLDER)
        yield AgentEvent(kind="text", agent=agent, text=reply or INPUT_HANDLED_PLACEHOLDER)
        yield AgentEvent(kind="agent_end", agent=agent, text=reply,
                         data={"messages": [], "handled_by": source,
                               "usage": {"turns": 0, "context_tokens": 0}})

    async def _before_agent_start(self, unit: AgentUnit, text: str,
                                  abort: AbortSignal | None) -> tuple[str, str | None]:
        """派发 `before_agent_start`:可换 `system_prompt`(链式)、可注入一条消息。

        返回 `(system_prompt, 注入的消息)` —— 提示词**总是**有值(没扩展时自己建)。

        注入的消息(pi 的 `{message: …}`)收字符串或 `{"content": …}`。按 pi 的语义它是
        **持久**的(落盘,下一轮仍在上下文里),所以落盘由调用方(`_stream_inner`)做,
        文本再交给 runner 插到本轮 user 消息之后。
        """
        tools = self.catalog.resolve(self.tool_names(unit))
        built = build_system_prompt(unit, self.base_prompt, tools=tools, cwd=self.workdir)
        if not self.bus.has("before_agent_start"):
            return built, None
        result = await self.bus.emit(
            "before_agent_start",
            {"prompt": text, "system_prompt": built, "agent": unit.name},
            ctx=self.extension_ctx(abort))
        for src, exc in result.errors:
            self.notes.append(f"扩展 {src} 的 before_agent_start 处理失败: {exc}")
        changed = result.payload.get("system_prompt")
        raw = result.payload.get("message")
        injected: str | None = None
        if isinstance(raw, str):
            injected = raw.strip() or None
        elif isinstance(raw, dict):
            injected = str(raw.get("content") or "").strip() or None
        return (str(changed) if changed else built), injected

    async def stream(self, text: str, session: Session, agent_override: str | None = None,
                     abort: AbortSignal | None = None, source: str = "interactive"):
        """处理一轮用户输入,产出事件。agent_override=manual(--agent / /agent)。

        `source` 进 `input` 事件的 payload(交互式 / 无头 / rpc)—— 扩展据此决定
        “要不要弹问”。目前调用方都用默认值:精确标签等真正需要它的人来传。

        `abort` 透传给 runner(协作式中断,见 abort.py)。即使被**硬取消**
        (`CancelledError`:web 的客户端 abort、TUI 的强制终止),本方法也会先把已有文本
        落盘再往上抛 —— 否则用户看过的半截回答在会话文件里消失(直播与回放不一致)。
        """
        # 回合内把会话绑在 runtime 上:扩展的 `appendEntry` / `ctx.session_manager` 靠它。
        # 用 wrapper + `finally` 而不是在主体里包 `try`:主体有十几个 `return`
        # (handled 提前结束、无 agent、错误返回…),逐个清一定会漏 —— 而漏掉的后果是
        # “回合外的写落到上一个会话里”,那是很难查的一类串状态。
        self._active_session = session
        try:
            async for event in self._stream_inner(text, session, agent_override, abort, source):
                yield event
        finally:
            self._active_session = None

    async def _stream_inner(self, text: str, session: Session,
                            agent_override: str | None, abort: AbortSignal | None,
                            source: str):
        # 旧会话首次被使用时回填 cwd(只写一次);新会话在 create(cwd=…) 时已带
        self.sessions.ensure_cwd(session, self.cwd)
        # `next_turn` 档在这里送达:它说的就是“下一次用户输入时再说”,而这就是那个时刻。
        # 排空 + 落盘之后,下面的 `_history()` 会自然把它算进上下文。
        self._drain_messages("next_turn")
        # `input` 在**自动压缩之前**:它处理的是“用户说了什么”,与上下文体积无关;而且
        # 改写后的文本要影响下游全部(标题、分派、历史)。
        text, handled, handled_by = await self._emit_input(text, session, source, abort)
        if handled is not None:
            self.sessions.append(session, {"type": "message", "role": "user",
                                           "content": text,
                                           "agent_id": self._active_agent(session) or "extension"})
            async for event in self._handled_turn(session, handled, handled_by):
                yield event
            return
        # 自动命名:**并行**跑(不拖首字延迟),回合末尾才套用(见 `_apply_title`)。
        # 触发条件是"这个会话还没有标题" —— 新建的与本功能上线前建的老会话都算。
        title_task: asyncio.Task[str | None] | None = None
        if not session.title.strip():
            # 输入取会话原本的第一句话,不是这一轮说的话:老会话续聊时,
            # "接着再补个测试"会把一个讲仓库结构的会话命名成"补充测试"。
            # (本轮的用户消息此刻还没落盘 —— 它在下面 append,所以新会话会回落到 text。)
            title_task = asyncio.create_task(
                suggest_title(self.llm_exec, self.first_user_text(session) or text))
        async for event in self._maybe_auto_compact(session):
            yield event
        active = self._active_agent(session)
        decision: Decision | None = None

        if agent_override and self.registry.get(agent_override):
            decision = Decision(agent=agent_override, confidence=1.0, source="manual",
                                reasoning="manual 指定")
        else:
            decision = self.dispatcher.rule_decide(text, active)
            if decision is None:
                decision = await self.dispatcher.decide_semantic(text, active)

        if decision is None:
            decision = Decision(agent=None, confidence=0.0, source="fallback",
                                reasoning="无匹配")
        unit = self.registry.get(decision.agent) if decision.agent else None
        # 展示用名:display_name 优先(如内置 general 显示为 "qi"),便于与 agents list 一致
        shown = (unit.config.display_name.strip() if unit and unit.config.display_name
                 else (decision.agent or "?"))
        yield AgentEvent(kind="dispatch", agent=decision.agent,
                         text=f"{shown} ({decision.source}, {decision.confidence:.2f})",
                         data={"confidence": decision.confidence, "source": decision.source,
                               "agent": decision.agent, "display_name": shown,
                               "reasoning": decision.reasoning})
        self.sessions.append(session, {"type": "dispatch", "agent": decision.agent,
                                       "display_name": (shown if decision.agent else None),
                                       "confidence": decision.confidence,
                                       "source": decision.source,
                                       "reasoning": decision.reasoning})

        if decision.agent is None:
            yield AgentEvent(kind="error", text="没有合适的 agent 且无 general,请装一个 general 或用 @ 点名")
            return
        if unit is None:
            yield AgentEvent(kind="error", text=f"agent {decision.agent} 不存在")
            return

        if active != unit.name:
            self._set_active(session, unit.name)

        # opening:每个 agent 在本会话内只展示一次(前端负责渲染)
        if unit.config.opening and unit.config.opening.message \
                and not self._opening_shown(session, unit.name):
            self.sessions.append(session, {"type": "custom", "custom_type": "opening_shown",
                                           "agent": unit.name})
            yield AgentEvent(kind="opening", agent=unit.name,
                             text=unit.config.opening.message,
                             data={"suggestions": unit.config.opening.suggestions})

        # 顺序要紧:先取上下文(不含本轮),再把 user 消息立即落盘。
        # 旧实现把 user 写在回合**结束后**,于是运行中刷新/断线就看不到自己说了什么;
        # 而若先落盘再取 history,本轮输入会进上下文两次(两条同样的 user)。
        history = self._history(session)
        self.sessions.append(session, {"type": "message", "role": "user",
                                       "content": text, "agent_id": unit.name})
        # `before_agent_start` 在 user 落盘**之后**才 fire(pi 同款):扩展从
        # `ctx.session_manager` 里就看得到本轮那句话,而且注入的 message 天然排在它后面。
        system_prompt, injected = await self._before_agent_start(unit, text, abort)
        if injected:            # 注入的消息**落盘**(下一轮仍在上下文里,pi 说的 persistent message),
            # 同时交给 runner 插到本轮 user 之后 —— 取 history 时它还不存在,所以不会重复。
            self.sessions.append(session, {"type": "message", "role": "user",
                                           "content": injected, "agent_id": unit.name,
                                           "injected_by": "before_agent_start"})
        runner = AgentRunner(
            # 运行单元:core 只认识“提示词 + 工具名单 + 名字”(见 `RunSpec`)
            RunSpec(name=unit.name, prompt=system_prompt, tools=self.tool_names(unit)),
            self.catalog, self.llm_exec,
            RunnerSettings(stop_after=self.stop_after),
            tool_ctx=self._tool_ctx(unit.name, unit),
            injected_messages=[injected] if injected else None,
            drain_injections=self._drain_messages,
            bus=self.bus,
            extension_ctx=lambda signal: self.extension_ctx(signal, session),
            report=self.notes.append)
        final_text = ""
        partial = ""
        # 正常结束才有(RUN_FINISHED 的 usage);硬取消那条路径拿不到。
        end_usage: dict = {}
        pending_tool: dict | None = None
        try:
            async for event in runner.run(text, history, abort=abort):
                if event.kind == "agent_end":
                    final_text = event.text
                    end_usage = (event.data or {}).get("usage") or {}
                elif event.kind == "text_delta":
                    # 当前轮的增量:硬取消发生在 assistant_message 之前时,就靠它抢救半截回答
                    partial += event.text or ""
                elif event.kind == "assistant_message":
                    partial = event.text or ""        # 该轮的权威全文
                    # 思考**先**落:它发生在这条消息之前。顺序反了,回放就成了
                    # "先回答、再思考"。
                    self._persist_thinking(session, unit.name,
                                           str((event.data or {}).get("thinking") or ""))
                    if event.text and event.data.get("tool_calls"):
                        # 宣布了工具调用的助手消息是"过程"而非最终回答 → 立刻落成 custom entry。
                        # **立即**落盘而不缓冲到下一轮:否则它将被写在它触发的工具卡片**之后**,
                        # 回放顺序就变成"工具卡 → 叙述",与真实因果相反。
                        # 不带工具调用那条由回合末尾的 message entry 代表,所以不会重复。
                        self._persist_narration(session, unit.name, event.text)
                elif event.kind == "tool_start":
                    pending_tool = {"tool": event.tool, "args": event.data.get("args") or {}}
                elif event.kind == "tool_end":
                    self._persist_tool(session, unit.name, event, pending_tool)
                    pending_tool = None
                yield event
        except asyncio.CancelledError:
            # 硬取消:收尾不做任何 await(已在取消状态),只做同步落盘
            self._persist_final(session, unit.name, _salvaged(final_text, partial))
            if title_task is not None:
                title_task.cancel()      # 别把它漏在后台(它还会往会话里写)
            raise

        # 助手侧在回合结束后落盘(与旧版一致;tool 往返已在上方单独落盘)
        self._persist_final(session, unit.name, final_text, usage=end_usage)
        if title_task is not None:
            await self._apply_title(session, title_task)

    def _persist_final(self, session: Session, agent: str, text: str,
                       usage: dict | None = None) -> None:
        """回合末尾的助手消息(正常结束与中断收尾共用)。

        `usage` 落盘在本条 entry 上:它原来是**只活在流里**的(RUN_FINISHED 的
        metadata),刷新/重开就没了 —— 于是"这个会话用了多少 token"在设置页与
        输入卡下方都无从得知。放在助手消息上是因为它天然按轮分片(一輪一条),
        汇总就是会话级(见 `usage_summary`)。

        参数与分支摘要条目里的 `usage` 同名同义(那边是压缩那一次的用量)。
        """
        entry: dict = {"type": "message", "role": "assistant",
                       "content": text or "(无文本输出)",
                       "agent_id": agent}
        if usage:
            entry["usage"] = usage
        self.sessions.append(session, entry)

    @staticmethod
    def first_user_text(session: Session) -> str:
        """当前分支上第一条用户消息(没有就空串)。命名拿它当输入。"""
        for entry in session.branch():
            if entry.get("type") == "message" and entry.get("role") == "user":
                return str(entry.get("content") or "")
        return ""

    async def _apply_title(self, session: Session, task: asyncio.Task[str | None]) -> None:
        """把并行起的命名结果套到会话上。

        **最多等 `TITLE_WAIT_S`**:命名是体验改进,但不该拖住"这一轮结束"(前端在
        RUN_FINISHED 之后才刷新列表 —— 等太久就是可见的卡顿)。等不到就把落盘挂到
        回调上,它完成时自己写盘:下一次刷新(下一轮 / 重开会话)就能看到标题。
        """
        done, _pending = await asyncio.wait({task}, timeout=TITLE_WAIT_S)
        if task in done:
            self._write_title(session, task.result())
            return
        task.add_done_callback(
            lambda t: self._write_title(session, None if t.cancelled() else t.result()))

    def _write_title(self, session: Session, title: str | None) -> None:
        """落盘**模型给的**标题。空标题、或用户已经手动改过 → 不动。

        两条都必须有:模型回垃圾时保持「未命名」好过写个垃圾进文件;而用户在流式期间
        手动改了名,自动命名不能把它覆盖回去。

        实际落盘走 `set_session_title` —— 那里同时发 `session_info_changed`,
        所以自动命名与用户改名两条路都不会漏事件(也不会重发)。
        """
        if not title or session.title.strip():
            return
        self.set_session_title(session, title, source="auto")

    def _persist_thinking(self, session: Session, agent: str, text: str) -> None:
        """落盘一步思考(entry `type=custom`, `custom_type=assistant_thinking`)。

        与叙述同一个理由:思考是**过程**,直播时看得见(pi 的 thinking block),
        不落盘则刷新/回放里整段消失 —— 界面上"思考与回答之间那条分隔线"也就
        少了上半截(只剩一条孤零零的线)。

        做成 `custom` 而不是 `message` 是**刻意的**:`_history()` 只读 `message`,
        所以思考**不进 LLM 上下文**(零提示词回归风险)。

        思考可能很长:与工具结果同档封顶,不让会话文件无界增长。
        每步一条 `assistant_message`,空文本跳过。
        """
        if not text.strip():
            return
        body = (text if len(text) <= MAX_TOOL_ENTRY_CHARS
                else text[:MAX_TOOL_ENTRY_CHARS] + "…(落盘已截断)")
        self.sessions.append(session, {"type": "custom",
                                       "custom_type": "assistant_thinking",
                                       "agent": agent, "content": body})

    def _persist_narration(self, session: Session, agent: str, text: str) -> None:
        """落盘"工具调用之前"的助手叙述(custom entry,**不进对话上下文**)。

        直播时这些文字由 `text_delta` 送到前端;不落盘则刷新/回放就只剩工具卡片,
        直播与回放不一致。做成 `custom` 而非 `message` 是**刻意的**:`_history()` 只读
        `message`,所以模型跨轮上下文完全不变(零提示词回归风险)。
        """
        self.sessions.append(session, {"type": "custom",
                                       "custom_type": "assistant_narration",
                                       "agent": agent, "content": text})

    def _persist_tool(self, session: Session, agent: str, event: AgentEvent,
                      pending: dict | None) -> None:
        """把一次工具往返落盘(entry `type=tool`)。

        补齐 `session.py` 已声明的第五类 entry(PLAN A5):此前 tool 往返**从不落盘**,
        于是历史回放里工具卡片无法重现。`status`/`duration_ms`/`exit_code` 来自
        AgentRunner 的结构化结果,前端不必解析 `result` 字符串。
        """
        data = event.data or {}
        result = event.text or ""
        if len(result) > MAX_TOOL_ENTRY_CHARS:
            result = result[:MAX_TOOL_ENTRY_CHARS] + "…(落盘已截断)"
        # 插件能给的结构可能很大(一份查询结果、一棵文件树)。同样封顶,
        # 但**换成可渲染的标记**而不是切字符串 —— 切 JSON 会得到非法 JSON。
        details = data.get("details")
        if details is not None:
            encoded = json.dumps(details, ensure_ascii=False)
            if len(encoded) > MAX_TOOL_DETAILS_CHARS:
                details = {"_truncated": True, "_full_chars": len(encoded)}
        self.sessions.append(session, {
            "type": "tool", "agent": agent, "tool": event.tool,
            "args": (pending or {}).get("args", {}),
            "status": data.get("status"), "duration_ms": data.get("duration_ms"),
            "exit_code": data.get("exit_code"), "error": data.get("error"),
            "details": details,
            "result": result,
        })
