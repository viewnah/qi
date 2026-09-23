# 供应商与凭证

qi 通过 litellm 调各家模型。**provider 的声明**(baseUrl / api / 模型清单)写在 `models.json`,格式与
分层见 [model-config.md](model-config.md);本文讲**凭证**:从哪来、怎么解析、怎么排查。

**事实源:`src/qi_agent/auth.py` + `config.py` + `paths.py`。**

## 1. 凭证解析顺序

对某个 provider,按这个顺序找密钥(`auth.resolve_key`),**先命中先用**:

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | **auth store** `~/.qi/agent/auth.json` | 按 provider 名存;`qi auth login` 写入 |
| 2 | **约定环境变量** | 见 §2 的表(如 `deepseek` → `DEEPSEEK_API_KEY`) |
| 3 | **`models.json` 里该 provider 的 `apiKey`** | 支持字面量 / `$ENV` / `!command`,见 §3 |
| 4 | — | 都没有 → 该 provider 报"缺密钥" |

**免密钥的 provider**:`ollama`(本地)—— 它 `ok=True`,显示为"无需密钥(本地 provider)"。

排查时会看到的几种状态(`ResolvedKey.describe()`,出现在 `qi --list-models` 与 `qi doctor`):

| 显示 | 含义 |
| --- | --- |
| `auth(ab12cd…)` | 命中了 auth store(只显示前 6 位) |
| `env:DEEPSEEK_API_KEY` | 命中了约定环境变量 |
| `config:$MY_KEY` | 命中了 `models.json` 的 `apiKey` 引用(原样回显写法) |
| `缺失(env:OPENAI_API_KEY(未设置))` | 找到了来源但值为空 —— 这一类最容易被忽略 |
| `缺失(config:…(未解析))` | `apiKey` 写法的语法不成立(命令失败 / 变量缺失 / 引号不配对) |
| `缺失(无密钥来源)` | 既没有约定环境变量,`models.json` 里也没写 `apiKey` |

## 2. 约定环境变量

provider 名 → 自动识别的环境变量(`DEFAULT_API_KEY_ENV`):

| provider | 环境变量 |
| --- | --- |
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `google` / `gemini` | `GEMINI_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `groq` | `GROQ_API_KEY` |
| `mistral` | `MISTRAL_API_KEY` |
| `xai` | `XAI_API_KEY` |
| `dashscope`(阿里云百炼) | `DASHSCOPE_API_KEY` |
| `moonshot`(月之暗面 Kimi) | `MOONSHOT_API_KEY` |
| `zhipu`(智谱 GLM) | `ZHIPUAI_API_KEY` |
| `minimax` | `MINIMAX_API_KEY` |
| `siliconflow`(硅基流动) | `SILICONFLOW_API_KEY` |
| `stepfun`(阶跃星辰) | `STEP_API_KEY` |
| `hunyuan`(腾讯混元) | `HUNYUAN_API_KEY` |
| `volcengine`(火山方舟) | `ARK_API_KEY` |
| `qianfan`(百度千帆) | `QIANFAN_API_KEY` |

下面那批是**预置 provider**(见 §2.1);表里的名字与 `presets.py` 一一对应,有测试锁住。

**这张表只对"名字对得上"的 provider 生效。** 你自己起的 provider 名(如 `my-proxy`)不在表里,要么在
`models.json` 里写 `apiKey`,要么用 `qi auth login my-proxy` —— 不会去猜你的变量名。

### 2.1 预置 provider(国产为主)

qi 自带一份预置表(`src/qi_agent/presets.py`),**两种用法、一套数据**:

**① 兜底(默认行为)** —— `models.json` 里**没写**的预置 provider,`load_config` 会自动补上。
所以零配置也能直接用:

```bash
export DEEPSEEK_API_KEY=sk-…          # 或 qi auth login deepseek
qi config --set defaultProvider=deepseek --set defaultModel=deepseek-v4-pro
qi                                    # 直接跑
```

TUI 里 `/login` 的 provider 选择器列出**全部**预置(带 `(无凭证 · 预置)` 标记)—— 那是登录入口。

**但 `/model`、`ctrl+l/p` 轮换、`/scoped-models`、`qi --list-models` 只列你能用的**:
预置 provider 要**解析得出凭证**(auth store / 约定环境变量 / `apiKey` 引用)才会出现 ——
没登录就不该在切换列表里看到它(想登录就去 `/login`)。`models.json` 里**显式写过**的
provider 不受这条限制:那是你自己的配置,可能正在配。`qi doctor` 列全量并标 `(预置)`。

**`models.json` 里写了同名 provider 就以你写的为准** —— 预置从不覆盖用户的定义(baseUrl /
apiKey / 模型清单全是)。

**② 物化(想改的时候)** —— 把预置**写进** `models.json`,之后这个 provider 就归你说了算:

```bash
qi init --list-presets          # 看有哪些(名字 / baseUrl / 约定环境变量 / 模型 / 注意项)
qi init --preset deepseek       # 写进 models.json,并把它的第一个模型设为默认
qi init --preset deepseek,moonshot --local   # 多个 / 写进项目 .qi/models.json
```

**交互流里也看得到这批**:`qi init` 的 provider 选择器把**没物化的预置**全列出来
(标 `[预置]`,排在已有 provider 之后、`＋ 新建 provider` 之前),选中即走 `apply_presets`
物化(baseUrl / apiKey 引用 / 模型清单先备好,下面几问回车即保留);没选中的预置不写盘。
新建时敲个预置名、或 `-y --provider deepseek`,同样先物化 —— 否则写出的 provider 没有 baseUrl。

“缺凭证”的告警口径只算**在用**的 provider(`models.json` 里写过的 + 默认/路由模型那个)——
预置那十家只是目录,不会让“凭证全部就绪”永远不成立。

预置了什么:**baseUrl + api + 约定环境变量名 + 模型清单**,每个模型都带两个数 ——

- `contextWindow` = 上下文窗口(最大**输入**);
- `maxTokens` = 该模型的最大**输出** tokens(pi `docs/models.md` 里这个字段就是这个语义,
  qi 会把它作为请求的 `max_tokens` 上限发出去)。

**两个数都从厂商自己的文档/模型页核过**(表中括号里就是),核不到的模型**干脆不收** ——
猜一个 `maxTokens` 会让请求要么被拒要么悄悄截断,比没有这一条更糟。想补别的模型:
`qi init --preset <名>` 先物化,再按厂商文档往 `models.json` 里加一条。

`reasoning` **故意不预置**:这些模型多数**默认就带思考**,而 `reasoning_effort` 是否被各家
OpenAI 兼容端点接受并不一致。默认不带参最不容易 400;想开就给该模型加 `"reasoning": true`
(provider 拒收时 qi 会自动去掉并提示一次,见 [tui.md](tui.md) §2)。

覆盖规矩(**不静默改用户写过的东西**):provider 段缺什么补什么(`api` / `baseUrl` / `apiKey`),
已有值不动;模型按 id 追加缺的,同 id 已存在则原样保留(你手调过的 `reasoning` / `contextWindow`
以你为准)。所以再跑一次是幂等的 —— 想拉新模型时更新 qi 后再跑一次即可。

| 预置 | baseUrl | 预置模型(id · ctx / max;**第一个 = 物化后的默认**) |
| --- | --- | --- |
| `deepseek` | `https://api.deepseek.com` | **`deepseek-flash`**(= V4.1 Flash)、`deepseek-v4-pro`(均 1M / 384K) |
| `dashscope` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | **`qwen3.7-plus`**(官方推荐:均衡 + 完整工具调用)、`qwen3.8-max`、`qwen3.8-flash`(均 1M / 128K) |
| `mimo` | `https://api.xiaomimimo.com/v1` | **`mimo-v2.6-flash`**(官方定位:高智能、低成本,排第一)、`mimo-v2.6-pro`、`mimo-v2.6-pro-ultraspeed`(速度档;均 1M / 128K;旧的 `mimo-v2.5*` **2026-10-21 下线**) |
| `moonshot` | `https://api.moonshot.cn/v1` | **`kimi-k3`**(1M / 128K)、`kimi-k2.7-code`(256K / 32K) |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4` | **`glm-5.2`**(1M / 128K)、`glm-4.7`(200K / 128K) |
| `minimax` | `https://api.minimax.cn/v1` | **`MiniMax-M3`**(1M / 128K) |
| `siliconflow` | `https://api.siliconflow.cn/v1` | **`deepseek-ai/DeepSeek-V4-Flash`**(1M / 384K) |
| `stepfun` | `https://api.stepfun.com/v1` | **`step-3.7-flash`**(256K / 256K) |
| `hunyuan` | `https://tokenhub.tencentmaas.com/v1` | **`hy4-preview`**(1M / 64K)、`hy3`(256K / 128K) |
| `volcengine` | `https://ark.cn-beijing.volces.com/api/v3` | **`doubao-seed-evolving`**(统一 id,周更自动迭代)、`doubao-seed-2-1-pro-260628`(均 256K / 128K) |
| `qianfan` | `https://qianfan.baidubce.com/v2` | **`ernie-5.1`**、`ernie-5.0`(均 128K / 64K) |

这张表是 **2026-09 逐个厂商模型页核过的快照**(各条注释里写了出处)。换世代时它会过时 ——
`qi init --refresh <名>` 就是为此准备的;`tests/test_presets.py` 里有一条快照测试,
厂商换线时会红,提醒去核对而不是把断言改宽。

**③ 刷新(以厂商接口为准)** —— 种子会过时,接口不会:

```bash
qi init --refresh deepseek           # GET {baseUrl}/models → 并进 models.json
qi init --refresh deepseek,moonshot
qi init --refresh-all                # 所有已配置(有凭证)的 provider
qi init --refresh deepseek --local   # 写项目 .qi/models.json
```

交互流里有同一条入口:`qi init` → `--- Add Models ---` 菜单选
**`↻ Refresh model list (GET /models)`**(与 `＋ Add a model` 同级,共用
`_refresh_entry_models`):失败只报错、回菜单不中断 init。

> 为什么挂在 `qi init` 下:`qi models` 子命令在本仓是**故意删掉的**(对齐 pi —— 列清单用
> `--list-models`),所以“刷新模型清单”跟 `--preset` 一样留在 `qi init` 这个“把 provider / 模型
> 写进 `models.json`”的家族里。`qi doctor` 与 `--list-presets` 的输出里都会提示这条。

为什么需要它(实测):DeepSeek 把当前模型名从 `deepseek-v4-flash` 换成了 **`deepseek-flash`**
(V4.1 Flash,旧名仍存在但请求被路由到新模型),而 `deepseek-v4-pro` 官方计划下线 ——
这种变更只能靠公告或接口,写死的表迟早落后一拍。

刷新规矩:

- **只加不删**:接口回来的新 id 追加进去;本地有、接口没返回的**保留并打印出来**
  (厂商的 `/models` 有可能只列有权限的模型、或者列不全,不该替用户删配置);
- **新加的条目只写 `id`** —— `/models` 不返回 `contextWindow` / `maxTokens`,先用 qi 的默认值,
  要精确就照厂商文档在 `models.json` 里补(种子里的那两个数在刷新时**不会被覆盖**);
- **写回「定义它的那个文件」**:provider 写在项目 `.qi/models.json` 就刷到那儿、写在用户级就刷到
  用户级;只有“预置兜底来的”(哪个文件都没定义)才物化到用户级 —— 否则会在别处长出一份同名定义,
  而合并取高优先级的那个,于是“刷新了却看不到效果”;
- 鉴权用**同一个 API key**(auth store / 约定环境变量 / `apiKey` 引用);拿不到 key 就报错、**不写文件**;
- 只认 `http(s)` 的 `baseUrl`(自建代理照常),网络失败/HTTP 错误都报清楚并且**不落盘**。

数过时的注意项(与 `--list-presets` 里打印的一致):

- `dashscope`:北京新账号可能要换成工作空间专属域名
  `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`;
- `volcengine`:方舟的模型 id 带日期后缀(会变),自定义部署还得用 Endpoint ID ——
  以控制台“模型列表”里的调用名为准;
- `hunyuan`:腾讯正把混元迁到 TokenHub(新 id 形如 `hy3` / `hy4-preview`),表里是旧平台的
  兼容端点;
- `siliconflow` 是聚合平台,模型 id 带组织前缀(`zai-org/GLM-5.2`、`Qwen/…`、`Pro/…`),
  与各家的官方 id 不同;
- `mimo`:按量付费用 `api.xiaomimimo.com`,订阅套餐(Token Plan)是
  `https://token-plan-cn.xiaomimimo.com/v1` 且 Key 前缀不同 —— 两家 token 不能混用;
  另外 `mimo-v2.5` / `mimo-v2.5-pro` 官方公告 **2026-10-21 10:00 下线**(种子已切到
  `mimo-v2.6-flash` / `mimo-v2.6-pro` / `mimo-v2.6-pro-ultraspeed` —— ultraspeed 是速度档,
  限流为定制服务,调它之前先问平台);
- `zhipu`:`GLM-5.3` 官方页面写着「API 即将上线」(Bailian 已可调),所以种子里是线上可用的
  `GLM-5.2`;上线后 `qi init --refresh zhipu` 拉一下就有;
- `hunyuan`:腾讯已把混元迁到 **TokenHub**(`https://tokenhub.tencentmaas.com/v1`,Key 也从
  TokenHub 控制台拿);旧平台 `https://api.hunyuan.cloud.tencent.com/v1` + `hunyuan-turbos-latest`
  仍可用但**Key 不通用**,且腾讯说旧平台不再新增模型;
- `volcengine`:普通模型 id 带日期后缀(会变),`doubao-seed-evolving` 是**版本无关**的统一 id
  (官方周更自动迭代、不用改配置),所以排第一。

**模型 id 会漂** —— 所以还有第三条路:**从接口拉**(见下)。表里的清单是核过厂商文档的快照。

## 3. `models.json` 里的 `apiKey` 写法

`apiKey` 的值按**前缀**解析:

| 写法 | 行为 |
| --- | --- |
| `"sk-abc123"` | 字面量 |
| `"$MY_KEY"` / `"${MY_KEY}"` | 环境变量插值。**变量缺失 → 整个值解析失败**(不返回半截,也不会变成空串去发请求) |
| `"!command -args"` | 执行命令,取 **stdout**(strip 后);非零退出 / 超时(30s)/ 引号不配对 → 解析失败 |
| `"$$literal"` / `"$!literal"` | 转义:得到字面 `$` / `!` 开头的字符串 |

`!command` 的三种常见写法:

```jsonc
"apiKey": "!security find-generic-password -ws 'anthropic'"   // 引号由 shlex 处理
"apiKey": "!op read 'op://vault/item/credential'"
"apiKey": "!bash -lc 'cat /tmp/k | tr -d \\n'"                // 需要管道时显式起 shell
```

**安全边界**:`!command` **不走 shell** —— 它被 `shlex` 拆成参数后以 `shell=False` 执行,所以没有
shell 注入面。代价是**不能直接写 `!cat a | jq -r .key`**(管道会被当成普通参数);要管道就显式写
`!bash -lc "…"`,把那层 shell 变成一个看得见的选择。

求值时机只有一处:**解析你自己写的 `apiKey` 时**。agent 或模型内容无法触发它(与 `auth.json` 同属
你的配置信任域)。

## 4. auth store:`~/.qi/agent/auth.json`

```json
{
  "deepseek": { "type": "api_key", "key": "sk-…" },
  "my-proxy": { "type": "api_key", "key": "…" }
}
```

- 写入时 `chmod 0600`(Windows 上无 posix 权限,忽略失败)。
- **文件损坏或不是对象 → 当作空**(`{}`),不报错、不影响启动。凭证读不出来时表现为"缺密钥",
  而不是把 qi 挡在门外。
- 只存 `type: api_key` 这一种。**qi 没有 OAuth**(见 §6)。

## 5. 命令行

| 命令 | 作用 |
| --- | --- |
| `qi auth login <provider>` | **输入可见**地写入 API key(auth store);TUI 里的 `/login` 是**遮罩**输入(见 [tui.md](tui.md) §2) |
| `qi auth logout <provider>` | 删除该 provider 的凭证 |
| `qi auth list` | 列出 auth store 里**有凭证**的 provider |
| `qi auth print-api-key <provider>` | 打印解析到的 key(用于喂给别的工具) |
| `qi auth print-bearer-token <provider>` | 同上 —— qi 没有 OAuth,所以"bearer token"就是 API key |
| `qi auth check <provider>` | 检查该 provider 能否解析出凭证 |
| `qi auth rm <provider>` | 同 `logout` |
| `qi --list-models` | 列出 provider / 模型,并显示每个 provider 的凭证状态 |
| `qi doctor` | 逐个 provider 打印凭证状态(`OK` / `缺密钥`)+ 来源 |

`qi auth print-bearer-token` 接受 `--no-refresh` 参数(为了与 pi 的命令面一致),但 qi 没有 OAuth,
所以**它是空操作**。

## 6. 环境变量总表

qi 自己识别的变量分两类。

**配置类(4 个)**:

| 变量 | 作用 |
| --- | --- |
| `QI_CONFIG_DIR` | 名字空间根(默认 `~/.qi`)。仅用于推导默认路径与迁移 |
| `QI_AGENT_HOME` | **agent 目录本身**(默认 `~/.qi/agent`)。全部用户级状态的挂载点:`settings.json` / `models.json` / `auth.json` / `sessions` / `skills` / `agents` / `extensions`。语义同 pi 的 `PI_CODING_AGENT_DIR` |
| `QI_AGENT_CONFIG` | **直接指定 `models.json` 文件**(优先于项目 / 全局路径) |
| `QI_THEME` | `dark` / `light` / `auto`,覆盖 `settings.theme`(见 [themes.md](themes.md)) |

**会话类(5 个)—— 只出现在 bash / powershell 的**子进程**里,不是给用户设的**:

| 变量 | 作用 |
| --- | --- |
| `QI_SESSION_ID` | 当前会话 id |
| `QI_SESSION_FILE` | 会话 JSONL 的绝对路径 |
| `QI_PROVIDER` / `QI_MODEL` | 当前模型 |
| `QI_REASONING_LEVEL` | 当前思考级别 |

它们由 `Runtime._session_env()` 生成、`merged_env()` 注入(对齐 pi 的
`exposeSessionEnvironment`),**先删后填**所以不会从父进程继承。细节见 [tools.md](tools.md) §3.1。

`models.json` 的完整查找顺序是 `QI_AGENT_CONFIG` → `<项目>/.qi/models.json` → `~/.qi/agent/models.json`,
细节见 [model-config.md](model-config.md)。

> `QI_AGENT_HOME` 指向的是**目录本身**,不是它的父目录 —— 拿它去拼 `agent/settings.json` 会得到
> `…/agent/agent/settings.json`。这是这个变量最常见的误用。

## 7. 与 pi 的差异

| | pi | qi |
| --- | --- | --- |
| 凭证来源顺序 | auth store → 约定环境变量 → `models.json` 引用 | **同** |
| `apiKey` 值语法 | `!cmd` / `$ENV` / 转义 | **同**(`!cmd` 同样不走 shell) |
| 订阅登录(Claude Pro/Max、Copilot、Codex、xAI) | **支持**(OAuth,带刷新) | **不支持** —— 只有 API key。`print-bearer-token` 与 `--no-refresh` 为兼容 pi 命令面而存在,语义是空操作 |
| auth store 结构 | `type` 支持多种(含 OAuth 凭证) | 只有 `type: api_key` |
| 云 provider(Azure / Bedrock / Vertex / Cloudflare) | 有独立章节 | 走 litellm 通用 provider 配置,没有专门文档 |

**没有 OAuth 的直接后果**:用订阅额度(而非 API key)的用户在 qi 上跑不通,必须换成 API key。
要补这块,需要的是 auth store 的 `type` 扩展 + 刷新逻辑 —— 现在没有,所以本文不描述不存在的机制。
