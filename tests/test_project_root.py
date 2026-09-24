"""项目目录 = **cwd**(对齐 pi),不是 git 根。

pi 的项目配置就在当前目录(`docs/settings.md`:"Project (current directory)"),qi 早先取
git 根 —— 于是从子目录启动时“项目”会跑到仓库根上。回归锁住:

* `project_home()` = `<cwd>/.qi`;
* `.agents/skills` 的祖先链**仍止于 git 根**(pi 的 `collectAncestorAgentsSkillDirs` 同此)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qi_agent import paths  # noqa: E402
from qi_agent.settings import load_settings_by_scope  # noqa: E402


def _repo(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """一个 git 仓库 + 两层子目录;cwd 定在深子目录。"""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    monkeypatch.chdir(sub)
    return repo, sub


def test_project_home_is_cwd_not_git_root(tmp_path, monkeypatch):
    repo, sub = _repo(tmp_path, monkeypatch)
    assert paths.project_home() == sub / ".qi"
    assert paths.project_home() != repo / ".qi"


def test_project_settings_follow_cwd(tmp_path, monkeypatch):
    repo, sub = _repo(tmp_path, monkeypatch)
    (repo / ".qi").mkdir()
    (repo / ".qi" / "settings.json").write_text('{"tuiMode": "regular"}', encoding="utf-8")
    (sub / ".qi").mkdir()
    (sub / ".qi" / "settings.json").write_text('{"tuiMode": "fullscreen"}', encoding="utf-8")

    candidates = {scope: path for path, scope in paths.settings_file_candidates()}
    assert candidates["project"] == sub / ".qi" / "settings.json"
    assert load_settings_by_scope()["project"].tuiMode == "fullscreen"


def test_git_root_settings_are_ignored_from_a_subdir(tmp_path, monkeypatch):
    repo, _sub = _repo(tmp_path, monkeypatch)
    (repo / ".qi").mkdir()
    (repo / ".qi" / "settings.json").write_text('{"tuiMode": "regular"}', encoding="utf-8")
    assert load_settings_by_scope().get("project") is None, "仓库根的 .qi 不该从子目录命中"


def test_agents_skills_ancestors_still_stop_at_git_root(tmp_path, monkeypatch):
    repo, sub = _repo(tmp_path, monkeypatch)
    chain = paths.project_context_ancestors()
    assert chain[0] == sub.resolve()
    assert repo.resolve() in chain
    assert chain[-1] == repo.resolve(), "祖先链止于 git 根(与 pi 的 .agents/skills 一致)"


def test_context_file_ancestors_go_to_filesystem_root(tmp_path, monkeypatch):
    """`AGENTS.md` 的祖先链**不停在 git 根**(pi 的 `loadProjectContextFiles` 同此)。"""
    repo, sub = _repo(tmp_path, monkeypatch)
    chain = paths.context_file_ancestors()
    assert chain[0] == sub.resolve()
    assert repo.resolve() in chain
    assert tmp_path.resolve() in chain, "git 根之上还要继续走"
    assert chain[-1].parent == chain[-1], "走到文件系统根才停"
