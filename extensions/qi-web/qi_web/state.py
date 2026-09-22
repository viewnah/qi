"""宿主进程内的共享状态:**每 cwd 一个 QiRuntime** + **会话级串行闸门**。

两条为什么需要:

* `QiRuntime.__init__` 要装载 config / settings / extensions / agents / skills,代价高,
  而且**绑定一个 cwd**(项目级 `.qi` 的发现靠它)→ 按 cwd 缓存(LRU)。
* `QiRuntime.stream()` 会往同一个会话文件追加 → **必须每会话串行**,否则 JSONL 交错。

**与 AG-UI 改造前的变化**:宿主不再自己维护"每会话一个 run + 追加式事件日志 + seq"
那套东西。改成 AG-UI 的**单次 POST、响应即流**之后:

* 事件不再需要缓冲给"晚连上来的客户端"——因为流就是这次请求的响应,没有"晚连"这回事;
* 于是 `Run` 类、`seq`、`?from=`、`?run_id=` 重放全部消失(见 design/web.md 的取舍记录);
* 只留下真正还需要的东西:一个**忙闲闸门**(防两个 POST 同时往一个会话追加)、
  以及 cwd→runtime 的缓存。

取消也变简单了:客户端 abort 请求 → ASGI 侧取消生成器 → `runtime.stream()` 被
`CancelledError` 打断。不再需要 `POST /cancel` 与后台 task。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Callable

from qi_agent.runtime import QiRuntime
from qi_agent.session import Session, SessionStore

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

    # ── 会话绑定 ──
    def bind_session(self, runtime: QiRuntime, session: Session) -> None:
        """**同步**把会话绑到 runtime 上(`bind_session`,不含事件)。

        单独抽出来是因为有两个调用方:AG-UI 每一轮开跑前的 `bind()`(它还额外派发
        `session_start`),以及 `/model` 这类**只改设置**的端点 —— 后者要做的事只有一件:
        让 `model_change` / `thinking_level_change` 知道该写进哪个文件。
        派发 `session_start` 是"这个扩展在这个会话上开工"的信号,换一次模型不该顺手发它。

        用 getattr 取:测试替身(runtime_factory)不一定有 `bind_session`,
        而缺一个不该让整个请求 500。
        """
        binder = getattr(runtime, "bind_session", None)
        if callable(binder):
            binder(session)

    async def bind(self, runtime: QiRuntime, session: Session, reason: str = "startup") -> None:
        """把会话绑到 runtime 上,**并**告知扩展"会话已绑定"(`session_start`)。

        为什么 web 必须显式做这件事:一个 runtime 服务同一 cwd 下的**多个**会话
        (`runtime_for` 按 cwd 缓存),而 `_active_session` 只在一个回合内有效 ——
        于是"换模型/级别该写进哪个文件"与"续会话按 entry 还原"这两件事都没有落点。
        CLI 与 TUI 各自在自己那一处调 `bind_session` + `start_session`,web 此前**一处都没有**,
        所以:web 端的 `/model` 不落盘、续会话不还原、`session_start` 从不派发 ——
        而 qi-mcp 的**直连工具**正挂在那个事件上(`directTools` 在 web 端从未注册过)。

        两个方法都用 getattr 取:测试替身(runtime_factory)不一定两个都有,
        而缺一个不该让整轮跑不起来。`bind_session` 先、`start_session` 后:
        事件处理器在 `session_start` 里就会读 `ctx.model`,那时必须已经是这个会话的值。
        """
        self.bind_session(runtime, session)
        starter = getattr(runtime, "start_session", None)
        if not callable(starter):
            return
        # 同一 runtime 对同一会话本来就幂等(宿主自己记账),这里不必再记一遍。
        result = starter(session, reason=reason)
        if inspect.isawaitable(result):
            await result                                   # 同步实现也认(替身常见)
