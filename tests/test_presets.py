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
    PRESET_ORDER,
    apply_presets,
    get_preset,
    model_entry,
    preset_names,
    provider_entry,
)  # noqa: E402

runner = CliRunner()


# ── 表自洽 ──────────────────────────────────────────────


def test_preset_order_puts_domestic_providers_first():
    """`qi init` 的列表顺序 = `preset_names()`:国产在前(deepseek、zai 打头),
    国外网关(opencode 两家)排最后 —— 字典序会把 opencode 塞到国产中间,选起来别扭。"""
    names = preset_names()
    assert names[0] == "deepseek" and names[1] == "zai"
    assert names[-2:] == ["opencode", "opencode-go"]
    # 顺序表与预置表必须完全对齐(新增预置忘了排就会掉到末尾,这里会红)
    assert set(PRESET_ORDER) == set(PRESETS)
    assert len(PRESET_ORDER) == len(PRESETS)
    assert set(names) == set(PRESETS) and len(names) == len(PRESETS)


def test_every_preset_is_well_formed():
    assert PRESETS, "预置表不该是空的"
    for name, preset in PRESETS.items():
        assert preset.provider == name, "键与 provider 名要一致(`--preset <键>` 就是它)"
        assert preset.label
        # provider 级端点可以缺(端点随 API 形态变后缀,如 opencode):此时每个模型自带
        if preset.base_url is not None:
            assert preset.base_url.startswith(("http://", "https://")), name
        else:
            assert all(m.base_url for m in preset.models), \
                f"{name} 没有 provider 级端点,模型就都得住自己的 baseUrl"
        assert preset.api_key_env.endswith("_API_KEY")
        # provider 级 api 可以缺(同上);写了就得合法
        assert preset.api is None or preset.api in SUPPORTED_APIS, preset.api
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
            # 模型级覆盖是可选的,但写了就得合法(api 走白名单、baseUrl 是 http 地址)
            if model.api:
                assert model.api in SUPPORTED_APIS, f"{name}/{model.id}: {model.api}"
            if model.base_url:
                assert model.base_url.startswith(("http://", "https://"))


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
    # provider 段只留 key:api/baseUrl 展开到每个模型(pi 的模型目录形状)
    assert set(entry) == {"apiKey", "models"}
    assert entry["apiKey"] == "$DEEPSEEK_API_KEY"
    assert all(m["api"] == "openai-completions" for m in entry["models"])
    assert all(m["baseUrl"] == PRESETS["deepseek"].base_url for m in entry["models"])
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
    missing_seed = next(m for m in seed_ids if m != "deepseek-v4-pro")
    assert changed == [f"deepseek/{missing_seed}"], changed
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
    # 模型级 api/baseUrl 覆盖要看得见 —— `[openai-responses]` 这类标记会被 rich 当
    # markup 吃掉(回归过一次):测试按**最终输出**断言,而不是代码意图。
    # 输出会折行,先拍平再找(拍平连折行处也接上了)
    flat = "".join(res.output.split())
    assert "gpt-5.6-sol[openai-responses]@https://opencode.ai/inference/v1" in flat
    assert "claude-sonnet-5-5[anthropic-messages]@https://opencode.ai/inference" in flat


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
    res = runner.invoke(app, ["init", "--preset", "minimax-cn", "--local"])
    assert res.exit_code == 0, res.output
    project_models = tmp_path / ".qi" / "models.json"
    assert project_models.is_file()
    assert "minimax-cn" in project_models.read_text(encoding="utf-8")


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
    # 兜底出来的模型也要"自解释"(api + baseUrl 都在模型上),否则 doctor/运行期
    # 只能靠 provider 段猜端点 —— 而预置物化后 provider 段本来就只有 apiKey
    zai = cfg.providers["zai"]
    assert zai.baseUrl is None and zai.api is None
    assert all(m.baseUrl and m.api for m in zai.models), zai.models


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

    第二条用户原话是“我把 mimo logout 了,它怎么还在” —— `models.json` 里**显式写过**
    的 provider **没有豁免**:写了 `apiKey` 却解析不出来(环境变量没导出、`$(…)` 跑不通)
    就是“用不了”,不该在选择器里冒充可选。与 pi 同口径:pi 的清单是
    `modelRuntime.getAvailableSnapshot()`,没凭证的 provider 一个模型都不在里面。
    想知道“我配的 provider 到底怎么啦”去看 `qi doctor` / `qi --list-models`(那两个面照列全量)。
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
        # `_tui_env` 写的 models.json 里只有 ollama(免密钥 provider → 不需要凭证)
        app._rt.cfg = load_config(tmp_path)[0]
        providers = {provider for provider, _, _ in app._model_options()}
        assert providers == {"ollama"}                # 预置那批一个都没登录 → 一个都不列

        AuthStore().set_key("deepseek", "sk-x")
        providers = {provider for provider, _, _ in app._model_options()}
        assert providers == {"ollama", "deepseek"}    # 登录了谁就多列谁

        # 显式写在 models.json 里、**没写 apiKey** 的 provider:一样过不了门槛
        # (本地无鉴权端点写一个字面 `apiKey` 就进来了 —— 下面那个 `local`)
        (tmp_path / "models.json").write_text(
            '{"providers": {'
            ' "my-proxy": {"baseUrl": "https://p.internal/v1",'
            '              "models": [{"id": "p1"}]},'
            ' "local": {"baseUrl": "http://127.0.0.1:8080/v1", "apiKey": "none",'
            '           "models": [{"id": "l1"}]}}}', encoding="utf-8")
        monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
        app._rt.cfg = load_config(tmp_path)[0]
        providers = {provider for provider, _, _ in app._model_options()}
        assert providers == {"deepseek", "local"}     # my-proxy 缺凭证 → 不列

        # `/model` 用的就是同一份清单
        app._command("/model")
        await pilot.pause(0.1)
        selector = app.screen
        assert isinstance(selector, tui_mod.ModelSelector)
        shown = selector.rendered_text().plain
        assert "local" in shown and "deepseek" in shown
        assert "my-proxy" not in shown and "kimi" not in shown
        assert "ollama" not in shown
        await pilot.press("escape")
        await pilot.pause(0.1)


def test_mimo_token_plan_is_a_separate_provider():
    """套餐额度是**另一套**域名/Key/模型集:Token Plan 的 key 打按量付费端点会 401
    `Invalid API Key`(实测)。pi 同样把它拆成独立 provider。"""
    from qi_agent.presets import PRESETS as all_presets

    plan = all_presets["mimo-token-plan-cn"]
    assert plan.base_url == "https://token-plan-cn.xiaomimimo.com/v1"
    assert plan.api_key_env == "MIMO_TOKEN_PLAN_CN_API_KEY"
    assert DEFAULT_API_KEY_ENV["mimo-token-plan-cn"] == "MIMO_TOKEN_PLAN_CN_API_KEY"
    # 目录只回 2 个聊天模型(asr/tts 不是聊天模型)
    assert [m.id for m in plan.models] == ["mimo-v2.6-flash", "mimo-v2.6-pro"]
    assert get_preset("mimo-token-plan") is plan             # 补 -cn 之前的旧名字
    # 两家必须指向不同端点,且互相在 note 里点名(这是最容易踩的坑)
    assert all_presets["mimo"].base_url != plan.base_url
    assert "mimo-token-plan-cn" in all_presets["mimo"].note
    assert "Invalid API Key" in plan.note
    # 展示顺序:紧跟按量付费的 mimo
    assert preset_names().index("mimo-token-plan-cn") == preset_names().index("mimo") + 1


@pytest.mark.parametrize("name, base_url, env, api, models", [
    # 智谱 Coding Plan(国区;国际站换 baseUrl)
    ("zai-coding-cn", "https://open.bigmodel.cn/api/coding/paas/v4",
     "ZAI_CODING_CN_API_KEY", "openai-completions",
     ["glm-5.3", "glm-5.3-flash", "glm-5.3-highspeed"]),
    # Kimi for Coding:Anthropic 形态,基址不带 /v1
    ("kimi-coding", "https://api.kimi.com/coding",
     "KIMI_CODING_API_KEY", "anthropic-messages",
     ["k3", "k3-256k", "kimi-for-coding", "kimi-for-coding-highspeed"]),
    # 阿里 Token Plan:聚合网关(国区;新加坡换 baseUrl)
    ("qwen-token-plan-cn", "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
     "QWEN_TOKEN_PLAN_CN_API_KEY", "openai-completions",
     ["qwen3.8-max", "qwen3.7-plus", "deepseek-v4.1-flash", "glm-5.2", "kimi-k2.7-code"]),
])
def test_subscription_tier_presets(name, base_url, env, api, models):
    """套餐/订阅是**另一套端点 + 另一套 key**(标准预置的 key 打过去会 401):
    小米、智谱、月之暗面、阿里都有这种变体,pi 也是逐个拆成独立 provider 的。"""
    preset = PRESETS[name]
    assert preset.base_url == base_url
    assert preset.api_key_env == env
    assert DEFAULT_API_KEY_ENV[name] == env
    assert preset.api == api
    assert [m.id for m in preset.models] == models
    # 物化时 api/baseUrl 落到**每个模型**上(provider 段只剩 apiKey)
    assert provider_entry(preset) == {"apiKey": f"${env}"}
    entry = model_entry(preset.models[0], preset)
    assert entry["api"] == api and entry["baseUrl"] == base_url
    # 拉目录的基址:Anthropic 形态的基址不带 /v1,但目录在 /v1/models
    if name == "kimi-coding":
        assert preset.catalog_base == "https://api.kimi.com/coding/v1"
    # 展示顺序:同家的套餐紧跟标准预置
    names = preset_names()
    if name == "zai-coding-cn":
        assert names.index(name) == names.index("zai-coding") + 1
    elif name == "qwen-token-plan-cn":
        assert names.index(name) == names.index("qwen-token-plan") + 1
    else:
        assert names.index(name) == names.index("moonshot-cn") + 1


def test_renamed_presets_keep_their_old_names_as_aliases():
    """改名不打断老用户:`zhipu` / `dashscope` / `zhipu-coding` 这些旧名继续认,
    物化后一律写成新名字(裸名 = 国际站,`-cn` = 国内站)。"""
    assert get_preset("zhipu") is PRESETS["zai"]
    assert get_preset("dashscope") is PRESETS["qwen"]
    assert get_preset("zhipu-coding") is PRESETS["zai-coding"]
    for alias in ("zhipu", "dashscope", "zhipu-coding"):
        assert alias not in PRESETS
    # CN/国际成对存在(裸名 = 国际站)
    for intl, cn in (("zai", "zai-cn"),
                     ("zai-coding", "zai-coding-cn"),
                     ("qwen", "qwen-cn"),
                     ("qwen-token-plan", "qwen-token-plan-cn"),
                     ("moonshot", "moonshot-cn"),
                     ("minimax", "minimax-cn")):
        assert intl in PRESETS and cn in PRESETS, (intl, cn)
        assert PRESETS[intl].base_url != PRESETS[cn].base_url, (intl, cn)


def test_mimo_preset_is_present():
    """小米 MiMo:按量付费端点 + 约定环境变量(`MIMO_API_KEY`)。"""
    preset = PRESETS["mimo"]
    assert preset.base_url == "https://api.xiaomimimo.com/v1"
    assert preset.api_key_env == "MIMO_API_KEY"
    assert DEFAULT_API_KEY_ENV["mimo"] == "MIMO_API_KEY"
    assert [m.id for m in preset.models] == ["mimo-v2.6-flash", "mimo-v2.6-pro",
                                             "mimo-v2.6-pro-ultraspeed"]
    assert preset.models[0].context_window == 1_048_576
    assert preset.models[0].max_output == 131_072
    assert "token-plan-cn.xiaomimimo.com" in preset.note
    # Anthropic 兼容端点是逃生门,配方要写在 note 里(api 与 baseUrl 成对)
    assert "anthropic/v1" in preset.note and "anthropic-messages" in preset.note


# ── OpenCode:一个品牌两个 provider(Zen 按量付费 / Go 套餐额度) ──


def test_opencode_presets_exist_and_share_one_key():
    """Zen 与 Go 是**两个 provider**(端点/模型集/计费都不同),但共用一个 key。

    端点随 API 形态变后缀(openai 形态带 `/v1`、anthropic 形态不带),所以
    **provider 段不写 api/baseUrl**,两条都落在模型上(同 pi 的 models-store.json)。
    """
    zen, go = PRESETS["opencode"], PRESETS["opencode-go"]
    assert zen.base_url is None and go.base_url is None
    assert zen.api is None and go.api is None
    assert zen.api_key_env == go.api_key_env == "OPENCODE_API_KEY"
    assert DEFAULT_API_KEY_ENV["opencode"] == "OPENCODE_API_KEY"
    assert DEFAULT_API_KEY_ENV["opencode-go"] == "OPENCODE_API_KEY"
    assert zen.default_model == "gpt-5.6-sol"             # 默认 = 第一个
    assert go.default_model == "deepseek-v4.1-flash"
    # 物化出来的 provider 段只剩 apiKey(不写空 api/baseUrl)
    assert provider_entry(zen) == {"apiKey": "$OPENCODE_API_KEY"}
    assert provider_entry(go) == {"apiKey": "$OPENCODE_API_KEY"}


def test_opencode_zen_alias_maps_to_the_opencode_preset():
    """pi / 旧扩展管 Zen 叫 `opencode-zen` —— 认这个名字,但只注册一家。"""
    assert get_preset("opencode-zen") is PRESETS["opencode"]
    assert get_preset("OpenCode-Zen") is PRESETS["opencode"]
    assert "opencode-zen" not in PRESETS
    assert "opencode-zen" not in preset_names()


def test_opencode_models_carry_their_own_api_and_base_url():
    """异构发生在模型间:Responses / Anthropic / chat completions 共存一个 provider,
    api 与 baseUrl 都标在**模型**上(resolve_model 的模型级覆盖链)。

    端点 `https://opencode.ai/inference…`:openai 形态带 `/v1`、anthropic 形态不带
    (SDK 自己拼 `/v1/messages`)。Go 前缀是 `/inference/go`(与 pi 的 store 一致;
    老的 `/zen/…` 仍是同网关别名)。
    """
    by_id = {m.id: m for m in PRESETS["opencode"].models}
    assert by_id["gpt-5.6-sol"].api == "openai-responses"
    assert by_id["gpt-5.6-sol"].base_url == "https://opencode.ai/inference/v1"
    assert by_id["claude-sonnet-5-5"].api == "anthropic-messages"
    assert by_id["claude-sonnet-5-5"].base_url == "https://opencode.ai/inference"  # 不带 /v1
    assert by_id["deepseek-v4.1-flash"].api == "openai-completions"
    assert by_id["deepseek-v4.1-flash"].base_url == "https://opencode.ai/inference/v1"
    # 落进 models.json 时 api 与 baseUrl 成对出现(每个模型都自带)
    entry = model_entry(by_id["claude-sonnet-5-5"])
    assert entry["api"] == "anthropic-messages"
    assert entry["baseUrl"] == "https://opencode.ai/inference"
    go_by_id = {m.id: m for m in PRESETS["opencode-go"].models}
    assert go_by_id["gpt-5.6-luna"].api == "openai-responses"
    assert go_by_id["gpt-5.6-luna"].base_url == "https://opencode.ai/inference/go/v1"
    assert go_by_id["qwen3.7-plus"].api == "anthropic-messages"               # pi store 口径
    assert go_by_id["qwen3.7-plus"].base_url == "https://opencode.ai/inference/go"
    for model in list(by_id.values()) + list(go_by_id.values()):
        assert model.api and model.base_url, f"{model.id} 缺 api/baseUrl"


@pytest.mark.parametrize("name, expected_ids", [
    # 快照(2026-10 对厂商目录核过):Zen
    # GET https://opencode.ai/inference/v1/models、Go 目录见 pi 的 models-store.json
    ("opencode", ["gpt-5.6-sol", "claude-sonnet-5-5", "deepseek-v4.1-flash",
                  "glm-5.2", "minimax-m3"]),
    ("opencode-go", ["deepseek-v4.1-flash", "glm-5.2", "kimi-k3", "qwen3.7-plus",
                     "gpt-5.6-luna"]),
])
def test_opencode_seeds_use_ids_from_the_vendors_live_catalog(name, expected_ids):
    """种子模型 id 必须是厂商 `/models` 目录里**真存在**的(两个目录匿名可拉,
    刷新用 `qi init --refresh <名字>`)。种子不在这份快照里 = 厂商目录变了,
    该去目录核对改表,而不是放宽断言。"""
    assert [m.id for m in PRESETS[name].models] == expected_ids


def test_every_preset_model_carries_both_numbers():
    """每个预置模型都带 `contextWindow` 与 `maxTokens`(用户要求:预置模型要有这两个数)。

    `api` / `baseUrl` 是**可选的模型级覆盖**(见 opencode)—— 有就写、没有就不写,
    不让条目比需要的大。
    """
    for preset in PRESETS.values():
        for model in preset.models:
            entry = model_entry(model)
            assert {"id", "contextWindow", "maxTokens"} <= set(entry), (preset.provider, entry)
            assert set(entry) <= {"id", "contextWindow", "maxTokens", "api", "baseUrl"}, \
                (preset.provider, entry)
            assert entry.get("api") == model.api, entry
            assert entry.get("baseUrl") == model.base_url, entry


@pytest.mark.parametrize("name, expected_ids", [
    ("deepseek", ["deepseek-flash", "deepseek-v4-pro"]),     # V4.1 Flash 才是当前模型名
    ("qwen", ["qwen3.7-plus", "qwen3.8-max", "qwen3.8-flash"]),
    ("qwen-cn", ["qwen3.7-plus", "qwen3.8-max", "qwen3.8-flash"]),
    ("zai", ["glm-5.2", "glm-4.7"]),                         # 5.3 官方还写着“API 即将上线”
    ("zai-coding", ["glm-5.3", "glm-5.3-flash", "glm-5.3-highspeed"]),
    ("mimo", ["mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"]),
    ("moonshot", ["kimi-k3", "kimi-k2.7-code"]),
    ("minimax", ["MiniMax-M3"]),
    ("minimax-cn", ["MiniMax-M3"]),
])
def test_preset_seeds_track_the_vendors_current_lineup(name, expected_ids):
    """种子要跟得上厂商**当前**的模型线。

    这是条**快照测试**:厂商换世代时它会红。那时该做的是去厂商当前的模型页核对、改表
    (并把核对日期写进注释),而**不是**把断言改宽 —— DeepSeek 那次就是活例子:
    我照着旧文档表格写了 `deepseek-v4-flash`,而当时线上已经是 `deepseek-flash`(V4.1)。
    """
    assert [m.id for m in PRESETS[name].models] == expected_ids
