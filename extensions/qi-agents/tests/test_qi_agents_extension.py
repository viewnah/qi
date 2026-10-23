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

REPO = Path(__file__).resolve().parents[2]  # tests/ → extensions/qi-agents/tests/,层级深了一层
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
                disallowed_tools: str | None = None, delegate: str | None = None,
                opening: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    extra = f"model: {model}\n" if model else ""
    # `tools=None` = **不写这一行** = 省略 = 继承父(而不是写个字符串 "None")
    if tools is not None:
        extra += f"tools: {tools}\n"
    if disallowed_tools is not None:
        extra += f"disallowed_tools: {disallowed_tools}\n"
    # `delegate=None` = 不写 = 缺省 true(false = 到此为止,不再往下派)
    if delegate is not None:
        extra += f"delegate: {delegate}\n"
    if opening is not None:
        extra += f"opening: {opening}\n"
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


@pytest.fixture(autouse=True)
def _clean_pending_cards():
    """render_call 会把运行卡入队;跨测试残留会让下次委派弹到死卡上(流进旧组件)。"""
    from qi_agents.render import PENDING_CARDS

    PENDING_CARDS.clear()
    yield
    PENDING_CARDS.clear()


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
    _write_role(home / "agents", "leaf", delegate="false")

    role = discover(None, "user")["scout"]
    assert role.tools == ["read", "grep"]         # 逗号写法也认
    assert role.disallowed_tools is None
    assert role.model == "beta/m3"
    assert role.delegate is True                  # 缺省 = 可以往下派
    assert discover(None, "user")["leaf"].delegate is False


def test_discovery_parses_opening_suggestions(tmp_path, monkeypatch):
    """`opening` 随角色元数据出来 —— 选择 agent 时就要能显示，不必先跑一轮。"""
    from qi_agents.discovery import discover

    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout",
                opening='{message: "我是侦察员", suggestions: ["查结构", "找报错"]}')
    _write_role(home / "agents", "plain")

    opening = discover(None, "user")["scout"].opening
    assert opening is not None
    assert opening.message == "我是侦察员"
    assert opening.suggestions == ("查结构", "找报错")
    assert discover(None, "user")["plain"].opening is None


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
    _write_role(home / "agents", "leaf", delegate="false")
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
    assert any("leaf" in m and "delegate=false" in m for m in ui.messages), ui.messages


# ── 与基座的兼容面(回归)──────────────────────────────
#
# 下面四条钉住的都是"扩展面对基座的两个形状"这件事 —— 它们在 P-E4c 之后的基座上
# 各自真的坏过一次,而且**都是静默的**(不抛错、只是行为悄悄不对)。

@pytest.mark.asyncio
async def test_inherited_child_keeps_subagent_within_budget(tmp_path, monkeypatch):
    """`tools:` 省略(= 继承父)时,子运行照常拿到 `subagent` —— **预算制**取代了旧版的
    结构性剥离(delegate 缺省开、深度 < MAX_DEPTH、名额未耗尽,denylist 仍一票否决)。

    旧断言恰好反过来:省略时把 tools=None 交给宿主,宿主解析成“父的当前集合”,于是
    子拿到了 subagent —— 那时的修法是无条件剥掉。现在改成分层委派,这条测试改成钉住
    新不变式:继承发生时**其它工具一个不少**,且 subagent 在预算内保留。
    深度上限、delegate: false、denylist 三条各有专门测试钉住。
    """
    from qi_agent.llm import ToolCallOut

    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="subagent", args=_SUB)]),
                ChatResponse(text="子:好了"), ChatResponse(text="父:收到")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None)        # 省略 → 继承父
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("查", session):
        pass

    child = llm.tools[1]
    assert "subagent" in child, f"预算内子角色应拿到 subagent(分层委派):{child}"
    assert "read" in child, f"继承父的其它工具不该被清掉:{child}"


@pytest.mark.asyncio
async def test_depth_ceiling_strips_subagent(tmp_path, monkeypatch):
    """深度到顶的运行清单里没有 `subagent`:主会话 = 0,子 = 1 …
    monkeypatch MAX_DEPTH=1 → 第 1 层子运行就是最后一层(父清单里有也照剥)。"""
    from qi_agent.llm import ToolCallOut

    import qi_agents.subagent as sub

    monkeypatch.setattr(sub, "MAX_DEPTH", 1)
    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="subagent", args=_SUB)]),
                ChatResponse(text="子:好了"), ChatResponse(text="父:收到")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None)
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("查", session):
        pass

    child = llm.tools[1]
    assert "subagent" not in child, f"深度到顶必须剥掉 subagent:{child}"
    assert "read" in child, f"剥的只能是 subagent:{child}"


@pytest.mark.asyncio
async def test_delegate_false_stops_further_delegation(tmp_path, monkeypatch):
    """角色 frontmatter `delegate: false` = 到此为止(缺省是 true)。"""
    from qi_agent.llm import ToolCallOut

    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="subagent", args=_SUB)]),
                ChatResponse(text="子:好了"), ChatResponse(text="父:收到")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None, delegate="false")
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("查", session):
        pass

    child = llm.tools[1]
    assert "subagent" not in child, "delegate: false 的角色不能再往下派"
    assert "read" in child


@pytest.mark.asyncio
async def test_denylist_subagent_beats_delegate(tmp_path, monkeypatch):
    """denylist 的优先级高于 delegate:继承父清单 + `disallowed_tools: [subagent]`
    → 子运行照拿其它继承工具,但没有 subagent。"""
    from qi_agent.llm import ToolCallOut

    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="subagent", args=_SUB)]),
                ChatResponse(text="子:好了"), ChatResponse(text="父:收到")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None, disallowed_tools='["subagent"]')
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("查", session):
        pass

    child = llm.tools[1]
    assert "subagent" not in child, f"denylist 对 subagent 一票否决:{child}"
    assert "read" in child


@pytest.mark.asyncio
async def test_nested_delegation_runs_three_levels_and_cleans_registry(tmp_path, monkeypatch):
    """e2e 嵌套委派:主会话(0) → scout(1) → writer(2) → scout(3)。

    钉住:孙层真的跑了(输入带父的 task)、输出逐层回流、深 3 拿不到 subagent(默认
    MAX_DEPTH=3 的边界)、details 是缩进树、结束后登记表清空(深度/名额都不泄漏)。
    """
    from qi_agent.llm import ToolCallOut

    import qi_agents.subagent as sub

    def call(id_: str, agent: str, task: str) -> ChatResponse:
        return ChatResponse(text="", tool_calls=[ToolCallOut(
            id=id_, name="subagent", args={"agent": agent, "task": task})])

    llm = _LLM([call("c1", "scout", "查上层"),
                call("c2", "writer", "查下层"),      # scout(深1)起的
                call("c3", "scout", "孙任务"),        # writer(深2)起的
                ChatResponse(text="孙:底层结果"),      # scout(深3)收尾
                ChatResponse(text="中:汇总孙层"),      # writer 恢复后收尾
                ChatResponse(text="子:汇总了"),        # scout(深1)恢复后收尾
                ChatResponse(text="父:收到")])         # 主恢复后收尾
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None)
    _write_role(home / "agents", "writer", tools=None)
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = [e async for e in runtime.stream("层层拆解", session)]

    # 主→子→孙→曾孙→孙收尾→子收尾→主收尾 = 7 次(每层子结束后父还要恢复一轮)
    assert len(llm.calls) == 7, f"主/子/孙/曾孙/孙尾/子尾/主尾 = 7 次:{len(llm.calls)}"
    assert "subagent" in llm.tools[1], "深 1 的 scout 应继续拿到 subagent"
    assert "subagent" in llm.tools[2], "深 2 的 writer 应继续拿到 subagent"
    assert "subagent" not in llm.tools[3], "深 3 到顶(MAX_DEPTH=3)不该再拿到"
    assert "孙任务" in llm.calls[3][-1].content, "曾孙的输入必须带着父的 task 文本"
    tool_msgs = [m.content for m in llm.calls[4] if m.role == "tool"]
    assert tool_msgs and "孙:底层结果" in tool_msgs[0], "曾孙的输出要回流给孙"
    tool_msgs = [m.content for m in llm.calls[5] if m.role == "tool"]
    assert tool_msgs and "中:汇总孙层" in tool_msgs[0], "孙层的输出要回流给子"
    tool_msgs = [m.content for m in llm.calls[6] if m.role == "tool"]
    assert tool_msgs and "子:汇总了" in tool_msgs[0], "子的输出要回流给主"
    assert "孙:底层结果" in tool_msgs[0], "result 是缩进树:曾孙的原文也要到主这一层"
    assert sub.REGISTRY == {}, "全部 run 结束后登记表必须清空"

    # details 是缩进树:主这一层的 tool_end 卡片里,子顶格、孙缩一格、曾孙缩两格
    cards = [e for e in events if e.kind == "tool_end" and e.tool == "subagent"]
    assert len(cards) == 1
    items = cards[0].data["details"]["ui"][0]["items"]
    assert items[0]["label"].startswith("scout") and items[0]["depth"] == 1
    assert items[1]["label"].startswith("  writer") and items[1]["depth"] == 2
    assert items[2]["label"].startswith("    scout") and items[2]["depth"] == 3


@pytest.mark.asyncio
async def test_spawn_budget_refuses_the_call_when_exhausted(tmp_path, monkeypatch):
    """名额只减不增:这棵树只剩 2 个名额时,要 3 个的 parallel 被整笔拒绝(0 个子运行起)。"""
    from qi_agent.llm import ToolCallOut

    import qi_agents.subagent as sub

    monkeypatch.setattr(sub, "SPAWN_BUDGET", 2)
    args = {"tasks": [{"agent": "scout", "task": "A"},
                      {"agent": "scout", "task": "B"},
                      {"agent": "scout", "task": "C"}]}
    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name="subagent", args=args)]),
                ChatResponse(text="父:知道了")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout")
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("三路并发", session):
        pass

    assert len(llm.calls) == 2, "整笔拒绝 → 一个子运行都不该起"
    tool_msgs = [m.content for m in llm.calls[1] if m.role == "tool"]
    assert tool_msgs and "预算耗尽" in tool_msgs[0]
    assert sub.REGISTRY == {}


@pytest.mark.asyncio
async def test_child_tool_start_flows_back_as_timeline_blocks(tmp_path, monkeypatch):
    """子运行的活动进时间线:agent 切换先出黄标题块 `━━━ 委派 → 名字 ━━━`,内容按
    深度缩进跟随(名字不再做行前缀);text_delta 不回流(逐字回流是噪音);run
    收束出完成块(标题 + 结论)。run 结束即注销登记表。"""
    from types import SimpleNamespace

    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    sub._LAST_TITLE = ""          # 模块级时间线状态,直接调 _run_one 时测试自管
    _write_role(tmp_path / "home" / "agents", "scout")

    class FakeApi:
        async def run_agent(self, spec, task, *, abort=None, on_event=None):
            if on_event is not None:
                on_event(SimpleNamespace(kind="tool_start", tool="read",
                                         data={"args": {"path": "src/x.py"}}))
                on_event(SimpleNamespace(kind="text_delta", text="逐字刷屏"))
            return "子结果"

    class _Ui:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def notify(self, message: str, *, level: str = "info") -> None:
            self.messages.append(str(message))

    ui = _Ui()
    role = sub.discover(None, "user")["scout"]
    out = await sub._run_one(
        FakeApi(), role, "x",
        ToolContext(agent_name="qi", workdir=tmp_path, ui=ui))

    assert out["ok"] is True and out["depth"] == 1
    assert ui.messages[:2] == ["━━━ 委派 → scout ━━━━━━━", '  · read {"path": "src/x.py"}'], \
        f"黄标题一次 + 内容缩进跟随:{ui.messages}"
    assert not any("逐字刷屏" in m for m in ui.messages), "text_delta 不该回流"
    # 收束块:活动行之后,时间线要知道结论(标题行 + 输出摘要)
    done = ui.messages.index("── scout ✓ 深度1 ──")
    assert ui.messages[done + 1] == "  子结果", ui.messages
    assert sub.REGISTRY == {}


@pytest.mark.asyncio
async def test_timeline_titles_each_agent_once_and_previews_thinking(
        tmp_path, monkeypatch):
    """时间线(对话流):agent 切换出一次黄标题块 `━━━ 委派 → 名字 ━━━`,思考 `✎ 预览`
    与活动 `·` 缩进跟随 —— 名字不再做行前缀;同 agent 连续轮次不重复标题。
    完整的思考/活动在 result 分段里(卡片),时间线只给可读的预览。"""
    from types import SimpleNamespace

    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    sub._LAST_TITLE = ""          # 模块级时间线状态,直接调 _run_one 时测试自管
    _write_role(tmp_path / "home" / "agents", "scout")

    class FakeApi:
        async def run_agent(self, spec, task, *, abort=None, on_event=None):
            if on_event is not None:
                on_event(SimpleNamespace(kind="assistant_message",
                                         data={"thinking": "第一轮:先定范围"}))
                on_event(SimpleNamespace(kind="assistant_message",
                                         data={"thinking": "第二轮:再看细节"}))
                on_event(SimpleNamespace(kind="tool_start", tool="read",
                                         data={"args": {"path": "a.py"}}))
            return "子结果"

    class _Ui:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def notify(self, message: str, *, level: str = "info") -> None:
            self.messages.append(str(message))

    ui = _Ui()
    role = sub.discover(None, "user")["scout"]
    out = await sub._run_one(
        FakeApi(), role, "x",
        ToolContext(agent_name="qi", workdir=tmp_path, ui=ui))

    assert out["ok"] is True
    # 黄标题只出一次;两轮思考与活动都是缩进内容块
    assert ui.messages[0] == "━━━ 委派 → scout ━━━━━━━"
    assert ui.messages.count("━━━ 委派 → scout ━━━━━━━") == 1, ui.messages
    assert any(m.startswith("  ✎ ") and "第一轮:先定范围" in m for m in ui.messages)
    assert any(m.startswith("  ✎ ") and "第二轮:再看细节" in m for m in ui.messages)
    assert '  · read {"path": "a.py"}' in ui.messages
    # 收束块:标题 + 结论摘要(前 DONE_SUMMARY_LINES 行)
    done = ui.messages.index("── scout ✓ 深度1 ──")
    assert ui.messages[done + 1] == "  子结果", ui.messages
    # 分段侧:思考与活动都收进了本 run 的 result(供 _render 组装三区)
    assert out["thoughts"] == ["第一轮:先定范围", "第二轮:再看细节"]
    assert out["steps"] == ['· read {"path": "a.py"}']


@pytest.mark.asyncio
async def test_stream_deltas_flush_into_agent_card_and_timeline_skips_thinking(
        tmp_path, monkeypatch):
    """有平铺卡时:thinking/text delta 累计进当前轮缓冲(节流/轮末刷进**本 agent
    的卡**),headless 时间线照旧全量(黄标题/活动/完成块);前端则归卡不重复。"""
    from types import SimpleNamespace

    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    sub._LAST_TITLE = ""
    _write_role(tmp_path / "home" / "agents", "scout", tools=None)

    class _Card:
        """既是架子也是卡(add_agent 返回自己)—— 单测不挂真组件。"""

        def __init__(self) -> None:
            self.streams: list[tuple[str, str]] = []
            self.acts: list[str] = []
            self.added: list[tuple] = []
            self.done: tuple | None = None

        def add_agent(self, name: str, parent: str, depth: int) -> Any:
            self.added.append((name, parent, depth))
            return self

        def set_stream(self, text: str, mode: str = "think") -> None:
            self.streams.append((mode, text))

        def add_activity(self, line: str) -> None:
            self.acts.append(line)

        def set_done(self, ok: bool, output: str, thoughts: list[str]) -> None:
            self.done = (ok, output, thoughts)

    class FakeApi:
        async def run_agent(self, spec, task, *, abort=None, on_event=None):
            for chunk in ("先想", "一点"):
                on_event(SimpleNamespace(kind="thinking_delta", text=chunk))
            on_event(SimpleNamespace(kind="tool_start", tool="read",
                                     data={"args": {"path": "a.py"}}))
            on_event(SimpleNamespace(kind="text_delta", text="直出文本"))
            on_event(SimpleNamespace(kind="assistant_message",
                                     data={"thinking": "先想一点"}))
            return "结论行1\n行2\n行3\n行4"

    class _Ui:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def notify(self, message: str, *, level: str = "info") -> None:
            self.messages.append(str(message))

    ui = _Ui()
    card = _Card()
    role = sub.discover(None, "user")["scout"]
    out = await sub._run_one(
        FakeApi(), role, "x",
        ToolContext(agent_name="qi", workdir=tmp_path, ui=ui), holder=[card])

    assert out["ok"] is True
    # 架子上为本 run 追加了一张卡,标题信息 = 名字 ← 父 · 深度
    assert card.added == [("scout", "qi", 1)], card.added
    # 卡:首个 delta 立即节流 flush + 轮末强制 flush(同轮的思考与输出顺序累计
    # —— 一个缓冲就是本轮的完整流,忠实于事件序)
    assert ("think", "先想") in card.streams, card.streams
    assert ("think", "先想一点直出文本") in card.streams, card.streams
    assert card.acts == ['read {"path": "a.py"}']
    # 收束定格交给卡(标题转 ✓、正文收起为摘要)
    assert card.done is not None and card.done[0] is True
    assert "结论行1" in card.done[1]
    # 时间线:黄标题 + 活动行,轮末 ✎ 预览**不再重复**(卡里刚流过)
    assert ui.messages[0] == "━━━ 委派 → scout ━━━━━━━"
    assert '  · read {"path": "a.py"}' in ui.messages
    assert not any("✎ " in m for m in ui.messages), ui.messages
    # 完成块:标题 + 摘要(前3行)+ 溢出提示
    done = ui.messages.index("── scout ✓ 深度1 ──")
    assert ui.messages[done + 1] == "  结论行1", ui.messages
    assert any("+1 行" in m and "全文见卡片" in m for m in ui.messages[done:])


@pytest.mark.asyncio
async def test_pending_card_pops_fifo_at_tool_entry(tmp_path, monkeypatch):
    """render_call 入队的**平铺架子**在 run_subagent(工具入口)按 FIFO 出队 ——
    与 runner 的 tool_start 严格同序,一次工具调用对一个架子;本次只调了一次
    → 队首被弹走并在其上建 agent 卡,后一个架子留着等下次调用。"""
    from qi_agent.llm import ToolCallOut

    from qi_agents.render import PENDING_CARDS, _run_stack, call_summary

    card1 = _run_stack(call_summary({"agent": "scout", "task": "去"}))
    card2 = _run_stack(call_summary({"agent": "scout", "task": "后续"}))
    assert PENDING_CARDS == [card1, card2]

    llm = _LLM([
        ChatResponse(text="", tool_calls=[ToolCallOut(
            id="c1", name="subagent",
            args={"agent": "scout", "task": "去查"})]),
        ChatResponse(text="收尾"),
    ])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None)
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("派 scout 去查", session):
        pass

    # 一次工具调用 → 队首出队(配对给了本次运行),下一张原地等着
    assert PENDING_CARDS == [card2], "应只弹走队首一张"
    # 配对成功的痕迹:该 run 在队首架子上建了自己的卡,输出流刷在里面
    assert len(card1._cards) == 1, f"本次运行应建一张卡:{card1._cards}"
    mine = card1._cards[0]
    assert mine._stream == "收尾" and mine._mode == "text"
    assert mine._done and mine._ok, "收束后该卡应定格 ✓"
    assert card2._cards == [], "没被弹走的架子不该收到任何运行"


@pytest.mark.asyncio
async def test_nested_runs_get_their_own_flattened_cards_on_one_stack(
        tmp_path, monkeypatch):
    """嵌套子调用没有自己的工具块 —— 它通过发起者的 `holder` 继承**同一个架子**,
    每个 agent 在上面追加**自己的一张卡 = 全树拍平**:标题带 名字 ← 父 · 深度,
    各卡只收自己的活动/流式/输出,不落时间线(时间线+卡片各一份就是重复)。"""
    from qi_agent.llm import ToolCallOut

    from qi_agents.render import PENDING_CARDS, _run_stack, call_summary

    def call(id_: str, agent: str, task: str) -> ChatResponse:
        return ChatResponse(text="", tool_calls=[ToolCallOut(
            id=id_, name="subagent", args={"agent": agent, "task": task})])

    # 模拟 TUI:主会话这次 subagent 调用的平铺架子已入队(内层继承同一个)
    top = _run_stack(call_summary({"agent": "scout", "task": "层层拆解"}))
    assert PENDING_CARDS == [top]

    llm = _LLM([call("c1", "scout", "查上层"),
                call("c2", "writer", "查下层"),
                call("c3", "scout", "孙任务"),
                ChatResponse(text="孙:底层结果"),
                ChatResponse(text="中:汇总孙层"),
                ChatResponse(text="子:汇总了"),
                ChatResponse(text="父:收到")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None)
    _write_role(home / "agents", "writer", tools=None)
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("层层拆解", session):
        pass

    # 只弹一个架子(顶层认领),内层继承而不是再弹
    assert PENDING_CARDS == [], "内层不该再弹架子(它没有自己的入队)"
    # 三层 = 三张平铺卡,标题信息齐(名字 ← 父 · 深度)
    assert len(top._cards) == 3, [c._name for c in top._cards]
    outer, mid, leaf = top._cards
    assert (outer._agent, outer._lvl) == ("scout", 1)
    assert (mid._agent, mid._origin, mid._lvl) == ("writer", "scout", 2)
    assert leaf._lvl == 3
    # 各卡只收**自己**的活动:外层派 writer、内层派 scout
    assert any("writer" in a for a in outer._acts), outer._acts
    assert any("孙任务" in a for a in mid._acts), mid._acts
    assert leaf._acts == [], f"叶子没派单:{leaf._acts}"
    # 各卡定格自己的输出(孙层的原文在孙卡,不在外层)
    assert leaf._done and leaf._output == "孙:底层结果", leaf._output
    assert mid._done and "中:汇总孙层" in mid._output, mid._output
    assert outer._done and "子:汇总了" in outer._output, outer._output
    # 各卡的流式是**自己**那一轮的(不串卡):外层收尾 vs 孙层收尾
    assert outer._stream == "子:汇总了", outer._stream
    assert leaf._stream == "孙:底层结果", leaf._stream


@pytest.mark.asyncio
async def test_frontend_timeline_never_carries_thinking_previews(tmp_path, monkeypatch):
    """过程归卡:有前端时时间线**一行扩展输出都不出**(✎ 思考 / `·` 活动行 /
    黄标题 / 完成节点块全归卡 —— 卡标题已有 `▸ 名字 ← 父 · 深度N ✓/✗`,节点
    再出一遍就是重复);无前端(headless/notes,没有卡可显示) → 过程与 ✎ 全量
    落时间线(唯一日志)。"""
    from types import SimpleNamespace

    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    class FakeApi:
        async def run_agent(self, spec, task, *, abort=None, on_event=None):
            on_event(SimpleNamespace(kind="assistant_message",
                                     data={"thinking": "独轮思考"}))
            on_event(SimpleNamespace(kind="tool_start", tool="read",
                                     data={"args": {"path": "a.py"}}))
            return "结论"

    class _Ui:
        def __init__(self, frontend: bool) -> None:
            self.has_frontend = frontend
            self.messages: list[str] = []

        def notify(self, message: str, *, level: str = "info") -> None:
            self.messages.append(str(message))

    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None)
    role = sub.discover(None, "user")["scout"]

    # 场景1:有前端 → 时间线零输出(过程、黄标题、完成节点块都归卡)
    sub._LAST_TITLE = ""
    ui = _Ui(frontend=True)
    out = await sub._run_one(FakeApi(), role, "x",
                             ToolContext(agent_name="qi", workdir=tmp_path, ui=ui),
                             holder=[])
    assert out["ok"] is True
    assert ui.messages == [], \
        f"前端时间线零扩展输出(过程/黄标题/完成节点全归卡,卡标题已有 ✓/深度):{ui.messages}"

    # 场景2:无前端(headless) → 过程、✎ 与结论摘要全量落时间线(唯一日志)
    sub._LAST_TITLE = ""
    ui2 = _Ui(frontend=False)
    out = await sub._run_one(FakeApi(), role, "x",
                             ToolContext(agent_name="qi", workdir=tmp_path, ui=ui2))
    assert out["ok"] is True
    assert any("独轮思考" in m for m in ui2.messages), ui2.messages
    assert any('· read {"path": "a.py"}' in m for m in ui2.messages), ui2.messages
    assert any("结论" in m for m in ui2.messages), ui2.messages


@pytest.mark.asyncio
async def test_unknown_caller_is_refused_fail_closed(tmp_path, monkeypatch):
    """身份闸:agent_name 既不是主会话、也不在运行登记表里 → 拒绝(fail-closed),
    不给“表漏了”留一个 depth=0 的后门(depth=0 = 新预算 + 主会话集合)。"""
    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    _write_role(tmp_path / "home" / "agents", "scout")

    class FakeApi:
        async def run_agent(self, spec, task, *, abort=None, on_event=None):
            raise AssertionError("不该起子运行")

    out = await sub.build_tool(FakeApi()).execute(
        {"agent": "scout", "task": "x"},
        ToolContext(agent_name="someone-elses-run", workdir=tmp_path))

    assert isinstance(out, str) and "fail-closed" in out, out
    assert sub.REGISTRY == {}


def test_ui_items_flattens_the_delegate_tree():
    """details 的 list 词汇表(web.md §16.2)没有树节点 → 用 label 缩进表达嵌套,
    `depth` 作为附加字段;输出正文不进 details(它只在 result 里,50k 截断)。"""
    import qi_agents.subagent as sub

    results = [{"agent": "scout", "task": "查上层", "ok": True, "depth": 1,
                "output": "…",
                "children": [{"agent": "writer", "task": "写底层", "ok": False,
                              "depth": 2, "error": "boom",
                              "children": [{"agent": "scout", "task": "再查", "ok": True,
                                            "depth": 3, "output": "…"}]}]}]
    items = sub._ui_items(results)
    assert items[0]["label"] == "scout · 查上层" and items[0]["depth"] == 1
    assert items[1]["label"].startswith("  writer") and items[1]["state"] == "error"
    assert items[1]["note"] == "boom" and items[1]["depth"] == 2
    assert items[2]["label"].startswith("    scout") and items[2]["depth"] == 3
    assert "output" not in items[0] and "output" not in items[2]


@pytest.mark.asyncio
async def test_subagent_receives_the_abort_signal_from_a_tool_context(tmp_path, monkeypatch):
    """中断信号在**两个 ctx 形状**上都要取到。

    `subagent` 是**工具**,拿到的 ctx 是 `ToolContext`(信号字段叫 `abort`);
    而 handler 路径给的是 `ExtensionContext`(信号叫 `signal`,`abort` 是**方法**)。
    两个形状都遇到过写错的版本(`getattr(ctx, "signal")` 在工具路径上恒 None),
    而且两次都不抛错 —— 症状只是"Esc 杀不掉子 agent"。
    """
    from qi_agent.abort import AbortSignal
    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    _write_role(tmp_path / "home" / "agents", "scout")
    captured: list = []

    class FakeApi:
        async def runAgent(self, spec, task, abort=None, on_event=None):
            captured.append(abort)
            return "子结果"

    role = sub.discover(None, "user")["scout"]
    signal = AbortSignal()
    await sub._run_one(FakeApi(), role, "x",
                       ToolContext(agent_name="qi", workdir=tmp_path, abort=signal))
    assert captured[0] is signal, "工具路径(ToolContext.abort)上的信号没传下去"
    assert sub.REGISTRY == {}, "run 结束后登记表必须清空"


@pytest.mark.asyncio
async def test_project_role_is_not_injected_without_trust(tmp_path, monkeypatch):
    """`--ext agent=<项目角色>` 在项目未被信任时**不注入提示词**。

    项目级角色的正文是**仓库控制的提示词**,与 `subagent` 的 `agentScope="project"` 是同一个
    风险;那条路有闸门,这条路此前没有 —— 未信任的项目可以靠一个 agent.md 直接改系统提示词。
    无界面时按 fail-closed 跳过(并留一条 note 说明为什么少了一层)。
    """
    llm = _LLM()
    project = tmp_path / "proj"
    _env(tmp_path, monkeypatch)
    _write_role(project / ".qi" / "agents", "evil",
                body="MARKER-项目角色提示词,未信任时不该出现。")
    runtime = _runtime(tmp_path, monkeypatch, llm, flags=["agent=evil"],
                       approve=False, project=project)
    assert runtime.project_trusted is False
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("你好", session):
        pass

    assert "MARKER-项目角色" not in llm.system()
    assert any("未被信任" in n and "evil" in n for n in runtime.notes), runtime.notes


@pytest.mark.asyncio
async def test_project_role_is_injected_when_trusted(tmp_path, monkeypatch):
    """信任了就照常注入 —— 闸门不能反过来把正常用法也挡掉。"""
    llm = _LLM()
    project = tmp_path / "proj"
    _env(tmp_path, monkeypatch)
    _write_role(project / ".qi" / "agents", "reviewer",
                body="MARKER-项目审查员。")
    runtime = _runtime(tmp_path, monkeypatch, llm, flags=["agent=reviewer"],
                       approve=True, project=project)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("你好", session):
        pass

    assert "MARKER-项目审查员" in llm.system()


# ── 呈现层:横幅/树地图 + TUI 自绘钩子(render.py) ────────────────────────

def test_render_sections_carry_owner_depth_and_process_blocks():
    """result 文本 = 横幅分段(不画地图:委派关系在活动行与横幅深度里自明),
    每段三区:`·` 活动 → `✎` 该层各轮思考 → 输出 —— 一个 agent 的过程
    物理相邻;归属写在横幅里,不靠空格缩进承载(纯文本场景会吃掉它)。"""
    from qi_agents.subagent import _render

    tree = [{"agent": "coordinator", "ok": True, "depth": 1, "output": "父:结论",
             "steps": ['· read {"p": 1}'], "thoughts": ["先核对定义"],
             "children": [{"agent": "librarian", "ok": True, "depth": 2,
                           "output": "中:依据",
                           "children": [{"agent": "scout", "ok": False, "depth": 3,
                                         "error": "Boom"}]},
                          {"agent": "scribe", "ok": True, "depth": 2,
                           "output": "稿"}]}]
    text = _render(tree)
    # 无地图(委派关系在活动行与深度里自明,地图是重复的第二份信息)
    assert "委派树" not in text and "└─" not in text and "├─" not in text
    # 分段:每段横幅自带归属,正文原样、顶格(归属不依赖缩进)
    assert "── coordinator (深度1, 成功) ──" in text
    assert "── librarian (深度2, 成功) ──" in text
    assert "── scout (深度3, 失败) ──" in text
    assert "[失败] Boom" in text
    assert "\n父:结论" in text and "\n中:依据" in text
    # 三区:活动紧随横幅、思考居中、输出在后(同段物理相邻)
    assert "── coordinator (深度1, 成功) ──\n· read" in text
    assert "✎ 第1轮: 先核对定义" in text
    assert text.index("✎ 第1轮: 先核对定义") < text.index("父:结论")


def test_render_single_result_is_banner_only():
    """单段无子树时省掉地图 —— 一个横幅已经说清归属。"""
    from qi_agents.subagent import _render

    text = _render([{"agent": "scout", "ok": True, "depth": 1, "output": "x"}])
    assert "委派树" not in text
    assert text.startswith("── scout (深度1, 成功) ──\nx")


def test_result_tree_paints_segments_and_passes_text_through():
    """TUI 自绘:横幅上色、正文缩进 2 格,`·` 活动 dim、`✎` 思考 dim 斜体;
    无横幅的自由文本(用法/预算拒绝)**原样透传**,不丢内容不改尾换行。"""
    from qi_agents.render import result_tree

    text = ("── a (深度1, 成功) ──\n· read {}\n✎ 第1轮: 想了下\n甲"
            "\n\n── b (深度2, 失败) ──\n[失败] x")
    tree = result_tree(text)
    painted = tree.plain
    assert "── a (深度1, 成功) ──" in painted
    assert "  甲" in painted                      # 正文缩进 = 归属
    assert "  · read {}" in painted and "  ✎ 第1轮: 想了下" in painted
    assert "[失败] x" in painted
    frags = [(painted[s.start:s.end], str(s.style)) for s in tree.spans]
    assert any("· read {}" in f and st == "dim" for f, st in frags), frags
    assert any("第1轮" in f and st == "dim italic" for f, st in frags), frags
    assert result_tree("spawn 预算耗尽:本次要 3 个名额").plain == \
        "spawn 预算耗尽:本次要 3 个名额"
    # 超长段:卡片只画前 BODY_LINES 行,给行数提示(完整文本在 result 里)
    long = result_tree("── a (深度1, 成功) ──\n" + "\n".join(f"L{i}" for i in range(50)))
    assert "…(+20 行" in long.plain


def test_call_summary_labels_each_mode():
    """render_call 的调用摘要:pending 阶段就看清在派谁、派几个。"""
    from qi_agents.render import call_summary

    single = call_summary({"agent": "coordinator", "task": "查调用方"})
    assert "coordinator" in single.plain and "查调用方" in single.plain
    par = call_summary({"tasks": [{"agent": "a", "task": "t"},
                                   {"agent": "b", "task": "t"}]})
    assert "并行委派 2" in par.plain and "a, b" in par.plain
    ch = call_summary({"chain": [{"agent": "a", "task": "t"},
                                  {"agent": "b", "task": "t"}]})
    assert "链式委派 2" in ch.plain and "a → b" in ch.plain


def test_tool_exposes_render_hooks_that_build_cards():
    """render_call / render_result 必须**一起**挂 —— 只挂 result 时卡片是内置
    ToolBlock,tui.py 的 tool_end 分支会把自绘结果丢掉(见 render.py 模块文档)。
    现在 render_call 返回**平铺架子**(头部 = 调用摘要,add_agent 追加每 agent 一卡),
    render_result 返回 None(各卡 set_done 已定格,树卡退役);ctrl+o 转发的
    set_output 变成全局开合,不覆盖平铺内容。"""
    from qi_agents.subagent import build_tool

    tool = build_tool(object())
    assert callable(tool.render_call) and callable(tool.render_result)
    stack = tool.render_call({"agent": "coordinator", "task": "x"}, None)
    assert stack is not None
    assert "coordinator" in stack._summary.content.plain, "架子头部 = 调用摘要"
    assert tool.render_result("── a (深度1, 成功) ──\n正文", None) is None, \
        "树卡退役:各卡在 run 结束时各自定格"
    # 每个 agent 追加自己的一张卡,标题 = 名字 ← 父 · 深度
    card = stack.add_agent("coordinator", "qi", 1)
    assert card is not None
    assert "▸ coordinator ← qi · 深度1" in card._head.content.plain
    # 运行中:活动与流式在正文里
    card.add_activity('read {"path": "a.py"}')
    card.set_stream("第一段思考", "think")
    assert "· read" in card._body.content.plain
    assert "第一段思考" in card._body.content.plain
    # 收束:自动收起为**摘要**(输出前 3 行 + 提示)→ 点击展开全文
    card.set_done(True, "行1\n行2\n行3\n行4", ["轮思考全文"])
    assert "✓" in card._head.content.plain
    summary = card._body.content.plain
    assert "行1" in summary and "点击展开" in summary, summary
    assert "行4" not in summary and "轮思考全文" not in summary, "收起态不显示全文"
    card.on_click()
    body = card._body.content.plain
    assert "行4" in body and "轮思考全文" in body, body
    # ctrl+o:前端把内置 tool_body 转发进 set_output —— 架子级全局开合,
    # 正文绝不被纯文本覆盖
    from rich.text import Text

    stack.set_output(Text("不该覆盖"))
    assert "不该覆盖" not in card._body.content.plain
    assert "点击展开" in card._body.content.plain, "全局开合 = 所有卡回到收起"
    assert "轮思考全文" not in card._body.content.plain, "收起后正文仍藏"


@pytest.mark.asyncio
async def test_step_log_groups_each_runs_activity_into_its_banner_section(
        tmp_path, monkeypatch):
    """过程入段:每个子 run 自己的工具活动(`·`)与各轮思考(`✎`)写进**它那段**,
    卡片上"谁想了什么、干了什么、说了什么"成一块 —— 而不是散落的行。"""
    from qi_agent.llm import ToolCallOut

    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(
                    id="c1", name="subagent",
                    args={"agent": "scout", "task": "你去查"})]),
                ChatResponse(text="", tool_calls=[ToolCallOut(
                    id="c2", name="subagent",
                    args={"agent": "writer", "task": "下层"})]),
                ChatResponse(text="孙答"),        # writer(深2)收尾
                ChatResponse(text="子答", reasoning="范围确认完毕"),  # scout 收尾
                ChatResponse(text="父收")])       # 主恢复后收尾
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None)
    _write_role(home / "agents", "writer", tools=None)
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("拆解", session):
        pass

    tool_msgs = [m.content for m in llm.calls[4] if m.role == "tool"]
    assert tool_msgs, "主这一轮应收到 subagent 的 tool 结果"
    text = tool_msgs[0]
    # scout 段:它的活动(它派了 writer)+ 它的思考 + 它的输出,同段相连
    assert "── scout (深度1, 成功) ──\n· subagent" in text, text
    assert "✎ 第1轮: 范围确认完毕" in text, text
    assert text.index("✎ 第1轮") < text.index("子答")
    assert "子答" in text
    # writer 段:没调工具/没思考 → 横幅直入输出
    assert "── writer (深度2, 成功) ──\n孙答" in text, text
    # 不再画地图(委派关系在活动行与横幅深度里自明)
    assert "委派树" not in text


@pytest.mark.asyncio
async def test_empty_final_text_falls_back_to_theninking_then_placeholder(tmp_path, monkeypatch):
    """收尾轮不吐文本(mimo-flash 的实测行为)时的两级兜底:
    有思考 → 附思考记录;两者都无 → 至少给占位 —— 段绝不能是空横幅。
    (用户看到的“卡片里只有横幅没有内容”就是这个洞。)"""
    from qi_agent.llm import ToolCallOut

    llm = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(
                    id="c1", name="subagent",
                    args={"agent": "scout", "task": "查"})]),
                ChatResponse(text="", reasoning="思:该先读 subagent.py 再看事件链"),
                ChatResponse(text="好")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None)
    runtime = _runtime(tmp_path, monkeypatch, llm, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("查", session):
        pass

    tool_msgs = [m.content for m in llm.calls[2] if m.role == "tool"]
    assert tool_msgs and "思:该先读 subagent.py" in tool_msgs[0], \
        f"output 空时应附最后思考:{tool_msgs}"
    assert "收尾轮未给最终文本" in tool_msgs[0]

    # 两级都空 → 占位符,而不是空段
    llm2 = _LLM([ChatResponse(text="", tool_calls=[ToolCallOut(
                    id="c1", name="subagent",
                    args={"agent": "scout", "task": "查"})]),
                 ChatResponse(text=""),
                 ChatResponse(text="好")])
    home = _env(tmp_path, monkeypatch)
    _write_role(home / "agents", "scout", tools=None)
    runtime = _runtime(tmp_path, monkeypatch, llm2, approve=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("查", session):
        pass

    tool_msgs = [m.content for m in llm2.calls[2] if m.role == "tool"]
    assert tool_msgs and "(无文本输出)" in tool_msgs[0]
    assert "── scout (深度1, 成功) ──\n(无文本输出)" in tool_msgs[0]


def test_result_tree_dims_step_lines():
    """卡片上 `· ` 步骤行画 dim(与输出正文区分),横幅/正文不受影响。"""
    from qi_agents.render import result_tree

    painted = result_tree("── a (深度1, 成功) ──\n· read {\"x\": 1}\n\n正文结论")
    dim_spans = [s for s in painted.spans if str(s.style) == "dim"]
    assert any(painted.plain[s.start:s.end].strip().startswith("· ") for s in dim_spans), \
        f"步骤行应被 dim 覆盖:{painted.spans}"
    # 正文行不在 dim 里(它得是正常可读的输出)
    assert all(not painted.plain[s.start:s.end].strip().startswith("正文")
               for s in dim_spans)
