"""AgentRunner(P4):单 agent tool-loop,消费 ToolCatalog + LLMClient。

system_prompt 的拼装在 `system_prompt.py`(对齐 pi 的 core/system-prompt.js):
默认基座 / 自定义 SYSTEM.md → 角色层(正文 + include)+ 项目上下文 + 技能清单
+ 数据源 + 工作目录。这里只负责在跑之前把**解析后的工具集**和 cwd 递给它。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .llm import ChatMessage, LLMClient, ToolCallOut, stream_llm
from .models import TOOL_ERROR, TOOL_OK, AgentEvent, AgentUnit, ToolOutcome
from .registry import Tool, ToolCatalog, ToolError
from .system_prompt import build_system_prompt


@dataclass
class RunnerSettings:
    max_turns: int = 60
    timeout_s: float = 600.0


def _accumulate_usage(total: dict, usage: dict | None) -> None:
    """把一次 LLM 调用的 usage 累加进总计。

    按需累加所有**整数**字段:provider 差异大(有的给 `cached_tokens`,有的给
    `cache_read_input_tokens`),不设白名单。非整数值忽略(bool 也是 int 的子类,单独排除)。
    """
    total["llm_calls"] = total.get("llm_calls", 0) + 1
    for key, value in (usage or {}).items():
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


class AgentRunner:
    def __init__(self, unit: AgentUnit, catalog: ToolCatalog, llm: LLMClient,
                 settings: RunnerSettings | None = None, tool_ctx=None,
                 base_prompt: str | None = None):
        self.unit = unit
        self.catalog = catalog
        self.llm = llm
        self.settings = settings or RunnerSettings()
        self.tool_ctx = tool_ctx
        self.base_prompt = base_prompt

    def _tools(self) -> list[Tool]:
        return self.catalog.resolve(self.unit.tools)

    async def run(self, user_input: str, history: list[ChatMessage] | None = None):
        """执行一轮用户输入,产出事件。history 为会话上下文(system 已在其中则跳过)。"""
        tools = self._tools()
        msgs: list[ChatMessage] = []
        if history is None or not any(m.role == "system" for m in history):
            # 工具集与 cwd 都要在跑之前定下来:清单进 prompt,且决定技能能否被读取
            msgs.append(ChatMessage(role="system", content=build_system_prompt(
                self.unit, self.base_prompt, tools=tools,
                cwd=self.tool_ctx.workdir if self.tool_ctx else None)))
        if history:
            msgs.extend(history)
        msgs.append(ChatMessage(role="user", content=user_input))

        yield AgentEvent(kind="agent_start", agent=self.unit.name)
        last_text = ""
        usage_total: dict = {}
        turns_used = 0
        schemas = [t.to_llm_schema() for t in tools] or None
        try:
            for turn in range(self.settings.max_turns):
                acc_text = ""
                acc_thinking = ""
                tool_calls: list[ToolCallOut] = []
                usage: dict = {}
                async with asyncio.timeout(self.settings.timeout_s):
                    async for delta in stream_llm(self.llm, msgs, tools=schemas):
                        if delta.reasoning:
                            # 思考内容:与回答分开流式(pi 的 thinking block)
                            acc_thinking += delta.reasoning
                            yield AgentEvent(kind="thinking_delta", agent=self.unit.name,
                                             text=delta.reasoning)
                        if delta.text:
                            acc_text += delta.text
                            # 逐字流式:Web 端靠它打字。CLI/TUI 只读回合末尾的 text 事件,
                            # 所以它们的输出不变——这是有意为之的向后兼容。
                            yield AgentEvent(kind="text_delta", agent=self.unit.name,
                                             text=delta.text)
                        if delta.finished:
                            tool_calls = delta.tool_calls
                            usage = delta.usage
                turns_used = turn + 1
                _accumulate_usage(usage_total, usage)
                msgs.append(ChatMessage(role="assistant", content=acc_text,
                                        tool_calls=tool_calls))
                last_text = acc_text
                # 每轮 LLM 回复单独声明一次:让 runtime 能把"工具之间的叙述"落盘,
                # 否则直播看得见、刷新后丢失(直播与回放不一致)。
                yield AgentEvent(kind="assistant_message", agent=self.unit.name,
                                 text=acc_text,
                                 data={"step": turns_used,
                                       "thinking": acc_thinking,
                                       "tool_calls": [c.name for c in tool_calls]})
                if not tool_calls:
                    break
                for call in tool_calls:
                    yield AgentEvent(kind="tool_start", agent=self.unit.name,
                                     tool=call.name, data={"args": call.args})
                    outcome = await self._execute(tools, call)
                    # 结构化结果:前端工具卡片靠 status/duration_ms/exit_code 渲染,
                    # 不再解析 text 前缀;text 仍是模型可见的原文(与旧版一致)。
                    yield AgentEvent(kind="tool_end", agent=self.unit.name, tool=call.name,
                                     text=outcome.result,
                                     data={"status": outcome.status,
                                           "duration_ms": outcome.duration_ms,
                                           "exit_code": outcome.exit_code,
                                           "error": outcome.error,
                                           # 插件/工具给客户端看的自由结构。
                                           # 这一行以前不存在 —— 于是插件就算自己造了
                                           # details 也**到这一行就被扔掉**(见 docs/web.md §16)。
                                           "details": outcome.details})
                    msgs.append(ChatMessage(role="tool", content=outcome.result,
                                            tool_call_id=call.id))
                if turn >= self.settings.max_turns - 1:
                    yield AgentEvent(kind="error", agent=self.unit.name,
                                     text=f"达到最大轮次 {self.settings.max_turns},已停止")
            else:
                yield AgentEvent(kind="error", agent=self.unit.name, text="达到最大轮次")
        except asyncio.TimeoutError:
            yield AgentEvent(kind="error", agent=self.unit.name,
                             text=f"执行超时(>{self.settings.timeout_s:.0f}s)")
        if last_text:
            yield AgentEvent(kind="text", agent=self.unit.name, text=last_text)
        yield AgentEvent(kind="agent_end", agent=self.unit.name, text=last_text,
                         data={"messages": [m.to_dict() for m in msgs[1:]],
                               "usage": {"turns": turns_used, **usage_total}})

    async def _execute(self, tools: list[Tool], call: ToolCallOut) -> ToolOutcome:
        """执行一次工具调用,返回**结构化**结果。

        计时在统一入口做,所以所有工具(含插件工具)都自动带上 duration_ms,不必各自上报。
        工具返回 `str`(旧约定)视为 `ok`;返回 `ToolOutcome` 则采用其 status/exit_code。
        """
        started = time.perf_counter_ns()

        def elapsed_ms() -> int:
            return (time.perf_counter_ns() - started) // 1_000_000

        tool = self.catalog.get(call.name)
        if tool is None:
            return ToolOutcome(status=TOOL_ERROR, error="unknown_tool",
                               result=f"Error: 未知工具 {call.name}", duration_ms=elapsed_ms())
        try:
            raw = await tool.execute(call.args, self.tool_ctx)
        except ToolError as exc:
            return ToolOutcome(status=TOOL_ERROR, error="tool_error",
                               result=f"Error: {exc}", duration_ms=elapsed_ms())
        except Exception as exc:  # noqa: BLE001 工具异常 → 可读结果
            return ToolOutcome(status=TOOL_ERROR, error="exception",
                               result=f"Error: 工具执行异常 {type(exc).__name__}: {exc}",
                               duration_ms=elapsed_ms())
        if isinstance(raw, ToolOutcome):
            if not raw.duration_ms:      # 工具未自行上报 → 统一计时兜底
                raw.duration_ms = elapsed_ms()
            return raw
        return ToolOutcome(status=TOOL_OK, result=str(raw), duration_ms=elapsed_ms())
