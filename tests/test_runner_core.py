"""runner(tool-loop)的核心行为。

原来是 `test_agent_core.py`,里面混着四类测试:**loader**(agent.md 解析)、**dispatcher**(auto 分派)、
**runtime 端到端(auto + opening)**、以及**runner**。P-E4c 把前两类行为整体移出 core(角色归 qi-agents、
分派取消),第三类里的分派/开场白也随之消失 —— 所以只剩 runner 这两条迁过来。

它们**用运行单元(`RunSpec`)而不是 agent**:那正是 core 现在的形状(`{name, prompt, tools}`)。
会话持久化的端到端由 `test_contract_p0.py` 覆盖,不需要在这里重复。
"""

from __future__ import annotations

import asyncio  # noqa: F401  与其它用例保持同一形态(asyncio_mode=auto)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent.llm import ChatMessage, ChatResponse, ToolCallOut  # noqa: E402
from qi_agent.registry import ToolCatalog  # noqa: E402
from qi_agent.runner import (  # noqa: E402
    AgentRunner,
    RunnerSettings,
    RunSpec,
    stop_after_turns,
)
from qi_agent.system_prompt import build_system_prompt  # noqa: E402
from qi_agent.tools import ToolContext, register_builtin_tools  # noqa: E402


class StubLLM:
    """可编程 stub:按调用次数返回预设响应。"""

    def __init__(self, script: list[ChatResponse]):
        self.script = list(script)
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append(messages)
        if not self.script:
            return ChatResponse(text="(stub 无响应)")
        return self.script.pop(0)


def make_catalog() -> ToolCatalog:
    c = ToolCatalog()
    register_builtin_tools(c)
    return c


def make_spec(catalog: ToolCatalog, name: str = "w",
              tools: list[str] | None = None) -> RunSpec:
    """一个运行单元。`tools` 给名单就收窄(模拟旧 agent.md 的 allowlist)。"""
    names = list(tools) if tools is not None else sorted(catalog.names)
    return RunSpec(name=name,
                   prompt=build_system_prompt(None, tools=catalog.resolve(names)),
                   tools=names)


@pytest.mark.asyncio
async def test_runner_tool_loop(tmp_path):
    catalog = make_catalog()
    # 收窄的工具集(旧样例 agent.md 就是这四个)
    spec = make_spec(catalog, "code-analyst", ["read", "ls", "grep", "bash"])
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    ctx = ToolContext(agent_name=spec.name, workdir=tmp_path)
    # LLM 第一轮调 grep,第二轮给文本
    llm = StubLLM([
        ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="grep",
                                                      args={"pattern": "hello", "path": "a.txt"})]),
        ChatResponse(text="找到了 hello world"),
    ])
    runner = AgentRunner(spec, catalog, llm,
                         RunnerSettings(stop_after=stop_after_turns(5)), tool_ctx=ctx)
    events = [e async for e in runner.run("找 hello")]
    kinds = [e.kind for e in events]
    assert "tool_start" in kinds and "tool_end" in kinds
    text_events = [e.text for e in events if e.kind == "text"]
    assert text_events and "hello" in text_events[-1]


@pytest.mark.asyncio
async def test_runner_bash_is_not_command_filtered(tmp_path):
    """bash 不做命令级过滤(对齐 pi):旧白名单会拒的写操作现在能跑,结果回到模型。"""
    catalog = make_catalog()
    spec = make_spec(catalog, "x", ["read", "ls", "grep", "bash"])
    ctx = ToolContext(agent_name=spec.name, workdir=tmp_path)
    llm = StubLLM([
        ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="bash",
                                                      args={"command": "mkdir -p made && echo ok > made/f.txt"})]),
        ChatResponse(text="done"),
    ])
    runner = AgentRunner(spec, catalog, llm,
                         RunnerSettings(stop_after=stop_after_turns(5)), tool_ctx=ctx)
    events = [e async for e in runner.run("建个目录")]
    tool_msgs = [e.text for e in events if e.kind == "tool_end"]
    assert tool_msgs and "安全策略拒绝" not in tool_msgs[0]
    assert (tmp_path / "made" / "f.txt").read_text(encoding="utf-8").strip() == "ok"
