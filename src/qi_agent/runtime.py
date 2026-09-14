"""AutoRuntime(runtime.py):manual/auto + sticky + @点名 + 会话 JSONL 持久化。

stream(): 一次用户输入 → 事件(CLI/TUI/HTTP 共享的 consumer 源)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

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
from .config import ResolvedModel, load_config, resolve_default_model, resolve_router_model
from .dispatcher import Decision, Dispatcher
from .llm import (
    ChatMessage,
    LiteLLMClient,
    LLMClient,
    chat_message_from_dict,
    normalize_thinking_level,
)
from .loader import LoadError, load_all_agents, load_top_level_skills, resolve_base_prompt
from .models import AgentEvent
from .registry import AgentRegistry, CapabilityRegistry, ToolCatalog, discover_plugins
from .runner import AgentRunner, RunnerSettings
from .session import Session, SessionStore
from .settings import load_settings, session_dir
from .tools import ToolContext, register_builtin_tools


@dataclass
class RuntimeConfig:
    workdir: Path
    max_turns: int = 60
    timeout_s: float = 600.0
    confidence_min: float = 0.6


# 落盘工具结果的字符上限。tools/ 内置工具已自行截断(200 行 / 50k 字符),
# 但插件工具可能不截断——落盘前再过一道上限,避免单个工具撑破会话文件。
MAX_TOOL_ENTRY_CHARS = 8000


class QiRuntime:
    """装配好的一次性运行时(每进程一个):装载配置/插件/agents,提供 stream()。"""

    def __init__(self, cwd: Path | None = None, runtime_cfg: RuntimeConfig | None = None,
                 session_store: SessionStore | None = None,
                 llm: LLMClient | None = None,
                 router_llm: LLMClient | None = None,
                 disable_router: bool = False,
                 skills_enabled: bool = True,
                 thinking_level: str | None = None,
                 extra_skill_paths: Iterable[Path] | None = None):
        self.cwd = Path(cwd) if cwd else Path.cwd()
        # 旧版扁平布局 → ~/.qi/agent/(幂等;显式设了 QI_AGENT_HOME 时不动)
        paths.ensure_layout()
        self.cfg, self.config_files = load_config(self.cwd)
        self.settings, self.settings_files = load_settings(self.cwd)
        self.runtime_cfg = runtime_cfg or RuntimeConfig(workdir=self.cwd)
        self.workdir = self.runtime_cfg.workdir
        # 基座提示词:项目 .qi/SYSTEM.md > ~/.qi/agent/SYSTEM.md > 包内置(见 system.py)
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
        self.plugins = discover_plugins(self.catalog, self.capabilities, self.cwd)

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
        default: ResolvedModel = resolve_default_model(self.cfg, self.cwd)
        # 思考级别:显式传参(CLI --thinking)> settings.defaultThinkingLevel > off
        level = thinking_level if thinking_level is not None else self.settings.defaultThinkingLevel
        self.thinking_level = normalize_thinking_level(level)
        self.llm_exec = llm or LiteLLMClient(default, auth, thinking_level=self.thinking_level)
        if disable_router:
            self.router_llm = None
        else:
            router_spec = resolve_router_model(self.cfg, self.cwd)
            self.router_llm = router_llm if router_llm is not None else LiteLLMClient(router_spec, auth)
        self.dispatcher = Dispatcher(self.registry, self.router_llm,
                                     confidence_min=self.runtime_cfg.confidence_min)

    # ── 工具上下文(每个会话独立) ──
    def _tool_ctx(self, agent_name: str, unit) -> ToolContext:
        return ToolContext(agent_name=agent_name, workdir=self.workdir,
                           data_sources=unit.data_sources,
                           ask=self._ask)

    async def _ask(self, question: str) -> str | None:
        """clarify:headless 默认无可交互输入。"""
        return None

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
        tokens = messages_tokens(self._history(session)) + estimate_tokens(self.base_prompt)
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

    async def stream(self, text: str, session: Session, agent_override: str | None = None):
        """处理一轮用户输入,产出事件。agent_override=manual(--agent / /agent)。"""
        # 旧会话首次被使用时回填 cwd(只写一次);新会话在 create(cwd=…) 时已带
        self.sessions.ensure_cwd(session, self.cwd)
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

        runner = AgentRunner(unit, self.catalog, self.llm_exec,
                             RunnerSettings(max_turns=self.runtime_cfg.max_turns,
                                            timeout_s=self.runtime_cfg.timeout_s),
                             tool_ctx=self._tool_ctx(unit.name, unit),
                             base_prompt=self.base_prompt)
        # 顺序要紧:先取上下文(不含本轮),再把 user 消息立即落盘。
        # 旧实现把 user 写在回合**结束后**,于是运行中刷新/断线就看不到自己说了什么;
        # 而若先落盘再取 history,本轮输入会进上下文两次(prompt 里出现两条同样的 user)。
        history = self._history(session)
        self.sessions.append(session, {"type": "message", "role": "user",
                                       "content": text, "agent_id": unit.name})
        final_text = ""
        pending_tool: dict | None = None
        async for event in runner.run(text, history):
            if event.kind == "agent_end":
                final_text = event.text
            elif event.kind == "tool_start":
                pending_tool = {"tool": event.tool, "args": event.data.get("args") or {}}
            elif event.kind == "tool_end":
                self._persist_tool(session, unit.name, event, pending_tool)
                pending_tool = None
            elif event.kind == "assistant_message" and event.text and event.data.get("tool_calls"):
                # 宣布了工具调用的助手消息是"过程"而非最终回答 → 立刻落成 custom entry。
                # **立即**落盘而不缓冲到下一轮:否则它将被写在它触发的工具卡片**之后**,
                # 回放顺序就变成"工具卡 → 叙述",与真实因果相反。
                # 不带工具调用那条由回合末尾的 message entry 代表,所以不会重复。
                self._persist_narration(session, unit.name, event.text)
            yield event

        # 助手侧在回合结束后落盘(与旧版一致;tool 往返已在上方单独落盘)
        self.sessions.append(session, {"type": "message", "role": "assistant",
                                       "content": final_text or "(无文本输出)",
                                       "agent_id": unit.name})

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
        self.sessions.append(session, {
            "type": "tool", "agent": agent, "tool": event.tool,
            "args": (pending or {}).get("args", {}),
            "status": data.get("status"), "duration_ms": data.get("duration_ms"),
            "exit_code": data.get("exit_code"), "error": data.get("error"),
            "result": result,
        })
