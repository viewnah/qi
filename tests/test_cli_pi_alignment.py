"""CLI 对齐 pi:本轮补的那批旗标(runtime 侧生效 + 无功能旗标明确报未实现)。

分两类:

* **功能已存在、只是缺旗标** —— `--system-prompt` / `--no-extensions` /
  `--no-context-files` / `--models` / `--list-models` / `--session-dir` /
  `--session-id` / `--session <path>` / `--provider|--model|--api-key` /
  `--append-system-prompt <文件>` / `--tui-mode`(fullscreen 也落地了);
* **功能根本不存在** —— `--prompt-template` / `--theme` / `--mode rpc`:
  接受,但 exit 2 说清缺什么(不假装支持,也不让它掉进扩展旗标的报错里)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from qi_agent import cli as cli_mod  # noqa: E402
from qi_agent import paths, runtime as runtime_mod  # noqa: E402
from qi_agent.cli import _cmd_list_models, _model_spec, _open_session, _text_or_file  # noqa: E402
from qi_agent.cli import app, normalize_short_flags  # noqa: E402
from qi_agent.llm import ChatResponse  # noqa: E402
from qi_agent.models import AgentEvent  # noqa: E402
from qi_agent.registry import EXTENSION_ENTRY_FILE  # noqa: E402
from qi_agent.runtime import parse_model_flag  # noqa: E402

_MODELS = json.dumps({
    "providers": {
        "ollama": {"api": "openai-completions", "models": [{"id": "x"}, {"id": "y"}]},
        # `apiKey` 用字面量:清单按凭证过滤(`selectable_models`),不写就等于“用不了”
        "beta": {"api": "openai-completions", "apiKey": "k-beta",
                 "models": [{"id": "m3"}]},
    },
})
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _env(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)
    return home


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    return project


def _runtime(tmp_path, monkeypatch, **kw):
    from qi_agent.runtime import QiRuntime

    _env(tmp_path, monkeypatch)
    return QiRuntime(cwd=_project(tmp_path), approve_project=True, **kw)


# ── --system-prompt / --append-system-prompt ────────────────────────────


def test_system_prompt_replaces_the_base(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, base_prompt_override="MARKER-我的基座")
    assert rt.base_prompt == "MARKER-我的基座"
    assert rt.base_prompt_source == "cli"          # 诊断要能说出它从哪来


def test_append_system_prompt_reads_a_file(tmp_path):
    (tmp_path / "rules.md").write_text("文件里的规矩", encoding="utf-8")
    assert _text_or_file(str(tmp_path / "rules.md")) == "文件里的规矩"
    assert _text_or_file("就是一段话") == "就是一段话"          # 不是文件 → 当文本
    assert _text_or_file("") == ""


# ── --no-context-files / --no-extensions / --models ─────────────────────


async def test_no_context_files_drops_agents_md(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    project = _project(tmp_path)
    (project / "AGENTS.md").write_text("MARKER-项目上下文", encoding="utf-8")

    class _LLM:
        def __init__(self):
            self.calls = []

        async def chat(self, messages, tools=None, temperature=None):
            self.calls.append(list(messages))
            return ChatResponse(text="完")

    from qi_agent.runtime import QiRuntime

    for flag, expected in ((False, True), (True, False)):
        llm = _LLM()
        rt = QiRuntime(cwd=project, approve_project=True, llm=llm,
                       no_context_files=flag)
        session = rt.sessions.create("t", cwd=rt.cwd)
        await rt.start_session(session, reason="startup")
        async for _e in rt.stream("hi", session):
            pass
        system = llm.calls[0][0].content
        assert ("MARKER-项目上下文" in system) is expected, f"no_context_files={flag}"


def _install_ext(home: Path, name: str) -> Path:
    d = home / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(
        "from qi_agent.extensions import Tool\n"
        "def _run(args, ctx):\n    return 'ok'\n"
        "def register(api):\n"
        f"    api.registerTool(Tool({name!r}, '探针',"
        " {'type': 'object', 'properties': {}}, _run))\n", encoding="utf-8")
    return d


def test_no_extensions_skips_discovery_but_keeps_explicit(tmp_path, monkeypatch):
    """pi 的语义:`--no-extensions` 关**发现**,但 `-e` 显式给的仍然生效。"""
    home = _env(tmp_path, monkeypatch)
    probe = _install_ext(home, "probe")

    plain = _runtime(tmp_path, monkeypatch)
    assert "probe" in plain.catalog.names, "正常情况下用户目录的扩展应该被发现"

    off = _runtime(tmp_path, monkeypatch, no_extensions=True)
    assert "probe" not in off.catalog.names

    explicit = _runtime(tmp_path, monkeypatch, no_extensions=True,
                        extra_extension_paths=[probe])
    assert "probe" in explicit.catalog.names, "`-e` 显式给的不该被 --no-extensions 干掉"


def test_models_flag_sets_the_run_scoped_cycle(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, scoped_models=["ollama/x", "beta/m3"])
    assert rt.scoped_models == ["ollama/x", "beta/m3"]
    assert _runtime(tmp_path, monkeypatch).scoped_models is None      # 没给 = 不覆盖


# ── 会话:--session-dir / --session-id / path 形态 ──────────────────────


def test_session_dir_flag_beats_settings(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    rt = _runtime(tmp_path, monkeypatch, session_dir_path=str(target))
    assert rt.sessions.root == target


def test_session_id_creates_with_that_exact_id(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    session = rt.sessions.create("t", cwd=rt.cwd, session_id="exact123")
    assert session.id == "exact123"
    assert "exact123" in str(session.path)
    assert rt.sessions.get("exact123") is not None


def test_session_accepts_a_file_path(tmp_path, monkeypatch):
    """`--session <path|id>` 的 path 形态:以前只按 id / stem 前缀匹配,传路径永远不命中。"""
    rt = _runtime(tmp_path, monkeypatch)
    session = rt.sessions.create("t", cwd=rt.cwd)
    assert _open_session(rt.sessions, str(session.path)) is not None
    assert _open_session(rt.sessions, session.id) is not None


# ── 模型:--provider / --model / --api-key ─────────────────────────────


def test_parse_model_flag():
    assert parse_model_flag("beta/m3:high") == ("beta/m3", "high")
    assert parse_model_flag("beta/m3") == ("beta/m3", None)
    # 模型 id 自带的冒号不是思考级别 —— 只有已知级别才切
    assert parse_model_flag("openrouter/x:free") == ("openrouter/x:free", None)
    with pytest.raises(ValueError):
        parse_model_flag("没有斜杠")


def test_model_spec_combines_provider_and_model(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert _model_spec(None, None) is None
    assert _model_spec("beta", "m3") == "beta/m3"
    assert _model_spec(None, "beta/m3") == "beta/m3"
    assert _model_spec("beta", None) == "beta/m3"       # 只给 provider → 它的第一个模型
    with pytest.raises(ValueError):
        _model_spec(None, "m3")


def test_provider_model_flags_change_the_runtime_model(tmp_path, monkeypatch):
    from qi_agent.llm import LiteLLMClient

    rt = _runtime(tmp_path, monkeypatch, model_override="beta/m3")
    assert isinstance(rt.llm_exec, LiteLLMClient), "没给 llm 时应该是真的 LiteLLMClient"
    assert rt.llm_exec.spec.provider == "beta"
    assert rt.llm_exec.spec.model == "m3"


def test_api_key_flag_is_used_and_not_persisted(tmp_path, monkeypatch):
    from qi_agent.llm import LiteLLMClient

    _env(tmp_path, monkeypatch)
    rt = _runtime(tmp_path, monkeypatch, api_key="sk-cli")
    assert isinstance(rt.llm_exec, LiteLLMClient)
    assert rt.llm_exec._resolved.key == "sk-cli"
    assert not (paths.global_home() / paths.AUTH_FILE_NAME).exists(), "命令行给的密钥不该落盘"


def test_model_flag_thinking_suffix_sets_the_level(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, model_override="beta/m3:high")
    assert rt.thinking_level == "high"


# ── --list-models ──────────────────────────────────────────────────────


def test_list_models_lists_and_filters(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    _cmd_list_models()
    all_out = capsys.readouterr().out
    assert "ollama" in all_out and "m3" in all_out

    _cmd_list_models("m3")
    filtered = capsys.readouterr().out
    assert "m3" in filtered and "ollama" not in filtered


# ── 短旗标:--no-extensions / --no-context-files 的短形态 ───────────────


@pytest.mark.parametrize("argv, expected", [
    (["-ne"], ["--no-extensions"]),
    (["-nc"], ["--no-context-files"]),
    (["-np"], ["--no-prompt-templates"]),
    (["-n", "-ne"], ["-n", "-ne"]),        # 值位置上的不动(那是会话名)
])
def test_short_forms_are_expanded(argv, expected):
    assert normalize_short_flags(argv) == expected


# ── 无对应功能的旗标:接受 + 明确报未实现 ──────────────────────────────


class FakeRuntime:
    def __init__(self, *args, **kwargs):
        from qi_agent.session import SessionStore

        self.kwargs = dict(kwargs)          # 录下来:测试要验证 CLI → runtime 的透传
        self.sessions = SessionStore()
        self.cwd = Path.cwd()
        self.notes: list[str] = []
        self.flag_errors: list[str] = []
        CREATED.append(self)

    async def start_session(self, session, reason: str = "startup") -> None:
        return None

    async def stream(self, prompt, session, agent_override=None):
        yield AgentEvent(kind="text", agent="core", text=f"echo:{prompt}")


CREATED: list[FakeRuntime] = []
runner = CliRunner()


@pytest.fixture
def fake_runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    CREATED.clear()
    monkeypatch.setattr(runtime_mod, "QiRuntime", FakeRuntime)
    return FakeRuntime


@pytest.mark.parametrize("flags, needle", [
    (["--prompt-template", "x.md"], "prompt 模板"),
    (["--no-prompt-templates"], "prompt 模板"),
    (["--theme", "a.json"], "自定义主题文件"),
    (["--use-theme", "dark"], "自定义主题文件"),
    (["--no-themes"], "自定义主题文件"),
    (["--mode", "rpc", "-p"], "rpc"),
])
def test_unimplemented_flags_are_reported_not_swallowed(fake_runtime, flags, needle):
    res = runner.invoke(app, [*flags, "hi"])
    assert res.exit_code == 2, res.output
    assert needle in res.output, res.output
    assert CREATED == [], "报未实现时不该把 runtime 建起来"


def test_supported_flags_do_not_trip_that_check(fake_runtime):
    res = runner.invoke(app, ["-p", "--no-session", "--no-extensions",
                              "--no-context-files", "hi"])
    assert res.exit_code == 0, res.output
    assert CREATED, "这批旗标都是支持,不该被那道闸门拦住"


def test_tui_mode_is_wired_and_validated(fake_runtime, monkeypatch):
    """`--tui-mode` 不再是「未实现」:值域当着报错,合法值一路传到 `run_tui`。"""
    from qi_agent import cli as cli_mod
    from qi_agent import tui as tui_mod

    seen: dict[str, object] = {}

    def fake_run_tui(prompt=None, **kwargs):
        seen["prompt"] = prompt
        seen.update(kwargs)

    monkeypatch.setattr(tui_mod, "run_tui", fake_run_tui)
    # 直接调 `_launch_tui`:走完整 CLI 会被“非 TTY 退化为提示”拦住(CliRunner 不是 TTY),
    # 而旗标 → `_launch_tui` 这一段由上面那条 `--tui-mode bogus` 与本次调用共同覆盖。
    cli_mod._launch_tui("hi", tui_mode="regular")
    assert seen["tui_mode"] == "regular" and seen["prompt"] == "hi"

    cli_mod._launch_tui("hi", tui_mode="fullscreen")
    assert seen["tui_mode"] == "fullscreen"

    res = runner.invoke(app, ["--tui-mode", "bogus", "hi"])
    assert res.exit_code == 2, res.output
    assert "未知 TUI 模式" in res.output and "regular/fullscreen" in res.output


def test_resume_without_tty_is_a_clear_error(fake_runtime):
    res = runner.invoke(app, ["-p", "-r", "hi"])
    assert res.exit_code == 2
    assert "--session" in res.output, res.output      # 告诉用户无头下该用什么


def test_cli_passes_the_new_flags_through(fake_runtime):
    """CLI → runtime 的透传(每一条都要真到,否则旗标就是摆设)。"""
    res = runner.invoke(app, ["-p", "--no-session",
                              "--system-prompt", "S", "--provider", "beta",
                              "--model", "m3", "--api-key", "k",
                              "--session-dir", "/tmp/qi-sessions",
                              "--models", "beta/m3,ollama/x", "hi"])
    assert res.exit_code == 0, res.output
    kwargs = CREATED[-1].kwargs
    assert kwargs.get("base_prompt_override") == "S"
    assert kwargs.get("model_override") == "beta/m3"       # provider + model 合出来的
    assert kwargs.get("api_key") == "k"
    assert kwargs.get("session_dir_path") == "/tmp/qi-sessions"
    assert kwargs.get("scoped_models") == ["beta/m3", "ollama/x"]
    assert kwargs.get("no_extensions") is False
    assert kwargs.get("no_context_files") is False


# ── `--no-session` 真的不落盘 ────────────────────────────────────────
#
# 名字与文档一直说"不落盘"(`docs/sessions.md` §3:`只在内存里跑完这一轮,不产生文件`),
# 而实现走的是 `store.create()` —— 于是它**照样留一个文件**。这类"文档与实现不一致"
# 最难发现:用户按文档去用一次性脚本,攒下一堆空会话才知道。

def test_no_session_creates_an_in_memory_session(tmp_path, monkeypatch):
    """`--no-session` → 内存会话:entries 能攒、能被查询,磁盘上不留文件。"""
    rt = _runtime(tmp_path, monkeypatch)
    session = rt.sessions.ephemeral("ephemeral", cwd=rt.cwd)
    rt.sessions.append(session, {"type": "message", "role": "user", "content": "一"})
    rt.sessions.append(session, {"type": "message", "role": "assistant", "content": "二"})

    assert session.ephemeral is True
    assert session.message_count == 2                  # 会话内容照常可用
    assert list(rt.sessions.root.glob("*.jsonl")) == []   # 但一个文件都没有


def test_no_session_headless_leaves_no_file(tmp_path, monkeypatch):
    """端到端:`qi -p --no-session "hi"` 跑完,sessions 目录里空无一物。

    用真 runtime(不 stub),否则测的是替身的行为 —— 而这条 bug 恰恰在真实现里。
    """
    home = _env(tmp_path, monkeypatch)
    project = _project(tmp_path)
    monkeypatch.chdir(project)

    class _StubLLM:
        async def astream(self, messages, tools=None, temperature=None):
            from qi_agent.llm import LLMDelta

            yield LLMDelta(text="答")
            yield LLMDelta(finished=True, usage={"prompt_tokens": 1, "completion_tokens": 1})

        async def chat(self, messages, tools=None, temperature=None):
            return ChatResponse(text="标题")

    real_runtime = runtime_mod.QiRuntime

    def _patched(*args, **kwargs):
        kwargs.setdefault("llm", _StubLLM())      # 不联网
        return real_runtime(*args, **kwargs)

    monkeypatch.setattr(runtime_mod, "QiRuntime", _patched)
    res = runner.invoke(app, ["-p", "--no-session", "--no-extensions",
                              "--no-context-files", "hi"])
    assert res.exit_code == 0, res.output
    assert list((home / "sessions").glob("*.jsonl")) == [], \
        "--no-session 说了不落盘,就不该留下文件"
