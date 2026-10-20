"""qi-a2a:两层 a2a.json 的合并、环境变量与校验。

这一层**不碰网络** —— 配置读错与协议写错是两类问题,分开测。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # tests/ → qi-a2a/ → extensions/ → 仓库根
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-a2a"))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_a2a import config  # noqa: E402
from qi_a2a.config import ConfigError, A2AEntry  # noqa: E402


def _env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return home, project


def _write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"a2aServers": servers}), encoding="utf-8")


def test_project_deep_merges_same_name(tmp_path, monkeypatch):
    """同名**深合并**:项目只写差异(tenant / headers),不用抄一遍 baseUrl。"""
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "a2a.json", {"hikqin": {"baseUrl": "http://gw:8080", "timeoutMs": 1000}})
    _write(project / ".qi" / "a2a.json",
           {"hikqin": {"tenant": "agent-1", "headers": {"X-Env": "dev"}}})

    entry = config.resolve(project)["hikqin"]
    assert entry.config["baseUrl"] == "http://gw:8080"          # 全局的还在
    assert entry.config["tenant"] == "agent-1"                  # 项目的叠上来
    assert entry.config["timeoutMs"] == 1000
    assert entry.scope == "project"                            # 来源层看得到
    assert entry.path == project / ".qi" / "a2a.json"


def test_null_deletes_a_key(tmp_path, monkeypatch):
    """`null` 删除键 —— 项目层关掉全局的某个 header 时不必重写整段。"""
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "a2a.json",
           {"a": {"baseUrl": "http://x", "headers": {"A": "1", "B": "2"}}})
    _write(project / ".qi" / "a2a.json", {"a": {"headers": {"B": None}}})

    entry = config.resolve(project)["a"]
    assert entry.headers == {"A": "1"}


def test_project_can_disable_a_global_entry(tmp_path, monkeypatch):
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "a2a.json", {"a": {"baseUrl": "http://x"}, "b": {"baseUrl": "http://y"}})
    _write(project / ".qi" / "a2a.json", {"a": {"disabled": True}})

    assert set(config.active(project)) == {"b"}
    assert config.resolve(project)["a"].disabled is True
    assert config.resolve(project)["b"].disabled is False


def test_disabled_only_accepts_literal_true(tmp_path, monkeypatch):
    """`"true"` 字符串 / 数字 1 都不算关(照 qi-mcp:避免手写配置误判)。"""
    _env(tmp_path, monkeypatch)
    entry = A2AEntry(name="a", config={"baseUrl": "http://x", "disabled": "true"})
    assert entry.disabled is False
    assert A2AEntry(name="a", config={"baseUrl": "http://x", "disabled": 1}).disabled is False


def test_arrays_replace_instead_of_merging(tmp_path, monkeypatch):
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "a2a.json", {"a": {"baseUrl": "http://x"}})
    _write(project / ".qi" / "a2a.json", {"a": {"directTools": ["one"]}})
    assert config.resolve(project)["a"].config["directTools"] == ["one"]


def test_env_placeholder_is_expanded(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    monkeypatch.setenv("A2A_TEST_TOKEN", "secret-value")
    _write(home / "a2a.json", {"a": {
        "baseUrl": "http://x",
        "headers": {"Authorization": "Bearer ${A2A_TEST_TOKEN}"},
    }})
    entry = config.resolve(tmp_path)["a"]
    assert entry.headers["Authorization"] == "Bearer secret-value"


def test_missing_env_names_the_variable(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    monkeypatch.delenv("A2A_MISSING_TOKEN", raising=False)
    _write(home / "a2a.json", {"a": {
        "baseUrl": "http://x", "headers": {"Authorization": "${A2A_MISSING_TOKEN}"}}})
    with pytest.raises(ConfigError, match="A2A_MISSING_TOKEN"):
        config.resolve(tmp_path)


def test_untrusted_project_layer_is_skipped(tmp_path, monkeypatch):
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "a2a.json", {"a": {"baseUrl": "http://global"}})
    _write(project / ".qi" / "a2a.json", {"a": {"baseUrl": "http://project"}})

    assert config.resolve(project, trusted=False)["a"].base_url == "http://global"
    assert config.resolve(project, trusted=True)["a"].base_url == "http://project"


def test_bad_json_points_at_the_file(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    path = home / "a2a.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="a2a.json"):
        config.resolve(tmp_path)


def test_url_scheme_is_rejected(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    _write(home / "a2a.json", {"a": {"baseUrl": "ftp://x"}})
    with pytest.raises(ConfigError, match="http/https"):
        config.resolve(tmp_path)


def test_entry_needs_an_endpoint_unless_disabled(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    _write(home / "a2a.json", {"a": {"description": "只有描述"}})
    with pytest.raises(ConfigError, match="缺少"):
        config.resolve(tmp_path)

    _write(home / "a2a.json", {"a": {"disabled": True}})
    assert config.resolve(tmp_path)["a"].disabled is True


def test_unknown_fields_are_reported_not_fatal(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    _write(home / "a2a.json", {"a": {"baseUrl": "http://x", "pushNotifications": True}})
    entry = config.resolve(tmp_path)["a"]
    assert config.unknown_fields(entry) == ["pushNotifications"]


def test_card_url_entry_summary_and_tool_name():
    entry = A2AEntry(name="hikqin analysis", config={
        "cardUrl": "http://x/a2a/agents/a1/card.json"})
    assert entry.summary().startswith("card: ")
    assert config.entry_tool_name(entry) == "a2a__hikqin_analysis"


def test_scope_files_prefers_project_second(tmp_path, monkeypatch):
    home, project = _env(tmp_path, monkeypatch)
    files = config.scope_files(project)
    assert files[0] == ("global", home / "a2a.json")
    assert files[1] == ("project", project / ".qi" / "a2a.json")
