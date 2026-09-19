"""P2-P5 测试:loader / runner / dispatcher / runtime(全部用 stub LLM,无网络)。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qi_agent.config import load_config  # noqa: E402
from qi_agent.dispatcher import Dispatcher  # noqa: E402
from qi_agent.llm import ChatMessage, ChatResponse, ToolCallOut  # noqa: E402
from qi_agent.loader import LoadError, load_agent_dir  # noqa: E402
from qi_agent.models import AgentUnit  # noqa: E402
from qi_agent.registry import AgentRegistry, ToolCatalog  # noqa: E402
from qi_agent.runner import (AgentRunner, RunnerSettings, spec_from_unit,
                              stop_after_turns)  # noqa: E402
from qi_agent.session import SessionStore  # noqa: E402
from qi_agent.tools import ToolContext, register_builtin_tools  # noqa: E402


class StubLLM:
    """可编程 stub:按调用次数返回预设响应。"""

    def __init__(self, script: list[ChatResponse]):
        self.script = list(script)
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append(messages)
        if not self.script:
            return ChatResponse(text="(stub 无响应)")
        return self.script.pop(0)


def make_catalog() -> ToolCatalog:
    c = ToolCatalog()
    register_builtin_tools(c)
    return c


def write_agent(root: Path, name: str, extra: dict | None = None, body: str = "你是 {name}。") -> Path:
    meta = {
        "name": name,
        "description": f"{name} 的用途描述(适用:{name} 任务)",
        "keywords": [name],
        "tools": ["read", "ls", "grep", "bash"],
    }
    meta.update(extra or {})
    text = "---\n" + _yaml(meta) + "\n---\n" + body.format(name=name)
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(text, encoding="utf-8")
    return d


def _yaml(m: dict) -> str:
    import yaml

    return yaml.safe_dump(m, allow_unicode=True, sort_keys=False).strip()


def write_config(tmp: Path, provider: str = "ollama") -> Path:
    """providers 写 models.json;默认模型写 settings.json(对齐 pi)。"""
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": provider, "defaultModel": "x"}), encoding="utf-8")
    cfg = tmp / "models.json"
    cfg.write_text(json.dumps({
        "providers": {provider: {"api": "openai-completions",
                                "models": [{"id": "x"}]}},
    }), encoding="utf-8")
    return cfg


# ── P2 loader ──────────────────────────────────────────────

def test_load_agent_basic(tmp_path):
    d = write_agent(tmp_path, "code-analyst", {"include": []}, body="分析代码。")
    catalog = make_catalog()
    unit = load_agent_dir(d, "user", catalog.names)
    assert unit.name == "code-analyst"
    assert unit.config.description
    assert "分析代码" in unit.system_prompt
    assert unit.tools == ["read", "ls", "grep", "bash"]


def test_load_agent_name_mismatch(tmp_path):
    d = write_agent(tmp_path, "dir-x")
    entry = d / "agent.md"
    # 手工改成不一致的 name
    entry.write_text(entry.read_text(encoding="utf-8").replace("name: dir-x", "name: other-name"), encoding="utf-8")
    catalog = make_catalog()
    with pytest.raises(LoadError, match="不一致"):
        load_agent_dir(d, "user", catalog.names)


def test_load_agent_unknown_tool(tmp_path):
    d = write_agent(tmp_path, "code-analyst", {"tools": ["no-such-tool"]})
    catalog = make_catalog()
    with pytest.raises(LoadError, match="未知工具"):
        load_agent_dir(d, "user", catalog.names)


def test_load_agent_denylist_and_wildcard(tmp_path):
    d = write_agent(tmp_path, "general", {"tools": ["*"], "disallowed_tools": ["write", "edit"]})
    catalog = make_catalog()
    unit = load_agent_dir(d, "user", catalog.names)
    assert "write" not in unit.tools and "edit" not in unit.tools
    assert "read" in unit.tools


def test_load_agent_plaintext_secret_blocked(tmp_path):
    d = write_agent(tmp_path, "x")
    (d / "mcp.json").write_text('{"mcpServers":{"s":{"type":"streamable-http","url":"https://x","headers":{"Authorization":"Bearer sk-real-secret-123456789"}}}}')
    catalog = make_catalog()
    with pytest.raises(LoadError, match="明文凭证"):
        load_agent_dir(d, "user", catalog.names)


def test_skills_discovery(tmp_path):
    d = write_agent(tmp_path, "code-analyst")
    (d / "skills" / "checklist").mkdir(parents=True)
    (d / "skills" / "checklist" / "SKILL.md").write_text(
        "---\nname: checklist\ndescription: 评审核对\n---\n正文", encoding="utf-8")
    catalog = make_catalog()
    unit = load_agent_dir(d, "user", catalog.names)
    assert [s.name for s in unit.skills] == ["checklist"]
    from qi_agent.runner import build_system_prompt

    # 技能清单只在有能读 SKILL.md 的工具时注入(对齐 pi),所以这里给 read
    prompt = build_system_prompt(unit, tools=catalog.resolve(["read"]))
    assert "checklist" in prompt and "评审核对" in prompt
    assert "<available_skills>" in prompt


# ── P4 runner(tool-loop) ─────────────────────────────────

@pytest.mark.asyncio
async def test_runner_tool_loop(tmp_path):
    catalog = make_catalog()
    d = write_agent(tmp_path, "code-analyst")
    unit = load_agent_dir(d, "user", catalog.names)
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    ctx = ToolContext(agent_name=unit.name, workdir=tmp_path)
    # LLM 第一轮调 grep,第二轮给文本
    llm = StubLLM([
        ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="grep",
                                                      args={"pattern": "hello", "path": "a.txt"})]),
        ChatResponse(text="找到了 hello world"),
    ])
    runner = AgentRunner(spec_from_unit(unit, catalog), catalog, llm, RunnerSettings(stop_after=stop_after_turns(5)), tool_ctx=ctx)
    events = [e async for e in runner.run("找 hello")]
    kinds = [e.kind for e in events]
    assert "tool_start" in kinds and "tool_end" in kinds
    text_events = [e.text for e in events if e.kind == "text"]
    assert text_events and "hello" in text_events[-1]


@pytest.mark.asyncio
async def test_runner_bash_is_not_command_filtered(tmp_path):
    """bash 不做命令级过滤(对齐 pi):旧白名单会拒的写操作现在能跑,结果回到模型。"""
    catalog = make_catalog()
    d = write_agent(tmp_path, "x")
    unit = load_agent_dir(d, "user", catalog.names)
    ctx = ToolContext(agent_name="x", workdir=tmp_path)
    llm = StubLLM([
        ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="bash",
                                                      args={"command": "mkdir -p made && echo ok > made/f.txt"})]),
        ChatResponse(text="done"),
    ])
    runner = AgentRunner(spec_from_unit(unit, catalog), catalog, llm, RunnerSettings(stop_after=stop_after_turns(5)), tool_ctx=ctx)
    events = [e async for e in runner.run("建个目录")]
    tool_msgs = [e.text for e in events if e.kind == "tool_end"]
    assert tool_msgs and "安全策略拒绝" not in tool_msgs[0]
    assert (tmp_path / "made" / "f.txt").read_text(encoding="utf-8").strip() == "ok"


# ── P5 dispatcher ─────────────────────────────────────────

def make_registry(tmp_path, *agents: str) -> AgentRegistry:
    catalog = make_catalog()
    reg = AgentRegistry()
    units = {}
    for a in agents:
        d = write_agent(tmp_path, a)
        units[a] = load_agent_dir(d, "user", catalog.names)
    reg.register_all(units)
    return reg


def test_mention_and_keywords(tmp_path):
    catalog = make_catalog()
    units = {}
    for a, kws in (("writer", ["文档", "文案"]), ("code-analyst", ["bug", "分析"])):
        d = write_agent(tmp_path, a, extra={"keywords": kws})
        units[a] = load_agent_dir(d, "user", catalog.names)
    reg = AgentRegistry()
    reg.register_all(units)
    disp = Dispatcher(reg, router_llm=None)
    # @ 点名
    d = disp.rule_decide("@writer 写文档")
    assert d is not None and d.agent == "writer" and d.source == "mention"
    # keywords 命中唯一
    d2 = disp.rule_decide("帮我分析这个 bug")
    assert d2 is not None and d2.agent == "code-analyst" and d2.source == "rules"
    # 无 active 且无命中 → None(交 router)
    d3 = disp.rule_decide("继续看看")
    assert d3 is None
    # 有 active_agent 但输入没命中 → 同样交 Router(不做会话亲和)
    d4 = disp.rule_decide("继续", active_agent="writer")
    assert d4 is None, f"规则层不该沿用 active_agent,却得到 {d4.agent}/{d4.source}"


def test_rule_layer_never_sticks_to_active_agent(tmp_path):
    """回归:规则层不认 active_agent —— 每轮都重新路由。

    旧实现有 sticky:上一轮定过的 agent 会以 confidence 0.9 吞掉后续输入,
    导致不设 keywords 的 agent(纯语义路由)永远拿不到分派。
    """
    catalog = make_catalog()
    units = {}
    for a, kws in (("writer", ["文档"]), ("code-analyst", ["review", "分析"])):
        d = write_agent(tmp_path, a, extra={"keywords": kws})
        units[a] = load_agent_dir(d, "user", catalog.names)
    reg = AgentRegistry()
    reg.register_all(units)
    disp = Dispatcher(reg, router_llm=None)

    # 命中 code-analyst 的 keyword,当前停在 writer → 直派 code-analyst
    d1 = disp.rule_decide("帮我 review 一下这段代码", active_agent="writer")
    assert d1 is not None and d1.agent == "code-analyst" and d1.source == "rules"

    # 命中两个 agent(歧义)→ 交 Router
    d2 = disp.rule_decide("帮我分析一下这个文档", active_agent="writer")
    assert d2 is None, f"歧义输入应交给 Router,却得到 {d2.agent}/{d2.source}"

    # 无 keywords 命中(含"继续")→ 也交 Router,不再沿用
    for text in ("继续", "刚才那个再加一句", "换个写法"):
        d = disp.rule_decide(text, active_agent="writer")
        assert d is None, f"'{text}' 不该被规则层定死,却得到 {d.agent}/{d.source}"

    # 没有 active_agent 时的行为不变
    assert disp.rule_decide("继续") is None


def test_keyword_ascii_word_boundary(tmp_path):
    """回归:ASCII keyword 按词边界匹配,`review` 不得命中 `code-reviewer` / `preview`。"""
    catalog = make_catalog()
    d = write_agent(tmp_path, "code-analyst", extra={"keywords": ["review"]})
    reg = AgentRegistry()
    reg.register_all({"code-analyst": load_agent_dir(d, "user", catalog.names)})
    disp = Dispatcher(reg, router_llm=None)

    # 子串包含但不是独立词 → 不命中
    assert disp.rule_decide("你并没有把任务分配给 code-reviewer 啊") is None
    assert disp.rule_decide("看看这个 preview 的效果") is None
    # 独立词 → 命中
    d1 = disp.rule_decide("帮我 review 这个文件")
    assert d1 is not None and d1.source == "rules"


@pytest.mark.asyncio
async def test_router_decision(tmp_path):
    reg = make_registry(tmp_path, "writer", "code-analyst")
    router = StubLLM([ChatResponse(text="", tool_calls=[
        ToolCallOut(id="r1", name="dispatch_to",
                    args={"agent": "writer", "confidence": 0.95, "reasoning": "要写文档"})])])
    disp = Dispatcher(reg, router_llm=router, confidence_min=0.6)
    d = await disp.decide_semantic("帮我写个项目说明", active_agent=None)
    assert d is not None and d.agent == "writer" and d.source == "router"


@pytest.mark.asyncio
async def test_router_low_confidence_falls_back(tmp_path):
    reg = make_registry(tmp_path, "code-analyst")   # 无 general
    router = StubLLM([ChatResponse(text="", tool_calls=[
        ToolCallOut(id="r1", name="dispatch_to",
                    args={"agent": "code-analyst", "confidence": 0.3, "reasoning": "低"})])])
    disp = Dispatcher(reg, router_llm=router)
    d = await disp.decide_semantic("随便")
    assert d.agent is None  # 无 general → 需澄清


# ── runtime 端到端(auto + 会话持久化) ─────────────────────

def _runtime_env(monkeypatch, base: Path) -> dict:
    home = base / "home"
    models = write_config(base)
    monkeypatch.setenv("QI_AGENT_CONFIG", str(models))
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    return {"home": home, "cfg": models}


@pytest.mark.asyncio
async def test_runtime_auto_end_to_end(tmp_path, monkeypatch):
    from qi_agent.runtime import QiRuntime, RuntimeConfig

    env = _runtime_env(monkeypatch, tmp_path)
    d = write_agent(env["home"] / "agents", "writer",
                    extra={"tools": ["*"], "disallowed_tools": ["write", "edit"]})
    workdir = tmp_path / "ws"
    workdir.mkdir()
    sessions = SessionStore(root=tmp_path / "sessions")

    # router 也是 stub(auto 需 decide_semantic);两轮各一条
    def _writer_call(i: str) -> ChatResponse:
        return ChatResponse(text="", tool_calls=[
            ToolCallOut(id=i, name="dispatch_to",
                        args={"agent": "writer", "confidence": 0.9, "reasoning": "写"})])

    router = StubLLM([_writer_call("r1"), _writer_call("r2")])
    exec_llm = StubLLM([ChatResponse(text="好的,我写好了。")])
    rt = QiRuntime(cwd=workdir,
                   runtime_cfg=RuntimeConfig(workdir=workdir),
                   session_store=sessions, llm=exec_llm, router_llm=router)
    assert rt.registry.get("writer") is not None

    session = sessions.create("t")
    events = [e async for e in rt.stream("写个说明", session)]
    kinds = [e.kind for e in events]
    assert "dispatch" in kinds and "agent_start" in kinds and "agent_end" in kinds
    disp_ev = next(e for e in events if e.kind == "dispatch")
    assert disp_ev.agent == "writer"
    # 会话持久化
    s2 = sessions.get(session.id)
    assert s2 is not None
    roles = [e.get("role") for e in s2.entries if e.get("type") == "message"]
    assert "assistant" in roles
    # 每轮重新路由:第二句仍走 router(stub 只有一条脚本,再次返回 writer)
    events2 = [e async for e in rt.stream("补充一句", session)]
    d2 = next(e for e in events2 if e.kind == "dispatch")
    assert d2.data["source"] == "router"
    assert d2.agent == "writer"


@pytest.mark.asyncio
async def test_runtime_reroutes_every_turn(tmp_path, monkeypatch):
    """回归:上一轮定过 agent 后,下一轮仍由 Router 裁决(不被 sticky 钉住)。

    旧行为:第 1 轮 Router 判给 general/无 keywords 的 agent 后,后续输入零命中
    → sticky 永久沿用,Router 再无机会介入。
    """
    from qi_agent.runtime import QiRuntime, RuntimeConfig

    env = _runtime_env(monkeypatch, tmp_path)
    write_agent(env["home"] / "agents", "writer", extra={"keywords": []})
    write_agent(env["home"] / "agents", "analyst", extra={"keywords": []})
    workdir = tmp_path / "ws"
    workdir.mkdir()

    def _call(agent: str, cid: str) -> ChatResponse:
        return ChatResponse(text="", tool_calls=[
            ToolCallOut(id=cid, name="dispatch_to",
                        args={"agent": agent, "confidence": 0.9, "reasoning": agent})])

    router = StubLLM([_call("writer", "r1"), _call("analyst", "r2")])
    rt = QiRuntime(cwd=workdir, runtime_cfg=RuntimeConfig(workdir=workdir),
                   session_store=SessionStore(root=tmp_path / "s"),
                   llm=StubLLM([ChatResponse(text="1"), ChatResponse(text="2")]),
                   router_llm=router)
    session = rt.sessions.create("t")

    d1 = next(e for e in [e async for e in rt.stream("写点东西", session)]
              if e.kind == "dispatch")
    assert (d1.agent, d1.data["source"]) == ("writer", "router")

    # 第二轮:无 keywords 命中,必须再问 Router,并接受改派
    d2 = next(e for e in [e async for e in rt.stream("分析一下", session)]
              if e.kind == "dispatch")
    assert (d2.agent, d2.data["source"]) == ("analyst", "router")
    # Router 拿到了上一轮 agent 作上下文
    assert any("writer" in m.content for m in router.calls[1])


@pytest.mark.asyncio
async def test_runtime_manual_override(tmp_path, monkeypatch):
    from qi_agent.runtime import QiRuntime, RuntimeConfig

    env = _runtime_env(monkeypatch, tmp_path)
    write_agent(env["home"] / "agents", "writer")
    write_agent(env["home"] / "agents", "code-analyst")
    sessions = SessionStore(root=tmp_path / "sessions")
    rt = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                   session_store=sessions,
                   llm=StubLLM([ChatResponse(text="我是 code-analyst")]),
                   disable_router=True)
    session = sessions.create("m")
    events = [e async for e in rt.stream("随便", session, agent_override="code-analyst")]
    d = next(e for e in events if e.kind == "dispatch")
    assert d.data["source"] == "manual"


# ── opening:每个 agent 每会话只展示一次(回归:曾每轮重复) ──────────

@pytest.mark.asyncio
async def test_opening_shown_once_per_agent(tmp_path, monkeypatch):
    """opening 只应在该 agent 的首轮展示;旧实现只看 entries[-1],导致每轮都重复。"""
    from qi_agent.runtime import QiRuntime, RuntimeConfig

    env = _runtime_env(monkeypatch, tmp_path)
    write_agent(env["home"] / "agents", "writer",
                extra={"opening": {"message": "开场白X", "suggestions": []}})
    sessions = SessionStore(root=tmp_path / "sessions")
    rt = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                   session_store=sessions,
                   llm=StubLLM([ChatResponse(text="a"), ChatResponse(text="b"),
                                ChatResponse(text="c")]),
                   disable_router=True)
    session = sessions.create("t")
    counts = []
    for i in range(3):
        evs = [e async for e in rt.stream(f"q{i}", session, agent_override="writer")]
        counts.append(sum(1 for e in evs if e.kind == "opening"))
    assert counts == [1, 0, 0], f"opening 展示次数应为 [1,0,0],实得 {counts}"


@pytest.mark.asyncio
async def test_opening_per_agent_not_per_session(tmp_path, monkeypatch):
    """按 agent 记:切到另一个 agent 时,展示它自己的开场白。"""
    from qi_agent.runtime import QiRuntime, RuntimeConfig

    env = _runtime_env(monkeypatch, tmp_path)
    write_agent(env["home"] / "agents", "writer",
                extra={"opening": {"message": "writer 开场", "suggestions": []}})
    write_agent(env["home"] / "agents", "analyst",
                extra={"opening": {"message": "analyst 开场", "suggestions": []}})
    sessions = SessionStore(root=tmp_path / "sessions")
    rt = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                   session_store=sessions,
                   llm=StubLLM([ChatResponse(text="1"), ChatResponse(text="2"),
                                ChatResponse(text="3")]),
                   disable_router=True)
    session = sessions.create("t")

    async def openings(agent: str) -> list[str]:
        evs = [e async for e in rt.stream("hi", session, agent_override=agent)]
        return [e.text for e in evs if e.kind == "opening"]

    assert await openings("writer") == ["writer 开场"]
    assert await openings("writer") == []            # 同 agent 不再重复
    assert await openings("analyst") == ["analyst 开场"]  # 换 agent → 它自己的


def test_builtin_general_has_no_opening():
    """内置 general 不带静态开场白:避免与模型自己的寒暄重复。"""
    from qi_agent.loader import builtin_agents_dir, load_agent_dir
    from qi_agent.registry import ToolCatalog
    from qi_agent.tools import register_builtin_tools

    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    d = builtin_agents_dir()
    assert d is not None
    unit = load_agent_dir(d / "general", "builtin", catalog.names)
    assert unit.config.opening is None
