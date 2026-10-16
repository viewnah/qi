"""runner 的 agent 级重试:一次 LLM 调用失败就**重跑它**(对齐 pi 的 `retry.*`)。

四条要钉住的语义(与 pi 一致):

1. **重跑的是这一次调用**,不是“续上那条流” —— 失败那次的半截输出由消费者丢弃,
   所以最终只落一份 `assistant_message`(不会出现两遍回答);
2. 重试时**上下文不变**:不重复插入用户消息,而且本轮已成功的工具往返仍在
   (所以模型不会把同一个工具再调一遍)—— 这是 pi “移除失败的 assistant 消息”的等价物;
3. 只有瞬时故障重试;配额/计费类与中断**永不**重试;
4. 退避是**指数**的,而且能被中断立刻打断(不能看起来像卡死)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent.abort import AbortSignal  # noqa: E402
from qi_agent.llm import ChatMessage  # noqa: E402
from qi_agent.registry import ToolCatalog  # noqa: E402
from qi_agent.retry import RetryPolicy  # noqa: E402
from qi_agent.runner import AgentRunner, RunnerSettings, RunSpec  # noqa: E402
from qi_agent.system_prompt import build_system_prompt  # noqa: E402
from qi_agent.tools import ToolContext, register_builtin_tools  # noqa: E402


class FlakyLLM:
    """可编程流式假 LLM:`plan` 每项要么是异常(抛,前面先吐一块半截),要么是
    `{"text", "tools"}`(整段成功)。"""

    def __init__(self, plan: list[object]) -> None:
        self.plan = list(plan)
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages, tools=None, temperature=None):  # pragma: no cover
        raise AssertionError("有 astream 时 runner 应该走流式")

    async def astream(self, messages, tools=None, temperature=None):
        from qi_agent.llm import LLMDelta

        self.calls.append(list(messages))
        step = self.plan.pop(0) if self.plan else {"text": "(script 用完了)"}
        if isinstance(step, BaseException):
            yield LLMDelta(text="半截")           # 已经吐出去的部分:消费者负责撤回
            raise step
        text = str(step.get("text") or "")
        if text:
            yield LLMDelta(text=text)
        yield LLMDelta(finished=True, tool_calls=step.get("tools") or [],
                       usage={"total_tokens": 1})


def _runner(tmp_path, llm, *, policy=None, tools=None) -> AgentRunner:
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    names = tools or ["ls"]
    spec = RunSpec(name="w", prompt=build_system_prompt(None, tools=catalog.resolve(names)),
                   tools=names)
    return AgentRunner(spec, catalog, llm,
                       RunnerSettings(retry=policy or RetryPolicy(base_delay_ms=0)),
                       tool_ctx=ToolContext(agent_name=spec.name, workdir=tmp_path))


async def _drain(runner, *, abort=None) -> list:
    return [ev async for ev in runner.run("你好", abort=abort)]


# ── 1. 重跑这一次调用,最终只有一份输出 ─────────────────────

@pytest.mark.asyncio
async def test_retry_reruns_the_call_and_lands_one_message(tmp_path):
    llm = FlakyLLM([RuntimeError("Anthropic stream ended before message_stop"),
                    {"text": "完整答案"}])
    events = await _drain(_runner(tmp_path, llm))

    retries = [ev for ev in events if ev.kind == "retry"]
    assert len(retries) == 1
    assert retries[0].data["attempt"] == 1 and retries[0].data["max"] == 3
    assert "ended without" in retries[0].data["error"] or \
        "message_stop" in retries[0].data["error"]
    assert retries[0].data["delay_ms"] == 0, "退避来自策略(测试里设为 0)"

    # 关键:失败那次**不发** assistant_message(所以不会落盘),只有成功的这条
    messages = [ev for ev in events if ev.kind == "assistant_message"]
    assert len(messages) == 1 and messages[0].text == "完整答案"
    assert events[-1].kind == "agent_end" and events[-1].text == "完整答案"
    assert len(llm.calls) == 2


# ── 2. 重试时上下文不变(不重复 user、保留工具往返)──────────

@pytest.mark.asyncio
async def test_retry_keeps_the_context_including_tool_roundtrip(tmp_path):
    """第 1 轮调工具成功、第 2 轮首次失败 → 重试那次调用时上下文里仍有工具结果。

    这条就是 pi “移除失败的 assistant 消息、重跑这一轮”的等价物:模型看得见
    已经跑过的工具,所以不会再把同一个工具调一遍。
    """
    from qi_agent.llm import ToolCallOut

    llm = FlakyLLM([
        {"text": "", "tools": [ToolCallOut(id="c1", name="ls", args={"path": "."})]},
        RuntimeError("connection lost"),
        {"text": "看完了"},
    ])
    events = await _drain(_runner(tmp_path, llm))
    assert [ev for ev in events if ev.kind == "retry"], "第二次调用失败要重试"

    first, retry = llm.calls[1], llm.calls[2]
    assert [m.role for m in first] == [m.role for m in retry], "重试不该改变消息表"
    assert sum(1 for m in retry if m.role == "user") == 1, "用户消息只出现一次"
    assert any(m.role == "tool" for m in retry), "已成功的工具结果必须留着"
    assert events[-1].text == "看完了"


# ── 3. 预算、配额、中断 ────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_budget_is_bounded(tmp_path):
    """一直失败:试满 `maxRetries` 次后把最后一个异常抛出去(不再无限重试)。"""
    llm = FlakyLLM([RuntimeError("502 Bad Gateway") for _ in range(10)])
    with pytest.raises(RuntimeError, match="502"):
        await _drain(_runner(tmp_path, llm, policy=RetryPolicy(max_retries=2,
                                                               base_delay_ms=0)))
    assert len(llm.calls) == 3, "初试 + 2 次重试"


@pytest.mark.asyncio
async def test_quota_errors_are_not_retried(tmp_path):
    """配额/计费类要 fail fast —— 退避重试只会把“额度没了”变成“卡了很久”。"""
    llm = FlakyLLM([RuntimeError("insufficient_quota: you exceeded your current quota")])
    with pytest.raises(RuntimeError, match="insufficient_quota"):
        await _drain(_runner(tmp_path, llm))
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_disabled_policy_never_retries(tmp_path):
    llm = FlakyLLM([RuntimeError("connection lost")])
    with pytest.raises(RuntimeError, match="connection lost"):
        await _drain(_runner(tmp_path, llm, policy=RetryPolicy(enabled=False)))
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_abort_during_backoff_stops_the_retry(tmp_path):
    """退避期间 escape:立刻停,不再重试,也不报错。"""
    abort = AbortSignal()
    llm = FlakyLLM([RuntimeError("connection lost")])
    runner = _runner(tmp_path, llm, policy=RetryPolicy(base_delay_ms=30_000))
    events = []
    async for ev in runner.run("你好", abort=abort):
        events.append(ev)
        if ev.kind == "retry":
            abort.abort()          # 正在退避(30s)→ 打断它
    assert [ev.kind for ev in events].count("retry") == 1
    assert len(llm.calls) == 1, "被中断后不该再发起第二次调用"
    assert events[-1].kind == "agent_end"


# ── 4. 退避是指数的 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_backoff_grows_between_attempts(tmp_path):
    llm = FlakyLLM([RuntimeError("overloaded"), RuntimeError("overloaded"),
                    {"text": "第三次成了"}])
    events = await _drain(_runner(tmp_path, llm,
                                  policy=RetryPolicy(base_delay_ms=1)))
    delays = [ev.data["delay_ms"] for ev in events if ev.kind == "retry"]
    assert delays == [1, 2], "指数退避:1ms → 2ms"
    assert events[-1].text == "第三次成了"
