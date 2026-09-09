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


@dataclass
class Message:
    role: str                       # user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    agent_id: str | None = None


@dataclass
class AgentEvent:
    kind: str                       # agent_start|tool_start|tool_end|text|agent_end|dispatch|error
    agent: str | None = None
    tool: str | None = None
    text: str = ""
    data: dict = field(default_factory=dict)
