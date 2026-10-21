"""`store.json` 的两层语义 —— 重点是那条**硬规则**:凭证只能来自用户级。"""

from __future__ import annotations

import json
import os

import pytest

from qi_store import config, paths
from qi_store.errors import ConfigError


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_user_layer_carries_auth(store_home):
    _write(store_home / "store.json", {
        "stores": {"hikqin": {"baseUrl": "http://h:6091/", "auth": {
            "login": "admin", "role": "admin", "access": "a", "refresh": "r", "exp": 1}}},
        "defaultStore": "hikqin",
    })
    cfg = config.load()
    source = cfg.source()
    assert source.base_url == "http://h:6091"        # 末尾斜杠被归一
    assert source.login_name == "admin"
    assert source.access_token == "a"


def test_project_layer_may_not_carry_auth(store_home, project, capsys):
    _write(store_home / "store.json", {
        "stores": {"hikqin": {"baseUrl": "http://user:6091", "auth": {
            "login": "me", "access": "user-token"}}}})
    _write(project / ".qi" / "store.json", {
        "stores": {"hikqin": {"baseUrl": "http://repo:6091",
                              "auth": {"login": "leak", "access": "leaked-token"}}}})
    cfg = config.load(project)
    source = cfg.source()
    # 项目级能覆盖地址(它不是秘密),但**绝不允许**覆盖凭证
    assert source.base_url == "http://repo:6091"
    assert source.access_token == "user-token"
    assert any("只能放用户级" in warning for warning in cfg.warnings)


def test_project_only_source_is_not_logged_in(store_home, project):
    _write(project / ".qi" / "store.json", {
        "stores": {"team": {"baseUrl": "http://team:6091",
                            "auth": {"access": "nope"}}}})
    cfg = config.load(project)
    assert cfg.sources["team"].access_token is None
    assert cfg.warnings


def test_broken_json_names_the_file(store_home):
    path = store_home / "store.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        config.load()
    assert str(path) in str(excinfo.value)


def test_unknown_source_lists_known_ones(store_home):
    _write(store_home / "store.json", {"stores": {"hikqin": {"baseUrl": "http://h:6091"}}})
    with pytest.raises(ConfigError) as excinfo:
        config.load().source("nope")
    assert "hikqin" in str(excinfo.value.hint)


def test_qi_store_url_overrides_base_url(store_home, monkeypatch):
    _write(store_home / "store.json", {"stores": {"hikqin": {"baseUrl": "http://h:6091"}}})
    monkeypatch.setenv("QI_STORE_URL", "http://other:7000/")
    assert config.load().source().base_url == "http://other:7000"


def test_save_auth_persists_and_is_private(store_home):
    path = config.save_auth("hikqin", {"login": "admin", "access": "a"}, base_url="http://h:6091/")
    assert path == store_home / "store.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["stores"]["hikqin"]["auth"]["login"] == "admin"
    assert stored["stores"]["hikqin"]["baseUrl"] == "http://h:6091"
    if os.name == "posix":     # Windows 的 chmod 只有只读位,不在这里断言
        assert (path.stat().st_mode & 0o077) == 0


def test_save_auth_none_removes_the_block(store_home):
    config.save_auth("hikqin", {"login": "admin", "access": "a"}, base_url="http://h:6091")
    config.save_auth("hikqin", None)
    stored = json.loads((store_home / "store.json").read_text(encoding="utf-8"))
    assert "auth" not in stored["stores"]["hikqin"]
    assert stored["stores"]["hikqin"]["baseUrl"] == "http://h:6091"   # 地址留着


def test_first_login_sets_default_store(store_home):
    config.save_auth("hikqin", {"access": "a"}, base_url="http://h:6091")
    config.save_default("hikqin")
    assert config.load().default_store == "hikqin"


def test_paths_are_three_places(store_home, project):
    """三个落点:用户级配置 / 项目级配置 / 账本目录 —— 别混。"""
    assert paths.user_config_file() == store_home / "store.json"
    assert paths.project_config_file(project) == project / ".qi" / "store.json"
    assert paths.installed_file() == store_home / "store" / "installed.json"
    assert paths.cache_file("../evil") .name == "..evil.json"   # 净化:逃不出缓存目录


def test_qi_config_dir_is_honoured(tmp_path, monkeypatch):
    """qi 的两个环境变量都要认。

    只认 `QI_AGENT_HOME` 时,设了 `QI_CONFIG_DIR` 的环境(qi 自己的测试隔离、多实例部署)
    会被我们写到**真实的** `~/.qi/agent` —— 这已经踩过一次:测试把 store.json / 账本 /
    技能写进了开发机的 home。
    """
    monkeypatch.delenv("QI_AGENT_HOME", raising=False)
    monkeypatch.setenv("QI_CONFIG_DIR", str(tmp_path / "ns"))
    assert paths.global_agent_dir() == tmp_path / "ns" / "agent"
    assert paths.user_config_file() == tmp_path / "ns" / "agent" / "store.json"

    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "explicit"))
    assert paths.global_agent_dir() == tmp_path / "explicit"     # 显式优先
