# 模型配置设计

> 状态:v1 实现。模型配置**不进入 agent.md**,统一归 `models.json`(格式对齐 pi)。
> 凭证明文只进 `auth.json` 或环境变量,`models.json` 只允许引用。

## 1. 设计要点

- 执行与分派各用一个**默认模型**,集中定义、按名使用:
  - `defaultProvider` / `defaultModel`:执行默认,**所有 agent 共用**
  - `routerProvider` / `routerModel`:Dispatcher 分派用(小而快);缺省回退 default
- 文件与格式对齐 pi:
  - `~/.qi/models.json`:provider + 模型定义(`baseUrl` / `api` / `apiKey` / `models`)
  - `~/.qi/auth.json`:凭证(0600,按 provider),与 pi 同格式
- 凭证解析顺序(对齐 pi):
  1. `~/.qi/auth.json` 按 provider
  2. 约定环境变量(`DEEPSEEK_API_KEY` 等)
  3. provider 的 `apiKey` 引用(字面量 / `$ENV` / `!command`)
- 本地 provider(ollama)免 key。

## 2. models.json 格式

> qi **不预置任何 provider 名录**。provider(含 `baseUrl`/`api`)与模型均由用户或以后的插件提供;内置 provider 预置将改为插件实现。

```json
{
  "defaultProvider": "deepseek",
  "defaultModel": "deepseek-chat",
  "providers": {
    "deepseek": {
      "baseUrl": "https://api.deepseek.com/v1",
      "api": "openai-completions",
      "apiKey": "$DEEPSEEK_API_KEY",
      "headers": {},
      "models": [
        {
          "id": "deepseek-chat",
          "name": "DeepSeek Chat",
          "reasoning": false,
          "input": ["text"],
          "contextWindow": 128000,
          "maxTokens": 16384
        }
      ]
    }
  }
}
```

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `defaultProvider` / `defaultModel` | ✅ | — | 执行默认模型(pi 放在 settings.json,qi 放在本文件) |
| `routerProvider` / `routerModel` | | 回退 default | Dispatcher 分派模型 |
| `providers.<name>.baseUrl` | | | API endpoint |
| `providers.<name>.api` | | `openai-completions` | `openai-completions` / `openai-responses` / `anthropic-messages` / `google-generative-ai` |
| `providers.<name>.apiKey` | | | 引用:字面量 / `$ENV` / `${ENV}` / `!command`;省略则查 auth store / 约定 env |
| `providers.<name>.headers` | | | 自定义请求头,值语法同 `apiKey` |
| `providers.<name>.models[]` | | | 模型列表,每个至少 `id` |
| `models[].reasoning` | | `false` | 是否支持扩展思考 |
| `models[].contextWindow` | | `128000` | 上下文窗口 token |
| `models[].maxTokens` | | `16384` | 最大输出 token |
| `models[].input` | | `["text"]` | 输入类型 |

模型条目可缺省;`resolve_model()` 会用 provider 默认 + 内置默认补齐。

## 3. apiKey 值语法(与 pi 一致)

- `!command`:执行命令取 stdout(失败/非零 = 未解析)
- `$ENV` / `${ENV}`:环境变量插值;缺失 = 未解析
- `$$` / `$!`:转义为字面量 `$` / `!`
- 其它:字面量

## 4. 凭证存储(auth.json,与 pi 同格式)

```json
{ "deepseek": { "type": "api_key", "key": "sk-…" } }
```

- 写入:`qi auth login <provider>` / `qi init` 引导
- 删除:`qi auth logout <provider>`;查看:`qi auth list`(只列名)
- 权限 0600;`models.json` 与 agent 导入包**永不落明文**

## 5. 查找层级

```
$QI_AGENT_CONFIG(env 指定文件,最高)
→ <项目>/.qi/models.json
→ ~/.qi/models.json
→ 内置默认(无)
```

键级深合并,内层覆盖外层;文件不存在则跳过。provider 按名覆盖,`models` 数组整体替换。

## 6. 启动解析流程

```
启动 → 分层装载 models.json → pydantic 校验
  ├─ defaultProvider/defaultModel 必须存在,否则启动失败 + 打印配置指引样例
  ├─ 校验 api 已知(openai-completions/openai-responses/anthropic-messages/google-generative-ai)
  ├─ 按 pi 顺序解析密钥(auth store → 约定 env → apiKey 引用;ollama 免 key)
  └─ router* 缺省 → Router 回退 default(L1/sticky 也不依赖模型)
```

| 用途 | 模型 |
|---|---|
| 执行 agent(tool-loop) | `defaultProvider/defaultModel`(必须) |
| Dispatcher Router(分类) | `routerProvider/routerModel`,缺省回退 default |
| 密钥/连通检查 | `qi doctor` |
| 查看已配 | `qi models list` |
| 引导配置 | `qi init`(复刻 QwenPaw:Provider Configuration → Add Models → Activate LLM Model;上下键选择 + 可见输入;凭证写 auth.json;已有凭证回车保留) |

## 7. qi init 用法

`qi init` 引导配置默认模型,流程与样式复刻 QwenPaw 的 `qwenpaw init`。

### 交互流程

```
Working dir: ~/.qi

=== LLM Provider Configuration ===
--- Provider Configuration ---
? 选择 provider                       # 上下键:已有 provider(带 [✓]/[✗])+ ＋ 新建
  ❯ deepseek [✓]
    ＋ 新建 provider
Provider name: <新 provider 名>       # 仅新建时
Base URL (OpenAI-compatible endpoint): <必填>
? API 类型                            # 上下键
  ❯ openai-completions
    openai-responses
    anthropic-messages
    google-generative-ai
<provider> API key: <可见输入>          # 已有凭证回车保留
✓ deepseek — API Key: sk-d...45, Base URL: https://api.deepseek.com/v1

--- Add Models ---
Current models for deepseek:          # 或 No models configured
  - DeepSeek Chat (deepseek-chat)
? Add a model?                        # 上下键;已有模型时默认「否」
Model identifier: deepseek-chat
Model display name [deepseek-chat]:
? Supports reasoning (扩展思考)?
contextWindow [128000]:
maxTokens [16384]:
✓ Model 'deepseek-chat' (deepseek-chat) added.
? Add a model?                        # 循环

? Configure another provider?         # 上下键,默认「否」

--- Activate LLM Model ---
? Select provider for LLM            # 上下键(带 [✓]/[✗])
  ❯ deepseek [✓]
? Select LLM model                   # 上下键
  ❯ DeepSeek Chat
✓ LLM: deepseek / deepseek-chat
✓ Configuration saved to ~/.qi/models.json

✓ Initialization complete!
```

要点:

- 全程**上下键选择**,只有 provider 名 / Base URL / 模型 id / API Key 等必填项才手动输入。
- `Base URL` 必填;已有 provider 的 `Base URL` / `API 类型` 直接回车保留。
- `API Key` **可见输入**(不隐藏),方便确认复制成功;已有凭证回车保留。
- `Add a model?` 循环逐个添加模型,含 `name` / `reasoning` / `contextWindow` / `maxTokens`,后三者有默认值回车即接受(已有模型默认「否」)。
- 最后 `Activate LLM Model` 写入 `defaultProvider` / `defaultModel`。
- 写盘:`models.json`(provider + 模型 + 默认模型)+ `auth.json`(API key,0600)。
- `-l` 写入项目 `<项目>/.qi/models.json`(否则全局 `~/.qi/`)。

### 非交互(`-y`)

```bash
# 新建/更新 provider 与模型,并设为默认
qi init -y --provider deepseek --model deepseek-chat \
    --base-url https://api.deepseek.com/v1 --api openai-completions \
    --api-key sk-xxx

# 用环境变量引用凭证(写入 models.json 的 apiKey,而不是 auth.json)
qi init -y --provider deepseek --model deepseek-chat --api-key-env DEEPSEEK_API_KEY

# 指定模型元数据
qi init -y --provider my-llm --model my-model \
    --base-url http://localhost:1234/v1 --api openai-completions \
    --reasoning --context-window 64000 --max-tokens 8000

qi init -y -l ...   # 写项目 .qi/
```

`-y` 模式必须提供 `--provider`(否则报错并提示先用交互模式)。

| 选项 | 说明 |
|---|---|
| `--provider <name>` | provider 名(已有或新建) |
| `--model <id>` | 模型 id |
| `--base-url <url>` | provider 的 API endpoint |
| `--api <type>` | `openai-completions` / `openai-responses` / `anthropic-messages` / `google-generative-ai` |
| `--api-key <key>` | 写入 `auth.json` |
| `--api-key-env <VAR>` | 在 `models.json` 引用环境变量 |
| `--reasoning/--no-reasoning` | 模型支持 reasoning |
| `--context-window <int>` | 上下文窗口 token(默认 128000) |
| `--max-tokens <int>` | 最大输出 token(默认 16384) |
| `-l, --local` | 写项目 `.qi/models.json` |

### 二次运行

已在配置好的 `models.json` 上重跑 `qi init` 是安全的:选已有 provider 时 `Base URL` / `API 类型` / `API Key` 均可直接回车保留,`Add a model?` 默认「否」,最后可重新选择默认模型;已有模型按 id upsert,不会重复。
