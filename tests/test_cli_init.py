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


def test_read_key_decodes_windows_scan_codes(monkeypatch):
    """Windows 方向键是 E0 H / E0 P;解码后要等于 POSIX 的 ESC[A / ESC[B。"""
    import builtins
    import sys
    import types

    from qi_agent import prompt as p

    fake_msvcrt = types.ModuleType("msvcrt")
    keys = iter(["\xe0", "H", "\xe0", "P"])
    fake_msvcrt.getwch = lambda: next(keys)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("termios", "tty"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(p.sys, "stdin", types.SimpleNamespace(fileno=lambda: 0))

    assert p._read_key() == "\x1b[A"
    assert p._read_key() == "\x1b[B"


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
        # 预置**不再问** API 类型(from_preset 只打印默认值,见 _configure_provider)
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

    data = _models(tmp_path)
    assert set(data["providers"]) == {"deepseek"}          # 只物化选中的那家
    prov = data["providers"]["deepseek"]
    assert "baseUrl" not in prov                   # 端点展开写在每个模型上
    assert prov["apiKey"] == "$DEEPSEEK_API_KEY"
    assert all(m["baseUrl"] == "https://api.deepseek.com" for m in prov["models"])
    assert [m["id"] for m in prov["models"]] == ["deepseek-flash", "deepseek-v4-pro"]
    assert _settings(tmp_path) == {"defaultProvider": "deepseek",
                                   "defaultModel": "deepseek-flash"}
    auth_path = tmp_path / "home" / "auth.json"     # 没粘 key → 不该有 deepseek 的凭证
    assert not auth_path.exists() or "deepseek" not in json.loads(
        auth_path.read_text(encoding="utf-8"))


def test_init_remove_deletes_a_provider_and_its_default(tmp_path, monkeypatch):
    """`qi init --remove <名>`:删 models.json 里的 provider 定义,默认模型指着它时一并清掉;
    密钥、以及指向它的默认模型一起删(用户反馈:没有清除机制;又说别分两步)。"""
    from qi_agent.auth import AuthStore

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    AuthStore().set_key("zai", "sk-zai")
    runner.invoke(app, ["init", "--preset", "zai"])
    assert "zai" in json.loads((home / "models.json").read_text(encoding="utf-8"))["providers"]

    # 别名(zhipu)也认;定义 + 密钥一次删干净
    res = runner.invoke(app, ["init", "--remove", "zhipu"])
    assert res.exit_code == 0, res.output
    assert "zai" not in json.loads((home / "models.json").read_text(encoding="utf-8"))["providers"]
    assert "zai" not in json.loads((home / "auth.json").read_text(encoding="utf-8"))
    # 默认模型指向它 → 已清掉,不留“默认模型不存在”的坑
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert "defaultProvider" not in settings and "defaultModel" not in settings




def test_init_interactive_can_skip_provider_config(tmp_path, monkeypatch):
    """已经配好 provider 时,选择器里有「✓ 下一步」—— 只想改默认模型的人不必再走一遍
    provider 向导(改名 `选择 provider` → `配置 provider`,因为这一步就是"要不要改配置")。"""
    from qi_agent.auth import AuthStore

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    AuthStore().set_key("zai", "sk-zai")
    runner.invoke(app, ["init", "--preset", "zai"])
    (home / "settings.json").write_text("{}", encoding="utf-8")     # 先把默认模型清掉

    inp = "\n".join([
        "✓ 下一步",
        "",     # Activate:provider
        "",     # Activate:model
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=inp)
    assert res.exit_code == 0, res.output
    assert "配置 provider" in res.output and "选择 provider" not in res.output
    assert "跳过 provider 配置" in res.output
    # provider 定义没被动过,只写了默认模型
    assert list(json.loads((home / "models.json").read_text(encoding="utf-8"))["providers"]) == ["zai"]
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == {
        "defaultProvider": "zai", "defaultModel": "glm-5.2"}


def test_init_interactive_has_no_skip_option_without_providers(tmp_path, monkeypatch):
    """零配置时不该出现「下一步」(没东西可跳过)—— 否则会直接走到“没有可用模型”报错。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["init", "--list-presets"])
    assert res.exit_code == 0
    # 交互流程:只有预置 + 新建,没有 下一步 / 删除
    inp = "\n".join(["＋ 新建 provider", "my-llm", "http://localhost:1234/v1", "",
                      "1", "m1", "", "", "", "", "", "", "", ""]) + "\n"
    out = runner.invoke(app, ["init"], input=inp).output
    assert "下一步" not in out and "删除 provider" not in out


def test_init_interactive_can_delete_a_provider(tmp_path, monkeypatch):
    """交互流里的「－ 删除 provider」:与 `qi init --remove` 同一条逻辑 ——
    用户反馈:命令行有 --remove,交互式却没有。"""
    from qi_agent.auth import AuthStore

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    AuthStore().set_key("zai", "sk-zai")
    runner.invoke(app, ["init", "--preset", "zai"])
    runner.invoke(app, ["init", "--preset", "deepseek"])
    assert set(json.loads((home / "models.json").read_text(encoding="utf-8"))["providers"]) == {
        "zai", "deepseek"}

    inp = "\n".join([
        "－ 删除 provider",   # 选择器里的删除入口
        "2",    # 删哪个:sorted(providers) = deepseek / zai → zai
        "1",    # 确认删除:是(一步到位:模型清单 + 密钥 + 默认模型)
        "deepseek [✓]",   # 回到选择器,选 deepseek 继续
        "",     # API key 回车保留
        "",     # Models: Done
        "",     # Configure another provider? 否
        "",     # Activate:provider(只有 deepseek)
        "",     # Activate:model(第一个)
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=inp)
    assert res.exit_code == 0, res.output
    assert "已删除 1 个 provider" in res.output
    assert set(json.loads((home / "models.json").read_text(encoding="utf-8"))["providers"]) == {
        "deepseek"}
    # 一步全删:密钥也不留
    assert "zai" not in json.loads((home / "auth.json").read_text(encoding="utf-8"))


def test_init_remove_refuses_providers_that_only_come_from_presets(tmp_path, monkeypatch):
    """只由预置补出来的 provider 不在文件里 —— 删它要说清楚,而不是静默成功。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("QI_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["init", "--remove", "qwen"])
    assert res.exit_code == 1
    assert "没找到定义" in res.output and "预置" in res.output


def test_provider_marks_say_what_is_missing(tmp_path, monkeypatch):
    """`[✓]` / `[✗ …]` = 这家的模型**现在能不能跑**:端点 + 模型 + 密钥,缺什么写什么。

    用户反馈:预置物化后端点写在模型上(provider 段只有 apiKey),旧判定只认
    provider 级 baseUrl,于是**全都显示 ✗**,看不出哪家真能用。
    """
    from qi_agent.auth import AuthStore
    from qi_agent.cli import _provider_mark
    from qi_agent.paths import QI_AGENT_HOME as HOME_ENV

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    no_models = {"baseUrl": "https://x/v1", "models": []}
    assert _provider_mark("p", no_models) == "✗ 没有模型"
    no_endpoint = {"models": [{"id": "m"}]}
    assert _provider_mark("p", no_endpoint) == "✗ 没有端点"
    endpoint_on_model = {"apiKey": "$DEEPSEEK_API_KEY", "models": [{"id": "m", "baseUrl": "https://x/v1"}]}
    assert _provider_mark("deepseek", endpoint_on_model) == "✗ 缺密钥"   # 端点够、key 缺
    AuthStore().set_key("deepseek", "sk-x")
    assert _provider_mark("deepseek", endpoint_on_model) == "✓"


def test_init_provider_picker_defaults_to_the_first_item(tmp_path, monkeypatch):
    """零配置的第一问:回车 = 第一项(deepseek),不是最后一项「＋ 新建 provider」。

    用户反馈:指针默认停在最后一个,想选国产预置得一路往回按。
    """
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    inp = "\n".join([
        "",     # 选择 provider:直接回车 = 第一项(deepseek 预置)
        "",     # Base URL 回车保留(预置的)
        "",     # API key 回车跳过(用 $DEEPSEEK_API_KEY)
        "",     # Add a model? 否(预置已带模型)
        "",     # Configure another provider? 否
        "",     # Select provider for LLM(只有它)
        "",     # Select LLM model(第一个)
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=inp)
    assert res.exit_code == 0, res.output
    # 列表顺序:国产在前,deepseek 打头
    lines = [ln.strip() for ln in res.output.splitlines()
             if ln.strip().endswith("[预置]")]
    assert lines[0].endswith("deepseek [预置]")
    assert lines[1].endswith("zai [预置]")
    assert lines[-1].endswith("opencode-go [预置]")
    assert "+ 新建 provider" in res.output or "＋ 新建 provider" in res.output
    assert set(_models(tmp_path)["providers"]) == {"deepseek"}


def test_init_yes_provider_matching_preset_materializes_it(tmp_path, monkeypatch):
    """`-y --provider <预置名>` 同样先物化 —— 否则写出的 provider 没有 baseUrl,是坏的。"""
    res = _run(tmp_path, monkeypatch, "-y", "--provider", "deepseek",
               "--model", "deepseek-flash")
    assert res.exit_code == 0, res.output
    prov = _models(tmp_path)["providers"]["deepseek"]
    assert "baseUrl" not in prov
    assert prov["apiKey"] == "$DEEPSEEK_API_KEY"
    assert all(m["baseUrl"] == "https://api.deepseek.com" for m in prov["models"])
    assert {"deepseek-flash", "deepseek-v4-pro"} <= {m["id"] for m in prov["models"]}
    assert _settings(tmp_path)["defaultModel"] == "deepseek-flash"


# ── OpenCode 预置:混合 API 形态的 provider ───────────────────────────


def test_init_interactive_opencode_materializes_mixed_api_models(tmp_path, monkeypatch):
    """物化 opencode:不问 Base URL / API 类型(端点与形态都写在模型上),原样落盘。"""
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    inp = "\n".join([
        str(preset_names().index("opencode") + 1),   # 选 opencode [预置]
        "",     # API key 回车跳过(用 $OPENCODE_API_KEY)
        "",     # Models: Done(预置已带模型)
        "",     # Configure another provider? 否
        "",     # Select provider for LLM
        "",     # Select LLM model(默认第一个)
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=inp)
    assert res.exit_code == 0, res.output
    # provider 段不带 api/baseUrl(端点随 API 形态变后缀,一个 URL 表达不了)
    prov = _models(tmp_path)["providers"]["opencode"]
    assert "baseUrl" not in prov and "api" not in prov
    assert prov["apiKey"] == "$OPENCODE_API_KEY"
    by_id = {m["id"]: m for m in prov["models"]}
    assert by_id["gpt-5.6-sol"]["api"] == "openai-responses"
    assert by_id["gpt-5.6-sol"]["baseUrl"] == "https://opencode.ai/inference/v1"
    assert by_id["claude-sonnet-5-5"]["api"] == "anthropic-messages"
    assert by_id["claude-sonnet-5-5"]["baseUrl"] == "https://opencode.ai/inference"
    assert by_id["deepseek-v4.1-flash"]["api"] == "openai-completions"
    assert by_id["deepseek-v4.1-flash"]["baseUrl"] == "https://opencode.ai/inference/v1"
    assert _settings(tmp_path) == {"defaultProvider": "opencode",
                                   "defaultModel": "gpt-5.6-sol"}
    # 列表里能看见模型级覆盖(否则用户看不出这些模型走的不是同一个 API)
    assert "api: openai-responses" in res.output
    assert "api: anthropic-messages" in res.output


def test_adding_a_model_to_a_mixed_provider_asks_api_and_base_url(tmp_path, monkeypatch):
    """混合形态 provider 才追问 API 类型,且 api 一变就**成对**追问 baseUrl
    (mimo anthropic / opencode 的 `/inference` 形态都是成对的);单形态 provider 不问。"""
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    inp = "\n".join([
        str(preset_names().index("opencode") + 1),   # 选 opencode [预置]
        "",     # API key 回车跳过
        "1",    # Models: ＋ Add a model
        "claude-opus-5",   # id
        "",     # display name(默认=id)
        "",     # reasoning(默认否)
        "",     # contextWindow(默认)
        "",     # maxTokens(默认)
        "3",    # API 类型 → anthropic-messages(SUPPORTED_APIS 第三个)
        "https://opencode.ai/inference",   # 该模型自己的 Base URL(端点成对地换)
        "",     # Models: Done
        "",     # Configure another provider? 否
        "", "", # activate:provider / model(默认)
    ]) + "\n"
    res = runner.invoke(app, ["init"], input=inp)
    assert res.exit_code == 0, res.output
    models = _models(tmp_path)["providers"]["opencode"]["models"]
    added = next(m for m in models if m["id"] == "claude-opus-5")
    assert added["api"] == "anthropic-messages"
    assert added["baseUrl"] == "https://opencode.ai/inference"
