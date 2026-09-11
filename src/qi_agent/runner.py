"""AgentRunner(P4):单 agent tool-loop,消费 ToolCatalog + LLMClient。

system_prompt = 基座层(SYSTEM.md / 包内置)+ 角色层(正文 + include)
                + 技能清单(渐进披露)+ 数据源上下文。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .llm import ChatMessage, ChatResponse, LLMClient, ToolCallOut
from .loader import builtin_system_prompt
from .models import AgentEvent, AgentUnit
from .registry import Tool, ToolCatalog, ToolError


@dataclass
class RunnerSettings:
    max_turns: int = 60
    timeout_s: float = 600.0


def build_system_prompt(unit: AgentUnit, base_prompt: str | None = None) -> str:
    """拼出完整 system prompt:基座层 → 角色层 → 技能 → 数据源。

    `base_prompt=None` 时用包内置基座提示词(调用方通常传入 `resolve_base_prompt()`
    的结果,以计入项目/用户 `SYSTEM.md` 覆盖)。
    """
    base = (builtin_system_prompt() if base_prompt is None else base_prompt).strip()
    parts = [base] if base else []
    parts.append(unit.system_prompt or f"你是 {unit.config.name}。")
    if unit.skills:
        lines = "\n".join(f"- {s.name}: {s.description}" for s in unit.skills)
        parts.append("可用技能(需要时用 read 读取其 SKILL.md 全文执行):\n" + lines)
    if unit.data_sources:
        lines = "\n".join(f"- {ds.id} ({ds.type}): {ds.description or ds.dsn}" for ds in unit.data_sources)
        parts.append("可用数据源(db 工具需 data_source_id 属于以下清单):\n" + lines
                     + "\n规则:先查 schema 确认表结构,只读查询,禁止写操作。")
    return "\n\n".join(p for p in parts if p.strip())


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
            msgs.append(ChatMessage(role="system",
                                    content=build_system_prompt(self.unit, self.base_prompt)))
        if history:
            msgs.extend(history)
        msgs.append(ChatMessage(role="user", content=user_input))

        yield AgentEvent(kind="agent_start", agent=self.unit.name)
        last_text = ""
        try:
            for turn in range(self.settings.max_turns):
                resp: ChatResponse = await asyncio.wait_for(
                    self.llm.chat(msgs, tools=[t.to_llm_schema() for t in tools] or None),
                    timeout=self.settings.timeout_s,
                )
                msgs.append(ChatMessage(role="assistant", content=resp.text,
                                        tool_calls=resp.tool_calls))
                last_text = resp.text
                if not resp.tool_calls:
                    break
                for call in resp.tool_calls:
                    yield AgentEvent(kind="tool_start", agent=self.unit.name,
                                     tool=call.name, data={"args": call.args})
                    result = await self._execute(tools, call)
                    yield AgentEvent(kind="tool_end", agent=self.unit.name,
                                     tool=call.name, text=result)
                    msgs.append(ChatMessage(role="tool", content=result,
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
        yield AgentEvent(kind="agent_end", agent=self.unit.name,
                         text=last_text, data={"messages": [m.to_dict() for m in msgs[1:]]})

    async def _execute(self, tools: list[Tool], call: ToolCallOut) -> str:
        tool = self.catalog.get(call.name)
        if tool is None:
            return f"Error: 未知工具 {call.name}"
        try:
            result = await tool.execute(call.args, self.tool_ctx)
            return str(result)
        except ToolError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 工具异常 → 可读结果
            return f"Error: 工具执行异常 {type(exc).__name__}: {exc}"
