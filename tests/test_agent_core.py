"""P2-P5 测试:loader / runner / dispatcher / runtime(全部用 stub LLM,无网络)。"""

from __future__ import annotations

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
from qi_agent.runner import AgentRunner, RunnerSettings  # noqa: E402
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
    cfg = tmp / "qi_agent.toml"
    cfg.write_text(f'[models.default]\nprovider="{provider}"\nmodel="x"\n', encoding="utf-8")
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

    prompt = build_system_prompt(unit)
    assert "checklist" in prompt and "评审核对" in prompt


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
    runner = AgentRunner(unit, catalog, llm, RunnerSettings(max_turns=5), tool_ctx=ctx)
    events = [e async for e in runner.run("找 hello")]
    kinds = [e.kind for e in events]
    assert "tool_start" in kinds and "tool_end" in kinds
    text_events = [e.text for e in events if e.kind == "text"]
    assert text_events and "hello" in text_events[-1]


@pytest.mark.asyncio
async def test_runner_blocked_bash(tmp_path):
    catalog = make_catalog()
    d = write_agent(tmp_path, "x")
    unit = load_agent_dir(d, "user", catalog.names)
    ctx = ToolContext(agent_name="x", workdir=tmp_path)
    llm = StubLLM([
        ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="bash",
                                                      args={"command": "rm -rf /tmp/evil"})]),
        ChatResponse(text="done"),
    ])
    runner = AgentRunner(unit, catalog, llm, RunnerSettings(max_turns=5), tool_ctx=ctx)
    events = [e async for e in runner.run("删文件")]
    tool_msgs = [e.text for e in events if e.kind == "tool_end"]
    assert any("安全策略拒绝" in t for t in tool_msgs)


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
    # sticky
    d4 = disp.rule_decide("继续", active_agent="writer")
    assert d4 is not None and d4.agent == "writer" and d4.source == "sticky"


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
    write_config(base)
    monkeypatch.setenv("QI_AGENT_CONFIG", str(base / "qi_agent.toml"))
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    return {"home": home, "cfg": base / "qi_agent.toml"}


@pytest.mark.asyncio
async def test_runtime_auto_end_to_end(tmp_path, monkeypatch):
    from qi_agent.runtime import QiRuntime, RuntimeConfig

    env = _runtime_env(monkeypatch, tmp_path)
    d = write_agent(env["home"] / "agents", "writer",
                    extra={"tools": ["*"], "disallowed_tools": ["write", "edit"]})
    workdir = tmp_path / "ws"
    workdir.mkdir()
    sessions = SessionStore(root=tmp_path / "sessions")

    # router 也是 stub(auto 需 decide_semantic)
    router = StubLLM([ChatResponse(text="", tool_calls=[
        ToolCallOut(id="r", name="dispatch_to",
                    args={"agent": "writer", "confidence": 0.9, "reasoning": "写"})])])
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
    # sticky: 同一会话再输入,rule path 直接沿用 writer
    events2 = [e async for e in rt.stream("补充一句", session)]
    d2 = next(e for e in events2 if e.kind == "dispatch")
    assert d2.data["source"] == "sticky"


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
