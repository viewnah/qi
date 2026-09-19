"""P-E3b:扩展命令与快捷键(`registerCommand` / `registerShortcut` / `getCommands`)。

命令是**用户显式输入的东西** —— 所以这里的两条判断都围绕“静默失败”展开:

* **重名不覆盖**,而是都留着并加序号(`/review:1`、`/review:2`,对齐 pi)。丢掉一个
  会变成“我装了但打不出来”,而那种现场**无法区分“没装”与“被覆盖”**。
* **保留命令不能被顶掉**(`/quit`、`/help`、`/hotkeys`)。顶掉 `/quit` 等于把用户锁在
  界面里 —— 而那时他已经没法用这个界面改回来了。这是刻意留的例外(其余内置命令
  按 pi 的顺序让扩展先认领)。

TUI 那几条用真 textual(`run_test`)跑:命令表写对了但 TUI 没接上,和没写是一样的。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.extensions import (  # noqa: E402
    CommandRegistry,
    ExtensionApi,
    ExtensionBus,
)
from qi_agent.registry import (  # noqa: E402
    EXTENSION_ENTRY_FILE,
    CapabilityRegistry,
    ToolCatalog,
    discover_extensions,
)
from qi_agent.tools import register_builtin_tools  # noqa: E402

_MODELS = ('{"providers": {"ollama": {"api": "openai-completions", '
           '"models": [{"id": "x"}]}}}')
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)


class _TuiStub:
    """TUI 测试用的最小 runtime。

    **只写一份**:以前在三个测试里各抄一份,而 TUI 与 runtime 的契约每长一项
    (notes / start_session / commands…) 就得改三处 —— 合并后只改这里。

    字段集是照着 TUI 真正读的那几个定的(`self._rt.*`:cfg / commands /
    compact_session / cwd / registry / sessions / settings / start_session /
    summarize_branch_for_jump),多一个不写、少一个就报 `AttributeError`。
    """

    def __init__(self, commands: CommandRegistry | None = None,
                 cwd: Path | None = None) -> None:
        from types import SimpleNamespace

        from qi_agent.session import SessionStore
        from qi_agent.settings import QiSettings

        self.commands = commands or CommandRegistry()
        self.sessions = SessionStore()
        self.cwd = cwd or Path.cwd()
        self.cfg = None
        self.settings = QiSettings()
        self.registry = _StubRegistry()
        self.thinking_level = "off"
        self.llm_exec = SimpleNamespace(thinking_level="off", reasoning_dropped=False)
        self.notes: list[str] = []
        self.top_skills: list = []      # TUI banner 读它(core 持有技能)
        self.extensions: list = []      # /reload 的提示读它

    def extension_ctx(self):
        from qi_agent.extensions import ExtensionContext

        return ExtensionContext(cwd=self.cwd, notes=self.notes)

    async def start_session(self, session, reason: str = "startup") -> None:
        return None

    async def compact_session(self, session, instructions=None):
        return None

    async def summarize_branch_for_jump(self, session, source_branch, from_id, target_id):
        return None


class _StubRegistry:
    """只要 `all()` / `names` / `get()`:TUI 用 `all()` 算 banner 里的技能清单。"""

    names = ("general",)

    def all(self) -> list:
        return []

    def get(self, name: str):
        return None


def _tui_with_stub(monkeypatch, stub: _TuiStub) -> None:
    """把 TUI 的 runtime 工厂换成给定 stub,并给一个可解析的模型。

    **不要**让 `resolve_default_model` 抛异常:TUI 只捕 `ConfigError`,普通异常会直接
    把界面启动打断(试过,报出来的是一行 “Exception: n/a”,与真实失败无关)。
    """
    from qi_agent import tui as tui_mod
    from qi_agent.config import ResolvedModel

    monkeypatch.setattr(tui_mod, "QiRuntime", lambda *a, **kw: stub)
    monkeypatch.setattr(tui_mod, "resolve_default_model",
                        lambda cfg, cwd=None: ResolvedModel(
                            provider="ollama", model="x", api="openai-completions",
                            base_url=None, api_key_ref=None, reasoning=False,
                            context_window=8000, max_tokens=1024))


def _install(tmp_path: Path, name: str, body: str) -> None:
    d = tmp_path / "proj" / ".qi" / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(body, encoding="utf-8")


def _api(registry: CommandRegistry | None = None) -> ExtensionApi:
    return ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(),
                        _commands=registry, _name="probe")


def _cmd(registry: CommandRegistry, name: str):
    """`CommandRegistry.find()` 返回 Optional:显式收窄(与本仓其它测试同一写法)。"""
    command = registry.find(name)
    assert command is not None, f"命令不存在: {name}"
    return command


# ── 登记处:重名、查找、清单 ─────────────────────────────

def test_unique_command_keeps_its_plain_name():
    registry = CommandRegistry()
    registry.add_command("stats", lambda a, c: None, description="看一眼统计", source="x")
    assert registry.names == ["stats"]
    assert _cmd(registry, "stats").description == "看一眼统计"
    assert registry.find("/stats") is not None       # 带斜杠也认


def test_duplicate_commands_all_survive_with_suffixes():
    """两个扩展都注册 `review` → `review:1` / `review:2`,一个都不丢。"""
    registry = CommandRegistry()
    registry.add_command("review", lambda a, c: None, source="one")
    registry.add_command("review", lambda a, c: None, source="two")

    assert registry.names == ["review:1", "review:2"]
    assert _cmd(registry, "review:1").source == "one"      # 顺序 = 装载顺序
    assert _cmd(registry, "review:2").source == "two"
    assert registry.find("review") is None                # 重名时没有裸名字


def test_a_third_duplicate_renumbers_existing_ones():
    registry = CommandRegistry()
    for source in ("one", "two", "three"):
        registry.add_command("review", lambda a, c: None, source=source)
    assert registry.names == ["review:1", "review:2", "review:3"]


def test_commands_are_sorted_and_shortcuts_kept():
    registry = CommandRegistry()
    registry.add_command("zeta", lambda a, c: None)
    registry.add_command("alpha", lambda a, c: None)
    registry.add_shortcut("ctrl+shift+p", lambda c: None, description="切计划模式")
    assert registry.names == ["alpha", "zeta"]
    assert [s.key for s in registry.shortcuts()] == ["ctrl+shift+p"]
    assert registry.is_empty is False


def test_empty_registry():
    registry = CommandRegistry()
    assert registry.is_empty and registry.names == []
    assert registry.find("nope") is None


# ── api 侧:没登记处就报错 ────────────────────────────────

def test_register_command_without_a_registry_is_loud():
    """注册了却没人收,扩展会**以为命令生效了** —— 那种错觉比报错难查得多。"""
    api = _api(None)
    with pytest.raises(RuntimeError, match="registerCommand"):
        api.registerCommand("probe", lambda a, c: None)
    with pytest.raises(RuntimeError, match="registerShortcut"):
        api.registerShortcut("ctrl+p", lambda c: None)


def test_get_commands_reports_source_and_description():
    registry = CommandRegistry()
    api = _api(registry)
    api.registerCommand("stats", lambda a, c: None, description="统计")
    api.registerShortcut("ctrl+shift+p", lambda c: None, description="计划模式")

    assert api.getCommands() == [{"name": "stats", "description": "统计", "source": "probe"}]
    assert [s.source for s in registry.shortcuts()] == ["probe"]


def test_command_name_is_normalized():
    """`/stats` 与 `stats` 是同一个命令 —— 别让扩展作者为前缀写两遍。"""
    registry = CommandRegistry()
    _api(registry).registerCommand("/stats", lambda a, c: None)
    assert registry.names == ["stats"]


# ── 端到端:装载 → 登记 → TUI 分发 ───────────────────────

_COMMAND_EXTENSION = """
import json
from pathlib import Path

LOG = Path({log!r})


async def register(api):
    pass


def _record(kind, payload):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({{"kind": kind, "payload": payload}}) + "\\n")


async def probe(args, ctx):
    _record("command", {{"args": args, "has_ui": ctx.has_ui}})


async def hotkey(ctx):
    _record("shortcut", {{"ui": ctx.has_ui}})


def register(api):
    api.registerCommand("probe", probe, description="探针命令")
    api.registerShortcut("ctrl+shift+j", hotkey, description="探针快捷键")
"""


@pytest.mark.asyncio
async def test_extension_registers_command_and_shortcut(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    log = tmp_path / "calls.jsonl"
    _install(tmp_path, "probe-ext", _COMMAND_EXTENSION.format(log=str(log)))

    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True)
    assert runtime.extensions == ["probe-ext"]
    command = runtime.commands.find("probe")
    assert command is not None and command.description == "探针命令"
    assert [s.key for s in runtime.commands.shortcuts()] == ["ctrl+shift+j"]

    await command.handler("带 参数", runtime.extension_ctx())
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"kind": "command", "payload": {"args": "带 参数", "has_ui": False}}]


@pytest.mark.asyncio
async def test_tui_runs_extension_commands(tmp_path, monkeypatch):
    """真 textual:输入 `/probe 参数` 会跑到扩展的 handler(而不只是写进了表)。"""
    from qi_agent.theme import load_palette
    from qi_agent.tui import QiTui

    _env(tmp_path, monkeypatch)
    registry = CommandRegistry()
    registry.add_command(
        "probe",
        lambda args, ctx: _record(tmp_path / "calls.jsonl", {"args": args}),
        description="探针命令", source="probe-ext")
    _tui_with_stub(monkeypatch, _TuiStub(registry, cwd=tmp_path))

    app = QiTui(palette=load_palette("dark"))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app._command("/probe 参数")
        await app.workers.wait_for_complete()
        await pilot.pause(0.05)

    rows = [json.loads(x) for x in
            (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["payload"]["args"] == "参数"


def _record(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "command", "payload": payload}) + "\n")


@pytest.mark.asyncio
async def test_tui_refuses_to_let_extensions_shadow_reserved_commands(tmp_path, monkeypatch):
    """扩展注册 `/quit` 也不生效 —— 否则用户会被锁在界面里。"""
    from qi_agent.theme import load_palette
    from qi_agent.tui import QiTui

    _env(tmp_path, monkeypatch)
    called: list[str] = []
    registry = CommandRegistry()
    registry.add_command("quit", lambda args, ctx: called.append("被顶掉了"),
                         source="sneaky")
    _tui_with_stub(monkeypatch, _TuiStub(registry, cwd=tmp_path))

    app = QiTui(palette=load_palette("dark"))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app._command("/quit")
        await app.workers.wait_for_complete()
        await pilot.pause(0.05)

    assert called == []                     # 扩展那条没跑(内置的 /quit 接管了)


@pytest.mark.asyncio
async def test_help_lists_extension_commands(tmp_path, monkeypatch):
    """装了扩展却没人知道能打什么 —— `/help` 要把它们列出来。"""
    from qi_agent.theme import load_palette
    from qi_agent.tui import QiTui

    _env(tmp_path, monkeypatch)
    registry = CommandRegistry()
    registry.add_command("probe", lambda args, ctx: None,
                         description="探针命令", source="probe-ext")
    _tui_with_stub(monkeypatch, _TuiStub(registry, cwd=tmp_path))

    app = QiTui(palette=load_palette("dark"))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        text = app._help_text()
        await pilot.pause(0.05)

    assert "/probe" in text and "探针命令" in text and "probe-ext" in text


def test_discovery_merges_commands_into_the_registry(tmp_path, monkeypatch):
    """命令靠发现阶段汇合(与 capabilities 并列),不是扩展自己去找 runtime。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _install(tmp_path, "cmd-ext", _COMMAND_EXTENSION.format(log=str(tmp_path / "x.jsonl")))

    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    registry = CommandRegistry()
    names = discover_extensions(catalog, CapabilityRegistry(), project,
                                bus=ExtensionBus(), commands=registry,
                                project_trusted=True)
    assert names == ["cmd-ext"]
    assert registry.names == ["probe"]
    assert [s.key for s in registry.shortcuts()] == ["ctrl+shift+j"]
    # 顺便确认那条“注册了但没人收”的路径真的会报错
    with pytest.raises(RuntimeError):
        discover_extensions(catalog, CapabilityRegistry(), project,
                            bus=ExtensionBus(), commands=None, project_trusted=True)
