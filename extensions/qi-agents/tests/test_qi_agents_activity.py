"""委派直播的**推送端**(design/web-lab.md 二期):`_run_one` 经 `ctx.on_update` 推活动。

钉三件事:

1. **真会推**:派单即 `start`,思考 / 工具 / 流式各有帧,收束 `done`;
2. **归因正确**:`run` / `role` / `parent` / `depth` 与平铺架子同源(嵌套时 depth = 父+1,
   parent = 发起者的名字)—— 前端靠它把整棵树排进一张卡;
3. **逐字一致**(回放红线):直播的 `line` 与结束后进 result 的同名行**一个字不差**。
   关掉直播,回放看到的是同一批文字;两边各写一套,迟早分叉。

不推的场景同样钉住:ctx 没有 `on_update`(handler 路径)→ 一帧不推、运行照常。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]   # tests/ → extensions/
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-agents"))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402

_MODELS = ('{"providers": {"ollama": {"api": "openai-completions", '
           '"models": [{"id": "x"}]}}}')
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _env(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)
    return home


def _write_role(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(
        f"---\nname: {name}\ndescription: 做这个的\ntools: [\"read\"]\n---\n你是 {name}。",
        encoding="utf-8")
    return d


class _Ui:
    """最小时间线 sink(直播测试不关心时间线,只要它不挡路)。"""

    has_frontend = True        # 前端在看:时间线只出节点(不影响直播断言)

    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, message: str, *, level: str = "info") -> None:
        self.messages.append(str(message))


class _Card:
    """最小平铺卡:记下收到的流式与活动,别的不管。"""

    def __init__(self) -> None:
        self.streams: list[tuple[str, str]] = []
        self.activity: list[str] = []

    def add_agent(self, name, parent, depth):
        return self

    def add_activity(self, line: str) -> None:
        self.activity.append(line)

    def set_stream(self, text: str, mode: str) -> None:
        self.streams.append((mode, text))

    def set_done(self, ok, output, thoughts) -> None:
        pass


class _Api:
    """按脚本回放子运行事件;`script` 是可调用列表(on_event → None)。"""

    def __init__(self, steps) -> None:
        self._steps = list(steps)

    async def run_agent(self, spec, task, *, abort=None, on_event=None):
        for step in self._steps:
            if on_event is not None:
                step(on_event)
        return "子结果"


def _act(tool="read", **args):
    return lambda emit: emit(SimpleNamespace(kind="tool_start", tool=tool,
                                             data={"args": args}))


def _think(text):
    return lambda emit: emit(SimpleNamespace(kind="assistant_message",
                                             data={"thinking": text}))


def _delta(text, kind="text_delta"):
    return lambda emit: emit(SimpleNamespace(kind=kind, text=text))


def _collect() -> list:
    """`ctx.on_update` 的收集器 —— 与 runner 拼出的 bus payload 同形。"""
    return []


@pytest.mark.asyncio
async def test_pushes_start_act_think_done_with_attribution(tmp_path, monkeypatch):
    """派单即 start;act/think/done 各就各位;归因字段全在。"""
    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    sub._LAST_TITLE = ""
    _write_role(tmp_path / "home" / "agents", "scout")
    role = sub.discover(None, "user")["scout"]

    updates = _collect()
    out = await sub._run_one(
        _Api([_think("先定范围"), _act(path="a.py")]), role, "读一遍",
        ToolContext(agent_name="qi", workdir=tmp_path, ui=cast(Any, _Ui()),
                    on_update=updates.append))

    assert out["ok"] is True
    phases = [u["qi"]["phase"] for u in updates]
    assert phases[0] == "start" and phases[-1] == "done", phases
    assert "act" in phases and "think" in phases, phases
    first = updates[0]["qi"]
    assert first["kind"] == "subagent" and first["role"] == "scout"
    assert first["parent"] == "qi" and first["depth"] == 1
    assert first["task"] == "读一遍" and first["run"].startswith("scout")
    done = updates[-1]["qi"]
    assert done["ok"] is True and done["error"] == ""
    # act 帧与 thought 帧的归因同样齐(不是只有 start 带)
    act = next(u["qi"] for u in updates if u["qi"]["phase"] == "act")
    assert act["run"] == first["run"] and act["depth"] == 1


@pytest.mark.asyncio
async def test_live_lines_are_verbatim_the_result_lines(tmp_path, monkeypatch):
    """回放红线:直播行与 result 段的同名行逐字一致(截断规则也一致)。"""
    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    sub._LAST_TITLE = ""
    _write_role(tmp_path / "home" / "agents", "scout")
    role = sub.discover(None, "user")["scout"]

    updates = _collect()
    out = await sub._run_one(
        _Api([_think("第一轮:先定范围"), _act(path="a.py"), _think("第二轮:再看细节")]),
        role, "x", ToolContext(agent_name="qi", workdir=tmp_path, ui=cast(Any, _Ui()),
                               on_update=updates.append))

    lines = [u["qi"].get("line") for u in updates if u["qi"]["phase"] in ("act", "think")]
    steps = out["steps"]
    thoughts = [f"✎ 第{i + 1}轮: {t}" for i, t in enumerate(out["thoughts"])]
    assert lines == [thoughts[0], steps[0], thoughts[1]], \
        f"直播行与 result 段必须同字:{lines} vs {thoughts + steps}"


@pytest.mark.asyncio
async def test_nested_run_attributes_depth_and_parent(tmp_path, monkeypatch):
    """嵌套:depth = 父层 + 1、parent = 发起者的名字(与平铺架子同源)。"""
    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    sub._LAST_TITLE = ""
    _write_role(tmp_path / "home" / "agents", "scout")
    role = sub.discover(None, "user")["scout"]

    updates = _collect()
    parent = sub._RunInfo(depth=1, tools=["read"], budget=sub._Budget(8),
                          name="coordinator")
    out = await sub._run_one(
        _Api([]), role, "x",
        ToolContext(agent_name="coordinator", workdir=tmp_path, ui=cast(Any, _Ui()),
                    on_update=updates.append),
        parent=parent)

    assert out["depth"] == 2
    frame = updates[0]["qi"]
    assert frame["depth"] == 2 and frame["parent"] == "coordinator", frame


@pytest.mark.asyncio
async def test_stream_frame_carries_the_whole_round(tmp_path, monkeypatch):
    """流式帧带**本轮全量**(与刷进卡的同一份),不是逐字 delta —— 节流在调用方。"""
    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    sub._LAST_TITLE = ""
    _write_role(tmp_path / "home" / "agents", "scout")
    role = sub.discover(None, "user")["scout"]

    updates = _collect()
    await sub._run_one(
        _Api([_delta("先想", "thinking_delta"), _delta("一点直出文本"),
              _think("本轮结束")]),
        role, "x", ToolContext(agent_name="qi", workdir=tmp_path, ui=cast(Any, _Ui()),
                               on_update=updates.append),
        holder=[_Card()])

    streams = [u["qi"] for u in updates if u["qi"]["phase"] == "stream"]
    assert streams, "有卡就有流式帧(节流由 STREAM_FLUSH_INTERVAL 与轮末强制决定)"
    texts = [(s["mode"], s["text"]) for s in streams]
    assert ("think", "先想") in texts, texts
    # 轮末的强制 flush 是本轮全量(末尾带思考 → mode 仍是 think)
    assert ("think", "先想一点直出文本") in texts, texts
    assert all(s["kind"] == "subagent" for s in streams)


@pytest.mark.asyncio
async def test_without_on_update_the_run_is_untouched(tmp_path, monkeypatch):
    """handler 路径的 ctx 没有 `on_update`(ExtensionContext)→ 不推也不炸。"""
    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    sub._LAST_TITLE = ""
    _write_role(tmp_path / "home" / "agents", "scout")
    role = sub.discover(None, "user")["scout"]

    ctx = ToolContext(agent_name="qi", workdir=tmp_path, ui=cast(Any, _Ui()))
    assert getattr(ctx, "on_update", None) is None
    out = await sub._run_one(_Api([_act(path="a.py")]), role, "x", ctx)
    assert out["ok"] is True and out["steps"] == ['· read {"path": "a.py"}']


@pytest.mark.asyncio
async def test_a_broken_on_update_does_not_kill_the_run(tmp_path, monkeypatch):
    """推送抛异常 → 旁路就是旁路:子运行照常完成。"""
    from qi_agent.tools import ToolContext

    import qi_agents.subagent as sub

    _env(tmp_path, monkeypatch)
    sub._LAST_TITLE = ""
    _write_role(tmp_path / "home" / "agents", "scout")
    role = sub.discover(None, "user")["scout"]

    def boom(_payload):
        raise RuntimeError("宿主的订阅者炸了")

    out = await sub._run_one(
        _Api([_act(path="a.py")]), role, "x",
        ToolContext(agent_name="qi", workdir=tmp_path, ui=cast(Any, _Ui()), on_update=boom))
    assert out["ok"] is True, "直播失败不该带倒子运行"
