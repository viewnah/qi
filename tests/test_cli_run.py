"""顶层无头运行:`qi -p "问题"` 的位置参数解析(回归:No such command)。

历史问题:Typer 的 Group 把第一个位置参数当子命令名解析,
`qi -p "你好"` 会报 `No such command '你好'`(docs/cli.md §1 / README 快速开始都依赖它)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from qi_agent import runtime as runtime_mod
from qi_agent.cli import app
from qi_agent.models import AgentEvent

runner = CliRunner()
CREATED: list["FakeRuntime"] = []  # 本轮构造的假运行时(fixture 里清空)


class FakeRuntime:
    """假运行时:记录 prompt,不读配置、不联网。"""

    def __init__(self, *args, **kwargs):
        from pathlib import Path

        from qi_agent.session import SessionStore

        self.sessions = SessionStore()
        self.cwd = Path.cwd()          # 真实 QiRuntime 必有:cli 用它写会话头
        self.notes: list[str] = []     # 启动提示(未信任/旧目录残留),cli 往 stderr 打
        self.started: list[str] = []   # `session_start` 的 reason 记账(扩展事件链)
        self.flag_errors: list[str] = []   # `--ext` 打错时的报错(cli 据此退出 2)
        self.prompts: list[str] = []
        self.overrides: list[str | None] = []
        CREATED.append(self)

    async def start_session(self, session, reason: str = "startup") -> None:
        """真实 QiRuntime 的会话级事件;假运行时只记账。"""
        self.started.append(reason)

    async def stream(self, prompt, session, agent_override=None):
        self.prompts.append(prompt)
        self.overrides.append(agent_override)
        # 与真实 runtime 一致:先发 dispatch(含 display_name),再发答案文本
        yield AgentEvent(kind="dispatch", agent="writer", text="qi (router, 0.90)",
                         data={"source": "router", "confidence": 0.9,
                               "agent": "writer", "display_name": "qi", "reasoning": "测试"})
        yield AgentEvent(kind="text", agent="writer", text=f"echo:{prompt}")


@pytest.fixture
def fake_runtime(tmp_path: Path, monkeypatch) -> type[FakeRuntime]:
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    CREATED.clear()
    monkeypatch.setattr(runtime_mod, "QiRuntime", FakeRuntime)
    return FakeRuntime


def _last(_cls: type[FakeRuntime]) -> FakeRuntime:
    assert CREATED, "QiRuntime 未被构造"
    return CREATED[-1]


def test_print_mode_accepts_positional_message(fake_runtime):
    res = runner.invoke(app, ["-p", "你好"])
    assert res.exit_code == 0, res.output
    assert "No such command" not in res.output
    assert _last(fake_runtime).prompts == ["你好"]
    assert "echo:你好" in res.output


def test_print_mode_joins_multiple_tokens(fake_runtime):
    res = runner.invoke(app, ["-p", "分析", "这个仓库"])
    assert res.exit_code == 0, res.output
    assert _last(fake_runtime).prompts == ["分析 这个仓库"]


def test_print_mode_with_agent_override(fake_runtime):
    res = runner.invoke(app, ["-p", "--agent", "writer", "写点东西"])
    assert res.exit_code == 0, res.output
    assert _last(fake_runtime).prompts == ["写点东西"]
    assert _last(fake_runtime).overrides == ["writer"]


def test_print_mode_after_double_dash(fake_runtime):
    res = runner.invoke(app, ["-p", "--", "你好"])
    assert res.exit_code == 0, res.output
    assert _last(fake_runtime).prompts == ["你好"]


def test_print_mode_message_named_like_subcommand(fake_runtime):
    """`-p version`:位置参数是消息,不该被当成子命令跑掉。"""
    res = runner.invoke(app, ["-p", "version"])
    assert res.exit_code == 0, res.output
    assert _last(fake_runtime).prompts == ["version"]


def test_print_mode_message_named_like_subcommand_group(fake_runtime):
    res = runner.invoke(app, ["--print", "agents"])
    assert res.exit_code == 0, res.output
    assert _last(fake_runtime).prompts == ["agents"]


def test_bare_qi_without_tty_prints_hint(fake_runtime):
    """无参且非 TTY(CI/管道):给提示而非 traceback。"""
    res = runner.invoke(app, [])
    assert res.exit_code == 0, res.output
    assert "需要 TTY" in res.output
    assert CREATED == []


def test_qi_with_message_without_print_is_interactive(fake_runtime):
    """不带 -p 给消息 = 进 TUI(并把消息作首条),不再走无头(对齐 pi)。"""
    res = runner.invoke(app, ["你好"])
    assert res.exit_code == 0, res.output
    assert "需要 TTY" in res.output
    assert CREATED == []                       # 未构造无头 runtime


def test_mode_json_without_print_is_still_headless(fake_runtime):
    """`--mode json` 是企业脚本路径:即使没有 -p 也不进 TUI。"""
    res = runner.invoke(app, ["--mode", "json", "你好"])
    assert res.exit_code == 0, res.output
    assert _last(fake_runtime).prompts == ["你好"]
    assert "echo:你好" in res.output


def test_tui_is_not_a_subcommand(fake_runtime):
    """`qi tui` 已移除:不再被当作子命令分派(而是普通消息)。"""
    res = runner.invoke(app, ["tui"])
    assert res.exit_code == 0, res.output
    assert "需要 TTY" in res.output
    assert CREATED == []


def test_real_subcommands_still_dispatch(fake_runtime):
    res = runner.invoke(app, ["version"])
    assert res.exit_code == 0, res.output
    assert res.output.startswith("qi ")
    assert CREATED == []  # 子命令不触发 headless 运行


def test_help_still_works(fake_runtime):
    res = runner.invoke(app, ["-h"])
    assert res.exit_code == 0, res.output
    assert "--print" in res.output
    assert "Commands" in res.output


# ── qi agents list / show:渲染 display_name ────────────────

def test_agents_list_renders_display_name(tmp_path, monkeypatch):
    """display_name 必须真的被渲染(此前写了却没人读)。"""
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["agents", "list"])
    assert res.exit_code == 0, res.output
    assert "显示名" in res.output          # 列存在
    assert "general" in res.output         # name
    assert "qi" in res.output              # display_name(内置 general)
    assert "builtin" in res.output         # 来源


def test_agents_list_display_name_falls_back_to_dash(tmp_path, monkeypatch):
    """未设 display_name 的 agent 回落为 '-'，不是空白。"""
    home = tmp_path / "home"
    d = home / "agents" / "writer"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(
        "---\nname: writer\ndescription: 写文档\nkeywords: []\ntools: [\"*\"]\n---\n你是写手。",
        encoding="utf-8")
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["agents", "list"])
    assert res.exit_code == 0, res.output
    assert "writer" in res.output


def test_agents_show_prints_display_name(tmp_path, monkeypatch):
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["agents", "show", "general"])
    assert res.exit_code == 0, res.output
    assert "显示名: qi" in res.output


# ── 无头输出分流:答案走 stdout,诊断走 stderr ────────────────

def test_dispatch_line_goes_to_stderr_not_stdout(fake_runtime):
    """--verbose 时诊断走 stderr、答案仍在 stdout(对齐 pi:进度不污染 stdout)。"""
    res = runner.invoke(app, ["-p", "--verbose", "你好"])
    assert res.exit_code == 0, res.output
    assert "echo:你好" in res.stdout          # 答案在 stdout
    assert "→ " not in res.stdout             # 分派行不在 stdout
    assert "→ " in res.stderr                 # 分派行在 stderr
    assert "router" in res.stderr


def test_print_mode_quiet_by_default(fake_runtime):
    """对齐 pi 的 `-p`:默认只输出答案 —— stdout 与 stderr 都干净。"""
    res = runner.invoke(app, ["-p", "你好"])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == "echo:你好"   # 只有答案
    assert res.stderr.strip() == ""            # 无分派/工具进度


def test_tool_progress_goes_to_stderr_not_stdout(fake_runtime):
    """工具进度:默认静默;--verbose 时走 stderr,stdout 仍只有答案。"""
    async def with_tool(self, prompt, session, agent_override=None):
        yield AgentEvent(kind="tool_start", tool="read", data={"args": {"path": "a"}})
        yield AgentEvent(kind="tool_end", tool="read", text="内容")
        yield AgentEvent(kind="text", agent="w", text="done")

    fake_runtime.stream = with_tool

    quiet = runner.invoke(app, ["-p", "x"])
    assert quiet.exit_code == 0, quiet.output
    assert quiet.stdout.strip() == "done"       # 默认只输出答案
    assert "read" not in quiet.stderr          # 默认不显示工具进度

    loud = runner.invoke(app, ["-p", "--verbose", "x"])
    assert loud.exit_code == 0, loud.output
    assert loud.stdout.strip() == "done"        # stdout 仍只有答案
    assert "read" in loud.stderr


def test_verbose_renders_args_as_json(fake_runtime):
    """工具参数用 JSON 而非 Python repr(不出现单引号)。"""
    async def with_tool(self, prompt, session, agent_override=None):
        yield AgentEvent(kind="tool_start", tool="ls", data={"args": {"path": "."}})
        yield AgentEvent(kind="text", agent="w", text="ok")

    fake_runtime.stream = with_tool
    res = runner.invoke(app, ["-p", "--verbose", "x"])
    assert res.exit_code == 0, res.output
    assert '{"path": "."}' in res.stderr
    assert "'path'" not in res.stderr          # 不是 Python repr


def test_verbose_tool_result_keeps_newlines_and_flags_truncation():
    """多行结果保留换行(不压成空格);超长时带截断提示。"""
    from qi_agent.cli import _tool_snippet

    listing = "# /tmp (3 项)\nd a\nf b\nd c"
    out = _tool_snippet(listing)
    assert out == listing                       # 换行保留,原样返回

    long_text = "\n".join(f"line {i}" for i in range(200))
    cut = _tool_snippet(long_text, limit=100)
    assert "已截断" in cut and "200 行" in cut   # 有明确截断提示


# ── 会话回放:展示名 ───────────────────────────────────

def _session_with(entries: list[dict], monkeypatch, tmp_path) -> str:
    """在受控 QI_AGENT_HOME 下写一个会话,返回其 id。"""
    from qi_agent.session import SessionStore

    home = tmp_path / "home"
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    store = SessionStore()
    s = store.create("回放测试")
    for e in entries:
        store.append(s, e)
    return s.id


def test_sessions_show_replays_display_name(tmp_path, monkeypatch):
    """回放应显示记录时的展示名(qi),不是内部名(general)。"""
    sid = _session_with([
        {"type": "dispatch", "agent": "general", "display_name": "qi",
         "source": "router", "confidence": 0.9, "reasoning": "测试"},
        {"type": "message", "role": "user", "content": "你好", "agent_id": "general"},
        {"type": "message", "role": "assistant", "content": "在的", "agent_id": "general"},
    ], monkeypatch, tmp_path)
    res = runner.invoke(app, ["sessions", "show", sid])
    assert res.exit_code == 0, res.output
    assert "→ qi (router, 0.9)" in res.output        # 分派行用展示名
    assert "qi: 在的" in res.output                  # 说话人标签用展示名
    assert "general" not in res.output               # 不泄露内部名


def test_sessions_show_falls_back_to_name_for_old_sessions(tmp_path, monkeypatch):
    """改动前写入的会话没有 display_name → 回落为 name,不应崩或空白。"""
    sid = _session_with([
        {"type": "dispatch", "agent": "writer", "source": "rules", "confidence": 1.0},
        {"type": "message", "role": "assistant", "content": "写好了", "agent_id": "writer"},
    ], monkeypatch, tmp_path)
    res = runner.invoke(app, ["sessions", "show", sid])
    assert res.exit_code == 0, res.output
    assert "→ writer (rules, 1.0)" in res.output
    assert "writer: 写好了" in res.output


def test_sessions_show_handles_unknown_author(tmp_path, monkeypatch):
    """消息的 agent_id 没有对应 dispatch 记录时,直接用原名。"""
    sid = _session_with([
        {"type": "message", "role": "assistant", "content": "hi", "agent_id": "ghost"},
    ], monkeypatch, tmp_path)
    res = runner.invoke(app, ["sessions", "show", sid])
    assert res.exit_code == 0, res.output
    assert "ghost: hi" in res.output


def test_sessions_show_user_line_has_no_speaker(tmp_path, monkeypatch):
    """用户消息不标说话人:agent_id 只是"将处理它的 agent",不是发言者。"""
    sid = _session_with([
        {"type": "dispatch", "agent": "general", "display_name": "qi",
         "source": "router", "confidence": 0.9},
        {"type": "message", "role": "user", "content": "你好", "agent_id": "general"},
        {"type": "message", "role": "assistant", "content": "在的", "agent_id": "general"},
    ], monkeypatch, tmp_path)
    res = runner.invoke(app, ["sessions", "show", sid])
    assert res.exit_code == 0, res.output
    assert "user 你好" in res.output          # 不出现 "user qi:"
    assert "user qi" not in res.output
    assert "assistant qi: 在的" in res.output
