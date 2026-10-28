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

交互流里同一条入口:`qi init` → `--- Add Models ---` 菜单选
`↻ Refresh model list (GET /models)`(与 `＋ Add a model` 同级,失败只报错不中断)。

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

    `api` / `base_url` 是**模型级覆盖**(不写就跟随 provider):同一批模型走不同
    API 形态 / 端点时用(实例:OpenCode Zen 的 anthropic 模型走另一个 baseUrl)。
    """

    id: str
    context_window: int
    max_output: int
    api: str | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class Preset:
    """一个预置 provider。"""

    provider: str                    # provider 名(`provider/模型` 里那个)
    label: str                       # 人看的名字
    api_key_env: str                 # 约定环境变量名(与 auth.DEFAULT_API_KEY_ENV 一致)
    base_url: str | None = None      # 厂商统一端点(None = 各模型自带,如 opencode)
    #: 拉模型目录的基址(`GET {catalog_base}/models`);None = 就是 `base_url`。
    #: 少数厂商两者不同:Anthropic 形态的基址不带 `/v1`(SDK 自己拼),但目录在 `/v1/models`
    catalog_base: str | None = None
    models: tuple[PresetModel, ...] = field(default_factory=tuple)
    api: str | None = DEFAULT_API    # provider 级 API 形态;None = 不写(模型各自声明)
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
    # ── 智谱 / Z.ai:国际站(`api.z.ai`)与国内站(`open.bigmodel.cn`)路径一样,
    #    只换域名 —— 按"裸名 = 国际、`-cn` = 国内"命名(与 pi 一致)。
    #    标准 API 与订阅套餐(GLM Coding Plan)是**两套端点、两套 key**,各自成对。
    "zai": Preset(
        provider="zai",
        label="Z.ai 智谱 GLM(国际站)",
        base_url="https://api.z.ai/api/paas/v4",
        api_key_env="ZAI_API_KEY",
        # 智谱「模型概览」:旗舰 GLM-5.2(1M / 128K);GLM-4.7 是 200K / 128K
        models=(PresetModel("glm-5.2", 1_048_576, 131_072),
                PresetModel("glm-4.7", 202_752, 131_072)),
    ),
    "zai-cn": Preset(
        provider="zai-cn",
        label="智谱 GLM(国内站)",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_env="ZAI_CN_API_KEY",
        models=(PresetModel("glm-5.2", 1_048_576, 131_072),
                PresetModel("glm-4.7", 202_752, 131_072)),
        note="两站 key 不通用;国际站是 `--preset zai`(`https://api.z.ai/api/paas/v4`)",
    ),
    "zai-coding": Preset(
        provider="zai-coding",
        label="Z.ai 智谱 GLM Coding Plan(国际站)",
        base_url="https://api.z.ai/api/coding/paas/v4",
        api_key_env="ZAI_CODING_API_KEY",
        models=(PresetModel("glm-5.3", 1_000_000, 131_072),          # 默认
                PresetModel("glm-5.3-flash", 1_000_000, 131_072),
                PresetModel("glm-5.3-highspeed", 1_000_000, 131_072)),
        note="套餐 key **只能**打 `/api/coding/paas/v4`(标准端点用不了,反之亦然);"
             "国内站是 `--preset zai-coding-cn`",
    ),
    "zai-coding-cn": Preset(
        provider="zai-coding-cn",
        label="智谱 GLM Coding Plan(国内站)",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        api_key_env="ZAI_CODING_CN_API_KEY",
        models=(PresetModel("glm-5.3", 1_000_000, 131_072),          # 默认
                PresetModel("glm-5.3-flash", 1_000_000, 131_072),
                PresetModel("glm-5.3-highspeed", 1_000_000, 131_072)),
        note="套餐 key **只能**打 `/api/coding/paas/v4`;套餐里另有视觉模型 `glm-4.6v`"
             "(128K / 32K),要的话 `qi init --refresh zai-coding-cn` 拉回来;"
             "国际站是 `--preset zai-coding`",
    ),
    # ── 阿里云:DashScope(百炼)就是通义千问的服务平台,provider 名统一用 `qwen`。
    #    国内站 `dashscope.aliyuncs.com` / 国际站 `dashscope-intl.aliyuncs.com`;
    #    套餐(Token Plan)另有两个区域:北京 `cn-beijing` / 新加坡 `ap-southeast-1`。
    "qwen": Preset(
        provider="qwen",
        label="通义千问 Qwen(国际站)",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key_env="QWEN_API_KEY",
        # 百炼模型页(逐个核过):三者都是 上下文 1000000 / 最大输出 131072。
        models=(PresetModel("qwen3.7-plus", 1_000_000, 131_072),     # 默认
                PresetModel("qwen3.8-max", 1_000_000, 131_072),
                PresetModel("qwen3.8-flash", 1_000_000, 131_072)),
    ),
    "qwen-cn": Preset(
        provider="qwen-cn",
        label="通义千问 Qwen(国内站 / 百炼)",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        # 官方选型建议:`qwen3.7-plus` 能力与成本均衡、工具调用完整,是编码/Agent 场景的
        # “多面手”(排第一 = 默认);要最强推理用 `qwen3.8-max`,要更省用 `qwen3.8-flash`。
        models=(PresetModel("qwen3.7-plus", 1_000_000, 131_072),
                PresetModel("qwen3.8-max", 1_000_000, 131_072),
                PresetModel("qwen3.8-flash", 1_000_000, 131_072)),
        note="北京地域新账号可能要用工作空间专属域名:"
             "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1;"
             "国际站是 `--preset qwen`(`dashscope-intl.aliyuncs.com`)",
    ),
    "qwen-token-plan": Preset(
        provider="qwen-token-plan",
        label="阿里云 Token Plan(新加坡)",
        base_url="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        api_key_env="QWEN_TOKEN_PLAN_API_KEY",
        # 套餐目录有 20 个模型(2026-10 取自 pi 目录),这里只放 5 个主力;
        # 窗口/输出按 pi 目录逐条抄(接口不返回这两个数)。
        models=(PresetModel("qwen3.8-max", 1_000_000, 131_072),      # 默认
                PresetModel("qwen3.7-plus", 1_000_000, 65_536),
                PresetModel("deepseek-v4.1-flash", 1_000_000, 384_000),
                PresetModel("glm-5.2", 1_000_000, 131_072),
                PresetModel("kimi-k2.7-code", 262_144, 262_144)),
        note="这是个**聚合网关**(qwen / deepseek / glm / kimi / minimax 都在上面);"
             "国内站是 `--preset qwen-token-plan-cn`(北京);"
             "全量目录用 `qi init --refresh qwen-token-plan` 拉",
    ),
    "qwen-token-plan-cn": Preset(
        provider="qwen-token-plan-cn",
        label="阿里云 Token Plan(北京)",
        base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        api_key_env="QWEN_TOKEN_PLAN_CN_API_KEY",
        models=(PresetModel("qwen3.8-max", 1_000_000, 131_072),      # 默认
                PresetModel("qwen3.7-plus", 1_000_000, 65_536),
                PresetModel("deepseek-v4.1-flash", 1_000_000, 384_000),
                PresetModel("glm-5.2", 1_000_000, 131_072),
                PresetModel("kimi-k2.7-code", 262_144, 262_144)),
        note="套餐 key 与百炼按量付费(`qwen-cn`)不通用;新加坡站是 "
             "`--preset qwen-token-plan`;全量目录用 `qi init --refresh qwen-token-plan-cn` 拉",
    ),
    # ── 小米 MiMo:按量付费只有一个全球域名;套餐(Token Plan)按区域分 cn/sgp/ams。
    "mimo": Preset(
        provider="mimo",
        label="小米 MiMo",
        base_url="https://api.xiaomimimo.com/v1",
        api_key_env="MIMO_API_KEY",
        # MiMo 模型页 + list-models(2026-09 核):现役文本模型是 v2.6 系列,
        # 上下文 1M(=1048576)/ 最大输出 128K(=131072);`flash` 官方定位“高智能、
        # 低成本”(默认),`pro` 是旗舰。**旧的 `mimo-v2.5` / `mimo-v2.5-pro` 官方公告
        # 2026-10-21 10:00 下线**,所以种子只放 v2.6 —— 下线公告:
        # https://mimo.mi.com/docs/zh-CN/quick-start/summary/model
        # `mimo-v2.6-pro-ultraspeed` = pro 的**速度档**,限流是定制服务,同样 1M / 128K。
        models=(PresetModel("mimo-v2.6-flash", 1_048_576, 131_072),
                PresetModel("mimo-v2.6-pro", 1_048_576, 131_072),
                PresetModel("mimo-v2.6-pro-ultraspeed", 1_048_576, 131_072)),
        note="**Token Plan 的 key(形如 `tp-…`)打这个端点会 401 `Invalid API Key`**"
             " —— 套餐用 `--preset mimo-token-plan-cn`(`https://token-plan-cn.xiaomimimo.com/v1`);"
             "按量付费的 key 形如 `sk-…`,两者不可混用;"
             "`mimo-v2.5` / `mimo-v2.5-pro` 官方公告 2026-10-21 10:00 下线;"
             "另有 **Anthropic 兼容端点** `https://api.xiaomimimo.com/anthropic/v1`"
             "(同 key)—— 要走它就在模型条目上**成对**写 "
             "`\"api\": \"anthropic-messages\"` + `\"baseUrl\": \"…/anthropic/v1\"`",
    ),
    "mimo-token-plan-cn": Preset(
        provider="mimo-token-plan-cn",
        label="小米 MiMo Token Plan(国内套餐)",
        base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key_env="MIMO_TOKEN_PLAN_CN_API_KEY",
        models=(PresetModel("mimo-v2.6-flash", 1_048_576, 131_072),
                PresetModel("mimo-v2.6-pro", 1_048_576, 131_072)),
        note="套餐 key **只能**打这个域名(打到按量付费端点会 401 `Invalid API Key`);"
             "另有区域端点 `token-plan-sgp` / `token-plan-ams`.xiaomimimo.com,换 baseUrl 即可",
    ),
    # ── 月之暗面:国际站 `api.moonshot.ai` / 国内站 `api.moonshot.cn`;
    #    订阅版(Kimi for Coding)是另一个域名 + Anthropic 形态。
    "moonshot": Preset(
        provider="moonshot",
        label="月之暗面 Kimi(国际站)",
        base_url="https://api.moonshot.ai/v1",
        api_key_env="MOONSHOT_API_KEY",
        # Kimi 文档:k3 上下文 1M、max_completion_tokens 上限 131072;
        # k2.7-code 上下文 256K、max_tokens 默认 32768
        models=(PresetModel("kimi-k3", 1_048_576, 131_072),
                PresetModel("kimi-k2.7-code", 262_144, 32_768)),
    ),
    "moonshot-cn": Preset(
        provider="moonshot-cn",
        label="月之暗面 Kimi(国内站)",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_CN_API_KEY",
        models=(PresetModel("kimi-k3", 1_048_576, 131_072),
                PresetModel("kimi-k2.7-code", 262_144, 32_768)),
        note="两站 key 不通用;国际站是 `--preset moonshot`(`api.moonshot.ai`)",
    ),
    # 订阅版:与按量付费完全两套(端点、key、协议形态都不同)。
    "kimi-coding": Preset(
        provider="kimi-coding",
        label="月之暗面 Kimi for Coding(套餐)",
        base_url="https://api.kimi.com/coding",
        api="anthropic-messages",        # SDK 自己拼 `/v1/messages`,所以基址不带 /v1
        catalog_base="https://api.kimi.com/coding/v1",   # 目录在 /coding/v1/models
        api_key_env="KIMI_CODING_API_KEY",
        models=(PresetModel("k3", 1_048_576, 131_072),               # 默认
                PresetModel("k3-256k", 262_144, 131_072),
                PresetModel("kimi-for-coding", 1_048_576, 32_768),
                PresetModel("kimi-for-coding-highspeed", 262_144, 32_768)),
        note="走 **Anthropic 形态**(`/coding` 基址,SDK 拼 `/v1/messages`),不是 OpenAI 兼容;"
             "订阅 key 与按量付费的 `moonshot` / `moonshot-cn` 不通用",
    ),
    # ── MiniMax:国际站 `api.minimax.io` / 国内站 `api.minimax.cn`。
    "minimax": Preset(
        provider="minimax",
        label="MiniMax(国际站)",
        base_url="https://api.minimax.io/v1",
        api_key_env="MINIMAX_API_KEY",
        # MiniMax 模型页:M3 上下文 1M / 最大输出 128K(pi 目录同值)
        models=(PresetModel("MiniMax-M3", 1_000_000, 131_072),),
    ),
    "minimax-cn": Preset(
        provider="minimax-cn",
        label="MiniMax(国内站)",
        base_url="https://api.minimax.cn/v1",
        api_key_env="MINIMAX_CN_API_KEY",
        models=(PresetModel("MiniMax-M3", 1_000_000, 131_072),),
        note="两站 key 不通用;国际站是 `--preset minimax`(`api.minimax.io`)",
    ),
    # ── OpenCode:一个品牌的两个计费体系,拆成两个 provider(端点/模型集/额度都不同)
    # 端点已**迁到 `/inference/…`**(2026-10 探测:
    #   Zen  GET https://opencode.ai/inference/v1/models  (84 个,匿名可拉;
    #        `POST /inference/v1/{chat/completions,responses,messages}` 均可达)
    #   Go   GET https://opencode.ai/inference/go/v1/models (目录在 pi 的 model store;
    #        `/zen/go/v1` 是同网关的旧别名,`/inference/go/v1/models` 自身 404)
    # pi 的 models-store.json 也是这个形状:**每个模型自带 api + baseUrl**,
    # provider 段既无 api 也无 baseUrl —— 同一厂商的端点随 API 形态变后缀
    # (openai 形态带 `/v1`、anthropic 形态不带,SDK 自己拼 `/v1/messages`),
    # 一个 provider 一个 URL 表达不了,所以这里照 pi 把两条都落到模型上。
    # —— 厂商不公布窗口/输出上限表,所以下面的数字分两类写注释:
    #   有厂商出处的(deepseek/glm/minimax/kimi/qwen):复用本表同模型已核过的值;
    #   没有的(gpt/claude):取自 pi-ai 目录(其源为厂商模型页),复核入口是
    #   厂商文档 + `qi init --refresh opencode`。模型 id 是活跃目录核对过的硬事实。
    "opencode": Preset(
        provider="opencode",
        label="OpenCode Zen(按量付费)",
        api_key_env="OPENCODE_API_KEY",
        api=None,                    # provider 段不写 api(每个模型自带)
        # 端点自带三套 API 形态,异构发生在模型间 → api/baseUrl 全在模型上。
        # `x-opencode-session` 头已内置(见 provider_headers.py),不用装扩展。
        models=(PresetModel("gpt-5.6-sol", 1_050_000, 128_000,   # 默认;Responses 形态
                            api="openai-responses",
                            base_url="https://opencode.ai/inference/v1"),
                PresetModel("claude-sonnet-5-5", 1_000_000, 128_000,
                            api="anthropic-messages",
                            base_url="https://opencode.ai/inference"),
                PresetModel("deepseek-v4.1-flash", 1_000_000, 393_216,   # 同 deepseek 预置
                            api="openai-completions",
                            base_url="https://opencode.ai/inference/v1"),
                PresetModel("glm-5.2", 1_048_576, 131_072,               # 同 zhipu 预置
                            api="openai-completions",
                            base_url="https://opencode.ai/inference/v1"),
                PresetModel("minimax-m3", 1_000_000, 131_072,            # 同 minimax 预置
                            api="openai-completions",
                            base_url="https://opencode.ai/inference/v1")),
        note="按量付费网关:Key 从 `opencode auth login` / opencode.ai 控制台拿;"
             "端点按 API 形态分(`/inference/v1` = openai 形态含 Responses,`/inference` = Anthropic 形态),"
             "所以 api/baseUrl 都写在**模型**上(同 pi 的 models-store.json);"
             "`qi init --refresh opencode` 随时以厂商目录为准换新模型",
    ),
    "opencode-go": Preset(
        provider="opencode-go",
        label="OpenCode Go(套餐额度)",
        api_key_env="OPENCODE_API_KEY",
        api=None,                    # 同上:形态与端点都在模型上
        # Go 是套餐额度体系(与按量付费分开计),目录 38 个模型;同 key。
        # 端点前缀 `/inference/go`(与 pi 的 models-store.json 一致)。
        models=(PresetModel("deepseek-v4.1-flash", 1_000_000, 393_216,  # 默认;同 deepseek 预置
                            api="openai-completions",
                            base_url="https://opencode.ai/inference/go/v1"),
                PresetModel("glm-5.2", 1_048_576, 131_072,
                            api="openai-completions",
                            base_url="https://opencode.ai/inference/go/v1"),
                PresetModel("kimi-k3", 1_048_576, 131_072,               # 同 moonshot 预置
                            api="openai-completions",
                            base_url="https://opencode.ai/inference/go/v1"),
                PresetModel("qwen3.7-plus", 1_000_000, 131_072,          # pi store 里是 Anthropic 形态
                            api="anthropic-messages",
                            base_url="https://opencode.ai/inference/go"),
                PresetModel("gpt-5.6-luna", 1_050_000, 128_000,          # Responses 形态
                            api="openai-responses",
                            base_url="https://opencode.ai/inference/go/v1")),
        note="套餐额度体系(与 Zen 按量付费分开计费),与 Zen 共用 `OPENCODE_API_KEY`;"
             "端点按 API 形态分(`/inference/go/v1` / `/inference/go`),api/baseUrl 写在模型上;"
             "模型集以 `qi init --refresh opencode-go` 拉到的目录为准(比 Zen 少得多)"
    ),
}
"""预置表(键 = `--preset` 收的名字,也是默认 provider 名)。"""


#: 预置的**展示顺序**:国产在前(deepseek / zhipu 打头),国外网关排最后。
#: `qi init` 的 provider 列表与 `--list-presets` 都按它走 —— 字典序会把 opencode 排到
#: 国产厂商中间,选起来别扭。没列进来的名字(order 落后于新增预置时)按字典序补在末尾。
PRESET_ORDER: tuple[str, ...] = (
    "deepseek",
    "zai", "zai-cn", "zai-coding", "zai-coding-cn",
    "qwen", "qwen-cn", "qwen-token-plan", "qwen-token-plan-cn",
    "mimo", "mimo-token-plan-cn",
    "moonshot", "moonshot-cn", "kimi-coding",
    "minimax", "minimax-cn",
    "opencode", "opencode-go",
)


def preset_names() -> list[str]:
    """全部预置名,按**展示顺序**(见 `PRESET_ORDER`;`qi init` 与补全都用它)。"""
    ordered = [name for name in PRESET_ORDER if name in PRESETS]
    return ordered + [name for name in sorted(PRESETS) if name not in PRESET_ORDER]


#: 别名 → 真名(承认生态里其它工具的叫法;物化后 provider 名永远是真名)
ALIASES: dict[str, str] = {
    "opencode-zen": "opencode",      # pi / 旧扩展管 Zen 叫 opencode-zen
    "zhipu": "zai",                  # 旧名(pi 已改用 zai)
    "zhipu-coding": "zai-coding",    # 旧名
    "zai-coding-cn": "zai-coding-cn",  # 真名(保留以免被旧别名误解)
    "dashscope": "qwen",             # DashScope 就是通义千问的服务平台
    "dashscope-cn": "qwen-cn",
    "mimo-token-plan": "mimo-token-plan-cn",   # 补 -cn 之前的名字
}


def get_preset(name: str) -> Preset | None:
    """按名字取预置(大小写不敏感;认 provider 名本身与 `ALIASES` 别名)。"""
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key)
    if key in PRESETS:
        return PRESETS[key]
    for preset in PRESETS.values():          # 也认 label 里的 provider 名
        if preset.provider.lower() == key:
            return preset
    return None


def default_model_shape(preset: Preset) -> tuple[str | None, str | None]:
    """预置的"默认形态"`(api, baseURL)` —— 给刷新新加的模型兜底用。

    优先取 chat completions 形态(opencode 的绝大多数模型是它);没有就退到第一个
    带完整覆盖的模型。预置只有 provider 级端点时直接返回那对。
    """
    for model in preset.models:
        if model.api == DEFAULT_API and model.base_url:
            return model.api, model.base_url
    for model in preset.models:
        if model.api and model.base_url:
            return model.api, model.base_url
    return preset.api, preset.base_url


def provider_entry(preset: Preset) -> dict[str, Any]:
    """预置 → `models.json` 里的 provider 段(不含 models,便于合并时分开处理)。

    **只有 `apiKey`**:`api` / `baseUrl` 一律展开写到每个模型条目上 —— 这是 pi 的
    模型目录的形状(连 deepseek 也是每条模型自带),provider 级那条只是默认值。
    好处是规则只有一条("端点看模型"),不用记"哪家要写哪级"。
    """
    return {"apiKey": f"${preset.api_key_env}"}


def model_entry(model: PresetModel, preset: Preset | None = None) -> dict[str, Any]:
    """预置模型 → `models.json` 的模型条目(`contextWindow` + `maxTokens` 都是核过的值)。

    `api` / `baseUrl` **每条都写**:模型自己没写就用 `preset` 的统一值(单端点厂商)。
    这样落盘的模型永远"自解释" —— 读 models.json 的人不用回头翻 provider 段。
    """
    entry: dict[str, Any] = {
        "id": model.id,
        "contextWindow": model.context_window,
        "maxTokens": model.max_output,
    }
    api = model.api or (preset.api if preset is not None else None)
    base_url = model.base_url or (preset.base_url if preset is not None else None)
    if api:
        entry["api"] = api
    if base_url:
        entry["baseUrl"] = base_url
    return entry


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
            models.append(model_entry(model, preset))
            changed.append(f"{preset.provider}/{model.id}")
    return data, changed
