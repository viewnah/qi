# 选一个模型

qi 通过 litellm 调各家模型。provider 与模型定义都在 `models.json`;**默认模型**在 `settings.json`。
凭证明文只进 `auth.json` 或环境变量,`models.json` 只允许**引用**。

## 选一条连接

| 想怎么接 | 用什么 |
| --- | --- |
| 用 qi 自带的**预置**(国产为主:baseUrl + 约定环境变量 + 核过的模型清单) | `qi init --preset <名>` 或交互式 `qi init` |
| 自己写一个 provider(自建代理 / 聚合平台) | 编辑 `models.json` 的 `providers` 段,或 `qi init -y` |
| 本地无鉴权端点(Ollama / llama.cpp / LM Studio) | 写 provider + 一个字面 `apiKey`(如 `"none"`) |
| 按次换模型,不写配置 | `qi --provider <名> --model <provider/模型>` |

## 认证

凭证解析顺序(auth store → 约定环境变量 → `models.json` 的 `apiKey` 引用)、约定变量表与排查口径见
[providers.md](providers.md)。`qi auth login <provider>` 写 `auth.json`(0600);TUI 里是 `/login`。

## 选默认模型

```jsonc
// ~/.qi/agent/settings.json
{ "defaultProvider": "deepseek", "defaultModel": "deepseek-chat" }
```

- **唯一来源是 `settings.json`**:`models.json` 里的 `defaultProvider` / `defaultModel`
  **完全不参与**(不是优先级更低),写了会被忽略并在 `qi doctor` 里报出来。
- 缺任一项时:**交互界面(TUI)照常启动**,顶部提示用 `/login` 登录 provider(或用 `qi init` 引导),
  真正提交消息才报错;无头路径(`-p` / `--mode json`)直接报错退出,并给出可照抄的迁移命令
  —— 与 pi 的 interactive / 非 interactive 同一取舍。
- 查看:`qi --list-models [搜索词]`(缺密钥的也列,这是诊断面;解析不出凭证的 provider 不列)。
- **TUI 里换模型**:`/model`(或 `ctrl+l`)选择器里 `enter` 只换**本会话**(落一条 `model_change`,
  续接这条会话才还原);按 **`ctrl+s`** 则连**默认一起改** —— 写回 `settings.json` 的
  `defaultProvider` / `defaultModel`,下次**新会话**就是它(对齐 pi 的 `app.models.save`)。
- **登录时顺手定默认**:`/login <provider>` 时如果**还没有模型**,qi 会选中该 provider 的
  第一个模型并**写成全局默认**(对齐 pi 的 `completeProviderAuthentication` 用 `persist: true`)
  —— 所以第一次登录后重启也还是它。已经有模型时不洗掉当前选择(只提示“当前模型不变”)。

## 接本地模型

本地端点不需要密钥,但要让它**进 `/model` 与 `ctrl+p` 清单**:

```jsonc
{ "providers": { "local": {
    "baseUrl": "http://localhost:1234/v1",
    "api": "openai-completions",
    "apiKey": "none",                       // 字面值 —— resolve_key 认它,请求只是多一个 Authorization 头
    "models": [{ "id": "qwen3-8b", "contextWindow": 32768, "maxTokens": 4096 }]
} } }
```

`ollama` 是唯一**免密钥**的内置特例(显示为"无需密钥(本地 provider)")。

## 配一个兼容端点

```jsonc
{
  "providers": {
    "deepseek": {
      "baseUrl": "https://api.deepseek.com/v1",
      "api": "openai-completions",
      "apiKey": "$DEEPSEEK_API_KEY",
      "headers": {},
      "models": [
        { "id": "deepseek-chat", "name": "DeepSeek Chat",
          "reasoning": false, "input": ["text"],
          "contextWindow": 128000, "maxTokens": 16384 }
      ]
    }
  }
}
```

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `providers.<name>.baseUrl` | | | API endpoint |
| `providers.<name>.api` | | `openai-completions` | `openai-completions` / `openai-responses` / `anthropic-messages` / `google-generative-ai` |
| `providers.<name>.apiKey` | | | 引用:字面量 / `$ENV` / `${ENV}` / `!command`;省略则查 auth store / 约定环境变量 |
| `providers.<name>.headers` | | | 自定义请求头,值语法同 `apiKey` |
| `providers.<name>.models[]` | | | 模型列表,每个至少 `id` |
| `models[].reasoning` | | `false` | 是否支持扩展思考(决定要不要下发 `reasoning_effort`) |
| `models[].contextWindow` | | `128000` | 上下文窗口 token(自动压缩的阈值基于它) |
| `models[].maxTokens` | | `16384` | 最大输出 token(作为请求的 `max_tokens` 上限) |
| `models[].input` | | `["text"]` | 输入类型 |

模型条目可缺省,`resolve_model()` 会用 provider 默认 + 内置默认补齐。

**`apiKey` 值语法**:

| 写法 | 行为 |
| --- | --- |
| `"sk-abc"` | 字面量 |
| `"$ENV"` / `"${ENV}"` | 环境变量插值;**缺失 = 整个值解析失败**(不返回半截) |
| `"!command args"` | 执行命令取 stdout;非零退出 / 超时(30s)/ 引号不配对 = 失败。**不走 shell**(`shlex` 拆参 + `shell=False`) |
| `"$$x"` / `"$!x"` | 转义:得到字面 `$x` / `!x` |

## 手写还是让 `qi init` 写

`qi init` 引导默认模型,三步:**选 provider**(已有 / 预置 `[预置]` / 新建 → Base URL → API 类型 →
API Key,直接写 `auth.json`)→ **Add Models**(`＋ Add a model` / `↻ Refresh model list (GET /models)` /
`✓ Done` 循环)→ **Activate LLM Model**(写 `defaultProvider` / `defaultModel` 进 `settings.json`)。
`-l` 写项目 `<项目>/.qi/models.json`(否则全局)。

```bash
qi init                       # 交互式
qi init --list-presets        # 看内置预置 provider
qi init --preset deepseek     # 物化预置(写 models.json)+ 设为默认;可逗号写多个;幂等,不覆盖你写过的值
qi init --refresh deepseek    # 拉厂商 /models 对齐本地清单(只加不删)

qi init -y --provider deepseek --model deepseek-chat \
    --base-url https://api.deepseek.com/v1 --api openai-completions --api-key sk-xxx
```

| 选项 | 说明 |
| --- | --- |
| `-y, --yes` | 非交互:必须配 `--provider`(与 `--model`) |
| `--provider <name>` / `--model <id>` | provider 名(已有或新建)/ 模型 id |
| `--base-url <url>` / `--api <type>` | endpoint / API 形态 |
| `--api-key <key>` / `--api-key-env <VAR>` | 写 `auth.json` / 在 `models.json` 里引用环境变量 |
| `--reasoning` / `--no-reasoning` | 模型是否支持扩展思考 |
| `--context-window <int>` / `--max-tokens <int>` | 两个元数据(默认 128000 / 16384) |
| `-l, --local` | 写项目 `.qi/models.json` |

已有模型按 id upsert,重跑安全;`--preset` / `--refresh` 都**只补缺不覆盖**。

## 查找层级

```text
models.json:  $QI_AGENT_CONFIG(env 指定文件,最高)
              → <项目>/.qi/models.json
              → ~/.qi/agent/models.json

默认模型:     <项目>/.qi/settings.json > ~/.qi/agent/settings.json(唯一来源)
```

键级深合并,内层覆盖外层;文件不存在则跳过。provider 按名覆盖,`models` 数组**整体替换**。

## 排错

| 现象 | 先看 |
| --- | --- |
| 模型不在 `/model` 或 `ctrl+p` 清单里 | 清单只列**解析得出凭证**的 provider —— 跑 `qi --list-models` 看 `credential` 列 |
| "我明明设了环境变量" | `qi doctor` 会给出凭证**来源**(`auth(...)` / `env:XXX` / `config:...`),区分"没读到"与"读到了但为空" |
| provider 拒收 `reasoning_effort` | qi 会自动去掉参数重试并在 footer 提示一次;也可把该模型的 `reasoning` 改成 `false` |
| 回合失败出现 `'ascii' codec can't encode` | **API key 里有非 ASCII 字符**(多半是把一段中文提示误粘进了密钥框)。现在 qi 在请求前就报“API key 含非 ASCII 字符(来源:…)”;修:`qi auth list` 看来源 → 重新 `qi auth login <provider>` / 改环境变量 / 改 `models.json` 的 `apiKey` |
| 相容端点返回 4xx | 先确认 `api` 形态选对(`openai-completions` vs `openai-responses` vs `anthropic-messages`) |
