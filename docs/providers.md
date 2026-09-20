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

**这张表只对"名字对得上"的 provider 生效。** 你自己起的 provider 名(如 `my-proxy`)不在表里,要么在
`models.json` 里写 `apiKey`,要么用 `qi auth login my-proxy` —— 不会去猜你的变量名。

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
| `qi auth login <provider>` | **输入可见**地写入 API key(auth store) |
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

qi 自己识别的变量只有 4 个:

| 变量 | 作用 |
| --- | --- |
| `QI_CONFIG_DIR` | 名字空间根(默认 `~/.qi`)。仅用于推导默认路径与迁移 |
| `QI_AGENT_HOME` | **agent 目录本身**(默认 `~/.qi/agent`)。全部用户级状态的挂载点:`settings.json` / `models.json` / `auth.json` / `sessions` / `skills` / `agents` / `extensions`。语义同 pi 的 `PI_CODING_AGENT_DIR` |
| `QI_AGENT_CONFIG` | **直接指定 `models.json` 文件**(优先于项目 / 全局路径) |
| `QI_THEME` | `dark` / `light` / `auto`,覆盖 `settings.theme`(见 [themes.md](themes.md)) |

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
