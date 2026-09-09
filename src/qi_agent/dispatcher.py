"""Dispatcher(P5,对齐 docs/dispatcher.md):信号分层 + 管线 + sticky。

L1 规则(keywords)/ L3 Router-LLM(读 description,结构化 tool-call)/ L4 兜底。
L2 embedding = 可插拔默认关(本文件留接口,未实现)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .llm import ChatMessage, ChatResponse, LLMClient
from .registry import AgentRegistry

MENTION_RE = re.compile(r"^@([a-z0-9]+(-[a-z0-9]+)*)\b")
CONFIDENCE_MIN = 0.6      # B6:可配 [runtime]
ROUTER_TOOL = "dispatch_to"

STICKY_NEW_TASK_WORDS = ("新建", "新开", "换成", "交给", "另起", "换个", "开始新")


@dataclass
class Decision:
    agent: str | None
    confidence: float
    source: str            # mention|rules|sticky|router|fallback|manual
    reasoning: str = ""


class Dispatcher:
    def __init__(self, registry: AgentRegistry, router_llm: LLMClient | None = None,
                 confidence_min: float = CONFIDENCE_MIN):
        self.registry = registry
        self.router_llm = router_llm
        self.confidence_min = confidence_min

    # ── 管线(规则路径:同步,零 LLM) ──
    def rule_decide(self, text: str, active_agent: str | None = None) -> Decision | None:
        """@点名 / L1 规则 / sticky。全部未命中且需要 Router 时返回 None。"""
        text = text.strip()
        m = MENTION_RE.match(text)
        if m and self.registry.get(m.group(1)):
            return Decision(agent=m.group(1), confidence=1.0, source="mention",
                            reasoning="@点名直派")
        if self.registry.names:
            rule_hit: list[str] = []
            for unit in self.registry.all():
                for kw in unit.config.keywords:
                    if kw and kw.lower() in text.lower():
                        rule_hit.append(unit.name)
                        break
            unique = sorted(set(rule_hit))
            if len(unique) == 1:
                return Decision(agent=unique[0], confidence=1.0, source="rules",
                                reasoning=f"keywords 命中 {unique[0]}")
        if active_agent and self.registry.get(active_agent) and not self._new_task_signal(text):
            return Decision(agent=active_agent, confidence=0.9, source="sticky",
                            reasoning="延续当前 agent")
        return None

    def _new_task_signal(self, text: str) -> bool:
        return any(w in text for w in STICKY_NEW_TASK_WORDS)

    # ── L3 Router(异步) ──
    async def decide_semantic(self, text: str, active_agent: str | None = None,
                              recent_summary: str = "") -> Decision:
        """Router-LLM 结构化分派 + L4 兜底。"""
        if self.router_llm is None:
            return self._fallback(text)
        candidates = "\n".join(
            f"- {u.name}: {u.config.description}" for u in self.registry.all())
        system = (
            "你是分派器。根据候选 agent 的 description 选择最合适的执行者,"
            "只调用一次 dispatch_to 工具。拿不准时 agent 选 general(若有)或不选。\n"
            f"候选:\n{candidates}"
        )
        user = text
        if recent_summary:
            user = f"会话摘要:{recent_summary}\n\n最新输入:{text}"
        if active_agent:
            user += f"\n(当前 agent:{active_agent})"
        for _attempt in range(2):
            resp: ChatResponse = await self.router_llm.chat(
                [ChatMessage(role="system", content=system),
                 ChatMessage(role="user", content=user)],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": ROUTER_TOOL,
                        "description": "选择执行 agent",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "agent": {"type": "string"},
                                "confidence": {"type": "number"},
                                "reasoning": {"type": "string"},
                            },
                            "required": ["agent", "confidence", "reasoning"],
                        },
                    },
                }],
            )
            call = next((c for c in resp.tool_calls if c.name == ROUTER_TOOL), None)
            if call:
                agent = str(call.args.get("agent", ""))
                if agent in self.registry.names:
                    conf = float(call.args.get("confidence", 1.0))
                    reasoning = str(call.args.get("reasoning", ""))
                    if conf >= self.confidence_min:
                        return Decision(agent=agent, confidence=conf, source="router",
                                        reasoning=reasoning)
                    return self._fallback(text, f"router 置信度 {conf:.2f} < {self.confidence_min}")
                if agent:  # 未知名 → 重试一次
                    continue
        return self._fallback(text, "router 无有效输出")

    def _fallback(self, text: str, reason: str = "无规则/语义命中") -> Decision:
        general = self.registry.get("general")
        if general:
            return Decision(agent="general", confidence=0.5, source="fallback",
                            reasoning=f"{reason};派给 general")
        return Decision(agent=None, confidence=0.0, source="fallback",
                        reasoning=f"{reason};无 general,需澄清")
