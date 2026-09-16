"""轮次语义(不限/上限)+ 协作式中断(对齐 pi)。

覆盖:
  1. runner **没有轮次上限**(对齐 pi):要停就靠嵌入方给的 `stop_after` 谓词(hook 形态,
     不是数字字段),`stop_after_turns(n)` 是它的常用形状
  2. 谓词生效时报“达到轮次上限 N”;不传就一直跑
  3. 协作式中断:未执行的工具调用补上"已中断"结果(不让 tool_calls 悬空)、保留半截文本、
     照常发 agent_end 并在 data 里标记 aborted
  4. 硬取消(asyncio.Task.cancel)时,runtime.stream 仍把半截回答落盘
  5. 工具层响应中断:bash 长命令被整组回收,不留孤儿进程
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qi_agent.abort import AbortSignal  # noqa: E402
from qi_agent.llm import ChatMessage, ChatResponse, LLMDelta, ToolCallOut  # noqa: E402
from qi_agent.loader import load_agent_dir  # noqa: E402
from qi_agent.models import ToolOutcome  # noqa: E402
from qi_agent.registry import ToolCatalog  # noqa: E402
from qi_agent.runner import AgentRunner, RunnerSettings, stop_after_turns  # noqa: E402
from qi_agent.runtime import HEADLESS_MAX_TURNS, QiRuntime, RuntimeConfig  # noqa: E402
from qi_agent.session import SessionStore  # noqa: E402
from qi_agent.tools import ToolContext, register_builtin_tools  # noqa: E402


def _catalog() -> ToolCatalog:
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    return catalog


def _write_agent(root: Path, name: str = "x", tools: list[str] | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    tools_yaml = json.dumps(tools or ["read", "bash"])
    (d / "agent.md").write_text(
        f'---\nname: {name}\ndescription: 测试用\nkeywords: []\ntools: {tools_yaml}\n---\n你是测试 agent。\n',
        encoding="utf-8")
    return d


class ScriptedLLM:
    """按调用次数返回预设响应;用完就返回一句普通文本(防跑飞)。"""

    def __init__(self, script: list[ChatResponse]):
        self.script = list(script)
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=None):
        self.calls += 1
        if not self.script:
            return ChatResponse(text="结束")
        return self.script.pop(0)


class StreamingLLM:
    """先吐若干 delta 再卡住/收尾 —— 用来测"流式途中被中断"。"""

    def __init__(self, chunks: list[str], hang: bool = True):
        self.chunks = chunks
        self.hang = hang

    async def astream(self, messages, tools=None, temperature=None):
        for chunk in self.chunks:
            yield LLMDelta(text=chunk)
            await asyncio.sleep(0)
        if self.hang:
            await asyncio.sleep(30)          # 卡住:中断必须能把它掐掉
        yield LLMDelta(finished=True)

    async def chat(self, messages, tools=None, temperature=None):   # pragma: no cover
        return ChatResponse(text="".join(self.chunks))


def _runner(tmp_path: Path, llm, settings: RunnerSettings, tools=None) -> AgentRunner:
    catalog = _catalog()
    unit = load_agent_dir(_write_agent(tmp_path, tools=tools), "user", catalog.names)
    ctx = ToolContext(agent_name=unit.name, workdir=tmp_path)
    return AgentRunner(unit, catalog, llm, settings, tool_ctx=ctx)


def _tool_call(i: int, name: str = "read") -> ToolCallOut:
    args = {"path": "nope.txt"} if name == "read" else {"command": "true"}
    return ToolCallOut(id=f"c{i}", name=name, args=args)


async def _collect(agen) -> list:
    return [ev async for ev in agen]


# ── 1. 轮次语义 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_predicate_means_no_turn_limit(tmp_path):
    """不传 `stop_after` = 没有轮次上限(对齐 pi:核心循环不管轮数):该跑多少轮跑多少轮。"""
    script = [ChatResponse(text="", tool_calls=[_tool_call(i)]) for i in range(8)]
    script.append(ChatResponse(text="终于做完了"))
    llm = ScriptedLLM(script)
    runner = _runner(tmp_path, llm, RunnerSettings())     # 无 stop_after

    events = [e async for e in runner.run("干活")]
    assert not [e for e in events if e.kind == "error"], "不限轮次不该出现轮次上限报错"
    assert llm.calls == 9                                    # 8 轮工具 + 1 轮收尾
    end = next(e for e in events if e.kind == "agent_end")
    assert end.data["usage"]["turns"] == 9
    assert "aborted" not in end.data


@pytest.mark.asyncio
async def test_stop_after_predicate_reports_limit(tmp_path):
    """给了谓词就按谓词停:每轮结束问一次嵌入方(pi 的 shouldStopAfterTurn 语义)。"""
    script = [ChatResponse(text="", tool_calls=[_tool_call(i)]) for i in range(5)]
    llm = ScriptedLLM(script)
    seen: list[int] = []

    def policy(turns: int) -> bool:
        seen.append(turns)
        return turns >= 2

    runner = _runner(tmp_path, llm, RunnerSettings(stop_after=policy))
    events = [e async for e in runner.run("干活")]
    errors = [e.text for e in events if e.kind == "error"]
    assert any("达到轮次上限 2" in t for t in errors), errors
    assert llm.calls == 2
    assert seen == [1, 2], "谓词应在每轮结束后被问到(不是预先算一个区间)"


def test_turn_policy_shape():
    """runner 里不再有任何"上限数字":轮次是嵌入方的谓词(默认无);headless 用工厂。"""
    assert RunnerSettings().stop_after is None            # 默认不限
    assert not hasattr(RunnerSettings(), "max_turns")     # 数字字段已彻底移除
    assert RuntimeConfig(workdir=Path(".")).timeout_s == 600.0
    assert not hasattr(RuntimeConfig(workdir=Path(".")), "max_turns")
    assert stop_after_turns(HEADLESS_MAX_TURNS)(HEADLESS_MAX_TURNS - 1) is False
    assert stop_after_turns(HEADLESS_MAX_TURNS)(HEADLESS_MAX_TURNS) is True


# ── 2. 协作式中断 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_abort_during_tools_fills_remaining_results(tmp_path):
    """中断时**未执行**的调用也要有结果 —— 否则 tool_calls 悬空、两边记录不一致(pi 同款)。

    场景:模型一次给出 3 个 bash 调用;第一个正跑着(`sleep 30`)时中断。
    第一个由工具层杀进程组回报“已中断”,后两个不再执行但同样补上结果。
    """
    calls = [ToolCallOut(id=f"c{i}", name="bash",
                        args={"command": "sleep 30", "timeout": 30}) for i in range(3)]
    llm = ScriptedLLM([ChatResponse(text="这就去", tool_calls=calls)])
    runner = _runner(tmp_path, llm, RunnerSettings(), tools=["bash"])
    abort = AbortSignal()

    async def fire() -> None:
        await asyncio.sleep(0.15)          # 第一个工具已经跑起来了
        abort.abort()

    async def consume() -> list:
        return [ev async for ev in runner.run("干活", abort=abort)]

    events, _ = await asyncio.wait_for(asyncio.gather(consume(), fire()), timeout=15)
    ends = [e for e in events if e.kind == "tool_end"]
    assert len(ends) == 3, "三个 tool_call 都要有对应结果"
    assert all(e.data["error"] == "aborted" for e in ends), [e.data for e in ends]
    assert all("已中断" in e.text for e in ends)
    assert llm.calls == 1                       # 中断后不再请求下一轮
    end = next(e for e in events if e.kind == "agent_end")
    assert end.data["aborted"] is True


@pytest.mark.asyncio
async def test_abort_before_start_ends_cleanly(tmp_path):
    """“回车后立刻按 escape”:回合不能挂住、不能报错,且不能真的跑工具。"""
    llm = ScriptedLLM([ChatResponse(text="", tool_calls=[_tool_call(0, "bash")])])
    runner = _runner(tmp_path, llm, RunnerSettings(), tools=["bash"])
    abort = AbortSignal()
    abort.abort()

    events = await asyncio.wait_for(
        _collect(runner.run("干活", abort=abort)), timeout=5)
    assert not [e for e in events if e.kind == "error"]
    assert not [e for e in events if e.kind == "tool_end"]
    end = next(e for e in events if e.kind == "agent_end")
    assert end.data["aborted"] is True
    assert llm.calls <= 1                       # 至多白跑一次请求


@pytest.mark.asyncio
async def test_abort_mid_stream_keeps_partial_text(tmp_path):
    """流式途中中断:半截文本要留住,半截的 tool_calls 一律丢(参数不可信)。"""
    llm = StreamingLLM(["我在", "想……"], hang=True)
    runner = _runner(tmp_path, llm, RunnerSettings())
    abort = AbortSignal()

    async def fire() -> None:
        await asyncio.sleep(0.05)
        abort.abort()

    events: list = []

    async def consume() -> None:
        async for ev in runner.run("干活", abort=abort):
            events.append(ev)

    await asyncio.wait_for(asyncio.gather(consume(), fire()), timeout=5)
    end = next(e for e in events if e.kind == "agent_end")
    assert end.data["aborted"] is True
    assert "我在" in end.text and "想" in end.text, end.text
    assert not [e for e in events if e.kind == "error"]


@pytest.mark.asyncio
async def test_abort_signal_survives_no_abort_path(tmp_path):
    """没给信号时行为与旧版一致:正常的工具往返照跑。"""
    llm = ScriptedLLM([ChatResponse(text="", tool_calls=[_tool_call(0, "bash")]),
                       ChatResponse(text="done")])
    runner = _runner(tmp_path, llm, RunnerSettings())
    events = [e async for e in runner.run("跑一下")]        # 不传 abort
    assert llm.calls == 2
    assert not [e for e in events if e.kind == "error"]


# ── 3. 硬取消也要保住半截回答 ──────────────────────────────

@pytest.mark.asyncio
async def test_runtime_persists_partial_text_on_hard_cancel(tmp_path, monkeypatch):
    """web 的取消是 ASGI 侧 CancelledError。半截回答必须落盘,否则刷新就没了。"""
    from qi_agent import paths

    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    (tmp_path / "models.json").write_text(json.dumps({
        "providers": {"ollama": {"api": "openai-completions", "models": [{"id": "x"}]}}}),
        encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x"}), encoding="utf-8")

    sessions = SessionStore(root=tmp_path / "sessions")
    rt = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                   session_store=sessions, llm=StreamingLLM(["半截", "回答"], hang=True),
                   disable_router=True)
    session = sessions.create("t")

    async def consume() -> None:
        async for _ev in rt.stream("你好", session):
            pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    saved = [e for e in session.entries
             if e.get("type") == "message" and e.get("role") == "assistant"]
    assert saved, "被取消的回合也必须留下助手消息"
    assert "半截" in saved[-1]["content"]


# ── 4. 工具层响应中断 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_bash_abort_reaps_process_group(tmp_path):
    """bash 长命令被中断要立刻回收(含子进程),而不是等它自己跑完。"""
    from qi_agent.tools import _bash

    abort = AbortSignal()
    ctx = ToolContext(agent_name="t", workdir=tmp_path, abort=abort)
    marker = "qi-abort-probe"

    async def fire() -> None:
        await asyncio.sleep(0.1)
        abort.abort()

    async def run() -> ToolOutcome:
        return await _bash({"command": f"sleep 30 & echo {marker}; wait", "timeout": 30}, ctx)

    started = asyncio.get_running_loop().time()
    _task, outcome = await asyncio.wait_for(asyncio.gather(fire(), run()), timeout=10)
    elapsed = asyncio.get_running_loop().time() - started

    assert outcome.error == "aborted"
    assert outcome.result == "操作已中断(用户中止本回合)"
    assert elapsed < 5, f"中断应立刻生效,实际用了 {elapsed:.1f}s"

    # 子进程不能逃逸(进程组回收)
    await asyncio.sleep(0.2)
    probe = await asyncio.create_subprocess_exec(
        "pgrep", "-fl", "sleep 30", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    out, _ = await probe.communicate()
    assert marker not in out.decode() or not out.decode().strip(), out.decode()
