"""宿主进程内的共享状态:**每 cwd 一个 QiRuntime** + **会话级串行闸门**。

两条为什么需要:

* `QiRuntime.__init__` 要装载 config / settings / plugins / agents / skills,代价高,
  而且**绑定一个 cwd**(项目级 `.qi` 的发现靠它)→ 按 cwd 缓存(LRU)。
* `QiRuntime.stream()` 会往同一个会话文件追加 → **必须每会话串行**,否则 JSONL 交错。

**与 AG-UI 改造前的变化**:宿主不再自己维护"每会话一个 run + 追加式事件日志 + seq"
那套东西。改成 AG-UI 的**单次 POST、响应即流**之后:

* 事件不再需要缓冲给"晚连上来的客户端"——因为流就是这次请求的响应,没有"晚连"这回事;
* 于是 `Run` 类、`seq`、`?from=`、`?run_id=` 重放全部消失(见 docs/web.md 的取舍记录);
* 只留下真正还需要的东西:一个**忙闲闸门**(防两个 POST 同时往一个会话追加)、
  以及 cwd→runtime 的缓存。

取消也变简单了:客户端 abort 请求 → ASGI 侧取消生成器 → `runtime.stream()` 被
`CancelledError` 打断。不再需要 `POST /cancel` 与后台 task。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..runtime import QiRuntime
from ..session import Session, SessionStore

#: 建 runtime 的方式。默认就是 `QiRuntime` 本身;测试传替身工厂(接受 `cwd=` 关键字)。
RuntimeFactory = Callable[..., QiRuntime]

MAX_RUNTIMES = 8          # 按 cwd 缓存的 runtime 上限(LRU 淘汰)


class RunBusy(Exception):
    """同一会话已有活跃 run(→ HTTP 409)。"""

    def __init__(self, run_id: str = ""):
        super().__init__("该会话已有活跃 run")
        self.run_id = run_id


class WebState:
    """挂在 `app.state.web` 上的宿主状态。"""

    def __init__(self, default_cwd: Path, runtime_factory: RuntimeFactory = QiRuntime):
        self.default_cwd = Path(default_cwd).resolve()
        self._runtime_factory = runtime_factory
        self._runtimes: dict[str, QiRuntime] = {}
        self._busy: set[str] = set()

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

    # ── 会话级闸门 ──
    def is_busy(self, session_id: str) -> bool:
        return session_id in self._busy

    def begin(self, session_id: str) -> None:
        """占用会话。已在跑 → `RunBusy`(调用方转 409)。

        这是**唯一**并发控制:AG-UI 的流与这次 HTTP 请求同生共死,
        所以"占着"就等于"有一个请求正在流"。
        """
        if session_id in self._busy:
            raise RunBusy()
        self._busy.add(session_id)

    def end(self, session_id: str) -> None:
        """释放会话。必须放在 `finally`,否则一次异常就把会话永久卡住。"""
        self._busy.discard(session_id)
