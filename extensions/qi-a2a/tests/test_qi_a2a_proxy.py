"""qi-a2a:`a2a` 代理工具与 directTools。

假客户端只实现工具真正用到的那几个方法 —— 协议细节在 `test_qi_a2a_protocol.py` 里测。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

REPO = Path(__file__).resolve().parents[3]  # tests/ → qi-a2a/ → extensions/ → 仓库根
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-a2a"))

import pytest  # noqa: E402

from qi_a2a import state  # noqa: E402
from qi_a2a.card import CardError  # noqa: E402
from qi_a2a.client import SendResult  # noqa: E402
from qi_a2a.config import A2AEntry, entry_tool_name  # noqa: E402
from qi_a2a.direct import register_direct  # noqa: E402
from qi_a2a.proxy import build_tool  # noqa: E402


def _write(project: Path, servers: dict) -> None:
    path = project / ".qi" / "a2a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"a2aServers": servers}, ensure_ascii=False),
                    encoding="utf-8")


def _text(result: Any) -> str:
    return result.result if hasattr(result, "result") else str(result)


def _ctx(project: Path) -> SimpleNamespace:
    return SimpleNamespace(workdir=project, project_trusted=True,
                           session_env={"QI_SESSION_ID": "s1"},
                           ui=SimpleNamespace(notify=lambda _text: None), notes=[])


def _card(tenant: str = "agent-1", name: str = "数据分析助手") -> dict:
    return {
        "name": name,
        "description": "跑 SQL 并给结论",
        "skills": [{"id": "s1", "name": "数据分析", "description": "SQL", "tags": ["分析"]}],
        "supportedInterfaces": [{"url": "http://gw:8080/a2a", "tenant": tenant}],
    }


class FakeClient:
    def __init__(self, payload=None, *, result: SendResult | None = None,
                 fail: Exception | None = None) -> None:
        self.payload = payload if payload is not None else _card()
        self.result = result or SendResult(text="回答", state="completed",
                                           task_id="t1", context_id="c1")
        self.fail = fail
        self.sent: list[dict] = []
        self.closed = False
        self.card_urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self) -> None:
        self.closed = True

    async def get_json(self, url):
        self.card_urls.append(url)
        if self.fail:
            raise self.fail
        return self.payload

    async def send(self, card, text="", *, context_id="", task_id="", decisions=None):
        self.sent.append({"text": text, "context_id": context_id, "task_id": task_id,
                          "tenant": card.tenant, "decisions": decisions})
        return self.result

    async def get_task(self, card, task_id):
        return {"id": task_id, "status": {"state": "working"}}

    async def cancel_task(self, card, task_id):
        return {"id": task_id, "status": {"state": "canceled"}}


@pytest.fixture(autouse=True)
def _clean_state():
    state.reset()
    yield
    state.reset()


# ── list ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_is_offline_and_shows_source_and_paths(tmp_path):
    _write(tmp_path, {"hikqin": {"baseUrl": "http://gw:8080", "tenant": "agent-1",
                                 "description": "本地网关"}})
    tool = build_tool(lambda _entry: FakeClient(fail=AssertionError("list 不该联网")))

    text = _text(await tool.execute({"action": "list"}, _ctx(tmp_path)))

    assert "hikqin" in text
    assert "[项目]" in text and "本地网关" in text
    assert "a2a.json" in text


@pytest.mark.asyncio
async def test_list_can_discover_cards(tmp_path):
    _write(tmp_path, {"hikqin": {"baseUrl": "http://gw:8080", "tenant": "agent-1"}})
    tool = build_tool(lambda _entry: FakeClient())

    text = _text(await tool.execute({"action": "list", "discover": True}, _ctx(tmp_path)))

    assert "数据分析助手" in text and "agent-1" in text


# ── send / resume ───────────────────────────────────────

@pytest.mark.asyncio
async def test_send_returns_state_ids_and_text(tmp_path):
    _write(tmp_path, {"hikqin": {"baseUrl": "http://gw:8080", "tenant": "agent-1"}})
    fake = FakeClient()
    tool = build_tool(lambda _entry: fake)

    text = _text(await tool.execute({"action": "send", "message": "分析一下"}, _ctx(tmp_path)))

    assert fake.sent[0]["text"] == "分析一下"
    assert fake.sent[0]["tenant"] == "agent-1"
    assert "completed" in text and "t1" in text and "c1" in text and "回答" in text
    assert fake.closed is True


@pytest.mark.asyncio
async def test_context_id_is_reused_within_the_session(tmp_path):
    _write(tmp_path, {"hikqin": {"baseUrl": "http://gw:8080", "tenant": "agent-1"}})
    fake = FakeClient()
    tool = build_tool(lambda _entry: fake)
    ctx = _ctx(tmp_path)

    await tool.execute({"action": "send", "message": "第一句"}, ctx)
    await tool.execute({"action": "send", "message": "继续"}, ctx)

    assert [call["context_id"] for call in fake.sent] == ["", "c1"]


@pytest.mark.asyncio
async def test_resume_requires_task_id_and_decisions(tmp_path):
    _write(tmp_path, {"hikqin": {"baseUrl": "http://gw:8080", "tenant": "agent-1"}})
    tool = build_tool(lambda _entry: FakeClient())

    from qi_agent.extensions import ToolError

    with pytest.raises(ToolError, match="task_id"):
        await tool.execute({"action": "resume", "decisions": [{"type": "approve"}]},
                           _ctx(tmp_path))
    with pytest.raises(ToolError, match="decisions"):
        await tool.execute({"action": "resume", "task_id": "t1"}, _ctx(tmp_path))


@pytest.mark.asyncio
async def test_resume_passes_decisions(tmp_path):
    _write(tmp_path, {"hikqin": {"baseUrl": "http://gw:8080", "tenant": "agent-1"}})
    fake = FakeClient(result=SendResult(text="已执行", state="completed",
                                        task_id="t1", context_id="c1"))
    tool = build_tool(lambda _entry: fake)

    await tool.execute({"action": "resume", "task_id": "t1",
                        "decisions": [{"type": "approve"}]}, _ctx(tmp_path))

    assert fake.sent[0]["decisions"] == [{"type": "approve"}]
    assert fake.sent[0]["task_id"] == "t1"


@pytest.mark.asyncio
async def test_send_requires_a_server_when_several_are_configured(tmp_path):
    _write(tmp_path, {"a": {"baseUrl": "http://a"}, "b": {"baseUrl": "http://b"}})
    tool = build_tool(lambda _entry: FakeClient())

    from qi_agent.extensions import ToolError

    with pytest.raises(ToolError, match="server"):
        await tool.execute({"action": "send", "message": "x"}, _ctx(tmp_path))


@pytest.mark.asyncio
async def test_bad_config_becomes_a_tool_error_with_the_file(tmp_path):
    path = tmp_path / ".qi" / "a2a.json"
    path.parent.mkdir(parents=True)
    path.write_text("{oops", encoding="utf-8")
    tool = build_tool(lambda _entry: FakeClient())

    from qi_agent.extensions import ToolError

    with pytest.raises(ToolError, match="a2a.json"):
        await tool.execute({"action": "list"}, _ctx(tmp_path))


@pytest.mark.asyncio
async def test_input_required_renders_actionable_next_step(tmp_path):
    _write(tmp_path, {"hikqin": {"baseUrl": "http://gw:8080", "tenant": "agent-1"}})
    fake = FakeClient(result=SendResult(
        state="input-required", task_id="t7", context_id="c7",
        interrupt={"action_requests": [{"id": "a1", "name": "delete_file",
                                       "args": {"path": "t.txt"}}],
                   "review_configs": [{"allowed_decisions": ["approve", "reject"]}]}))
    tool = build_tool(lambda _entry: fake)

    text = _text(await tool.execute({"action": "send", "message": "删掉"}, _ctx(tmp_path)))

    assert "input-required" in text
    assert "delete_file" in text and 'task_id="t7"' in text
    assert "approve" in text and "reject" in text


@pytest.mark.asyncio
async def test_card_error_is_tool_error(tmp_path):
    _write(tmp_path, {"hikqin": {"baseUrl": "http://gw:8080", "tenant": "agent-1"}})
    tool = build_tool(lambda _entry: FakeClient(fail=CardError("名片 404")))

    from qi_agent.extensions import ToolError

    with pytest.raises(ToolError, match="名片 404"):
        await tool.execute({"action": "card"}, _ctx(tmp_path))


@pytest.mark.asyncio
async def test_unknown_action_lists_the_valid_ones(tmp_path):
    _write(tmp_path, {"hikqin": {"baseUrl": "http://gw:8080"}})
    tool = build_tool(lambda _entry: FakeClient())

    from qi_agent.extensions import ToolError

    with pytest.raises(ToolError, match="list / card / send"):
        await tool.execute({"action": "nope"}, _ctx(tmp_path))


# ── directTools ─────────────────────────────────────────

class FakeCatalog:
    def __init__(self) -> None:
        self.names: set[str] = set()


class FakeApi:
    def __init__(self, catalog_names=(), active=()) -> None:
        self.catalog = FakeCatalog()
        self.catalog.names.update(catalog_names)
        self._active = list(active)
        self.registered = []
        self.active_updates = []

    def register_tool(self, tool) -> None:
        self.registered.append(tool)
        self.catalog.names.add(tool.name)

    def get_active_tools(self):
        return list(self._active)

    def set_active_tools(self, names) -> None:
        self.active_updates.append(list(names))
        self._active = list(names)


def _entry(name: str, **cfg) -> A2AEntry:
    return A2AEntry(name=name, config={"baseUrl": "http://gw", **cfg})


def test_direct_is_opt_in_and_named_by_entry(tmp_path):
    api = FakeApi()
    ctx = _ctx(tmp_path)
    added = register_direct(api, {"a": _entry("a"), "b": _entry("b", directTools=True)}, ctx)

    assert added == ["a2a__b"]
    assert [tool.name for tool in api.registered] == ["a2a__b"]
    assert "a2a__b" in api.catalog.names


def test_direct_list_form_selects_entries(tmp_path):
    api = FakeApi()
    entries = {"a": _entry("a", directTools=["a"]), "b": _entry("b", directTools=["c"])}
    assert register_direct(api, entries, _ctx(tmp_path)) == ["a2a__a"]


def test_direct_preserves_a_narrowed_active_tool_set(tmp_path):
    api = FakeApi(catalog_names=("read", "bash"), active=("read",))
    register_direct(api, {"a": _entry("a", directTools=True)}, _ctx(tmp_path))
    assert api.active_updates == [["read", "a2a__a"]]


def test_direct_does_not_touch_the_active_set_when_it_was_never_narrowed(tmp_path):
    api = FakeApi(catalog_names=("read",), active=("read",))
    register_direct(api, {"a": _entry("a", directTools=True)}, _ctx(tmp_path))
    assert api.active_updates == []


def test_entry_tool_name_sanitizes():
    assert entry_tool_name(_entry("hikqin analysis")) == "a2a__hikqin_analysis"
    assert entry_tool_name(_entry("a.b/c")) == "a2a__a_b_c"


# ── 扩展入口 ────────────────────────────────────────────

def test_register_wires_tool_command_and_session_hook():
    from qi_a2a import register

    class Bus:
        def __init__(self) -> None:
            self.handlers = {}

        def on(self, event, handler):
            self.handlers[event] = handler

    class Api(FakeApi):
        def __init__(self) -> None:
            super().__init__()
            self.commands = {}
            self.events = Bus()

        def register_command(self, name, handler, *, description=""):
            self.commands[name] = handler

        def on(self, event, handler):
            self.events.on(event, handler)

    api = Api()
    register(cast(Any, api))

    assert [tool.name for tool in api.registered] == ["a2a"]
    assert "a2a" in api.commands
    assert "session_start" in api.events.handlers
