# 与 pi 的对照(汇总)

> **为什么有这个文件**:`docs/` 各页原来自带的「与 pi 的对应 / 差异 / 对照」小节,按"手册只讲 qi
> 怎么用、上游对照留给设计文档"的口径统一搬到这里。**那些小节已从 `docs/` 删除,且不留回指链接。**
> 参照物:pi v0.85.1(pi-coding-agent)的 `docs/` 与 `dist/`。行为一律以本仓代码为准 ——
> 上游改动后这张表可能过时,发现不一致先看代码。
>
> 各页正文里仍保留**逐处的短注**(如"对齐 pi 的 `--tools`"、某列"pi 的 action"),那些是使用说明的
> 一部分;这里收的是成节的对照与"为什么不一样"。

## 1. 命令行面(原 `cli.md` §10)

**第一类:接受旗标,但功能还没实现** —— 传了会退出码 2 说清缺什么(不假装生效):

| pi 旗标 | 缺什么 |
| --- | --- |
| `--prompt-template` / `--no-prompt-templates` | qi 没有 prompt 模板的发现 / 注入机制。要固定前缀就写进 `.qi/SYSTEM.md`,或做成技能 |
| `--theme` / `--use-theme` / `--no-themes` | 不支持自定义主题文件(内置只有 `dark` / `light` / `auto`)。选主题用 `QI_THEME=light` 或 `qi config --set theme=` |
| `--mode rpc` | 输出模式只有 `text` / `json`;stdio JSON-RPC 是 v2 项(见 [PLAN.md](PLAN.md) 的 v2 清单) |

**第二类:明确不保留**(理由落档):

| pi 的 | 为什么 qi 不做 |
| --- | --- |
| `/share` 的 **Radius** 路径 | 那是 pi 的**托管服务**(上传 JSONL,并先注入 `pi.share` entry:`systemPrompt` + 工具定义);qi 没有对应物,不做 |
| `/share` 的 **viewer 预览链接** | pi 展示自己的预览服务链接(`getShareViewerUrl(gistId)`);qi 只能给 gist 链接本身 |
| `/share` 的 **`gh` 依赖** | **刻意偏离**:pi 的 gist 路径就是 shell 出 `gh`(先 `gh auth status` 查登录,再 `gh gist create --public=false`);qi **直调 API**(stdlib `urllib`),少一个外部依赖 |
| `pi config` 的资源启停 TUI | **已对齐**(`qi config` TTY 下面板) |
| `-e/--extension <source>` 的 **npm / git** 来源 | Python 侧的分发走 pip(`settings.packages`);没有 npm / git 那种"从任意源拉"的通道 |
| `--append-system-prompt` 传**文件路径** | **已实现**(值是可读文件就读文件内容) |

> `pi install` / `remove` / `uninstall` / `update` **已对齐** —— 它们曾在这一节里,理由写的是
> "封装 pip 会藏起环境差异";现在的做法把那条顾虑直接讲给了用户(先打命令再跑、失败补 uv 出路),
> 所以不再需要靠"不做"来避开它。
>
> 曾经把 `--provider` / `--api-key` / `--models` 列在这里(理由写"模型在 `models.json` 配置")——
> 那三条现在都实现了,而且那条理由记错了对象:`--models` 是 Ctrl+P 的轮换清单,不是"模型配在哪"。
> skill 加载开关(`--skill` / `--no-skills`)也早已对齐。

命令面本身**刻意与 pi 对齐**:裸命令进 TUI、`-p` 无头、`-c` 续会话、`--session` / `--fork` / `-n`、
`--mode json`、`-e <path>`、`--ext`、`-a`。差异集中在 qi 多出来的部分(扩展声明层、`qi list`、
信任不记忆)。要按 pi 的习惯敲命令通常能 work,遇到不认识的就 `qi --help` —— **不要照 pi 的
`docs/cli.md` 抄 qi 的选项**(两者并不完全一致)。

逐个开关的对齐清单(原先 `docs/cli.md` §1/§2 的「pi 对齐」列)已随本节一起从手册移除 ——
手册现在只讲 qi 自己的选项;要复核某个开关对没对上,只能拿上游 `docs/cli.md` 与这里的表逐项比。

## 2. 会话(原 `sessions.md` §8)

| | pi | qi |
| --- | --- | --- |
| 文件位置 | `~/.pi/agent/sessions/` | `~/.qi/agent/sessions/`(同构;qi 多一个 `sessionDir` 覆盖) |
| 名字 | `session_info` entry 的 `name`(每次改名追加一条,**最后一条胜**) | header 的 `title`(`set_title()` 整文件重写;旧会话兼容) |
| 起名 | **不自动起名**:没名字就显示第一句话 | 模型自动起名,另加第一句话回落 |
| 分叉来源 | header `parentSession`,选择器按它排成树 | 同名同义(`Session.parent_session`),`/fork` `/clone` `--fork` 都写 |
| 会话格式 | JSONL + 树(`id`/`parentId`) | 同构,见 [session-format.md](../docs/session-format.md) |
| 续接 / 指定 / 分叉 | `-c` / `--session` / `--fork` | 同名同义 |
| 新会话落盘时机 | `newSession()` 起就有对象,文件推迟到第一条 assistant 回答(`_persist`) | 同构:`reserve()` 预留,`_has_assistant` 触发 `flush()` |
| 不落盘运行 | `SessionManager.inMemory()` | `--no-session` → `store.ephemeral()`(同义) |
| 分支交互 | `/tree` | `/tree` + 跳分支自动写 `branch_summary` |
| 模型/级别随会话 | `model_change` / `thinking_level_change` entry,续接时还原 | 同构(键名 `model_id`,见 [session-format.md](../docs/session-format.md) §6.1);还原前多一道凭证检查 |

qi 在这一层**刻意与 pi 保持一致**:字段名、命令名、`/tree` 语义都对齐,便于两边共享同一套认知。
差异集中在 qi 多出来的部分(扩展自定义 entry、`branch_summary` 的自动生成)。

## 3. 上下文压缩(原 `compaction.md` §8)

| | pi | qi |
| --- | --- | --- |
| 阈值公式 | `contextWindow - reserveTokens` | 同 |
| 默认值 | `reserve 16384` / `keepRecent 20000` | 同 |
| 切点 | turn 边界或 assistant,**不在 tool 上** | 同 |
| split turn | 有 | 同 |
| 摘要格式 | 同一套 7 段结构 | 同(提示词直接移植) |
| 摘要进上下文的方式 | `system \| summary \| kept 消息` | 同位置,但摘要用 **user** 角色(理由见 [compaction.md](../docs/compaction.md) §4) |
| `custom`(叙述/思考) | 无此类型 | **压缩时当它不可见** —— 因为 `_history()` 本来就不读它,保持一致,不额外发明规则 |

## 4. 主题(原 `themes.md` §6)

| | pi | qi |
| --- | --- | --- |
| 内置主题 | `dark` / `light` | 同,**逐字取自 pi**(两个 JSON 直接移植) |
| 选择优先级 | env → settings → 探测 → dark | 同 |
| 探测方式 | OSC 11 | 同(超时 150ms) |
| `theme` 取值 | 主题名(含自定义) | 只认 `dark`/`light`/`auto` |
| 自定义主题文件 | **支持**(pi 的 `themes.md` 教怎么写) | **不支持** —— 这是 qi 目前最大的主题缺口:没有主题目录的发现规则,也没有 `ThemeError` 之外的加载路径 |
| 主题 JSON 格式 | `vars` / `colors` / `export` | 同(所以上游加语义键时可以直接跟随) |

## 5. 安全与信任(原 `security.md` §6)

| | pi | qi |
| --- | --- | --- |
| 扩展/技能/项目上下文的分野 | 扩展=代码,技能与上下文=文本 | 同 |
| 未信任时挡什么 | 项目级扩展与项目级配置 | 同(项目级扩展) |
| 信任决定是否记住 | **记住**(`~/.pi/agent/trust.json`,可整目录继承) | **记住**(`~/.qi/agent/trust.json`;`/trust`,同样走祖先链) |
| 交互式询问 | 启动时询问是否信任该目录 | **未实现**(`ask` 保守判不信任) |
| `bash` 命令级过滤 | 无 | 无 |
| 工具级审批弹窗 | 不内置 | 不内置 |
| 凭证命令 `!cmd` | 有,不走 shell | 同 |

**照 pi 写会错的地方**:pi 的信任是"记住 + 询问",qi 的信任是"每次表态或写死 `always`";
所以文档不能说"qi 会问你一次并记住"。

## 6. 供应商与凭证(原 `providers.md` §7)

| | pi | qi |
| --- | --- | --- |
| 凭证来源顺序 | auth store → 约定环境变量 → `models.json` 引用 | **同** |
| `apiKey` 值语法 | `!cmd` / `$ENV` / 转义 | **同**(`!cmd` 同样不走 shell) |
| 订阅登录(Claude Pro/Max、Copilot、Codex、xAI) | **支持**(OAuth,带刷新) | **不支持** —— 只有 API key。`print-bearer-token` 与 `--no-refresh` 为兼容 pi 命令面而存在,语义是空操作 |
| auth store 结构 | `type` 支持多种(含 OAuth 凭证) | 只有 `type: api_key` |
| 云 provider(Azure / Bedrock / Vertex / Cloudflare) | 有独立章节 | 走 litellm 通用 provider 配置,没有专门文档 |

**没有 OAuth 的直接后果**:用订阅额度(而非 API key)的用户在 qi 上跑不通,必须换成 API key。
要补这块,需要的是 auth store 的 `type` 扩展 + 刷新逻辑 —— 现在没有([providers.md](../docs/providers.md) 因此不描述不存在的机制)。

## 7. JSON 事件流(原 `json.md` §6)

| | pi | qi |
| --- | --- | --- |
| 形状 | 事件流(JSONL) | 同 |
| 用途 | 脚本 / 上游服务 | 同 |
| stdout 纯净 | 是 | 是(提示走 stderr) |
| 事件名 | pi 的命名 | qi 自己的;`dispatch`/`opening` 是 qi 遗留 |

**与 pi 的差别集中在事件清单本身**:qi 有 `compaction_start`/`compaction_end`、`thinking_delta`
这类 pi 没有(或命名不同)的事件,`dispatch`/`opening` 则是 qi 去掉 auto 分派后留下的兼容读取面。
消费端应当**按 [json.md](../docs/json.md) §2 的表实现,并对未知 `kind` 容忍**,而不是照 pi 的清单写死。

## 8. 技能(原 `skills.md` §7)

| | pi | qi |
| --- | --- | --- |
| 发现规则 | `~/.agents/skills` + 项目 `.agents/skills`(递归、含 SKILL.md 即技能) | 同构(第 1、4 层) |
| 私有层 | 无 | **多 4 层**:`~/.qi/agent/skills`、`<cwd>/.qi/skills`、以及两侧 `settings.skills[]` |
| 注入方式 | `<available_skills>` + 渐进披露 | 同(共用同一形态) |
| `--skill` / `--no-skills` | 有 | 同,且组合语义一致(`--no-skills` 不影响 `--skill`) |
| 同层同名 | 报错 | 同 |
| 跨层同名 | 高优先覆盖 | 同 |

**照 pi 写会错的地方**:qi 的层数与顺序不同([skills.md](../docs/skills.md) §1 那张表),跨工具目录
不接受根级 `*.md`,而 qi 的私有层接受且相对路径按各自基准解析。要判断一个技能实际会不会被加载,
以 `top_level_skill_dirs()` 的顺序为准。

## 9. 系统提示词(原 `configuration.md` §1 对照表)

pi 里其实是两个正交概念:编译进 dist 的**默认 prompt**(代码内字符串,永远存在)与
`SYSTEM.md` / `APPEND_SYSTEM.md`(可选覆盖 / 追加)。qi v2 采用同一形态:

| | pi 0.85.1 | qi v2 |
| --- | --- | --- |
| 默认基座 | 代码内字符串 + 运行时拼接 | `src/qi_agent/system_prompt.py`(同样代码内) |
| 覆盖 | `SYSTEM.md` **整体替换**默认 prompt | 同左(`<cwd>/.qi/SYSTEM.md` > `~/.qi/agent/SYSTEM.md`) |
| 追加 | `APPEND_SYSTEM.md` | **不实现**(v1 决策保留;需要时用 `SYSTEM.md` 写全;命令行侧有 `--append-system-prompt`) |
| 动态块 | 工具清单、条件化 guidelines、`<project_context>`、`<available_skills>`、cwd | 同左;v1 还多过**数据源清单**与**角色层**两项 qi 独有内容,两者随 P-E4c 移出 core(角色层现由 qi-agents 经 `before_agent_start` 注入) |
| 祖先链边界 | 走到文件系统根 | **走到文件系统根**(与 pi 一致;`.agents/skills` 那条另止于 git 根) |
| 自身文档索引 | README/docs/examples 绝对路径 + 12 条指路 | 从 `docs/docs.json` 的 navigation 生成(见 [configuration.md](../docs/configuration.md)) |

## 10. 开发与测试(原 `development.md` §7)

| | pi | qi |
| --- | --- | --- |
| 环境准备 | `npm install` + `npm run build` | `uv sync`(+ 三个扩展 `pip install -e`) |
| 从源码跑 | `./pi-test.sh` | `uv run qi …` |
| 测试入口 | `./test.sh`(非 LLM)/ `npm test` | **`.venv/bin/python -m pytest`**(没有 `test.sh`) |
| 包结构 | monorepo:`packages/*`,锁步发版 | 单包 core + `extensions/*` 三个独立包(锁步 `0.1.0`) |
| 设计记录 | `AGENTS.md` + `CHANGELOG.md` | `design/`(PLAN、决策记录、变更记录) |

差异最大的一行是**测试入口**:qi 没有包装脚本,`pytest` 就是入口。写进文档是为了避免有人照 pi 的
习惯敲 `./test.sh` 之后以为仓库坏了。

## 11. 扩展机制(原 `extensions.md` §2 / §2.1 / §3.4)

> 本节标题与行文里的「原 §N」= 改造**前**手册的编号;手册现已按「怎么写扩展」重排(§1–§12),
> 同名内容的现址见 [extensions.md](../docs/extensions.md)。

### 11.1 概念对照(原 §2)

| 概念 | pi | qi |
| --- | --- | --- |
| 代码单元 | extension(`*.ts`,default export 工厂) | **extension**(`*.py`,`register(api)` / `default(api)`) |
| 分发单元 | package(npm/git/本地,`pi install`) | **packages 声明层**(`settings.packages`,pip 生态)+ 装 pip 包 / 放目录;qi **不提供 `install` 子命令**,装配由 `qi list` / `qi doctor` 做声明比对 |
| 全局目录 | `~/.pi/agent/extensions/*.ts` 或 `*/index.ts` | `~/.qi/agent/extensions/<name>/extension.py` |
| 项目目录 | `.pi/extensions/`(信任后才加载) | `.qi/extensions/`(信任后才加载) |
| 附加路径 | `settings.extensions`(路径列表) | 同名,同一个语义 |
| 单次试用 | `pi -e <path>` | `qi -e <path>`(TUI 与无头两个模式都生效,只本进程,scope=temporary) |
| 入口文件名 | 无固定名(`*.ts` / `index.ts`) | 固定 `extension.py`(E9:qi 保持"1 目录 = 1 扩展") |
| 依赖 | `dependencies` + `peerDependencies`(宿主提供) | 同构,但 Python 没有 peer:明文规定**扩展不得把宿主写进 `dependencies`**(见 [extensions.md](../docs/extensions.md) §10) |

### 11.2 qi 的增量(pi 没有的)(原 §2.1)

| 增量 | 用途 | 为什么 pi 没有 |
| --- | --- | --- |
| `provides_config(kind, types)` | 声明消费的 **agent 配置种类**,无提供者则整个配置种类不装载(静默跳过) | pi 的扩展没有"消费型配置"这个概念;`data_sources.json` 是 qi 独有的 |
| `add_route(path, handler)` / `add_static(path, dir)` —— **未实现**(P-E5) | 扩展往 HTTP 宿主挂页面/路由 | pi 不做 HTTP 宿主(`--mode rpc` 外置),`design/web.md` 已定"宿主在框架 + UI 插件化" |
| ~~`registerProfile(name, spec)`~~ | ~~注册具名 `{system_prompt, tools, model}`~~ → **已否决(E14)** | 扩展用 `registerFlag` + `before_agent_start` 自己实现"按名切换运行配置",core 零新增概念 |

**边界原则**:增量只做"宿主必须先知道"的事(配置门控、HTTP 挂载)。凡是扩展自己能用工具 + 事件做到的,
不往上加 —— 每加一条,扩展面就离 pi 远一分。

### 11.3 剩余差异(原 §3.4)

已经全齐的面:事件(36/36)、`api.*` 方法(pi 的 26 个 + qi 的 6 个增量)、
`ctx` 成员(基础 18 项 + 命令上下文 7 项)、**`ctx.ui` 的全部 28 个方法**
(含组件层的 `set_widget` / `custom` / `set_footer` / `set_header` /
`set_editor_component` / `add_autocomplete_provider` / `on_terminal_input`)、
**渲染回调与工具渲染钩子(TUI 已消费)**、命令参数补全、`/resume` 选择器过闸门。

| 剩余差异 | 形状 | 为什么 |
| --- | --- | --- |
| `ctx.ui.set_editor_component` 的**边界** | 要求组件是 **`TextArea`(或子类)**;不是则记 note 并拒绝 | qi 的编辑器是承重的(历史环 / kill-ring / 补全 / 光标定位)。把边界画在 TextArea 上,15 处调用点就不必各自做兼容 —— 写一个 `TextArea` 子类仍然能做 vim 式模态编辑(拦截按键、自己改文本) |
| `ctx.ui.add_autocomplete_provider` 的**形状** | `factory(text, cursor_offset) -> list[str \| {value, label, description}]`;**返回退订函数**(pi 返回 void) | pi 传的是 pi-tui 的 `AutocompleteProvider` 对象(包住内置 provider)——那暴露的是 pi-tui 的补全内部结构。qi 用与命令参数补全**同一套元素形状**,两种补全只学一次 |
| `ctx.ui.on_terminal_input` 的 `data` 改写 | 只支持 `{"consume": True}`(吃掉按键);不支持"换成另一个键" | Textual 的 `events.Key` 不是为改写设计的。需要改写按键的场景应当用 `set_editor_component` 接管编辑器 |
| `register_command` 的 `get_argument_completions` / `add_autocomplete_provider` 返回 **async** | **支持**(只是晚一拍):结果回来时若文本与光标未动就回填,动了就丢掉 | 补全主路径是同步的(每次按键都要算),所以 async 走 worker + 一次重刷;回填要校对文本,否则旧的候选对不上现在的 token |
| `constrainedSampling` | **收下但忽略**(注册时记一条 note) | 逐工具受限采样要 provider 配合(JSON schema / grammar),litellm 没有可移植的对应物。**收而不报才是最坏的**:扩展会以为参数被约束住了 |
| `user_bash` | 只在 TUI 发 | 无头模式没有 `!` 命令这回事 |
| `agent_settled` | 保守近似 | qi 没有回合级重试/重压机制;provider 级重试在客户端内部,那时 `stream()` 还没返回 |
| 事件 payload 的**结构** | 键名已双份,但**结构**不同处不假造:如 `turn_end` qi 给 `{turn_index, text, tool_calls}`,pi 给 `{turnIndex, message, toolResults}`;`agent_end` 同理 | qi 的 payload 面向自己的前端词汇;加一个键是兼容,换一套结构就是另一回事了 |
| `deliver_as` 取值 | `follow_up` / `next_turn`(pi `followUp` / `nextTurn`) | qi 的 snake_case 约定;**两套都收** |
| 设置类 entry 的键名 | `model_id`(pi 是 `modelId`) | E28:语义对齐、命名守本地约定(本仓 entry 一律 snake_case:`custom_type` / `agent_id` / `duration_ms`) |
| `register_flag` 的取值通道 | 只能 `--ext name=value` | E19:typer 的选项表静态,放宽未知长旗标会把用户的笔误变成一句 prompt |
| 入口文件名 | 固定 `extension.py`(pi 是任意 `*.ts` / `index.ts`) | E9:qi 保持"1 目录 = 1 扩展" |
| `add_route` / `add_static` | 未实现 | qi 增量(HTTP 挂载),不属 pi 面 |

**已对齐的几件(上一版还列在"剩余"里)**:`renderShell`、`executionMode`、payload 的 `type` 字段
与驼峰键别名、async 参数/补全提供者、**模型/级别的落盘 + 环境变量 + 续会话还原**(见 E28)。

#### `executionMode` 的并发语义(E27)

pi 的 `ToolDefinition.executionMode` 是 `"sequential" | "parallel"`(默认顺序)。qi 照这个口径:

| 规则 | 内容 |
| --- | --- |
| 默认 | 不声明 = 顺序 —— **与以前逐字节一致**(没有隐含的行为变更) |
| 成批 | **相邻**的 parallel 工具并成一批并发跑(`asyncio.gather`) |
| 打断 | 一个顺序工具把批次**切断** —— 顺序工具永远不会与并行工具重叠(否则"我声明了顺序"就没意义) |
| 并发安全 | **由声明方负责**:写 `"parallel"` 等于说"我的实现是并发安全的"。qi 不做文件变更队列 |
| 顺序不变 | 事件与上下文顺序仍然确定:先把整批的 `tool_execution_start` 发完,再按**声明序**发 `tool_execution_end` / `tool_result` / 消息 |
| 没有 session 级默认 | pi 另有 AgentOptions 级的 `toolExecution` 默认值;qi 只认**逐工具**声明 |

### 11.0 出处与立场(原 `extensions.md` 头部与 §1)

- 参照物:pi v0.85.1 的 `docs/extensions.md` / `docs/packages.md` / `docs/usage.md`、`examples/extensions/subagent/`。
- qi 的立场:**core 只留 pi 也有的东西;qi 自己的增值功能全部拆成扩展。** 这不是折中,是 pi 已经写下的立场(pi `docs/usage.md`):
  > It intentionally does not include built-in **MCP, sub-agents**, permission popups, plan mode, to-dos, or background bash. You can build or install those workflows as extensions or packages, or use external tools such as containers and tmux.
- qi 要拆的三样(MCP / 多 agent / web)**正好落在这份清单上**。分界表原来带两列 pi 标注(core「pi 也有」/ 扩展「pi 故意不做」),手册侧已去掉。

### 11.4 事件与 API 面的对齐口径(原 `extensions.md` §3.1 / §3.2 / §3.3 / §3.5)

- **事件:pi 的 36 个事件全部已接**(手册现在只写"事件共 36 个")。
- **顺序按 pi**:`tool_execution_start`(带**未被闸门改过**的原始参数)→ `tool_call`(闸门在这里改)→ 真正执行。
- **payload 兼容层**:总线在派发前给每个 payload 补上 pi 的判别字段 `type`(= 事件名),以及两边同名同义键的驼峰别名(`tool_name` → `toolName`、`tool_call_id` → `toolCallId`、`system_prompt` → `systemPrompt`、`turn_index` → `turnIndex` …),所以照 pi 写的 handler(`event.type` / `event.toolName`)与 qi 的既有消费者同时成立。payload **结构**不同处不假造:`turn_end` qi 给 `{turn_index, text, tool_calls}`,pi 给 `{turnIndex, message, toolResults}`;`agent_end` 同理。
- **`api.*` 的参数形状**:pi 的 options 对象写法(`registerCommand(name, {handler, description, getArgumentCompletions})`、`registerFlag(name, {type, default})`、`registerTool({name, label, ..., execute})`)与 qi 的关键字写法都收;`register_tool` 也收 pi 形状的 dict;`get_all_tools()` 的元数据里同时给 pi 的 `promptGuidelines`/`sourceInfo` 键;`get_commands()` 的形状来自 pi 的 `SlashCommandInfo`;`events.on` 返回退订函数与 pi 同形;`send_message` 收 pi 形状 `{customType, content, display, details}` 且认 `followUp`/`nextTurn`;`send_user_message` 的 `trigger_turn` 缺省与 pi 同义。
- **`ctx` 成员**:pi 的 `ExtensionContext` 与 `ExtensionCommandContext` **每一个成员都有对应物**(手册侧只写"snake_case 是正式名,驼峰是别名");`ctx.mode` 的四个取值就是 pi 的 `ExtensionMode`。
- **`register_flag` 的取值通道**:pi 是直接 `--name value`;qi 只能 `--ext name=value`(E19)。
- **渲染回调**:pi 的回调直接返回组件,qi 同形(TUI 里返回 textual widget),但参数不同 —— qi 没有 pi-tui 的 `TUI` / `Theme` / `keybindings` 对象。三条规则里的第 1 条(按形参个数适配)正是为 pi 的三参写法 `(x, theme, context)` 准备的:会拿到 `(payload, ctx, None)`,不报错也不静默失效。

### 11.5 中间件链语义的来源(原 `extensions.md` §4)

「规则全部来自 pi 的实测行为」:顺序 / 快照遍历 / 链式 patch / 两种裁决形状 / 不重新校验 / 失败隔离 / fail-safe / 异常可见。其中「不重新校验」两边都成立(pi 也不重校 `tool_call` 改过的参数);「payload 是同一个 dict,原地改也对后续可见」对齐 pi 的 `event.input` 可原地改。

### 11.6 前置件里的上游对照(原 `extensions.md` §5)

- `ctx.ui` 的**两层划分与 pi 一致**;非 TUI 前端下 `custom()` 返回 `None` 与 pi 在 RPC 模式同形;组件层门控的写法来自 pi 的 `if (ctx.mode === "tui")`。
- 信任模型:pi 同样把信任决定存在用户 home 里(`trust.json`)、不在仓库里 —— qi「项目级 `defaultProjectTrust` 会被忽略」是同一个模型。
- hook 粒度:**采用 pi 的粒度,不超出**(per-prompt `before_agent_start` + 通知型 `turn_start`)。
- 依赖契约:公开 import 白名单是 pi 的 `## Available Imports` 的对应物;**禁止钉宿主版本**这条 —— pi 用 `peerDependencies` + `"*"`,Python 没有 peer,所以 qi 改成「明文规定 + 装载时报」。
- `qi install` 对齐 pi 的 `pi install <source> [-l]`;`remove`/`uninstall` 也**真卸包**(先从指定作用域删声明,无别的作用域声明时才卸),与 pi 同义。

### 11.7 决策记录里被移出手册的上游对照(原 §10 各行)

| # | 上游侧的说法(已从手册删去) |
| --- | --- |
| E1 | 命名改叫 extension 是**对齐 pi** |
| E2 | core = **pi 也有的**;MCP / 多 agent / web 全拆扩展(**pi `usage.md` 的立场**) |
| E3 | agent-as-tool 来自 **pi 的 subagent 模型** |
| E4 | hook 粒度**采用 pi 的粒度,不超出** |
| E9 | 不支持 **pi 的单文件 `*.ts` 形态** |
| E12 | pi 选子进程是因为 Node 起一次 0.04s,这个选择**不能平移到 Python**;pi 自己的 SDK 也是进程内 |
| E14 | 少一个增量就**离 pi 近一步** |
| E17 | 与 CC / pi 的**三方对照** |
| E19 | 照抄 pi 的宽容解析;pi 能这么做是因为它的 CLI 是**手写 argv 循环**(`cli/args.ts`,447 行),未知长旗标直接进 `unknownFlags` |
| E23 | MCP 这块**没有 pi 可抄**:pi 官方文档明确不内置 MCP,205 个 TS 源文件里 "MCP" 仅出现一次(注释里的 "MCP bridges") |
| E24 | 主会话只给一个 `mcp` 代理工具是 **pi 的做法**;**否掉了「纯照 pi」**(代理默认 + `directTools`) |
| E25 | **照搬 pi-mcp-adapter**;③ 照抄 pi 少一层自造协议 |
| E26 | 驼峰是 **pi 的**别名;理由是**扩展作者照 pi 写的代码应当能跑** |
| E28 | 三条一起做对应 **pi 的三处对应物**(`appendModelChange` / `exposeSessionEnvironment` / `getSessionContextSettings`);键名差异(pi 是 `modelId`)、还原前的凭证检查(**pi 对应 `hasConfiguredAuth`**) |

> 手册侧现在只留 qi 的结论;上表是删掉的上游依据 —— 复核某条决策时按这里回看。

## 12. 模型配置(原 `models.md` 的对照位)

| | pi | qi |
| --- | --- | --- |
| `models.json` 分层与格式 | 项目 > 全局,键级深合并 | 同(见 [models.md](../docs/models.md) 的「查找层级」) |
| 默认模型的归属 | `settings.json` | 同(`models.json` 里的 `defaultProvider`/`defaultModel` 完全不参与) |
| `apiKey` 值语法 | `!cmd` / `$ENV` / 转义 | 同 |
| `auth.json` 结构 | `type: api_key` 等 | 同格式,但只有 `api_key` 一种 |
| **分派模型**(`routerProvider` / `routerModel`) | pi 无此概念(qi v1 的 Dispatcher 用) | **已作废**:`auto` 分派取消,core 是单 agent。配置里这两个键仍在(老配置不报错),但没有自动分派会用到它们;`qi doctor` 仍会打印解析结果 |

## 13. TUI(原 `tui.md` 的几段对照)

- **v1 的分派行** `● → qi (router, 0.90)`:v3 不再发,只在回放旧会话时出现(见 [usage.md](../docs/usage.md))。
- **会话树差异(已落档)**:
  - `/tree` 选择器已支持 pi 的过滤键与搜索、标签;**未做**折叠/展开(`alt/ctrl+←/→`)、
    树内过滤的持久化(pi 把 `treeFilterMode` 存进 settings);
  - 标签存储不同:qi 直接写在 entry 上(`label` / `labelTimestamp` 字段),pi 另写一条
    `type=label` 的 entry;两边都是"标签属于某条 entry"的语义;
  - `/tree` 跳转时 pi 会**先问**要不要摘要被放弃的分支,qi 也一样先问(默认「不摘要」);
  - TUI 里回放会话(以及 web 的 `/messages`)只展示**当前分支**;
  - 除了 `--export` 与 TUI 回放,其它导出仍拷**整个文件**(含其它分支)。
- **路径补全差异(已落档)**:来源基本对齐(引号路径、`fd` 全树、目录优先、分隔符含
  空格/tab/`"`/`'`/`=`),但 fd 的**正则语义**是简化的(按路径段前缀连成 `a[\/]b`,pi 还叠了
  评分排序与 scoped 查询);回退扫描不分隐藏文件、不做模糊排序。隐藏文件:走 fd 时会像 pi 一样
  列出(它总带 `--hidden`);回退扫描只在 `@.` 开头时才列。pi 的候选还带来源标签 `[u]/[p]/[t]`
  (user / project / third-party)—— qi 的候选结构(`Candidate`:value/label/detail/source)与标签渲染
  已就位,但 prompt 模板与扩展命令这两个来源还没接。pi 在 bash 运行中会拒绝再跑一条;qi 允许并发。
- **消息队列差异**:pi 的 steer 是在**同一次 run 内**的下一个模型边界注入(可以影响正在进行的工作),
  qi 的 runtime 没有 mid-run 注入接口,所以 steer 实际是"下一回合"(顺序语义一致:steer 先于 follow-up)。
- **压缩显示的差异**:`branch_summary` 的树内过滤/标签渲染 pi 有,qi 只显示底色块
  (`[compaction]` / `[branch]`)。

## 14. 设置(原 `settings.md` §6 的上游描述)

pi 的 `~/.pi/agent/models-store.json` 不是用户配置,而是**远程模型目录缓存**:
被 `withRemoteCatalog()` 包装的内置 provider 会去 `GET <catalogBaseUrl>/api/models/providers/<id>`
拉模型清单,按 provider 存 `{models, checkedAt, lastModified, etag}`,用 `If-None-Match` 做 304 重验证,
4 小时节流,`PI_OFFLINE` 时跳过;启动时叠加在内置清单之上。它只对**内置 provider** 生效,
自定义 provider 永远没有条目,所以那份文件通常是 `{}` —— 这是正常状态。

**qi 不引入这个文件**:模型清单来自静态 `models.json`(加 litellm 的模型表),没有远程目录服务端,
照抄只会多一个永远为 `{}` 的文件。见 [settings.md](../docs/settings.md) §6。

## 15. 其余页面的上游短注(已从 `docs/` 全部移除)

这些是原先散在各页正文里的「对齐 pi / pi 同款 / pi 的 X 对应物」短注。手册侧现在只留 qi 自己的
说法;下面是**删掉的是什么**,按文件列出 —— 需要复核某处行为的上游出处时从这里回看。
(`cli.md` 与 `extensions.md` 的整节对照见本文件 §1 与 §11。)

| 文件 | 被移除的上游短注 |
| --- | --- |
| `cli.md` | 头部「参数尽量与 pi 保持一致」;§1/§2/§4/§5/§6 表格的「pi 对齐」整列(逐个开关的命令名/✅ 对照);各单元格里的出处注(`Print response and exit`、`pi --tools`/`-xt`/`-nbt`/`-nt`、`pi -e`、`pi --models`、`pi --api-key`、`pi -ne`/`-nc`、`pi config`、`pi /login` `/logout`、`pi auth print-*`/`check` 的 0/1/2 退出码);§3 `remove`「与 pi 同义」、`update` 默认「pi 同默认」、`--models` 拉远端目录;§4「面板/清单范围与 pi 逐字相同」、`--no-refresh` 为兼容 pi 命令面;§7 `--offline` 对齐 pi 的语义、输出约定与轮次/中断表的 pi 对照(`killTrackedDetachedChildren`、`shouldStopAfterTurn`);§8 二期「pi 能当场换渲染器」 |
| `how-qi-works.md` | 标题「8 个对齐 pi core tools」;`find` 与 `glob` 的命名分歧(hikqin/pi);`powershell`「pi 同款、参数与 UTF-8 前缀照搬 pi」;shell 解析顺序对齐 pi(`utils/shell.js`、`commandTransport`);`edit` 按 pi 精确编辑;截断策略对齐 pi 的 `output-accumulator`;会话环境变量对齐 `exposeSessionEnvironment`;bash 无过滤「与 pi 取向一致」+ pi `docs/security.md` 引文;`--tools`/`defaultTools` 对齐;`shellPath` 对齐;stdin 对齐 pi 的 `ignore`;容器化路线;文件工具路径限会话目录「与 pi 同为进程权限模型」;真实文件系统「pi 路线」;变更记录「与 pi 对齐」 |
| `settings.md` | 「文件位置与字段名对齐 pi」;全局/项目配对 `~/.pi/agent/` ↔ `<cwd>/.pi/`;数组整体替换「与 pi 一致」;`shellPath`/`skillsEnabled`/`enableSkillCommands`/`editorPaddingX`/`outputPad`/`autocompleteMaxVisible` 的 pi 默认值;`tuiMode`「qi 默认与 pi 不同(pi 默认 regular)」;第二张表的「pi 中的用途」列;`branchSummary`(pi 的 defaults to no summary);`thinkingBudgets`(pi 另有内置默认表);`defaultThinkingLevel`(`DEFAULT_THINKING_LEVEL`);`defaultTools`「pi 同义」;`retry.provider`(`getProviderRetrySettings`,pi 用毫秒);`retry.enabled`/`maxRetryDelayMs`(pi 有回合级退避重试);`extensions`「对齐 pi」 |
| `configuration.md` | 头部「对齐 pi 的 `core/system-prompt.js`」;「与 pi 的差异(语义)」小节;§3「pi 同款行为」;§4 三处(「不写 pi 那句"你可能还有别的工具"」、pi 的条件化 guidelines、措辞「与 pi 同形」);§5 三处(「对齐 pi」、「pi 会注入空块」、pi 的 `git worktree` 影子去重);§6 三处(「pi 式 XML」×2、「pi 同款」);§6.1 标题「pi 的 `Additional docs`」与「pi 把"主题 → 文件"写死在源码里」;§6.2「pi 的 text or file contents 口径」;§8 决策表(`customPrompt`、`APPEND_SYSTEM.md`、工具清单措辞、XML 形态) |
| `session-format.md` | `parentSession`「pi 同名字段」;`firstMessage` 回落;`isChanging`;`isSettingsEntry`;`getSessionContextSettings` + `restoredModel`;`hasConfiguredAuth`;`appendModelChange`(只在新会话写);`modelId` 命名差异(pi 驼峰 vs qi `model_id`);`_persist`;`SessionManager` 构造时就 `newSession()` |
| `tui.md` | §1「逐项对齐 pi」+「以 pi 的 `modes/interactive` 为基线、`theme/*.json` 逐字移植、渲染细节对照过 `user-message.js`/`assistant-message.js`/`tool-execution.js`/`footer.js`/`dynamic-border.js`」;§1.1「与 pi 同名同义,默认相反」+「pi 的默认 `regular`」行;样例里「照搬 pi 句式」;§1.3「命令选择器 = pi 的 `showSelector()`」「`pi_select_text` 的 `SelectList` 版式」「勾选标记是 pi 的 `✓`/空格」;`md*`/`syntax*` 键;已知差异「Textual 与 pi 自研渲染器的边界」「pi 能当场换渲染器」「pi 有 `fullscreenExitOutput`」;§2 命令表里的 `showModelSelector`、`getAvailableSnapshot()`、`→ ✓ 模型 [provider]` 行、「与 pi 的差异:pi 的 `/login` 主要做订阅登录(`auth.oauth`)」、「对齐 pi」;会话树「对齐 pi 的 `id`/`parentId`」+ 整列 pi + 「pi 在加载时迁移」;树键位「pi 的 `app.tree.*`」;压缩「对齐 pi 的 `core/compaction`」「pi 同款底色块」;键位「已对齐 pi;`core/keybindings.js`」+ 整列「pi 的 action」;思考「对齐 pi 的 `thinkingLevel`」「按 pi 的写法显示 `• thinking off`」「pi `DEFAULT_THINKING_LEVEL`」;「匹配 pi 语义」「`ctrl+p` 在 pi 里是切模型」;「尚未对齐」表头「pi 用途」;会话选择器「对齐 pi 的 `SessionSelectorComponent`」「pi 的三档同名同义」「版式与 pi 一致」「pi 的 `firstMessage` 回落」;补全「对齐 pi 的 autocomplete / `handleBashCommand`」「`getArgumentCompletions`」「`select-list.js` 版式」「来源标签是 pi 的口径」;输入层「对齐 pi `pi-tui/components/editor.js`」+ 整列「pi 的 action」「`undo`(pi 不用 `ctrl+z`)」「`kill-ring.js`」;§3「语义对齐 pi」+ 整列 pi + 「与 pi 一致」 |
| `providers.md` | 清单口径「与 pi 同口径(`modelRuntime.getAvailableSnapshot()`)」;`maxTokens`「pi `docs/models.md` 的语义」;`qi models` 子命令「对齐 pi」;`print-bearer-token --no-refresh`「为与 pi 的命令面一致」;`QI_AGENT_HOME`「语义同 pi 的 `PI_CODING_AGENT_DIR`」;会话环境变量「对齐 `exposeSessionEnvironment`」 |
| `themes.md` | 调色板「逐字取自 pi(`dist/modes/interactive/theme/` 的两个 JSON)」「两边的配色是同一份数据,日后可直接 diff 上游」;兜底「`dark`(与 pi 一致)」;「pi 本身不画底色」;样例里的 `$schema` 指向 pi 仓库(文件里该字段确实存的是那个 URL);「pi 的 JSON 里大小写混用」;footer 格式化「逐条对齐 pi 的 `footer.js`」 |
| `security.md` | 「pi 同样是这个模型:信任决定存在用户 home 里」;`/trust`「与 pi 同形」「连带上一层(与 pi 同)」;bash「与 pi 对齐」;bash-allowlist 记录「含与 pi 的对照」;「没有工具级审批弹窗(pi 也不内置)」 |
| `models.md` | 「格式对齐 pi」;「文件与格式对齐 pi,两级布局」;`auth.json`「与 pi 同格式」;「凭证解析顺序(对齐 pi)」;「默认模型只属于 `settings.json`(pi 语义)」;启动流程「按 pi 顺序解析密钥」 |
| `sessions.md` | `/resume`「与 pi 同一种分工(它也没有 `pi sessions`)」;懒建「对齐 pi:`newSession()` 只算路径、`flushed=false`、真写盘在 `_persist`」;恢复模型「来自 pi 的 `restoredModel` / `getSessionContextSettings`」;选择器显示第一句话「pi 的 `firstMessage` 同款」 |
| `compaction.md` | `reserveTokens` 16384「pi 默认值」;「split turn(pi 同款)」;「`_history()` 的装配规则(pi 语义)」 |
| `skills.md` | 层序说明「为了跟 pi 的发现规则对齐,再叠上 qi 自己的私有层」;`/skill:` 段尾「pi 的同一句话是 "use prompting or `/skill:name` to force it"」 |
| `quickstart.md` | `qi "分析这个仓库"` 注释「对齐 pi 的 `pi "问题"`」 |
| `packages.md` | 「`qi install` 存在(对齐 pi)」;「`remove`/`uninstall` 真卸包(与 pi 同义)」 |
| `usage.md` | `qi -r` 注释「对齐 pi」 |
| `development.md` | 「qi 没有 `test.sh`(pi 有 `./test.sh`,照那个敲会失败)」 |
