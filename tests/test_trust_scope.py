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


# ── 按目录记住决定(`~/.qi/agent/trust.json`,对齐 pi) ────────────────


def test_store_remembers_and_forgets(tmp_path):
    from qi_agent.trust import TrustStore

    store = TrustStore(tmp_path / "trust.json")
    assert store.get(tmp_path) is None

    store.set(tmp_path, True)
    assert store.get(tmp_path) == (True, str(tmp_path.resolve()))
    assert store.forget(tmp_path) is True
    assert store.get(tmp_path) is None
    assert store.forget(tmp_path) is False


def test_store_inherits_from_parent_and_closest_wins(tmp_path):
    """pi 的 "closest saved decision on the current or parent path applies"。"""
    from qi_agent.trust import TrustStore

    store = TrustStore(tmp_path / "trust.json")
    parent, child = tmp_path / "repo", tmp_path / "repo" / "sub" / "deep"
    child.mkdir(parents=True)

    store.set(parent, True)
    assert store.get(child) == (True, str(parent.resolve())), "父目录的决定该对子目录生效"

    store.set(child, False)
    assert store.get(child) == (False, str(child.resolve())), "最近的那条赢"


def test_store_set_parent_also_remembers_the_parent(tmp_path):
    """pi 的 `/trust` 连带记住上一层 —— 同一条路径下的平级项目一起生效。"""
    from qi_agent.trust import TrustStore

    store = TrustStore(tmp_path / "trust.json")
    child = tmp_path / "a" / "b" / "proj"
    child.mkdir(parents=True)

    store.set(child, True, parent=True)

    nested = store.get(child / "nested")            # 自己那条 → 对子目录生效
    assert nested is not None and nested[0] is True
    sibling = store.get(tmp_path / "a" / "b" / "sibling")   # 上一层那条 → 对平级生效
    assert sibling is not None and sibling[0] is True
    assert str((tmp_path / "a" / "b").resolve()) in store.decisions(), store.decisions()


def test_store_file_is_private_and_tolerates_junk(tmp_path):
    import stat

    from qi_agent.trust import TrustStore

    path = tmp_path / "trust.json"
    store = TrustStore(path)
    store.set(tmp_path, True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, "信任库该是 0600"

    path.write_text("{ 坏的 json", encoding="utf-8")
    assert store.load() == {}, "坏文件当“没有决定”,不能因此崩启动"


def test_resolver_precedence(tmp_path):
    """`-a`/`-na` > trust.json > defaultProjectTrust(只用户级)。"""
    from qi_agent.settings import QiSettings, resolve_project_trust

    user_always = QiSettings(defaultProjectTrust="always")
    assert resolve_project_trust(user_always, stored=(False, "/x"))[0] is False
    assert resolve_project_trust(user_always, stored=(True, "/x"))[0] is True
    # 已存决定之上还有 CLI
    assert resolve_project_trust(user_always, approve=False, stored=(True, "/x"))[0] is False
    assert resolve_project_trust(user_always, approve=True, stored=(False, "/x"))[0] is True
    # 没有已存决定时,才轮到 default
    assert resolve_project_trust(user_always)[0] is True
    assert "trust.json" in resolve_project_trust(user_always, stored=(True, "/x"))[1]


def test_runtime_honors_a_remembered_decision(tmp_path, monkeypatch):
    """端到端:记住的 `true` 让项目被信任(不用 `-a`、用户级也没表态)。"""
    from qi_agent.trust import TrustStore

    project = _env(tmp_path, monkeypatch, user={})
    TrustStore().set(project, True)

    rt = _runtime(project)
    assert rt.project_trusted is True, rt.trust_reason
    assert "trust.json" in rt.trust_reason
    assert "from-repo" in rt.extensions


def test_runtime_honors_a_remembered_denial(tmp_path, monkeypatch):
    """记住的 `false` 也照样赢过用户级的 `always`(最近的决定优先)。"""
    from qi_agent.trust import TrustStore

    project = _env(tmp_path, monkeypatch, user={"defaultProjectTrust": "always"})
    TrustStore().set(project, False)

    rt = _runtime(project)
    assert rt.project_trusted is False, rt.trust_reason
    assert "from-repo" not in rt.extensions
