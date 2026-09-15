"""核心数据模型:AgentConfig / Skill / Message / 事件(对齐 docs/agent-config.md §4)。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
WILDCARD = "*"


class Opening(BaseModel):
    message: str = ""
    suggestions: list[str] = []


class AgentConfig(BaseModel):
    """agent.md frontmatter(v1 字段,见 agent-config.md §4)。"""

    name: str
    display_name: str = ""
    description: str = ""
    keywords: list[str] = []
    tools: list[str] | None = None          # None/省略 或 ["*"] = 全部;名单 = allowlist
    disallowed_tools: list[str] = []        # v1 denylist(B3,Claude 同款)
    include: list[str] = []
    opening: Opening | None = None
    mcp_servers: list[str] = []             # 全局 [mcp.servers] 绑定(默认无,显式声明)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(
                f"name {v!r} 不合法:小写字母/数字/连字符,不能首尾连字符/连续连字符"
            )
        return v

    @property
    def tools_all(self) -> bool:
        return self.tools is None or WILDCARD in (self.tools or [])

    def resolves_tools(self, catalog_names: set[str]) -> list[str]:
        """按 catalog 解析工具名:全部 → 排除 denylist;名单 → allowlist 再排除 denylist。"""
        if self.tools_all:
            names = [n for n in sorted(catalog_names) if n not in self.disallowed_tools]
        else:
            names = [t for t in (self.tools or []) if t not in self.disallowed_tools]
            unknown = [t for t in (self.tools or []) if t not in catalog_names]
            if unknown:
                raise ValueError(f"tools 引用了未知工具: {unknown}(可用: {sorted(catalog_names)})")
        return names


@dataclass
class Skill:
    name: str
    description: str          # 进 system prompt(渐进披露)
    path: Path                # SKILL.md 位置(用时 read 全文)
    source: str = ""          # 来源标签(诊断用:qi-global / agents-project / agent …)


@dataclass
class DataSource:
    id: str
    type: str
    dsn: str
    description: str = ""


@dataclass
class McpServerSpec:
    name: str
    config: dict             # {type/url/command/args/env/headers…}


@dataclass
class AgentUnit:
    """装载后的 agent(运行时形态,agent-config.md §7)。"""

    config: AgentConfig
    source: str                       # "user" | "project"
    path: Path                        # agent 目录
    system_prompt: str                # 正文 + include 拼合
    skills: list[Skill] = field(default_factory=list)
    data_sources: list[DataSource] = field(default_factory=list)
    mcp_private: list[McpServerSpec] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)   # 解析后的工具名

    @property
    def name(self) -> str:
        return self.config.name


# ── 会话消息与事件(CLI/TUI/HTTP 共享 stream 的载体)────────────────

ROLES = ("system", "user", "assistant", "tool")


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
