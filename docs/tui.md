# TUI(交互界面)

> 状态:v1 实现(视觉基线对齐 pi)。本文只放交互形态:布局、`/` 内部命令、消息渲染。与 [cli.md](cli.md) 区分:CLI = 一次性命令;TUI = 交互命令。
> 相关:[dispatcher.md](dispatcher.md)(分派行渲染)、[web.md](web.md)(v2 同款视图)、[settings.md](settings.md)(`theme`)。

## 1. 布局(已实现;逐项对齐 pi)

配色/版式以 pi 的 `modes/interactive` 为基线,数据源是同一份 `theme/dark.json` /
`theme/light.json`(`src/qi_agent/themes/` 逐字移植),渲染细节对照过 pi 的
`user-message.js` / `assistant-message.js` / `tool-execution.js` / `footer.js` / `dynamic-border.js`。

```text
(终端滚动历史保留 —— inline 渲染,不占全屏、不进备用屏)

 qi v0.1.0                                                    ← bold accent + dim
 escape interrupt · ctrl+c clear/exit · ctrl+o tools · / commands · @ files · ! bash   ← 快捷提示
 qi 是多 agent 编码框架:专职角色 + auto 分派;/help 看全部命令。       ← dim 引导

[Agents]
  code-analyst, general

 读一下 pyproject.toml 然后总结          ← 用户消息:userMessageBg 底色块(padding 1,1)

● → qi (router, 0.90)                    ← qi 的 auto 分派(pi 无此概念,按其行风格)

 我来读一下这个文件。                      ← 助手 markdown(padding 0,1,无底色)

 read pyproject.toml                     ← 工具块:tool{Pending,Success,Error}Bg 底色
 [project]                                 标题 = `read <path>`(工具名 bold + 路径 accent)
 name=qi                                   输出 10/15/20 行(按工具),超出给省略提示

────────── ⠦ Working ──────────          ← 编辑器上边框;工作中内嵌 spinner(80ms)
 输入…
────────────────────────────────          ← 下边框(空闲时上下都是整行 ─)
~/Desktop/qi (master) • tui               ← footer 1:cwd(+git 分支)(+会话名),dim
↑12k ↓678 1.2%/1.0M (auto)      deepseek/v4.1 • medium   ← footer 2:token 统计 + 右对齐模型
qi · auto                                 ← footer 3:状态行
```

实现要点:

- **inline**:`App.run(inline=True, inline_no_clear=True)`;transcript 最多占
  `终端高 - 6`(编辑器 3 + footer 3),再长在内部滚动(不吞终端滚动历史)。
- **主题**:`theme` 设 `auto`(默认)时用 OSC 11 探测终端背景色 → dark/light,
  并把探测到的背景色设为 Textual 主题底色 —— 于是 inline 区域看不出“被填色”。
  `QI_THEME=dark|light|auto` 可直接覆盖。
- **编辑器**:上下两条 `─` 动态边框(`border` 色);工作态上边框嵌入
  `⠋…⠏ Working`(accent + muted)。
- **markdown**:Rich `Markdown` 渲染,样式名字映射到 pi 的 `md*` 键;
  代码高亮用 pygments 主题但颜色取自 pi 的 `syntax*` 键(无底色)。
- **分派行**:`● → <display_name> (source, 置信度)`,无底色,不加卡片。

已知差异(Textual 与 pi 自研渲染器的边界,不做逐字节对齐):OSC133 zone 标记、
代码块左侧 `│` 边线、图片/kitty 协议、超长历史进终端原生 scrollback。

## 2. `/` 内部命令

> 以**实现状态**为准(不再写“草案”)。pi 的 23 个内置命令见 `dist/core/slash-commands.js`;
> 下表里「计划中」= qi 尚无对应后端能力,单独提示,不冒充“未知命令”。

### 已实现(对齐 pi)

| 命令 | 说明 |
| --- | --- |
| `/help` | 命令帮助(含实现/计划分区) |
| `/hotkeys` | 快捷键(明写哪些 pi 键位还没做) |
| `/quit` | 退出 |
| `/new` | 新会话 |
| `/resume [id]` | 不给 id = 列出历史会话;给 id = 恢复 |
| `/sessions` | 列出历史会话 |
| `/name <name>` | 会话显示名(进 footer) |
| `/session` | 会话信息(ID/文件/cwd/消息数·仅当前分支/节点数与分支点/模型/用量) |
| `/tree` | 会话树:跳到本会话任意节点继续(同文件内分支,见下节) |
| `/fork [序号\|id]` | 从某条用户消息**之前**分叉出新会话,并把那条消息放回编辑器(对齐 pi) |
| `/clone [名字]` | 把当前分支复制成新会话 |
| `/compact [提示]` | 压缩上下文:把旧消息压成结构化摘要(可给一句关注点) |
| `/model [p/m]` | 当前模型 / 切换模型(等同 ctrl+l / ctrl+p) |
| `/export [file]` | 导出会话 JSONL(默认 `./qi-<id>.jsonl`;**pi 默认导出 HTML** —— qi 无 HTML 导出器) |
| `/import <file>` | 从 JSONL 导入并切换会话(重名给提示,不静默覆盖) |
| `/copy` | 复制最后一条回答到剪贴板(OSC 52) |
| `/reload` | 重载 agents / plugins / 配置(主题改动需重开) |
| `/login <provider>` | **只给指引**:`qi auth login <provider>`(密钥不进会话记录) |
| `/logout [provider]` | 删除已存凭证(无密钥输入,可直接在 TUI 里做) |
| `/changelog` | 显示 `CHANGELOG.md`(qi 仓库暂无该文件) |

### 会话树(格式 v2,对齐 pi 的 `id`/`parentId`)

会话文件里**每条 entry 带 `id`/`parentId`**,历史 = 「从当前节点沿 parent 回溯到根」的
那条链。所以同一个文件里可以并存多条分支:回到旧节点继续提问,新内容是它的子节点,
旧分支原样保留。header 里写 `version: 2`。

| 操作 | qi | pi |
| --- | --- | --- |
| `/tree` | 弹出树(深度缩进;`●` 当前节点、`│` 当前分支、`·` 其它分支)→ 选中即把「当前节点」移到那里,transcript 换成那条分支 | 同 |
| `/fork` | 选一条用户消息 → **新会话文件**(只到它的 parent)+ 消息放回编辑器 | 同 |
| `/clone` | 把当前分支复制成**新会话文件** | 同 |
| 旧会话 | 读入时补链,**首次写入**时落盘(读路径不写文件) | pi 在加载时迁移 |
| 当前节点 | 内存里(`/tree` 改它);落盘约定 = 文件最后一条 entry | 同 (leaf) |

上下文只走当前分支:`_history` / `_active_agent` / `_opening_shown` / 回放 / web `/messages`
全部按 `branch()`;所以另一条分支上的 state、开场白、消息不会串进来。

进 TUI 时的会话选择与 headless 同义:`qi -c`(续最近)/ `--session <id>` /
`--fork <id>` / `-n <名>` / `--no-session` 都生效,有历史就把当前分支回放到 transcript
(以前 TUI 无视这些参数、每次都新建一个叫 `tui` 的会话)。

qi 与 pi 的差异(已落档):

- 树里只显示块,不支持 pi 树选择器内的过滤键(它的 ctrl+t/ctrl+u/ctrl+l/ctrl+a);
- `/tree` 跳转时 pi 会**先问**要不要摘要被放弃的分支,qi 直接做并提示;
- `qi sessions show` 与 web `/messages` 只展示**当前分支**(web 契约仍把 header 放在 `entries[0]`);
- `--export` / `qi sessions show` 之外的导出仍拷**整个文件**(含其它分支)。

### 上下文压缩(对齐 pi 的 `core/compaction`)

| 机制 | 触发 | 行为 |
| --- | --- | --- |
| 压缩 compaction | 上下文超过 `contextWindow - reserveTokens`,或 `/compact` | 把旧消息压成结构化摘要(`## Goal` / `## Progress` / `## Next Steps` / `## Critical Context`…),落成 `type=compaction` entry,带 `firstKeptEntryId` |
| 分支摘要 branch summary | `/tree` 跳到别的分支 | 把**被放弃的那段**压成摘要(`type=branch_summary`)挂到跳转点,切回来时上下文不断 |

细节(逐条对齐 pi):

- 压缩后模型看到的是 `system | 摘要 | firstKeptEntryId 起的消息`;摘要本身作为一条
  user 消息注入(provider 对多个 system 支持不一)。
- 切点走「从最新往回累加 token(chars/4),累到 `keepRecentTokens`」,只落在 turn 边界或
  assistant 上(**绝不在工具结果上** —— qi 的工具结果虽不进跨轮上下文,但也不是元数据);
  单个 turn 超预算时做 **split turn**:前缀单独摘要后与历史摘要拼接。
- 重复压缩:从上一次的 `firstKeptEntryId` 起重新摘要(老消息会被重新纳入),并把旧摘要
  作为迭代上下文(`UPDATE` 提示词);`/compact <提示>` 会追加 `Additional focus:`。
- 触发估算用**重建后的上下文**(摘要 + 保留段),不是原始 entry 之和 —— 否则每轮都会重复压。
- TUI 里压缩显示成 pi 同款底色块(`[compaction]` / `[branch]`,`ctrl+o` 展开看摘要)。
- qi 与 pi 的差异:pi 在 `/tree` 跳转前**先问**要不要摘要,qi 直接做并提示;pi 还有
  `branch_summary` 的树内过滤/标签渲染,qi 只显示块。

### qi 独有

| 命令 | 说明 |
| --- | --- |
| `/agents` | 列出 agent |
| `/mode auto\|manual` | 切换分派模式 |
| `/agent <name>` | manual 下锁定执行 agent |
| `/tools` | 当前/全部 agent 工具清单(简版) |
| `/clear` | 清屏 |
| `@name` 开头 | 直接点名 agent(与 pi 无关,qi 的 auto 分派补充) |

### 计划中(pi 有,qi 缺后端能力)

| pi 命令 | 缺什么 |
| --- | --- |
| `/scoped-models` | Ctrl+P 轮换清单(qi 现在轮完 models.json 里全部) |
| `/settings` | TUI 内设置面板 |
| `/share` | GitHub gist 分享 |
| `/trust` | 项目信任门控(qi 只有 `defaultProjectTrust` 字段,没有信任判定) |

### 键位(已对齐 pi;`core/keybindings.js`)

| 键 | qi 行为 | pi 的 action |
| --- | --- | --- |
| `escape` | 中断当前回合(取消 worker) | `app.interrupt` |
| `ctrl+c` | 清空输入框;再按一次退出 | `app.clear` + `app.exit` |
| `ctrl+d` | 输入框为空时退出;非空删右侧字符 | `app.exit` |
| `ctrl+o` | 展开/折叠工具输出 | `app.tools.expand` |
| `shift+tab` | 循环思考级别(off→minimal→…→max→off) | `app.thinking.cycle` |
| `ctrl+t` | 显示/隐藏思考块 | `app.thinking.toggle` |

思考(对齐 pi 的 `thinkingLevel`):

- 级别经 litellm 的 `reasoning_effort` 下发(`minimal/low/medium/high`);
  **`xhigh`/`max` 收敛为 `high`** —— litellm / 多数 provider 没有这两个档。
- 只有模型在 `models.json` 里声明 `reasoning: true` 时才带参(否则某些 provider 会 400);
  footer 右侧显示 `模型 • <级别>`(off 时按 pi 的写法显示 `• thinking off`)。
- 思考内容与回答**分开流式**(`thinking_delta`),灰色斜体渲染;`ctrl+t` 可随时隐藏/显示。
- **provider 拒收 `reasoning_effort` 时**(实测自建 LiteLLM 代理默认就是这样)自动去掉参数重试,
  并在 footer 提示一次“已按不思考运行”——不会因此整轮失败。
- 思考内容**不落盘**(仅当轮展示):会话 JSONL 仍只有 message/tool/dispatch/state 四类。
| `ctrl+x` | 复制最后一条回答 | `app.message.copy` |
| `ctrl+g` | `$EDITOR` 编辑当前输入 | `app.editor.external` |
| `ctrl+l` | 模型选择器(模态列表) | `app.model.select` |
| `ctrl+p` / `ctrl+shift+p` | 下一个 / 上一个模型 | `app.model.cycleForward/Backward` |
| `ctrl+z` | 挂起(回到 shell) | `app.suspend` |

实现注记:Textual 的 `Input` 默认把 `ctrl+c/ctrl+x/ctrl+d` 绑到「复制/剪切/删右侧」,
qi 用 `priority=True` 抢过来以匹配 pi 语义(`ctrl+d` 非空时仍自己调 `delete_right`);
另关闭 Textual 的 command palette,因为它的默认键 `ctrl+p` 在 pi 里是切模型。

**尚未对齐**(qi 缺后端能力或输入层,`/hotkeys` 里也如实列出):

| 键 | pi 用途 | qi 缺什么 |
| --- | --- | --- |
| `ctrl+n` / `ctrl+r` | 会话列表过滤 / 重命名会话 | 无会话选择器 UI(pi 里这两个键只在那面板内生效);`/name` 可改名 |
| `ctrl+y` / `alt+y` | kill-ring 的 yank / yank-pop | Textual 无 kill-ring,`ctrl+y` 仍是它的 redo |
| `ctrl+v` | 粘贴图片 | 无图片输入,目前只会粘文本 |

### 补全与 bash 模式(对齐 pi 的 autocomplete / `handleBashCommand`)

| 触发 | 行为 |
| --- | --- |
| 行首 `/xx` | 命令名补全(候选来自 `TUI_COMMANDS`,与 `_command` 的已实现分支对应) |
| 任意位置 `@xx` | 相对路径补全(目录优先、以 `/` 结尾继续往下补;隐藏文件只在 `@.` 时列出) |
| `tab` | 接受候选(命令补完带一个空格);**无候选时仍是缩进** |
| `↑` / `↓` | 面板开着时选候选,否则移动光标 |
| `escape` | 先关面板,再考虑中断 |
| `!<命令>` | 执行 shell,输出以 pi 同款「上下 `─` + `$ cmd` + 输出」块展示,并把「命令 + 输出」落成一条 user 消息供后续回合参考 |
| `!!<命令>` | 同样执行,但**不进上下文**(块边框变 `dim`,标题标出) |

与 pi 的差异(已落档):pi 用 `fd` 走全树模糊匹配并支持 `@"带空格的路径"`;qi 只扫描当前目录层级、前缀匹配。
pi 在 bash 运行中会拒绝再跑一条;qi 允许并发。两者都不影响普通聊天。

### 输入层:多行编辑器(对齐 pi `pi-tui/components/editor.js`)

qi 用 Textual 的 `TextArea` 做成 pi 的编辑器,补齐了这些键(其余是 Textual 自带):

| 键 | 行为 | pi 的 action |
| --- | --- | --- |
| `enter` | 提交 | `tui.input.submit` |
| `shift+enter` / `ctrl+j` | 换行 | `tui.input.newLine` |
| `ctrl+b` / `ctrl+f` | 光标左 / 右 | `cursorLeft` / `cursorRight` |
| `alt+b` `alt+f` `alt+←/→` | 按词移动 | `cursorWordLeft` / `cursorWordRight` |
| `alt+d` | 删后一个词 | `deleteWordForward` |
| `ctrl+-` | 撤销 | `undo`(pi 不用 `ctrl+z` —— 那个是挂起) |
| `ctrl+w` / `ctrl+u` / `ctrl+k` / `alt+backspace` | 删词 / 删到行首 / 删到行尾 | 同名 action(Textual 自带) |
| `ctrl+v` | 粘贴(含括号粘贴,多行可用) | 文本部分对齐 |

实现注记:

- `TextArea` 在 `tab_behavior="indent"` 下会把 **escape 当成「换焦点」、tab 当「缩进」并吞掉**;
  qi 在 `Editor._on_key` 里先判断补全面板是否开着:开着就归补全(tab/↑/↓/escape),否则
  放行给 TextArea(缩进)或发 `Interrupt` 消息给 App(中断)。
  不走 priority 绑定 —— 那会抢掉模型选择器的 escape。
- 编辑器 `height: auto`(最多 8 行,矮终端还会再降);transcript 上限按
  `终端高 - (编辑器当前行数 + 上下边框 + footer)` 动态算 —— 编辑器长高时 transcript 让位,
  否则 inline 区域会把输入框挤掉。
- `enter` 是提升到 priority 的绑定(`TextArea` 原生会把 enter 插成换行,必须先抢)。

qi 的输入层是**多行编辑器**(见下面「输入层」一节):`/` 与 `@` 补全、`!` bash 模式、
消息队列仍未实现——它们现在是纯输入层功能,不再受控件限制。

## 3. 消息队列(已实现;语义对齐 pi)

| 操作 | pi | qi |
| --- | --- | --- |
| 回合中 `Enter` | steer:下一个模型边界送达 | 排队,当前回合结束后立即作为下一回合发送 |
| `Alt+Enter` | follow-up:全部工作完成后送达 | 排队,排在所有 steer 之后 |
| `Esc` | 中止当前轮 + 排队退回编辑器 | 同(有排队就退回,否则只中止) |
| `Alt+Up` | 取回排队消息 | 同(多条按 steer→follow-up 顺序拼回编辑器) |

行为细节:

- 回合进行中**不再并发起第二个 worker**(旧实现会两个回合互踩同一会话文件);
  普通对话提交一律进队列,footer 状态行显示 `排队 N`。
- **命令(`/x`)与 bash(`!x`)例外:回合进行中也立即执行**(与 pi 一致) —— 否则
  `/quit`、`/tree` 这类命令会被推到回合结束后,等于按不下去。
- 每条排队消息会以 dim 行写进 transcript(带“当前回合结束后发送”/“follow-up”标签),
  不会静默丢消息。
- `/new` 清空队列。

与 pi 的差异:pi 的 steer 是在**同一次 run 内**的下一个模型边界注入(可以影响正在进行的工作),
qi 的 runtime 没有 mid-run 注入接口,所以 steer 实际是“下一回合”。顺序语义一致(steer 先于 follow-up)。
队列只存在于当前 TUI 进程(不落盘)。

## 4. 扩展点(v2)

- 插件可注册自定义 TUI 命令(对齐 pi `registerCommand`)
- web v2 视图复用同一事件流与渲染结构(web.md)
