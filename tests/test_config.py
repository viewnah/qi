"""P1 基础测试:models.json 分层/深合并、模型解析、凭证解析。"""

from __future__ import annotations

import json

import pytest

from qi_agent.auth import AuthStore, resolve_key, resolve_value
from qi_agent.config import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_TOKENS,
    ConfigError,
    ModelEntry,
    QiConfig,
    deep_merge,
    default_model_spec,
    legacy_default_keys,
    load_config,
    resolve_default_model,
    resolve_model,
)


def test_deep_merge():
    base = {"a": 1, "m": {"x": 1, "y": 2}}
    over = {"m": {"y": 9, "z": 3}, "b": 2}
    assert deep_merge(base, over) == {"a": 1, "m": {"x": 1, "y": 9, "z": 3}, "b": 2}


def test_model_entry_defaults():
    entry = ModelEntry(id="m")
    assert entry.reasoning is False
    assert entry.contextWindow == DEFAULT_CONTEXT_WINDOW
    assert entry.maxTokens == DEFAULT_MAX_TOKENS
    assert entry.input == ["text"]


def test_resolve_model_merges_provider_and_entry():
    cfg = QiConfig.model_validate({
        "providers": {
            "my-llm": {
                "baseUrl": "http://x/v1",
                "api": "openai-completions",
                "apiKey": "$MY_KEY",
                "models": [{"id": "m", "reasoning": True, "contextWindow": 999,
                            "maxTokens": 123}],
            }
        }
    })
    spec = resolve_model(cfg, "my-llm", "m")
    assert spec.base_url == "http://x/v1"
    assert spec.api == "openai-completions"
    assert spec.api_key_ref == "$MY_KEY"
    assert spec.reasoning is True
    assert spec.context_window == 999
    assert spec.max_tokens == 123
    # 未知模型条目:给默认值,不报错
    unknown = resolve_model(cfg, "my-llm", "nope")
    assert unknown.context_window == DEFAULT_CONTEXT_WINDOW
    assert unknown.api == "openai-completions"


def test_load_config_precedence(tmp_path, monkeypatch):
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "models.json").write_text(json.dumps({
        "providers": {"deepseek": {"models": [{"id": "user-model"}]}},
    }), encoding="utf-8")
    env_file = tmp_path / "env" / "models.json"
    env_file.parent.mkdir()
    env_file.write_text(json.dumps({
        "providers": {"openai": {"models": [{"id": "env-model"}]}},
    }), encoding="utf-8")
    monkeypatch.setenv("QI_AGENT_CONFIG", str(env_file))
    monkeypatch.setenv("QI_AGENT_HOME", str(user_dir))
    # cwd 隔离,避免读到仓库自身 .qi/models.json
    cfg, loaded = load_config(cwd=tmp_path)
    assert {"deepseek", "openai"} <= set(cfg.providers)      # 两层 provider 合并
    assert [str(p) for p in loaded] == [str(env_file), str(user_dir / "models.json")]


def test_models_json_default_keys_rejected(tmp_path, monkeypatch):
    """默认模型只属于 settings.json:models.json 里写了既不生效、也不静默。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "models.json").write_text(json.dumps({
        "defaultProvider": "deepseek",
        "defaultModel": "deepseek-chat",
        "providers": {"deepseek": {"models": [{"id": "deepseek-chat"}]}},
    }), encoding="utf-8")
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    monkeypatch.delenv("QI_AGENT_CONFIG", raising=False)

    cfg, _files = load_config(cwd=tmp_path)
    assert "deepseek" in cfg.providers                # provider 照常可用
    assert not hasattr(cfg, "defaultProvider")        # 但默认模型字段不存在了
    assert default_model_spec(tmp_path) == (None, None, "未配置")

    hits = legacy_default_keys(cwd=tmp_path)
    assert len(hits) == 1
    path, provider, model = hits[0]
    assert path == home / "models.json"
    assert (provider, model) == ("deepseek", "deepseek-chat")


def test_missing_default_model_error_carries_migration_hint(tmp_path, monkeypatch):
    """无默认模型时报错要给出可照抄的迁移命令(而不是只说"缺少")。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "models.json").write_text(json.dumps({
        "defaultProvider": "deepseek",
        "defaultModel": "deepseek-chat",
        "providers": {"deepseek": {"models": [{"id": "deepseek-chat"}]}},
    }), encoding="utf-8")
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    monkeypatch.delenv("QI_AGENT_CONFIG", raising=False)
    cfg, _files = load_config(cwd=tmp_path)

    with pytest.raises(ConfigError) as excinfo:
        resolve_default_model(cfg, tmp_path)
    message = str(excinfo.value)
    assert "settings.json" in message
    assert "qi config --set defaultProvider=deepseek" in message
    assert "--set defaultModel=deepseek-chat" in message


def test_resolve_value(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert resolve_value("$FOO") == "bar"
    assert resolve_value("${FOO}_X") == "bar_X"
    assert resolve_value("$$literal") == "$literal"
    assert resolve_value("$!bang") == "!bang"
    assert resolve_value("plain-literal") == "plain-literal"
    assert resolve_value("$MISSING_VAR_X") is None


def test_resolve_value_command_without_shell():
    """`!command` 按 shlex 拆参数以 shell=False 执行(无 shell 注入面)。"""
    assert resolve_value("!printf 'sk-from-cmd'") == "sk-from-cmd"
    assert resolve_value('!printf "a b"') == "a b"          # shlex 负责引号
    assert resolve_value("!sh -c 'exit 3'") is None          # 非零退出码视为未解析
    assert resolve_value("!printf '") is None                # 引号不配对
    assert resolve_value("!") is None                        # 空命令


def test_resolve_value_command_has_no_shell_pipeline():
    """刻意的边界:管道不当 shell 用。要 shell 得显式写 `!bash -lc "…"`。"""
    # `|` 作为普通参数传给 echo(无 shell 解释) → 原样输出
    assert resolve_value("!echo a | tr a b") == "a | tr a b"
    # 显式起 shell 才真的走管道
    assert resolve_value("!sh -c 'echo a | tr a b'") == "b"


def test_resolve_key_order(tmp_path, monkeypatch):
    store = AuthStore(path=tmp_path / "auth.json")
    # 约定 env
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-default-env")
    assert resolve_key("deepseek", None, store).source == "env:DEEPSEEK_API_KEY"
    # auth store 优先于约定 env
    store.set_key("deepseek", "from-store")
    assert resolve_key("deepseek", None, store).source == "auth"
    # 无 store/env 时使用 provider 的 apiKey 引用
    store.remove("deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("MY_DS", "from-config")
    assert resolve_key("deepseek", "$MY_DS", store).source.startswith("config:")
    # 引用未解析
    assert not resolve_key("deepseek", "$NOT_SET_ANYWHERE", store).ok
    # ollama 免 key
    assert resolve_key("ollama", None, store).ok


def test_auth_store_roundtrip_and_perms(tmp_path):
    store = AuthStore(path=tmp_path / "auth.json")
    store.set_key("deepseek", "sk-abc")
    assert store.get("deepseek") == "sk-abc"
    assert store.providers() == ["deepseek"]
    assert store.remove("deepseek")
    assert not store.remove("deepseek")
    assert store.providers() == []
