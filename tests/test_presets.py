"""预置 provider(`qi init --preset …` / `qi init --list-presets`)。

要点三条:

* 表本身**自洽**(provider/api/baseUrl/env/models 都在且认得),并与 `auth.DEFAULT_API_KEY_ENV`
  一一对应 —— 两处各写一份清单,靠测试锁住;
* 合并**不覆盖**用户写过的东西(再跑一次 preset 不该把自建代理 / 手调的 reasoning 洗掉);
* 物化出来的 `models.json` 真的能被 `resolve_model` 解析(不是只写了个好看的文件)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.auth import DEFAULT_API_KEY_ENV  # noqa: E402
from qi_agent.config import SUPPORTED_APIS, load_config, resolve_model  # noqa: E402
from qi_agent.presets import (
    PRESETS,
    apply_presets,
    get_preset,
    model_entry,
    preset_names,
)  # noqa: E402

runner = CliRunner()


# ── 表自洽 ──────────────────────────────────────────────


def test_every_preset_is_well_formed():
    assert PRESETS, "预置表不该是空的"
    for name, preset in PRESETS.items():
        assert preset.provider == name, "键与 provider 名要一致(`--preset <键>` 就是它)"
        assert preset.label and preset.base_url.startswith(("http://", "https://"))
        assert preset.api_key_env.endswith("_API_KEY")
        assert preset.api in SUPPORTED_APIS, preset.api
        assert preset.models, f"{name} 没有模型"
        ids = [model.id for model in preset.models]
        assert len(ids) == len(set(ids)), f"{name} 的模型 id 重复"
        assert all(model_id.strip() for model_id in ids)
        # 两个数都必须有、且合理:上下文窗口 > 0、最大输出 > 0、输出不超过窗口
        for model in preset.models:
            assert model.context_window > 0, f"{name}/{model.id} 缺 contextWindow"
            assert model.max_output > 0, f"{name}/{model.id} 缺 maxTokens"
            assert model.max_output <= model.context_window, \
                f"{name}/{model.id} 的最大输出比上下文窗口还大" 


def test_preset_env_names_match_the_auth_map():
    """provider → 环境变量 只写一份口径(两处各写一份,靠这条锁住)。"""
    for preset in PRESETS.values():
        assert DEFAULT_API_KEY_ENV.get(preset.provider) == preset.api_key_env, \
            f"{preset.provider} 的约定环境变量两边不一致"


def test_get_preset_is_case_insensitive_and_unknown_is_none():
    assert get_preset("DeepSeek") is PRESETS["deepseek"]
    assert get_preset("  moonSHOT ") is PRESETS["moonshot"]
    assert get_preset("不存在") is None
    assert get_preset("") is None
    assert "deepseek" in preset_names()


# ── 合并语义 ────────────────────────────────────────────


def test_apply_presets_writes_provider_and_models():
    data: dict = {}
    _data, changed = apply_presets(data, ["deepseek"])
    entry = data["providers"]["deepseek"]
    assert entry["baseUrl"] == PRESETS["deepseek"].base_url
    assert entry["apiKey"] == "$DEEPSEEK_API_KEY"
    # 期望值从表里取(种子换了不该让测试跟着改常数)
    assert [m["id"] for m in entry["models"]] == \
        [model.id for model in PRESETS["deepseek"].models]
    assert entry["models"][0]["contextWindow"] == 1_000_000
    # maxTokens 就是模型的最大输出(pi 同名字段的语义),预置里必须有
    assert entry["models"][0]["maxTokens"] == 393_216
    assert any("deepseek-v4-pro" in item for item in changed)


def test_apply_presets_never_clobbers_what_the_user_wrote():
    """已有值不动:自建代理的 baseUrl、自备的 key 来源、手调过的模型元数据。"""
    data = {
        "providers": {
            "deepseek": {
                "baseUrl": "https://my-proxy.internal/v1",
                "apiKey": "!op read 'op://vault/deepseek'",
                "models": [{"id": "deepseek-v4-pro", "reasoning": True, "maxTokens": 4096}],
            },
        },
    }
    _data, changed = apply_presets(data, ["deepseek"])
    entry = data["providers"]["deepseek"]
    assert entry["baseUrl"] == "https://my-proxy.internal/v1"     # 没被洗掉
    assert entry["apiKey"] == "!op read 'op://vault/deepseek'"
    kept = next(m for m in entry["models"] if m["id"] == "deepseek-v4-pro")
    assert kept == {"id": "deepseek-v4-pro", "reasoning": True, "maxTokens": 4096}
    seed_ids = [model.id for model in PRESETS["deepseek"].models]
    assert [m["id"] for m in entry["models"]] == ["deepseek-v4-pro", *[
        model_id for model_id in seed_ids if model_id != "deepseek-v4-pro"]]
    # 只补了缺的:api 段 + 种子里的新模型(用户已写的 deepseek-v4-pro 没动)
    assert "deepseek.api" in changed
    missing_seed = next(m for m in seed_ids if m != "deepseek-v4-pro")
    assert f"deepseek/{missing_seed}" in changed
    assert not any("baseUrl" in item or "apiKey" in item for item in changed)


def test_apply_presets_is_idempotent():
    data: dict = {}
    apply_presets(data, ["moonshot"])
    before = json.dumps(data, sort_keys=True)
    _data, changed = apply_presets(data, ["moonshot"])
    assert changed == []
    assert json.dumps(data, sort_keys=True) == before


def test_apply_presets_unknown_name_raises():
    with pytest.raises(KeyError):
        apply_presets({}, ["nope"])


# ── CLI ─────────────────────────────────────────────────


def _cli_env(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.chdir(tmp_path)
    return home


def test_init_list_presets_prints_the_table(tmp_path, monkeypatch):
    from qi_agent.cli import app

    _cli_env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["init", "--list-presets"])
    assert res.exit_code == 0, res.output
    for name in preset_names():
        assert name in res.output
    assert "qi init --preset" in res.output


def test_init_preset_writes_models_and_default_model(tmp_path, monkeypatch):
    from qi_agent.cli import app

    home = _cli_env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["init", "--preset", "deepseek,moonshot"])
    assert res.exit_code == 0, res.output

    data = json.loads((home / "models.json").read_text(encoding="utf-8"))
    assert sorted(data["providers"]) == ["deepseek", "moonshot"]

    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert (settings["defaultProvider"], settings["defaultModel"]) == \
        ("deepseek", PRESETS["deepseek"].default_model)

    # 写出来的东西真能解析(不是只写了个好看的文件)
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(home / "models.json"))
    cfg, _files = load_config(tmp_path)
    resolved = resolve_model(cfg, "moonshot", "kimi-k3")
    assert resolved.base_url == PRESETS["moonshot"].base_url
    assert resolved.context_window == 1_048_576
    assert resolved.api_key_ref == "$MOONSHOT_API_KEY"


def test_init_preset_is_idempotent_and_unknown_errors(tmp_path, monkeypatch):
    from qi_agent.cli import app

    home = _cli_env(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init", "--preset", "zhipu"]).exit_code == 0
    first = (home / "models.json").read_text(encoding="utf-8")

    res = runner.invoke(app, ["init", "--preset", "zhipu"])
    assert res.exit_code == 0 and "无改动" in res.output
    assert (home / "models.json").read_text(encoding="utf-8") == first

    res = runner.invoke(app, ["init", "--preset", "不存在的预置"])
    assert res.exit_code == 2
    assert "未知预置" in res.output


def test_init_preset_local_writes_project_file(tmp_path, monkeypatch):
    from qi_agent.cli import app

    _cli_env(tmp_path, monkeypatch)
    (tmp_path / ".git").mkdir(exist_ok=True)          # 项目根 = 有 .git 的那层
    res = runner.invoke(app, ["init", "--preset", "siliconflow", "--local"])
    assert res.exit_code == 0, res.output
    project_models = tmp_path / ".qi" / "models.json"
    assert project_models.is_file()
    assert "siliconflow" in project_models.read_text(encoding="utf-8")


# ── 兜底:models.json 里没写也能用(这才是“预置”的意义)──────────


def test_presets_are_a_fallback_for_unconfigured_providers(tmp_path, monkeypatch):
    """`models.json` 里没写的预置 provider 自动可用 —— `/login` 与直接跑都据此工作。"""
    home = _cli_env(tmp_path, monkeypatch)
    (home / "models.json").write_text(
        '{"providers": {"my-proxy": {"baseUrl": "https://proxy.internal/v1",'
        ' "models": [{"id": "p1"}]}}}', encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(home / "models.json"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    cfg, _files = load_config(tmp_path)
    assert "my-proxy" in cfg.providers                      # 用户的照旧
    assert "deepseek" in cfg.providers                      # 预置兜底补上
    assert cfg.presetProviders == set(PRESETS)              # 全都没写过 → 全算预置
    assert "my-proxy" not in cfg.presetProviders            # 用户写的不算预置


def test_user_provider_definition_always_wins(tmp_path, monkeypatch):
    """同名 provider 用户写了就用用户的(baseUrl / api / 模型清单全以他为准)。"""
    home = _cli_env(tmp_path, monkeypatch)
    (home / "models.json").write_text(json.dumps({"providers": {"deepseek": {
        "baseUrl": "https://my-proxy.internal/v1",
        "apiKey": "sk-mine",
        "models": [{"id": "my-own-model", "contextWindow": 4096}],
    }}}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(home / "models.json"))

    cfg, _files = load_config(tmp_path)
    prov = cfg.providers["deepseek"]
    assert prov.baseUrl == "https://my-proxy.internal/v1"
    assert prov.apiKey == "sk-mine"
    assert [m.id for m in prov.models] == ["my-own-model"]   # 预置的模型没被塞进来
    assert "deepseek" not in cfg.presetProviders
    assert "moonshot" in cfg.presetProviders                 # 别的仍然是预置


def test_materializing_a_preset_removes_it_from_the_fallback(tmp_path, monkeypatch):
    """`qi init --preset` 物化之后,该 provider 就由 models.json 说了算(不再是“预置”)。"""
    from qi_agent.cli import app

    home = _cli_env(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init", "--preset", "deepseek"]).exit_code == 0
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(home / "models.json"))

    cfg, _files = load_config(tmp_path)
    assert "deepseek" not in cfg.presetProviders
    assert [m.id for m in cfg.providers["deepseek"].models] == \
        [model.id for model in PRESETS["deepseek"].models]   # 与预置一致(物化出来的)


@pytest.mark.asyncio
async def test_login_picker_marks_preset_providers(tmp_path, monkeypatch):
    """预置 provider 在 `/login` 里带 `(无凭证 · 预置)` 标记,且真的能登录。"""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_tui_style import MODEL, PALETTE, FakeRuntime, _tui_env  # noqa: E402
    from textual.widgets import Input  # noqa: E402

    from qi_agent import tui as tui_mod  # noqa: E402
    from qi_agent.auth import AuthStore  # noqa: E402

    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = tui_mod.QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        app._rt.cfg = load_config(tmp_path)[0]        # 真配置:预置兜底补好的那份
        app._command("/login")
        await pilot.pause(0.1)
        picker = app.screen
        assert isinstance(picker, tui_mod.PickerScreen)
        labels = dict(picker._options)
        assert "deepseek" in labels and "moonshot" in labels      # 预置的也在
        assert "预置" in labels["deepseek"], labels
        await pilot.press("escape")
        await pilot.pause(0.1)

        # `--login deepseek` 直奔输入框,存下来的 key 立刻能用(兜底提供了 baseUrl/模型)
        app._command("/login deepseek")
        await pilot.pause(0.1)
        app.screen.query_one("#prompt-input", Input).value = "sk-preset"
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert AuthStore().get("deepseek") == "sk-preset"


@pytest.mark.asyncio
async def test_model_selector_hides_presets_you_have_not_logged_into(tmp_path, monkeypatch):
    """`/model`(以及 ctrl+l / ctrl+p / `/scoped-models`)只列**能用**的。

    用户的原话:“别的 provider 我还没登陆,怎么 /model 切换时就可以选择呢?” ——
    预置那批只在解析得出凭证时才进 `/model`;没登录的想登录就去 `/login`(那里列全部)。
    `models.json` 里**显式写过**的 provider 不受这条限制(那是用户自己的配置)。
    """
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_tui_style import MODEL, PALETTE, FakeRuntime, _tui_env  # noqa: E402

    from qi_agent import tui as tui_mod  # noqa: E402
    from qi_agent.auth import AuthStore  # noqa: E402

    monkeypatch.chdir(tmp_path)
    _tui_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tui_mod, "QiRuntime", FakeRuntime)
    monkeypatch.setattr(tui_mod, "resolve_default_model", lambda cfg, cwd=None: MODEL)

    app = tui_mod.QiTui(palette=PALETTE)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app._rt is not None
        # `_tui_env` 写的 models.json 里只有 ollama(显式配置 → 不受凭证门槛限制)
        app._rt.cfg = load_config(tmp_path)[0]
        providers = {provider for provider, _, _ in app._model_options()}
        assert providers == {"ollama"}                # 预置那批一个都没登录 → 一个都不列

        AuthStore().set_key("deepseek", "sk-x")
        providers = {provider for provider, _, _ in app._model_options()}
        assert providers == {"ollama", "deepseek"}    # 登录了谁就多列谁

        # 显式写在 models.json 里的 provider 不设门槛(用户自己的配置,可能正在配)
        (tmp_path / "models.json").write_text(
            '{"providers": {"my-proxy": {"baseUrl": "https://p.internal/v1",'
            ' "models": [{"id": "p1"}]}}}', encoding="utf-8")
        monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
        app._rt.cfg = load_config(tmp_path)[0]
        providers = {provider for provider, _, _ in app._model_options()}
        assert providers == {"deepseek", "my-proxy"}

        # `/model` 用的就是同一份清单
        app._command("/model")
        await pilot.pause(0.1)
        selector = app.screen
        assert isinstance(selector, tui_mod.ModelSelector)
        shown = selector.rendered_text().plain
        assert "my-proxy" in shown and "deepseek" in shown and "kimi" not in shown
        assert "ollama" not in shown
        await pilot.press("escape")
        await pilot.pause(0.1)


def test_mimo_preset_is_present():
    """小米 MiMo:按量付费端点 + 约定环境变量(`MIMO_API_KEY`)。"""
    preset = PRESETS["mimo"]
    assert preset.base_url == "https://api.xiaomimimo.com/v1"
    assert preset.api_key_env == "MIMO_API_KEY"
    assert DEFAULT_API_KEY_ENV["mimo"] == "MIMO_API_KEY"
    assert [m.id for m in preset.models] == ["mimo-v2.5-pro", "mimo-v2.5"]
    assert preset.models[0].context_window == 1_048_576
    assert preset.models[0].max_output == 131_072
    assert "token-plan-cn.xiaomimimo.com" in preset.note


def test_every_preset_model_carries_both_numbers():
    """每个预置模型都带 `contextWindow` 与 `maxTokens`(用户要求:预置模型要有这两个数)。"""
    for preset in PRESETS.values():
        for model in preset.models:
            entry = model_entry(model)
            assert set(entry) == {"id", "contextWindow", "maxTokens"}, (preset.provider, entry)


@pytest.mark.parametrize("name, expected_ids", [
    ("deepseek", ["deepseek-flash", "deepseek-v4-pro"]),     # V4.1 Flash 才是当前模型名
    ("dashscope", ["qwen3.7-plus", "qwen3.8-max", "qwen3.8-flash"]),
    ("zhipu", ["glm-5.2", "glm-4.7"]),                       # 5.3 官方还写着“API 即将上线”
    ("volcengine", ["doubao-seed-evolving", "doubao-seed-2-1-pro-260628"]),
    ("hunyuan", ["hy4-preview", "hy3"]),                     # 已迁到 TokenHub
    ("qianfan", ["ernie-5.1", "ernie-5.0"]),                 # 5.1 是最新
    ("mimo", ["mimo-v2.5-pro", "mimo-v2.5"]),
    ("moonshot", ["kimi-k3", "kimi-k2.7-code"]),
    ("minimax", ["MiniMax-M3"]),
    ("stepfun", ["step-3.7-flash"]),
])
def test_preset_seeds_track_the_vendors_current_lineup(name, expected_ids):
    """种子要跟得上厂商**当前**的模型线。

    这是条**快照测试**:厂商换世代时它会红。那时该做的是去厂商当前的模型页核对、改表
    (并把核对日期写进注释),而**不是**把断言改宽 —— DeepSeek 那次就是活例子:
    我照着旧文档表格写了 `deepseek-v4-flash`,而当时线上已经是 `deepseek-flash`(V4.1)。
    """
    assert [m.id for m in PRESETS[name].models] == expected_ids
