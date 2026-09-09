# 模型配置设计

> 状态:设计讨论中。模型配置**不进入 agent.md**,统一归全局配置(应用配置主题再定文件名/格式,暂以 `[models.*]` 段表示)。

## 1. 设计要点

- 执行与分派各用一个**命名模型**,集中定义、按名使用:
  - `[models.default]`:执行默认,**所有 agent 共用**
  - `[models.router]`:Dispatcher 分派用(小而快)
- 密钥不进配置文件,只存**环境变量名**。
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
| `api_key_env` | | 环境变量名(密钥来源);本地 ollama 可省 |
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

## 4. 查找层级(应用配置主题再细化)

同应用配置:`QI_AGENT_*` 环境变量指定 → 项目 `.qi/` → 用户 `~/.qi/` → 内置默认。
