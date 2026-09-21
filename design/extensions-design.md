# 扩展机制:设计与决策记录

> **这是设计记录,不是手册。**手册在 [extensions.md](../docs/extensions.md)(§1–§8 机制 + §10 决策索引)。
> 本文放**阶段计划、未定清单、与 Claude Code 的三方对照、以及 E20→E25 的完整论证** —— 写给改动者,不进 wheel、不注入提示词。
>
> **节号沿用 `docs/extensions.md` 的编号**(§9 / §11 / §12),这样代码注释里的 `E11`/`P-E4c` 之类
> 决策编号与 §11.x 条目仍然指得回同一处;不要重编号。

## 9. 阶段与步骤

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **P-E1a/b/c 宿主骨架** ✅ | 改名对齐(plugin→extension:`qi.plugins`→`qi.extensions`、`.qi/plugins`→`.qi/extensions`、`plugin.py`→`extension.py`、`PluginApi`→`ExtensionApi`、`discover_plugins`→`discover_extensions`)+ 激活 `settings.extensions`(父目录或单个扩展目录都行)+ **信任门控**(`-a`/`-na` + `defaultProjectTrust` + headless fail-safe)+ legacy 目录迁移(连入口名一起改) | `tests/test_extensions.py` 19 项:未信任项目的 `.qi/extensions/` 不加载;用户级/项目级 settings 的分层正确;迁移后**能装载**(不只目录到位);entry point 组确为 `qi.extensions` |
| **P-E1d 事件面骨架** ✅ | 总线 `ExtensionBus`(`emit` / `emit_until` / 快照遍历 / patch 链 / 失败隔离 / fail-safe)+ `api.on()` + `ctx` 最小集(cwd/model/thinkingLevel/signal/hasUI/isProjectTrusted/notes)+ 首个真实事件 `session_start`(幂等 + `reason`) | `tests/test_extension_bus.py` 19 项:链语义逐条钉住;真实 `QiRuntime` 端到端收到 `session_start`;未信任项目的扩展**连 register 都不执行**;handler 崩了变成 `notes` 而不是崩会话 |
| **P-E2a 工具注册面** ✅ | `Tool` 搬到公开面(带 `prompt_snippet` / `prompt_guidelines` / `source_info`)+ `registerTool`(正式名)/ `add_tool`(别名)+ `register_tool` 统一盖章 + **内置工具改走 `registerTool`**(dogfood,来源记 `builtin`) | `tests/test_extensions.py` 5 项 + `test_system_prompt.py` 2 项:来源四个键逐条钉住;两条 import 路径是**同一个类**;snippet 回落规则;snippet 不进 tool schema;工具自带指南只随**实际启用**的工具出现 |
| **P-E2b 运行时工具集 + `exec`** ✅ | 动态注册(装载后、事件里都能注册且**下一轮就能调**)+ `getAllTools`(元数据 + `source_info`)/ `getActiveTools` / `setActiveTools`(覆盖优先于 agent 的 `tools`,跳角色生效)+ `exec`(不经 shell) | `tests/test_extension_tools.py` 13 项:端到端证明覆盖**穿过 runner**(下一轮 tool schema 里真的只剩 `read`);无宿主时 `getActiveTools` 降级而 `setActiveTools` **报错**;`exec` 的超时/中断都真 kill、带空格参数不被拆 |
| **P-E2c-1 输入面** ✅ | `input`(`transform` / `handled`)+ `before_agent_start`(链式 `system_prompt`)+ `bus.has()` 逐事件护栏 | `tests/test_extension_events.py` 12 项:`handled` **真的不跑 agent**(LLM 零调用);`transform` 影响模型看到的 + 落盘的;链式顺序;handler 崩了不拖垮这一轮;零扩展时行为与从前一致 |
| **P-E2c-2 轮次与工具事件** ✅ | runner 侧派发点全部接上:`agent_start`/`agent_end`、`turn_start`/`turn_end`、`context`、`tool_call`、`tool_result`(`agent_settled` 不做,见 docs/extensions.md §3.1) | `tests/test_extension_runner_events.py` 10 项:`turn` 事件的**次数与 index** 钉住;`context` 换掉的列表就是真发出去的那份;`block` = 工具**真的没执行**(带一个“去掉 gate 就真跑”的对照组);handler 崩了 fail-safe 拦住;`tool_result` 的 patch 真的改到模型看到的 |
| **P-E2d 依赖契约** ✅ | 装载时检查 pip 通道扩展的 `requires()`:把宿主写进 `dependencies` 就报告(含版本满足判定与修复动作);`packaging` 可选,拿不到就只报原始 spec 不猜 | `tests/test_extension_deps.py` 9 项:归一化(`Qi.Agent` 算、`qi-agent-extra` 不算)· 版本不满足时说清“不满足”· **报告了但仍然装载** · 一路到 `runtime.notes` 看得见 |
| **P-E3a `ctx.ui`** ✅ | `ExtensionUi`(后端可选 + 无后端时按 `default` 回答 + `notify` 落 notes)+ TUI 后端(复用 `PickerScreen` 弹选择/确认,新增 `PromptScreen` 做输入)+ `ToolContext.ui` + 修好 `clarify` 从没问过人 | `tests/test_extension_ui.py` 20 项:无头时 `confirm` 必然 False 且**闸门真的拦住**;有人点“是”就放行;扩展自己抛错/前端抛错都走 default;`clarify` 回归 |
| **P-E3b 命令与快捷键 + 旗标** ✅ | `registerCommand`(handler 收 `(args, ctx)`,重名加序号不覆盖)+ `registerShortcut`(textual 动态 `bind`)+ `getCommands` + **`registerFlag` / `getFlag`**(值走 core 静态 `--ext name=value`,未知名字退 2);TUI 侧:扩展命令**先于内置**认领(保留 `/quit` `/help` `/hotkeys`)、`/help` 列出扩展命令 | `tests/test_extension_commands.py` 13 项 + `test_extension_flags.py` 15 项:重名变 `:1`/`:2` 且一个不丢 · 没登记处就**报错** · 真 textual 下 `/cmd` 真跑到 handler · **扩展不能顶掉 `/quit`** · `--ext` 的布尔/字符串/打错三种形态 · 打错**不退 0** |
| **P-E3c-1 会话读写** ✅ | `appendEntry`(唯一写口 + 宿主盖章;**回合外报错**)+ `ctx.session_manager`(只读视图)+ `before_agent_start` 的 `message` 注入(持久:落盘 + 本轮进上下文)+ 回合内把会话绑在 runtime 上(wrapper + `finally`) | `tests/test_extension_session.py` 7 项:**跨回合读回来**(第一轮写、第二轮读入 prompt);custom entry **不进**上下文;user 先落盘再 fire hook;注入只发生一次 |
| **P-E3c-2 主动发消息** ✅ | `sendMessage` / `sendUserMessage` + 三档 `deliver_as`(`steer` 在每次 LLM 调用前排空、`follow_up` 在本该收工时排空且**不停**、`next_turn` 留到下一次输入)+ 排空与落盘**同一时刻**(不会出现“历史里有但还没送达”) | `tests/test_extension_messages.py` 7 项:三档各自的送达时机(含“第几次 LLM 调用才看得到”);排空**只送一次**;`follow_up` 真的多跑一轮;**轮次上限优先**(带 `turn_end` 持续入队的跑飞场景);非法 `deliver_as` 进 notes |
| **P-E3d-1 模型 / 思考级别 / 改名** ✅ | `setModel` / `get-setThinkingLevel` / `set_session_title` 收进 **runtime**(唯一入口)+ `model_select` / `thinking_level_select` / `session_info_changed` 三个通知型事件 + `_emit_notice`(后台任务,同步调用点也能发) | `tests/test_extension_model.py` 8 项(真 runtime):换客户端时 `thinking_level` / `retry` **真的带过去**;**两个来源都发且只发一次**(`set` / `cycle` / `user` / `auto`);空标题不发事件;标题内存+header 两处都改;`models.json` 未登记的 id 仍可用(刻意宽容) |
| **P-E3d-2 压缩事件** ✅ | `session_before_compact`(可 cancel / 可自带摘要)+ `session_compact` / `session_compact_failed`。压缩已经住在 runtime(`compact_session`),所以这是一处就能接完的 | 扩展能取消一次压缩、或用自己写的摘要代替(`tests/test_compaction_events.py` 7 条:取消、自带摘要(不调模型)、成功、失败先报再抛、坏 handler 不阻掉压缩、零扩展零开销) |
| **P-E3d-3 推迟项** | renderer 三件套 + `session_before_switch` / `session_before_fork` / `session_before_tree` | 理由见 本文 §11.10:两条都**不在三件套扩展的关键路径上**,且各自需要一次专门决策(渲染 API 形状 / 会话操作改成 runtime 拥有) |
| **P-E4a 运行单元 + 子运行** ✅ | runner 入参 `AgentUnit` → **`RunSpec = {name, prompt, tools}`**(core 里不再有任何地方能问出“这是哪个角色”)+ `spec_from_unit()` 过渡件 + `ctx.runAgent`(见 docs/extensions.md §3.2) | `tests/test_extension_run_agent.py` 6 项:子运行**不污染父会话**;提示词/工具真的隔离;`tools` 缺省**继承父**(不放大权限);**闸门在子运行里也生效**;`model` 另建客户端不碰父;空提示词报错 |
| **P-E4b 扩展间消息 + provider** ✅ | `api.events`(peer 消息:两套 API、同一总线对象)+ `registerProvider`(进内存 cfg,不写盘) | `tests/test_extension_interop.py` 9 项:两个扩展真能互相发消息;无人订阅是 no-op;坏 handler 不拖垮其他(且进 notes);async handler 排后台任务;**同名的宿主事件与 peer 消息互不干扰**(两张表);注册的 provider 立即可用、**不写盘**、覆盖同名会提示 |
| **P-E4c 移除(破坏性)** | `AgentRegistry` / `load_all_agents` / dispatcher 从 core 移出;仓库自己 4 个项目 agent 搬 `examples/`(E15);core `dependencies` 去掉 `mcp`;`spec_from_unit` 搬进 qi-agents | `qi` 裸启动**单 agent**跑通;core 不再 import `dispatcher`/`loader.load_all_agents`;`pip install qi-agent` 不再拖 `mcp` |
| **P-E5 三件套迁移** | qi-mcp → qi-agents → qi-web(按依赖从少到多);**qi-agents 落地后把仓库 agents 搬回 `.qi/agents/`(E15)**;`ctx.ui` 的 web 侧 + `add_route`/`add_static` | 三个包各自可装可卸;不装时启动提示与 `qi doctor` 正确;`qi web` 由扩展提供;`qi --agent <name>` 由 qi-agents 提供。**子命令分派的前提**:轻量发现宿主必须给齐 `commands` / `flags` / `cli_commands` 三个登记处 —— 装载器对"扩展装载失败"是**整体中断**,少给一个不是"少注册一样东西",而是**所有兄弟扩展的命令一起消失**(`qi web` 曾因此从未注册,见 docs/extensions.md §8.2) |
| **P-E6 收尾** | 参考扩展样例(至少一个 `examples/extensions/` 下的完整例子)+ 目录通道的 PEP 723 声明解析 + 冲突报告 + 文档重写(agent-config.md / [web.md](web.md) / [dispatcher.md](dispatcher.md) 归档)+ 迁移提示打磨 | 新用户按 extensions.md 能写出并装上第一个扩展;声明与实装不一致时 `qi doctor` 报得出来 |

**依赖关系**:P-E1 ✅ → **P-E2 ✅** → **P-E3 ✅**(a/b/c/d-1) → **P-E4a ✅** → **P-E4b ✅** → P-E4c → P-E5 → **P-E7 ✅**(接口面) → **P-E8 ✅**(前端消费) → P-E6。

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **P-E7 与 pi 的接口对齐** ✅ | ① 事件面补完(36/36):`message_*`/`tool_execution_*`/`before_provider_*`/`after_provider_response`/`user_bash`/`ui_prompt_*`/`resources_discover`/`session_shutdown`/`agent_settled`/`session_before_*`/`session_tree`;② `api.*` 补完(renderer 三件套 / `setSessionName` / `setLabel` / `setModel` / 思考级别 / `unregisterProvider` / `exec` 形状 / `sendMessage` 的 pi 形状与 `triggerTurn`);③ `ctx` 补完(`mode` / `abort` / `isIdle` / `hasPendingMessages` / `shutdown` / `compact` / `getContextUsage` / `getSystemPrompt*` / `waitForIdle` / 四个会话操作 / `ModelView` / `ModelRegistryView`);④ `ctx.ui` 数据层全集 + 组件层的 `setWidget`/`custom`;⑤ 命名与参数形状双收(E26) | `tests/test_extension_alignment.py` 21 项 + 全量 963 项;docs/extensions.md §3 按代码重写,并逐条记下 §3.4 的剩余差异 |
| **P-E8 前端真的消费这些接口** ✅ | ① 工具 `renderCall`/`renderResult` → `ExtensionToolBlock` 整个替换默认卡片;② `register_entry_renderer`/`register_message_renderer` → 回放里按 `custom_type` 接管;③ `register_markdown_transformer` → user/assistant 最终文本;④ `set_footer`/`set_header`/`set_editor_component`/`add_autocomplete_provider`/`on_terminal_input` 全部实现;⑤ 命令参数补全接进补全面板;⑥ `/new` `/resume` `/fork` `/tree` + 交互式选择器全部过 `session_before_*` 闸门 | `tests/test_tui_extension_render.py` 18 项 + 全量 981 项;docs/extensions.md 新增 §3.5(渲染回调契约) |

> **P-E3 剩下的两块与三件套无关**:P-E3d-2(压缩事件,**已落地**)与 P-E3d-3(renderer + 会话操作事件)。
> 三件套扩展真正依赖的扩展面已全部就位 —— 除了 `ctx.runAgent`,那是 **P-E4** 的事。
> **P-E2 整段完成**:工具面(注册 / 元数据 / 来源 / 运行时集合 / `exec`)+ 输入面(`input`、`before_agent_start`)+ 轮次与工具事件(`turn_*`、`context`、`tool_call`、`tool_result`)+ 依赖契约。
> 剩下的 P-E6 尾巴:PEP 723 目录通道声明解析、冲突报告深度、`qi doctor` 汇总。

**命名规则**(E26 —— 取代原先的“方法名照 pi 保留 camelCase”):**snake_case 是正式名,
pi 的驼峰是别名**(两者绑定同一个函数对象);**参数形状两边都收**(pi 的 options 对象与
qi 的关键字)。qi 自己的数据结构用 snake_case(`prompt_snippet` / `source_info` 的键),
**事件 payload 的键也用 snake_case**、不发 `type` 判别字段。v1 的旧名(`add_tool`)作为别名留着。

## 11. 未定清单

1. 装载顺序是否承诺稳定(现在是"确定但非 API";pi 也不承诺)
2. `ctx.ui` 的无 UI 默认值表(哪些默认拒绝、哪些默认继续)
3. 旧会话里 `agent_id` / `dispatch` entry 的**渲染归属**:core 前端容忍渲染 vs qi-agents 注册 `registerEntryRenderer`
4. `qi -e <path>` 的临时扩展是否进包依赖解析
5. **冲突报告的深度**:只报告"同一包被要求两个版本",还是做完整的解析预演(读全部 `requires()` + 已装版本 + 约束求解)
6. **litellm 的 ~6.8s import**(与本设计无关的独立性能问题):它拖累每一次 `qi -p` 的首次响应。查瘦身开关(`LITELLM_MODE=PRODUCTION` 一类)或换 provider 直连 —— 它会同时放大 docs/extensions.md §7.2 选了进程内的那个理由
7. **`agent.md` 的 `tools` 省略语义在 agent-as-tool 下有提权风险**(E17 决定先不动):父会话被 `-t` 收窄时,子 agent 按"省略 = catalog 全部"会拿到**全工具**。qi-agents 落地时二选一:① 在扩展里显式解析(用父的 active tools 作基,推荐,不动 `agent.md` 语义);② 改 `agent.md` 的省略语义为"继承父"
8. ~~`registerFlag` 怎么做~~ → **已决(E19)**,已实现:宽容长旗标 + `--ext` 两条并存。仍待定的两条尾巴:① **与 core 选项同名的扩展旗标收不到**(click 先吃掉,如 `--agent`;现在无诊断,只写在文档里);② 短旗标是否要开放给扩展(现照 pi 一律拒绝)。

9. **静态检查对 `extensions/` 下的文件解析不到(环境问题;已装 editable,等分析器重启)** ——
   表现:从测试里 `import qi_mcp…` 报 `reportMissingImports`;**包内相对导入也报**
   (`extensions/qi-mcp/qi_mcp/role.py` 的 `from .direct import …` 同样 unresolved)。

   **最后这条证据把机制说清了**:相对导入要能解析,分析器只需知道"这个文件属于 `qi_mcp` 这个包"
   —— 它连这都不知道,说明问题**不在"包装没装"**(装没装只影响绝对导入),而在**它没有把这些
   文件归入任何包上下文**。所以此前那句"这条意见('这个包没装')是对的"只对了一半:它报的
   现象像"没装",根因是"没建立包上下文"。

   已排除的(逐条实测):

   | 试过 | 结果 |
   | --- | --- |
   | 同一文件相邻两行 `import qi_agents` / `import qi_mcp` | 前者不报、后者报 → 不是按文件缓存 |
   | 造**全新名字**的同构包 `qi_probe` + 进 extraPaths | 也报 → 不是名字被缓存 |
   | site-packages 放符号链接 | 仍报 |
   | `extraPaths` / `executionEnvironments`(两种相对路径写法) | 无效 |
   | **`pip install -e extensions/qi-{agents,mcp,web}`(真正的 editable 安装)** | 结构上到位了(`_editable_impl_*.pth` + dist-info 齐全,任意目录 import 正常),**分析器仍报旧判定** |

   → 剩下的唯一解释是**分析器在会话内缓存了环境/路径**,要它重启(或清缓存)才会看到新装的包。
   仓库侧能做的都做了:**装法已在 `README` / 各扩展 README 里写明**(`pip install -e ./extensions/<名>`),
   运行期正确(`qi doctor` 的扩展一节能列出它们,722 passed)。
   **处理原则不变:不动代码、不加 `# type: ignore`** —— 下一次分析器重启后若仍报,再回来查;
   在那之前不应为一个工具链缓存改产品代码。

10. **`session_start` 谁发?(已查清,不是 bug)** —— 起疑的经过:参考扩展注册了 `session_start`
    处理器,但跑完 `runtime.stream()` 之后 `runtime.notes` 是空的。查下来:**它由前端"绑定会话"
    时派发**(`cli.py:314` 的默认 prompt 命令 —— 交互与 `-p`/`--mode json` 都走它;`tui.py:3030`
    的 TUI 绑定),而 `stream()` **不发**这个事件 —— 它的语义是"会话已绑定",不是"开始一回合"。
    所以那是**测试绕过了前端的职责**,不是产品缺陷。

    记下来是因为它会再骗人一次:扩展测试若直接调 `stream()`,任何挂在 `session_start` 上的注册
    (qi-mcp 的**直连工具**正是一个)都不会发生,而现象是"静默没有工具"。正确写法:

    ```python
    await runtime.start_session(session, reason="startup")   # 前端做的事
    async for event in runtime.stream("…", session): …
    ```

11. **`sendUserMessage` 的 `triggerTurn`(P-E7 已接一半)**:pi 在 agent 空闲时 `triggerTurn`
    会直接开一轮。qi 的做法是把“该开一轮”记成一条**请求**(`take_turn_request()`),
    由前端在命令 / 本轮处理完之后领取并跑 —— 所以语义到位了,但**不由 runtime 自己嵌套跑一轮**。
    要真做到“handler 里直接跑一轮”,需要嵌套流式(与当前回合共享会话写入),
    那就与 `ctx.runAgent`(E12)是同一套机制的另一半;现在不做。
12. **P-E3d-3 曾经推迟的两块,P-E7/P-E8 已补完**:
    - **renderer 三件套与工具渲染钩子**:**已端到端接线** —— `RendererRegistry` 登记,
      TUI 消费(`register_entry_renderer` / `register_message_renderer` 在 `_replay_branch`
      里接管该 `custom_type`,`register_markdown_transformer` 改 user/assistant 的最终文本,
      工具的 `render_call` / `render_result` 走 `ExtensionToolBlock`)。契约见 docs §3.5。
    - **`session_before_switch` / `session_before_fork` / `session_before_tree` / `session_tree`**:
      已实现。runtime 拥有四个会话操作,并对外给了一个公开闸门
      `before_session_op(event, payload)`;TUI 的命令路径(`/new` `/resume <id>` `/fork` `/tree`)
      **与交互式会话选择器**都过它。
    - **两个组件层接口的协议已定(不再悬着)**:
      * `set_editor_component`:**边界是“必须返回 `TextArea`(或子类)”**。此前担心的
        "15 处 `query_one("#editor", Editor)` 怎么办"其实很小 —— 调用点用到的只有
        `text` / `load_text` / `move_cursor` / `cursor_location` / `focus` / `styles`,
        全部是 `TextArea` 就有的;真正 `Editor` 专有的只有 `reset()` 与 `history_browsing`
        两个,给它们加了兼容回落。于是 15 处只需把 expect_type 从 `Editor` 换成 `TextArea`,
        自定义组件也只需是个 `TextArea` 子类就能做模态编辑。
      * `add_autocomplete_provider`:**形状定为
        `factory(text, cursor_offset) -> list[str | {value, label, description}]`**
        (与命令的 `getArgumentCompletions` 同一套元素形状),并**返回退订函数**
        (pi 返回 void)。pi 那个形状(包住一个 pi-tui 的 `AutocompleteProvider`)不能镜像 ——
        它暴露的是 pi-tui 的补全内部结构。
    - **実装里踩到的两个 Textual 陷阱**(写下来免得下次再踩):`remove()` 的生效是延迟的
      (挂同名 id 会撞 `DuplicateIds`),而 `call_after_refresh` **不够** —— 它等的是下一帧,
      摘除消息可能还在队列后面;正解是 `await widget.remove()`(可 await 形式)。
      另外换组件会在途的 `TextArea.Changed` 打成“`#editor` 不在”的状态,消息处理器要宽一点
      (`on_text_area_changed` 与 `_completion_candidates`)。

## 12. agent 配置对齐(Claude Code / pi / qi 三方对照)

> 结论先行:**qi 不照抄任何一家的字段集**(决策 E17「先不对齐」)。本节是**为什么**,以及将来做 `qi agents import claude:` adapter([agent-config-design.md §10](agent-config-design.md))时哪些字段可映射、哪些必须降级。

### 12.1 三方对照

| CC 字段 | pi subagent | qi `agent.md` | 差异 |
| --- | --- | --- | --- |
| `name` ✅必填 | ✅ | ✅ 必填 | CC **文件名不必一致**;qi **必须等于目录名** |
| `description` ✅必填 | ✅ | ✅ 必填 | 三家都是路由信号 —— 最对齐的一格 |
| `tools` | ✅ | ✅ 三态 | CC 省略 = 继承子 agent 可用全部;pi 省略 = **继承父**;qi 省略/`*` = **catalog 全部**(见 本文 §11.7) |
| `disallowedTools` | ❌ | `disallowed_tools` | CC 先应用 denylist 再把 tools 在剩余池里解析,两边都列则移除;支持 `mcp__server__*` 模式 |
| `model` | ✅ | ❌ | qi 把模型放在 `models.json`(v3 保持,E17) |
| `skills` | ❌ | 私有 `skills/` 目录 | CC = 按名**预加载全文**;qi = **渐进披露**(只进 description)且**无共享技能库** —— 机制不同 |
| `mcpServers` | ❌ | `mcp_servers` | 语义**接近**(这个 agent 能用哪些 server);CC 可内联定义,qi 只按名绑定声明表(默认无、显式声明) |
| `hooks` | ❌ | ❌ | CC 有 per-agent 生命周期 hook;qi v3 的扩展事件是**全局**的 |
| `permissionMode` · `maxTurns` · `effort` · `memory` · `background` · `isolation` · `omitClaudeMd` · `experimental` | ❌ | ❌ | CC 的执行策略字段 —— qi **一个都不抄**,理由见 12.2 |
| `color` | ❌ | `display_name` | 都是表现层(一个是颜色,一个是显示名) |
| `initialPrompt` | ❌ | `opening.message` | CC = 自动提交的首个 **user** 轮;qi = 助手侧**开场白**(进历史) |
| —— | ❌ | `keywords` | qi 独有(Dispatcher L1 规则层;v3 后暂不消费,保留不废弃 —— E17) |
| —— | ❌ | `include` | qi 独有:`assets/` 下 md 按序拼进 system prompt |
| —— | ❌ | `opening.suggestions` | qi 独有:UI 快捷提问,不进历史 |
| —— | ❌ | 私有 `mcp.json` / `data_sources.json` / `assets/` | qi 独有:**目录级自包含**(见 docs/extensions.md §7.4 的 scope 接口) |

**两家的重心不同**:CC 的可选字段大半是**执行策略**(能碰什么、跑多久、花多少、在哪隔离);qi 的大半是**内容与打包**(角色说明 + 私有资源)。

**结构性差异**:

| | Claude Code | pi subagent | qi |
| --- | --- | --- | --- |
| 形态 | 单文件 `.md` | 单文件 `.md` | **目录** `agents/<name>/` |
| 来源层 | 5 层(managed > `--agents` CLI > 项目 > 用户 > 插件),**递归扫描** | 2 层(用户 > 项目),`agentScope` 可限 | 3 处(builtin `general` → 用户 → 项目),不递归 |
| 嵌套 | **允许**,默认 3 层(`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`) | 环境变量防递归(第三方有 `maxSubagentDepth`) | 单层(本文 §11 未定 B4) |

### 12.2 为什么 qi 不抄 CC 的执行策略字段

| CC 字段 | 与 qi 哪条既定决策冲突 |
| --- | --- |
| `permissionMode` | A6:bash **无命令级过滤**是故意的(限制靠 `tools` 收窄或容器/VM) |
| `maxTurns` | A12:轮次是**谓词** `stop_after`,且 **qi 自己不传**(同 pi 定义了却不实现) |
| `effort` | 思考级别是**全局** `thinkingLevel`(settings.json),不是 per-agent |
| `memory` / `background` / `isolation` / `omitClaudeMd` | qi 没有这些机制;真要做得先有对应能力,不是加个字段 |

抄进来就是两套政策并存 —— 和 `auto` / `-t` 那次一样,**半对齐比不对齐更容易踩空**。

**per-agent 模型若确实需要**:走**工具参数**(如 `subagent(model="haiku")`)而不是 `agent.md` 字段。这也是 pi 生态的做法 —— 第三方 pi-subagents 把 `session: "fork"` / `async` / `depth` 全放工具参数与自己的 config,不往 agent `.md` 加字段。

### 12.3 将来做导入 adapter 时的降级表

| 处理 | 字段 |
| --- | --- |
| **可直接映射** | `name` · `description` · `disallowedTools` → `disallowed_tools` · `mcpServers` → `mcp_servers` · `color` → `display_name` |
| **需转换** | `model`(qi 无此字段 → 提示丢弃或转成工具参数)· `tools`(CC 是 `Read/Grep/Glob/Bash` 大写名,qi 是小写 `read/grep/find/bash` → 需名字映射表;且两边工具集不重合:CC 有 `WebFetch/WebSearch/Task`,qi 有 `ls/find/powershell`) |
| **需实例化(不能拷字段)** | `skills`:CC 的"预加载全局技能全文" ≠ qi 的"私有渐进披露" → 要变成技能目录 |
| **导入时丢弃并提示** | `permissionMode` · `maxTurns` · `effort` · `memory` · `background` · `isolation` · `hooks` · `omitClaudeMd` · `experimental` |
| **反向导出会丢** | `keywords` · `include` · `opening.suggestions` |

导入 adapter 的价值因此**不是格式转换,而是一份诚实的降级报告**:告诉用户哪些能力在 qi 里没有对应物。

## 13. 角色自带 MCP:能力的归属(E20 → E25 的完整论证)

docs/extensions.md §7.4 定的是形状,P-E5 ② 落到具体接口。**qi-agents 与 qi-mcp 互不 import** —— 两边都只依赖 core 的
一个小契约:

```python
# qi-mcp:声明“我提供 mcp_servers 这个种类” + 给一个解析器
api.provides_config("mcp_servers")                 # 已有(P-E1)
api.registerResolver("mcp_servers", resolve)       # 新增:resolve(scope) -> list[Tool]

# qi-agents:问宿主“谁能提供”,自己不知道 MCP 存在
tools = await api.resolveTools("mcp_servers", scope=role_dir)   # 新增;没人提供 → [],角色照跑
```

为什么不用直接依赖(qi-agents 的 `dependencies` 里写 `qi-mcp`):

| | 直接 import 兄弟扩展 | 能力交接 |
| --- | --- | --- |
| 只装 qi-agents | **装不上**:被拖上 MCP 栈与它的传输层依赖,或直接 ImportError | 角色照跑,只是没有 MCP 工具(优雅降级) |
| 想换 MCP 实现 | 换不掉 | 换个提供者即可(扩展系统的本意) |
| qi-web 的位置 | 还得替这两者做一层中介 | 什么都不用做 |

**代价**:core 要新增两个 API(`registerResolver` / `resolveTools`)—— `provides_config` 此前
只有“声明”那一半,这次补齐“消费”那一半。这是 P-E5 ② 的第一件事,先于写客户端。

**传输范围(E21)**:stdio + Streamable HTTP。旧 SSE 不做 —— 已过时,为它多背一块代码不值。

**工具命名(E22)**:`mcp__<server>__<tool>`(Claude Code 约定)。

- 双下划线几乎不可能与内置工具或第三方扩展撞名;
- 一个前缀就能 grep / 分节 / 过滤;
- **因此角色的 `tools:` 白名单支持通配**:`tools: read, grep, mcp__github__*` —— 这也是唯一让
  “角色只拿到某个 server 的工具”写得出来的办法(逐条列工具名会在 server 升级后失效)。

**作用域三层**(沿用 v1,勿改):全局 `~/.qi/agent/mcp.json` → 项目 `<git 根>/.qi/mcp.json`
(同名覆盖全局)→ **agent 私有** `<agent 目录>/mcp.json`(自动绑定,只本角色可见)。

**边界澄清(易混,写下来免得绕回去)** —— 分工不是“谁读那个文件”,而是**“谁知道 `mcp.json`
这个名字”**:

| | qi-agents | qi-mcp |
| --- | --- | --- |
| **知道** | 角色目录怎么找(`.qi/agents/<名>/`)、作用域是什么、项目信任状态 | `mcp.json` 的格式、三层布局与优先级、怎么起 server 与注册工具 |
| **不知道** | `mcp.json` 存在 —— 它一行都不碰 | 角色目录怎么找 —— **它从不去遍历 `.qi/agents/`** |

交接值是**一个作用域对象**(qi-agents 定义,消费方鸭子类型读它,不必 import):

```python
Scope{name: str, dir: Path, trust: bool}      # qi-agents 造
# qi-mcp 只做一件“层内”的事:
declared = read_json(scope.dir / "mcp.json")   # 它自己的文件名,不是 qi-agents 要关心的
```

**反过来的做法更差**:若让 qi-agents 读 `<agent 目录>/mcp.json` 再把内容交给 qi-mcp,
qi-agents 就必须知道文件名、格式、以及“agent 私有”这一层的存在 —— 把 MCP 知识**漏回**了
上游。同理,全局/项目两层**与角色无关**(不是 agent 层),qi-mcp 自己处理,不需要作用域。

**谁把作用域递给 qi-mcp?—— 它自己不去找。** 调用链只有一步:

```text
qi-agents(唯一知道角色目录的人)
  └ api.resolveTools("mcp_servers", scope=Scope{name, dir, trust})
      └ core CapabilityRegistry.resolve_tools 把这个 scope **原样**转给每个 resolver
          └ qi-mcp 的 resolver(scope=…) 读 scope.dir/"mcp.json"
```

- `scope=None` 的含义就是“**没有 agent 层**”(主会话 / 非角色运行)→ qi-mcp 只管全局+项目两层;
- 所以 qi-mcp **没有“agents 目录在哪”这个配置项** —— 它连这个名字都不知道,也永远不会去搜;
- 可测性(这是交接的现成收益):qi-mcp 用一个**假 scope**(任意临时目录 + 一份 mcp.json)就能
  完整测;qi-agents 在没装 qi-mcp 时 `resolveTools` 得 `[]` 也能照跑。

**没有 pi 先例可抄(E23)**:pi **明确不内置 MCP** —— 官方文档 `docs/usage.md`:
“It intentionally does not include built-in MCP, sub-agents, permission popups, plan mode,
 to-dos, or background bash. You can build or install those workflows as extensions or
 packages.”。205 个 TS 源文件里 “MCP” 只出现一次,还是注释里那句 “MCP bridges”。
所以这一块**不能拿“对齐 pi”当依据**:上面这套 scope 交接是 qi 自己的决定,理由只有一条 ——
让两个扩展互不知道对方的存在。

**补正(E23 的半个更正)**:生态里**确实有** MCP 扩展可对照 —— `pi-mcp-adapter`(pi 的 MCP 扩展,
非核心;pi 核心不含 MCP 这点不变)。它的做法:

- **配置层是固定文件清单**(后到者胜):`~/.config/mcp/mcp.json` → `~/.agents/mcp.json` →
  `~/.agents/mcp/mcp.json` → `<Pi agent dir>/mcp.json` → `.mcp.json` → `.pi/mcp.json`,
  外加三种**包来源**(Agent Plugins 目录 / Claude 插件目录 / Pi 包清单 `pi.mcp`,后者按
  `<包名>__<server>` 加前缀);
- **没有 per-agent / per-subagent 层** —— pi 核心没有子 agent,它的 MCP 配置是宿主/会话级的,
  所以根本不存在“角色目录”这件事;
- 别的扩展要给它服务器,走**共享事件总线按值推**:
  `pi.events.emit("pi-mcp-adapter:runtime-register:v1", {version:1, name, definition:{url}})`,
  适配器**同步**写回 `request.result`;会话级、永不落盘、重名 fail closed、只走 proxy 工具。
  **注意它是按值传 definition,不是“给你一个目录”** —— 适配器永远不会去走对方的目录。

**对 qi 的意义**:② 的形状(pull + scope)与 pi 的(push + by-value)不同,但**边界完全一致**
(pi 的适配器同样不碰对方目录)。qi 选 pull 多一条 pi 做不到的理由:角色的 MCP 工具只能进
**那一次子运行**的工具清单(`RunSpec.tools`),不能污染主会话(runtime-register 是会话级的)。

**为什么这个形状自洽**(而不是 E20 当年推的 scope 交接):

1. **agent 目录是 qi-agents 的“自包含包”** —— docs/extensions.md §7.4 的差异点就是 `agent.md` + `skills/` +
   `mcp.json` + `data_sources.json` 可整体 import/export。**谁拥有包格式,谁就知道包里有什么成员**;
   qi-agents 认识 `mcp.json` 不叫泄漏,叫包主人点自己的东西。我先前那条“谁知道文件名”的分界
   在这里**用错了层级** —— 它适用于两个互不相关的扩展,不适用于“包的主人”。
2. **E24 的顾虑在新形状下前提消失**:角色的 MCP 面 = **该角色按需注册的 server 集合** ∩
   (其 `tools:` 里有没有 `mcp`)。角色没有 `mcp.json` → 没有 server,代理无物可调;白名单再不写
   `mcp` → 彻底没有路径。**顾虑的前提没了,不是被绕过。**
3. **pi 的先例直接可抄**:照写比自己发明 `Scope` 交接少一层概念,也少一处双方要共同维护的协议。
