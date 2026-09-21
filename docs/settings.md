# 设置(`settings.json`)

> 文件位置与字段名**对齐 pi**,便于两边共享同一套认知。
> 读写入口:`qi config`(见 [cli.md](cli.md) §5);扩展声明在 `settings.packages` 与 `settings.extensions`(见 [extensions.md](extensions.md) §5.5)。

## 1. 位置与分层

```text
<git根>/.qi/settings.json        项目覆盖(可提交共享)
~/.qi/agent/settings.json        全局
```

配对关系与 pi 一致:全局比项目深一层(`~/.pi/agent/` ↔ `<项目>/.pi/`),因为全局侧是
"全部用户级状态的挂载点"。`QI_AGENT_HOME` 指向的就是全局 agent 目录本身。

## 2. 合并规则

- 键级**深合并**:项目覆盖全局;嵌套对象递归合并。
- **数组整体替换**(不逐项合并)—— 与 pi 一致,避免"想删一项却删不掉"。
- 未知字段原样保留(`extra=allow`),方便扩展与前后版本共存。
- 文件损坏/JSON 非法 → 启动报错(不静默降级),`qi doctor` 会指出具体文件。

```jsonc
// ~/.qi/agent/settings.json
{ "theme": "dark", "compaction": { "enabled": true, "reserveTokens": 16384 } }

// <项目>/.qi/settings.json
{ "compaction": { "reserveTokens": 8192 } }

// 结果:theme=dark, compaction={enabled: true, reserveTokens: 8192}
```

## 3. 字段

### 已生效

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `defaultProvider` | string | — | 默认 provider(**唯一来源**;`models.json` 里的同名键不参与) |
| `defaultModel` | string | — | 默认模型 id,同上;缺任一项则启动失败并给出迁移命令 |
| `sessionDir` | string | — | 会话目录;相对路径按**各自 settings.json 所在目录**解析,支持 `~`;项目优先于全局 |
| `shellPath` | string | — | `bash` 用哪个 shell(对齐 pi);空 = 按平台解析:`shellPath` → Windows 上 Git Bash 已知路径 → PATH 的 bash → `/bin/bash` → `sh`。指向不存在的文件会直接报错(不静默回退)。见 [tools.md](tools.md) §2 |
| `defaultProjectTrust` | string | `ask` | 项目信任:`ask`(默认,现保守判**不信任**)/ `always` / `never`。**只从用户级读** —— 项目级写了会被忽略并提示(仓库不能自称可信) |
| `skills` | string[] | `[]` | 追加技能路径;支持 glob、`~`、相对路径与排除项(见 §4) |
| `skillsEnabled` | boolean | `true` | qi 扩展:关闭技能自动发现(等价 pi 的 `--no-skills` CLI 开关) |
| `doubleEscapeAction` | string | `tree` | 空编辑器连按两次 `escape`:`tree`(默认)/`fork`/`none`;非法值按 `tree`。见 [tui.md](tui.md) §2 |
| `hideThinkingBlock` | boolean | `false` | 启动时不展示思考块(`ctrl+t` 仍可切换) |
| `editorPaddingX` | number | `1` | 编辑器左右内边距(pi 默认 `0`,qi 视觉基线用 `1`) |
| `outputPad` | number | `1` | 助手输出的左侧缩进(pi 默认 1;`0` = 顶格) |
| `autocompleteMaxVisible` | number | `5` | 补全面板最多显示几行(pi 默认 5;候选本身不裁,面板内滚动) |

### 仅存储(预留,尚未参与行为)

这些字段按 pi 的名字收下并原样保留,但 v1 还没有对应实现,写进去**不会生效**:

| 字段 | pi 中的用途 | qi 现状 |
| --- | --- | --- |
| `theme` | 主题名(dark/light/auto) | TUI 已接:自动探测终端背景(OSC 11),失败落 dark;`QI_THEME` 可覆盖。见 [tui.md](tui.md) §1 |
| `defaultThinkingLevel` | 默认思考级别 | 已接:`off`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`;TUI `shift+tab` 可运行时切换(`--thinking` 可覆盖)。见 [tui.md](tui.md) §2 |
| `enabledModels` | 模型轮换(Ctrl+P) | 已接:`/scoped-models` 勾选后写回（全局设置）；空 = 轮换 models.json 里全部 |
| `quietStartup` | 隐藏启动头 | 已接:不写 banner(含快捷键提示);启动提示(会话不存在之类)仍会出 |
| `defaultProjectTrust` | `ask`/`always`/`never` | 项目信任尚未实现(`-a` 未落地) |
| `defaultTools` | 初始内置工具集 | **已接**:只挑**内置**那一档(pi 同义),扩展装的工具照旧全留 —— 所以 `[]` = "不要内置、只留扩展"(与"没配"是两件事)。`--tools` / `-t` 等 CLI 旗标**压过**它 |
| `compaction` | `{enabled, reserveTokens(默认 16384), keepRecentTokens(默认 20000)}` | 已接:超 `contextWindow - reserveTokens` 自动压缩;`/compact` 手动压缩。见 [tui.md](tui.md) §2 |
| `retry.provider` | `{timeoutMs, maxRetries}` | **已接**(请求级,对齐 pi 的 `getProviderRetrySettings`):→ litellm 的 `timeout`(秒)/ `num_retries`。pi 用毫秒、litellm 用秒,代码里换算。这是**唯一**该管请求超时的地方 —— agent 层不再套 `asyncio.timeout`(旧行为会把整轮打成“执行超时”) |
| `retry.enabled` / `retry.maxRetries` / `retry.baseDelayMs` | 回合级重试 | 未接(pi 有:失败回合退避重试)。已接的只是上面的 `retry.provider.*`;pi 的 `retry.provider.maxRetryDelayMs` 无对应 litellm 参数,也未映射 |
| `packages` | **扩展声明层**:这个环境该装哪些扩展 | **已接**:`qi list` / `qi doctor` 读它做「声明了没装 / 装了没声明」的双向比对,并输出可复制的装法。qi **不替你调 pip**(理由见 [extensions.md §5.5](extensions.md)),所以这里只是声明 |
| `extensions` | 额外扩展路径 | **已接**(P-E1):每一条可以是扩展目录的**父目录**,也可以直接指向**单个扩展目录**(对齐 pi);支持 `!排除` / `-排除`(见 §4);项目级那份受信任门控 |

> 上表是**诚实清单**:写在文档里的是"已收下但未生效",避免用户以为写了就有效。

## 4. 资源路径解析

`skills`(以及将来其它资源字段)里的每一条按下列规则解析:

- 相对路径 → 相对**该条所在的 settings.json 目录**:`~/.qi/agent/settings.json` 里的
  相对路径相对 `~/.qi/agent`,`<项目>/.qi/settings.json` 里的相对 `<项目>/.qi`。
  → 所以两个作用域各自写 `"skills": ["x"]` 指的是**两个不同的目录**,互不影响。
- `~` 展开为 home;绝对路径直接用;支持 glob(`*` / `?` / `[`)。
- 排除项:前缀 `!` 或 `-`;强制纳入:前缀 `+`。

同一套语法对 **`extensions`** 也生效(排除项作用于整个发现集)—— 所以内建目录
(`~/.qi/agent/extensions/`、`<项目>/.qi/extensions/`)里扫出来的扩展也能被单独关掉:

```jsonc
{ "extensions": ["-~/.qi/agent/extensions/some-ext"] }   // 只关这一个
```

这正是 `qi config` 面板里“关”的写法(加一条否定项,**不是删条目**)—— 状态因此被记住,
面板才能区分“没配过”与“主动关了”。pip 装的包没有路径可否定,它用 `packages` 的**对象形态**:

```jsonc
{ "packages": [{ "source": "qi-mcp", "extensions": [] }] }   // 这个包不贡献扩展
```

（注:glob 相对**该条所在的 settings.json 目录**解析 —— 写绝对路径可以,写**绝对 glob**不行。）

排除项作用于**整个发现集**,不只是数组里纳入的根 —— 因此默认目录里扫出来的技能也能单独关掉:

```jsonc
{
  "skills": [
    "~/my-extra-skills",        // 追加一个根
    "-skills/experimental",     // 关掉 ~/.qi/agent/skills/experimental
    "!~/noisy-skills"           // 同样效果,`!` 与 `-` 等价
  ]
}
```

不存在的排除路径会被丢弃(运行时解析,没有目标可排除)。

## 5. 与技能发现的关系

`settings.json` 的 `skills` 只是技能来源之一,完整优先级与发现规则见
[agent-config.md](agent-config.md) §5。

## 6. `models-store.json`(pi 有,qi 没有)

pi 的 `~/.pi/agent/models-store.json` 不是用户配置,而是**远程模型目录缓存**:
被 `withRemoteCatalog()` 包装的内置 provider 会去 `GET <catalogBaseUrl>/api/models/providers/<id>`
拉模型清单,按 provider 存 `{models, checkedAt, lastModified, etag}`,用 `If-None-Match`
做 304 重验证,4 小时节流,`PI_OFFLINE` 时跳过;启动时叠加在内置清单之上。

- 它只对**内置 provider** 生效。自定义 provider(`models.json` 里手写的)永远没有条目,
  所以那份文件通常是 `{}` —— 这是正常状态。
- **qi 不引入这个文件**:模型清单来自静态 `models.json`(加 litellm 的模型表),
  qi 没有 pi.dev 那样的远程目录服务端,照抄只会多一个永远为 `{}` 的文件。

## 7. `qi config` 速查

```bash
qi config                       # 看:合并后的设置、来源文件、默认模型来源、顶层技能
qi config --json                # 机器可读
qi config --set theme=light     # 写全局(JSON 值解析,失败则当字符串)
qi config --set 'skills=["~/x"]'
qi config -l --set theme=dark   # 写项目 <git根>/.qi/settings.json
qi config --unset theme         # 删键(支持点号路径,如 compaction.enabled)
```
