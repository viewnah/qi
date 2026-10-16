"""agent 级重试的纯函数部分:策略解析、指数退避、可重试判定(对齐 pi)。

判定表抄的是 pi 的形状(`pi-ai/dist/utils/retry.js`),所以这里的用例也取自 pi 注释里
点过名的那些真实错误文本 —— 免得“对齐”只落在字段名上。
"""
from __future__ import annotations

import pytest

from qi_agent.retry import (
    DEFAULT_POLICY,
    RetryPolicy,
    agent_retry_policy,
    is_retryable_error,
    retry_delay_ms,
)

# ── 策略解析 ──────────────────────────────────────────────

def test_default_policy_matches_pi():
    """默认值就是 pi 的默认值(enabled / 3 次 / 2s 起 / 封顶 60s)。"""
    assert DEFAULT_POLICY == RetryPolicy(enabled=True, max_retries=3,
                                         base_delay_ms=2000, max_agent_delay_ms=60000)
    assert agent_retry_policy(None) == DEFAULT_POLICY
    assert agent_retry_policy({}) == DEFAULT_POLICY


def test_policy_reads_the_agent_level_keys_only():
    """只认 agent 级的四个键;`retry.provider.*` 是另一层(见 llm)。"""
    policy = agent_retry_policy({
        "enabled": False, "maxRetries": 5, "baseDelayMs": 100, "maxAgentDelayMs": 700,
        "provider": {"maxRetries": 9, "timeoutMs": 1000},   # 不该被读进来
    })
    assert policy == RetryPolicy(enabled=False, max_retries=5,
                                 base_delay_ms=100, max_agent_delay_ms=700)


@pytest.mark.parametrize("bad", ["3", -1, 1.5, True, None])
def test_policy_falls_back_on_bad_values(bad):
    """坏值回落默认 —— 配置写错不该让一轮跑不起来。"""
    policy = agent_retry_policy({"maxRetries": bad, "baseDelayMs": bad})
    assert policy.max_retries == DEFAULT_POLICY.max_retries
    assert policy.base_delay_ms == DEFAULT_POLICY.base_delay_ms


def test_zero_retries_is_honoured_not_treated_as_missing():
    """`maxRetries: 0` = 明确关掉重试(不是“没配”)。"""
    assert agent_retry_policy({"maxRetries": 0}).max_retries == 0


# ── 指数退避 ──────────────────────────────────────────────

def test_delay_is_exponential_and_capped():
    """2s → 4s → 8s…,封顶 maxAgentDelayMs(pi 的 `retryDelayMs`)。"""
    policy = RetryPolicy(base_delay_ms=2000, max_agent_delay_ms=60000)
    assert [retry_delay_ms(policy, n) for n in (1, 2, 3, 4)] == [2000, 4000, 8000, 16000]
    # 封顶:第 6 次起 2000×2^5=64000 > 60000
    assert retry_delay_ms(policy, 6) == 60000
    assert retry_delay_ms(policy, 20) == 60000, "再往后也不涨(否则看起来像卡死)"


# ── 可重试判定(pi 的真实错误文本)────────────────────────

@pytest.mark.parametrize("text", [
    "Rate limit exceeded, please try again",              # 限流
    "The server is overloaded",                            # 过载
    "502 Bad Gateway",
    "connection reset by peer",
    "socket hang up",
    "fetch failed",
    "Anthropic stream ended before message_stop",           # ← 用户报的那一类
    "Anthropic stream ended without a stop reason",
    "Azure OpenAI Responses stream ended without a stop reason",
    "Codex stream ended without a stop reason",
    "you can retry your request",
    "litellm.MidStreamFallbackError: Response payload is not completed: "
    "<TransferEncodingError: 400>",
])
def test_transient_failures_are_retryable(text):
    assert is_retryable_error(RuntimeError(text)) is True


@pytest.mark.parametrize("text", [
    "insufficient_quota: you exceeded your current quota",
    "Monthly usage limit reached",
    "GoUsageLimitError",
    "billing hard limit reached",
    "subscription_sharing_usage_limit_exceeded",
])
def test_quota_and_billing_failures_are_not_retryable(text):
    """配额/计费要 fail fast —— 退避重试只会把“额度没了”变成“卡了很久”。"""
    assert is_retryable_error(RuntimeError(text)) is False


def test_quota_wins_over_a_transient_keyword_in_the_same_message():
    """同一句里既有 429 又有配额:按不可重试处理(pi 的判定顺序)。"""
    assert is_retryable_error(
        RuntimeError("429 insufficient_quota: quota exceeded")) is False


@pytest.mark.parametrize("text", [
    "400 invalid_request_error: messages.0.content: field required",
    "上下文长度超过模型上限",
])
def test_deterministic_errors_are_not_retryable(text):
    """请求本身有问题:重试一百次也是同样的错。"""
    assert is_retryable_error(RuntimeError(text)) is False


def test_translation_does_not_hide_the_original_error():
    """provider 层翻过一遍的异常仍要认得出来 —— 原始关键字在 cause 链上。"""
    from qi_agent.llm import ProviderStreamError

    raw = RuntimeError(
        "litellm.MidStreamFallbackError: <TransferEncodingError: 400, message='Not enough data'>")
    friendly = ProviderStreamError("上游的流**中途断了**(响应体不完整)。重试也尽了。")
    friendly.__cause__ = raw
    assert is_retryable_error(friendly) is True, "只看最外层会把瞬时故障误判成不可重试"
