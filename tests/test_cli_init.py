"""qi init:models.json + auth.json(非交互 / 交互降级 / 上下键选择)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from qi_agent.cli import app
from qi_agent.presets import preset_names

runner = CliRunner()


def _new_provider_no(existing: int = 0) -> str:
    """非 TTY 编号输入下,「＋ 新建 provider」是第几项。

    选择器现在是 **已有 provider + 没物化的预置 + ＋ 新建**(新建永远排最后),
    所以编号 = 已有数 + 预置表大小 + 1 —— 写死会因为预置表加一家就红。
    """
    return str(existing + len(preset_names()) + 1)


def _run(tmp_path: Path, monkeypatch, *args: str):
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return runner.invoke(app, ["init", *args])


def _models(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "home" / "models.json").read_text(encoding="utf-8"))


def _settings(tmp_path: Path) -> dict:
    """默认模型属于 settings.json(对齐 pi),不再写进 models.json。"""
    path = tmp_path / "home" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def test_init_creates_provider_and_model(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, "-y", "--provider", "my-llm", "--model", "my-model",
               "--base-url", "http://localhost:1234/v1", "--api", "openai-completions",
               "--context-window", "64000", "--max-tokens", "8000", "--reasoning")
    assert res.exit_code == 0, res.output
    data = _models(tmp_path)
    assert "defaultProvider" not in data          # 已不再写进 models.json
    assert "defaultModel" not in data
    assert _settings(tmp_path) == {"defaultProvider": "my-llm", "defaultModel": "my-model"}
    prov = data["providers"]["my-llm"]
    assert prov["baseUrl"] == "http://localhost:1234/v1"
    assert prov["api"] == "openai-completions"
    entry = prov["models"][0]
    assert entry["id"] == "my-model"
    assert entry["contextWindow"] == 64000
    assert entry["maxTokens"] == 8000
    assert entry["reasoning"] is True


def test_init_defaults_for_new_model(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, "-y", "--provider", "ollama", "--model", "llama3.1:8b")
    assert res.exit_code == 0, res.output
    entry = _models(tmp_path)["providers"]["ollama"]["models"][0]
    assert entry["contextWindow"] == 128000
    assert entry["maxTokens"] == 16384
    assert entry["reasoning"] is False


def test_init_api_key_env_ref(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, "-y", "--provider", "deepseek",
               "--model", "deepseek-chat", "--api-key-env", "DS_KEY")
    assert res.exit_code == 0, res.output
    assert _models(tmp_path)["providers"]["deepseek"]["apiKey"] == "$DS_KEY"


def test_init_api_key_stored_in_auth_json(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, "-y", "--provider", "custom", "--model", "m",
               "--api-key", "sk-test-123")
    assert res.exit_code == 0, res.output
    auth = json.loads((tmp_path / "home" / "auth.json").read_text(encoding="utf-8"))
    assert auth["custom"] == {"type": "api_key", "key": "sk-test-123"}


def test_init_upserts_existing_model(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, "-y", "--provider", "p", "--model", "m",
         "--context-window", "1000")
    res = _run(tmp_path, monkeypatch, "-y", "--provider", "p", "--model", "m",
               "--context-window", "2000")
    assert res.exit_code == 0, res.output
    models = _models(tmp_path)["providers"]["p"]["models"]
    assert len(models) == 1
    assert models[0]["contextWindow"] == 2000


def test_init_rejects_unknown_api(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, "-y", "--provider", "p", "--model", "m",
               "--api", "bogus-api")
    assert res.exit_code == 2


# ── 交互流程(CliRunner 非 TTY,退回编号输入) ──────────────

# 新建 provider + 新建模型 + 激活 LLM
INTERACTIVE_INPUT = "\n".join([
    _new_provider_no(),       # 选择 provider:新建(排在 已有+预置 之后)
    "my-llm",                 # Provider name
    "http://localhost:1234/v1",   # Base URL
    "1",                      # API 类型: openai-completions
    "sk-test-9",             # API key(可见、必填)
    "1",                      # Add a model? 是
    "my-model",               # Model identifier
    "",                       # Model display name(默认=id)
    "",                       # Supports reasoning(默认否)
    "",                       # contextWindow(默认)
    "",                       # maxTokens(默认)
    "",                       # Add a model? 否
    "",                       # Configure another provider? 否
    "1",                      # Select provider for LLM
    "1",                      # Select LLM model
]) + "\n"


def test_init_interactive_creates_default(tmp_path, monkeypatch):
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["init"], input=INTERACTIVE_INPUT)
    assert res.exit_code == 0, res.output
    data = _models(tmp_path)
    assert ("defaultProvider" not in data) and ("defaultModel" not in data)
    assert _settings(tmp_path) == {"defaultProvider": "my-llm", "defaultModel": "my-model"}
    assert data["providers"]["my-llm"]["baseUrl"] == "http://localhost:1234/v1"
    assert data["providers"]["my-llm"]["api"] == "openai-completions"
    entry = data["providers"]["my-llm"]["models"][0]
    assert entry["id"] == "my-model"
    assert entry["name"] == "my-model"
    assert entry["contextWindow"] == 128000
    assert entry["maxTokens"] == 16384
    assert entry["reasoning"] is False
    auth = json.loads((tmp_path / "home" / "auth.json").read_text(encoding="utf-8"))
    assert auth["my-llm"]["key"] == "sk-test-9"
    assert "Initialization complete" in res.output


def test_init_interactive_continue_second_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    inp = "\n".join([
        # provider 1
        _new_provider_no(), "p1", "http://a/v1", "1", "key1",
        "1", "m1", "", "", "", "",   # add model + id + display/reasoning/ctx/max
        "",                              # Add a model? 否
        "1",                             # Configure another provider? 是
        # provider 2
        _new_provider_no(1), "p2", "http://b/v1", "1", "key2",
        "1", "m2", "", "", "", "",
        "",                              # Add a model? 否
        "",                              # Configure another provider? 否
        # activate
        "2",                             # provider p2
        "1",                             # model m2
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=inp)
    assert res.exit_code == 0, res.output
    data = _models(tmp_path)
    assert set(data["providers"]) == {"p1", "p2"}
    assert _settings(tmp_path) == {"defaultProvider": "p2", "defaultModel": "m2"}


def _first_run_input() -> str:
    return "\n".join([
        _new_provider_no(), "p", "http://a/v1", "1", "orig-key",
        "1", "m1", "", "", "", "",   # add model + id + display/reasoning/ctx/max
        "",     # Add a model? 否
        "",     # Configure another provider? 否
        "1", "1",  # activate: provider, model
    ]) + "\n"


def test_init_second_run_keeps_key_and_baseurl(tmp_path, monkeypatch):
    """二次 init:选已有 provider 时保留凭证与 baseUrl,可改默认模型。"""
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"], input=_first_run_input()).exit_code == 0
    # 二次:选已有 provider → BaseURL 回车保留 → api 回车保留 → key 回车保留
    second = "\n".join([
        "1",    # 选择 provider(p)
        "",     # Base URL(回车保留)
        "",     # API 类型(回车保留)
        "",     # API key(回车保留)
        "",     # Add a model? 否(已有模型)
        "",     # Configure another provider? 否
        "",     # Select provider for LLM(默认 p)
        "",     # Select LLM model(默认 m1)
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=second)
    assert res.exit_code == 0, res.output
    data = _models(tmp_path)
    assert data["providers"]["p"]["baseUrl"] == "http://a/v1"
    assert len(data["providers"]["p"]["models"]) == 1
    auth = json.loads((tmp_path / "home" / "auth.json").read_text(encoding="utf-8"))
    assert auth["p"]["key"] == "orig-key"


def test_init_second_run_updates_key(tmp_path, monkeypatch):
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"], input=_first_run_input()).exit_code == 0
    second = "\n".join([
        "1",        # 已有 provider
        "",         # Base URL 保留
        "",         # API 类型保留
        "new-key",  # API key 更新
        "",         # Add a model? 否
        "",         # another provider? 否
        "", "",     # activate
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=second)
    assert res.exit_code == 0, res.output
    auth = json.loads((tmp_path / "home" / "auth.json").read_text(encoding="utf-8"))
    assert auth["p"]["key"] == "new-key"


def test_select_arrow_keys(monkeypatch):
    from qi_agent import prompt as p

    monkeypatch.setattr(p, "_is_tty", lambda: True)
    keys = iter(["\x1b[B", "\x1b[B", "\r"])
    monkeypatch.setattr(p, "_read_key", lambda: next(keys))
    assert p.select("pick", ["a", "b", "c"], default=0) == 2

    keys = iter(["\x1b[A", "\r"])  # 上键从 0 回绕到末尾
    monkeypatch.setattr(p, "_read_key", lambda: next(keys))
    assert p.select("pick", ["a", "b", "c"], default=0) == 2


# ── Models 菜单:和「＋ Add a model」同级的「↻ Refresh model list」 ──────


def test_init_interactive_refresh_model_list(tmp_path, monkeypatch):
    """交互流里就地「刷新模型列表」= `qi init --refresh` 那条:GET /models,只加不删。"""
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("qi_agent.model_catalog.fetch_model_ids",
                        lambda base_url, api_key, timeout=None: ["m1", "m-new"])
    inp = "\n".join([
        _new_provider_no(), "p1", "http://a/v1", "1", "key1",
        "2",     # Models 菜单 → Refresh(还没模型,菜单默认在「＋ Add a model」)
        "",      # 回菜单:有模型了 → 默认「✓ Done」
        "",      # Configure another provider? 否
        "", "",  # activate:provider / model
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=inp)
    assert res.exit_code == 0, res.output
    assert "Refresh model list" in res.output            # 菜单项和 Add 同级出现
    assert "接口返回 2 个模型;新增 2" in res.output
    models = _models(tmp_path)["providers"]["p1"]["models"]
    assert [m["id"] for m in models] == ["m1", "m-new"]
    assert _settings(tmp_path) == {"defaultProvider": "p1", "defaultModel": "m1"}


def test_init_interactive_refresh_failure_keeps_the_flow(tmp_path, monkeypatch):
    """拉取失败只报错不中断:回到菜单还能手动加模型、走完 init。"""
    from qi_agent.model_catalog import CatalogError

    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    def boom(base_url, api_key, timeout=None):
        raise CatalogError("connection refused")

    monkeypatch.setattr("qi_agent.model_catalog.fetch_model_ids", boom)
    inp = "\n".join([
        _new_provider_no(), "p1", "http://a/v1", "1", "key1",
        "2",     # Refresh → 拉取失败,回菜单(仍没模型 → 默认「添加」)
        "",      # ＋ Add a model
        "m1", "", "", "", "",   # id / display / reasoning / ctx / max
        "",      # Done
        "",      # Configure another provider? 否
        "", "",  # activate
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=inp)
    assert res.exit_code == 0, res.output
    assert "拉取失败" in res.output and "connection refused" in res.output
    models = _models(tmp_path)["providers"]["p1"]["models"]
    assert [m["id"] for m in models] == ["m1"]


# ── 预置 provider:选择列表里必须看得见 ──────────────────────


def test_init_interactive_lists_and_materializes_presets(tmp_path, monkeypatch):
    """零配置跑 `qi init`,选择器要列出**全部**预置(标 [预置]);选中即物化。

    用户反馈:「qi init 没有显示预置的 provider」—— 预置只藏在 `--list-presets`
    后面的话,新用户在交互流里就只剩手抄 baseUrl 一条路。
    """
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    inp = "\n".join([
        str(preset_names().index("deepseek") + 1),   # 选 deepseek [预置]
        "",     # Base URL 回车保留(预置的)
        "",     # API 类型 回车(预置的)
        "",     # API key 回车跳过(用 $DEEPSEEK_API_KEY)
        "",     # Add a model? 否(预置已带模型)
        "",     # Configure another provider? 否
        "",     # Select provider for LLM(只有它)
        "",     # Select LLM model(第一个)
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=inp)
    assert res.exit_code == 0, res.output
    for name in preset_names():                     # 每家预置都要在列表里
        assert f"{name} [预置]" in res.output, name
    assert "已套用预置 deepseek" in res.output

    data = _models(tmp_path)
    assert set(data["providers"]) == {"deepseek"}          # 只物化选中的那家
    prov = data["providers"]["deepseek"]
    assert prov["baseUrl"] == "https://api.deepseek.com"
    assert prov["apiKey"] == "$DEEPSEEK_API_KEY"
    assert [m["id"] for m in prov["models"]] == ["deepseek-flash", "deepseek-v4-pro"]
    assert _settings(tmp_path) == {"defaultProvider": "deepseek",
                                   "defaultModel": "deepseek-flash"}
    auth_path = tmp_path / "home" / "auth.json"     # 没粘 key → 不该有 deepseek 的凭证
    assert not auth_path.exists() or "deepseek" not in json.loads(
        auth_path.read_text(encoding="utf-8"))


def test_init_yes_provider_matching_preset_materializes_it(tmp_path, monkeypatch):
    """`-y --provider <预置名>` 同样先物化 —— 否则写出的 provider 没有 baseUrl,是坏的。"""
    res = _run(tmp_path, monkeypatch, "-y", "--provider", "deepseek",
               "--model", "deepseek-flash")
    assert res.exit_code == 0, res.output
    prov = _models(tmp_path)["providers"]["deepseek"]
    assert prov["baseUrl"] == "https://api.deepseek.com"
    assert prov["apiKey"] == "$DEEPSEEK_API_KEY"
    assert {"deepseek-flash", "deepseek-v4-pro"} <= {m["id"] for m in prov["models"]}
    assert _settings(tmp_path)["defaultModel"] == "deepseek-flash"
