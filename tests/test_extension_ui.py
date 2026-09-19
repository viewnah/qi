"""P-E3a:`ctx.ui` —— 扩展向前端要交互的唯一入口。

这一块的重点**不是**“能弹窗”,而是**没窗的时候怎么办**。`-p` / CI / 脚本里没有人可问,
而扩展代码不会为此写两套分支 —— 所以:

* 没有前端时每个方法返回**调用方给的 `default`**(`confirm` 默认 False),于是
  "交互"退化成"按事先声明好的策略走",而**永远不会挂住**;
* `notify` 在没有前端时进 `notes`,**不丢弃** —— 否则“扩展说了一句话”就凭空消失;
* 前端自己抛异常也走 `default`(记一条 note):交互是辅助手段,不该成为新的失败点。

还有一条端到端要钉:扩展在 `tool_call` 里用 `confirm` 当闸门、而在无头环境里必然拿到
False —— 这正好把"拿不准就不做"这个安全边落到实处。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.extensions import ExtensionContext, ExtensionUi  # noqa: E402
from qi_agent.llm import ChatResponse, ToolCallOut  # noqa: E402
from qi_agent.registry import EXTENSION_ENTRY_FILE  # noqa: E402

_MINIMAL_MODELS = json.dumps({
    "providers": {"ollama": {"api": "openai-completions",
                             "baseUrl": "http://127.0.0.1:11434/v1",
                             "models": [{"id": "x"}]}},
})


# ── 无前端:确定默认 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_defaults_to_false_without_a_frontend():
    """**安全边**:没人可问时“不做”。"""
    ui = ExtensionUi()
    assert await ui.confirm("真的要做吗?") is False
    assert await ui.confirm("只读操作?", default=True) is True     # 但调用方可以改


@pytest.mark.asyncio
async def test_select_and_input_return_the_given_default_without_a_frontend():
    ui = ExtensionUi()
    assert await ui.select("选一个", ["a", "b"]) is None
    assert await ui.select("选一个", ["a", "b"], default="b") == "b"
    assert await ui.input("说点什么") is None
    assert await ui.input("说点什么", default="默认值") == "默认值"


def test_notify_without_a_frontend_lands_in_notes():
    """不丢弃 —— 事情发生了但没人看见是最难诊断的一类。"""
    notes: list[str] = []
    ExtensionUi(notes=notes).notify("索引完成:17 个文件")
    assert notes == ["索引完成:17 个文件"]


def test_has_frontend_reports_whether_anyone_can_be_asked():
    assert ExtensionUi().has_frontend is False
    assert ExtensionUi(frontend=object()).has_frontend is True


# ── 有前端:转发与兜底 ───────────────────────────────────

class _Frontend:
    """记下被问了什么,按 `answers` 依次回答(或抛异常)。"""

    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, tuple, dict]] = []
        self.notices: list[tuple[str, str]] = []

    def _next(self):
        value = self.answers.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def confirm(self, message, *, title=None, default=False):
        self.calls.append(("confirm", (message,), {"title": title, "default": default}))
        return self._next()

    async def select(self, message, options, *, title=None, default=None):
        self.calls.append(("select", (message, tuple(options)),
                           {"title": title, "default": default}))
        return self._next()

    async def input(self, message, *, title=None, default=None, secret=False):
        self.calls.append(("input", (message,),
                           {"title": title, "default": default, "secret": secret}))
        return self._next()

    def notify(self, message, *, level="info"):
        self.notices.append((message, level))


@pytest.mark.asyncio
async def test_requests_are_forwarded_to_the_frontend():
    front = _Frontend([True, "b", "输入的文本"])
    ui = ExtensionUi(frontend=front)

    assert await ui.confirm("继续?", title="危险操作") is True
    assert await ui.select("哪一个", ["a", "b"], default="a") == "b"
    assert await ui.input("名字", secret=True) == "输入的文本"

    assert front.calls[0] == ("confirm", ("继续?",), {"title": "危险操作", "default": False})
    assert front.calls[1][1][1] == ("a", "b")          # options 原样传过去
    assert front.calls[2][2]["secret"] is True


@pytest.mark.asyncio
async def test_frontend_cancel_falls_back_to_default():
    """前端返回 None = 用户取消 → 与“没人可问”走同一条路(用 default)。"""
    assert await ExtensionUi(frontend=_Frontend([None])).confirm("x", default=True) is True
    assert await ExtensionUi(frontend=_Frontend([None])).select("x", ["a"]) is None


@pytest.mark.asyncio
async def test_a_broken_frontend_falls_back_instead_of_raising():
    """界面坏不等于回合该失败:走 default,并留一条 note。"""
    notes: list[str] = []
    ui = ExtensionUi(frontend=_Frontend([RuntimeError("界面炸了")]), notes=notes)

    assert await ui.confirm("继续?") is False
    assert await ui.select("选", ["a"]) is None
    assert await ui.input("说") is None
    assert len(notes) == 3
    assert all("前端处理失败" in n for n in notes)


def test_notify_goes_to_the_frontend_and_not_notes():
    front = _Frontend([])
    notes: list[str] = []
    ExtensionUi(frontend=front, notes=notes).notify("好了", level="warning")
    assert front.notices == [("好了", "warning")]
    assert notes == []                                 # 有人看就别塞进启动提示


def test_notify_falls_back_to_notes_when_the_frontend_breaks():
    class _Bad:
        def notify(self, message, *, level="info"):
            raise RuntimeError("画不出来")

    notes: list[str] = []
    ExtensionUi(frontend=_Bad(), notes=notes).notify("重要的一句话")
    assert "重要的一句话" in notes                      # 至少有地方能看到


def test_ctx_ui_is_always_present():
    """`ctx.ui` 不是 Optional —— 扩展不必到处写 `if ctx.ui is not None`。"""
    ctx = ExtensionContext(cwd=Path("/tmp"))
    assert ctx.ui is not None
    assert ctx.ui.has_frontend is False


# ── TUI 后端:模态映射(用 stub app,不渲染) ─────────────

class _StubApp:
    """只实现 `_TuiUi` 用到的那两样(`_note` 与 `await_screen`)。"""

    def __init__(self, answer=None) -> None:
        self.answer = answer
        self.notes: list[tuple[str, str]] = []
        self.screens: list = []

    def _note(self, text: str, tone: str = "dim") -> None:
        self.notes.append((text, tone))

    async def await_screen(self, screen):
        self.screens.append(screen)
        return self.answer


@pytest.mark.asyncio
async def test_tui_backend_maps_confirm_to_a_picker():
    from qi_agent.tui import _TuiUi

    app = _StubApp(answer="yes")
    ui = ExtensionUi(frontend=_TuiUi(app))
    assert await ui.confirm("删掉这个文件?", title="危险") is True
    screen = app.screens[0]
    assert "危险" in screen._title and "删掉这个文件?" in screen._title
    assert [value for value, _ in screen._options] == ["yes", "no"]


@pytest.mark.asyncio
async def test_tui_backend_confirm_uses_the_callers_default_on_escape():
    """escape 关掉模态 = 没回答 → 落回 default(与无界面时同一条路)。"""
    from qi_agent.tui import _TuiUi

    ui = ExtensionUi(frontend=_TuiUi(_StubApp(answer=None)))
    assert await ui.confirm("x", default=True) is True
    assert await ui.confirm("x", default=False) is False


@pytest.mark.asyncio
async def test_tui_backend_marks_which_option_is_the_default():
    from qi_agent.tui import _TuiUi

    app = _StubApp(answer=None)
    await ExtensionUi(frontend=_TuiUi(app)).confirm("x", default=True)
    labels = [label for _, label in app.screens[0]._options]
    assert any("默认" in label for label in labels)     # 人得看得出哪个是“不选也是它”


@pytest.mark.asyncio
async def test_tui_backend_select_passes_options_through():
    from qi_agent.tui import _TuiUi

    app = _StubApp(answer="b")
    assert await ExtensionUi(frontend=_TuiUi(app)).select("选", ["a", "b"]) == "b"
    assert [value for value, _ in app.screens[0]._options] == ["a", "b"]


def test_tui_backend_notify_maps_level_to_tone():
    from qi_agent.tui import _TuiUi

    app = _StubApp()
    backend = _TuiUi(app)
    backend.notify("平常的")
    backend.notify("警告", level="warning")
    backend.notify("错误", level="error")
    assert app.notes == [("平常的", "dim"), ("警告", "warning"), ("错误", "error")]


@pytest.mark.asyncio
async def test_await_screen_returns_none_when_the_app_is_not_running():
    """界面没在跑时不挂一个永远不会被解析的 future,直接返回 None。

    没有这条护栏的话,任何“构造了 App 但没 `run()`”的场景(测试、脚本嵌入)一旦
    走到 `ctx.ui`,就会静默挂死 —— 而挂死比报错难查得多。
    """
    from qi_agent.theme import resolve_theme
    from qi_agent.tui import PickerScreen, QiTui

    app = QiTui(palette=resolve_theme(probe=False))
    assert app.is_running is False
    assert await app.await_screen(PickerScreen("x", [("a", "a")])) is None


# ── 端到端:无头环境的 fail-safe + clarify 的回归 ────────

def _env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "models.json").write_text(_MINIMAL_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x"}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)


def _install(tmp_path: Path, name: str, body: str) -> None:
    d = tmp_path / "proj" / ".qi" / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(body, encoding="utf-8")


class _ScriptedLLM:
    def __init__(self, script: list[ChatResponse]) -> None:
        self.script = list(script)
        self.calls: list[list] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append(list(messages))
        return self.script.pop(0) if self.script else ChatResponse(text="完")

    def tool_messages(self, index: int) -> list[str]:
        return [m.content for m in self.calls[index] if m.role == "tool"]


def _call(name: str, args: dict) -> ChatResponse:
    return ChatResponse(text="", tool_calls=[ToolCallOut(id="c1", name=name, args=args)])


_ASKING_GATE = """
import json
from pathlib import Path

from qi_agent.extensions import Tool

LOG = Path({log!r})


async def touch(args, ctx):
    LOG.write_text("跑了", encoding="utf-8")
    return "碰过了"


def register(api):
    api.registerTool(Tool("touch", "写个文件", {{
        "type": "object", "properties": {{}}}}, touch))

    async def gate(payload, ctx):
        # 拿不准就不做:没有界面时 ctx.ui.confirm 必然返回 False
        ok = await ctx.ui.confirm("允许 touch 吗?", title="危险操作", default=False)
        if not ok:
            return {{"block": True, "reason": "用户没有明确同意"}}

    api.on("tool_call", gate)
"""


async def _run(tmp_path, monkeypatch, llm, frontend=None):
    (tmp_path / "proj" / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch)
    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=tmp_path / "proj", disable_router=True,
                        approve_project=True, llm=llm, ui_frontend=frontend)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    events = [e async for e in runtime.stream("跑一下", session)]
    return runtime, events


@pytest.mark.asyncio
async def test_ui_gate_blocks_when_nobody_can_be_asked(tmp_path, monkeypatch):
    """无头环境(`-p` / CI)里 `confirm` 必然 False → **闸门拦住**。

    这条是“拿不准就不做”的落地证明:扩展不需要为无头环境写第二套分支,
    而是它的 default 天然就是安全边。
    """
    llm = _ScriptedLLM([_call("touch", {}), ChatResponse(text="被拦了")])
    log = tmp_path / "touch.txt"
    _install(tmp_path, "gate", _ASKING_GATE.format(log=str(log)))
    _runtime, events = await _run(tmp_path, monkeypatch, llm)

    assert not log.exists()                              # 工具真的没跑
    assert [e for e in events if e.kind == "tool_end"][0].data["error"] == "denied"
    assert "用户没有明确同意" in llm.tool_messages(1)[0]


@pytest.mark.asyncio
async def test_ui_gate_runs_when_the_frontend_says_yes(tmp_path, monkeypatch):
    """有人可问、且他点了“是” → 工具就跑。"""
    llm = _ScriptedLLM([_call("touch", {}), ChatResponse(text="好了")])
    log = tmp_path / "touch.txt"
    _install(tmp_path, "gate", _ASKING_GATE.format(log=str(log)))
    front = _Frontend([True])
    _runtime, _events = await _run(tmp_path, monkeypatch, llm, frontend=front)

    assert log.read_text(encoding="utf-8") == "跑了"
    assert front.calls[0][0] == "confirm"
    assert front.calls[0][2]["default"] is False          # 默认值一路传到了前端


@pytest.mark.asyncio
async def test_ui_gate_respects_an_explicit_no(tmp_path, monkeypatch):
    llm = _ScriptedLLM([_call("touch", {}), ChatResponse(text="被拦了")])
    log = tmp_path / "touch.txt"
    _install(tmp_path, "gate", _ASKING_GATE.format(log=str(log)))
    _runtime, events = await _run(tmp_path, monkeypatch, llm, frontend=_Frontend([False]))

    assert not log.exists()
    assert [e for e in events if e.kind == "tool_end"][0].data["error"] == "denied"


@pytest.mark.asyncio
async def test_clarify_can_now_actually_ask(tmp_path, monkeypatch):
    """回归:内置 `clarify` 以前**从没问过人**(`_ask` 无条件返回 None)。

    接上 `ctx.ui` 之后,有前端时它应当真的问并把答案带回给模型。无头时行为不变。
    """
    llm = _ScriptedLLM([_call("clarify", {"question": "要哪个环境?"}),
                        ChatResponse(text="好")])
    _env(tmp_path, monkeypatch)

    # 1) 有前端 → 问到答案
    from qi_agent.runtime import QiRuntime

    front = _Frontend(["预发布"])
    runtime = QiRuntime(cwd=tmp_path, disable_router=True, llm=llm, ui_frontend=front)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    async for _e in runtime.stream("帮我部署", session):
        pass
    assert "用户回答: 预发布" in llm.tool_messages(1)[0]
    assert front.calls[0][0] == "input"

    # 2) 无前端 → 与从前一致(不吃 stdin,不挂住)
    llm2 = _ScriptedLLM([_call("clarify", {"question": "要哪个环境?"}), ChatResponse(text="好")])
    runtime2 = QiRuntime(cwd=tmp_path, disable_router=True, llm=llm2)
    session2 = runtime2.sessions.create("t", cwd=runtime2.cwd)
    async for _e in runtime2.stream("帮我部署", session2):
        pass
    assert "[需要用户澄清]" in llm2.tool_messages(1)[0]
