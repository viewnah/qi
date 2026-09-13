"""P0 契约测试:工具结果结构化 / usage 透出 / 会话 cwd / 工具往返落盘。

这四项都是**前端与历史回放依赖的契约**(见 docs/web.md §13),因此单独成文件锁定形状:
一旦事件 `data` 或会话 entry 的字段被改名,这里必须同步改,不能悄悄漂移。

全部离线:stub LLM + tmp_path,不联网、不读用户配置。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from qi_agent import paths
from qi_agent.llm import ChatResponse, ToolCallOut, usage_to_dict
from qi_agent.loader import load_agent_dir
from qi_agent.models import TOOL_ERROR, TOOL_OK, AgentEvent, ToolOutcome
from qi_agent.registry import Tool, ToolCatalog
from qi_agent.runner import AgentRunner, RunnerSettings, _accumulate_usage
from qi_agent.session import SessionStore
from qi_agent.tools import ToolContext, _bash, register_builtin_tools


class StubLLM:
    """可编程 stub:按调用次数返回预设响应。"""

    def __init__(self, script: list[ChatResponse]):
        self.script = list(script)

    async def chat(self, messages, tools=None, temperature=None):
        if not self.script:
            return ChatResponse(text="(stub 无响应)")
        return self.script.pop(0)


def _catalog() -> ToolCatalog:
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    return catalog


def _agent_unit(tmp_path: Path, catalog: ToolCatalog, name: str = "w"):
    """装一个 tools=["*"] 的最小 agent。"""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(
        f'---\nname: {name}\ndescription: {name} 的用途\nkeywords: []\ntools: ["*"]\n---\n你是 {name}。\n',
        encoding="utf-8",
    )
    return load_agent_dir(d, "user", catalog.names)


def _minimal_config(base: Path, monkeypatch) -> Path:
    """最小可运行配置(默认模型写在 settings.json,对齐 pi)。"""
    (base / "models.json").write_text(
        '{"providers": {"ollama": {"api": "openai-completions", "models": [{"id": "x"}]}}}',
        encoding="utf-8",
    )
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x"}), encoding="utf-8"
    )
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(base / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return home


def _runtime(tmp_path: Path, monkeypatch, llm, sessions: SessionStore):
    from qi_agent.runtime import QiRuntime, RuntimeConfig

    _minimal_config(tmp_path, monkeypatch)
    return QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                     session_store=sessions, llm=llm, disable_router=True)


def _tool_call(name: str, args: dict, call_id: str = "c1") -> ChatResponse:
    return ChatResponse(text="", tool_calls=[ToolCallOut(id=call_id, name=name, args=args)])


def _events_on_disk(session) -> list[dict]:
    return [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _require(session):
    """store.get() 返回 Optional:显式收窄,供后续属性访问与传参。"""
    assert session is not None
    return session


# ── 1. 工具结果结构化 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_end_carries_structured_status(tmp_path):
    """返回 str 的工具 → status=ok、有 duration_ms、无 exit_code;text 仍是模型可见原文。"""
    catalog = _catalog()
    unit = _agent_unit(tmp_path, catalog)
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    ctx = ToolContext(agent_name=unit.name, workdir=tmp_path)
    llm = StubLLM([
        _tool_call("grep", {"pattern": "hello", "path": "a.txt"}),
        ChatResponse(text="找到了"),
    ])
    runner = AgentRunner(unit, catalog, llm, RunnerSettings(max_turns=5), tool_ctx=ctx)
    events = [e async for e in runner.run("找 hello")]

    end = next(e for e in events if e.kind == "tool_end")
    assert end.data["status"] == TOOL_OK
    assert isinstance(end.data["duration_ms"], int)
    assert end.data["duration_ms"] >= 0
    assert end.data["exit_code"] is None and end.data["error"] is None
    assert "hello" in end.text          # 模型可见文本不变(与旧版逐字一致)


@pytest.mark.asyncio
async def test_unknown_tool_is_structured_error(tmp_path):
    """未知工具:status=error 且 error 可机器判定,不再靠解析 'Error: ' 前缀。"""
    catalog = _catalog()
    unit = _agent_unit(tmp_path, catalog)
    llm = StubLLM([_tool_call("nope", {}), ChatResponse(text="继续")])
    runner = AgentRunner(unit, catalog, llm, RunnerSettings(max_turns=5),
                         tool_ctx=ToolContext(agent_name=unit.name, workdir=tmp_path))
    events = [e async for e in runner.run("用不存在的工具")]
    end = next(e for e in events if e.kind == "tool_end")
    assert end.data["status"] == TOOL_ERROR
    assert end.data["error"] == "unknown_tool"
    assert end.text.startswith("Error: 未知工具")


@pytest.mark.asyncio
async def test_tool_error_is_tagged_tool_error(tmp_path):
    """工具主动抛 ToolError(如 bash 被安全策略拒绝)→ error='tool_error'。"""
    catalog = _catalog()
    unit = _agent_unit(tmp_path, catalog)
    llm = StubLLM([_tool_call("bash", {"command": "rm -rf /tmp/evil"}), ChatResponse(text="好的")])
    runner = AgentRunner(unit, catalog, llm, RunnerSettings(max_turns=5),
                         tool_ctx=ToolContext(agent_name=unit.name, workdir=tmp_path))
    events = [e async for e in runner.run("删文件")]
    end = next(e for e in events if e.kind == "tool_end")
    assert end.data["status"] == TOOL_ERROR
    assert end.data["error"] == "tool_error"
    assert "安全策略拒绝" in end.text


@pytest.mark.asyncio
async def test_bash_reports_exit_code(tmp_path):
    """bash 是唯一上报 exit_code 的工具:成功 0,失败非 0 且 status=error。"""
    if shutil.which("git") is None:
        pytest.skip("需要 git")
    ctx = ToolContext(agent_name="w", workdir=tmp_path)
    ok = await _bash({"command": "ls"}, ctx)
    assert ok.status == TOOL_OK and ok.exit_code == 0

    # tmp_path 不是 git 仓库 → git status 非零退出(该命令本身在只读白名单内)
    bad = await _bash({"command": "git status"}, ctx)
    assert bad.status == TOOL_ERROR
    assert isinstance(bad.exit_code, int) and bad.exit_code != 0
    assert bad.result.startswith("exit=")
    assert bad.error is None            # 有 exit_code 时不再重复归类


@pytest.mark.asyncio
async def test_bash_timeout_is_tagged(tmp_path):
    """超时拿不到退出码 → 用 error 字段给出机器可读原因。

    用 `tail -f /dev/null`(阻塞、零输出、零 CPU、无内存尖峰)而不是 `cat`:
    后者在 pytest 下会立即收到 EOF 并正常退出,测不到超时分支。
    """
    ctx = ToolContext(agent_name="w", workdir=tmp_path)
    out = await _bash({"command": "tail -f /dev/null", "timeout": 0.3}, ctx)
    assert out.status == TOOL_ERROR
    assert out.error == "timeout"
    assert out.exit_code is None


# ── 2. usage 透出 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_end_reports_accumulated_usage(tmp_path):
    """多轮调用的 usage 累加后随 agent_end 透出(前端状态栏与成本显示要用)。"""
    catalog = _catalog()
    unit = _agent_unit(tmp_path, catalog)
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    llm = StubLLM([
        ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="grep",
                                                      args={"pattern": "hello", "path": "a.txt"})],
                     usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
        ChatResponse(text="完成",
                     usage={"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}),
    ])
    runner = AgentRunner(unit, catalog, llm, RunnerSettings(max_turns=5),
                         tool_ctx=ToolContext(agent_name=unit.name, workdir=tmp_path))
    events = [e async for e in runner.run("找 hello")]

    usage = next(e for e in events if e.kind == "agent_end").data["usage"]
    assert usage["llm_calls"] == 2 and usage["turns"] == 2
    assert usage["prompt_tokens"] == 30
    assert usage["completion_tokens"] == 12
    assert usage["total_tokens"] == 42


def test_accumulate_usage_sums_ints_and_counts_calls():
    """按需聚合所有整数字段(provider 字段差异大),非整数忽略;bool 不算 int。"""
    total: dict = {}
    _accumulate_usage(total, {"prompt_tokens": 5, "cached_tokens": 2, "model": "x",
                              "ratio": 0.5, "ok": True})
    _accumulate_usage(total, None)                    # 无 usage 也要计入调用次数
    assert total == {"llm_calls": 2, "prompt_tokens": 5, "cached_tokens": 2}


def test_usage_to_dict_normalizes_provider_objects():
    """litellm 返回的是对象而非 dict:必须归一,否则 SSE/--mode json 序列化会炸。"""

    class Pydanticish:
        def model_dump(self) -> dict:
            return {"prompt_tokens": 3}

    class Attrs:
        prompt_tokens = 7
        completion_tokens = 1
        something_else = object()

    assert usage_to_dict(None) == {}
    assert usage_to_dict({"a": 1}) == {"a": 1}
    assert usage_to_dict(Pydanticish()) == {"prompt_tokens": 3}
    assert usage_to_dict(Attrs()) == {"prompt_tokens": 7, "completion_tokens": 1}

    # 结果必须可 JSON 序列化(这正是归一化的目的)
    json.dumps(usage_to_dict(Pydanticish()))


# ── 3. 会话 cwd ───────────────────────────────────────────

def test_create_stamps_and_reads_back_cwd(tmp_path):
    """cwd 进 header,get/list 都能读回;不传时仍为 None(旧调用方式不受影响)。"""
    store = SessionStore(root=tmp_path / "s")
    proj = tmp_path / "proj"
    proj.mkdir()

    session = store.create("t", cwd=proj)
    assert session.cwd == str(proj.resolve())
    assert json.loads(
        (tmp_path / "s" / f"{session.path.stem}.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )["cwd"] == str(proj.resolve())

    fetched = _require(store.get(session.id))
    assert fetched.cwd == str(proj.resolve())
    listed = store.list()
    assert listed and listed[0].cwd == str(proj.resolve())

    assert store.create("t2").cwd is None


def test_ensure_cwd_backfills_legacy_session_once(tmp_path):
    """v0.1 会话头没有 cwd:首次使用时补一次;已有值时绝不覆盖。"""
    root = tmp_path / "s"
    root.mkdir()
    path = root / "20250101T000000_abc123.jsonl"
    path.write_text(
        json.dumps({"type": "session", "id": "abc123", "title": "旧会话",
                    "created_at": "2025-01-01T00:00:00"}, ensure_ascii=False) + "\n"
        + json.dumps({"type": "message", "role": "user", "content": "hi",
                      "agent_id": "general"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    store = SessionStore(root=root)
    legacy = _require(store.get("abc123"))
    assert legacy.cwd is None

    assert store.ensure_cwd(legacy, tmp_path) is True
    reread = _require(store.get("abc123"))
    assert reread.cwd == str(tmp_path.resolve())
    assert len(reread.entries) == 2                      # 只补 header,其余 entry 不动
    assert reread.entries[1]["content"] == "hi"

    # 已有 cwd → 不再写入,也不被别的目录覆盖(跨目录恢复旧会话不能改历史)
    assert store.ensure_cwd(reread, "/elsewhere") is False
    assert _require(store.get("abc123")).cwd == str(tmp_path.resolve())


def test_ensure_cwd_refuses_malformed_header(tmp_path):
    """header 不合法(不是 session 行)时不猜、不改文件。"""
    root = tmp_path / "s"
    root.mkdir()
    path = root / "x.jsonl"
    path.write_text(json.dumps({"type": "message", "role": "user", "content": "hi"}) + "\n",
                    encoding="utf-8")
    store = SessionStore(root=root)
    session = _require(store.get("x"))
    assert session.cwd is None
    before = path.read_text(encoding="utf-8")
    assert store.ensure_cwd(session, tmp_path) is False
    assert path.read_text(encoding="utf-8") == before


# ── 4. runtime 落盘契约(user 即时 / tool entry / cwd 回填) ──

@pytest.mark.asyncio
async def test_user_message_persisted_before_turn_finishes(tmp_path, monkeypatch):
    """用户消息在**发起时**落盘:运行中刷新/断线也要能看到自己说了什么。"""
    sessions = SessionStore(root=tmp_path / "sess")

    class BoomLLM:
        async def chat(self, messages, tools=None, temperature=None):
            raise RuntimeError("boom")

    rt = _runtime(tmp_path, monkeypatch, BoomLLM(), sessions)
    session = sessions.create("t", cwd=tmp_path)
    with pytest.raises(RuntimeError):
        _ = [e async for e in rt.stream("你好", session)]

    roles = [e["role"] for e in _events_on_disk(session) if e.get("type") == "message"]
    assert roles == ["user"]        # 回合炸了,但 user 已在盘上(assistant 尚未)


@pytest.mark.asyncio
async def test_tool_entry_persisted_with_structured_fields(tmp_path, monkeypatch):
    """补齐 PLAN A5 的第五类 entry:工具往返必须落盘,且带结构化字段。"""
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    sessions = SessionStore(root=tmp_path / "sess")

    class ToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools=None, temperature=None):
            self.calls += 1
            if self.calls == 1:
                return _tool_call("grep", {"pattern": "hello", "path": "a.txt"})
            return ChatResponse(text="完成", usage={"total_tokens": 9})

    rt = _runtime(tmp_path, monkeypatch, ToolLLM(), sessions)
    session = sessions.create("t", cwd=tmp_path)
    events = [e async for e in rt.stream("找 hello", session)]

    tools = [e for e in session.entries if e.get("type") == "tool"]
    assert len(tools) == 1
    entry = tools[0]
    assert entry["tool"] == "grep" and entry["status"] == TOOL_OK
    assert entry["args"] == {"pattern": "hello", "path": "a.txt"}
    assert isinstance(entry["duration_ms"], int) and entry["result"]
    assert entry["exit_code"] is None and entry["error"] is None

    # 顺序:header → dispatch → user → tool → assistant
    kinds = [e.get("type") for e in session.entries]
    assert kinds.index("tool") > kinds.index("message")
    assert session.entries[-1]["type"] == "message"
    assert session.entries[-1]["role"] == "assistant"
    assert any("完成" in e.text for e in events if e.kind == "text")

    # 重新读盘也能拿到 cwd 与 tool entry(历史回放的前提)
    again = _require(sessions.get(session.id))
    assert again.cwd == str(tmp_path.resolve())
    assert [e.get("type") for e in again.entries].count("tool") == 1


@pytest.mark.asyncio
async def test_stream_backfills_legacy_cwd(tmp_path, monkeypatch):
    """旧会话被使用时自动回填 cwd,历史会话也能按项目分组。"""
    sessions = SessionStore(root=tmp_path / "sess")
    legacy = sessions.create("旧")
    # 手工抹掉 cwd,模拟 v0.1 写的会话
    legacy.entries[0].pop("cwd", None)
    legacy.cwd = None
    sessions.save(legacy)

    class EchoLLM:
        async def chat(self, messages, tools=None, temperature=None):
            return ChatResponse(text="ok")

    rt = _runtime(tmp_path, monkeypatch, EchoLLM(), sessions)
    reloaded = _require(sessions.get(legacy.id))
    assert reloaded.cwd is None
    _ = [e async for e in rt.stream("你好", reloaded)]

    header = json.loads(reloaded.path.read_text(encoding="utf-8").splitlines()[0])
    assert header["cwd"] == str(tmp_path.resolve())


def test_tool_entry_result_is_bounded(tmp_path, monkeypatch):
    """插件工具可能不自行截断:落盘前再过一道上限,避免撑破会话文件。"""
    from qi_agent.runtime import MAX_TOOL_ENTRY_CHARS

    sessions = SessionStore(root=tmp_path / "sess")

    class EchoLLM:
        async def chat(self, messages, tools=None, temperature=None):
            return ChatResponse(text="ok")

    rt = _runtime(tmp_path, monkeypatch, EchoLLM(), sessions)
    session = sessions.create("t")
    event = AgentEvent(kind="tool_end", tool="big",
                       text="x" * (MAX_TOOL_ENTRY_CHARS + 500),
                       data={"status": TOOL_OK, "duration_ms": 1})
    rt._persist_tool(session, "w", event, {"args": {"a": 1}})

    entry = session.entries[-1]
    assert entry["result"].endswith("…(落盘已截断)")
    assert len(entry["result"]) <= MAX_TOOL_ENTRY_CHARS + len("…(落盘已截断)")


@pytest.mark.asyncio
async def test_custom_tool_returning_tool_outcome_is_respected(tmp_path):
    """插件/自定义工具可直接返回 ToolOutcome,其 status 与 exit_code 被采用。"""
    catalog = _catalog()

    async def _custom(args, ctx):
        return ToolOutcome(status=TOOL_ERROR, result="自定义失败", exit_code=3)

    catalog.register(Tool("custom", "自定义工具", {"type": "object", "properties": {}}, _custom))
    unit = _agent_unit(tmp_path, catalog)

    llm = StubLLM([_tool_call("custom", {}), ChatResponse(text="继续")])
    runner = AgentRunner(unit, catalog, llm, RunnerSettings(max_turns=5),
                         tool_ctx=ToolContext(agent_name=unit.name, workdir=tmp_path))
    events = [e async for e in runner.run("调用自定义工具")]

    end = next(e for e in events if e.kind == "tool_end")
    assert end.data["status"] == TOOL_ERROR
    assert end.data["exit_code"] == 3
    assert end.text == "自定义失败"
    assert isinstance(end.data["duration_ms"], int)
