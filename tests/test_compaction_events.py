"""P-E3d-2:压缩的三个事件(`session_before_compact` / `session_compact` / `session_compact_failed`)。

压缩**只有一个入口**(`QiRuntime.compact_session`:TUI 的 `/compact` 与自动压缩都走它),
所以三个事件都在那一处发 —— 这也是设计里说"一处就能接完"的那件事。

契约(`docs/extensions.md` §3.1):

* `session_before_compact` —— 可 `{cancel: true}` 拦下,或 `{summary: "…"}` 自带摘要
  (自带时**不调模型**);
* `session_compact` —— 成功落盘后,带 `entry`;
* `session_compact_failed` —— 失败时**先发事件再把异常抛出去**。

测试不跑真模型:把 `prepare_compaction` / `compact` 换成桩,直接验证事件时机与载荷。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent import runtime as runtime_mod  # noqa: E402

_MODELS = json.dumps({"providers": {"ollama": {"api": "openai-completions",
                                               "models": [{"id": "x"}]}}})
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))


@pytest.fixture
def rt(tmp_path, monkeypatch):
    """真 runtime + 桩压缩;返回 `(runtime, 模型调用记录)`。"""
    from qi_agent.runtime import QiRuntime

    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch)
    runtime = QiRuntime(cwd=project, approve_project=True)

    prep = types.SimpleNamespace(tokens_before=1000, first_kept_entry_id="kept-1")
    monkeypatch.setattr(runtime_mod, "prepare_compaction", lambda *a, **k: prep)
    calls: list[str | None] = []

    async def fake_compact(llm, prep, *, instructions=None):
        calls.append(instructions)
        return {"type": "compaction", "summary": "模型给的摘要",
                "firstKeptEntryId": prep.first_kept_entry_id,
                "tokensBefore": prep.tokens_before, "usage": {}}

    monkeypatch.setattr(runtime_mod, "compact", fake_compact)
    return runtime, calls


async def _settle() -> None:
    """通知型事件走后台任务(`_emit_notice`),让它们跑完。"""
    for _ in range(3):
        await asyncio.sleep(0)


def _collect(runtime, event: str, payloads: list) -> None:
    runtime.bus.on(event, lambda payload, ctx: payloads.append(payload), source="probe")


async def test_success_emits_session_compact(rt):
    runtime, calls = rt
    seen: list = []
    _collect(runtime, "session_compact", seen)
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    entry = await runtime.compact_session(session)
    await _settle()

    assert entry is not None and entry["type"] == "compaction"
    assert calls == [None]
    assert seen and seen[0]["entry"]["summary"] == "模型给的摘要"
    assert seen[0]["provided"] is False
    # 落盘了(而不是只在内存里)
    stored = [e for e in runtime.sessions.get(session.id).entries if e.get("type") == "compaction"]
    assert stored and stored[0]["summary"] == "模型给的摘要"


async def test_before_compact_can_cancel(rt):
    runtime, calls = rt
    runtime.bus.on("session_before_compact", lambda payload, ctx: {"cancel": True},
                   source="probe")
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    assert await runtime.compact_session(session) is None
    assert calls == [], "取消了就不该调模型"
    assert not [e for e in runtime.sessions.get(session.id).entries
                if e.get("type") == "compaction"], "取消了不该落盘"


async def test_before_compact_can_supply_the_summary(rt):
    """`{summary}` 自带摘要时**不调模型** —— 扩展可能比模型更清楚该记住什么。"""
    runtime, calls = rt
    runtime.bus.on("session_before_compact", lambda payload, ctx: {"summary": "扩展写的摘要"},
                   source="probe")
    seen: list = []
    _collect(runtime, "session_compact", seen)
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    entry = await runtime.compact_session(session)
    await _settle()

    assert calls == [], "自带摘要时不该再调模型"
    assert entry["summary"] == "扩展写的摘要"
    # 与 compact() 造同一个形状(否则回放/上下文重建会缺字段)
    assert entry["firstKeptEntryId"] == "kept-1"
    assert entry["tokensBefore"] == 1000
    assert seen and seen[0]["provided"] is True


async def test_before_compact_payload_carries_the_context(rt):
    runtime, _calls = rt
    seen: list = []
    _collect(runtime, "session_before_compact", seen)
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    await runtime.compact_session(session, "只看性能")
    await _settle()

    assert seen and seen[0]["instructions"] == "只看性能"
    assert seen[0]["tokens_before"] == 1000


async def test_failure_emits_then_raises(rt, monkeypatch):
    runtime, _calls = rt
    seen: list = []
    _collect(runtime, "session_compact_failed", seen)

    async def boom(*a, **k):
        raise RuntimeError("摘要服务挂了")

    monkeypatch.setattr(runtime_mod, "compact", boom)
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    with pytest.raises(RuntimeError, match="摘要服务挂了"):
        await runtime.compact_session(session)
    await _settle()

    assert seen and "摘要服务挂了" in seen[0]["error"]


async def test_broken_handler_does_not_block_compaction(rt):
    """handler 抛异常 → 记 note 并继续(与其它事件同一条规矩:一个扩展坏不拖垮这一步)。"""
    runtime, calls = rt

    def broken(payload, ctx):
        raise RuntimeError("我坏了")

    runtime.bus.on("session_before_compact", broken, source="probe")
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    entry = await runtime.compact_session(session)
    assert entry is not None and calls == [None]
    assert any("session_before_compact" in note for note in runtime.notes), runtime.notes


async def test_no_subscribers_is_zero_overhead(rt):
    """零扩展时不建后台任务、不发事件(与总线其余部分一致)。"""
    runtime, _calls = rt
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    assert runtime.bus.has("session_compact") is False
    assert (await runtime.compact_session(session)) is not None
