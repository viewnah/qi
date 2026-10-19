"""**一条真链路**:真 `subagent` 工具(qi-agents)→ 直播帧 → qi-lab 的 AG-UI 流。

为什么单开一个文件:推送端(qi-agents 的 `_web`)、宿主端(qi-lab 的过滤/广播/翻译)、
runner 的注入、前端渲染各自都有测试,但「**真委派在过程中就把活动推出来**」从来没被
端到端验证过 —— 而用户看到的现象恰恰是「subagent 的执行过程要等完全结束才展示」。

所以这里不只看"有没有帧",而是钉**帧到达的顺序**:活动帧必须出现在
`TOOL_CALL_RESULT` 之前。只要顺序错了,界面上就只能是"结束才看到",而且不会有任何报错。

需要 `real_extensions` 标记:qi-lab 的 conftest 默认 stub 掉 `qi.extensions` entry point
(免得测试随本机装了什么漂移),这条测试要的正是**真的** qi-agents。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

pytestmark = pytest.mark.real_extensions

_ROLE = """---
name: scout
description: 探路的
tools: ["read"]
---
你是 scout,只做一件事:读一遍。
"""


class _DelegatingLLM:
    """按**内容**分派调用(不能用调用计数:回合开头的自动起标题也会调 LLM)。

    · 起标题的调用 → 一个短标题;
    · 子运行(提示词里是 scout)→ 子结果;
    · 主运行 → 第一次要委派,之后给最终回答。
    """

    def __init__(self) -> None:
        self.delegated = False

    async def chat(self, messages, tools=None, temperature=None):  # pragma: no cover
        raise AssertionError("qi 有 astream 时走流式")

    async def astream(self, messages, tools=None, temperature=None):
        from qi_agent.llm import LLMDelta

        head = str(getattr(messages[0], "content", "") or "")
        if "起标题" in head or "标题" in head[:40]:
            yield LLMDelta(text="一次委派")
            yield LLMDelta(finished=True, usage={"total_tokens": 1})
            return
        if "scout" in head:
            yield LLMDelta(text="scout 读完了 README")
            yield LLMDelta(finished=True, usage={"total_tokens": 1})
            return
        if not self.delegated:
            self.delegated = True
            from qi_agent.llm import ToolCallOut

            yield LLMDelta(text="我让 scout 去看")
            yield LLMDelta(finished=True, usage={"total_tokens": 1},
                           tool_calls=[ToolCallOut(id="call_delegate", name="subagent",
                                                   args={"agent": "scout",
                                                         "task": "读一遍 README"})])
            return
        yield LLMDelta(text="它说读完了")
        yield LLMDelta(finished=True, usage={"total_tokens": 1})


def _frames(tmp_path, monkeypatch, llm, roles: dict[str, str] | None = None) -> list[dict]:
    from qi_agent import paths
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore
    from qi_lab.app import create_app
    from qi_lab.state import WebState

    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    for role_name, body in (roles or {"scout": _ROLE}).items():
        (home / "agents" / role_name).mkdir(parents=True, exist_ok=True)
        (home / "agents" / role_name / "agent.md").write_text(body, encoding="utf-8")
    # 关键:`packages` 声明是 pip 包扩展的**事实来源** —— 没声明就不加载
    # (registry 里那条 `if key not in declared_pips: continue`),真机上你的
    # settings.json 正是这么声明的。少了它,这条测试只会验出“未知工具 subagent”。
    (home / "settings.json").write_text(
        '{"defaultProvider": "stub", "defaultModel": "m1", '
        '"packages": ["pip:qi-agents"]}', encoding="utf-8")
    (tmp_path / "models.json").write_text(
        '{"providers": {"stub": {"api": "openai-completions", "models": [{"id": "m1"}]}}}',
        encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))

    store = SessionStore(root=tmp_path / "sessions")
    state = WebState(project, runtime_factory=lambda cwd: QiRuntime(
        cwd=project, runtime_cfg=RuntimeConfig(workdir=project),
        session_store=store, llm=llm, approve_project=True))
    app = create_app(cwd=project, state=state, allowed_hosts=[], bind_host="127.0.0.1")

    out: list[dict] = []
    with TestClient(app, base_url="http://127.0.0.1") as client:
        sid = client.post("/api/sessions",
                          json={"title": "t", "cwd": str(project)}).json()["id"]
        with client.stream("POST", "/api/ag-ui", json={
                "threadId": sid, "runId": "r1",
                "messages": [{"role": "user", "content": "让 scout 读一下 README"}]}) as resp:
            assert resp.status_code == 200, resp.read()
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    out.append(json.loads(line[len("data: "):]))
    return out


def test_a_real_delegation_streams_progress_before_the_tool_finishes(tmp_path, monkeypatch):
    """真委派:活动帧必须**在 `TOOL_CALL_RESULT` 之前**到达 —— 否则界面只能是"结束才看到"。"""
    frames = _frames(tmp_path, monkeypatch, _DelegatingLLM())
    kinds = [(f.get("type"), f.get("name") or f.get("activityType") or "") for f in frames]
    order = [k for k, _ in kinds]

    assert "TOOL_CALL_RESULT" in order, f"委派工具没跑起来:{kinds}"
    result_at = order.index("TOOL_CALL_RESULT")

    starts = [i for i, (k, n) in enumerate(kinds) if k == "SUBAGENT_STARTED"]
    acts = [i for i, (k, n) in enumerate(kinds)
            if k == "ACTIVITY_SNAPSHOT" and n == "qi.subagent"]
    assert starts, f"没有 SUBAGENT_STARTED:委派直播根本没发出来:{kinds}"
    assert acts, f"没有 qi.subagent 的 ACTIVITY_SNAPSHOT:{kinds}"
    assert min(starts) < result_at, "子智能体的“生”必须在工具结束之前到 —— 否则就是结束才看到"
    assert min(acts) < result_at, "活动内容必须在工具结束之前到（这是用户要的“过程中流式展示”）"

    # 生 → 内容 的顺序(单队列 FIFO 保证卡先来、内容再来)
    assert min(starts) < min(acts), f"顺序错了:{kinds}"
    start = frames[min(starts)]
    assert start["name"] == "scout" and start.get("parentToolCallId")
    snap = frames[min(acts)]
    assert snap["content"]["run"] and snap["content"].get("toolCallId")

    # **归卡键必须与工具卡一致**:不一致时前端只会把它暂存(pendingActivity),永远不显示 ——
    # 现象正是「工具卡在闪、里面什么都没有」。
    tool_start = next(f for f in frames if f["type"] == "TOOL_CALL_START")
    assert snap["content"]["toolCallId"] == tool_start["toolCallId"], \
        f"活动帧归不了卡:content={snap['content']['toolCallId']!r} " \
        f"TOOL_CALL_START={tool_start['toolCallId']!r}"
    assert start.get("parentToolCallId") == tool_start["toolCallId"], \
        "SUBAGENT_STARTED 的 parentToolCallId 也要指向同一张卡"


#: 会**再往下派**的角色:工具清单里必须带 `subagent`(否则二级根本没发生,不是 UI 的问题)
_COORDINATOR = """---
name: coordinator
description: 指挥的
tools: ["read", "subagent"]
---
你是 coordinator。你可以把子任务再派出去。
"""

_ROLE_SCOUT = """---
name: scout
description: 探路的
tools: ["read"]
---
你是 scout。
"""


class _NestedLLM:
    """主 → coordinator → scout 两级委派。"""

    def __init__(self) -> None:
        self.delegated = False
        self.inner = False

    async def chat(self, messages, tools=None, temperature=None):  # pragma: no cover
        raise AssertionError("走 astream")

    async def astream(self, messages, tools=None, temperature=None):
        from qi_agent.llm import LLMDelta, ToolCallOut

        head = str(getattr(messages[0], "content", "") or "")
        if "标题" in head[:40]:
            yield LLMDelta(text="两级委派")
            yield LLMDelta(finished=True, usage={"total_tokens": 1})
            return
        if "scout" in head:
            yield LLMDelta(text="scout 读完了")
            yield LLMDelta(finished=True, usage={"total_tokens": 1})
            return
        if "coordinator" in head:
            if not self.inner:                      # 只派一次,否则递归到天荒地老
                self.inner = True
                yield LLMDelta(text="coordinator 再派 scout")
                yield LLMDelta(finished=True, usage={"total_tokens": 1},
                               tool_calls=[ToolCallOut(id="call_inner", name="subagent",
                                                       args={"agent": "scout",
                                                             "task": "读 README"})])
                return
            yield LLMDelta(text="coordinator 收尾")
            yield LLMDelta(finished=True, usage={"total_tokens": 1})
            return
        if not self.delegated:
            self.delegated = True
            yield LLMDelta(text="我派 coordinator")
            yield LLMDelta(finished=True, usage={"total_tokens": 1},
                           tool_calls=[ToolCallOut(id="call_outer", name="subagent",
                                                   args={"agent": "coordinator",
                                                         "task": "安排一下"})])
            return
        yield LLMDelta(text="都完成了")
        yield LLMDelta(finished=True, usage={"total_tokens": 1})


def test_nested_delegation_shows_every_level_on_the_delegation_card(tmp_path, monkeypatch):
    """二级(嵌套)委派也要显示:两个 `SUBAGENT_STARTED`、活动归到**同一张工具卡**。

    一棵委派树在前端是「一张工具卡 + 里面平铺的若干子智能体卡」(靠 depth 缩进),
    所以二级不出现 = 用户会以为"再次指派没反应"。
    """
    frames = _frames(tmp_path, monkeypatch, _NestedLLM(),
                     roles={"coordinator": _COORDINATOR, "scout": _ROLE_SCOUT})
    starts = [f for f in frames if f.get("type") == "SUBAGENT_STARTED"]
    names = [f.get("name") for f in starts]
    assert "coordinator" in names, f"一级没派起来:{names}"
    assert "scout" in names, f"二级(嵌套)没派起来或没显示:{names}"

    tool_start = next(f for f in frames if f["type"] == "TOOL_CALL_START")
    card = tool_start["toolCallId"]
    # 两个层级的活动都收敛到**同一张**工具卡(这就是"一棵树一张卡")
    for f in starts:
        assert f["parentToolCallId"] == card, f"{f['name']} 归到了别的卡:{f['parentToolCallId']}"
    nested = next(f for f in starts if f["name"] == "scout")
    assert nested["metadata"]["depth"] == 2, "二级的深度要标对(前端靠它缩进)"
    assert nested.get("parentSubagentRunId") == "coordinator", "二级要指认它的父"

    acts = [f for f in frames if f.get("type") == "ACTIVITY_SNAPSHOT"
            and f.get("activityType") == "qi.subagent"]
    runs = {f["content"]["run"] for f in acts}
    assert len(runs) >= 2, f"两级活动没收进同一张卡:{runs}"
    assert all(f["content"]["toolCallId"] == card for f in acts), "活动帧也归同一张卡"


#: 三级链的顶:只编排,把活原样交给 lead(正文刻意不写角色名,免得被 LLM 脚本误判)
_TREE = """---
name: tree
description: 三级委派测试
tools: ["subagent"]
---
你是 tree,一个只做编排的角色:把收到的任务原样转交给下一级去安排。
"""


class _ThreeLevelLLM:
    """主 → tree(深1)→ lead(深2)→ scout(深3)。

    按 system prompt 的**第一句**分流(不能按“包含角色名”:角色的正文里会提到下一级)。
    """

    def __init__(self) -> None:
        self.seen: set[str] = set()

    async def chat(self, messages, tools=None, temperature=None):  # pragma: no cover
        raise AssertionError("走 astream")

    async def astream(self, messages, tools=None, temperature=None):
        from qi_agent.llm import LLMDelta, ToolCallOut

        head = str(getattr(messages[0], "content", "") or "")
        first = head.strip().splitlines()[0] if head.strip() else ""

        if first.startswith("你负责给一段对话起标题") or "起标题" in first[:40]:
            yield LLMDelta(text="三级委派")
            yield LLMDelta(finished=True, usage={"total_tokens": 1})
            return
        tool = None
        if first.startswith("你是 scout"):
            text = "scout 读完了"
        elif first.startswith("你是 coordinator"):
            if "coordinator" not in self.seen:
                self.seen.add("coordinator")
                text, tool = "coordinator 再派 scout", ("scout", "call_inner")
            else:
                text = "coordinator 收尾"
        elif first.startswith("你是 tree"):
            if "tree" not in self.seen:
                self.seen.add("tree")
                text, tool = "tree 派 coordinator", ("coordinator", "call_mid")
            else:
                text = "tree 收尾"
        else:                                    # 主运行
            if "main" not in self.seen:
                self.seen.add("main")
                text, tool = "我派 tree", ("tree", "call_root")
            else:
                text = "全部完成"
        yield LLMDelta(text=text)
        calls = []
        if tool is not None:
            calls = [ToolCallOut(id=tool[1], name="subagent",
                                 args={"agent": tool[0], "task": "安排一下"})]
        yield LLMDelta(finished=True, usage={"total_tokens": 1}, tool_calls=calls)


def test_three_level_delegation_keeps_a_depth_chain_on_one_card(tmp_path, monkeypatch):
    """三级委派(tree → coordinator → scout)整棵树都在同一张卡上,深度与父子链都要对。

    `MAX_DEPTH = 3`,所以这条链正好顶到上限 —— 多一级就该被拒(那是另一条测试的事),
    而少一级、深度错、父子链断掉,用户看到的就是“树只画了一半”。
    """
    frames = _frames(tmp_path, monkeypatch, _ThreeLevelLLM(),
                     roles={"tree": _TREE, "coordinator": _COORDINATOR, "scout": _ROLE_SCOUT})
    starts = [f for f in frames if f.get("type") == "SUBAGENT_STARTED"]
    names = [f["name"] for f in starts]
    assert names == ["tree", "coordinator", "scout"], f"三级链没跑起来:{names}"

    depths = [f["metadata"]["depth"] for f in starts]
    assert depths == [1, 2, 3], f"深度标错:{depths}"
    parents = [f.get("parentSubagentRunId") for f in starts]
    assert parents[0] is None, "顶层由主 agent 派:没有父 run id"
    assert parents[1:] == ["tree", "coordinator"], f"父子链断了:{parents}"

    card = next(f for f in frames if f["type"] == "TOOL_CALL_START")["toolCallId"]
    assert all(f["parentToolCallId"] == card for f in starts), "整棵树必须归同一张卡"
    acts = [f for f in frames if f.get("type") == "ACTIVITY_SNAPSHOT"
            and f.get("activityType") == "qi.subagent"]
    assert len({f["content"]["run"] for f in acts}) == 3, "三层的活动都要在"
