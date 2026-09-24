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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from qi_agent.system_prompt import build_system_prompt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qi_agent.abort import AbortSignal  # noqa: E402
from qi_agent.llm import ChatMessage, ChatResponse, LLMDelta, ToolCallOut  # noqa: E402
from qi_agent.models import ToolOutcome  # noqa: E402
from qi_agent.registry import ToolCatalog  # noqa: E402
from qi_agent.runner import AgentRunner, RunnerSettings, RunSpec, stop_after_turns  # noqa: E402
from qi_agent.runtime import QiRuntime, RuntimeConfig  # noqa: E402
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
    """P-E4c 起直接造运行单元(角色归 qi-agents,core 不再需要 agent.md)。"""
    catalog = _catalog()
    names = list(tools) if tools else sorted(catalog.names)
    spec = RunSpec(name="w",
                   prompt=build_system_prompt(None, tools=catalog.resolve(names)),
                   tools=names)
    ctx = ToolContext(agent_name=spec.name, workdir=tmp_path)
    return AgentRunner(spec, catalog, llm, settings, tool_ctx=ctx)


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
    """runner 里不再有任何"上限数字",也不再套回合级请求超时(都对齐 pi)。"""
    assert RunnerSettings().stop_after is None            # 默认不限
    assert not hasattr(RunnerSettings(), "max_turns")     # 数字字段已彻底移除
    assert not hasattr(RunnerSettings(), "timeout_s")     # 回合级超时也已移除
    config = RuntimeConfig(workdir=Path("."))
    assert not hasattr(config, "max_turns")
    assert not hasattr(config, "timeout_s")
    assert stop_after_turns(3)(2) is False
    assert stop_after_turns(3)(3) is True


def test_request_timeout_lives_at_the_provider_layer():
    """超时/重试归 provider SDK(pi 的 `retry.provider`),不再是 agent loop 的事。"""
    from qi_agent.llm import provider_retry_params

    assert provider_retry_params(None) == {}
    assert provider_retry_params({"enabled": True}) == {}
    assert provider_retry_params({"provider": {"timeoutMs": 30000, "maxRetries": 2}}) == {
        "timeout": 30.0, "num_retries": 2}
    # 脏配置不往下游塞:负数、布尔、非字典一律忽略
    assert provider_retry_params({"provider": {"timeoutMs": -1}}) == {}
    assert provider_retry_params({"provider": {"maxRetries": True}}) == {}
    assert provider_retry_params({"provider": "nope"}) == {}


def test_qi_itself_imposes_no_turn_cap():
    """对齐 pi:qi 自己的运行时默认**不设**轮次上限(钩子留着,但没人传)。

    pi 同形:`shouldStopAfterTurn` 定义在 pi-agent-core,pi-coding-agent 从不实现它。
    """
    import inspect

    assert inspect.signature(QiRuntime.__init__).parameters["stop_after"].default is None
    # 生产代码里不该出现“谁给 qi 自己安了个上限”的调用
    import qi_agent.cli as cli_mod
    import qi_agent.runtime as runtime_mod

    for module in (cli_mod, runtime_mod):
        source = inspect.getsource(module)
        assert "stop_after=stop_after_turns(" not in source, module.__name__


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
                   session_store=sessions, llm=StreamingLLM(["半截", "回答"], hang=True))
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


# ── 4. 工具层响应中断 ────────────────────────────────────

def _no_orphan(pattern: str) -> None:
    """该进程应该已经不在(用独特命令名避免和别的测试撞上)。"""
    import subprocess

    found = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.strip()
    assert not found, f"逃逸的进程: {found}"


@pytest.mark.asyncio
async def test_task_cancel_reaps_child(tmp_path):
    """**SIGINT(Ctrl-C)那条路径也不能留孤儿**。

    Ctrl-C 不经过协作信号:Python 默认转 KeyboardInterrupt → asyncio 取消主任务 →
    `run_shell` 的 `await` 直接被 CancelledError 打断。旧实现的 finally 只取消 watcher,
    子进程(独立进程组)就没人管了。
    """
    from qi_agent.tools import _bash

    marker = "sleep 31337"
    ctx = ToolContext(agent_name="t", workdir=tmp_path)
    task = asyncio.ensure_future(
        _bash({"command": f"{marker} & echo 起; wait", "timeout": 60}, ctx))
    await asyncio.sleep(0.3)                       # 命令已经跑起来
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    _no_orphan(marker)


@pytest.mark.asyncio
async def test_kill_live_children_reaps_from_outside(tmp_path):
    """信号处理器要能**同步**回收在跑的命令(那里不能 await)。"""
    from qi_agent.tools import _bash
    from qi_agent.tools.shell import _LIVE_CHILDREN, kill_live_children

    marker = "sleep 31338"
    ctx = ToolContext(agent_name="t", workdir=tmp_path)
    task = asyncio.ensure_future(
        _bash({"command": f"{marker} & echo 起; wait", "timeout": 60}, ctx))
    await asyncio.sleep(0.3)
    assert _LIVE_CHILDREN, "在跑的命令应该被登记(否则信号来了找不到它)"

    kill_live_children()
    outcome = await asyncio.wait_for(task, timeout=5)      # 被杀 → 很快返回
    assert outcome.exit_code not in (0, None)
    await asyncio.sleep(0.2)
    _no_orphan(marker)
    assert not _LIVE_CHILDREN, "杀完要除名,否则 pid 复用时会误杀"


# ── 5. 退出信号(对齐 pi 的 print 模式)────────────────────

def test_exit_code_matches_pi():
    """128 + 信号号:pi 的 print 模式用 143(SIGTERM)/129(SIGHUP)。

    `SIGHUP` / `SIGUSR1` 用 `getattr` 取:Windows 上没有这两个名字,直接写属性
    访问点会让类型检查器(以及运行期)在该平台报错。
    """
    import signal as signal_mod

    from qi_agent.cli import _exit_code_for

    assert _exit_code_for(signal_mod.SIGTERM) == 143
    sighup = getattr(signal_mod, "SIGHUP", None)
    if sighup is not None:                     # Windows 没有 SIGHUP
        assert _exit_code_for(sighup) == 129
    other = getattr(signal_mod, "SIGUSR1", None) or signal_mod.SIGBREAK
    assert _exit_code_for(other) == 128 + other


def test_windows_has_no_sighup_but_still_imports():
    """Windows 的 signal 没有 SIGHUP:注册表与注册点都不能在 import 期崩掉。"""
    import signal as signal_mod

    import qi_agent.cli as cli_mod

    assert cli_mod._EXIT_CODE_BY_SIGNAL[signal_mod.SIGTERM] == 143
    sighup = getattr(signal_mod, "SIGHUP", None)
    if sighup is not None:
        assert cli_mod._EXIT_CODE_BY_SIGNAL[sighup] == 129
        assert cli_mod._exit_signal_numbers() == [signal_mod.SIGTERM, sighup]
        return
    assert cli_mod._SIGHUP is None, "探测不到就不该留下名字(否则注册时会 AttributeError)"
    assert cli_mod._exit_signal_numbers() == [signal_mod.SIGTERM]


def _installed_handler(sig: int) -> Callable[[int, Any], None]:
    """取当前信号处理器并断言可调用(typeshed 的返回是 `Handlers | Callable | None`)。"""
    import signal as signal_mod
    from typing import cast

    handler = signal_mod.getsignal(sig)
    assert callable(handler)
    return cast("Callable[[int, Any], None]", handler)


def test_exit_signal_handlers_install_and_restore():
    """装上 SIGTERM/SIGHUP 处理器,并能恢复原样(测试/嵌入方要干净收尾)。"""
    import signal as signal_mod

    from qi_agent.cli import _install_exit_signal_handlers

    before = signal_mod.getsignal(signal_mod.SIGTERM)
    restore = _install_exit_signal_handlers()
    try:
        handler = _installed_handler(signal_mod.SIGTERM)
        assert handler is not before
        with pytest.raises(SystemExit) as excinfo:
            handler(signal_mod.SIGTERM, None)                  # 直接当普通函数调用
        assert excinfo.value.code == 143
    finally:
        restore()
    assert signal_mod.getsignal(signal_mod.SIGTERM) is before


def test_run_headless_maps_ctrl_c_to_130():
    """Ctrl-C:不打 traceback、退出码 130(与 143/129 同属 128+n);信号处理器要恢复。"""
    import signal as signal_mod

    import typer

    from qi_agent.cli import _run_headless

    before = signal_mod.getsignal(signal_mod.SIGTERM)

    async def boom() -> None:
        raise KeyboardInterrupt

    with pytest.raises(typer.Exit) as excinfo:
        _run_headless(boom())
    assert excinfo.value.exit_code == 130
    assert signal_mod.getsignal(signal_mod.SIGTERM) is before       # finally 里恢复


@pytest.mark.asyncio
async def test_sigterm_handler_reaps_running_command(tmp_path):
    """端到端:SIGTERM 处理器真的会把在跑的命令组带走(无头被 kill 时的兜底)。"""
    import signal as signal_mod

    from qi_agent.cli import _install_exit_signal_handlers
    from qi_agent.tools import _bash
    from qi_agent.tools.shell import _LIVE_CHILDREN

    marker = "sleep 31339"
    ctx = ToolContext(agent_name="t", workdir=tmp_path)
    task = asyncio.ensure_future(
        _bash({"command": f"{marker} & echo 起; wait", "timeout": 60}, ctx))
    await asyncio.sleep(0.3)
    assert _LIVE_CHILDREN

    restore = _install_exit_signal_handlers()
    handler = _installed_handler(signal_mod.SIGTERM)
    try:
        with pytest.raises(SystemExit):
            handler(signal_mod.SIGTERM, None)
    finally:
        restore()

    await asyncio.wait_for(task, timeout=5)
    await asyncio.sleep(0.2)
    _no_orphan(marker)


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
