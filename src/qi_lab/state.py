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

import asyncio
import inspect
import weakref
from pathlib import Path
from typing import Any, Callable

from .core import QiRuntime, Session, SessionStore

from . import activity, bundled

#: 建 runtime 的方式。默认就是 `QiRuntime` 本身;测试传替身工厂(接受 `cwd=` 关键字)。
RuntimeFactory = Callable[..., Any]  # 测试替身允许鸭子类型
MAX_RUNTIMES = 8          # 按 cwd 缓存的 runtime 上限(LRU 淘汰)


def _default_runtime_factory(cwd: Path | str) -> QiRuntime:
    """默认工厂:把 qi-lab 的内置扩展作为目录通道交给 core。

    外部已声明的同名扩展由 `bundled.extension_paths()` 过滤掉,所以这里不用再
    区分“内置 / 外部”—— core 会按它自己的装载顺序去发现外部版本。
    """
    root = Path(cwd)
    return QiRuntime(cwd=root, extra_extension_paths=bundled.extension_paths(root))


class RunBusy(Exception):
    """同一会话已有活跃 run(→ HTTP 409)。"""

    def __init__(self, run_id: str = ""):
        super().__init__("该会话已有活跃 run")
        self.run_id = run_id


class WebState:
    """挂在 `app.state.web` 上的宿主状态。"""

    def __init__(self, default_cwd: Path, runtime_factory: RuntimeFactory | None = None):
        self.default_cwd = Path(default_cwd).resolve()
        self._runtime_factory = runtime_factory or _default_runtime_factory
        # 适配层可能在第一个 runtime 建立之前就 import 内置扩展;先把生效的目录放进 path。
        bundled.ensure_sys_path(self.default_cwd)
        self._runtimes: dict[str, QiRuntime] = {}
        self._busy: set[str] = set()
        # ── 委派直播(见 activity.py)──
        # 每个 AG-UI 流开一条队列;总线 handler 把直播内容**广播**给所有活跃队列。
        # 订了哪个 runtime 用 WeakSet 记:总线只有 `on` 没有 `off`,而 runtime 是
        # LRU 缓存 —— 强引用会把淘汰掉的 runtime 钉住,id 又可能被复用误判已订。
        self._activity: list[asyncio.Queue] = []
        self._activity_subs: weakref.WeakSet = weakref.WeakSet()

    # ── runtime 缓存 ──
    def runtime_for(self, cwd: Path | str | None) -> QiRuntime:
        key = str(Path(cwd).resolve()) if cwd else str(self.default_cwd)
        if key not in self._runtimes:
            if len(self._runtimes) >= MAX_RUNTIMES:      # LRU:最久未用的先走
                self._runtimes.pop(next(iter(self._runtimes)))
            self._runtimes[key] = self._runtime_factory(cwd=Path(key))
        else:                                            # 命中即刷新 LRU 顺序
            self._runtimes[key] = self._runtimes.pop(key)
        runtime = self._runtimes[key]
        self._subscribe_activity(runtime)
        return runtime

    # ── 委派直播队列 ──
    def _subscribe_activity(self, runtime: QiRuntime) -> None:
        """把直播 handler 挂上 runtime 的总线(每个 runtime 只挂一次)。

        挂不上只是没有直播(读音:总线不可用、runtime 不可弱引用)—— 不影响任何
        既有能力,所以这里不往上抛。
        """
        if runtime in self._activity_subs:
            return
        try:
            runtime.bus.on("tool_execution_update", self._on_activity,
                           source="qi-lab")
        except Exception:  # noqa: BLE001 直播是旁路:失败不该影响读接口
            return
        self._activity_subs.add(runtime)

    def _on_activity(self, payload: Any, ctx: Any) -> None:
        """总线 handler:过滤出委派直播内容,广播给活跃流的队列。

        **广播而不是归因**:payload 里没有会话信息(runtime 按 cwd 缓存,同 cwd
        的多个会话共用一个),而每个流拿到内容后按 `toolCallId` 归卡 —— 不是本流
        的 id,前端直接丢弃,无害。没有活跃队列时连过滤都不做(零订阅零开销,
        与宿主 `bus.has` 的护栏同向)。
        """
        if not self._activity:
            return
        content = activity.activity_content(payload)
        if content is None:
            return
        for queue in list(self._activity):
            queue.put_nowait((activity.KIND_ACTIVITY, content))

    def open_activity(self) -> asyncio.Queue:
        """开一条直播队列(每个 AG-UI 流一个);流结束必须 `close_activity`。"""
        queue: asyncio.Queue = asyncio.Queue()
        self._activity.append(queue)
        return queue

    def close_activity(self, queue: asyncio.Queue) -> None:
        """收掉一条直播队列。必须在 finally:泄漏的队列会让广播越推越多。"""
        try:
            self._activity.remove(queue)
        except ValueError:                 # 已经收过了(幂等)
            pass

    @property
    def sessions(self) -> SessionStore:
        """默认 cwd 的会话仓库(settings.json 的 sessionDir 覆盖也走它)。"""
        return self.runtime_for(self.default_cwd).sessions

    def session_cwd(self, session: Session) -> Path:
        return Path(session.cwd) if session.cwd else self.default_cwd

    # ── 模型配置/凭证 ──
    def refresh_config(self) -> None:
        """重读各 runtime 的 `models.json`(新建 provider 之后调)。

        runtime 在 `__init__` 里读一次配置就不再动 —— 不重读的话 `/api/config` 与
        `/api/models` 会说“没这个 provider”,而文件里已经有了。逐 runtime 按自己的
        cwd 重读(项目级 models.json 也可能不同)。

        重读失败**保留旧配置**:文件已经写成了,把它报成 500 只会让调用方重试。
        """
        from .core import load_config

        for runtime in list(self._runtimes.values()):
            try:
                runtime.cfg, runtime.config_files = load_config(
                    getattr(runtime, "cwd", self.default_cwd))
            except Exception:            # noqa: BLE001 坏配置不该把写操作报成失败
                continue

    def refresh_credentials(self) -> None:
        """凭证变了(登录 / 退出 / 新建 provider)后重建各 runtime 的客户端。

        `LiteLLMClient` 在构造时就把 key 解析好了 —— 不重建的话,用户以为登上了,
        请求还是拿旧 key / 空客户端去跑。与 TUI 登录后调 `reload_credentials()` 同一条。
        """
        for runtime in list(self._runtimes.values()):
            reload = getattr(runtime, "reload_credentials", None)
            if not callable(reload):
                continue
            try:
                reload()
            except Exception:            # noqa: BLE001 重建失败保持旧客户端
                continue

    def refresh_skills(self) -> None:
        """技能增删导入之后重扫(runtime 缓存的那份不重扫,新技能要**重启进程**才生效)。

        为什么必须做:runtime 按 cwd 缓存,`top_skills` 在 `__init__` 里读一次就不再动 ——
        于是会出现最难查的一种不一致:*GET `/api/skills` 说它在*(那是直接读磁盘),
        *模型却用不上*(提示词取自 `runtime.top_skills`)。

        为什么不调 `QiRuntime.reload()`:那条路还会派发 `resources_discover`、
        `session_start` 并重启活动会话 —— 一个几十行的 SKILL.md 不该惊动扩展。
        这里只重扫技能,口径与 `reload()` 一致(`enabled` 只看 `settings.skillsEnabled`)。

        重扫失败**保留旧清单**:那次写已经落盘了,把它报成 500 只会让调用方重试
        (然后撞上自己的 409)。测试替身没有技能槽就不动。
        """
        from .core import load_top_level_skills

        for runtime in list(self._runtimes.values()):
            settings = getattr(runtime, "settings", None)
            if settings is None or not hasattr(runtime, "top_skills"):
                continue
            try:
                runtime.top_skills = load_top_level_skills(
                    getattr(runtime, "cwd", self.default_cwd), settings,
                    list(getattr(runtime, "_extra_skill_paths", ())),
                    enabled=getattr(settings, "skillsEnabled", True))
            except Exception:            # noqa: BLE001 坏技能目录不该把写操作报成失败
                continue

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
