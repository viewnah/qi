"""预置 provider(国产为主)+ 它们的模型清单。

**两种用法,一套数据**:

* **兜底**(默认行为):`models.json` 里**没写**的预置 provider,由 `config.load_config`
  自动补上(`with_preset_providers`)—— 所以 `export DEEPSEEK_API_KEY=…` 或
  `qi auth login deepseek` 之后**立刻能用**,不必先物化配置文件;TUI 的 `/login` 也直接
  看得到这批。
* **物化**(显式):`qi init --preset deepseek` 把它们**写进** `models.json`,方便改
  (改成自建代理域名 / 加模型 / 调 `contextWindow`)。

**models.json 里写了同名 provider 就以它为准** —— 预置从不覆盖用户写的东西。

## 数据口径(重要)

表里每个模型都带 **`contextWindow`(上下文窗口=最大输入)与 `maxTokens`(最大输出)**
—— 这正是 pi `docs/models.md` 里这两个字段的语义,且**两个数都从厂商自己的文档/模型页
核过**(见每条注释里的出处)。核不到的模型**干脆不收**:预置宁缺毋滥 —— 猜一个
`maxTokens` 会让请求要么被拒要么悄悄截断,比没有这一条更糟。想补别的模型就直接改
`models.json`(`qi init --preset <名>` 先物化,再按厂商文档加一条)。

`reasoning` 一律留 False:这些模型多数**默认就带思考**,而 `reasoning_effort` 是不是被
各家 OpenAI 兼容端点接受并不一致 —— 默认不带参最不容易 400,想开就在 `models.json` 里
给该模型加 `"reasoning": true`(provider 拒收时 qi 会自动去掉并提示一次)。

## 种子会过时 —— 以接口为准

模型 id 会漂(上新 / 下线 / 改名)。本表只是**离线可用的种子**;要跟厂商对齐就用:

    qi init --refresh <provider>        # GET {baseUrl}/models,把实际可用的 id 并进 models.json
    qi init --refresh-all               # 所有已配置(有凭证)的 provider

`refresh` **只加不删**:接口回来的新 id 追加进去(带 qi 的默认 ctx/max,厂商文档里有就自己补),
本地有、接口没返回的**保留并报出来**(厂商可能只是没列全,不该替用户删配置)。

实例:DeepSeek 从 `deepseek-v4-flash` 迁到 `deepseek-flash`(旧名还在但路由到新模型)——
这种变更只能靠接口/公告,写死的表迟早落后一拍。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_API = "openai-completions"

#: 几条常见的“取整”写法换算成 token 数(厂商文档常写 128K / 1M 这种)。
K = 1024
M = 1024 * 1024


@dataclass(frozen=True)
class PresetModel:
    """一个预置模型条目。

    `context_window` = 上下文窗口(最大输入),`max_output` = 模型的最大输出 tokens
    (pi 的 `maxTokens` 字段同义)。两个数都必须有出处 —— 见模块 docstring 的口径。
    """

    id: str
    context_window: int
    max_output: int


@dataclass(frozen=True)
class Preset:
    """一个预置 provider。"""

    provider: str                    # provider 名(`provider/模型` 里那个)
    label: str                       # 人看的名字
    base_url: str
    api_key_env: str                 # 约定环境变量名(与 auth.DEFAULT_API_KEY_ENV 一致)
    models: tuple[PresetModel, ...] = field(default_factory=tuple)
    api: str = DEFAULT_API
    note: str = ""                   # 需要提醒用户的话(写进 `qi init --preset` 的输出)

    @property
    def default_model(self) -> str | None:
        """物化后默认用哪个模型(取第一个)。"""
        return self.models[0].id if self.models else None


PRESETS: dict[str, Preset] = {
    "deepseek": Preset(
        provider="deepseek",
        label="DeepSeek(深度求索)",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        # 官方「模型 & 价格」(2026-09 核对):当前模型名是 `deepseek-flash`
        # (= DeepSeek-V4.1-Flash,1M 上下文 / 输出最大 384K = 393216);
        # `deepseek-v4-flash` 是**旧名**(底层已下线,请求被路由到 V4.1-Flash),
        # `deepseek-v4-pro` 仍在但官方计划下线 —— 所以以 `deepseek-flash` 为首选。
        # 觉得跟不上了就 `qi init --refresh deepseek`(以接口为准)。
        models=(PresetModel("deepseek-flash", 1_000_000, 393_216),
                PresetModel("deepseek-v4-pro", 1_000_000, 393_216)),
    ),
    "dashscope": Preset(
        provider="dashscope",
        label="阿里云百炼(通义千问)",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        # 百炼模型页(逐个核过):三者都是 上下文 1000000 / 最大输出 131072。
        # 官方选型建议:`qwen3.7-plus` 能力与成本均衡、工具调用完整,是编码/Agent 场景的
        # “多面手”(排第一 = 默认);要最强推理用 `qwen3.8-max`,要更省用 `qwen3.8-flash`。
        models=(PresetModel("qwen3.7-plus", 1_000_000, 131_072),
                PresetModel("qwen3.8-max", 1_000_000, 131_072),
                PresetModel("qwen3.8-flash", 1_000_000, 131_072)),
        note="北京地域新账号可能要用工作空间专属域名:"
             "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    ),
    "mimo": Preset(
        provider="mimo",
        label="小米 MiMo",
        base_url="https://api.xiaomimimo.com/v1",
        api_key_env="MIMO_API_KEY",
        # MiMo 模型页:上下文窗口 1M / 最大输出 128K
        models=(PresetModel("mimo-v2.5-pro", 1_048_576, 131_072),
                PresetModel("mimo-v2.5", 1_048_576, 131_072)),
        note="订阅套餐(Token Plan)是另一个域名:`https://token-plan-cn.xiaomimimo.com/v1`,"
             "Key 形如 `tp-…`(按量付费的 Key 形如 `sk-…`,两者不可混用)",
    ),
    "moonshot": Preset(
        provider="moonshot",
        label="月之暗面 Kimi",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        # Kimi 文档:k3 上下文 1M、max_completion_tokens 上限 131072;
        # k2.7-code 上下文 256K、max_tokens 默认 32768
        models=(PresetModel("kimi-k3", 1_048_576, 131_072),
                PresetModel("kimi-k2.7-code", 262_144, 32_768)),
    ),
    "zhipu": Preset(
        provider="zhipu",
        label="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_env="ZHIPUAI_API_KEY",
        # 智谱「模型概览」(2026-09 核):线上旗舰是 **GLM-5.2**(1M 上下文 / 最大输出 128K);
        # GLM-5.3 官方页面写的是“API 即将上线”,所以不放进种子(上线后
        # `qi init --refresh zhipu` 就能拉回来)。GLM-4.7:200K / 128K。
        models=(PresetModel("glm-5.2", 1_048_576, 131_072),
                PresetModel("glm-4.7", 202_752, 131_072)),
        note="GLM-5.3 官方标注“API 即将上线”(Bailian 已可调);上线后 `qi init --refresh zhipu` 拉一下",
    ),
    "minimax": Preset(
        provider="minimax",
        label="MiniMax",
        base_url="https://api.minimax.cn/v1",
        api_key_env="MINIMAX_API_KEY",
        # MiniMax 文档:上下文 1,000,000;最大输出 128K
        models=(PresetModel("MiniMax-M3", 1_000_000, 131_072),),
    ),
    "siliconflow": Preset(
        provider="siliconflow",
        label="硅基流动(SiliconFlow)",
        base_url="https://api.siliconflow.cn/v1",
        api_key_env="SILICONFLOW_API_KEY",
        # 硅基流动模型页:1M 上下文 + 最高 384K 输出
        models=(PresetModel("deepseek-ai/DeepSeek-V4-Flash", 1_000_000, 393_216),),
        note="聚合平台:模型 id 带组织前缀(`zai-org/GLM-5.2`、`Qwen/…`、`Pro/…`),"
             "各家新模型在这儿的 id 与官方不同 —— 以模型广场/`qi init --refresh siliconflow` 为准",
    ),
    "stepfun": Preset(
        provider="stepfun",
        label="阶跃星辰 StepFun",
        base_url="https://api.stepfun.com/v1",
        api_key_env="STEP_API_KEY",
        # 阶跃/百炼模型页:上下文 262144 / 最大输出 262144
        models=(PresetModel("step-3.7-flash", 262_144, 262_144),),
    ),
    "hunyuan": Preset(
        provider="hunyuan",
        label="腾讯混元(TokenHub)",
        base_url="https://tokenhub.tencentmaas.com/v1",
        api_key_env="HUNYUAN_API_KEY",
        # 腾讯已把混元迁到 **TokenHub**(旧平台 `api.hunyuan.cloud.tencent.com` 不再新增模型)。
        # TokenHub 模型列表(2026-09 核):`hy4-preview` 上下文 1M / 最大输出 64K;
        # `hy3` 上下文 256K / 最大输出 128K。
        models=(PresetModel("hy4-preview", 1_048_576, 65_536),
                PresetModel("hy3", 262_144, 131_072)),
        note="这里是 TokenHub(新平台,Key 也从 TokenHub 控制台拿);"
             "旧平台仍是 `https://api.hunyuan.cloud.tencent.com/v1` + `hunyuan-turbos-latest`,"
             "两边的 Key 不通用",
    ),
    "volcengine": Preset(
        provider="volcengine",
        label="火山方舟(豆包)",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key_env="ARK_API_KEY",
        # 厂商产品页(2026-09 核):上下文 256K(=262144)。最大输出两处口径不一(产品页写 256K、
        # 文档写“单次最大输出 128K(含思考)”)—— **取小的那侧**(要 128K 之上的输出极少见,
        # 而报一个超出上限的值会被接口打回)。
        # `doubao-seed-evolving` 是**版本无关**的统一 id(周更自动迭代,不用改配置),所以排第一。
        models=(PresetModel("doubao-seed-evolving", 262_144, 131_072),
                PresetModel("doubao-seed-2-1-pro-260628", 262_144, 131_072)),
        note="方舟的普通模型 id 带日期后缀(会变);`doubao-seed-evolving` 是统一 id 不需要换。"
             "自定义部署还要用 Endpoint ID —— 以控制台“模型列表”里的调用名为准",
    ),
    "qianfan": Preset(
        provider="qianfan",
        label="百度千帆(文心)",
        base_url="https://qianfan.baidubce.com/v2",
        api_key_env="QIANFAN_API_KEY",
        # 千帆模型列表:上下文 128k(=131072) / 最大输出 65536
        # 千帆模型列表:`ernie-5.1` 是文心系列**最新**模型,与 `ernie-5.0` 同为
        # 上下文 128k(=131072) / 最大输出 65536
        models=(PresetModel("ernie-5.1", 131_072, 65_536),
                PresetModel("ernie-5.0", 131_072, 65_536)),
    ),
}
"""预置表(键 = `--preset` 收的名字,也是默认 provider 名)。"""


def preset_names() -> list[str]:
    """全部预置名(排序,给 `--list-presets` 与补全用)。"""
    return sorted(PRESETS)


def get_preset(name: str) -> Preset | None:
    """按名字取预置(大小写不敏感;认 provider 名本身)。"""
    key = (name or "").strip().lower()
    if key in PRESETS:
        return PRESETS[key]
    for preset in PRESETS.values():          # 也认 label 里的 provider 名
        if preset.provider.lower() == key:
            return preset
    return None


def provider_entry(preset: Preset) -> dict[str, Any]:
    """预置 → `models.json` 里的 provider 段(不含 models,便于合并时分开处理)。"""
    return {
        "api": preset.api,
        "baseUrl": preset.base_url,
        "apiKey": f"${preset.api_key_env}",
    }


def model_entry(model: PresetModel) -> dict[str, Any]:
    """预置模型 → `models.json` 的模型条目(`contextWindow` + `maxTokens` 都是核过的值)。"""
    return {
        "id": model.id,
        "contextWindow": model.context_window,
        "maxTokens": model.max_output,
    }


def apply_presets(data: dict[str, Any], names: list[str]) -> tuple[dict[str, Any], list[str]]:
    """把预置合并进 `models.json` 的内容(返回 `(新内容, 变更说明)`)。

    合并规矩(与 `qi init` 的其它路径一致:**不静默覆盖用户写过的东西**):

    * provider 段:缺什么补什么(`api` / `baseUrl` / `apiKey`),已有值**不动** ——
      用户改成自建代理或别的 key 来源之后,再跑一次 preset 不该把它洗掉;
    * 模型列表:按 id 追加缺的;同一个 id 已存在就**原样保留**(不覆盖 reasoning /
      contextWindow —— 用户手调过的以用户为准)。

    `data` 会被原地修改(调用方拿到的就是它),同时返回便于链式使用。
    """
    providers: dict[str, Any] = data.setdefault("providers", {})
    changed: list[str] = []
    for name in names:
        preset = get_preset(name)
        if preset is None:
            raise KeyError(name)
        raw_entry = providers.get(preset.provider)
        entry: dict[str, Any] = raw_entry if isinstance(raw_entry, dict) else {}
        if not isinstance(raw_entry, dict):
            providers[preset.provider] = entry
        for key, value in provider_entry(preset).items():
            if not entry.get(key):
                entry[key] = value
                changed.append(f"{preset.provider}.{key}")
        models: list[Any] = entry.setdefault("models", [])
        known = {m.get("id") for m in models if isinstance(m, dict)}
        for model in preset.models:
            if model.id in known:
                continue
            models.append(model_entry(model))
            changed.append(f"{preset.provider}/{model.id}")
    return data, changed
