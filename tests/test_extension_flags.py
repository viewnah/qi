"""P-E3b-2:扩展旗标(`registerFlag` / `getFlag` + core 的 `--ext name=value`)。

**为什么不是 `--plan` 这种短形式**(§11.8 的三选一选了 b):typer 的选项表是静态的
(实测:`typer.main.get_command` 每次重建;往 `TyperGroup` 里塞裸 `click.Option` 会崩在
它自己的内置属性上)。要支持任意短旗标,只能放宽 `ignore_unknown_options` ——
代价是**打错的选项会静默变成一句 prompt**,而那种 bug 的现场("模型答非所问")
和根因("你打错了")隔得很远。

所以 core 只静态声明一个 `--ext name=value`,并且**未知名字一律报错**(退出码 2)。
这条测试里最要紧的就是这一条:它是用 `--ext` 换来的、唯一没有让步的保护。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.extensions import (  # noqa: E402
    ExtensionApi,
    ExtensionBus,
    FlagRegistry,
)
from qi_agent.registry import EXTENSION_ENTRY_FILE, ToolCatalog  # noqa: E402

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


def _install(tmp_path: Path, name: str, body: str) -> None:
    d = tmp_path / "proj" / ".qi" / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(body, encoding="utf-8")


def _api(registry: FlagRegistry | None = None) -> ExtensionApi:
    return ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(),
                        _flags=registry, _name="probe")


# ── 登记处 ──────────────────────────────────────────────

def test_declared_default_is_used_until_something_provides_a_value():
    flags = FlagRegistry()
    flags.add("plan", type="boolean", default=False, source="plan-ext")
    assert flags.value("plan") is False
    assert flags.provide("plan") is None            # 裸名字 = 打开
    assert flags.value("plan") is True
    assert flags.names == ["plan"]
    assert flags.is_empty is False


def test_boolean_words_and_dashes_are_accepted():
    flags = FlagRegistry()
    flags.add("plan")
    for raw, expected in (("plan=true", True), ("plan=0", False), ("--plan=YES", True),
                          ("plan=off", False), ("plan=maybe", None)):
        fresh = FlagRegistry()
        fresh.add("plan")
        problem = fresh.provide(raw)
        if expected is None:
            assert problem and "不是布尔值" in problem
        else:
            assert problem is None and fresh.value("plan") is expected, raw


def test_unknown_flag_name_is_rejected_with_the_known_names():
    """`--ext agnet=x` 必须报错 —— 这是 `--ext` 方案里保留的“打错就报错”那一层。"""
    flags = FlagRegistry()
    flags.add("agent", type="string", default="", source="qi-agents")
    problem = flags.provide("agnet=reviewer")
    assert problem is not None
    assert "agnet" in problem
    assert "agent" in problem                        # 把已注册的列出来,便于对照


def test_unknown_flag_name_when_nothing_is_declared_says_so():
    problem = FlagRegistry().provide("plan")
    assert problem is not None and "没有任何扩展声明旗标" in problem


def test_string_flags_require_a_value():
    """字符串旗标缺值要报错(pi 同款)——静默变成空串会表现为“传了但没生效”。"""
    flags = FlagRegistry()
    flags.add("agent", type="string", default="general")
    assert flags.value("agent") == "general"
    assert flags.provide("agent=reviewer") is None
    assert flags.value("agent") == "reviewer"

    bare = FlagRegistry()
    bare.add("agent", type="string", default="general")
    problem = bare.provide("agent")
    assert problem is not None and "需要值" in problem
    assert bare.value("agent") == "general"      # 没生效,也没被清空


def test_duplicate_declaration_keeps_the_first_and_says_so():
    flags = FlagRegistry()
    flags.add("plan", default=False, source="one")
    flags.add("plan", default=True, source="two")
    assert flags.value("plan") is False              # 第一条胜
    assert any("one" in p and "two" in p for p in flags.problems)
    assert len(flags.specs()) == 1


# ── api 侧 ──────────────────────────────────────────────

def test_register_flag_without_a_registry_is_loud():
    with pytest.raises(RuntimeError, match="registerFlag"):
        _api(None).registerFlag("plan")


def test_get_flag_round_trip():
    flags = FlagRegistry()
    api = _api(flags)
    api.registerFlag("plan", type="boolean", default=False, description="计划模式")
    assert api.getFlag("plan") is False
    assert flags.provide("plan") is None
    assert api.getFlag("plan") is True
    assert flags.specs()[0].source == "probe"
    assert flags.specs()[0].description == "计划模式"


# ── 端到端:真 runtime ──────────────────────────────────

_PLAN_EXTENSION = """
from qi_agent.extensions import Tool

def register(api):
    api.registerFlag("plan", type="boolean", default=False, description="只读计划模式")

    async def read_flag(args, ctx):
        return "plan=" + str(api.getFlag("plan"))

    api.registerTool(Tool("flag_reader", "读旗标", {"type": "object", "properties": {}},
                          read_flag))
"""


def _runtime(tmp_path, monkeypatch, flags: list[str] | None = None):
    _env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)   # 可重复调用(同一测试里多次比较)
    _install(tmp_path, "plan-ext", _PLAN_EXTENSION)
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=project, approve_project=True,
                     extension_flags=flags)


def test_flag_default_applies_when_not_passed(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    assert runtime.extensions == ["plan-ext"]
    assert runtime.flags.value("plan") is False
    assert runtime.flag_errors == []


def test_ext_flag_value_reaches_the_extension(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, flags=["plan"])
    assert runtime.flags.value("plan") is True
    assert runtime.flag_errors == []


def test_ext_value_forms(tmp_path, monkeypatch):
    assert _runtime(tmp_path, monkeypatch, flags=["plan=false"]).flags.value("plan") is False
    assert _runtime(tmp_path, monkeypatch, flags=["plan=1"]).flags.value("plan") is True


def test_unknown_ext_flag_becomes_a_fatal_error_not_a_note(tmp_path, monkeypatch):
    """打错的旗标是**致命**的(与未知 CLI 选项同类),不能混在 notes 里当提示。"""
    runtime = _runtime(tmp_path, monkeypatch, flags=["agnet=reviewer"])
    assert runtime.flag_errors and "agnet" in runtime.flag_errors[0]
    assert not any("agnet" in n for n in runtime.notes)      # 不重复进 notes


# ── CLI:退出码 ──────────────────────────────────────────

#: 本测试构造过的假 runtime(autouse fixture 里清空 —— 与 test_cli_run 同一写法)。
#: 不用类属性存:那会让多个测试之间静默串状(这类泄漏会表现为“单独跑过、一起跑挂”)。
CREATED: list[dict] = []
#: 每次 `stream()` 收到的 prompt(用来验“宽容解析没把 prompt 吃掉”)。
PROMPTS: list[str] = []


@pytest.fixture(autouse=True)
def _clear_created():
    CREATED.clear()
    PROMPTS.clear()
    yield
    CREATED.clear()
    PROMPTS.clear()


class _RecordingRuntime:
    """只够 headless 路径走完:记下构造参数,并让旗标检查通过。"""

    def __init__(self, **kwargs):
        from pathlib import Path

        from qi_agent.session import SessionStore

        CREATED.append(kwargs)
        self.sessions = SessionStore()
        self.cwd = Path.cwd()
        self.notes: list[str] = []
        self.flag_errors: list[str] = []
        self.llm_exec = None

    async def start_session(self, session, reason: str = "startup") -> None:
        return None

    async def stream(self, prompt, session, agent_override=None):
        from qi_agent.models import AgentEvent

        PROMPTS.append(prompt)
        yield AgentEvent(kind="text", agent="general", text="好")


def _cli(monkeypatch, tmp_path, runtime_cls) -> CliRunner:
    from qi_agent import cli as cli_mod
    from qi_agent import runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "QiRuntime", runtime_cls)
    monkeypatch.setattr(cli_mod, "QiRuntime", runtime_cls, raising=False)
    return CliRunner()


def test_cli_passes_ext_values_to_the_runtime(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    _cli(monkeypatch, tmp_path, _RecordingRuntime)

    from qi_agent.cli import app

    result = CliRunner().invoke(app, ["-p", "--ext", "plan", "--ext", "agent=x", "你好"])
    assert result.exit_code == 0, result.output
    assert CREATED[-1]["extension_flags"] == ["plan", "agent=x"]


def test_cli_exits_2_on_a_bad_ext_flag(tmp_path, monkeypatch):
    """退出码 2 = 命令行打错了(与未知选项同类),而不是“运行时失败”(1)。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)

    class _BadFlagRuntime(_RecordingRuntime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.flag_errors = ["--ext agnet 没有对应的扩展旗标(是不是打错了?)"]

    _cli(monkeypatch, tmp_path, _BadFlagRuntime)
    from qi_agent.cli import app

    result = CliRunner().invoke(app, ["-p", "--ext", "agnet=x", "你好"])
    assert result.exit_code == 2
    assert "agnet" in result.output or "agnet" in (result.stderr or "")


def test_help_mentions_ext():
    """`--ext` 得能被人发现 —— 否则扩展的旗标等于没有入口。"""
    from qi_agent.cli import app

    result = CliRunner().invoke(app, ["-h"])
    assert "--ext" in result.output
    assert "name=value" in result.output


# ── 照抄 pi 的宽容解析(2026-09:读 pi 源码后落地) ─────────

def test_classify_splits_message_flag_and_error():
    """三条规则(见 `_classify_cli_args`):长旗标宽容、短旗标报错、其余是消息。"""
    from qi_agent.cli import _classify_cli_args

    messages, flags, errors = _classify_cli_args(
        ["你好", "--plan", "--plan=x", "-z", "-"])
    assert messages == ["你好", "-"]                 # `-` 单独出现是消息(stdin 约定)
    assert flags == ["plan", "plan=x"]
    assert errors == ["未知选项: -z"]


def test_classify_treats_everything_after_double_dash_as_literal():
    from qi_agent.cli import _classify_cli_args

    messages, flags, errors = _classify_cli_args(["你好", "--", "--plan", "-z"])
    assert messages == ["你好", "--plan", "-z"]      # 全字面
    assert flags == [] and errors == []


def test_bare_long_flag_does_not_eat_the_following_prompt(tmp_path, monkeypatch):
    """**刻意与 pi 不同**:pi 会把 `--plan` 后面的 token 当值吃掉(`pi --plan "问题"`
    里的那句 prompt 就没了),qi 不消费 —— 所以 `--plan 你好` 里 `你好` 仍是 prompt。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    _cli(monkeypatch, tmp_path, _RecordingRuntime)

    from qi_agent.cli import app

    result = CliRunner().invoke(app, ["-p", "--plan", "你好"])
    assert result.exit_code == 0, result.output
    assert CREATED[-1]["extension_flags"] == ["plan"]
    assert PROMPTS[-1] == "你好"                     # ← pi 在这里会丢掉这句话


def test_long_flag_with_equals_carries_its_value(tmp_path, monkeypatch):
    """`--name=value` 整条进旗标通道。

    注意这里**不能**用 `--agent` 举例:它是 core 自己的选项(v1 的手动指定 agent),
    click 会先吃掉它 —— 扩展旗标与 core 选项同名的那个先于扩展生效(已写进文档)。
    """
    _env(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    _cli(monkeypatch, tmp_path, _RecordingRuntime)

    from qi_agent.cli import app

    result = CliRunner().invoke(app, ["-p", "--plan=x", "你好"])
    assert result.exit_code == 0, result.output
    assert CREATED[-1]["extension_flags"] == ["plan=x"]
    assert PROMPTS[-1] == "你好"


def test_direct_flag_wins_over_ext_form(tmp_path, monkeypatch):
    """同名声时直接写的更具体 → 放后面,`provide` 后者覆盖前者。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    _cli(monkeypatch, tmp_path, _RecordingRuntime)

    from qi_agent.cli import app

    result = CliRunner().invoke(app, ["-p", "--ext", "plan=false", "--plan", "你好"])
    assert result.exit_code == 0, result.output
    assert CREATED[-1]["extension_flags"] == ["plan=false", "plan"]


def test_unknown_short_flag_is_rejected_before_anything_runs(tmp_path, monkeypatch):
    """短旗标不允许扩展占用(pi 同款):`-z` 直接退 2,连 runtime 都不构造。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    _cli(monkeypatch, tmp_path, _RecordingRuntime)

    from qi_agent.cli import app

    result = CliRunner().invoke(app, ["-p", "-z", "你好"])
    assert result.exit_code == 2
    assert "未知选项: -z" in (result.output + (result.stderr or ""))
    assert CREATED == []                             # 没白构造一个 runtime


def test_double_dash_keeps_everything_literal(tmp_path, monkeypatch):
    """`--` 之后一律当字面消息 —— 这是 prompt 以 `--` 开头时唯一的出路。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    _cli(monkeypatch, tmp_path, _RecordingRuntime)

    from qi_agent.cli import app

    result = CliRunner().invoke(app, ["-p", "--", "--plan", "这段是 prompt"])
    assert result.exit_code == 0, result.output
    assert CREATED[-1]["extension_flags"] == []      # `--plan` 没被当旗标
    assert PROMPTS[-1] == "--plan 这段是 prompt"
