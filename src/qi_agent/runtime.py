"""AutoRuntime(runtime.py):manual/auto + sticky + @点名 + 会话 JSONL 持久化。

stream(): 一次用户输入 → 事件(CLI/TUI/HTTP 共享的 consumer 源)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .auth import AuthStore
from .config import ModelSpec, QiConfig, load_config, require_default_model
from .dispatcher import Decision, Dispatcher
from .llm import LiteLLMClient, LLMClient, chat_message_from_dict
from .loader import LoadError, load_all_agents
from .models import AgentEvent
from .registry import AgentRegistry, CapabilityRegistry, ToolCatalog, discover_plugins
from .runner import AgentRunner, RunnerSettings, build_system_prompt
from .session import Session, SessionStore
from .tools import ToolContext, register_builtin_tools


@dataclass
class RuntimeConfig:
    workdir: Path
    max_turns: int = 60
    timeout_s: float = 600.0
    confidence_min: float = 0.6


class QiRuntime:
    """装配好的一次性运行时(每进程一个):装载配置/插件/agents,提供 stream()。"""

    def __init__(self, cwd: Path | None = None, runtime_cfg: RuntimeConfig | None = None,
                 session_store: SessionStore | None = None,
                 llm: LLMClient | None = None,
                 router_llm: LLMClient | None = None,
                 disable_router: bool = False):
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.cfg, self.config_files = load_config(self.cwd)
        self.runtime_cfg = runtime_cfg or RuntimeConfig(workdir=self.cwd)
        self.workdir = self.runtime_cfg.workdir
        self.sessions = session_store or SessionStore()

        self.catalog = ToolCatalog()
        register_builtin_tools(self.catalog)
        self.capabilities = CapabilityRegistry()
        self.plugins = discover_plugins(self.catalog, self.capabilities, self.cwd)

        units = load_all_agents(self.cwd, self.catalog.names,
                                has_data_source_provider=self.capabilities.has_provider("data_sources"),
                                ds_types=self.capabilities.types("data_sources"))
        self.registry = AgentRegistry()
        self.registry.register_all(units)

        auth = AuthStore()
        default: ModelSpec = require_default_model(self.cfg)
        self.llm_exec = llm or LiteLLMClient(default, auth)
        if disable_router:
            self.router_llm = None
        else:
            router_spec = self.cfg.models.router or default
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
        for e in reversed(session.entries):
            if e.get("type") == "state" and e.get("key") == "active_agent":
                return e.get("value")
        return None

    def _set_active(self, session: Session, agent: str | None) -> None:
        self.sessions.append(session, {"type": "state", "key": "active_agent", "value": agent})

    def _history(self, session: Session) -> list:
        """取最近会话消息(含 tool 往返),供上下文。"""
        out = []
        for e in session.entries:
            if e.get("type") == "message" and e.get("role") != "system":
                d = dict(e)
                d.pop("type", None); d.pop("ts", None); d.pop("agent_id", None)
                out.append(chat_message_from_dict(d))
        return out[-40:]  # 简易窗口:最近 40 条(摘要机制 v2)

    async def stream(self, text: str, session: Session, agent_override: str | None = None):
        """处理一轮用户输入,产出事件。agent_override=manual(--agent / /agent)。"""
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
        yield AgentEvent(kind="dispatch", agent=decision.agent,
                         text=f"{decision.agent or '?'} ({decision.source}, {decision.confidence:.2f})",
                         data={"confidence": decision.confidence, "source": decision.source,
                               "reasoning": decision.reasoning})
        self.sessions.append(session, {"type": "dispatch", "agent": decision.agent,
                                       "confidence": decision.confidence,
                                       "source": decision.source,
                                       "reasoning": decision.reasoning})

        if decision.agent is None:
            yield AgentEvent(kind="error", text="没有合适的 agent 且无 general,请装一个 general 或用 @ 点名")
            return
        unit = self.registry.get(decision.agent)
        if unit is None:
            yield AgentEvent(kind="error", text=f"agent {decision.agent} 不存在")
            return

        if active != unit.name:
            self._set_active(session, unit.name)

        # opening:新会话首轮(历史为空时)
        if unit.config.opening and not session.entries[-1:][0].get("opening_shown"):
            # 简化:opening 消息由前端负责展示;此处仅标一次
            self.sessions.append(session, {"type": "custom", "custom_type": "opening_shown",
                                           "agent": unit.name})
            if unit.config.opening.message:
                yield AgentEvent(kind="opening", agent=unit.name,
                                 text=unit.config.opening.message,
                                 data={"suggestions": unit.config.opening.suggestions})

        runner = AgentRunner(unit, self.catalog, self.llm_exec,
                             RunnerSettings(max_turns=self.runtime_cfg.max_turns,
                                            timeout_s=self.runtime_cfg.timeout_s),
                             tool_ctx=self._tool_ctx(unit.name, unit))
        history = self._history(session)
        final_text = ""
        async for event in runner.run(text, history):
            if event.kind == "agent_end":
                final_text = event.text
            yield event

        # 持久化消息(tool 往返省略,存 user/assistant 文本 + agent_id)
        if final_text:
            self.sessions.append(session, {"type": "message", "role": "user",
                                           "content": text, "agent_id": unit.name})
            self.sessions.append(session, {"type": "message", "role": "assistant",
                                           "content": final_text, "agent_id": unit.name})
        else:
            self.sessions.append(session, {"type": "message", "role": "user",
                                           "content": text, "agent_id": unit.name})
            self.sessions.append(session, {"type": "message", "role": "assistant",
                                           "content": "(无文本输出)", "agent_id": unit.name})
