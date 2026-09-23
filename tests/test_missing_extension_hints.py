"""缺失扩展时的两条提示(对齐 design/extensions-design.md 的迁移清单)。

以前这两种情况都是**静默失效**:

* `<项目>/.qi/agents/<名>/agent.md` 摆在那里,但没装 qi-agents —— 文件毫无作用,一声不响;
* `qi web` —— typer 只报 `No such command 'web'`,不告诉你 `pip install qi-web` 就有。

两条都属于"该说出来的话":用户看得见的东西(一个目录、一条命令)与它实际的效果不一致。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from typer import Exit  # noqa: E402

from qi_agent import cli as cli_mod  # noqa: E402
from qi_agent import paths  # noqa: E402
from qi_agent.cli import _OFFICIAL_EXTENSION_COMMANDS, main  # noqa: E402

_MODELS = json.dumps({"providers": {"ollama": {"api": "openai-completions",
                                               "models": [{"id": "x"}]}}})
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _env(tmp_path: Path, monkeypatch) -> Path:
    """一份可用的最小配置(真 QiRuntime 要能解析出默认模型)。"""
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)
    return home


def _write_role(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / paths.AGENT_FILE_NAME).write_text(
        f"---\nname: {name}\ndescription: 做这个的\n---\n你是{name}。", encoding="utf-8")


def _runtime(tmp_path, monkeypatch):
    from qi_agent.runtime import QiRuntime

    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch)
    return QiRuntime(cwd=project, approve_project=True)


def _agent_notes(runtime) -> list[str]:
    return [n for n in runtime.notes if "qi-agents" in n]


# ── 角色目录在、扩展不在 ─────────────────────────────────────────────


def test_agents_dir_without_the_extension_is_reported(tmp_path, monkeypatch):
    """默认(conftest 把 entry point 桩成空)= 没装 qi-agents。"""
    _write_role(tmp_path / "proj" / ".qi" / paths.AGENTS_DIR_NAME, "scout")
    runtime = _runtime(tmp_path, monkeypatch)
    assert _agent_notes(runtime), runtime.notes
    assert "不会被使用" in _agent_notes(runtime)[0]
    assert "pip install qi-agents" in _agent_notes(runtime)[0], "要给出装法"


def test_global_agents_dir_counts_too(tmp_path, monkeypatch):
    home = _env(tmp_path, monkeypatch)
    _write_role(home / paths.AGENTS_DIR_NAME, "scout")
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True)
    assert _agent_notes(runtime), runtime.notes


def test_no_agents_dir_means_no_note(tmp_path, monkeypatch):
    """没角色目录就别多嘴 —— 提示只在“文件在那儿却没用”时才值钱。"""
    runtime = _runtime(tmp_path, monkeypatch)
    assert _agent_notes(runtime) == []
    assert not any("角色目录" in n for n in runtime.notes)


def test_empty_agents_dir_means_no_note(tmp_path, monkeypatch):
    """目录建了但里面没有 agent.md(空目录 / 只有别的文件)→ 不算“有角色”。"""
    (tmp_path / "proj" / ".qi" / paths.AGENTS_DIR_NAME).mkdir(parents=True)
    runtime = _runtime(tmp_path, monkeypatch)
    assert _agent_notes(runtime) == []


def test_agents_dir_with_the_extension_is_quiet(tmp_path, monkeypatch):
    """装了 qi-agents(`subagent` 工具在)就不再提示。"""
    from qi_agent.runtime import QiRuntime

    _write_role(tmp_path / "proj" / ".qi" / paths.AGENTS_DIR_NAME, "scout")
    _env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)

    class _WithSubagent(QiRuntime):
        def _has_agent_support(self) -> bool:      # 模拟装上了 qi-agents
            return True

    runtime = _WithSubagent(cwd=project, approve_project=True)
    assert _agent_notes(runtime) == []


# ── 官方扩展子命令缺扩展 ─────────────────────────────────────────────


def test_official_command_table_covers_qi_web():
    assert _OFFICIAL_EXTENSION_COMMANDS.get("web") == "qi-web"


def test_qi_web_without_the_extension_prints_install_hint(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["qi", "web"])
    monkeypatch.setattr(cli_mod.paths, "ensure_layout", lambda: [])

    def _must_not_run(*_a, **_k):
        raise AssertionError("提醒之后不该继续进 typer")

    monkeypatch.setattr(cli_mod, "app", _must_not_run)
    with pytest.raises(Exit) as excinfo:
        main()
    assert excinfo.value.exit_code == 2
    err = capsys.readouterr().err
    assert "qi-web" in err and "pip install" in err, err
    assert "No such command" not in err


def test_other_commands_are_untouched(monkeypatch):
    """非官方子命令(= 正常启动)不该被这道闸门碰。"""
    seen: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["qi", "-p", "hi"])
    monkeypatch.setattr(cli_mod.paths, "ensure_layout", lambda: [])
    monkeypatch.setattr(cli_mod, "app", lambda *a, **k: seen.append(list(sys.argv[1:])))
    main()
    assert seen == [["-p", "hi"]]
