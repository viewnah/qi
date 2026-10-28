# 配置、模型、技能、扩展与凭证

前五个是**只读视图**(设置页的数据源),后面是写操作(新建 provider / 凭证;带确认头)。
通用约定见 [`README.md`](README.md)。

## `GET /api/config`

provider / 凭证状态 / 默认模型的只读视图。**凭证只回掩码与来源,永不回明文。**

### 响应 `ConfigView` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `providers` | `ProviderInfo[]` | 见下 |
| `default_model` | `string \| null` | **带 provider 前缀**的完整标签(诊断面用) |
| `default_model_name` | `string` | 不带前缀的模型名,**给展示用**(输入卡右下那行放不下 `provider/model`,而 id 自己可能还带斜杠) |
| `default_model_source` | `string` | 有默认模型时为 `"settings"` |
| `default_model_context_window` | `int` | 上下文窗口(tokens)。`0` = 取不到 → 前端不画占用条(别除零) |
| `router_model` | `string \| null` | 路由模型标签;与默认相同则为 `null` |
| `settings_files` / `config_files` | `string[]` | 实际读了哪些文件(诊断) |
| `session_dir` | `string` | 会话目录 |
| `auth_file` | `string` | **凭证文件绝对路径** —— 二次确认弹层要显示"改的是哪个文件" |
| `models_write_file` | `string` | 新建 provider 写哪个用户级 `models.json`(弹层里显示目标文件) |
| `checks` | `object[]` | `{name, ok, detail}` 三条:`models.json` / `默认模型` / `凭证` |

`ProviderInfo`:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `string` | provider 名 |
| `label` | `string` | 预置 provider 的人读名字(自定义 provider 为空) |
| `base_url` / `api` | `string \| null` | |
| `models` | `int` | 该 provider 下配了几个模型 |
| `model_ids` | `string[]` | 模型 id 清单 —— 设置页按 provider → model 两级展示 |
| `credential_ok` | `bool` | 凭证解析得出(**解析** = settings/环境变量/auth 文件三层都能供) |
| `credential_source` | `string` | 凭证来自哪一层:`auth` / `env:<变量名>`(未设置时带 `(未设置)`)/ `config:<apiKey 引用>`(未解析时带 `(未解析)`)/ `none`(provider 免 key)/ `无密钥来源`(解析不出) |
| `credential_masked` | `string` | **只有尾 4 位**:`…abcd`;空 = 没有 |

```bash
curl -s http://127.0.0.1:3456/api/config | jq '{default_model, checks, providers: [.providers[] | {name, credential_ok}]}'
```

## `GET /api/models`

可选模型清单 + 当前值。**口径与 TUI 的 `/model` 完全一致**:都走 core 的
`selectable_models()` —— **解析得出凭证的 provider 才列**(`models.json` 里显式写过、
但 `$ENV` 没导出的也不算)。两处各写一份清单,迟早一个改了另一个没改。

### 查询参数

| 名 | 类型 | 说明 |
| --- | --- | --- |
| `session` | `string` | 给了就**先绑定它** —— 当前值要反映那条会话的还原结果,而不是该 runtime 上残留的上一轮状态(每个会话一个 runtime)。不存在 → 404 |

### 响应 `ModelCatalog` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `models` | `ModelOption[]` | `{provider, id, context_window}` —— **没有**"是否当前"字段:当前是**会话级**的 |
| `currently` | `string` | `provider/id`;空 = 没有可用模型(界面据此禁用菜单,而不是画空面板) |
| `thinking_level` | `string` | 当前思考级别 |
| `thinking_levels` | `string[]` | 合法取值(**界面照它画那一行,不自己写死一份**) |

## `POST /api/model`

换模型 / 换思考级别(下一回合生效)。**返回新的清单**而不只是 204:界面要在同一次往返里
把 chip 上的字与菜单里的 ✓ 一起更新掉,否则它会显示"已经切了"而实际没切。

### 请求体 `ModelSelect`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `provider` | `string \| null` | 与 `model` **必须成对** |
| `model` | `string \| null` | 同上 |
| `thinking_level` | `string \| null` | 见下 |
| `session` | `string \| null` | 作用在哪个会话上:换模型要落进**那个**文件。不给 = 只改 runtime 当前状态(不落盘) |

两个字段都是可选的、**可以只给一个** —— 让"只换级别"和"只换模型"各是一条请求,
不必为了改一个值把另一个也传上来(那会覆盖掉会话里刚还原的值)。

`thinking_level` 合法值(`qi_agent.llm.THINKING_LEVELS`,照 `/api/models` 的
`thinking_levels` 走):

```
off  minimal  low  medium  high  xhigh  max
```

### 响应 `ModelCatalog` (200,与 GET 同形状)

| 码 | 什么时候 |
| --- | --- |
| 422 | 只给了 `provider` 或只给了 `model`(切到半个模型);`provider`+`model` 与 `thinking_level` 一个都没给;`thinking_level` 不在合法集合 |
| 400 | `set_model()` 失败(配置/凭证问题)—— 配置错误转 400 而不是 500 |
| 404 | `session` 给了但会话不存在 |

**顺序不能反**:带 `session` 时**先绑再设** —— 绑定会按会话里的 entry 还原模型,
于是"这次没传的字段"保持会话里的值,而不是被上一次绑定的残留顶掉。

```bash
curl -s -X POST http://127.0.0.1:3456/api/model \
  -H 'Content-Type: application/json' \
  -d '{"provider":"anthropic","model":"claude-sonnet-4-5","session":"<sid>","thinking_level":"high"}' \
  | jq '{currently, thinking_level}'
```

## `GET /api/skills`

顶层技能(六层来源 + settings 追加),设置页展示用。

> 技能的**写端点**(新建 / 删除 / 导入)与这个读端点的完整说明都搬去了
> [`skills.md`](skills.md) —— 那一节要讲可写边界与三条不变量,与配置只读视图不是一件事。

### 响应 `SkillList` (200)

`skills[]`:`{name, description, source, path}`。

## `GET /api/extensions`

已装载的扩展名。**只回名字** —— `discover_extensions()` 的职责是装载不是描述,
不编造版本/作者这类拿不到的信息。

### 响应 `ExtensionList` (200)

```json
{"extensions": ["lab", "agents", "mcp"]}
```

> qi-mcp 是项目声明、仓库未信任时它可能不在这个列表里,但 `GET /api/mcp` 照样能用
> (那是直接 import `qi_mcp.config`,不经过扩展装载)。两件事别混。

---

## `POST /api/providers`

新建一个**用户级** provider:写 `~/.qi/agent/models.json`(声明)与 `auth.json`(凭证)。
**需要 `X-Qi-Confirm: yes`**(缺 → 428),成功回刷新后的 `ConfigView`。
能力位是 `GET /api/meta` 的 `capabilities.provider_write`;没有它 → 界面只保留“配凭证 / 登录已有”,
不摆一个点了才 404 的「添加 Provider」。

已存在 provider 的“配凭证”走 [`POST /api/auth/{provider}`](#post-apiauthprovider) ——
两条路分开,免得“加一个”把已有声明默默改写。

### 请求体 `ProviderAdd`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `string` | provider 名;字母/数字/`.`/`_`/`-`,首字符必须是字母或数字 |
| `base_url` | `string` | 必须 `http://` 或 `https://` |
| `api` | `string` | `openai-completions` / `openai-responses` / `anthropic-messages` / `google-generative-ai`;空 = `openai-completions` |
| `api_key` | `string` | 必填。写进 `auth.json`,**不回显** |
| `models` | `string[]` | 至少一个模型 id(去重保序) |

模型条目的 `contextWindow` / `maxTokens` 用 core 的默认值;要精确就之后改 `models.json`
(与 `qi init --refresh` 的口径一致:先能用,再校准)。

### 响应 `ConfigView` (200)

| 码 | 什么时候 |
| --- | --- |
| 400 | 名字不合法 / baseUrl 不是 http(s) / api 不在闭集 / 没有模型 id / 没给 key / key 看着不像密钥 / `models.json` 写不进去(会回滚已写的 key) |
| 409 | 同名 provider 已在某个 `models.json` 里定义(项目级 / `$QI_AGENT_CONFIG` 也算);预置 provider 已存在**且有可用凭证**;目标用户级 `models.json` 里已有同名 provider |
| 428 | 缺确认头 |

```bash
curl -s -X POST http://127.0.0.1:3456/api/providers \
  -H 'Content-Type: application/json' -H 'X-Qi-Confirm: yes' \
  -d '{"name":"my-provider","base_url":"https://api.example.com/v1",
       "api":"openai-completions","api_key":"sk-…","models":["model-a"]}' \
  | jq '[.providers[] | select(.credential_ok)]'
```

---

## `POST /api/auth/{provider}`

写入 provider 凭证。**需要 `X-Qi-Confirm: yes`**(缺 → 428)。

### 路径参数

| 名 | 说明 |
| --- | --- |
| `provider` | provider 名(与 `/api/config` 的 `providers[].name` 对齐) |

### 请求体 `AuthWrite`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `key` | `string` | **至少 1 个字符**。只在请求体里出现,**不进日志**、不回显 |

### 响应 **204**(空 body)

```bash
curl -i -X POST http://127.0.0.1:3456/api/auth/anthropic \
  -H 'Content-Type: application/json' -H 'X-Qi-Confirm: yes' \
  -d '{"key":"sk-ant-…"}'
# HTTP/1.1 204 —— 响应里没有这个 key,也没有任何回显
```

## `DELETE /api/auth/{provider}`

删掉某个 provider 的凭证。**需要 `X-Qi-Confirm: yes`**(缺 → 428)。

### 响应 **204**(空 body)

| 码 | 什么时候 |
| --- | --- |
| 404 | 这个 provider 没有凭证 |

改的是哪个文件看 `GET /api/config` 的 `auth_file`。

---

## 相关状态码

| 码 | 什么时候 |
| --- | --- |
| 401 / 400 | 口令、Host(通用约定) |
| 422 | body 校验、模型配对、思考级别不在集合 |
| 428 | 凭证写/删缺确认头 |
| 404 | 会话不存在、凭证不存在 |
| 503 | `ConfigError` / `LoadError`(配置坏了是可读错误,不是 500 栈) |
