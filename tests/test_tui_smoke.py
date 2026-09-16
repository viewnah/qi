"""P9:回归集结构校验 + TUI 冒烟(stub 运行时,无网络)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_MODELS = (
    '{"providers": {"ollama": {"api": "openai-completions", '
    '"models": [{"id": "x"}]}}}'
)
# 默认模型属于 settings.json(对齐 pi)
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def test_router_cases_yaml_structure():
    import yaml

    cases = yaml.safe_load(Path(__file__).parent.joinpath("router_cases.yaml").read_text(encoding="utf-8"))
    assert isinstance(cases, list) and len(cases) >= 7
    for c in cases:
        assert "input" in c and "expect" in c
    # 样例 agent 覆盖回归集用到的名字
    agents_dir = Path(__file__).resolve().parents[1] / "examples" / "agents"
    have = {p.name for p in agents_dir.iterdir() if (p / "agent.md").exists()}
    for c in cases:
        if not str(c["expect"]).startswith("@"):
            assert c["expect"] in have, f"回归集期望 {c['expect']} 但样例里没有(have={have})"


def _tui_env(tmp_path, monkeypatch) -> None:
    """最小可启动环境:一份 models.json + 空的 QI_AGENT_HOME。"""
    from qi_agent import paths

    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))


@pytest.mark.asyncio
async def test_tui_smoke_import(tmp_path, monkeypatch):
    _tui_env(tmp_path, monkeypatch)
    from qi_agent.tui import Editor, QiTui

    app = QiTui()
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app.query_one("#editor", Editor).load_text("/quit")
        await pilot.press("enter")          # 多行编辑器:enter = 提交
        await pilot.pause(0.05)
    assert True  # 冒烟:能启动并退出


def test_tui_quit_action_is_textual_builtin():
    """不再自定义 action_quit:复用基类,避免与 async 基类方法签名不兼容。"""
    from textual.binding import Binding

    from qi_agent.tui import QiTui

    # 基类(App)提供 action_quit,qi 不再覆盖
    assert getattr(QiTui.action_quit, "__qualname__", "").startswith("App.")
    # 且绑定表把 ctrl+c 映射到 pi 的「清空/退出」语义(不再是 ctrl+c 直接退)
    actions: dict[str, str] = {}
    for b in QiTui.BINDINGS:
        if isinstance(b, Binding):
            actions[b.key] = b.action
        else:  # BindingType 的元组形态 (key, action, description)
            actions[b[0]] = b[1]
    assert actions.get("ctrl+c") == "clear_or_exit"


def test_tui_bindings_match_pi():
    """键位对齐 pi `core/keybindings.js`:同名同义(取不到的已在 /hotkeys 标注)。"""
    from textual.binding import Binding

    from qi_agent.tui import QiTui

    actions: dict[str, str] = {}
    for b in QiTui.BINDINGS:
        assert isinstance(b, Binding)
        actions[b.key] = b.action
    assert actions == {
        "escape": "interrupt",
        "ctrl+c": "clear_or_exit",
        "ctrl+d": "exit_or_delete",
        "ctrl+o": "toggle_expand",
        "ctrl+t": "toggle_thinking",
        "ctrl+x": "copy_answer",
        "ctrl+g": "external_editor",
        "ctrl+l": "select_model",
        "ctrl+p": "cycle_model",
        "ctrl+shift+p": "cycle_model_back",
        "ctrl+z": "suspend_process",
    }


@pytest.mark.asyncio
async def test_tui_ctrl_c_clears_then_exits(tmp_path, monkeypatch):
    """ctrl+c:有输入 = 清空;再按一次 = 退出(对齐 pi 的 ctrl+c 双击退出)。"""
    _tui_env(tmp_path, monkeypatch)
    from qi_agent.tui import Editor, QiTui

    app = QiTui()
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        field = app.query_one("#editor", Editor)
        field.load_text("写了一半\n第二行")   # 多行也要能一次清空
        await pilot.press("ctrl+c")
        await pilot.pause(0.05)
        assert field.text == ""             # 第一次只清空
        assert app._exit is False
        await pilot.press("ctrl+c")         # 空编辑器再按 = 退出
        await pilot.pause(0.05)
        assert app._exit is True


@pytest.mark.asyncio
async def test_tui_ctrl_d_exits_only_when_empty(tmp_path, monkeypatch):
    """ctrl+d:空输入框退出;非空则删右侧字符,不退出。"""
    _tui_env(tmp_path, monkeypatch)
    from qi_agent.tui import Editor, QiTui

    app = QiTui()
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        field = app.query_one("#editor", Editor)
        field.load_text("abc")
        field.move_cursor((0, 0))
        await pilot.press("ctrl+d")
        await pilot.pause(0.05)
        assert field.text == "bc"           # 删右侧字符
        assert app._exit is False
        field.load_text("")
        await pilot.press("ctrl+d")
        await pilot.pause(0.05)
        assert app._exit is True


@pytest.mark.asyncio
async def test_tui_initial_prompt_is_submitted(tmp_path, monkeypatch):
    """`qi "问题"` = 进 TUI 后自动提交该消息(用 `/mode manual` 验证其生效)。"""
    _tui_env(tmp_path, monkeypatch)
    from qi_agent.tui import QiTui

    assert QiTui()._initial_prompt is None      # 裸 `qi` 不自动提交
    app = QiTui(initial_prompt="/mode manual")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert app._auto is False, "初始消息未被提交"


@pytest.mark.asyncio
async def test_tui_ctrl_c_twice_quits(tmp_path, monkeypatch):
    """ctrl+c → 清空/退出(pi 语义):连按两次才退出。

    回归:qi 曾把 ctrl+c 直接绑到 quit。现在对齐 pi 的 app.clear/app.exit。
    """
    _tui_env(tmp_path, monkeypatch)
    from qi_agent.tui import QiTui

    app = QiTui()
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert app._exit is False
        await pilot.press("ctrl+c")
        await pilot.pause(0.05)
        assert app._exit is False, "第一次 ctrl+c 只应该 arm,不应该退出"
        await pilot.press("ctrl+c")
        await pilot.pause(0.05)
        assert app._exit is True, "第二次 ctrl+c 应该退出"


def test_inline_driver_tolerates_x10_mouse_bytes():
    """回归:旧式 X10 鼠标报文(`ESC [ M` + 原始坐标字节)不是合法 UTF-8。

    Textual 的 inline 驱动用严格 UTF-8 解码器读 stdin,坐标 ≥ 0x80 时
    `UnicodeDecodeError` 在输入线程抛出 → `App.panic` → TUI 带栈退出
    (textualize/textual#6456)。加固后应替换坏字节而不是抛异常。
    """
    from textual.drivers import linux_inline_driver as drv

    from qi_agent.tui import _harden_inline_input

    _harden_inline_input()
    _harden_inline_input()  # 幂等
    # 驱动内部就是 `getincrementaldecoder("utf-8")()`;这里照抄同一调用形态。
    factory = getattr(drv, "getincrementaldecoder")
    decode = factory("utf-8")().decode

    report = b"\x1b[MC\x85\x85\x85"  # Cx=0x85:C(0x43) 之后再遇 0x85 → position 4
    assert "\ufffd" in decode(report, final=False)


def test_reset_mouse_reporting_writes_disable_sequences(monkeypatch):
    """崩溃残留的鼠标上报必须在进界面前关掉。

    否则残留的 X10 上报会让鼠标一动就发原始坐标字节,被加固后的解码器替换成
    U+FFFD 灌进输入框(报错里的 `^[[MC` 就是这么漏进 shell 的)。
    """
    import io

    from qi_agent.tui import _MOUSE_OFF, _reset_mouse_reporting

    assert "\x1b[?1000l" in _MOUSE_OFF and "\x1b[?1006l" in _MOUSE_OFF
    stream = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", stream)
    _reset_mouse_reporting()
    assert stream.getvalue() == _MOUSE_OFF


def test_run_tui_runs_inline_without_mouse(tmp_path, monkeypatch):
    """TUI 关掉鼠标上报:免得终端退回 X10 报文(见上一条),也让原生选择/复制可用。"""
    _tui_env(tmp_path, monkeypatch)
    from qi_agent import tui as tui_mod

    calls: dict[str, dict[str, object]] = {}

    class FakeApp:
        def __init__(self, **kwargs: object) -> None:
            calls["init"] = kwargs

        def run(self, **kwargs: object) -> None:
            calls["run"] = kwargs

    monkeypatch.setattr(tui_mod, "QiTui", FakeApp)
    monkeypatch.setattr(tui_mod, "resolve_theme", lambda *a, **k: None)
    monkeypatch.setattr(tui_mod, "_reset_mouse_reporting", lambda: None)

    tui_mod.run_tui("你好")

    assert calls["run"] == {"inline": True, "inline_no_clear": True, "mouse": False}
    assert calls["init"]["initial_prompt"] == "你好"
