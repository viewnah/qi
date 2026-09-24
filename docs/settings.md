# 设置(`settings.json`)

> 文件位置、分层与合并规则见下。
> 读写入口:`qi config`(见 [cli.md](cli.md));扩展声明在 `settings.packages` 与 `settings.extensions`(见 [extensions.md](extensions.md) §10)。

## 1. 位置与分层

```text
<cwd>/.qi/settings.json          项目覆盖(可提交共享)
~/.qi/agent/settings.json        全局
```

**项目级就在当前目录(cwd)的 `.qi/`** —— 对齐 pi 的 `.pi/settings.json`(它的文档写的是
"Project (current directory)")。仓库根(`.git`)只用来界定 `.agents/skills` 的祖先探测,
不是配置目录。

配对关系:全局比项目深一层(`~/.qi/agent/` ↔ `<cwd>/.qi/`),因为全局侧是
"全部用户级状态的挂载点"。`QI_AGENT_HOME` 指向的就是全局 agent 目录本身。

## 2. 合并规则

- 键级**深合并**:项目覆盖全局;嵌套对象递归合并。
- **数组整体替换**(不逐项合并)—— 避免"想删一项却删不掉"。
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
| `shellPath` | string | — | `bash` 用哪个 shell;空 = 按平台解析:`shellPath` → Windows 上 Git Bash 已知路径 → PATH 的 bash → `/bin/bash` → `sh`。指向不存在的文件会直接报错(不静默回退)。见 [how-qi-works.md](how-qi-works.md) |
| `defaultProjectTrust` | string | `ask` | 项目信任:`ask`(默认,现保守判**不信任**)/ `always` / `never`。**只从用户级读** —— 项目级写了会被忽略并提示(仓库不能自称可信) |
| `skills` | string[] | `[]` | 追加技能路径;支持 glob、`~`、相对路径与排除项(见 §4) |
| `skillsEnabled` | boolean | `true` | qi 扩展:关闭技能自动发现(等价 `--no-skills` CLI 开关) |
| `enableSkillCommands` | boolean | `true` | 把技能注册成 `/skill:<名>` 命令 —— 技能平时只进描述,这条是**强制加载全文**的入口。关掉则这批命令不存在 |
| `doubleEscapeAction` | string | `tree` | 空编辑器连按两次 `escape`:`tree`(默认)/`fork`/`none`;非法值按 `tree`。见 [slash-commands.md](slash-commands.md) |
| `hideThinkingBlock` | boolean | `false` | 启动时不展示思考块(`ctrl+t` 仍可切换) |
| `editorPaddingX` | number | `1` | 编辑器左右内边距(qi 视觉基线用 `1`) |
| `outputPad` | number | `1` | 助手输出的左侧缩进(`0` = 顶格) |
| `autocompleteMaxVisible` | number | `5` | 补全面板最多显示几行(候选本身不裁,面板内滚动) |
| `tuiMode` | string | `fullscreen` | TUI 渲染模式:`fullscreen`(qi 默认)= 备用屏、qi 拥有视口,滚轮只滚 transcript;`regular` = inline(不占全屏、滚动交给终端)。非法/缺失值按 `fullscreen`。理由见 [usage.md](usage.md)。启动时读;改完要重启(不想重启就用 `--tui-mode`) |

### 收下但按实际状态落地

这些字段都收下;下表逐条说明落地状态(**写了会不会生效**):

| 字段 | 用途 | 现状 |
| --- | --- | --- |
| `theme` | 主题名(dark/light/auto) | TUI 已接:自动探测终端背景(OSC 11),失败落 dark;`QI_THEME` 可覆盖。见 [usage.md](usage.md) |
| `branchSummary` | 分支摘要:/tree 跳转时被放弃那段要不要压成摘要;`skipPrompt: true` = **不问也不摘要** | **已接**(`skipPrompt`):默认**先问一句**,默认答案「不摘要」;无前端时同样不摘要。`reserveTokens` **未接** —— qi 的摘要预算固定,不加不生效的旋钮 |
| `thinkingBudgets` | 每个思考级别的 token 预算;Anthropic/Google/Bedrock 原生用,OpenAI 兼容形态要靠每模型的 `compat.thinkingTokenBudgetField` | **部分已接**:**只对 Anthropic 形态**生效(litellm 只在那里有对应参数),且**不配就不带**;钳制到至少留 1024 token 给答案。OpenAI 兼容那半不做 —— qi 没有 compat 层 |
| `modelThinkingLevels` | `{}` | 按模型的思考级别:`{"provider/模型": "high"}`(也认裸模型 id)。四级优先:`--thinking` > `--model provider/id:<级别>` > 这里 > `defaultThinkingLevel`;换模型时自动采纳,但本会话显式设过(`/thinking`)就不覆盖 |
| `defaultThinkingLevel` | 默认思考级别 | 已接:`off`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`;**不配默认 `medium`**;TUI `shift+tab` 可运行时切换(`--thinking` 可覆盖)。见 [slash-commands.md](slash-commands.md) |
| `enabledModels` | 模型轮换(Ctrl+P) | 已接:`/scoped-models` 勾选后写回（全局设置）；空 = 轮换 models.json 里全部。已记进 settings 但**现已不可用**的条目(provider 删了 / 凭证没了)在面板里画成删除线 + `[unavailable]`,取消勾选才移出 |
| `quietStartup` | 隐藏启动头 | 已接:不写 banner(含快捷键提示);启动提示(会话不存在之类)仍会出 |
| `defaultTools` | 初始内置工具集 | **已接**:只挑**内置**那一档,扩展装的工具照旧全留 —— 所以 `[]` = "不要内置、只留扩展"(与"没配"是两件事)。`--tools` / `-t` 等 CLI 旗标**压过**它 |
| `compaction` | `{enabled, reserveTokens(默认 16384), keepRecentTokens(默认 20000)}` | 已接:超 `contextWindow - reserveTokens` 自动压缩;`/compact` 手动压缩。见 [slash-commands.md](slash-commands.md) |
| `retry.provider` | `{timeoutMs, maxRetries}` | **已接**(请求级):→ litellm 的 `timeout`(秒)/ `num_retries`。毫秒→秒在代码里换算。这是**唯一**该管请求超时的地方 —— agent 层不再套 `asyncio.timeout`(旧行为会把整轮打成“执行超时”) |
| `retry.enabled` / `retry.maxRetries` / `retry.baseDelayMs` | 回合级重试 | 未接(失败回合退避重试)。已接的只是上面的 `retry.provider.*`;`maxRetryDelayMs` 无对应 litellm 参数,未映射 |
| `packages` | **扩展声明层**:这个环境该装哪些扩展 | **已接**:`qi list` / `qi doctor` 读它做「声明了没装 / 装了没声明」的双向比对,并输出可复制的装法。qi **不替你调 pip**(理由见 [extensions.md §11](extensions.md)),所以这里只是声明 |
| `extensions` | 额外扩展路径 | **已接**(P-E1):每一条可以是扩展目录的**父目录**,也可以直接指向**单个扩展目录**;支持 `!排除` / `-排除`(见 §4);项目级那份受信任门控 |

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
[skills.md](skills.md) §1。

## 6. `models-store.json`:qi 不使用

模型清单全部来自静态配置(`models.json` 加 litellm 的模型表),qi **没有**远程模型目录缓存 ——
`~/.qi/agent/models-store.json` 这个文件不存在,也不需要存在。

后果只有一条:新模型不会自己出现。`qi init --refresh <名>` 是拿厂商 `/models` 接口对齐本地清单的
入口(见 [providers.md](providers.md) §2.1)。

## 7. `qi config` 速查

```bash
qi config                       # 看:合并后的设置、来源文件、默认模型来源、顶层技能
qi config --json                # 机器可读
qi config --set theme=light     # 写全局(JSON 值解析,失败则当字符串)
qi config --set 'skills=["~/x"]'
qi config -l --set theme=dark   # 写项目 <cwd>/.qi/settings.json
qi config --unset theme         # 删键(支持点号路径,如 compaction.enabled)
```
