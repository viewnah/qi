"""P1 基础测试:配置分层/深合并、凭证三源解析。"""

from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from qi_agent.auth import AuthStore, resolve_key
from qi_agent.config import ModelSpec, QiConfig, deep_merge, load_config


def test_deep_merge():
    base = {"a": 1, "m": {"x": 1, "y": 2}}
    over = {"m": {"y": 9, "z": 3}, "b": 2}
    assert deep_merge(base, over) == {"a": 1, "m": {"x": 1, "y": 9, "z": 3}, "b": 2}


def test_model_spec_unknown_provider_rejected():
    with pytest.raises(ValidationError):
        ModelSpec(provider="unknown-xyz", model="m")


def test_load_config_precedence(tmp_path, monkeypatch):
    user_dir = tmp_path / "user"
    user_toml = user_dir / "qi_agent.toml"
    user_dir.mkdir()
    user_toml.write_text('[models.default]\nprovider="deepseek"\nmodel="user-model"\n', encoding="utf-8")
    env_file = tmp_path / "env" / "qi_agent.toml"
    env_file.parent.mkdir()
    env_file.write_text('[models.default]\nmodel="env-model"\n', encoding="utf-8")
    monkeypatch.setenv("QI_AGENT_CONFIG", str(env_file))
    monkeypatch.setenv("QI_AGENT_HOME", str(user_dir))
    # 项目层不参与(此处无 git);env 文件应覆盖用户层同键
    cfg, loaded = load_config()
    assert cfg.models.default is not None
    assert cfg.models.default.provider == "deepseek"  # 来自用户层
    assert cfg.models.default.model == "env-model"     # env 文件覆盖
    assert [str(p) for p in loaded] == [str(env_file), str(user_toml)]


def test_resolve_key_order(tmp_path, monkeypatch):
    store = AuthStore(path=tmp_path / "auth.json")
    spec = ModelSpec(provider="deepseek", model="m")
    # 3. 约定 env
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-default-env")
    assert resolve_key(spec, store).source == "env:default"
    # 2. auth store 优先于约定 env
    store.set_key("deepseek", "from-store")
    assert resolve_key(spec, store).source == "auth"
    # 1. 显式 api_key_env 最高
    monkeypatch.setenv("MY_DS", "from-explicit")
    spec2 = ModelSpec(provider="deepseek", model="m", api_key_env="MY_DS")
    assert resolve_key(spec2, store).source == "env:MY_DS"
    # 显式变量未设置 → missing
    spec3 = ModelSpec(provider="deepseek", model="m", api_key_env="NOT_SET_ANYWHERE")
    assert not resolve_key(spec3, store).ok
    # ollama 免 key
    assert resolve_key(ModelSpec(provider="ollama", model="q"), store).ok


def test_auth_store_roundtrip_and_perms(tmp_path):
    store = AuthStore(path=tmp_path / "auth.json")
    store.set_key("deepseek", "sk-abc")
    assert store.get("deepseek") == "sk-abc"
    assert store.providers() == ["deepseek"]
    assert store.remove("deepseek")
    assert not store.remove("deepseek")
    assert store.providers() == []
