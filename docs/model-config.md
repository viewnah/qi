# 模型配置设计

> 状态:设计定稿(v1 实现,PLAN A2/A4 已收口)。模型配置**不进入 agent.md**,统一归全局配置 `qi_agent.toml` 的 `[models.*]` 段。

## 1. 设计要点

- 执行与分派各用一个**命名模型**,集中定义、按名使用:
  - `[models.default]`:执行默认,**所有 agent 共用**
  - `[models.router]`:Dispatcher 分派用(小而快)
- 凭证三源,配置文件本身**永不落明文**:
  1. 配置显式 `api_key_env`(特殊变量)
  2. `~/.qi/auth.json`(auth store,0600,按 provider)
  3. 约定环境变量(`DEEPSEEK_API_KEY` 等)
- 若出现「某些角色需要不同模型」的需求,再加 per-agent 覆盖机制(agent.md 字段宽松兼容,加回无痛)。

## 2. v1 字段

```toml
# ~/.qi/qi_agent.toml
[models.default]                    # 执行默认(所有 agent 共用)
provider = "openai"                 # 或 anthropic / deepseek / ollama
model = "…"                         # 模型名
api_key_env = "OPENAI_API_KEY"      # 只存环境变量名,密钥不进文件
base_url = "…"                      # 可选,兼容端点

[models.router]                     # 分派用小而快的模型
provider = "…"
model = "…"
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `provider` | ✅ | `openai` / `anthropic` / `deepseek` / `ollama` |
| `model` | ✅ | 模型名 |
| `api_key_env` | | 环境变量名(可选);缺省按 provider 查 auth store / 约定 env;本地 ollama 可省 |
| `base_url` | | 可选,兼容端点 / 自建网关 |
| `temperature` | | 可选,模型级默认(agent.md 不再声明温度) |

## 3. 待定 / 候选补充字段

参考 hikqin `model.toml` 对照,候选(未定):

| 候选字段 | 说明 |
|---|---|
| `max_tokens` | 单次回复上限(hikqin 示例 65536) |
| `context_length` | 上下文窗口(hikqin 示例 200000) |
| `top_p` / `frequency_penalty` / `presence_penalty` | 采样参数(penalty 两个非必需) |
| `model_type` | `chat` / `completion` 等 |

## 4. 凭证解析(auth store,已定稿)

```
~/.qi/auth.json            # 权限 0600,git 不跟踪
{ "deepseek": { "type": "api_key", "key": "sk-…" } }
```

解析顺序(对齐 pi:store > env):

```
1. 配置显式 api_key_env(该变量)
2. ~/.qi/auth.json 按 provider(如 deepseek)
3. 约定环境变量(DEEPSEEK_API_KEY…)
```

- 写入:`qi auth login <provider>` / `qi init` 引导;删除:`qi auth logout <provider>`;查看:`qi auth list`(只列名)
- 配置文件与 agent 导入包**永不落明文**(会被分享/提交);明文只允许出现在 auth store 或环境变量

## 5. 查找层级(已定稿)

配置文件:`qi_agent.toml`。查找顺序:

```
$QI_AGENT_CONFIG(env 指定文件,最高)
→ <项目>/.qi/qi_agent.toml
→ ~/.qi/qi_agent.toml
→ 内置默认(无)
```

键级深合并,内层覆盖外层;文件不存在则跳过。

## 6. 启动解析流程

```
启动 → 分层装载 qi_agent.toml → pydantic 校验([models.*] 段)
  ├─ [models.default] 必须存在,否则启动失败 + 打印配置指引样例
  ├─ 校验 provider 已知(openai/anthropic/deepseek/ollama…)
  ├─ 校验 api_key_env 指向的环境变量是否设置(ollama/本地免 key)
  └─ [models.router] 缺省 → Router 回退用 default(L1/sticky 也不依赖模型)
```

| 用途 | 模型 |
|---|---|
| 执行 agent(tool-loop) | `[models.default]`(必须) |
| Dispatcher Router(分类) | `[models.router]`,缺省回退 default |
| 密钥/连通检查 | `qi doctor`(按解析顺序验三源) |
| 查看已配 | `qi models list` |
