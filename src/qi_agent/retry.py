"""agent 级重试:一次 LLM 调用遇上**瞬时**故障就重跑它一次(A8,对齐 pi)。

pi 的 `retry.*`(`pi-coding-agent/docs/settings.md`、实现见 `pi-ai/dist/utils/retry.js`):

| 设置 | 默认 | 含义 |
| --- | --- | --- |
| `retry.enabled` | `true` | 瞬时故障自动重试 |
| `retry.maxRetries` | `3` | 最大重试次数 |
| `retry.baseDelayMs` | `2000` | 初始退避(指数:2s → 4s → 8s) |
| `retry.maxAgentDelayMs` | `60000` | 退避上限 |

两条与 pi 完全一致的原则:

1. **只重试瞬时故障**:过载 / 限流 / 5xx / 网络 / **流提前结束**都算;配额与计费类
   (`insufficient_quota`、`billing`…) **不重试** —— 那是要 fail fast 让上层去处理的事,
   退避重试只会把“额度没了”拖成“卡了很久”。
2. **重试的语义是“重跑这一次 LLM 调用”,不是“续上这条流”**:失败那次已经产出的半截
   文本由调用方丢掉(它从不落盘,等价于 pi 的“移除失败的 assistant 消息”)。这就是
   为什么这里敢重试而不会输出两遍 —— 在**流**里重试才会重复,在**轮**里重试不会。

分类沿用 pi 的做法:**看错误文本**(两张模式表,见 `RETRYABLE_PATTERNS`),而不是按异常
类型分 —— provider 把同一件事包成十几个不同异常类,类型分不过来,文本反而稳定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 瞬时故障(pi `RETRYABLE_PROVIDER_ERROR_PATTERN` 的同形表)。
#: 前三段是服务端压力/状态码,中间是网络与传输,最后是“流提前结束”与 provider 自己
#: 给的“你可以重试”提示 —— `ended without` 那一组是本模块存在的直接原因:
#: 上游把流掐断时,qi 以前只能整轮报错。
RETRYABLE_PATTERNS = [
    # 服务端压力 / 状态码
    "overloaded", "server_busy", "servers are currently busy",
    "currently experiencing high demand", "model is at capacity",
    "rate.?limit", "too many requests",
    "429", "500", "502", "503", "504", "520", "524",
    "service.?unavailable", "server.?error", "internal.?error",
    "provider.?returned.?error", "exceeded request buffer limit while retrying upstream",
    # 网络 / 传输
    "network.?error", "connection.?error", "connection.?refused", "connection.?lost",
    "connection.?reset",
    "other side closed", "fetch failed", "getaddrinfo", "ENOTFOUND", "EAI_AGAIN",
    "upstream.?connect", "reset before headers", "socket hang up",
    "socket connection was closed", "timed? out", "timeout", "terminated",
    "websocket.?closed", "websocket.?error",
    # 流提前结束(各 SDK 的措辞)
    "ended without", "stream ended before message_stop",
    "stream ended before a terminal response event",
    "http2 request did not get a response", "pending stream has been canceled",
    "retry delay",
    # 流提前结束 —— **litellm / httpx 的措辞**。pi 的表里没有这两条,因为 pi 直连
    # provider SDK(听起来是 “stream ended before message_stop”);qi 经 litellm,
    # 同一件事被包成 “Response payload is not completed: <TransferEncodingError>”,
    # 所以得单独列。不列它就等于用户那份报错永远不重试(实测踩到过)。
    "response payload is not completed", "TransferEncodingError",
    "not enough data to satisfy transfer length header", "MidStreamFallback",
    # provider 明说“可以重试”
    "you can retry your request", "try your request again", "please retry your request",
    "ResourceExhausted",
]

#: 明确**不该**重试的(pi `NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN` 的同形表):
#: 配额 / 计费 / 订阅上限。这些不是“等一会儿就好”,重试只会让用户多等 —— pi 让它们
#: 立刻失败,好让配额与用量限制的处理路径接手。
NON_RETRYABLE_PATTERNS = [
    "GoUsageLimitError", "FreeUsageLimitError",
    "Monthly usage limit reached", "available balance",
    "insufficient_quota", "out of budget", "quota exceeded", "billing",
    "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable",
]

_RETRYABLE = re.compile("|".join(RETRYABLE_PATTERNS), re.IGNORECASE)
_NON_RETRYABLE = re.compile("|".join(NON_RETRYABLE_PATTERNS), re.IGNORECASE)


@dataclass(frozen=True)
class RetryPolicy:
    """agent 级重试策略(`settings.retry` 解析后的形状)。"""

    enabled: bool = True
    max_retries: int = 3
    base_delay_ms: int = 2000
    max_agent_delay_ms: int = 60000


DEFAULT_POLICY = RetryPolicy()


def _count(value: object, fallback: int) -> int:
    """非负整数才算数(bool 是 int 的子类,要单独挡掉);坏值回落默认。"""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def agent_retry_policy(retry: dict | None) -> RetryPolicy:
    """`settings.retry` → agent 级策略。

    **只读 agent 级的四个键**:`retry.provider.*` 是另一个层(provider/SDK 的请求级重试,
    见 `llm.provider_retry_params`),pi 甚至建议那边保持 0 —— 两层混在一起会让“重试了几次”
    无法解释。缺省/坏值一律回落到 pi 的默认值。
    """
    src = retry if isinstance(retry, dict) else {}
    enabled = src.get("enabled")
    return RetryPolicy(
        enabled=enabled if isinstance(enabled, bool) else DEFAULT_POLICY.enabled,
        max_retries=_count(src.get("maxRetries"), DEFAULT_POLICY.max_retries),
        base_delay_ms=_count(src.get("baseDelayMs"), DEFAULT_POLICY.base_delay_ms),
        max_agent_delay_ms=_count(src.get("maxAgentDelayMs"),
                                  DEFAULT_POLICY.max_agent_delay_ms),
    )


def retry_delay_ms(policy: RetryPolicy, attempt: int) -> int:
    """第 `attempt` 次重试前等多久(指数退避,与 pi 的 `retryDelayMs` 同形)。

    `attempt` 从 1 起:`baseDelayMs × 2^(attempt-1)`,封顶 `maxAgentDelayMs`。
    封顶是必要的(pi 出于同一个理由加的):长时间故障下退避会指数跑到几小时,
    那看起来就像“卡死了”。
    """
    delay = policy.base_delay_ms * 2 ** max(0, attempt - 1)
    return min(delay, policy.max_agent_delay_ms)


def is_retryable_error(exc: BaseException) -> bool:
    """这个异常值得重跑一次吗(pi 的两张表)?

    看**整条 `raise ... from ...` 链**:qi 在 provider 层会把异常翻译成人话
    (`llm.friendly_provider_error`),原始关键字留在 `__cause__` 上 —— 只看最外层会把
    “翻译过的瞬时故障”误判成不可重试。
    """
    chunks: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chunks.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    text = "\n".join(chunks)
    if _NON_RETRYABLE.search(text):
        return False            # 配额/计费优先:即使同一句里还提到 429 也不重试
    return bool(_RETRYABLE.search(text))
