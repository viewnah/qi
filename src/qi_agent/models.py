"""核心数据模型:Skill / Message / 事件 / 工具结果。

角色相关类型(AgentConfig / AgentUnit / DataSource / McpServerSpec)随 P-E4c 移出 core ——
它们属于 qi-agents / qi-mcp(E1.1/E15)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path



@dataclass
class Skill:
    name: str
    description: str          # 进 system prompt(渐进披露)
    path: Path                # SKILL.md 位置(用时 read 全文)
    source: str = ""          # 来源标签(诊断用:qi-global / agents-project / agent …)


@dataclass
@dataclass
@dataclass
@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


# 工具执行状态(ToolOutcome.status)
TOOL_OK = "ok"
TOOL_ERROR = "error"


@dataclass
class ToolOutcome:
    """一次工具执行的**结构化**结果。

    前端渲染工具卡片(status 点、耗时、退出码)只依赖这些字段,不再靠解析 `result`
    字符串前缀。`result` 保留为**模型可见文本**,与旧版 `execute()` 的返回值逐字一致,
    因此模型行为不变。

    `duration_ms` 由 AgentRunner 统一计时,工具自己不必上报。
    需要上报退出码的进程型工具(bash)返回本对象;其余工具继续返回 `str` 即可,
    由 AgentRunner 包成 `ok`。
    """

    status: str = TOOL_OK               # ok | error
    result: str = ""                    # 模型可见文本
    duration_ms: int = 0
    exit_code: int | None = None        # 仅进程型工具有意义
    error: str | None = None            # 机器可读原因: unknown_tool|tool_error|exception|timeout|denied
    details: dict | None = None         # 给**客户端**看的自由结构;不进 LLM 上下文

    # `details` 是插件与 UI 之间**唯一的**下行通道(见 docs/web.md §16)。
    # 为什么需要它:`result` 是给模型看的散文,UI 不该去解析它;而
    # status/duration_ms/exit_code 只够画一个状态点,画不了插件自己的东西
    # (todo 清单、查询结果、进度……)。给它一个开放字典,宿主就不必为每个插件改接口。
    #
    # 约定:想画结构化 UI 时放 `details["ui"]`,词汇表见 docs/web.md §16.2;
    # 不认识 `ui` 的客户端会退回"折叠显示原始 JSON",所以**永不会白屏或报错**。

    @property
    def ok(self) -> bool:
        return self.status == TOOL_OK


@dataclass
class Message:
    role: str                       # user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    agent_id: str | None = None


@dataclass
class AgentEvent:
    kind: str                       # agent_start|text_delta|assistant_message|tool_start|tool_end|text|agent_end|dispatch|opening|error
    agent: str | None = None
    tool: str | None = None
    text: str = ""
    data: dict = field(default_factory=dict)

    # 事件 data 的字段约定(消费者据此渲染,不必解析 text):
    #   text_delta → 逐字增量(在 text 上;Web 端打字靠它。CLI/TUI 只读回合末尾的 text 事件)
    #   assistant_message → 每轮 LLM 回复完成一次;{"step": int, "tool_calls": [名字]}。
    #                 宣布了工具调用的那条属于"过程"(runtime 落成 custom entry),
    #                 不带工具调用的那条才是最终回答(落成 message entry);也供轨迹视图分段
    #   tool_start → {"args": {...}}
    #   tool_end   → {"status": "ok|error", "duration_ms": int,
    #                 "exit_code": int|None, "error": str|None}
    #   agent_end  → {"messages": [...], "usage": {"prompt_tokens": int,
    #                 "completion_tokens": int, "total_tokens": int,
    #                 "llm_calls": int, "turns": int}}
    #   dispatch   → {"confidence", "source", "agent", "display_name", "reasoning"}
    #   opening    → {"suggestions": [...]}
    #
    # 顺序不变量:一轮里 text_delta* 与 tool_start/tool_end 按**真实发生顺序**穿插
    # (工具调用之间的叙述不再丢弃)。消费端不得把所有 text_delta 提到开头、
    # 把所有工具行推到末尾 —— 那样会失去因果顺序。
