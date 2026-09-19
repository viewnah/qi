"""Dispatcher(P5,对齐 docs/dispatcher.md):信号分层 + 管线。

L1 规则(keywords)/ L3 Router-LLM(读 description,结构化 tool-call)/ L4 兜底。
**每轮输入都重新路由**:规则层不认 active_agent,只有 @点名 / keywords 唯一命中才能
零 LLM 短路,其余一律交 L3;active_agent 只作为 Router 的上下文提示(不做会话亲和)。
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


@dataclass
class Decision:
    agent: str | None
    confidence: float
    source: str            # mention|rules|router|fallback|manual
    reasoning: str = ""


class Dispatcher:
    def __init__(self, registry: AgentRegistry, router_llm: LLMClient | None = None,
                 confidence_min: float = CONFIDENCE_MIN):
        self.registry = registry
        self.router_llm = router_llm
        self.confidence_min = confidence_min

    # ── 管线(规则路径:同步,零 LLM) ──
    def rule_decide(self, text: str, active_agent: str | None = None) -> Decision | None:
        """@点名 / L1 规则。两者都不命中时返回 None → 交 L3 Router。

        `active_agent` 只作签名兼容:规则层**不做会话亲和**(对齐 B7' 每轮重新路由),
        确定性直派只有 @点名 与 keywords 唯一命中两种。
        """
        text = text.strip()
        m = MENTION_RE.match(text)
        if m and self.registry.get(m.group(1)):
            return Decision(agent=m.group(1), confidence=1.0, source="mention",
                            reasoning="@点名直派")
        unique = self._keyword_hits(text) if self.registry.names else []
        if len(unique) == 1:
            return Decision(agent=unique[0], confidence=1.0, source="rules",
                            reasoning=f"keywords 命中 {unique[0]}")
        # 无命中 / 多命中(歧义)→ 交 L3 Router 裁决
        return None

    def _keyword_hits(self, text: str) -> list[str]:
        """L1 规则命中:返回命中的 agent 名(排序去重)。

        ASCII keyword 按词边界匹配,避免 `review` 误命中 `code-reviewer` / `preview`;
        中文(非 ASCII)keyword 无词边界概念,保持子串匹配。
        """
        text_lower = text.lower()
        hit: list[str] = []
        for unit in self.registry.all():
            for kw in unit.config.keywords:
                if self._kw_hit(kw, text_lower):
                    hit.append(unit.name)
                    break
        return sorted(set(hit))

    @staticmethod
    def _kw_hit(kw: str, text_lower: str) -> bool:
        kw = (kw or "").strip().lower()
        if not kw:
            return False
        if kw.isascii():
            return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])",
                             text_lower) is not None
        return kw in text_lower

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
            "每轮输入都独立判断,不要因为是同一会话就默认沿用上一个 agent:\n"
            "- 像新任务(context 与当前 agent 职责不符)→ 改派更合适的 agent;\n"
            "- 是当前任务的延续(追问、补充、'继续'、纠正)→ 仍选当前 agent。\n"
            f"候选:\n{candidates}"
        )
        user = text
        if recent_summary:
            user = f"会话摘要:{recent_summary}\n\n最新输入:{text}"
        if active_agent:
            user += (f"\n(当前 agent:{active_agent};若最新输入是该任务的延续,应继续选它)")
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
                    conf = self._confidence(call.args.get("confidence"))
                    reasoning = str(call.args.get("reasoning", ""))
                    if conf is None:
                        # 模型给的不是数字 → 退到兜底,而不是把回合打崩
                        return self._fallback(text, "router 没给出可解析的 confidence")
                    if conf >= self.confidence_min:
                        return Decision(agent=agent, confidence=conf, source="router",
                                        reasoning=reasoning)
                    return self._fallback(text, f"router 置信度 {conf:.2f} < {self.confidence_min}")
                if agent:  # 未知名 → 重试一次
                    continue
        return self._fallback(text, "router 无有效输出")

    @staticmethod
    def _confidence(raw: object) -> float | None:
        """把模型给的 confidence 收敛成 float;不是数字就返回 None。

    为什么需要它(实测):Router-LLM 不保证字段类型 —— `"high"` 会 ValueError、
    `null` 会 TypeError,而这两句异常会**穿穿整个分派**(`decide_semantic` 没兜住它),
    把一个回合直接打崩。而这里本来就有兜底路径(`_fallback` 派给 general),
    所以“解析不了”应当降级,而不是崩。
    """
        if isinstance(raw, bool):        # bool 是 int 子类,但 `confidence: true` 没有含义
            return None
        if isinstance(raw, (int, float)):
            try:
                return float(raw)
            except (TypeError, ValueError):   # 前面已收窄到 int/float;防的是子类实现异常
                return None
        if isinstance(raw, str):
            try:
                return float(raw.strip())
            except ValueError:
                return None
        return None                      # None / 对象 / 列表… 都不算数字

    def _fallback(self, text: str, reason: str = "无规则/语义命中") -> Decision:
        general = self.registry.get("general")
        if general:
            return Decision(agent="general", confidence=0.5, source="fallback",
                            reasoning=f"{reason};派给 general")
        return Decision(agent=None, confidence=0.0, source="fallback",
                        reasoning=f"{reason};无 general,需澄清")
