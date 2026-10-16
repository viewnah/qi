# qi-lab 的多智能体过程展示(web 端委派可视化)

> 状态:**一期、二期已实现(2026-10-10,含子智能体区分修版);三期未实现**。动手前先读 §6 红线与 §7 验收。
> 一期的落地记录与两处与原计划的偏差写在 §5.1 末尾的「落地」小节;
>
> 相关文档:[web.md](web.md)(AG-UI 事件 / `details["ui"]` 词汇表,§16 是本文的落点)、
> [development.md](development.md)(测试布局)、`extensions/qi-lab/docs/api/ag-ui.md`(事件表)、
> `extensions/qi-agents` 的 `render.py` / `subagent.py`(TUI 侧的既有答案)。
>
> 本文回答四个问题:**① 现在 web 上到底显示了什么 ② TUI 的设计思路能抄什么 ③ AG-UI 协议里
> 有没有对应事件 ④ 别的 agent 框架怎么展示多智能体执行过程**,然后给分期方案。

---

## 0. 结论

**协议不缺,是 qi 没发、前端没画。**

AG-UI 在 qi 钉的那个版本(`@ag-ui/encoder@0.0.59`,`qi_lab/app.py:679`)的事件集里
**就已经有** `ACTIVITY_SNAPSHOT` / `ACTIVITY_DELTA` 和整套 `SUBAGENT_STARTED / FINISHED / ERROR`;
qi 的 `ui/src/api/types.ts:508-521` 收了 `ACTIVITY_*`(注释写着"结构化活动通道,todo / plan 走这里"),
但 **`qi_lab/agui.py` 从不发**,`ui/src/state/turn.ts:606-613` 是**显式忽略分支**(注释:"留着分支,
这样将来后端一开始发,编译器不会因为'没处理'而报错")—— 分支留好了,没人接。

web 端现在的表现:**派单后长时间静默 → 结束甩一堵文本墙**。根因是子运行的事件只喂给了
TUI 的平铺卡和 `runtime.notes`,**从来没进过 AG-UI 流**。

---

## 1. 现状:一次委派在 web 上的逐帧(证据)

| 阶段 | web 上看到什么 | 出处 |
| --- | --- | --- |
| 派单 | 一行脉动点 `· subagent agent=… task=…`(args 单行,`text-overflow: ellipsis`) | `qi_lab/agui.py:289-303`(`TOOL_CALL_START/ARGS`);`ui/src/theme/app.css:1329` |
| **子 agent 跑(可能几分钟)** | **什么都不出** | 子事件只进 `subagent._on_event`(`subagent.py:431`)→ TUI 卡 / 时间线;web 无 frontend → `notify` 落 `runtime.notes`(`extensions.py:816`,`_frontend is None` 分支);`qi_lab/` 下 grep `frontend` **0 命中** |
| 结束 | `TOOL_CALL_RESULT.content` = 横幅分段长文(`<pre>` 墙)+ `metadata["qi.tool"].details.ui` 扁平列表 | `subagent.py:_render`(:551)/`_ui_items`(:589)/`details`(:698);`qi_lab/agui.py:306-318` |
| 展开 | `✓/▸/○` 行 + label;**深度缩进被 CSS 吃掉** | `ui/src/components/Rows.tsx:59-80`;`app.css:3008`(`.ui-list__label` 无 `white-space: pre`,而 `_ui_items` 把深度编码成 label 的前导空格) |
| 刷新 | `qi.history` 能重建 details;**直播部分全丢** | `turn.ts:555-578`(注释:"插件唯一的 UI 下行通道") |

### 1.1 顺手查出的两个真缺陷(一期要一起修)

1. **失败被画成“未开始”**(**已修,2026-10-10**):`_ui_items` 发 `state:"error"`,
   词汇表 v1 只有 `done | active | pending`(`design/web.md` §16.2,行 644)→
   `Rows.tsx` 的三分支把 `error` 落进 else,画成 `○`、不加删除线;错误文本只在
   `note` 里当一行小字。修法:词汇表 v1.1 加 `error` 词 + `✗` 形符(降级规则见 §5.1)。
2. **结构上不可能有“运行中”**(**二期已解,2026-10-10**):`details` 只在 `run_subagent` 返回前
   构造一次(`subagent.py:698`),所以 `active` 永远不会出现 —— 这不是 bug,
   是数据模型缺一层 → 二期用 `ACTIVITY_SNAPSHOT` 把“运行中”那层补上(见 §5.2 落地)。

---

## 2. TUI 的设计资产:要抄的是这六条

出处 `extensions/qi-agents/qi_agents/render.py` 与 `subagent.py`。

1. **不画树地图**(`subagent.py:551` 的 `_render` docstring)。派单行 `· subagent …` + 横幅里的
   深度已经把结构说清了,再画一棵树是重复的第二份信息。
2. **一个 agent 一张卡,全树平铺**:`render_call`(:438)建平铺容器 `_RunStack`(:349)入
   `PENDING_CARDS`(:50)队列,每个子运行认领一张(`add_agent`,:368),嵌套子层追加到同一架子。
   卡标题 = `▸ 名字 ← 由谁指派 · 深度N ✓/✗` —— 归属、层级、状态一行讲完。
3. **每张卡三区**:活动 `· tool args`(`add_activity`,:327)→ 思考 `✎ 第N轮`(`set_stream`,:320)
   → 输出。三者**物理相邻**,一个 agent 干了什么 / 想了什么 / 说了什么一眼可见。
4. **正文固定高内滚 + 自动跟底**(`scroll_end`),新卡挂载 `scroll_visible`;用户手动上滚即停。
5. **完成即定格**(`set_done`,:333):标题转 ✓/✗、正文收起为摘要,点击展开全文;
   `ctrl+o` 是全局开合(`set_output`,:396),**不覆盖**平铺内容。
6. **文本是唯一事实源**:`banner()`(:61)/ `BANNER_RE`(:58)定的 `── 名字 (深度N, 成功/失败) ──`
   被 `result_tree`(:144)解析回卡片 —— 拷贝 / 导出 / 纯文本渲染都不丢归属。
   **这条直接给了 web 一条零后端改动的路**(§5.1 第 3 点)。

配套常数:`BODY_LINES = 30`(:48)—— 展开态每段最多画行数,§5.2 的截断策略直接借它。

---

## 3. AG-UI 有没有对应事件:有,而且两层(已核对,非转述)

核对方法见附录 B:直接拉官方包的 `.d.ts`(`@ag-ui/core@0.0.59` 与 `@ag-ui/core@1.0.2`)。

### 3.1 qi 钉的 0.0.59 事件集里就已经有多智能体词汇

`@ag-ui/core@0.0.59` 的 `EventType`(**qi 的 `app.py:679` 引的就是这个版本的编码器**):

```
… STATE_SNAPSHOT, STATE_DELTA, MESSAGES_SNAPSHOT,
   ACTIVITY_SNAPSHOT, ACTIVITY_DELTA, RAW, CUSTOM,
   RUN_STARTED, RUN_FINISHED, RUN_ERROR, STEP_STARTED, STEP_FINISHED,
   REASONING_*(THINKING_* 标 deprecated),
   SUBAGENT_STARTED, SUBAGENT_FINISHED, SUBAGENT_ERROR
```

`SubagentStartedEvent`(0.0.59 的 zod schema 原文字段):

| 字段 | 语义 | 与 qi 的形状对应 |
| --- | --- | --- |
| `subagentRunId`(必填) | **每次调用一个**,不复用 | qi 侧需要自己生成(角色名 + 序号,`subagent._unique_name`) |
| `name`(必填) | 可复用的名字 | qi 的角色名(`role.name`) |
| `description` | 给人看的说明 | 角色的 `description` |
| `parentSubagentRunId` | 嵌套委派链 | qi 的 depth 链 |
| **`parentToolCallId`** | 协议注释原文:为"agent 以工具形式暴露给模型"的模式准备 —— **正是 qi 的 `subagent` 工具** | 一次 `subagent` 调用的 `tool_call_id` |
| `parentMessageId` | 持有该工具调用的消息 | 父轮的 messageId |

`ACTIVITY_SNAPSHOT`: `{type, messageId, activityType: string(开放), content: Record<string, any>,
subagentRunId?, replace?: true(默认), metadata?}` —— `activityType` 取值归生产者,
`replace:false` 明确"不要覆盖",增量改用 `ACTIVITY_DELTA`。
**这就是"子 agent 当前进度快照"的天然载体,而且 0.0.59 就有。**

`SUBAGENT_FINISHED` 带 `result` + `outcome: success|suspended`;`SUBAGENT_ERROR` 带
`message`/`code`,协议注释明说"**run 可以继续,父 agent 可以处理失败,所以这不是 `RUN_ERROR`**"
—— 与 qi 的"子运行失败 → 结果里如实说、继续跑"完全同构。

### 3.2 1.0 只是改名与补充,没有新概念

`@ag-ui/core@1.0.2`(`PROTOCOL_VERSION = "1.0"`):事件族相同;`SubAgentInfo` → `SubagentInfo`;
`ActivityMessage{role:"activity"}` 进了 `Role` 枚举;`MultiAgentCapabilities{supported, delegation,
handoffs, subagents:[{name, description}]}` 用于能力协商(与 qi 的 `/api/meta` capabilities 是两层,
不冲突)。

**结论:不存在"协议不支持"的障碍。缺的是 ① 后端不发 ② 前端不画 ③ `details.ui` 词汇表没有
"运行中 / 失败 / 层级"这几个词。**

### 3.3 样式:AG-UI 不管,样式归宿主

AG-UI 是事件协议,**不带任何样式**。qi 的答案是 `details["ui"]` 词汇表
(`design/web.md` §16.2 行 644)—— "插件输出数据,宿主实现渲染";两条硬规则
(不认识的 `type` 退回 JSON 不丢弃;有 `details` 没 `ui` → 折叠显示 JSON)保证降级路径永远存在。
`ui/src/components/Rows.tsx:49-145` 的 `UiNodeView` / `Details` 就是它的实现。

---

## 4. 别的 agent 框架怎么展示(按模式归类)

> 这一节是**模式归纳**,不是逐产品截图核对;标 ⚠️ 的条目只到"公开文档/常规做法"的把握。

| 模式 | 谁在用 | 核心做法 | 对 qi 的启示 |
| --- | --- | --- | --- |
| **A. Trace / Span 树** | LangSmith、Langfuse、Arize Phoenix、OpenAI Agents SDK 的 tracing、CrewAI(接 Langfuse)⚠️ | 每个子 agent 是嵌套 span;左侧树 + 右侧时间轴 / 甘特;点 span 看输入输出与耗时 | 强在**回放与排障**,弱在**直播**(要等 run 结束才有树)。qi 没有 trace 后端,照搬太重 |
| **B. 会话流里嵌一个可展开块** | Claude Code(Task 子代理)⚠️、pi(子 agent 是独立 run,从 fleet / transcript 侧看) | 主时间线只留一行,点开进嵌套转录;结构靠缩进而不是另开一屏 | **最贴 qi**:qi 的工具卡本来就是"一行 + 点开展开",把子 agent 塞进这张卡即可,不新增 UI 范式 |
| **C. 图 / 泳道 / 多列聊天** | LangGraph(图上节点高亮当前走到哪)⚠️、AutoGen Studio(多 actor 聊天视图)、CrewAI(逐 task 推进) | 关系是**一等公民**,运行状态涂在图上 | 表达力最强,但要维护图数据;与 qi"不画树地图"的既有判断相悖 |
| **D. 计划 / 步骤侧栏** | Manus、Devin 这类产品 ⚠️ | 任务清单常驻侧栏,步骤 ✓/▸/○ | 适合线性计划;qi 的委派是树 + 并行,只适合当**摘要层** |

qi 目前 **A/B/C/D 一类都没有**:直播期空白 + 回放期文本墙。
按 qi 自己的哲学(`design/web.md` §16.3"扩展点放数据层")与 TUI 的既有资产,
落点是 **B(主形态)+ D(摘要层)**:主时间线不动,委派长在那张 `subagent` 工具卡里。

---

## 5. 方案:三期(从"零改动"到"真直播")

### 5.1 一期 —— 把"事后可读"做对(不动协议、不动 `src/`、不动 TUI)

**目标**:点开那张 `subagent` 工具卡,看到 TUI 同款的平铺卡,而不是文本墙。

1. **结构进 `details.ui`**(主路):`_ui_items` 升级,每个 agent 一个 `list` 项,带**显式**
   `depth` / `task` / `role`,活动 / 思考 / 输出作为子节点。词汇表已有的"只约定已知键、
   不禁止附加键"让它成为**纯加法**(§16.2)。
2. **`state` 扩词**:加 `error`(现在发的 `error` 根本不在词汇表里 → §1.1 缺陷 1)。
   取值扩展的代价:老宿主把 `error` 当 `pending`。兜底:同时给 `note`,不认 `error` 的宿主
   至少读到错误文本 —— 这与"不认识的 type 退回 JSON"是同一条降级哲学。
3. **前端按 `depth` 缩进**:补 `padding-left` / `white-space`(显式字段与 label 前导空格任一生效)。
   状态形符沿用 `✓/▸/○/✗` 且**不单靠颜色**(`.ui-list__mark` 已是这个写法)。
4. **result 文本解析成三区**(兜底路,零后端改动):前端用 `BANNER_RE` 把横幅切段,
   每段显示活动 / 思考 / 输出,失败段标红。**两条路的关系**:结构走 `details.ui`(单份结构源),
   解析只在 `details` 被 `MAX_TOOL_DETAILS_CHARS` 截断或缺 `ui` 时兜底 —— 避免契约两处实现。
5. **文档与测试**:`design/web.md` §16.2 补词汇表 v1.1 草案(`depth`、`error`);
   新测试按仓库惯例做变异验证。

**落地(2026-10-10)**—— 实现顺序与原计划有**两处偏差**,都是往小改的方向:

| 计划 | 实际 | 为什么 |
| --- | --- | --- |
| 改 `qi-agents/_ui_items`(加 `task`/`role`、子节点三区) | **qi-agents 一行没改** | `depth` / `error` / `note` / “正文不进 details” 本来就有,缺的只是**渲染端**;
子节点三区被判定为重复存储(撞 `MAX_TOOL_DETAILS_CHARS`) |
| 结构进 `details.ui`,正文靠文本解析兜底 | **结构进 `details`,正文走横幅解析(主路)** | 同一份内容不存两遍:结构小、正文大(50k 截断),各归各 |

落地清单:

- `ui/src/state/segments.ts`(新):`parseBanners` 按 `render.py` 同源正则切段(卡头名字 /
  深度 / 成败、预序栈补 `← 归属`、三区分类),`itemDepth` 缩进回退链;
  **解析不出 → `null` → 退回原文 `<pre>`**。
- `ui/src/api/types.ts`:`UiNode` 的 list 项加 `state: "error"` 与 `depth`。
- `ui/src/components/Rows.tsx`:`UiNodeView` 按 `depth` 缩进 + `✗` 形符;`Tool` 展开态
  先 `Details`(结构)、再 `Segments`(切好的卡)、最后原始输出收进 `<details>` 折叠。
- `ui/src/theme/app.css`:`.segs/.seg*/.seg-raw` 一组,`.ui-list__*` 补 error 态;
  色值全部走 `var(--dsw-*)`(`check:design` 过)。
- 测试:`ui/src/state/segments.test.ts` **15 例**(fixture 是 `subagent._render()` 的
  **真实输出**,不是手写样本 —— 改了 `banner()` 没同步解析器就红);变异验证 4 处
  (状态词校验 / 归属链 / 思考续行 / `depth` 回退)全部红、还原全绿。
- 文档:`design/web.md` §16.2 补 v1.1 小节。

验证:`tsc --noEmit` 0、`vitest` 142/142、`check:design` ✓、`npm run build` 已重出
`qi_lab/static`。**Python 侧零改动 → 无需跑 pytest 变异**(仍跑了一遍全量,见 §7)。

改动面:`qi-agents/subagent.py:_ui_items` + `qi-lab/ui`(+ 若要同步:`qi-web/ui` 是复制件)。
工作量:**半天~一天**。

### 5.2 二期 —— 过程直播(关键是"通道选型")

**目标**:派单那一刻起,卡里就有 `▸ 角色 ← 由谁指派 · 深度N`,活动行 `· tool args`、
思考行 `✎ …` 实时滚动、自动跟底;结束后定格收起。

#### 通道三选一

| | 做法 | 评价 |
| --- | --- | --- |
| **A(推荐)** | **复用 `ctx.on_update`**(`tools/__init__.py:55` → `runner.py:519-537` 的 `tool_execution_update` 总线事件,payload 自由) | 已存在、同步可调、自带 `tool_call_id`;`subagent._on_event` 已握着全部子事件(开始 / 思考 delta / 工具 / 结束),只要多推一次,并**在 payload 里自己带归因** |
| B | `ExtensionUi` 上加 `ui.activity(...)` 数据方法 | 语义最干净(数据层扩展点),但要动 `src/qi_agent/extensions.py` + TUI 实现 + qi-agents 三处 |
| C | 宿主直接订阅共享总线,靠 `ctx.agent_name` 认人 | **不可行**:`runner._on_update` 发事件用的是 `self.extension_ctx(...)` = **runtime 的 ctx(父的)**(`runner.py:533`),payload 里没有 agent 字段 → 认不出是谁在跑;要认人就得改 `src` |

#### 宿主侧(qi-lab,不改 `src`)

- `runtime.bus.on("tool_execution_update", handler)`(`extensions.py:1619`,公开可用)。
  **注意总线只有 `on` 没有 `off`**(只有 `remove_message`,`:1630`)→ 必须**进程内注册一次
  + 按"当前活跃会话"过滤**(会话级串行保证同一时刻只有一个 run),**不能每轮注册**(handler 会堆)。
- handler 把帧塞进 per-run 队列;`app.py` 的 `ag_ui` 处理器里
  `async for ev in runtime.stream(text, session)` 循环排空 → 翻译成 AG-UI 帧:

```jsonc
{ "type": "ACTIVITY_SNAPSHOT", "messageId": "<subagentRunId>",
  "activityType": "qi.subagent", "subagentRunId": "scout#1", "replace": true,
  "content": { "phase": "act", "name": "scout", "parent": "coordinator", "depth": 2,
               "task": "…", "line": "read_file path=…", "toolCallId": "call_x" } }
```

- `phase`:`start | think | act | done | error`,与 TUI 卡的
  `add_agent / set_stream / add_activity / set_done` 一一对应。
- 收尾可再发 `SUBAGENT_STARTED / FINISHED / ERROR` 当生命周期锚点(§3.1 说明了它们
  本来就在 0.0.59 的事件集里,严格客户端也认)。
- **`ACTIVITY_*` 是"补上早就声明过的通道",不是发明新事件** —— `types.ts:508` 的注释与
  `turn.ts:610` 的忽略分支都是这次要兑现的欠账。

#### 回放红线(必须做)

`runtime.run_agent` 的子运行**不落盘**(`runtime.py:576` 的 docstring:"子运行不碰会话 —— 不落盘"),
`app.py:732` 的注释也把"直播与回放不一致"当作要避免的缺陷。所以:

> **结束时同一份内容必须收敛进 `details.ui`**(复用一期词汇表)→ `qi.history` 回放时画出同一批卡。

截断策略借 TUI 的 `BODY_LINES = 30`:每 agent 保留最后 N 行活动、思考逐轮截断,
总预算受 `MAX_TOOL_DETAILS_CHARS` 约束(超限会换成 `{"_truncated": true}`,前端只剩一句"太大了")。

#### 前端

- `turn.ts` 把 `ACTIVITY_SNAPSHOT / ACTIVITY_DELTA` 从"显式忽略"(`:606-613`)改成处理,
  按 `messageId`(`subagentRunId`)归到当前 `subagent` 工具卡下。
- running 态卡体 = 平铺架子(卡头 + 活动行 + 思考行),**自动跟底,用户上滚即停**;
  结束收起为摘要行(✓/✗),点开展开三区;全局一个开合键(对应 TUI 的 `ctrl+o`)。
- 层级用 `depth` 缩进,**不画树地图**。
- `/api/meta` capabilities 加 `activity`:前端有则画实时,无则退回一期。

#### 落地(2026-10-10)

| 方案里的设想 | 实际落地 | 为什么改 |
| --- | --- | --- |
| handler “按当前活跃会话过滤” | **广播**:每条开流的队列都收到(qi-agents 侧 `_web` → `runner._on_update` 的 bus 事件无会话号);前端按 `content.toolCallId` 收敛,不是本流的帧随新轮丢弃 | payload 里就没有会话身份 —— 要过滤得改 `src/`,不值得;归卡是硬事实(`tool_call_id`),认卡比认会话可靠 |
| per-run 队列,主流循环排空 | **per-stream 单队列混装** `(kind, payload)`(`activity.py` 的三态 kind),主事件 pump 任务与 bus handler 都往里写,`agui.mixed_frames` 合并出帧 | 子运行期间主流静默(LLM 调用不产事件),活动帧若等主流事件就饿死;同一队列的 FIFO 还顺带保证“卡帧在前、活动在后” |
| `phase: start \| think \| act \| done \| error` | `start \| stream \| act \| think \| done`(失败并入 `done` 的 `ok:false, error`);补上 `stream`(流式缓冲) | `error` 当阶段与 `ok/err` 双字段二选一;`stream` 是思考/正文的实时态,缺了直播只剩干巴巴的活动行 |
| `messageId = subagentRunId` | `messageId = "qi-act-<run>"`(同一 run 的多帧共享)+ `replace: true` | 同一“消息”的更新应是同一 id;`subagentRunId` 单独放字段当归因键(与 AG-UI 多智能体字段同名) |
| 收尾补 `SUBAGENT_STARTED/FINISHED/ERROR` | **已发**(见下面的修版) | 用户反馈“分不清谁是谁”暴露了根因:生命与内容混在一个通道里,前端只能倒推;0.0.59 里本来就有原生事件 |
| 结束时收敛进 `details.ui` | **一期已完成**(横幅契约);二期只管运行中,`TOOL_CALL_RESULT` 一到直播表清空 | 同一份文字不显两遍(回放红线的反面) |
| `turn.ts` 按 `messageId` 归卡 | 按 `content.toolCallId` 归卡,早到帧进 `pendingActivity` 暂存(每轮清) | `messageId` 标的是“哪个子 run”,不是“哪张工具卡”;归卡必须用卡的 id |
| capability `activity` | ✓ `/api/meta` 新增 `activity: true` | 没有它,界面退回一期(只能结束看) |

推送端(qi-agents)只加了一个闭包 `_web(phase, **extra)`:五个调用点、吞掉一切异常 ——
直播是旁路,推不成也不能带倒子运行。宿主侧的新增面是 `qi_lab/activity.py`(过滤与形状)+
`state.py` 的订阅与广播(WeakSet 防强引用钉住被驱逐的 runtime)+ `agui.feed_activity` /
`mixed_frames` + `app.py` 的双生产者队列。测试:`test_lab_activity.py`(25)与
`test_qi_agents_activity.py`(6),含逐字一致的回放红线断言,变异验证 ×10(含前端 ×4)。

#### 修版:子智能体是协议的一等事件,界面上是**一张有身份的卡**(2026-10-10)

用户反馈:「还是不行,我希望在思考过程中分配到不同子智能体,能明显区分不同子智能体。」
分两件事修。

**一、协议:生命与内容分开(AG-UI 已有,不必发明)**

一期把生 / 死也塞进 `ACTIVITY_SNAPSHOT` 的 `phase` 里 —— 能用,但前端只能**从内容帧里
倒推“好像多了个人”**。0.0.59 的 `EventType` 里本来就有 `SUBAGENT_STARTED /
FINISHED / ERROR`:

- 生:`SUBAGENT_STARTED{subagentRunId, name, description, parentToolCallId, parentSubagentRunId?}`
  —— `parentToolCallId` 正好指回发起它的那张工具卡;
- 死:`SUBAGENT_FINISHED{subagentRunId, outcome}` / `SUBAGENT_ERROR{subagentRunId, message, code}`;
- 内容:`ACTIVITY_SNAPSHOT{activityType, content, replace, subagentRunId}`(不变)。

`SUBAGENT_FINISHED / ERROR` 协议里没有归属字段 → qi 在三条事件上都带一份
`metadata{toolCallId, depth, parent}`(AG-UI 允许的自由字段),前端三把钥匙同一把锁。

1.0 另给了 `ActivityMessage{role:"activity", subagentRunId?}` 与
`MultiAgentCapabilities{subagents:[SubagentInfo{name, description?}]}` —— 前者是
“活动作为一种消息”、后者是“声明有哪些角色”。三期做角色选择器时用得上,二期不需要。

**二、视觉:身份靠结构,不靠色相**

先查了 dsw 的呈现 token 池(实测):只有 deepseek(蓝紫)/ red / green / amber
四个色相,其中三个是**状态语义色**。拿绿色标一个 agent,用户会读成“它成功了” ——
所以身份不能用色相:

| 手段 | 作用 |
| --- | --- |
| 首字母**徽标**(方块,`bg-layer-3`) | 扫读时第一眼区分(scout / librarian / scribe) |
| 名字**加粗 + 正文档位** | 不再是 11px caption —— 它是这张卡的主语 |
| **缩进 + 左侧深度条**(明度编码层级) | 一眼看出谁在谁下面,且不冒充状态色 |
| 归属写全 `← 由 X 指派 · 深度 N` | 用户问的“分配到不同子智能体”就是这一行 |
| **活着才有脉动点 + 更亮的边与底** | 人一多,眼睛先找到正在跑的那个 |
| 入场动画(`prefers-reduced-motion` 关) | 新 agent 出现时不被长列表吃掉 |

活动行继续用一期的一行语言(`· tool …` / `✎ 第N轮: …`),所以运行中看到的与结束后
横幅卡里重放的是同一段文字(回放红线)。行语言与 `Segments` 同源(`seg__line`),
但卡片外壳分开:`sub*` = 运行中(未定稿),`seg` = 结束后(已定稿)。

前端契约守卫(`ui/src/api/contract.test.ts`)在这次改动的第一轮里就变红了 ——
它从 python 源码刮事件类型与前端清单对拍,`SUBAGENT_*` 三条被当场要求声明。

### 5.3 三期(可选,做完再评估)

- 全套 `SUBAGENT_*` + `MultiAgentCapabilities.subagents` 用于角色选择器;
- 侧栏只读"委派摘要"(模式 D):本次委派了谁、深度、成败、耗时;
- **qi-web 同步**:两份 `ui/src` 是复制关系,不同步就会两套行为。

---

## 6. 风险与红线

1. **子运行不落盘** → 直播信息不收敛 = 刷新即丢(§5.2 红线)。
2. **`MAX_TOOL_DETAILS_CHARS` / `_clip`** → 三区内容必须有裁剪策略,否则 details 整包被替换。
3. **并行与嵌套**:parallel 的多个子 agent 同时推,按 `subagentRunId` 分卡天然正确;
   chain 有顺序;嵌套靠 `parentSubagentRunId` / depth。
4. **总线无 `off`** → 一次性注册 + 过滤,别每轮 `on`。
5. **推送必须旁路**:沿用 `subagent._on_event` 现有的 `try/except`("卡片是旁路:失败不该带
   倒子运行"),web 推送坏了不能影响回合。
6. **不改 TUI 行为**:`subagent.py` / `render.py` 是两边共用的,新增推送对 TUI 要无感
   (TUI 已有自己的卡,别重复画)。
7. **AG-UI 严格客户端**对新帧的反应要实测(qi 自己是唯一消费者,`RAW` 兜底路径仍在)。
8. **`ACTIVITY_*` 目前在 `turn.ts` 是"显式忽略"** —— 改成处理的那一刻,`qi-web` 的复制件
   也得一起改,否则两个扩展行为分叉。

---

## 7. 验收与测试

- **后端**:agui 帧序测试(`ACTIVITY_*` 出现的时机、`subagentRunId` 归属、`replace` 语义)、
  `details.ui` 结构与截断测试、`_ui_items` 的 `state/depth` 断言;新测试按仓库惯例做**变异验证**
  (改一处逻辑必须红)。
- **前端**:`turn.ts` reducer 对 `ACTIVITY_SNAPSHOT` 的分支(vitest 纯函数,`environment: "node"`)、
  depth 缩进函数、三区解析兜底;`tsc --noEmit`、`npm run check:design`(色值门禁)、
  `npm run build` 重新生成 `qi_lab/static`。
- **文档**:`docs/api/ag-ui.md` 事件表 + `ACTIVITY_*`、`docs/api/README.md` 能力位行、
  `design/web.md` §16.2 的 v1.1 草案、扩展 `README` 测试表 —— 否则 `test_lab_docs_api` 会红。
- **端到端**:真跑一次多层委派,肉眼看三件事 —— 派单瞬间有没有卡、运行中有没有活动 / 思考、
  刷新后是否同一张卡。

---

## 8. 影响面与建议顺序

| 期 | 改动 | 不改 |
| --- | --- | --- |
| 一期 | `qi-lab/ui`(segments 新模块、Rows、types、CSS)、`design/web.md` §16.2 v1.1 | **`qi-agents` 也没改**(计划里有,落地时确认不需要)、`src/`、`agui.py`、TUI、协议 |
| 二期 | `qi-agents/subagent.py:_on_event`(推送)、`qi-lab`(agui 翻译 + app 订阅)、`qi-lab/ui`、`docs/api` | `src/qi_agent/`(复用 `ctx.on_update`)、TUI 行为 |

**建议先做一期**:改动小、风险低,先把 §1.1 那两个明确缺陷(失败画成未开始、深度看不见)
与文本墙一起修掉;二期单独一轮(涉及总线订阅与回放收敛,值得单独验证 + 变异测试)。

---

## 附录 A · 证据索引

| 断言 | 位置 |
| --- | --- |
| web 无 frontend,`notify` 落 `notes` | `src/qi_agent/extensions.py:816`;`qi_lab/` grep `frontend` = 0 |
| 子运行事件只进 `_on_event` | `extensions/qi-agents/qi_agents/subagent.py:431` |
| `details` 只在结束时构造 | 同上 `:698` |
| `_ui_items` 发 `state:"error"`、深度靠前导空格 | 同上 `:589` |
| 词汇表 v1 的 state 只有三值 | `design/web.md:644` |
| 失败画成 `○` | `ui/src/components/Rows.tsx:59-80` |
| 深度缩进被吃 | `ui/src/theme/app.css:3008` |
| `ACTIVITY_*` 已声明但忽略 | `ui/src/api/types.ts:508-521`;`ui/src/state/turn.ts:606-613` |
| qi 只发这些事件 | `qi_lab/agui.py:240-347`;`docs/api/ag-ui.md:78-104` |
| 钉的编码器版本 | `qi_lab/app.py:679` |
| 直播与回放不一致是红线 | `qi_lab/app.py:732` |
| `tool_execution_update` 载荷 | `src/qi_agent/runner.py:519-537` |
| 总线只有 `on` 没有 `off` | `src/qi_agent/extensions.py:1619` / `:1630` |
| 子运行不落盘 | `src/qi_agent/runtime.py:576` |
| TUI 平铺卡契约 | `qi_agents/render.py:48/50/58/61/144/320/327/333/368/396/438` |

## 附录 B · AG-UI 事件集的核对方法

本文 §3 的事件集是**拉官方包 `.d.ts` 读出来的**,不是转述。复核:

```bash
# 0.0.59(qi 引用的那个)
curl -sL "https://cdn.jsdelivr.net/npm/@ag-ui/core@0.0.59/dist/index.d.ts" | grep -A40 "declare enum EventType"
# 1.0(PROTOCOL_VERSION = "1.0")
curl -sL "https://cdn.jsdelivr.net/npm/@ag-ui/core@1.0.2/dist/index.d.ts"
```

qi 侧没有依赖 `@ag-ui/*`(`extensions/qi-lab/ui/package.json` 无该依赖)——
事件类型是手写的(`ui/src/api/types.ts`),所以"协议演进了而 qi 没跟上"这件事
不会有任何工具报警。这也是本文要把核对方法写下来的原因。
