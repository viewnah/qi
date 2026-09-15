"""settings.json 分层 + agent 目录迁移 + 顶层技能发现 + config/auth 命令。

对齐 pi 的三条关键语义:
  1. `~/.qi/agent/` ↔ `<项目>/.qi` 是配对的一对(全局深一层);
  2. 默认模型属于 settings.json(项目 > 全局),models.json 为兼容老位置;
  3. 资源路径按**各自** settings.json 所在目录解析,排除项作用于整个发现集。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from qi_agent import paths
from qi_agent.cli import app
from qi_agent.config import default_model_spec, load_config
from qi_agent.loader import (
    LoadError,
    load_top_level_skills,
    scan_skill_root,
    top_level_skill_dirs,
)
from qi_agent.settings import (
    SettingsError,
    QiSettings,
    double_escape_action,
    load_settings,
    load_settings_by_scope,
    parse_value,
    session_dir,
    set_value,
    settings_exclude_paths,
    settings_include_paths,
    unset_value,
)

runner = CliRunner()


# ── 脚手架 ──────────────────────────────────────────────

def _env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """隔离环境:返回 (agent 目录, home)。

    必须同时钉住 HOME —— `~/.agents/skills` 是照着 HOME 找的,不隔离就会读到
    开发机上的真实技能。
    """
    home = tmp_path / "home"
    home.mkdir()
    agent = tmp_path / ".qi" / "agent"
    agent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("QI_AGENT_HOME", str(agent))
    monkeypatch.delenv("QI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("QI_AGENT_CONFIG", raising=False)
    return agent, home


def _proj(tmp_path: Path, name: str = "proj") -> Path:
    proj = tmp_path / name
    (proj / ".git").mkdir(parents=True)
    return proj


def _skill(root: Path, dirname: str, name: str, desc: str = "d") -> Path:
    """在 root/dirname/SKILL.md 写一个技能,返回文件路径。"""
    folder = root / dirname
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {desc}\n---\n正文\n", encoding="utf-8")
    return path


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── 1. settings.json 分层与合并 ─────────────────────────

def test_settings_project_overrides_global_deep_merge(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    proj = _proj(tmp_path)
    _write(agent / "settings.json", {
        "theme": "dark",
        "compaction": {"enabled": True, "reserveTokens": 16384},
        "skills": ["a", "b"],
    })
    _write(proj / ".qi" / "settings.json", {
        "compaction": {"reserveTokens": 8192},
        "skills": ["c"],
    })

    settings, files = load_settings(proj)
    assert [p.name for p in files] == ["settings.json", "settings.json"]
    assert files[0].parent == proj / ".qi"          # 高优先级在前
    assert settings.theme == "dark"                 # 全局保留
    assert settings.compaction == {"enabled": True, "reserveTokens": 8192}   # 深合并
    assert settings.skills == ["c"]                 # 数组整体替换(不逐项合并)


def test_settings_unknown_keys_preserved_and_defaults(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    _write(agent / "settings.json", {"myExtension": {"x": 1}})
    settings, _files = load_settings(tmp_path)
    assert settings.model_extra is not None and settings.model_extra["myExtension"] == {"x": 1}
    assert settings.defaultProjectTrust == "ask"    # 默认值
    assert settings.skillsEnabled is True


def test_double_escape_action_defaults_and_normalizes():
    """双击 escape 的默认/非法值都归一到 tree(对齐 pi 的 getDoubleEscapeAction)。"""
    assert double_escape_action(None) == "tree"
    assert double_escape_action(QiSettings()) == "tree"
    assert double_escape_action(QiSettings(doubleEscapeAction="fork")) == "fork"
    assert double_escape_action(QiSettings(doubleEscapeAction=" TREE ")) == "tree"
    assert double_escape_action(QiSettings(doubleEscapeAction="nope")) == "tree"
    assert double_escape_action(QiSettings(doubleEscapeAction="none")) == "none"


def test_settings_invalid_json_raises(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    (agent / "settings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SettingsError):
        load_settings(tmp_path)


def test_settings_by_scope_keeps_scopes_separate(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    proj = _proj(tmp_path)
    _write(agent / "settings.json", {"skills": ["u"], "theme": "dark"})
    _write(proj / ".qi" / "settings.json", {"skills": ["p"]})

    scopes = load_settings_by_scope(proj)
    assert set(scopes) == {"user", "project"}
    assert scopes["user"].skills == ["u"]           # 未被项目覆盖
    assert scopes["project"].skills == ["p"]


# ── 2. 默认模型只认 settings.json ────────────────────────

def test_default_model_comes_from_settings(tmp_path, monkeypatch):
    _agent, _home = _env(tmp_path, monkeypatch)
    _write(_agent / "settings.json", {"defaultProvider": "new", "defaultModel": "new-model"})
    provider, model, source = default_model_spec(tmp_path)
    assert (provider, model) == ("new", "new-model")
    assert source.startswith("settings:")


def test_models_json_default_keys_are_not_read(tmp_path, monkeypatch):
    """models.json 里的 defaultProvider/defaultModel 不再生效(无兼容层)。"""
    agent, _home = _env(tmp_path, monkeypatch)
    _write(agent / "models.json", {
        "defaultProvider": "p", "defaultModel": "m",
        "providers": {"p": {"models": [{"id": "m"}]}},
    })
    cfg, _files = load_config(tmp_path)
    assert not hasattr(cfg, "defaultProvider")          # 已不在配置面
    assert default_model_spec(tmp_path) == (None, None, "未配置")


# ── 2b. 默认模型来源仍是两层(项目 > 全局) ───────────

def test_project_settings_default_model_beats_global(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    proj = _proj(tmp_path)
    _write(agent / "settings.json", {"defaultProvider": "g", "defaultModel": "gm"})
    _write(proj / ".qi" / "settings.json", {"defaultModel": "pm"})
    assert default_model_spec(proj) == ("g", "pm", default_model_spec(proj)[2])   # 逐字段覆盖
    assert default_model_spec(proj)[:2] == ("g", "pm")


# ── 3. 旧扁平布局迁移 ───────────────────────────────────

def test_legacy_layout_migrated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / ".qi"
    root.mkdir()
    _write(root / "models.json", {"providers": {}})
    (root / "sessions").mkdir()
    (root / "agents" / "a").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("QI_CONFIG_DIR", str(root))
    monkeypatch.delenv("QI_AGENT_HOME", raising=False)

    moved = paths.ensure_layout()

    assert len(moved) == 3
    assert (root / "agent" / "models.json").is_file()
    assert (root / "agent" / "sessions").is_dir()
    assert (root / "agent" / "agents" / "a").is_dir()
    assert not (root / "models.json").exists()      # 原位已搬走
    assert paths.global_home() == root / "agent"


def test_migration_never_overwrites_existing_target(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / ".qi"
    (root / "agent").mkdir(parents=True)
    _write(root / "agent" / "models.json", {"kept": True})
    _write(root / "models.json", {"old": True})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("QI_CONFIG_DIR", str(root))
    monkeypatch.delenv("QI_AGENT_HOME", raising=False)

    assert paths.ensure_layout() == []
    assert json.loads((root / "agent" / "models.json").read_text(encoding="utf-8")) == {"kept": True}
    assert (root / "models.json").is_file()         # 旧文件保持原样,不删也不覆盖


def test_migration_skipped_when_agent_home_env_set(tmp_path, monkeypatch):
    """显式指定 QI_AGENT_HOME(测试/CI)时不做迁移 —— 那是调用方自己的目录。"""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / ".qi"
    root.mkdir()
    _write(root / "models.json", {"old": True})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("QI_CONFIG_DIR", str(root))
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "custom-agent"))

    assert paths.migrate_legacy_layout() == []
    assert (root / "models.json").is_file()


# ── 4. 顶层技能:来源与优先级 ───────────────────────────

def test_skill_layer_order_low_to_high(tmp_path, monkeypatch):
    agent, home = _env(tmp_path, monkeypatch)
    proj = _proj(tmp_path)
    entries = top_level_skill_dirs(proj)
    labels = [label for _path, label, _md in entries]

    assert labels[0] == "agents-global"
    assert labels[1] == "qi-global"
    assert labels[-1] in ("qi-project", "settings-project")
    assert labels.index("qi-global") < labels.index("agents-project")
    assert labels.index("agents-project") < labels.index("qi-project")
    assert entries[0][0] == home / ".agents" / "skills"
    assert entries[1][0] == agent / "skills"
    assert entries[0][2] is False       # .agents 层不认根级 *.md(对齐 pi)
    assert entries[1][2] is True        # qi 私有层认


def test_skill_priority_project_beats_global(tmp_path, monkeypatch):
    agent, home = _env(tmp_path, monkeypatch)
    proj = _proj(tmp_path)
    _skill(home / ".agents" / "skills", "s", "dup", "跨工具全局")
    _skill(agent / "skills", "s", "dup", "qi 全局")
    _skill(proj / ".agents" / "skills", "s", "dup", "跨工具项目")
    _skill(proj / ".qi" / "skills", "s", "dup", "qi 项目")

    skills = {s.name: s for s in load_top_level_skills(proj)}
    assert skills["dup"].description == "qi 项目"       # 最高:qi 项目私有
    assert skills["dup"].source == "qi-project"

    # 去掉最高层,次高应为跨工具项目层
    (proj / ".qi" / "skills" / "s" / "SKILL.md").unlink()
    skills = {s.name: s for s in load_top_level_skills(proj)}
    assert skills["dup"].source == "agents-project"

    # 再去掉项目层,应为 qi 全局(而非跨工具全局)
    import shutil
    shutil.rmtree(proj / ".agents")
    skills = {s.name: s for s in load_top_level_skills(proj)}
    assert skills["dup"].description == "qi 全局"


def test_skill_same_layer_conflict_raises(tmp_path, monkeypatch):
    _agent, _home = _env(tmp_path, monkeypatch)
    proj = _proj(tmp_path)
    _skill(proj / ".qi" / "skills", "a", "same", "A")
    _skill(proj / ".qi" / "skills", "b", "same", "B")

    with pytest.raises(LoadError, match="在 qi-project 层重复"):
        load_top_level_skills(proj)


def test_cross_tool_root_md_ignored_but_group_discovered(tmp_path, monkeypatch):
    agent, home = _env(tmp_path, monkeypatch)
    agents_root = home / ".agents" / "skills"
    agents_root.mkdir(parents=True)
    (agents_root / "loose.md").write_text(
        "---\nname: loose\ndescription: 根级 md\n---\n", encoding="utf-8")
    _skill(agents_root / "group", "nested", "grouped", "分组目录内")

    names = {s.name for s in load_top_level_skills(tmp_path)}
    assert "grouped" in names          # 分组目录里的 SKILL.md 会被发现
    assert "loose" not in names        # .agents 根级 *.md 被忽略

    # qi 私有层则认根级 *.md
    (agent / "skills").mkdir(parents=True, exist_ok=True)
    (agent / "skills" / "solo.md").write_text(
        "---\nname: solo\ndescription: qi 根级 md\n---\n", encoding="utf-8")
    assert "solo" in {s.name for s in load_top_level_skills(tmp_path)}


def test_skill_dir_with_skill_md_is_not_recursed_further(tmp_path, monkeypatch):
    _agent, _home = _env(tmp_path, monkeypatch)
    root = tmp_path / "root"
    _skill(root, "outer", "outer", "外层")
    _skill(root / "outer", "inner", "inner", "不该被发现")   # outer 已含 SKILL.md

    found: list = []
    scan_skill_root(root, label="t", out=found, allow_root_md=True)
    assert {s.name for s in found} == {"outer"}


def test_skills_disabled_still_loads_cli_paths(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    _skill(agent / "skills", "x", "from-dir")
    extra = tmp_path / "cli-skill"
    _skill(tmp_path, "cli-skill", "from-cli")

    both = load_top_level_skills(tmp_path, extra_paths=[extra])
    assert {s.name for s in both} == {"from-dir", "from-cli"}

    only_cli = load_top_level_skills(tmp_path, extra_paths=[extra], enabled=False)
    assert {s.name for s in only_cli} == {"from-cli"}       # 对齐 pi --no-skills + --skill


# ── 5. settings.skills:各作用域各按自己基准解析 ─────────

def test_settings_skills_resolve_against_own_scope(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    proj = _proj(tmp_path)
    _write(agent / "settings.json", {"skills": ["u"]})
    _skill(agent, "u", "user-skill")
    _write(proj / ".qi" / "settings.json", {"skills": ["p"]})
    _skill(proj / ".qi", "p", "proj-skill")

    skills = {s.name: s.source for s in load_top_level_skills(proj)}
    assert skills == {"user-skill": "settings-global", "proj-skill": "settings-project"}


def test_settings_skills_honor_tilde_and_absolute(tmp_path, monkeypatch):
    agent, home = _env(tmp_path, monkeypatch)
    _skill(home / "tilde-skills", "t", "tilde-skill")
    abs_dir = tmp_path / "abs-skills"
    _skill(abs_dir, "a", "abs-skill")
    _write(agent / "settings.json", {"skills": ["~/tilde-skills", str(abs_dir)]})

    names = {s.name for s in load_top_level_skills(tmp_path)}
    assert {"tilde-skill", "abs-skill"} <= names


def test_settings_skills_exclusion_applies_to_discovered_set(tmp_path, monkeypatch):
    """排除项要能关掉**默认目录**里扫出来的技能,而不只是数组里纳入的根。"""
    agent, _home = _env(tmp_path, monkeypatch)
    _skill(agent / "skills", "keep", "keep")
    _skill(agent / "skills", "drop", "drop")
    _write(agent / "settings.json", {"skills": ["-skills/drop"]})

    names = {s.name for s in load_top_level_skills(tmp_path)}
    assert names == {"keep"}


def test_settings_include_and_exclude_paths_are_separate(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    for name in ("extra", "skip", "also-skip"):
        (agent / name).mkdir(parents=True)
    _write(agent / "settings.json",
           {"skills": ["extra", "-skip", "!also-skip", "-missing"]})

    scopes = load_settings_by_scope(tmp_path)
    includes = settings_include_paths(scopes["user"], "user", "skills", tmp_path)
    excludes = settings_exclude_paths(scopes["user"], "user", "skills", tmp_path)
    assert includes == [agent / "extra"]
    # 不存在的排除路径会被丢弃(运行时解析,没有目标可排除)
    assert excludes == [agent / "skip", agent / "also-skip"]


# ── 6. sessionDir ───────────────────────────────────────

def test_session_dir_project_beats_global(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    proj = _proj(tmp_path)
    _write(agent / "settings.json", {"sessionDir": "sessions-global"})
    assert session_dir(load_settings(tmp_path)[0], tmp_path,
                       load_settings_by_scope(tmp_path)) == agent / "sessions-global"

    _write(proj / ".qi" / "settings.json", {"sessionDir": "sessions-proj"})
    assert session_dir(load_settings(proj)[0], proj,
                       load_settings_by_scope(proj)) == proj / ".qi" / "sessions-proj"


def test_session_dir_absent_returns_none(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert session_dir(load_settings(tmp_path)[0], tmp_path,
                       load_settings_by_scope(tmp_path)) is None


# ── 7. 点号键读写 ───────────────────────────────────────

def test_set_unset_value_roundtrip(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    set_value("user", "compaction.enabled", False, tmp_path)
    set_value("user", "defaultModel", "m1", tmp_path)
    settings, _files = load_settings(tmp_path)
    assert settings.compaction == {"enabled": False}
    assert settings.defaultModel == "m1"

    _path, removed = unset_value("user", "compaction.enabled", tmp_path)
    assert removed is True
    raw = json.loads((agent / "settings.json").read_text(encoding="utf-8"))
    assert "enabled" not in raw.get("compaction", {})     # 叶子键已删(空对象保留)
    _path, removed_again = unset_value("user", "compaction.enabled", tmp_path)
    assert removed_again is False


def test_parse_value_json_then_string():
    assert parse_value("true") is True
    assert parse_value('["a"]') == ["a"]
    assert parse_value("light") == "light"          # 非 JSON → 当字符串


# ── 8. CLI:qi config ────────────────────────────────────

def _invoke(monkeypatch, tmp_path, *args: str):
    monkeypatch.chdir(tmp_path)
    return runner.invoke(app, list(args))


def test_cli_config_set_and_show(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    res = _invoke(monkeypatch, tmp_path, "config", "--set", "defaultModel=m1")
    assert res.exit_code == 0, res.output
    assert json.loads((agent / "settings.json").read_text(encoding="utf-8"))["defaultModel"] == "m1"

    res = _invoke(monkeypatch, tmp_path, "config", "--json")
    assert res.exit_code == 0
    assert '"defaultModel": "m1"' in res.output


def test_cli_config_local_writes_project(tmp_path, monkeypatch):
    _agent, _home = _env(tmp_path, monkeypatch)
    proj = _proj(tmp_path)
    res = _invoke(monkeypatch, proj, "config", "-l", "--set", "theme=light")
    assert res.exit_code == 0, res.output
    data = json.loads((proj / ".qi" / "settings.json").read_text(encoding="utf-8"))
    assert data == {"theme": "light"}


def test_cli_config_set_requires_kv(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = _invoke(monkeypatch, tmp_path, "config", "--set", "novalue")
    assert res.exit_code == 2
    assert "K=V" in res.output


def test_cli_config_unset(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    _write(agent / "settings.json", {"theme": "dark", "defaultModel": "m"})
    res = _invoke(monkeypatch, tmp_path, "config", "--unset", "theme")
    assert res.exit_code == 0, res.output
    assert "theme" not in json.loads((agent / "settings.json").read_text(encoding="utf-8"))


def test_cli_config_lists_top_level_skills(tmp_path, monkeypatch):
    agent, _home = _env(tmp_path, monkeypatch)
    _skill(agent / "skills", "s", "visible-skill", "看得见")
    res = _invoke(monkeypatch, tmp_path, "config")
    assert res.exit_code == 0, res.output
    assert "visible-skill" in res.output
    assert "qi-global" in res.output


# ── 9. CLI:qi auth(对齐 pi 的退出码) ────────────────────

def _auth_env(tmp_path, monkeypatch, *, api_key: str | None = None) -> Path:
    agent, _home = _env(tmp_path, monkeypatch)
    _write(agent / "models.json", {
        "providers": {"cc": {
            "baseUrl": "https://x/v1", "api": "openai-completions",
            "models": [{"id": "m1"}],
            **({"apiKey": api_key} if api_key is not None else {}),
        }},
    })
    monkeypatch.delenv("CC_API_KEY", raising=False)
    return agent


def test_cli_auth_print_api_key_from_models_json(tmp_path, monkeypatch):
    _auth_env(tmp_path, monkeypatch, api_key="sk-plaintext")
    res = _invoke(monkeypatch, tmp_path, "auth", "print-api-key", "--provider", "cc")
    assert res.exit_code == 0, res.output
    assert res.output.strip() == "sk-plaintext"


def test_cli_auth_print_api_key_by_model(tmp_path, monkeypatch):
    _auth_env(tmp_path, monkeypatch, api_key="sk-by-model")
    res = _invoke(monkeypatch, tmp_path, "auth", "print-api-key", "--model", "m1")
    assert res.exit_code == 0, res.output
    assert res.output.strip() == "sk-by-model"


def test_cli_auth_print_api_key_without_credentials_fails(tmp_path, monkeypatch):
    _auth_env(tmp_path, monkeypatch)
    res = _invoke(monkeypatch, tmp_path, "auth", "print-api-key", "--provider", "cc")
    assert res.exit_code == 1                     # 对齐 pi:凭证类错误退 1
    assert "Error:" in res.output


def test_cli_auth_requires_provider_or_model(tmp_path, monkeypatch):
    _auth_env(tmp_path, monkeypatch, api_key="sk")
    res = _invoke(monkeypatch, tmp_path, "auth", "print-api-key")
    assert res.exit_code == 1
    assert "--provider" in res.output


def test_cli_auth_check_exit_codes(tmp_path, monkeypatch):
    _auth_env(tmp_path, monkeypatch, api_key="sk-ready")
    ok = _invoke(monkeypatch, tmp_path, "auth", "check", "--provider", "cc")
    assert ok.exit_code == 0 and ok.output.strip() == "ready"

    unknown = _invoke(monkeypatch, tmp_path, "auth", "check", "--provider", "nope")
    assert unknown.exit_code == 1 and unknown.output.strip() == "not_ready"

    invalid = _invoke(monkeypatch, tmp_path, "auth", "check")
    assert invalid.exit_code == 2 and invalid.output.strip() == "invalid"


def test_cli_auth_check_json_and_credentials(tmp_path, monkeypatch):
    _auth_env(tmp_path, monkeypatch, api_key="sk-json")
    res = _invoke(monkeypatch, tmp_path, "auth", "check", "--provider", "cc",
                  "--json", "--credentials")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload == {"status": "ready", "provider": "cc",
                       "authType": "api_key", "credentials": "sk-json"}


def test_cli_auth_bearer_token_validates_min_expiry(tmp_path, monkeypatch):
    _auth_env(tmp_path, monkeypatch, api_key="sk-bearer")
    ok = _invoke(monkeypatch, tmp_path, "auth", "print-bearer-token",
                 "--provider", "cc", "--min-expiry", "30m")
    assert ok.exit_code == 0 and ok.output.strip() == "sk-bearer"

    bad = _invoke(monkeypatch, tmp_path, "auth", "print-bearer-token",
                  "--provider", "cc", "--min-expiry", "bogus")
    assert bad.exit_code == 1
    assert "min-expiry" in bad.output


def test_cli_auth_store_key_wins_over_models_json(tmp_path, monkeypatch):
    agent = _auth_env(tmp_path, monkeypatch, api_key="sk-plaintext")
    _write(agent / "auth.json", {"cc": {"type": "api_key", "key": "sk-from-store"}})
    res = _invoke(monkeypatch, tmp_path, "auth", "print-api-key", "--provider", "cc")
    assert res.exit_code == 0
    assert res.output.strip() == "sk-from-store"   # auth store 优先(对齐 pi 顺序)
