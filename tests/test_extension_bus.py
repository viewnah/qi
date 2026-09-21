"""P-E1d:事件总线(中间件链语义)+ `ctx` 最小集 + `session_start` 端到端。

为什么单测语义词:§4 那几条规则(顺序 / patch 链 / 失败隔离 / fail-safe)是**契约**。
它们一旦被"顺手优化"掉,表现是"某个扩展偶尔不生效"或"装了闸门反而放行",
两种都很难从现象反推到根因 —— 所以用测试钉住。

端到端那几条用**真实 QiRuntime**(配最小 models/settings),不是假运行时:
装载路径(发现 → register(api) → api.on → runtime.start_session → 派发)整条链
只有真的跑一次才证明通了。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.extensions import (  # noqa: E402
    ExtensionBus,
    ExtensionContext,
    EmitResult,
)
from qi_agent.registry import EXTENSION_ENTRY_FILE  # noqa: E402


def _ctx(**kw) -> ExtensionContext:
    return ExtensionContext(cwd=kw.pop("cwd", Path("/tmp")), **kw)


# ── 注册与顺序 ──────────────────────────────────────────

async def test_handlers_run_in_registration_order():
    bus = ExtensionBus()
    seen: list[str] = []

    def a(payload, ctx):
        seen.append("a")

    def b(payload, ctx):
        seen.append("b")

    bus.on("x", a, source="first")
    bus.on("x", b, source="second")
    await bus.emit("x", {}, ctx=_ctx())

    assert seen == ["a", "b"]


async def test_side_effects_keep_registration_order_even_when_async():
    """同步/异步 handler 混排时顺序仍然是注册序(链是串行的,不是 gather)。

    如果哪天改成并发派发,`before_agent_start` 的 systemPrompt 链会立刻变成竞态。
    """
    bus = ExtensionBus()
    seen: list[str] = []

    async def slow(payload, ctx):
        seen.append("slow-start")
        await _noop()
        seen.append("slow-end")

    def fast(payload, ctx):
        seen.append("fast")

    bus.on("x", slow)
    bus.on("x", fast)
    await bus.emit("x", {}, ctx=_ctx())
    assert seen == ["slow-start", "slow-end", "fast"]


async def _noop() -> None:
    return None


async def test_handler_registered_during_dispatch_does_not_run_now():
    """快照遍历:handler 里再 on() 不影响本次派发。

    否则"中间件自注册"会让派发越跑越多(甚至无限)。
    """
    bus = ExtensionBus()
    seen: list[str] = []

    def self_registering(payload, ctx):
        seen.append("outer")
        bus.on("x", lambda p, c: seen.append("inner"))

    bus.on("x", self_registering)
    await bus.emit("x", {}, ctx=_ctx())
    assert seen == ["outer"]

    await bus.emit("x", {}, ctx=_ctx())      # 下一次派发它才在
    assert seen == ["outer", "outer", "inner"]


# ── patch 链 ────────────────────────────────────────────

async def test_dict_returns_are_patched_and_chained():
    bus = ExtensionBus()
    bus.on("x", lambda p, c: {"text": p["text"] + "-a"})
    bus.on("x", lambda p, c: {"text": p["text"] + "-b"})

    out = await bus.emit("x", {"text": "start"}, ctx=_ctx())
    assert out.payload["text"] == "start-a-b"
    assert out.stopped_by is None


async def test_in_place_mutation_is_visible_to_later_handlers():
    """原地改 payload 对后续可见(对齐 pi 的 `event.input` 可原地改)。"""
    bus = ExtensionBus()
    bus.on("x", lambda p, c: p.update({"n": p["n"] + 1}))
    bus.on("x", lambda p, c: p.update({"n": p["n"] * 10}))

    assert (await bus.emit("x", {"n": 1}, ctx=_ctx())).payload["n"] == 20


async def test_unknown_return_values_are_ignored_not_errors():
    """扩展可能比宿主新:不认识的返回值忽略,不报错(§4)。"""
    bus = ExtensionBus()
    bus.on("x", lambda p, c: "我返回了个字符串")
    bus.on("x", lambda p, c: ["列表"])
    bus.on("x", lambda p, c: None)

    out = await bus.emit("x", {}, ctx=_ctx())
    assert out.errors == []
    assert out.returns == ["我返回了个字符串", ["列表"], None]


# ── 失败隔离 ────────────────────────────────────────────

async def test_one_handler_crashing_does_not_stop_the_chain():
    bus = ExtensionBus()
    seen: list[str] = []

    def boom(payload, ctx):
        raise RuntimeError("炸了")

    bus.on("x", boom, source="bad-ext")
    bus.on("x", lambda p, c: seen.append("after"))

    out = await bus.emit("x", {}, ctx=_ctx())
    assert seen == ["after"]                      # 后面的照跑
    assert [s for s, _ in out.errors] == ["bad-ext"]
    assert isinstance(out.errors[0][1], RuntimeError)
    assert out.ok is False


async def test_async_handler_crash_is_also_isolated():
    bus = ExtensionBus()

    async def boom(payload, ctx):
        raise ValueError("异步炸了")

    bus.on("x", boom)
    out = await bus.emit("x", {}, ctx=_ctx())
    assert len(out.errors) == 1


async def test_fail_safe_result_on_error_stops_with_the_safe_verdict():
    """`tool_call` 这类拦截器的错误兜底:**拦住**而不是放行。

    放行等于"装了闸门反而更不安全" —— 所以给 on_error_result 时,出错即裁决。
    """
    bus = ExtensionBus()
    seen: list[str] = []

    def boom(payload, ctx):
        raise RuntimeError("闸门自己崩了")

    bus.on("tool_call", boom, source="gate")
    bus.on("tool_call", lambda p, c: seen.append("later"))

    out = await bus.emit_until("tool_call", {"tool": "bash"}, ctx=_ctx(),
                               stop_keys=("block",),
                               on_error_result={"block": True, "reason": "闸门异常"})
    assert out.result == {"block": True, "reason": "闸门异常"}
    assert out.stopped_by == "gate"
    assert seen == []                             # fail-safe 直接定案,不再往下走


# ── 裁决型派发(emit_until)──────────────────────────────

async def test_first_verdict_wins_and_stops_the_chain():
    bus = ExtensionBus()
    bus.on("input", lambda p, c: {"action": "continue"})
    bus.on("input", lambda p, c: {"action": "handled"})
    bus.on("input", lambda p, c: {"action": "handled-again"})

    out = await bus.emit_until("input", {"text": "ping"}, ctx=_ctx(),
                               stop_values={"action": ("handled",)})
    assert out.result == {"action": "handled"}
    assert out.stopped_by == _handler_of(bus, 1)
    assert len(out.returns) == 2                  # 第三个没跑


async def test_falsy_verdict_does_not_stop():
    """`{block: False}` / `{handled: None}` 不算裁决 —— 只有真值才停。"""
    bus = ExtensionBus()
    bus.on("tool_call", lambda p, c: {"block": False})
    bus.on("tool_call", lambda p, c: {"block": True, "reason": "真拦"})

    out = await bus.emit_until("tool_call", {}, ctx=_ctx(), stop_keys=("block",))
    assert out.result == {"block": True, "reason": "真拦"}


async def test_transform_chain_survives_because_transform_is_not_a_verdict():
    """`action: transform` 不入 stop_values → 改写继续往下链(pi 的 input 语义)。

    这正是两种裁决形状必须分开的原因:同是 `action` 键,`transform` 链式、`handled` 定案。
    """
    bus = ExtensionBus()
    bus.on("input", lambda p, c: {"action": "transform", "text": p["text"] + "1"})
    bus.on("input", lambda p, c: {"action": "transform", "text": p["text"] + "2"})
    bus.on("input", lambda p, c: {"action": "transform", "text": p["text"] + "3"})

    out = await bus.emit_until("input", {"text": "q"}, ctx=_ctx(),
                               stop_values={"action": ("handled",)})
    assert out.payload["text"] == "q123"
    assert out.stopped_by is None


def _handler_of(bus: ExtensionBus, index: int) -> str:
    return bus._handlers["input"][index][0]        # noqa: SLF001 测试要看来源标签


# ── 零扩展 ──────────────────────────────────────────────

async def test_empty_bus_is_cheap_and_safe():
    bus = ExtensionBus()
    assert bus.is_empty is True

    out = await bus.emit("anything", {"a": 1}, ctx=_ctx())
    assert isinstance(out, EmitResult)
    assert out.payload["a"] == 1
    # pi 兼容层:任何事件都带 `type`(pi 的判别字段),原 payload 的键一个不少
    assert out.payload["type"] == "anything"
    assert out.returns == [] and out.errors == []

    bus.on("x", lambda p, c: None)
    assert bus.is_empty is False
    assert bus.events == ["x"]
    assert bus.handler_count("x") == 1


# ── ctx 最小集 ──────────────────────────────────────────

def test_extension_context_minimal_set():
    ctx = _ctx(model="deepseek/deepseek-chat", thinking_level="high", has_ui=True,
               project_trusted=False)
    assert ctx.model == "deepseek/deepseek-chat"
    assert ctx.thinking_level == "high"
    assert ctx.has_ui is True
    assert ctx.is_project_trusted() is False    # pi 的 ctx.isProjectTrusted() 同形
    assert ctx.signal is None


def test_extension_context_is_frozen_but_notes_are_shared():
    """frozen 管字段重绑定;`notes` 是宿主与扩展共享的**一个**列表(提示通道)。"""
    notes: list[str] = []
    ctx = ExtensionContext(cwd=Path("/tmp"), notes=notes)
    notes.append("扩展加的一句话")
    assert ctx.notes == ["扩展加的一句话"]
    with pytest.raises(Exception):
        ctx.cwd = Path("/elsewhere")           # type: ignore[misc]


# ── 端到端:真实 runtime 装载扩展并派发 session_start ──────

_MINIMAL_MODELS = json.dumps({
    "providers": {"ollama": {"api": "openai-completions",
                          "baseUrl": "http://127.0.0.1:11434/v1",
                          "models": [{"id": "x"}]}},
})


def _runtime_env(tmp_path: Path, monkeypatch) -> Path:
    """最小可跑的配置:一个 provider + 一个默认模型 + 空的 settings。"""
    (tmp_path / "models.json").write_text(_MINIMAL_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x"}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)
    return home


def _write_extension(root: Path, name: str, body: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(body, encoding="utf-8")
    return d


@pytest.mark.asyncio
async def test_extension_receives_session_start(tmp_path, monkeypatch):
    """一条链走完:发现 → register(api) → api.on → start_session → handler 被调用。"""
    _runtime_env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)          # 锚定项目根(信任门控按它定位)
    log = tmp_path / "seen.jsonl"
    _write_extension(paths.project_home(project) / paths.EXTENSIONS_DIR_NAME, "watcher", f"""
import json
from pathlib import Path

LOG = Path({str(log)!r})

def register(api):
    def on_start(payload, ctx):
        LOG.write_text(json.dumps({{
            "reason": payload["reason"],
            "session": payload["session"],
            "cwd": payload["cwd"],
            "ctx_cwd": str(ctx.cwd),
            "trusted": ctx.is_project_trusted(),
            "has_ui": ctx.has_ui,
            "model": ctx.model,
        }}, ensure_ascii=False), encoding="utf-8")
    api.on("session_start", on_start)
""")

    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True)
    assert runtime.extensions == ["watcher"]
    assert runtime.project_trusted is True

    session = runtime.sessions.create("t", cwd=project)
    await runtime.start_session(session)

    seen = json.loads(log.read_text(encoding="utf-8"))
    assert seen["reason"] == "startup"
    assert seen["session"] == session.id
    assert seen["cwd"] == str(project)
    assert seen["ctx_cwd"] == str(project)
    assert seen["trusted"] is True
    assert seen["has_ui"] is False          # headless 默认
    assert seen["model"] == "ollama/x"      # "provider/model"


@pytest.mark.asyncio
async def test_session_start_is_at_most_once_per_session(tmp_path, monkeypatch):
    """同一 runtime 对同一会话只发一次:否则"开一次资源"会变成开 N 次。"""
    _runtime_env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    counter = tmp_path / "count.txt"
    _write_extension(paths.project_home(project) / paths.EXTENSIONS_DIR_NAME, "counter", f"""
from pathlib import Path

COUNT = Path({str(counter)!r})

def register(api):
    def on_start(payload, ctx):
        n = int(COUNT.read_text() or "0") if COUNT.exists() else 0
        COUNT.write_text(str(n + 1))
    api.on("session_start", on_start)
""")

    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True)
    session = runtime.sessions.create("t", cwd=project)
    await runtime.start_session(session)
    await runtime.start_session(session)            # 再来一次
    assert counter.read_text() == "1"

    other = runtime.sessions.create("t2", cwd=project)
    await runtime.start_session(other)              # 另一个会话 → 该发
    assert counter.read_text() == "2"


@pytest.mark.asyncio
async def test_broken_handler_becomes_a_note_not_a_crash(tmp_path, monkeypatch):
    """扩展的 session_start 抛异常 → 进 notes(界面上看得见),会话照常。"""
    _runtime_env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _write_extension(paths.project_home(project) / paths.EXTENSIONS_DIR_NAME, "broken", """
def register(api):
    def on_start(payload, ctx):
        raise RuntimeError("我坏了")
    api.on("session_start", on_start)
""")

    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True)
    session = runtime.sessions.create("t", cwd=project)
    await runtime.start_session(session)            # 不抛
    assert any("broken" in note and "session_start" in note for note in runtime.notes)


@pytest.mark.asyncio
async def test_untrusted_project_extension_never_receives_events(tmp_path, monkeypatch):
    """未信任 → 扩展没被装载 → 连 handler 都不存在(而不是装载了但不派发)。"""
    _runtime_env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    marker = tmp_path / "should-not-exist"
    _write_extension(paths.project_home(project) / paths.EXTENSIONS_DIR_NAME, "probe", f"""
from pathlib import Path

def register(api):
    Path({str(marker)!r}).write_text("register 被执行了")
""")

    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=False)
    assert runtime.extensions == []
    assert not marker.exists()
    assert any("未信任" in note for note in runtime.notes)
