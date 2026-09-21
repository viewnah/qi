"""工具收窄的四个 CLI 旗标(对齐 pi)+ argv 预处理。

为什么需要预处理:click 的短选项是**按字符**解析的(只在 `_short_opt` 里查 `-n`、`-x`
这类单字符键),所以 `-nt` 会被拆成 `-n t` —— 用户敲 `qi -nt hi` 的后果是**会话名被改成
"t"**,而工具一个都没关(静默)。pi 的 CLI 是手写 argv 循环,不存在这回事;qi 用一次
argv 展开补齐(见 `cli.normalize_short_flags`)。

语义照 pi(`docs/usage.md` 旗标表 + `docs/settings.md` 的 `defaultTools` 段):
`--tools` 严格白名单 / `--exclude-tools` 过滤结果 / `--no-builtin-tools` 只去内置 /
`--no-tools` 全禁。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from qi_agent import paths, runtime as runtime_mod  # noqa: E402
from qi_agent.cli import app, normalize_short_flags  # noqa: E402
from qi_agent.models import AgentEvent  # noqa: E402
from qi_agent.registry import EXTENSION_ENTRY_FILE  # noqa: E402

runner = CliRunner()
CREATED: list["FakeRuntime"] = []


# ── argv 预处理(纯函数) ────────────────────────────────────────────────


@pytest.mark.parametrize("argv, expected", [
    (["-nt", "hi"], ["--no-tools", "hi"]),
    (["-nbt", "hi"], ["--no-builtin-tools", "hi"]),
    (["-t", "read,grep", "hi"], ["--tools", "read,grep", "hi"]),
    (["-xt", "bash", "hi"], ["--exclude-tools", "bash", "hi"]),
    (["--tools=read"], ["--tools=read"]),          # 带 `=` 的不吃下一个 token
    (["-n", "我的会话"], ["-n", "我的会话"]),        # 普通短选项原样
    (["-n", "-nt"], ["-n", "-nt"]),                # ⭐ 值位置上的 `-nt` 是**会话名**
    (["-p", "--", "-nt"], ["-p", "--", "-nt"]),    # `--` 之后一律字面
    (["-e", "/x/y"], ["-e", "/x/y"]),
])
def test_normalize_short_flags(argv, expected):
    assert normalize_short_flags(argv) == expected


def test_short_form_no_longer_silently_renames_session(fake_runtime):
    """回归:这条路径以前会把会话名改成 "t" 而工具照旧全开。

    这里走 long form —— `CliRunner` 不经过 `main()`,所以短旗标的展开由下一个用例单测;
    本用例证明"收窄真的到了 runtime 且会话名没被动"。
    """
    res = runner.invoke(app, ["-p", "--no-session", "--no-tools", "hi"])
    assert res.exit_code == 0, res.output
    rt = CREATED[-1]
    assert rt.kwargs.get("no_tools") is True, "工具没收窄"
    assert rt.kwargs.get("name") is None, "会话名被静默改掉了"


def test_main_normalizes_argv_before_typer(monkeypatch):
    """`main()` 在交给 typer 之前先展开 pi 的短旗标(接线不靠 CliRunner 能测到)。"""
    from qi_agent import cli as cli_mod

    seen: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["qi", "-nt", "--no-session", "hi"])
    monkeypatch.setattr(cli_mod.paths, "ensure_layout", lambda: [])
    monkeypatch.setattr(cli_mod, "_dispatch_extension_command", lambda argv: False)
    monkeypatch.setattr(cli_mod, "app", lambda *a, **k: seen.append(list(sys.argv[1:])))
    cli_mod.main()
    assert seen == [["--no-tools", "--no-session", "hi"]], seen


# ── CLI → runtime 的透传与冲突检测 ──────────────────────────────────────


class FakeRuntime:
    def __init__(self, *args, **kwargs):
        from qi_agent.session import SessionStore

        self.kwargs = dict(kwargs)
        self.sessions = SessionStore()
        self.cwd = Path.cwd()
        self.notes: list[str] = []
        self.flag_errors: list[str] = []
        self.prompts: list[str] = []
        CREATED.append(self)

    async def start_session(self, session, reason: str = "startup") -> None:
        return None

    async def stream(self, prompt, session, agent_override=None):
        self.prompts.append(prompt)
        yield AgentEvent(kind="text", agent="writer", text=f"echo:{prompt}")


@pytest.fixture
def fake_runtime(tmp_path: Path, monkeypatch) -> type[FakeRuntime]:
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    CREATED.clear()
    monkeypatch.setattr(runtime_mod, "QiRuntime", FakeRuntime)
    return FakeRuntime


@pytest.mark.parametrize("flags, key, value", [
    (["--tools", "read,grep"], "tools", "read,grep"),
    (["--exclude-tools", "bash"], "exclude_tools", "bash"),
    (["--no-tools"], "no_tools", True),
    (["--no-builtin-tools"], "no_builtin_tools", True),
])
def test_tool_flags_reach_the_runtime(fake_runtime, flags, key, value):
    res = runner.invoke(app, ["-p", "--no-session", *flags, "hi"])
    assert res.exit_code == 0, res.output
    assert CREATED[-1].kwargs.get(key) == value


def test_option_written_after_the_message_gets_the_right_hint(fake_runtime):
    """选项写在消息后面 = click 不再解析它。

    以前它会报"`--ext no-session` 没有对应的扩展旗标" —— 用户压根没写 `--ext`,
    那是在指错方向。现在直说"要写在消息之前"。
    """
    res = runner.invoke(app, ["-p", "hi", "--no-session"])
    assert res.exit_code == 2, res.output
    assert "要写在**消息之前**" in res.output
    assert "--ext" not in res.output, res.output


def test_tools_is_a_strict_allowlist_not_additive(fake_runtime):
    """`--tools` 与整集选择矛盾 → 报出来(不替用户挑一个赢)。"""
    res = runner.invoke(app, ["-p", "--tools", "read", "--no-tools", "hi"])
    assert res.exit_code == 2, res.output
    assert "冲突" in res.output
    assert CREATED == [], "冲突时不该把 runtime 建起来"


def test_version_flag_prints_and_exits(fake_runtime):
    res = runner.invoke(app, ["-v"])
    assert res.exit_code == 0, res.output
    assert res.output.strip().startswith("qi ")
    assert CREATED == []


def test_offline_flag_is_accepted(fake_runtime):
    """对齐 pi 的旗标:qi 启动期没有网络操作,所以它接受但无作用。"""
    res = runner.invoke(app, ["-p", "--no-session", "--offline", "hi"])
    assert res.exit_code == 0, res.output
    assert "no such option" not in res.output.lower()


# ── 语义(真 runtime:工具集真的变了) ──────────────────────────────────

_MODELS = json.dumps({"providers": {"ollama": {"api": "openai-completions",
                                               "models": [{"id": "x"}]}}})
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'

_PLUGIN_EXT = """
from qi_agent.extensions import Tool


def _run(args, ctx):
    return "plug"


def register(api):
    api.registerTool(Tool("plug", "扩展工具", {"type": "object", "properties": {}}, _run))
"""


def _env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)


def _runtime(tmp_path, monkeypatch, **kw):
    """`kw` 里 `default_tools=` 是 **settings 字段**(写进 settings.json),其余给 QiRuntime。"""
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    default_tools = kw.pop("default_tools", "缺席")
    _env(tmp_path, monkeypatch)
    if default_tools != "缺席":
        (tmp_path / "home" / "settings.json").write_text(
            json.dumps({"defaultProvider": "ollama", "defaultModel": "x",
                        "defaultTools": default_tools}), encoding="utf-8")
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=project, approve_project=True, **kw)


def _install_plugin(tmp_path: Path) -> None:
    d = tmp_path / "proj" / ".qi" / paths.EXTENSIONS_DIR_NAME / "plug"
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(_PLUGIN_EXT, encoding="utf-8")


def test_tools_allowlist_is_strict(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, tools="read,grep")
    assert rt.tool_names() == ["grep", "read"]


def test_exclude_tools_filters_the_rest(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, exclude_tools="bash,powershell")
    names = rt.tool_names()
    assert "bash" not in names and "powershell" not in names
    assert "read" in names, "排除不该把别的也清掉"


def test_no_tools_empties_the_set(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, no_tools=True)
    assert rt.tool_names() == []


def test_no_builtin_tools_keeps_extension_tools(tmp_path, monkeypatch):
    """`-nbt` 的意义就在这里:关掉内置、留下扩展装的。"""
    _install_plugin(tmp_path)
    rt = _runtime(tmp_path, monkeypatch, no_builtin_tools=True)
    assert rt.tool_names() == ["plug"]


def test_unknown_tool_name_is_reported_not_swallowed(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, tools="read,nope")
    assert rt.tool_names() == ["read"]
    assert any("nope" in n for n in rt.notes), rt.notes


def test_no_flags_leaves_the_set_untouched(tmp_path, monkeypatch):
    """没给旗标 = 不设覆盖(不是"设成空集")。"""
    rt = _runtime(tmp_path, monkeypatch)
    assert rt.tool_names() == sorted(rt.catalog.names)
    assert not any("本次运行的工具集" in n for n in rt.notes)


async def test_narrowed_set_reaches_the_model(tmp_path, monkeypatch):
    """端到端:收窄真的穿过 runner(tool schema 里只剩允许的那些)。"""
    from qi_agent.llm import ChatResponse

    class _Scripted:
        def __init__(self):
            self.tools: list[list[str]] = []

        async def chat(self, messages, tools=None, temperature=None):
            self.tools.append(sorted(t["function"]["name"] for t in (tools or [])))
            return ChatResponse(text="完")

    llm = _Scripted()
    rt = _runtime(tmp_path, monkeypatch, tools="read", llm=llm)
    session = rt.sessions.create("t", cwd=rt.cwd)
    await rt.start_session(session, reason="startup")
    async for _ in rt.stream("hi", session):
        pass
    assert llm.tools == [["read"]], llm.tools


# ── settings.defaultTools(pi 同名段:没给 CLI 旗标时才看它) ─────────────


def test_default_tools_limits_the_builtin_set(tmp_path, monkeypatch):
    """`defaultTools` 只挑**内置**那一档 —— 扩展工具照旧全留(pi 的原话)。"""
    rt = _runtime(tmp_path, monkeypatch, default_tools=["read", "grep"])
    names = rt.tool_names()
    assert "read" in names and "grep" in names
    assert "bash" not in names, "没列进 defaultTools 的内置工具该被关掉"
    assert any("defaultTools" in note for note in rt.notes)


def test_empty_default_tools_means_no_builtins_but_keeps_extensions(tmp_path, monkeypatch):
    """`defaultTools: []` 是个**有意义**的配置(不要内置、只留扩展),不能与“没配”混为一谈。"""
    _install_plugin(tmp_path)
    rt = _runtime(tmp_path, monkeypatch, default_tools=[])
    assert rt.tool_names() == ["plug"], rt.tool_names()


def test_unset_default_tools_changes_nothing(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    assert rt.tool_names() == sorted(rt.catalog.names)


def test_cli_tool_flags_win_over_default_tools(tmp_path, monkeypatch):
    """pi 同口径:`--tools` 是**替换**默认集(它压过 `defaultTools`)。"""
    rt = _runtime(tmp_path, monkeypatch, default_tools=["read"], tools="grep")
    assert rt.tool_names() == ["grep"]


def test_unknown_names_in_default_tools_are_reported(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, default_tools=["read", "nope"])
    assert rt.tool_names() == ["read"]
    assert any("nope" in note for note in rt.notes), rt.notes
