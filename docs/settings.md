# 设置设计(`settings.json`)

> 状态:v1 实现。文件位置与字段名**对齐 pi**,便于两边共享同一套认知。
> 读写入口:`qi config`(见 [cli.md](cli.md) §5)。

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
| `skills` | string[] | `[]` | 追加技能路径;支持 glob、`~`、相对路径与排除项(见 §4) |
| `skillsEnabled` | boolean | `true` | qi 扩展:关闭技能自动发现(等价 pi 的 `--no-skills` CLI 开关) |

### 仅存储(预留,尚未参与行为)

这些字段按 pi 的名字收下并原样保留,但 v1 还没有对应实现,写进去**不会生效**:

| 字段 | pi 中的用途 | qi 现状 |
| --- | --- | --- |
| `theme` | 主题名(dark/light/自定义) | TUI 尚未接主题 |
| `defaultThinkingLevel` | 默认思考级别 | 无思考级别概念 |
| `enabledModels` | 模型轮换(Ctrl+P) | 无模型轮换 |
| `quietStartup` | 隐藏启动头 | 未接 |
| `defaultProjectTrust` | `ask`/`always`/`never` | 项目信任尚未实现(`-a` 未落地) |
| `defaultTools` | 初始内置工具集 | 工具集由 agent.md 的 `tools` 决定 |
| `compaction` | 上下文压缩参数 | 压缩机制在 v2 |
| `retry` | 重试参数 | 未接 |
| `packages` | npm/git 资源包 | qi 走 pip entry point,无 npm 包概念 |
| `extensions` / `prompts` / `themes` | 其它资源路径 | 仅 `skills` 已接 |

> 上表是**诚实清单**:写在文档里的是"已收下但未生效",避免用户以为写了就有效。

## 4. 资源路径解析

`skills`(以及将来其它资源字段)里的每一条按下列规则解析:

- 相对路径 → 相对**该条所在的 settings.json 目录**:`~/.qi/agent/settings.json` 里的
  相对路径相对 `~/.qi/agent`,`<项目>/.qi/settings.json` 里的相对 `<项目>/.qi`。
  → 所以两个作用域各自写 `"skills": ["x"]` 指的是**两个不同的目录**,互不影响。
- `~` 展开为 home;绝对路径直接用;支持 glob(`*` / `?` / `[`)。
- 排除项:前缀 `!` 或 `-`;强制纳入:前缀 `+`。

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
