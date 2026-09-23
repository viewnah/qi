# 斜杠命令

TUI 里输入 `/` 会补全全部命令(内置的、扩展注册的、技能命令都在里面)。命令**在回合进行中也立即执行**
(与 `!` bash 一样),否则 `/quit`、`/tree` 这类会按不下去。

## 模型与设置

| 命令 | 说明 |
| --- | --- |
| `/model [p/m]` | 不给参数 = 开选择器(与 `ctrl+l` 同一个);给 `p/m` 或唯一模型名 = 直接切。**只列解析得出凭证的模型**。切换会落一条 `model_change`,下次打开这条会话仍用它 |
| `/scoped-models` | 挑 `ctrl+p` 轮换哪些模型(空 = 全部,写回 `settings.enabledModels`);已不可用的条目画删除线 + `[unavailable]`,取消勾选才移出 |
| `/thinking` | 不给参数 = 开选择器;切换落一条 `thinking_level_change` entry,下次打开按它还原 |
| `/settings` | 偏好面板:主题 / 思考级别 / 交互开关(`enter` 换值、`ctrl+s` 保存到**用户级** settings) |
| `/login [provider]` | 不给 provider 先弹选择器,然后**遮罩输入** API key → 写 `auth.json`(0600)并重建客户端;key 不进 transcript。qi 的凭证层**只有 api_key**,没有订阅登录(OAuth) |
| `/logout [provider]` | 删 `auth.json` 里那条(**环境变量与 `models.json` 的 `apiKey` 不受影响**);此后那家的模型不再进 `/model` 与 `ctrl+p` 清单 |

## 会话与上下文

| 命令 | 说明 |
| --- | --- |
| `/new` | 新会话(**不落文件** —— 与裸 `qi` 一样是预留,说话才落盘) |
| `/resume [id]` | 不给 id = 打开会话选择器;给 id = 直接恢复 |
| `/name <name>` | 会话显示名(进 footer)。**命名即落盘**:预留中的会话会因此立刻写出 —— 否则 `/new` / 退出会让名字连会话一起消失 |
| `/session` | 会话信息(ID / 文件 / cwd / 消息数·仅当前分支 / 节点数与分支点 / 模型 / 用量) |
| `/tree` | 会话树:跳到本会话任意节点继续(同文件内分支) |
| `/fork [序号\|id]` | 从某条用户消息**之前**分叉出新会话,并把那条消息放回编辑器 |
| `/clone [名字]` | 把当前分支复制成新会话 |
| `/compact [提示]` | 压缩上下文:把旧消息压成结构化摘要(可给一句关注点) |

## 导出与分享

| 命令 | 说明 |
| --- | --- |
| `/export [file]` | 导出会话:`.html` → 自包含 HTML(只含当前分支),其余 → 原始 JSONL;默认 `./qi-<id>.jsonl` |
| `/import <file>` | 从 JSONL 导入并切换会话(重名给提示,不静默覆盖) |
| `/copy` | 复制最后一条回答到剪贴板(OSC 52) |
| `/share` | 把当前会话传成**私有** GitHub gist(一个自包含 `.html`)并复制链接。直调 API,不依赖 `gh`;token 顺序照 qi 的凭证原则(auth store → 环境变量):`qi auth login github` → `GITHUB_TOKEN` → `GH_TOKEN` |

## 运行时与项目

| 命令 | 说明 |
| --- | --- |
| `/hotkeys` | 快捷键(哪些键位还没做也如实写明) |
| `/reload` | 重载扩展 / 技能 / 配置(主题改动需重开) |
| `/trust [yes\|no\|forget]` | 按**目录**记住信任决定(`~/.qi/agent/trust.json`);连带记住上一层,写完要重启才生效 |
| `/changelog` | 显示 `CHANGELOG.md`(qi 仓库暂无该文件) |
| `/quit` | 退出 |

## 由资源注册的命令

- 扩展用 `api.register_command` 注册的命令 —— 重名时都留着并变成 `name:1` / `name:2`,一个都不丢。
- 技能注册的 **`/skill:<名> [参数]`** —— 加载并执行该技能的全文(强制加载的入口);
  由 `settings.enableSkillCommands` 控制,默认开。见 [skills.md](skills.md)。

`/quit` 与 `/hotkeys` **扩展顶不掉**(顶掉 `/quit` 等于把用户锁在界面里)。
