"""安全回归:仓库**不能**通过自己的配置让自己可信。

这道门控存在的唯一理由是"项目级扩展 = 仓库控制的任意代码,必须在用户信任之后才加载"
(`docs/extensions.md` §5.3 / E16)。但 `defaultProjectTrust` 以前是从 **runtime 的合并
settings**(项目覆盖用户)里读的 —— 于是:

    <仓库>/.qi/settings.json:  {"defaultProjectTrust": "always"}

一行就够:qi 会判定"已信任",然后把 `.qi/extensions/` 里的任意代码跑起来。实测确实如此。

pi 的模型是一样的结论:信任决定存在**用户 home**(`trust.json`),不在仓库里 ——
仓库里的文件不能为仓库自己背书。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402

_PROBE = "def register(api):\n    pass\n"


def _env(tmp_path: Path, monkeypatch, *, user: dict | None = None,
         project: dict | None = None, with_project_ext: bool = True) -> Path:
    (tmp_path / "models.json").write_text(json.dumps(
        {"providers": {"ollama": {"api": "openai-completions", "models": [{"id": "x"}]}}}),
        encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x", **(user or {})}),
        encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))

    project_dir = tmp_path / "proj"
    (project_dir / ".git").mkdir(parents=True, exist_ok=True)
    if project or with_project_ext:
        qi = project_dir / ".qi"
        qi.mkdir(exist_ok=True)
        if project is not None:
            (qi / "settings.json").write_text(json.dumps(project), encoding="utf-8")
        if with_project_ext:
            ext = qi / paths.EXTENSIONS_DIR_NAME / "from-repo"
            ext.mkdir(parents=True, exist_ok=True)
            (ext / "extension.py").write_text(_PROBE, encoding="utf-8")
    return project_dir


def _runtime(cwd: Path):
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=cwd)


def test_repo_cannot_declare_itself_trusted(tmp_path, monkeypatch):
    """**核心回归**:项目 settings 里的 `always` 不生效,也不该让仓库代码跑起来。"""
    project = _env(tmp_path, monkeypatch, user={}, project={"defaultProjectTrust": "always"})
    rt = _runtime(project)

    assert rt.project_trusted is False, f"仓库自我声明生效了(漏洞):{rt.trust_reason}"
    assert "from-repo" not in rt.extensions, "仓库里的扩展代码被加载了"
    assert any("defaultProjectTrust" in n and "已忽略" in n for n in rt.notes), rt.notes


def test_user_level_always_still_trusts(tmp_path, monkeypatch):
    """正当路径不能被误伤:用户在**自己家里**表态就生效。"""
    project = _env(tmp_path, monkeypatch, user={"defaultProjectTrust": "always"})
    rt = _runtime(project)

    assert rt.project_trusted is True, rt.trust_reason
    assert "from-repo" in rt.extensions
    assert not any("已忽略" in n for n in rt.notes)


def test_project_never_is_honored_too(tmp_path, monkeypatch):
    """反方向同理:仓库也不能用 `never` 把自己锁... 关键是**项目那份一律不算**。"""
    project = _env(tmp_path, monkeypatch, user={"defaultProjectTrust": "always"},
                   project={"defaultProjectTrust": "never"})
    rt = _runtime(project)

    assert rt.project_trusted is True, "项目级 never 不该推翻用户级的 always"
    assert any("已忽略" in n for n in rt.notes)


def test_cli_flag_still_wins(tmp_path, monkeypatch):
    """`-a` / `-na` 仍是最高的那一档(与项目里写什么无关)。"""
    from qi_agent.runtime import QiRuntime

    project = _env(tmp_path, monkeypatch, user={}, project={"defaultProjectTrust": "always"})
    assert QiRuntime(cwd=project, approve_project=True).project_trusted is True
    assert QiRuntime(cwd=project, approve_project=False).project_trusted is False
