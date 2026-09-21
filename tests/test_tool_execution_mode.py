"""`executionMode`(pi 的逐工具并发声明)。

pi 的 `ToolDefinition.executionMode` 是 `"sequential" | "parallel"`(默认顺序)。qi 的行为:

* **默认不变** —— 不声明就是顺序,与以前逐字节一致;
* 声明 `"parallel"` 的工具,与**相邻**的 parallel 工具并成一批并发跑;
* 一个顺序工具会把批次**打断**,所以顺序工具永远不会与并行工具重叠;
* 事件与上下文顺序仍然确定:先把整批的 start 发完,再按声明序发 end / 结果 / 消息。

这个文件测的就是上面四条 —— 尤其是"并发真的发生了"与"顺序没有被并发打乱"。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent.extensions import Tool  # noqa: E402
from qi_agent.llm import ChatResponse, ToolCallOut  # noqa: E402
from qi_agent.registry import ToolCatalog  # noqa: E402
from qi_agent.runner import AgentRunner, RunSpec, plan_tool_batches  # noqa: E402

SLEEP = 0.06


class _CallsLLM:
    """第一轮宣布给定的工具调用,第二轮收工。"""

    def __init__(self, calls: list[ToolCallOut]) -> None:
        self._calls = calls
        self.turns = 0

    async def chat(self, messages, tools=None, temperature=None):  # noqa: ANN001, ARG002
        self.turns += 1
        if self.turns == 1:
            return ChatResponse(text="", tool_calls=self._calls)
        return ChatResponse(text="done", tool_calls=[])


def _tool(name: str, trace: list, *, mode: str = "") -> Tool:
    async def execute(args, ctx):  # noqa: ANN001, ARG001
        trace.append((name, "start", time.perf_counter()))
        await asyncio.sleep(SLEEP)
        trace.append((name, "end", time.perf_counter()))
        return f"{name} 结果"

    return Tool(name=name, description="d", parameters={"type": "object", "properties": {}},
                execute=execute, execution_mode=mode)


def _call(cid: str, name: str) -> ToolCallOut:
    return ToolCallOut(id=cid, name=name, args={})


async def _run(catalog: ToolCatalog, calls: list[ToolCallOut]) -> list:
    runner = AgentRunner(RunSpec(name="qi", prompt="p", tools=sorted(catalog.names)),
                         catalog, _CallsLLM(calls))
    return [event async for event in runner.run("go")]


def _spans(trace: list, name: str) -> tuple[float, float]:
    start = next(t for n, kind, t in trace if n == name and kind == "start")
    end = next(t for n, kind, t in trace if n == name and kind == "end")
    return start, end


# ── 批次规划(纯函数)────────────────────────────────────────

def test_plan_batches_groups_only_adjacent_parallel_tools():
    catalog = ToolCatalog()
    catalog.register(_tool("seq", []))
    catalog.register(_tool("par", [], mode="parallel"))
    calls = [_call("1", "par"), _call("2", "par"), _call("3", "seq"),
             _call("4", "par"), _call("5", "par")]
    assert [[c.id for c in batch] for batch in plan_tool_batches(calls, catalog)] == [
        ["1", "2"], ["3"], ["4", "5"]]


def test_plan_batches_unknown_tool_is_sequential():
    """不认识的名字(动态注册还没生效/拼错)按顺序处理 —— 不能默认并发。"""
    catalog = ToolCatalog()
    calls = [_call("1", "ghost"), _call("2", "ghost")]
    assert [[c.id for c in batch] for batch in plan_tool_batches(calls, catalog)] == [["1"], ["2"]]


# ── 并发真的发生了 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_parallel_tools_actually_overlap():
    catalog = ToolCatalog()
    trace: list = []
    catalog.register(_tool("a", trace, mode="parallel"))
    catalog.register(_tool("b", trace, mode="parallel"))

    await _run(catalog, [_call("1", "a"), _call("2", "b")])

    a_start, a_end = _spans(trace, "a")
    b_start, b_end = _spans(trace, "b")
    # 两个区间必须相交(否则就是顺序跑的,"并发"名不副实)
    assert a_start < b_end and b_start < a_end
    # 并且总耗时接近一次 sleep,而不是两次
    assert max(a_end, b_end) - min(a_start, b_start) < SLEEP * 1.8


@pytest.mark.asyncio
async def test_sequential_tools_do_not_overlap_by_default():
    catalog = ToolCatalog()
    trace: list = []
    catalog.register(_tool("a", trace))          # 不声明 = 顺序
    catalog.register(_tool("b", trace))

    await _run(catalog, [_call("1", "a"), _call("2", "b")])

    a_start, a_end = _spans(trace, "a")
    b_start, b_end = _spans(trace, "b")
    assert a_end <= b_start                       # 完全串行
    assert max(a_end, b_end) - min(a_start, b_start) >= SLEEP * 1.9


@pytest.mark.asyncio
async def test_sequential_tool_never_overlaps_a_parallel_batch():
    """顺序工具把批次打断 —— 它前后的 parallel 工具都不会与它重叠。"""
    catalog = ToolCatalog()
    trace: list = []
    catalog.register(_tool("p1", trace, mode="parallel"))
    catalog.register(_tool("p2", trace, mode="parallel"))
    catalog.register(_tool("mid", trace))
    catalog.register(_tool("p3", trace, mode="parallel"))
    catalog.register(_tool("p4", trace, mode="parallel"))

    await _run(catalog, [_call("1", "p1"), _call("2", "p2"), _call("3", "mid"),
                         _call("4", "p3"), _call("5", "p4")])

    mid_start, mid_end = _spans(trace, "mid")
    for name in ("p1", "p2", "p3", "p4"):
        start, end = _spans(trace, name)
        assert end <= mid_start or start >= mid_end, f"{name} 与顺序工具重叠了"


# ── 事件与上下文顺序仍然确定 ──────────────────────────────

@pytest.mark.asyncio
async def test_parallel_batch_emits_all_starts_then_all_ends_in_order():
    catalog = ToolCatalog()
    catalog.register(_tool("a", [], mode="parallel"))
    catalog.register(_tool("b", [], mode="parallel"))

    events = await _run(catalog, [_call("1", "a"), _call("2", "b")])
    tool_events = [(e.kind, e.tool, (e.data or {}).get("tool_call_id"))
                   for e in events if e.kind in ("tool_start", "tool_end")]

    assert tool_events == [("tool_start", "a", "1"), ("tool_start", "b", "2"),
                           ("tool_end", "a", "1"), ("tool_end", "b", "2")]


@pytest.mark.asyncio
async def test_internal_tool_events_carry_the_call_id():
    """`tool_call_id` 是并发配对的前提 —— 少了它前端与落盘只能靠“猜”。"""
    catalog = ToolCatalog()
    catalog.register(_tool("a", []))
    events = await _run(catalog, [_call("abc", "a")])
    ids = [(e.data or {}).get("tool_call_id") for e in events
           if e.kind in ("tool_start", "tool_end")]
    assert ids == ["abc", "abc"]


@pytest.mark.asyncio
async def test_tool_messages_keep_declaration_order():
    """并发跑完了,但进上下文的顺序仍是模型声明的顺序(可复现)。"""
    catalog = ToolCatalog()
    catalog.register(_tool("a", [], mode="parallel"))
    catalog.register(_tool("b", [], mode="parallel"))
    events = await _run(catalog, [_call("1", "a"), _call("2", "b")])
    end = next(e for e in events if e.kind == "agent_end")
    tool_msgs = [m for m in (end.data or {}).get("messages", []) if m.get("role") == "tool"]
    assert [m.get("content") for m in tool_msgs] == ["a 结果", "b 结果"]
    assert [m.get("tool_call_id") for m in tool_msgs] == ["1", "2"]


@pytest.mark.asyncio
async def test_blocked_parallel_call_yields_denied_without_running():
    """闸门仍然逐条生效:被拦的那条不执行,同批其它的照跑。"""
    from qi_agent.abort import AbortSignal
    from qi_agent.extensions import ExtensionBus, ExtensionContext

    catalog = ToolCatalog()
    trace: list = []
    catalog.register(_tool("a", trace, mode="parallel"))
    catalog.register(_tool("b", trace, mode="parallel"))

    bus = ExtensionBus()
    bus.on("tool_call", lambda payload, ctx: {"block": True, "reason": "测试拦截"})
    runner = AgentRunner(RunSpec(name="qi", prompt="p", tools=["a", "b"]), catalog,
                         _CallsLLM([_call("1", "a"), _call("2", "b")]), bus=bus,
                         extension_ctx=lambda signal=None: ExtensionContext(cwd=Path("/tmp")))
    events = [event async for event in runner.run("go", abort=AbortSignal())]

    assert trace == []                       # 两条都被拦 → 一个都没执行
    results = [e for e in events if e.kind == "tool_end"]
    assert all((e.data or {}).get("error") == "denied" for e in results)
    assert [e.data.get("tool_call_id") for e in results] == ["1", "2"]
