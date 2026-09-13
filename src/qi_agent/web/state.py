"""宿主进程内的共享状态:**每 cwd 一个 QiRuntime** + **每会话一个活跃 run**。

为什么需要这两件事:

* `QiRuntime.__init__` 要装载 config / settings / plugins / agents / skills,代价高,
  而且**绑定一个 cwd**(项目级 `.qi` 的发现靠它)→ 按 cwd 缓存(LRU)。
* `QiRuntime.stream()` 会往同一个会话文件追加 → **必须每会话串行**,否则 JSONL 交错。
  因此"一会话一活跃 run",第二个请求返回 409(而不是排队后静默乱序)。

事件日志(`Run.events`)是**追加式**的:断线重连的客户端带上 `from=<seq>` 就能补齐,
这与 docs/web.md §8.2 记的重连约定一致(逻辑 seq + 快照替换,而不是只靠 Last-Event-ID)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..models import AgentEvent
from ..runtime import QiRuntime
from ..session import Session, SessionStore

#: 建 runtime 的方式。默认就是 `QiRuntime` 本身;测试传替身工厂(接受 `cwd=` 关键字)。
RuntimeFactory = Callable[..., QiRuntime]

MAX_RUNTIMES = 8          # 按 cwd 缓存的 runtime 上限(LRU 淘汰)
MAX_RUN_EVENTS = 5000     # 单个 run 保留的事件上限(供重连回放)
MAX_RUNS_KEPT = 20        # 保留的 run 条数:完成的 run 仍可被 `?run_id=` 重放
HEARTBEAT_S = 15.0        # SSE 心跳间隔(浏览器与反代都会掐掉静默连接)


class RunBusy(Exception):
    """同一会话已有活跃 run(→ HTTP 409)。"""

    def __init__(self, run_id: str):
        super().__init__("该会话已有活跃 run")
        self.run_id = run_id


def frame(kind: str, payload: dict, seq: int | None = None) -> str:
    """SSE 帧。`event:` 用事件 kind;`id:` 用 seq(客户端可据此续传)。

    `seq=None` 时不发 `id:` 行——给**快照**用:快照是"一代"的起点而不是事件,
    不能与 run 的 seq 0 撞号,否则客户端会把快照当成 seq 0 的事件。
    """
    body = json.dumps(payload, ensure_ascii=False)
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {kind}\ndata: {body}\n\n"


def comment(text: str) -> str:
    return f": {text}\n\n"


@dataclass
class Run:
    """一轮用户输入的执行状态(含可供重连回放的事件日志)。"""

    run_id: str
    session_id: str
    session: Session
    events: list[dict] = field(default_factory=list)
    done: bool = False
    error: str | None = None
    task: asyncio.Task | None = None
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    # ── 写入(由驱动任务调用) ──
    def append(self, kind: str, payload: dict) -> None:
        seq = len(self.events)
        self.events.append({"seq": seq, "kind": kind, "payload": payload})
        if len(self.events) > MAX_RUN_EVENTS:      # 超长 run:丢最老的,保住 seq 单调
            del self.events[: len(self.events) - MAX_RUN_EVENTS]
        self._changed.set()

    def append_event(self, ev: AgentEvent) -> None:
        self.append(ev.kind, {"kind": ev.kind, "agent": ev.agent, "tool": ev.tool,
                              "text": ev.text, "data": ev.data})

    def finish(self, status: str, error: str | None = None) -> None:
        self.done = True
        self.error = error
        self.append("run.finished", {"kind": "run.finished", "status": status,
                                     "error": error, "session_id": self.session_id,
                                     "run_id": self.run_id})
        self._changed.set()

    # ── 读取(由 SSE 端点调用) ──
    @property
    def first_seq(self) -> int:
        return self.events[0]["seq"] if self.events else 0

    def items_from(self, from_seq: int):
        """产出 seq >= from_seq 的事件项;`from_seq` 早于保留窗口时从窗口头开始。"""
        for item in self.events:
            if item["seq"] >= from_seq:
                yield item

    def next_seq(self) -> int:
        return self.events[-1]["seq"] + 1 if self.events else 0

    async def wait_more(self, seen: int, timeout: float = HEARTBEAT_S) -> bool:
        """等到出现 seq >= seen 的新事件或 run 结束。

        返回 **False 表示超时**:调用方应发一个心跳注释帧(而不是断开连接)。
        """
        deadline = time.monotonic() + timeout
        while not self.done and self.next_seq() <= seen:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._changed.clear()
            if self.done or self.next_seq() > seen:   # clear 与检查之间可能已更新
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)
        return True


class WebState:
    """挂在 `app.state.web` 上的宿主状态。"""

    def __init__(self, default_cwd: Path, runtime_factory: RuntimeFactory = QiRuntime):
        self.default_cwd = Path(default_cwd).resolve()
        self._runtime_factory = runtime_factory
        self._runtimes: dict[str, QiRuntime] = {}
        self._runs: dict[str, Run] = {}

    # ── runtime 缓存 ──
    def runtime_for(self, cwd: Path | str | None) -> QiRuntime:
        key = str(Path(cwd).resolve()) if cwd else str(self.default_cwd)
        if key not in self._runtimes:
            if len(self._runtimes) >= MAX_RUNTIMES:      # LRU:最久未用的先走
                self._runtimes.pop(next(iter(self._runtimes)))
            self._runtimes[key] = self._runtime_factory(cwd=Path(key))
        else:                                            # 命中即刷新 LRU 顺序
            self._runtimes[key] = self._runtimes.pop(key)
        return self._runtimes[key]

    @property
    def sessions(self) -> SessionStore:
        """默认 cwd 的会话仓库(settings.json 的 sessionDir 覆盖也走它)。"""
        return self.runtime_for(self.default_cwd).sessions

    def session_cwd(self, session: Session) -> Path:
        return Path(session.cwd) if session.cwd else self.default_cwd

    # ── run 注册表 ──
    def active_run(self, session_id: str) -> Run | None:
        run = self._runs.get(session_id)
        return run if run and not run.done else None

    def run_by_id(self, run_id: str) -> Run | None:
        """按 run_id 查(含**已完成**的):客户端晚连时仍可重放整轮事件。"""
        for run in self._runs.values():
            if run.run_id == run_id:
                return run
        return None

    def recent_run(self, session_id: str) -> Run | None:
        """该会话最近一次 run(含已完成的)。"""
        return self._runs.get(session_id)

    def start_run(self, session: Session, text: str, agent: str | None) -> Run:
        existing = self.active_run(session.id)
        if existing is not None:
            raise RunBusy(existing.run_id)
        run = Run(run_id=uuid.uuid4().hex[:12], session_id=session.id, session=session)
        self._runs[session.id] = run          # 同一会话只留最新一个 run
        if len(self._runs) > MAX_RUNS_KEPT:   # 淘汰最老的已完成 run
            for sid, old in list(self._runs.items()):
                if old.done:
                    del self._runs[sid]
                    break
        run.task = asyncio.create_task(self._drive(run, text, agent))
        return run

    async def _drive(self, run: Run, text: str, agent: str | None) -> None:
        """把 `runtime.stream()` 的事件推进日志;异常转成 error 帧而不是断连。

        (流式**已吐出部分文本后**失败时,AgentRunner 会直接抛出——宿主在这里兜住它,
        否则客户端只会看到连接莫名断开。见 docs/web.md §13 的已知限制。)
        """
        try:
            runtime = self.runtime_for(self.session_cwd(run.session))
            async for ev in runtime.stream(text, run.session, agent_override=agent):
                run.append_event(ev)
            run.finish("ok")
        except asyncio.CancelledError:                    # 显式取消(客户端 Stop)
            run.finish("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 任何 LLM/工具异常都转成 error 帧
            run.append("error", {"kind": "error", "agent": None, "tool": None,
                                 "text": f"{type(exc).__name__}: {exc}", "data": {}})
            run.finish("error", error=f"{type(exc).__name__}: {exc}")

    def cancel_run(self, session_id: str) -> bool:
        run = self.active_run(session_id)
        if run is None or run.task is None:
            return False
        run.task.cancel()
        return True
