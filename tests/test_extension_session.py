"""P-E3c-1:会话读写面 —— `appendEntry` / `ctx.session_manager` / `before_agent_start` 的 message 注入。

三件事共用同一条管道:**回合内把会话绑在 runtime 上**(`QiRuntime._active_session`),
扩展才拿得到“当前会话”去读/写。几条要点:

* `appendEntry` 写的 entry **不进 LLM 上下文**(与 `message` 相反)—— 这是它作为“存扩展状态”
  的正当性所在:不然每存一次状态就污染一次对话。
* 它是**持久**的:下一轮(甚至重开会话后)能读回来。所以在同一个回合里写、在下一个回合读,
  是这里最要紧的一条端到端。
* 回合外(没有活动会话)写要**报错**,不静默丢弃 —— 静默丢的表现是“我存了但重启后没了”。
* `before_agent_start` 注入的 message 反过来:**要进上下文**,而且按 pi 的语义是持久的。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.llm import ChatResponse  # noqa: E402
from qi_agent.registry import EXTENSION_ENTRY_FILE  # noqa: E402

_MODELS = ('{"providers": {"ollama": {"api": "openai-completions", '
           '"models": [{"id": "x"}]}}}')
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


class _ScriptedLLM:
    """每次 `chat()` 记下收到的消息表;按脚本回答(默认纯文本)。"""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[list] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append(list(messages))
        text = self.replies.pop(0) if self.replies else "好"
        return ChatResponse(text=text)

    def messages_of(self, index: int) -> list:
        return self.calls[index]

    def contents(self, index: int) -> list[str]:
        return [m.content for m in self.calls[index]]


def _runtime(tmp_path, monkeypatch, llm, flags: list[str] | None = None):
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch)
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=project, approve_project=True, llm=llm,
                     extension_flags=flags)


async def _drain(runtime, text: str, session) -> list:
    return [e async for e in runtime.stream(text, session)]


def _entries(session) -> list[dict]:
    if not session.path.exists():
        return []
    return [json.loads(line) for line in
            session.path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _custom(session, custom_type: str) -> list[dict]:
    return [e for e in _entries(session)
            if e.get("type") == "custom" and e.get("custom_type") == custom_type]


# ── appendEntry ─────────────────────────────────────────

_STATE_EXTENSION = """
from qi_agent.extensions import Tool


async def register(api):
    pass


def register(api):
    def on_start(payload, ctx):
        # 上一轮存的状态读回来(跨回合持久化)
        seen = ctx.session_manager.custom_entries("turn_count")
        count = (seen[-1]["data"]["count"] if seen else 0) + 1
        api.appendEntry("turn_count", {"count": count})
        return {"system_prompt": payload["system_prompt"] + f"\\n[第 {count} 轮]"}
    api.on("before_agent_start", on_start)
"""


@pytest.mark.asyncio
async def test_append_entry_persists_and_stays_out_of_context(tmp_path, monkeypatch):
    """自定义 entry 落盘,但**不进** LLM 上下文(否则每存一次状态就污染一次对话)。"""
    llm = _ScriptedLLM()
    _install(tmp_path, "state", _STATE_EXTENSION)
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    await _drain(runtime, "第一句", session)

    rows = _custom(session, "turn_count")
    assert len(rows) == 1
    assert rows[0]["data"] == {"count": 1}
    assert rows[0]["source"] == "state"          # 哪个扩展写的
    assert rows[0]["agent"] == "qi"         # 当时哪个角色在跑

    # 上下文里只有 system + user(entry 不该出现)
    contents = llm.contents(0)
    assert not any("turn_count" in c for c in contents)
    assert not any('"count"' in c for c in contents)


@pytest.mark.asyncio
async def test_append_entry_round_trips_across_turns(tmp_path, monkeypatch):
    """跨回合读回来 —— 这是 appendEntry 存在的理由(持久化扩展状态)。"""
    llm = _ScriptedLLM()
    _install(tmp_path, "state", _STATE_EXTENSION)
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    await _drain(runtime, "一", session)
    await _drain(runtime, "二", session)
    await _drain(runtime, "三", session)

    assert [r["data"]["count"] for r in _custom(session, "turn_count")] == [1, 2, 3]
    assert "[第 3 轮]" in llm.messages_of(2)[0].content    # 扩展真的读到了上一轮的值


@pytest.mark.asyncio
async def test_append_entry_outside_a_turn_is_loud(tmp_path, monkeypatch):
    """回合外没有活动会话 → 报错(不静默丢:“我存了但重启后没了”最难查)。"""
    llm = _ScriptedLLM()
    runtime = _runtime(tmp_path, monkeypatch, llm)
    api = runtime.commands  # 只是拿个东西占位,下面直接用 host 方法
    del api
    with pytest.raises(RuntimeError, match="appendEntry"):
        runtime.append_extension_entry("x", {"a": 1}, "probe")


# ── session_manager(读) ─────────────────────────────────

_READER_EXTENSION = """
import json
from pathlib import Path

LOG = Path({log!r})


def register(api):
    def on_start(payload, ctx):
        sm = ctx.session_manager
        LOG.write_text(json.dumps({{
            "available": sm.available,
            "session_id": sm.session_id,
            "path": sm.path,
            "title": sm.title,
            "kinds": sorted({{e.get("type") or "?" for e in sm.entries()}}),
            "custom": len(sm.custom_entries()),
        }}, ensure_ascii=False), encoding="utf-8")
    api.on("before_agent_start", on_start)
"""


@pytest.mark.asyncio
async def test_session_manager_reads_the_current_session(tmp_path, monkeypatch):
    llm = _ScriptedLLM()
    log = tmp_path / "sm.json"
    _install(tmp_path, "reader", _READER_EXTENSION.format(log=str(log)))
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("标题在这", cwd=runtime.cwd)
    await _drain(runtime, "你好", session)

    seen = json.loads(log.read_text(encoding="utf-8"))
    assert seen["available"] is True
    assert seen["session_id"] == session.id
    assert seen["path"].endswith(".jsonl")
    assert seen["title"] == "标题在这"
    assert "message" in seen["kinds"]           # 能看到本轮的 user 消息
    assert seen["custom"] == 0


def test_session_manager_without_a_session_is_empty_not_crashing():
    """回合外(没有活动会话):读返回空,`available` 为 False。"""
    from qi_agent.extensions import SessionView

    sm = SessionView(None)
    assert sm.available is False
    assert sm.entries() == [] and sm.custom_entries() == []
    assert sm.session_id is None and sm.path is None and sm.title == ""


# ── before_agent_start 的 message 注入 ───────────────────

_INJECT_EXTENSION = """
def register(api):
    def on_start(payload, ctx):
        if payload["prompt"].startswith("请带上"):
            return {"message": "【上下文】仓库用 uv 管理依赖。"}
    api.on("before_agent_start", on_start)
"""


@pytest.mark.asyncio
async def test_injected_message_reaches_the_model_and_persists(tmp_path, monkeypatch):
    """注入的 message:**本轮就进上下文**,而且落盘(下一轮还在)。"""
    llm = _ScriptedLLM()
    _install(tmp_path, "inject", _INJECT_EXTENSION)
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    await _drain(runtime, "请带上上下文", session)
    contents = llm.contents(0)
    assert any("仓库用 uv 管理依赖" in c for c in contents)
    # 落盘了(带来源标记),所以下一轮仍在上下文里
    injected = [e for e in _entries(session)
                if e.get("injected_by") == "before_agent_start"]
    assert len(injected) == 1 and injected[0]["role"] == "user"

    await _drain(runtime, "换一句普通的", session)          # 这一轮不该再注入
    # 但历史里还在(所以第二轮仍能看到它)—— 且**不重复注入**
    assert any("仓库用 uv 管理依赖" in c for c in llm.contents(1))
    assert len([e for e in _entries(session)
                if e.get("injected_by") == "before_agent_start"]) == 1


@pytest.mark.asyncio
async def test_injected_message_accepts_a_dict_and_ignores_blanks(tmp_path, monkeypatch):
    llm = _ScriptedLLM()
    _install(tmp_path, "inject2", """
def register(api):
    def on_start(payload, ctx):
        if payload["prompt"] == "dict":
            return {"message": {"content": "【来自字典】", "custom_type": "x"}}
        if payload["prompt"] == "blank":
            return {"message": "   "}
    api.on("before_agent_start", on_start)
""")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    await _drain(runtime, "dict", session)
    assert any("【来自字典】" in c for c in llm.contents(0))

    await _drain(runtime, "blank", session)
    assert not [e for e in _entries(session)
                if e.get("injected_by") == "before_agent_start"
                and e.get("content", "").strip() == ""]      # 空的不落盘
