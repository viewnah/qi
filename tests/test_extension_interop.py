"""P-E4b:扩展之间(`api.events`)+ `registerProvider`。

**为什么把 peer 消息与宿主事件分成两套**(即两套 API 而不是一套):
宿主事件是“qi 在通知你”,有链式返回值与裁决(`handled` / `block`);peer 消息只是
“扩展之间说一声”。混作一件事的话,迟早有人给宿主事件 emit 一条自定义消息
(然后奇怪为什么没人理),或者拿 peer 消息去拦工具调用(拦不住 —— 拦截必须走 `tool_call`)。

两套 API 但**同一个对象**(总线):总线本来就是“所有扩展共享的那一个”,而且它是必填参数。
另起一个对象迟早会出现“两个扩展各拿一个总线、消息静默发不到”。

`registerProvider` 的边界也在这里钉住:**只改内存,不写 `models.json`** ——
写盘会让“跑一次带代理的扩展”永久改变用户的模型配置,而那是个静默副作用。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.extensions import ExtensionBus  # noqa: E402
from qi_agent.registry import EXTENSION_ENTRY_FILE  # noqa: E402

_MODELS = json.dumps({
    "providers": {"ollama": {"api": "openai-completions", "models": [{"id": "x"}]}},
})
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)


def _install(tmp_path: Path, name: str, body: str) -> None:
    d = tmp_path / "proj" / ".qi" / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(body, encoding="utf-8")


def _runtime(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch)
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=project, disable_router=True, approve_project=True)


def _log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ── peer 消息 ───────────────────────────────────────────

_EMITTER = """
import json
from pathlib import Path

LOG = Path({log!r})


def register(api):
    def on_start(payload, ctx):
        api.events.emit("index:done", {{"files": 17}})
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({{"who": "emitter"}}) + "\\n")

    api.on("session_start", on_start)
"""

_LISTENER = """
import json
from pathlib import Path

LOG = Path({log!r})


def register(api):
    def on_message(data):
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({{"who": "listener", "data": data}}) + "\\n")

    api.events.on("index:done", on_message)
"""


@pytest.mark.asyncio
async def test_peer_message_reaches_another_extension(tmp_path, monkeypatch):
    """两个扩展通过 `api.events` 通信,而**不借助全局变量**。"""
    log = tmp_path / "peer.jsonl"
    _install(tmp_path, "a-emitter", _EMITTER.format(log=str(log)))
    _install(tmp_path, "b-listener", _LISTENER.format(log=str(log)))
    runtime = _runtime(tmp_path, monkeypatch)
    assert runtime.extensions == ["a-emitter", "b-listener"]

    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await runtime.start_session(session)               # 触发 emitter 的 session_start

    rows = _log(log)
    assert {"who": "emitter"} in rows
    assert {"who": "listener", "data": {"files": 17}} in rows


@pytest.mark.asyncio
async def test_emitting_an_event_nobody_listens_to_is_a_no_op(tmp_path, monkeypatch):
    log = tmp_path / "peer.jsonl"
    _install(tmp_path, "a-emitter", _EMITTER.format(log=str(log)))
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await runtime.start_session(session)               # 不该炸

    assert runtime.notes == []                         # 也不该记错误
    assert _log(log) == [{"who": "emitter"}]


@pytest.mark.asyncio
async def test_a_broken_peer_handler_does_not_stop_the_others(tmp_path, monkeypatch):
    """与宿主事件同一条规则:一个扩展坏掉不拖垮别的;而且**看得见**(进 notes)。"""
    log = tmp_path / "peer.jsonl"
    _install(tmp_path, "a-emitter", _EMITTER.format(log=str(log)))
    _install(tmp_path, "b-bad", """
import json
from pathlib import Path

LOG = Path({log!r})


def register(api):
    def boom(data):
        raise RuntimeError("我坏了")

    api.events.on("index:done", boom)
""".format(log=str(log)))
    _install(tmp_path, "c-good", _LISTENER.format(log=str(log)))
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await runtime.start_session(session)

    assert {"who": "listener", "data": {"files": 17}} in _log(log)   # 后面的照跑
    assert any("b-bad" in n and "index:done" in n for n in runtime.notes)


_ASYNC_LISTENER = """
import asyncio
import json
from pathlib import Path

LOG = Path({log!r})


def register(api):
    async def on_message(data):
        await asyncio.sleep(0)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({{"who": "async", "data": data}}) + "\\n")

    api.events.on("index:done", on_message)
"""


@pytest.mark.asyncio
async def test_async_peer_handlers_are_scheduled(tmp_path, monkeypatch):
    """async handler 排成后台任务 —— `emit` 本身**不等待**(与 pi 的同步 emit 一致)。"""
    log = tmp_path / "peer.jsonl"
    _install(tmp_path, "a-emitter", _EMITTER.format(log=str(log)))
    _install(tmp_path, "b-async", _ASYNC_LISTENER.format(log=str(log)))
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await runtime.start_session(session)
    await asyncio.sleep(0)                             # 给后台任务一次机会
    await asyncio.sleep(0)

    assert {"who": "async", "data": {"files": 17}} in _log(log)


def test_host_events_and_peer_messages_do_not_collide():
    """**两套 API 但名字可以重合**:同名的宿主事件与 peer 消息互不干扰。

    这条是“为什么用两张表”的直接证据 —— 如果合成一张,`api.on("input")` 与
    `api.events.on("input")` 就会互相触发,而两者的 handler 签名都不一样
    (宿主事件收 `(payload, ctx)`,peer 消息只收 `data`)。
    """
    bus = ExtensionBus(notes=[])
    fired: list[str] = []

    bus.on("input", lambda payload, ctx: fired.append("host"))
    bus.on_message("input", lambda data: fired.append("peer"))

    bus.send_message("input", {"x": 1})
    assert fired == ["peer"]                           # 只动 peer 那张表
    assert bus.events == ["input"]                     # 宿主事件的订阅还在


# ── registerProvider ────────────────────────────────────

_PROVIDER_EXT = """
def register(api):
    api.registerProvider("proxy", {
        "api": "openai-completions",
        "baseUrl": "http://127.0.0.1:9/v1",
        "models": [{"id": "m-proxy"}],
    })
"""


def test_registered_provider_becomes_usable(tmp_path, monkeypatch):
    """注册完就能用:`set_model`(以及 `--ext` 选模型)走的是同一份内存 cfg。"""
    _install(tmp_path, "proxy-ext", _PROVIDER_EXT)
    runtime = _runtime(tmp_path, monkeypatch)
    assert runtime.extensions == ["proxy-ext"]
    assert "proxy" in runtime.cfg.providers          # 进了内存 cfg

    resolved = runtime.set_model("proxy", "m-proxy")
    assert (resolved.provider, resolved.model) == ("proxy", "m-proxy")
    assert resolved.base_url == "http://127.0.0.1:9/v1"   # 用的是扩展给的那份


def test_registered_provider_is_not_persisted(tmp_path, monkeypatch):
    """**不写 `models.json`** —— 写盘会让“跑一次带代理的扩展”永久改变用户的模型配置。"""
    _install(tmp_path, "proxy-ext", _PROVIDER_EXT)
    _env(tmp_path, monkeypatch)                     # 先建文件,才能对比“之后有没有被改”
    models_file = tmp_path / "models.json"
    before = models_file.read_text(encoding="utf-8")
    _runtime(tmp_path, monkeypatch)
    assert models_file.read_text(encoding="utf-8") == before


def test_overriding_an_existing_provider_is_allowed_but_noted(tmp_path, monkeypatch):
    """覆盖是代理/自定义端点的正当用途,但**要说一声**:
    “我明明配了 models.json,却被别人改了” 很难查。"""
    _install(tmp_path, "proxy-ext", _PROVIDER_EXT.replace('"proxy"', '"ollama"'))
    runtime = _runtime(tmp_path, monkeypatch)
    assert any("覆盖" in n and "ollama" in n for n in runtime.notes)


def test_invalid_provider_config_fails_the_load(tmp_path, monkeypatch):
    """配置不合法 → **装载期就报错**(而不是装一个坏 provider 等以后发作)。

    这与 `discover_extensions` 的既有契约一致:“扩展坏 → 启动报错(不静默)”。
    我一开始把它写成“应该只记一条 note”—— 那是错的:一个注册不出 provider 的扩展
    等于没生效,而“装了但没生效”最难查。
    """
    _install(tmp_path, "bad-ext", """
def register(api):
    api.registerProvider("broken", {"models": "这不是列表"})
""")
    with pytest.raises(RuntimeError, match="broken"):
        _runtime(tmp_path, monkeypatch)
