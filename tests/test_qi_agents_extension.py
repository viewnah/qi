"""qi-agents 扩展:角色发现 / 角色选择 / `subagent` 委派。

这是**第一个真扩展**,所以这里同时验证两件事:
1. 扩展本身的行为对不对(角色发现、旗标注入、委派工具);
2. **扩展面够不够用** —— 角色系统此前住在 core(P-E4c 移出),现在只用公开面
   (`registerFlag` / `before_agent_start` / `registerTool` / `registerCommand` / `api.runAgent`)
   就能重新搭起来,core 一行不用改。这正是 E14/E15 押的那注。

扩展是 **pip 包形态**(entry point `qi.extensions`),所以测试用与 `discover_extensions` 相同的
机制把它接进来(替掉 `importlib.metadata.entry_points`),而不是真装进 venv ——
真装进去会让所有其它测试的运行时都多出一个 `subagent` 工具与 `agent` 旗标。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-agents"))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.llm import ChatResponse  # noqa: E402

_MODELS = ('{"providers": {"ollama": {"api": "openai-completions", '
           '"models": [{"id": "x"}]}}}')
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _write_role(root: Path, name: str, *, description="做这个的", body="你是{name}。",
                tools: str | None = '["read"]', model: str | None = None,
                disallowed_tools: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    extra = f"model: {model}\n" if model else ""
    # `tools=None` = **不写这一行** = 省略 = 继承父(而不是写个字符串 "None")
    if tools is not None:
        extra += f"tools: {tools}\n"
    if disallowed_tools is not None:
        extra += f"disallowed_tools: {disallowed_tools}\n"
    (d / "agent.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n"
        + body.format(name=name), encoding="utf-8")
    return d


class _LLM:
    """记下每次调用收到的消息与工具名;按脚本回答。"""

    def __init__(self, script: list[ChatResponse] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[list] = []
        self.tools: list[list[str]] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append(list(messages))
        self.tools.append(sorted(t["function"]["name"] for t in (tools or [])))
        return self.script.pop(0) if self.script else ChatResponse(text="好")

    def system(self, index: int = -1) -> str:
        return self.calls[index][0].content


def _install_entry_point(monkeypatch) -> None:
    """把 qi_agents 当成已安装的 pip 扩展(entry point `qi.extensions`)。"""
    import importlib.metadata as metadata

    def fake_entry_points(*, group=None, **kw):
        if group != "qi.extensions":
            return []
        import qi_agents

        ep = type("EP", (), {"name": "agents", "load": staticmethod(lambda: qi_agents),
                             "dist": None})()
        return [ep]

    monkeypatch.setattr(metadata, "entry_points", fake_entry_points)


def _env(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)
    return home


def _runtime(tmp_path, monkeypatch, llm, *, flags=None, approve=True, project=None):
    _env(tmp_path, monkeypatch)
    _install_entry_point(monkeypatch)
    project = project or (tmp_path / "proj")
    (project / ".git").mkdir(parents=True, exist_ok=True)
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=project, llm=llm, approve_project=approve,
                     extension_flags=flags or [])


# ── 角色发现 ────────────────────────────────────────────

def test_discovery_reads_both_layers_and_project_wins(tmp_path, monkeypatch):
    from qi_agents.discovery import discover

    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "reviewer", description="用户版")
    _write_role(home / "agents", "scout", description="只读侦察")
    _write_role(tmp_path / "proj" / ".qi" / "agents", "reviewer", description="项目版")
    _write_role(tmp_path / "proj" / ".qi" / "agents", "broken")   # 缺 description → 跳过
    (tmp_path / "proj" / ".qi" / "agents" / "broken" / "agent.md").write_text(
        "---\nname: broken\n---\n没描述\n", encoding="utf-8")

    assert set(discover(tmp_path / "proj", "user")) == {"reviewer", "scout"}
    both = discover(tmp_path / "proj", "both")
    assert set(both) == {"reviewer", "scout"}                 # broken 被跳过
    assert both["reviewer"].source == "project"               # 项目覆盖用户
    assert both["reviewer"].description == "项目版"


def test_discovery_parses_tools_and_model(tmp_path, monkeypatch):
    from qi_agents.discovery import discover

    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools="read, grep", model="beta/m3")

    role = discover(None, "user")["scout"]
    assert role.tools == ["read", "grep"]         # 逗号写法也认
    assert role.disallowed_tools is None
    assert role.model == "beta/m3"


# ── 角色选择:`--ext agent=<名>` ─────────────────────────

@pytest.mark.asyncio
async def test_agent_flag_injects_the_role_into_the_prompt(tmp_path, monkeypatch):
    """`qi --ext agent=reviewer` → 那个角色的说明出现在本轮 system prompt 里。

    这是 E14 的路子:core 不认识角色,扩展用 `registerFlag` + `before_agent_start` 自己做到。
    """
    llm = _LLM()
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "reviewer", body="MARKER-审查员:只读,挑缺陷。")
    runtime = _runtime(tmp_path, monkeypatch, llm, flags=["agent=reviewer"])

    assert runtime.extensions == ["agents"]
    assert runtime.flag_errors == []
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("看看这段代码", session):
        pass

    system = llm.system()
    assert "MARKER-审查员" in system
    # 追加在基座**之后**(顺序:基座 → 角色层 → …)
    assert system.index("你是运行在 qi 框架中") < system.index("MARKER-审查员")


@pytest.mark.asyncio
async def test_unknown_agent_name_becomes_a_note_not_a_crash(tmp_path, monkeypatch):
    llm = _LLM()
    _env(tmp_path, monkeypatch)
    runtime = _runtime(tmp_path, monkeypatch, llm, flags=["agent=nope"])
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("干活", session):
        pass

    assert any("nope" in n for n in runtime.notes)
    assert "MARKER" not in llm.system()


# ── 委派:subagent 工具 ─────────────────────────────────

_SUB = {"agent": "scout", "task": "查一下"}


def test_discovery_parses_disallowed_tools(tmp_path, monkeypatch):
    from qi_agents.discovery import discover

    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", disallowed_tools='["bash", "mcp__gh__*"]')
    role = discover(tmp_path / "proj", "user")["scout"]
    assert role.disallowed_tools == ["bash", "mcp__gh__*"]


@pytest.mark.asyncio
async def test_disallowed_tools_subtracts_from_the_resolved_list(tmp_path, monkeypatch):
    """denylist 在 allowlist/MCP **之后**应用 —— 两边都列到就移除(Claude Code 口径)。"""
    from qi_agent.llm import ToolCallOut

    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="subagent", args=_SUB)]),
                ChatResponse(text="子:好了"), ChatResponse(text="父:收到")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools='["read", "grep", "bash"]',
                disallowed_tools='["bash"]')
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("查", session):
        pass

    assert llm.tools[1] == ["grep", "read"], llm.tools


@pytest.mark.asyncio
async def test_disallowed_tools_with_inherited_tools(tmp_path, monkeypatch):
    """`tools` 省略 = 继承父;此时要减就先得把父的当前集合具象化。"""
    from qi_agent.llm import ToolCallOut

    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="subagent", args=_SUB)]),
                ChatResponse(text="子:好了"), ChatResponse(text="父:收到")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None, disallowed_tools='["bash", "powershell"]')
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("查", session):
        pass

    child = llm.tools[1]
    assert "bash" not in child and "powershell" not in child
    assert "read" in child, f"继承父集合时不该把别的也清掉:{child}"


@pytest.mark.asyncio
async def test_subagent_tool_runs_a_role_in_process(tmp_path, monkeypatch):
    """`subagent` 把一个任务交给另一个角色 —— 进程内受管子运行(E12)。"""
    from qi_agent.llm import ToolCallOut

    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="subagent", args=_SUB)]),
                ChatResponse(text="子:找到了 3 处"),         # 子运行
                ChatResponse(text="父:已收到")])              # 父收尾
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", body="MARKER-侦察员:只读。")
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("帮我查", session):
        pass

    assert len(llm.calls) == 3
    assert "MARKER-侦察员" in llm.calls[1][0].content        # 子运行用自己的提示词
    tool_msg = [m.content for m in llm.calls[2] if m.role == "tool"]
    assert tool_msg and "子:找到了 3 处" in tool_msg[0]        # 子结果回到父


@pytest.mark.asyncio
async def test_subagent_chain_passes_previous(tmp_path, monkeypatch):
    from qi_agent.llm import ToolCallOut

    args = {"chain": [{"agent": "scout", "task": "先侦察"},
                      {"agent": "writer", "task": "基于 {previous} 写文档"}]}
    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="subagent", args=args)]),
                ChatResponse(text="侦察结果 A"), ChatResponse(text="文档 B"),
                ChatResponse(text="父:好了")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", body="侦察员")
    _write_role(home / "agents", "writer", body="撰写者")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("走流程", session):
        pass

    # 第 2 棒的 task 里,{previous} 被换成了第 1 棒的输出
    second_task = llm.calls[2][-1].content
    assert "侦察结果 A" in second_task and "{previous}" not in second_task


@pytest.mark.asyncio
async def test_subagent_unknown_role_is_a_readable_error(tmp_path, monkeypatch):
    from qi_agent.llm import ToolCallOut

    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(
        id="c1", name="subagent", args={"agent": "nope", "task": "x"})]),
        ChatResponse(text="父:知道了")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout")
    runtime = _runtime(tmp_path, monkeypatch, llm)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("查", session):
        pass

    tool_msg = [m.content for m in llm.calls[1] if m.role == "tool"]
    assert tool_msg and "未知角色" in tool_msg[0] and "scout" in tool_msg[0]
    assert len(llm.calls) == 2                              # 子运行没跑


@pytest.mark.asyncio
async def test_project_roles_need_trust(tmp_path, monkeypatch):
    """项目角色是**仓库控制的提示词** → 未信任时默认拒绝(且问不出来就不做)。"""
    from qi_agent.llm import ToolCallOut

    project = tmp_path / "proj"
    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(
        id="c1", name="subagent",
        args={"agent": "scout", "task": "看代码", "agentScope": "both"})]),
        ChatResponse(text="父:知道了")])
    _env(tmp_path, monkeypatch)
    _write_role(project / ".qi" / "agents", "scout", body="项目里的侦察员")
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=False, project=project)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("查", session):
        pass

    tool_msg = [m.content for m in llm.calls[1] if m.role == "tool"]
    assert tool_msg and "未获批准" in tool_msg[0]
    assert len(llm.calls) == 2                              # 项目角色没跑


# ── /agents 命令(补回 core 删掉的 `qi agents list`)────────

def test_agents_command_lists_roles(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from typing import Any

    from qi_agent.extensions import ExtensionApi, ExtensionBus, FlagRegistry
    from qi_agent.registry import CommandRegistry, ToolCatalog

    from qi_agents import register

    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", description="只读侦察")
    registry = CommandRegistry()
    register(ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(),
                          _commands=registry, _flags=FlagRegistry(), _name="agents"))

    command = registry.find("agents")
    assert command is not None, "`/agents` 命令没登记上"

    class _Ui:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def notify(self, message: str, *, level: str = "info") -> None:
            self.messages.append(message)

    ui = _Ui()
    ctx: Any = SimpleNamespace(cwd=tmp_path, ui=ui)      # 鸭子类型的最小 ctx
    command.handler("", ctx)                             # 命令处理器是**同步**的
    assert any("scout" in m and "只读侦察" in m for m in ui.messages)
