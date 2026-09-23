# TUI(交互界面)

> 本文只放交互形态:布局、`/` 内部命令、消息渲染。与 [cli.md](cli.md) 区分:CLI = 一次性命令;TUI = 交互命令。
> 相关:[qi-web README](../extensions/qi-web/README.md)(web 端复用同一事件流与渲染结构)、[settings.md](settings.md)(`theme`)、[themes.md](themes.md)。

## 1. 布局与渲染模式(已实现;逐项对齐 pi)

配色/版式以 pi 的 `modes/interactive` 为基线,数据源是同一份 `theme/dark.json` /
`theme/light.json`(`src/qi_agent/themes/` 逐字移植),渲染细节对照过 pi 的
`user-message.js` / `assistant-message.js` / `tool-execution.js` / `footer.js` / `dynamic-border.js`。

### 1.1 两种模式(`tuiMode` / `--tui-mode`)

与 pi 同名同义的两个值,但**默认相反**:

| | `fullscreen`(**qi 默认**) | `regular` |
| --- | --- | --- |
| 屏幕 | 备用屏(alt screen),qi 拥有视口 | 主屏,不占全屏 |
| 滚动 | 滚轮 / 拖动喂给 qi 自己的 transcript,**只在界面内移动** | 交给终端 —— 滚轮 / PageUp 会翻到启动 qi 之前的 shell 输出 |
| 退出后 | 恢复进入前的屏幕(终端回滚历史完好) | 内容留在终端里 |
| 鼠标 | 开着(不为别的,就是为了上面的滚动) | 关掉(见下) |
| pi 的默认 | 否(pi 默认 `regular`,fullscreen 标为 experimental) | 是 |

**为什么 qi 默认 fullscreen**:pi 的 `regular` 把滚动权交给终端,你向上翻能看到启动前的 shell
输出;qi 要的是“滚动只在本界面内”,所以默认进备用屏。想回到 pi 的默认行为:
`--tui-mode regular` 或 `settings.json` 里 `{"tuiMode": "regular"}`。

实测的实现路径是 Textual 自己的非 inline 模式(`App.run()`,即备用屏),不是自写渲染器:
布局上只需把 transcript 改成 `1fr` 并在内部滚动、底部固定(见 1.3)。

### 1.2 屏幕内容

```text
(fullscreen:整屏归 qi —— 内容装不下时只有 transcript 自己在滚)

 qi v0.1.0                                                    ← bold accent + dim
 escape interrupt · ctrl+c/ctrl+d clear/exit · / commands · ! bash · ctrl+o tools   ← 紧凑快捷键行
 Qi can explain its own features and look up its docs.         ← dim 引导语(照搬 pi 句式)
 Ask it how to use or extend Qi.

[Skills]
  termio                                  ← 顶层技能(core 的能力,紧凑态只列名字)
[Extensions]
  qi-web, qi-agents                       ← 装上的扩展名(与 [Skills] 并列;空段不显示)

 读一下 pyproject.toml 然后总结          ← 用户消息:userMessageBg 底色块(padding 1,1)

● → qi (router, 0.90)                    ← v1 分派行:**只在回放旧会话时出现**(v3 不再发)

 我来读一下这个文件。                      ← 助手 markdown(padding 0,1,无底色)

 read pyproject.toml                     ← 工具块:tool{Pending,Success,Error}Bg 底色
 [project]                                 标题 = `read <path>`(工具名 bold + 路径 accent)
 name=qi                                   输出 10/15/20 行(按工具),超出给省略提示

────────── ⠦ Working ──────────          ← 编辑器上边框;工作中内嵌 spinner(80ms);**固定在底部**
 输入…
────────────────────────────────          ← 下边框(空闲时上下都是整行 ─)
~/Desktop/qi (master) • tui               ← footer 1:cwd(+git 分支)(+会话名),dim
↑12k ↓678 1.2%/1.0M (auto)      deepseek/v4.1 • medium   ← footer 2:token 统计 + 右对齐模型
排队 2 / LSP 就绪                        ← footer 3:**只在这些内容时出现**(排队 / 扩展状态 / 瞬时提示);
                                            空闲时不出这一行 —— 默认不再有 `qi · auto`
```

### 1.3 实现要点

- **两种模式的 CSS 选择器**:`get_default_screen()` 在 `regular` 时给首屏打上 `.regular` 类
  (必须在首屏创建时就带上 —— 放到 `on_mount` 里再加,inline 启动的第一帧会按 fullscreen
  规则把 Screen 撑到终端高,内联区域先空掉一屏再缩回去)。
- **fullscreen 的布局**:`Screen` / `#body` `height: 100%`,`#log { height: 1fr }` 占满剩余
  空间并在**内部**滚动,编辑器 + 上下边框 + footer 固定在底部。`_sync_log_height()` 在这里
  **不插手** —— 再用 inline style 塞 `max-height` 会把 `1fr` 盖掉。
- **regular(inline)的布局**:`App.run(inline=True, inline_no_clear=True, mouse=False)`;
  `Screen` 高度随内容,transcript 最多占 `终端高 - (编辑器 3 + footer 2~3)`,再长在内部滚动。
  扩展挂件的两个槽(`#ext-widgets-above/below`)空时**不占行** —— 否则会在输入框与
  footer 之间各撑出一块空白。
- **鼠标只在 fullscreen 开**:fullscreen 下开着上报**就是为了滚动**(滚轮/拖动喂给 qi 的
  transcript,而不是终端回滚缓冲)。regular 下必须关:上报鼠标会让**不支持 SGR(1006)**
  的终端退回旧式 X10 报文(`ESC [ M` + 原始坐标字节),坐标 ≥ 0x80 时整段不是合法 UTF-8,
  Textual inline 驱动里的严格解码器会抛 `UnicodeDecodeError` → 输入线程死 → `App.panic`
  把 TUI 带栈带走(`0x85 in position 4`)。上游未修(textualize/textual#6456),
  所以 `tui.py` 里另加两层:进界面前 `_reset_mouse_reporting()` 关掉残留上报
  (上次崩溃没走到还原时终端会一直留着),`_harden_inline_input()` 只把
  `linux_inline_driver` 的解码器换成 `errors="replace"`(坏字节变 U+FFFD,
  一个坏字节不该打死会话)。
- **命令选择器 = pi 的 `showSelector()`**:pi 是把 `editorContainer` 的内容**换掉**,
  所以选择器与编辑器同宽、贴在同一条底线上(上面 transcript 不动、下面 footer 不动),
  形状是「上下 `DynamicBorder` + accent bold 标题 + `→` 列表 + 键位提示」,**无底色无遮罩**。
  qi 用 `EditorSlotPanel`(ModalScreen)做到同一形状:全宽、`align-vertical: bottom`、
  `margin-bottom` = footer 实际行数、`border-top/bottom: solid`、底色取 `$background`
  (= 探测到的终端底色,看不出“填色”但又盖住底下的编辑器)、backdrop 透明。
  列表渲染与补全面板**同一份**(`pi_select_text`,pi 的 `SelectList` 版式)。
  非选择器类的面板(`PromptScreen` / `EditorScreen` / `CustomScreen` / 会话 / 树 / scoped)
  共用同一外壳,只换正文。
- **主题**:`theme` 设 `auto`(默认)时用 OSC 11 探测终端背景色 → dark/light,
  并把探测到的背景色设为 Textual 主题底色 —— 于是区域看不出“被填色”。
  `QI_THEME=dark|light|auto` 可直接覆盖。
- **编辑器**:上下两条 `─` 动态边框(`border` 色);工作态上边框嵌入
  `⠋…⠏ Working`(accent + muted)。
- **markdown**:Rich `Markdown` 渲染,样式名字映射到 pi 的 `md*` 键;
  代码高亮用 pygments 主题但颜色取自 pi 的 `syntax*` 键(无底色)。
- **settings 驱动**:`quietStartup` / `hideThinkingBlock` / `editorPaddingX` / `outputPad` /
  `autocompleteMaxVisible` 已接(启动时落到控件上),见 [settings.md](settings.md) §3。
- **分派行**:`● → <display_name> (source, 置信度)`,无底色,不加卡片。

已知差异(Textual 与 pi 自研渲染器的边界,不做逐字节对齐):OSC133 zone 标记、
代码块左侧 `│` 边线、图片/kitty 协议。`tuiMode` 改完要**重启**才生效(pi 能当场换渲染器);
`tuiMode` **不在 `/settings` 面板里** —— 面板只放值域有限的那几个键(见 settings.md §3),
改渲染模式走 `qi config --set tuiMode` 或 `--tui-mode`。`/settings` 是主 TUI 里的模态面板
(与 `/model` 同一套 `EditorSlotPanel` 外壳,占编辑器那一格);资源面板(`qi config` 里)仍是
CLI 起的 inline 小 App —— 那里没有跑着的事件循环;fullscreen 退出时**不重放 transcript**
(pi 有 `fullscreenExitOutput: transcript|resume-hint`)。

## 2. `/` 内部命令

> 以**实现状态**为准(不再写“草案”)。pi 的 23 个内置命令见 `dist/core/slash-commands.js`;
> 下表里「计划中」= qi 尚无对应后端能力,单独提示,不冒充“未知命令”。

### 已实现(对齐 pi)

| 命令 | 说明 |
| --- | --- |
| `/hotkeys` | 快捷键(明写哪些 pi 键位还没做) |
| `/quit` | 退出 |
| `/new` | 新会话(**不落文件** —— 与裸 `qi` 一样是预留,说话才落盘) |
| `/resume [id]` | 不给 id = 打开**会话选择器**(占编辑器那一格的面板);给 id = 直接恢复 |
| `/name <name>` | 会话显示名(进 footer) |
| `/session` | 会话信息(ID/文件/cwd/消息数·仅当前分支/节点数与分支点/模型/用量) |
| `/tree` | 会话树:跳到本会话任意节点继续(同文件内分支,见下节) |
| `/fork [序号\|id]` | 从某条用户消息**之前**分叉出新会话,并把那条消息放回编辑器(对齐 pi) |
| `/clone [名字]` | 把当前分支复制成新会话 |
| `/compact [提示]` | 压缩上下文:把旧消息压成结构化摘要(可给一句关注点) |
| `/model [p/m]` | 不给参数 = **开选择器**(与 ctrl+l 同一个,pi 的 `showModelSelector`;`✓` 标当前、`[provider] · default` 徽标)；给 `p/m` 或唯一模型名 = 直接切。切换会往会话里落一条 `model_change`,**下次打开这条会话仍用它**([sessions.md](sessions.md) §6.1) |
| `/scoped-models` | 挑 Ctrl+P 轮换哪些模型(面板里勾选;空 = 全部,写回 `settings.enabledModels`) |
| `/export [file]` | 导出会话 JSONL(默认 `./qi-<id>.jsonl`;**pi 默认导出 HTML** —— qi 无 HTML 导出器) |
| `/import <file>` | 从 JSONL 导入并切换会话(重名给提示,不静默覆盖) |
| `/copy` | 复制最后一条回答到剪贴板(OSC 52) |
| `/reload` | 重载 agents / plugins / 配置(主题改动需重开) |
| `/login [provider]` | 不给 provider 先弹**选择器**(列 `models.json` 里的 provider + `(已存凭证)`/`(无凭证)`),然后**遮罩输入** API key → 写 `auth.json`(0600)并重建客户端;key 不进 transcript。当前**没可用模型**时顺带选中该 provider 的第一个模型(pi 同口径)。与 pi 的差异:pi 的 `/login` 主要在做**订阅登录**(`auth.oauth`),qi 的凭证层只有 api_key |
| `/logout [provider]` | 不给 provider 先弹已存凭证的选择器;删的是 `auth.json` 里那条(**环境变量与 `models.json` 的 `apiKey` 不受影响**,消息里会说) |
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

**裸 `qi` 与 `/new` 走懒建**:会话对象立刻就有(所以 `/session`、footer、扩展的
`ctx.session_manager` 都照常工作),但**文件推迟到第一条助手回答**才出现。这样"进来看看就退出"、
"连点两次 `/new`"、"问了句就被 Ctrl+C"都不会在会话目录里留下空文件
(该事故的成因与三态见 [session-format.md](session-format.md) §9.1)。`--no-session` 更进一步:
它是**内存会话**,聊完整轮也不产生文件。

qi 与 pi 的差异(已落档):

- `/tree` 选择器已支持 pi 的过滤键与搜索、标签(见下表);**未做**折叠/展开
  (`alt/ctrl+←/→`)、树内过滤的持久化(pi 把 `treeFilterMode` 存进 settings);
- 标签存储不同:qi 直接写在 entry 上(`label` / `labelTimestamp` 字段),pi 另写一条
  `type=label` 的 entry;两边都是「标签属于某条 entry」的语义;
- `/tree` 跳转时 pi 会**先问**要不要摘要被放弃的分支,qi 也一样先问(默认「不摘要」);
- TUI 里回放会话(以及 web `/messages`)只展示**当前分支**(web 契约仍把 header 放在 `entries[0]`);
- 除了 `--export` 与 TUI 回放,其它导出仍拷**整个文件**(含其它分支)。

树选择器键位(模态,`/tree`;对齐 pi 的 `app.tree.*`):

| 键 | 行为 |
| --- | --- |
| 输入框打字 | 搜索节点(空格分词,全部命中才显示) |
| `↑` / `↓` / `enter` | 选 / 跳到该节点继续 |
| `ctrl+d` / `ctrl+t` / `ctrl+u` / `ctrl+l` / `ctrl+a` | 过滤:默认(隐藏状态类)/ 隐藏工具结果 / 只看用户消息 / 只看有标签 / 全部 |
| `ctrl+o` / `shift+ctrl+o` | 过滤模式循环 / 反向循环 |
| `shift+l` | 编辑标签(输入框变标签编辑器,`enter` 保存;留空 = 清除) |
| `shift+t` | 标签是否附带时间戳 |
| `escape` | 先退出标签编辑,再关面板 |

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
- qi 与 pi 的差异pi 在 `/tree` 跳转前会**先问**要不要摘要 —— qi 现在也一样(默认「不摘要」);`settings.branchSummary.skipPrompt: true` 关掉这一问(= 也不摘要,与 pi 的 defaults to no summary 同义)。但这句已不再成立:pi 还有
  `branch_summary` 的树内过滤/标签渲染,qi 只显示块。

### 命令(内置;**输入 `/` 补全全部** —— 扩展注册的命令也在里面)

两类**动态**命令不在这张表里,但同样出现在 `/` 补全:扩展用 `api.registerCommand` 注册的
(`/mcp` 那种),以及技能注册的 **`/skill:<名> [参数]`**(加载并执行该技能的全文;
由 `settings.enableSkillCommands` 控制,默认开)。

| 命令 | 说明 |
| --- | --- |
| `/hotkeys` · `/quit` | 键位 / 退出 —— **这两条扩展不能顶掉**(顶掉 `/quit` 等于把用户锁在界面里) |
| `/new` · `/resume` · `/session` | 新会话 / 选或恢复(选择器里可改名、删除)/ 会话信息 |
| `/name` | 设置会话显示名 |
| `/tree` · `/fork` · `/clone` | 跳到本会话任意节点(跳前先问「要把被放弃的那段压成摘要吗」,默认不摘要;`settings.branchSummary.skipPrompt` 可关掉这一问)/ 从某条消息 fork / 复制当前分支 |
| `/trust [yes\|no\|forget]` | 按**目录**记住信任决定(`~/.qi/agent/trust.json`);连带记住上一层,写完要重启才生效 |
| `/compact` | 压缩上下文(摘要旧消息);可跟 `<提示>` 追加 focus |
| `/model` · `/scoped-models` · `/thinking` | 不给参数 = **开选择器**(占编辑器那一格,与 ctrl+l / shift+tab 同源)/ 挑 Ctrl+P 轮换的模型 / 挑思考级别(`✓` 标当前、带说明、`· default`)。两者的切换都会落 `model_change` / `thinking_level_change` entry,下次打开这条会话按它还原([sessions.md](sessions.md) §6.1) |
| `/settings` | 偏好面板:主题 / 思考级别 / 交互开关(enter 换值、ctrl+s 保存到**用户级** settings) |
| `/export` · `/import` | 导出(按扩展名:`x.html` → 自包含 HTML(当前分支),其余 → JSONL)/ 导入会话 JSONL |
| `/copy` | 复制最后一条回答 |
| `/reload` | 重载扩展 / 技能 / 配置 |
| `/login` · `/logout` | 登录(选择器/遮罩输入,key 只进 auth store)/ 删除已存凭证 |
| `/changelog` | 显示 `CHANGELOG.md` |
| `/share` | 把当前会话传成**私有** GitHub gist(一个自包含 `.html`),并把链接复制到剪贴板。直调 API,不依赖 `gh`;token 顺序照 qi 的凭证总原则(**auth store → 环境变量**):`qi auth login github` → `GITHUB_TOKEN` → `GH_TOKEN`,需要一个带 `gist` 权限的 token |

扩展可以注册**自己的** `/命令`(`api.registerCommand`):重名时都留着并变成 `name:1` / `name:2`,
一个都不丢;它们会出现在 **`/` 补全**里(带描述与来源,见 [extensions.md](extensions.md) §3.2)。

### 计划中(pi 有,qi 缺后端能力)

（无 —— pi 的斜杠命令面已全部对齐；`PLANNED_COMMANDS` 现在是空集合。）

### 键位(已对齐 pi;`core/keybindings.js`)

| 键 | qi 行为 | pi 的 action |
| --- | --- | --- |
| `escape` | 中断当前回合:**协作式**(置位 AbortSignal,runner 干净收尾 —— 未执行的工具补上“已中断”结果、半截回答照常落盘);再按一次、或 3s 宽限期到点仍未收尾 → `cancel_all()` 强制终止 | `app.interrupt` |
| `escape` ×2 | 空编辑器连按两次(500ms 内):按 `settings.doubleEscapeAction` 开 `/tree` 或 `/fork`(默认 `tree`) | `getDoubleEscapeAction` |
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
- **默认 `medium`**(pi `DEFAULT_THINKING_LEVEL`):什么都不配时 footer 右边就是
  `模型 • medium`,不是 `• thinking off`;写歪的值仍按 `off` 处理(两件事不混)。
- 思考内容与回答**分开流式**(`thinking_delta`),灰色斜体渲染;`ctrl+t` 可随时隐藏/显示。
- **provider 拒收 `reasoning_effort` 时**(实测自建 LiteLLM 代理默认就是这样)自动去掉参数重试,
  并在 footer 提示一次“已按不思考运行”——不会因此整轮失败。
- 思考内容**不落盘**(仅当轮展示):会话 JSONL 仍只有 message/tool/dispatch/state 四类。
| `ctrl+x` | 复制最后一条回答 | `app.message.copy` |
| `ctrl+g` | `$EDITOR` 编辑当前输入 | `app.editor.external` |
| `ctrl+l` | 模型选择器(占编辑器那一格的面板,见 §1.3) | `app.model.select` |
| `ctrl+p` / `ctrl+shift+p` | 下一个 / 上一个模型 | `app.model.cycleForward/Backward` |
| `ctrl+z` | 挂起(回到 shell) | `app.suspend` |

实现注记:Textual 的 `Input` 默认把 `ctrl+c/ctrl+x/ctrl+d` 绑到「复制/剪切/删右侧」,
qi 用 `priority=True` 抢过来以匹配 pi 语义(`ctrl+d` 非空时仍自己调 `delete_right`);
另关闭 Textual 的 command palette,因为它的默认键 `ctrl+p` 在 pi 里是切模型。

**尚未对齐**(qi 缺后端能力或输入层,`/hotkeys` 里也如实列出):

| 键 | pi 用途 | qi 缺什么 |
| --- | --- | --- |
| `ctrl+v` | 粘贴图片 | 无图片输入,目前只会粘文本 |

会话选择器(`/resume`,占编辑器那一格;对齐 pi 的 `SessionSelectorComponent`):

| 键 | 行为 |
| --- | --- |
| 输入框打字 | 过滤。三态语法:`空格` 分词(模糊子序列)/ `"短语"` 精确连续 / `re:<正则>` 正则(语法错 → 列表里报错) |
| `tab` | 切换范围:**当前目录**(默认)↔ **全部** |
| `↑` / `↓` / `enter` | 选 / 恢复 |
| `ctrl+n` | 只看命名会话 |
| `ctrl+s` | 排序循环:树状(threaded)→ 最近(recent)→ 最相关(fuzzy)。pi 的三档同名同义 |
| `ctrl+p` | 显示 / 隐藏会话文件路径(默认关) |
| `ctrl+r` | 重命名选中会话(整块换成名字输入框,`enter` 保存) |
| `ctrl+d` | 删除选中会话:**先确认**(`enter` 确认、`escape` 取消);当前会话删不掉 |
| `escape` | 先退出重命名态 / 取消删除确认,再关面板 |

版式与 pi 一致:一行一条(没有 Textual `OptionList` 的 `tall` 边框与 `$surface` 底),
光标 `›`、选中行整行 `selectedBg` 底色,左侧名字按状态上色(当前 = accent、有名字 = warning),
右侧 muted 的 `消息数 年龄`(范围 = 全部时再前置 cwd),放不下时左半优先、右侧收成 `…`。
头部三行 = 标题(范围 + `名字:` + `排序:`)+ 范围指示(`◉ 当前目录 | ○ 全部`)+ 两行键位提示。

**没起过名的会话显示第一句话**(pi 的 `firstMessage` 回落):老会话的默认标题 `tui`
不算名字,所以那批会话现在是"按内容认"而不是一排同名条目(见 [sessions.md](sessions.md) §7)。
树状排序按 header 的 `parentSession` 缩进(`└─`/`├─`,深度 > 0 时画 `│` 延续线)——
`/fork` / `/clone` / `qi --fork` 建的会话都带这个字段。

实现注记:Textual 的 **priority 绑定是从 App 往下检查**的(`reversed(_binding_chain)`),
所以 App 级的 `ctrl+d/o/t/l/p/x/c` 会盖掉模态里同名的键 —— 而 pi 的选择器恰好全用这些键。
qi 的做法:`QiTui.check_action` 在 `screen_stack > 1` 时把 App 级快捷键返回 `False`,
让模态自己的 priority 绑定接管(仅模态打开时生效,关上面板立即恢复)。

### 补全与 bash 模式(对齐 pi 的 autocomplete / `handleBashCommand`)

| 触发 | 行为 |
| --- | --- |
| 行首 `/xx` | 命令名补全(候选来自 `TUI_COMMANDS`,与 `_command` 的已实现分支对应) |
| `/model` `/thinking` `/login` | **参数补全**(对齐 pi 的 `getArgumentCompletions`):模型 `provider/model`、思考级别、provider |
| 任意位置 `@xx` | 路径补全:**装了 `fd` 就走全树搜索**(尊重 .gitignore),否则只扫当前目录一层;目录优先、以 `/` 结尾继续往下补 |
| `@"带 空格的路径"` | 引号路径:带空格(或已在引号里)的候选自动补成对引号;目录的收尾引号不闭合(光标停在它前面,可继续往下补) |
| `tab` | 接受候选:命令补一个空格、文件补一个空格、`@目录/` 与参数补全不补;**无候选时仍是缩进** |
| `↑` / `↓` | 面板开着时选候选;否则在行首/空编辑器时翻输入历史,其余情况移动光标 |
| `escape` | 先关面板,再考虑中断 |
| `!<命令>` | 执行 shell,输出以 pi 同款「上下 `─` + `$ cmd` + 输出」块展示,并把「命令 + 输出」落成一条 user 消息供后续回合参考 |
| `!!<命令>` | 同样执行,但**不进上下文**(块边框变 `dim`,标题标出) |

面板版式(对齐 pi 的 `select-list.js`):画在编辑器**下边框之下**(不是输入框上方),选中行
`→` 前缀 + `accent` 标签,其余候选的说明用 `muted`,标签按列对齐(命令列 12..32 自适应,
其余固定 32);**没有底色 / 边框 / 滚动条**,候选超过 `autocompleteMaxVisible` 时尾部给
`(n/m)`。来源标签(`[u]/[p]/[t]`)写在**说明里**(pi 的口径),不是标签前缀。
面板与编辑器同缩进(共用 `editorPaddingX`)。

与 pi 的差异(已落档):路径补全的**来源**基本对齐(引号路径、`fd` 全树、目录优先、分隔符
含空格/tab/`"`/`'`/`=`),但 fd 的**正则语义**是简化的(按路径段前缀连成 `a[\/]b`,`pi` 还叠了
评分排序与 scoped 查询);回退扫描不分隐藏文件、不做模糊排序。
隐藏文件:走 fd 时会像 pi 一样列出(它总带 `--hidden`);回退扫描只在 `@.` 开头时才列。
pi 的候选还带来源标签 `[u]/[p]/[t]`(user / project / third-party)—— qi 的候选结构
(`Candidate`:value/label/detail/source)与标签渲染已就位,但 prompt template 与插件命令
这两个来源还没接,所以目前实际只会渲染内置候选(无标签)。
pi 在 bash 运行中会拒绝再跑一条;qi 允许并发。两者都不影响普通聊天。

### 输入层:多行编辑器(对齐 pi `pi-tui/components/editor.js`)

qi 用 Textual 的 `TextArea` 做成 pi 的编辑器,补齐了这些键(其余是 Textual 自带):

| 键 | 行为 | pi 的 action |
| --- | --- | --- |
| `enter` | 提交 | `tui.input.submit` |
| `shift+enter` / `ctrl+j` | 换行 | `tui.input.newLine` |
| `ctrl+b` / `ctrl+f` | 光标左 / 右 | `cursorLeft` / `cursorRight` |
| `↑` / `↓` | 无补全面板时翻输入历史(首次进入留住草稿、手动改动即退出);否则移动光标 | `historyPrevious` / `historyNext` |
| `alt+b` `alt+f` `alt+←/→` | 按词移动 | `cursorWordLeft` / `cursorWordRight` |
| `alt+d` | 删后一个词 | `deleteWordForward` |
| `ctrl+-` | 撤销 | `undo`(pi 不用 `ctrl+z` —— 那个是挂起) |
| `ctrl+w` / `ctrl+u` / `ctrl+k` / `alt+backspace` | 删词 / 删到行首 / 删到行尾 | 同名 action(Textual 自带) |
| `ctrl+y` / `alt+y` | kill-ring 的 yank / yank-pop(删掉的文本进环、连续删合并) | `tui.editor.yank` / `tui.editor.yankPop` |
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
- **kill-ring**(`KillRing`,对齐 pi 的 `kill-ring.js`):在 `Editor` 覆写 Textual 的删除
  action,按「删前/删后文本求差」拿到真正删掉的段进环;`ctrl+y` 粘回、`alt+y` 轮换。
  已知差异:`ctrl+k` 在行尾/空行时 Textual 走「并下一行 / 删整行」,这两支不进环
  (pi 会推一个 `\n`)。

qi 的输入层是**多行编辑器**(见下面「输入层」一节):`/` 与 `@` 补全、参数补全、输入历史、
`!` bash 模式、消息队列都已落地。

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
- web v2 视图复用同一事件流与渲染结构([web.md](../design/web.md))
