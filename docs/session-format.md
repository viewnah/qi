# 会话文件格式

一次会话 = **一个 JSONL 文件**,一行一个 JSON 对象。格式是**树**(v2):每条 entry 带 `id` 与
`parentId`,「历史」= 从某个节点沿 `parentId` 回溯到根的那条链。所以同一个文件里可以并存多条
分支 —— 回到旧节点继续提问,新内容成为它的子节点,旧分支原样保留。

**事实源:`src/qi_agent/session.py`。**本文任何一处与代码不一致,以代码为准。

## 1. 文件与读写约定

- **位置**:`~/.qi/agent/sessions/<YYYYmmddTHHMMSS>_<id>.jsonl`
  (`settings.json` 的 `sessionDir` 可覆盖目录;全局一份,不按项目分目录 —— 按项目分组靠 header 的 `cwd`)。
- **文件名里的 `<id>`** = header 的 `id`,12 位小写 hex(`uuid4().hex[:12]`)。
- **编码**:UTF-8,`ensure_ascii=False` —— 中文原样落盘,不是 `\uXXXX`。
- **读取**(`SessionStore._read`):逐行 `json.loads`;**空行跳过,坏行也跳过**。
  一行损坏不会让整条会话读不出来 —— 「会话读不出来」比「丢一行」严重得多。
- **写入有三种**,语义不同:
  - `append()` —— 追加一行,不重写文件;
  - `save()` —— 整文件重写(改名、改标题、迁移、分叉都走它);
  - `flush()` —— **整文件写出**("预留但还没落盘"的会话首次落盘,见 §9.1)。
  例外:如果一个读入的老会话刚被内存补过链(`Session.migrated`),`append()` 也会先
  **整文件重写** —— 否则新节点的 `parentId` 指向的那条链只存在于内存里,重开即断。

## 2. header(`entries[0]`)

元数据,**不参与树**:

| 键 | 说明 |
| --- | --- |
| `type` | 恒为 `"session"` |
| `version` | `2` = 树格式;`1` = 线性旧格式。**坏值按 2 处理**(不因一行脏数据报错) |
| `id` | 会话 id |
| `title` | 会话标题 |
| `created_at` | `YYYY-MM-DDTHH:MM:SS`(本地时区) |
| `cwd` | 会话工作目录(绝对路径)。旧会话可能没有 → 首次使用时由 `ensure_cwd()` 回填并整文件重写 |
| `parentSession` | 本会话从哪个会话文件分叉出来(**只有** `/fork` `/clone` / `qi --fork` 会写,pi 同名字段)。会话选择器的树状视图按它缩进;老会话没有这个键 |

> **标题有两处**:`Session.title`(运行时读的)与 header entry 的 `title`(磁盘上的真相)。
> 只改一处会出现「列表里是新名、重开又变回旧的」,所以改名统一走 `SessionStore.set_title()`
> —— 它同时改内存、header、磁盘,写盘失败还会把内存**回滚**。
>
> **空标题与历史默认名 `tui` 都算「没有名字」**(`SessionStore`/`has_title()` 同一口径):
> 自动命名据此决定要不要起标题,会话选择器据此决定显示标题还是**回落第一句话**
> (`Session.display_label`,pi 的 `firstMessage` 同款)。见 [sessions.md](sessions.md) §7。

## 3. entry 的公共字段

`append()` 会自动补三个字段(调用方显式给了就不覆盖):

| 字段 | 由谁补 | 说明 |
| --- | --- | --- |
| `ts` | `append()` | 写入时间戳 |
| `id` | `append()` | 12 位 hex,树节点标识 |
| `parentId` | `append()` | 父节点 = **写入那一刻的当前节点**;根节点为 `null` |

## 4. 树语义

| 概念 | 定义 | 代码 |
| --- | --- | --- |
| `tree_entries` | 参与树的 entry = 除 header 外的全部 | `Session.tree_entries` |
| `leaf` | 文件里**最后一条**带 `id` 的非 header entry | `Session.leaf` |
| `current` | 当前节点:**优先 `position`,否则 `leaf`** | `Session.current` |
| `branch(leaf=None)` | 从 `leaf`(默认 `current`)沿 `parentId` 回溯到根,**正序**返回 | `Session.branch()` |
| `children(id)` | 某节点的直接子节点(`id=None` = 根层),用于树渲染 | `Session.children()` |
| `branch_points` | 分叉点数 = 有 >1 个子节点的节点数 | `Session.branch_points` |

**`position` 是内存态,不落盘**:它表示「下一次提问挂在哪」,默认 `None`(即用 `leaf`)。
`/tree` 跳到旧节点时通过 `SessionStore.set_position()` 设定;此后新 entry 就挂在那个节点下,
于是文件里出现第二条分支。

**历史只走 `branch()`** —— 会话上下文、消息计数、回放都只看这条链,不看整个文件。
`entries` 则是**文件里的全部 entry**(含 header 与其它分支),两者不要混用。

**损坏时的退化**:悬空 `parentId`、成环、`current` 指向不存在的节点,`branch()` 都**不抛异常**
—— 成环时截断,认不出目标时退回文件顺序。理由同上:「读不出来」比「丢几条分支」严重。

## 5. entry 类型

qi 目前**写入** 8 类,另有 1 类**只读**。

| `type` | 谁写 | 关键键 | 说明 |
| --- | --- | --- | --- |
| `session` | `SessionStore.create()` | 见 §2 | header,不参与树 |
| `message` | `runtime._persist_final()` / 用户消息落盘 | `role` · `content` · `agent_id` · `usage`(仅助手) | 唯一**进 LLM 上下文**的类型 |
| `tool` | `runtime._persist_tool()` | `tool` · `args` · `status` · `duration_ms` · `exit_code` · `error` · `result` · `details` | 一次工具往返;结构化字段来自 `AgentRunner`,前端不必解析 `result` 字符串 |
| `custom` | `runtime._persist_thinking()` / `_persist_narration()` / `append_extension_entry()` | `custom_type` · `content` 或 `data` · `source`+`agent`(扩展写) | **不进上下文**(见 §6) |
| `compaction` | `compaction.compact()` → `Runtime.compact_session()` | `summary` · `firstKeptEntryId` · `tokensBefore` · `usage` | 一次压缩的产物;`firstKeptEntryId` 决定窗口起点 |
| `branch_summary` | `Runtime.summarize_branch_for_jump()` | `summary` · `fromId` · `usage` | `/tree` 跳分支时,把「被放弃的那段」压成摘要挂到新位置 |
| `model_change` | `Runtime.set_model()` → `SessionStore.set_context_setting()` | `provider` · `model_id` | **设置类**:记录"这时换成了哪个模型"。续会话时按它还原(见 §6.1);`_history()` 不读它 |
| `thinking_level_change` | `Runtime.set_thinking_level()` | `thinking_level` | **设置类**:同上,记思考级别。只在级别**真的变了**时才写(pi 的 `isChanging`) |
| `dispatch` | **无人写**(遗留) | `agent` · `display_name` · `confidence` · `reasoning` · `source` | v3 取消 auto 分派后不再产生;`cli.py` / `tui.py` / qi-web 仍**读取**它,以便旧会话能正确回放 |

`custom` 的两个来源形状不同,`source` 字段是区分点:

```jsonc
// core 写的(思考、叙述)
{"type": "custom", "custom_type": "assistant_thinking", "agent": "general", "content": "…"}

// 扩展写的(api.appendEntry;章由宿主盖,扩展只给 custom_type 与 data)
{"type": "custom", "custom_type": "plan_steps", "source": "qi-plan", "agent": "general", "data": {…}}
```

`append_extension_entry()` 是**扩展自定义 entry 的唯一写口**。**没有活动会话时它直接报错**
(回合外的写往往是"想存但存错地方"的第一步)。扩展自己拼 entry 迟早会出现两种形状,而回放与
诊断都得同时认两种,所以章由宿主盖。

> 用户消息的 `agent_id` 是"**将处理它的** agent",不是发言者(`"extension"` 表示这条是扩展注入的)。

## 6. 上下文组装:`custom` 为什么不进上下文

`Runtime._history()` 只看当前分支,且**只读 `message`**,装配规则是:

```text
最近一次 compaction 的 summary(若有)
  → 从它的 firstKeptEntryId 起头的 message
  → 沿途的 branch_summary
```

思考与叙述做成 `custom` 而不是 `message` 是**刻意的**:它们在界面上要看得见(否则刷新/回放里
整段消失,直播与回放不一致),但**不能进 LLM 上下文**,否则每一轮都在给模型灌过程文本
(零提示词回归风险)。

### 6.1 设置类 entry:模型与思考级别

`model_change` / `thinking_level_change`(pi 同名两类)走的是**同一条取舍**:

- **不进上下文** —— `_history()` 只读 `message`,所以切换模型不会往提示词里加任何东西。
- **不在 `/tree` 默认视图里显示** —— 每一行都重复一遍"模型: x/y"只会淹没对话;
  `all` 过滤档仍能看到它们(pi 的 `isSettingsEntry` 也是这个口径)。
- **但要在文件里** —— 两个理由:
  1. **续会话时按它还原**。`Runtime.bind_session()` 读当前分支上最后一条同类 entry
     (`session.context_settings()`),把模型与级别恢复过来,而不是回 settings 默认
     (pi 的 `getSessionContextSettings` + `restoredModel`)。
     还原前先问一句"拿得到 key 吗"(`resolve_key(...).ok`,对应 pi 的 `hasConfiguredAuth`):
     provider 还在、但密钥已删时**退回默认 + 记一条 note**,而不是发一次注定 401 的请求。
     CLI 显式给的 `--model` / `--thinking` 是当次覆盖,**优先级高于**会话里记的旧值。
  2. **时间线上留下事实**。回放/诊断里能看出"这一轮开始换的模型",而不是只有结果没有原因。

**为什么 agent 看不见切换动作**:换模型是**界面状态**,不作为消息进对话 —— 所以 agent
"知道自己现在是什么"却"不知道刚才换过"。要让它能自查,走会话环境变量
(`QI_PROVIDER` / `QI_MODEL` …),见 [tools.md](tools.md) §3.1。

**写入时机**(三处,都由 `Runtime` 收口,所以两个入口 —— TUI 的 `/model` 与扩展的
`api.setModel` —— 不会一个漏写一个重写):

| 时机 | 写什么 |
| --- | --- |
| 新会话绑定时 | 起点两条(pi:只在**新**会话写 `appendModelChange`) |
| 换模型 / 换级别 | 各自的 entry;**同值不重写**(否则每次启动都加两行) |
| 换模型触发级别自动调整 | 那条 `thinking_level_change` 也写(它是一次真实的切换) |

**回合外也能写**:`_active_session` 只在一个回合内有效,而 `/model` 发生在两次提问之间 ——
所以前端选完会话要调 `bind_session()`,写入点取 `_active_session or _bound_session`。

### 6.2 "哪个会话"必须按**会话**记,不能按"来过没有"

`bind_session()` 要服务一种 CLI/TUI 里少见、而 web 是常态的形态:**一个 runtime 服务
同一 cwd 下的多条会话**(`WebState.runtime_for` 按 cwd 缓存)。为此有三条记账规则,
每一条早先都写错过,而症状都是"**静默串会话**"(不报错,只是拿着另一条会话的设置跑):

| 记账 | 判据 | 写错的症状 |
| --- | --- | --- |
| 还原与否 | `_restored_session` = **上一轮绑的那个 id** | 用 `set[str]` 记"还原过的 id" → A → B → A 切回来**不再还原**,留在 B 的模型上 |
| 显式级别 | `_level_pins: set[session_id]`(**会话级**)+ `_level_pinned_cli`(整场运行) | 用一个 `bool` → 在 A 上设过一次,之后**任何**会话里记的级别都不再生效 |
| 还原失败 | 退回 `_default_llm_exec`(构造期那份) | 只记一条 note → 留在**上一个会话**的模型上,而 note 写着"已用默认模型继续" |

三条的判据可以合成一句:**"当前设置"是会话的属性,不是 runtime 的属性**。
runtime 上那份只是一个**缓存**,每次换会话都要用会话里记的 entry 重填。

两条边界(与上面三条配对,别把它们一起改坏):

- **同级重绑不还原**。web 每一轮开跑前都会 `bind_session(same_session)`,若同级也还原,
  用户刚换的模型会被每一轮悄悄洗掉。
- **会话级的"显式设过"不拦还原**。会话里记的级别**就是**用户在那个会话上显式选的那个 ——
  还原它不是在覆盖选择,而是在装回来。所以 `_level_pins` 只用在一处:换模型时的
  `modelThinkingLevels` 自动联动(不该覆盖用户的显式选择)。**只有命令行那档**(`--thinking`
  / `--model p/m:级别`)才拦还原 —— 那是当次的显式覆盖,与 `--model` 同级。

**绑定必须是同步的**(TUI 这条线踩过一次):`_select_session()` 之后紧跟一句**同步**的
`_sync_model_from_runtime()`,而 `session_start` 走的是 async worker —— 若只靠 worker 里的
bind,续会话时界面读到的是 restore **之前**的模型。表现是"footer 显示 settings 默认、
请求却发会话里记的那个"(见 `tests/test_tui_model_restore.py`)。所以 `_select_session()`
自己先同步 bind(`_bind_current_session`),`/resume` / `/fork` / `/import` 也各自 bind。
web 端同理:每轮开跑前 `WebState.bind()`(`bind_session` + `start_session`),而
`POST /api/model` 只做同步的 `bind_session`(换一次模型不该顺手派发 `session_start`)。

**命名差异**:pi 是驼峰 `modelId`,qi 用本地约定 `model_id`(与 `custom_type` /
`agent_id` / `duration_ms` 一致)。语义相同、键名不同,读 pi 会话时注意。

## 7. 封顶(会话文件不无界增长)

| 常量 | 值 | 作用于 |
| --- | --- | --- |
| `MAX_TOOL_ENTRY_CHARS` | 8000 | `tool.result` · 思考正文 · 叙述正文(超出后追加 `…(落盘已截断)`) |
| `MAX_TOOL_DETAILS_CHARS` | 8000 | `tool.details` |

`details` 超限时**不切字符串**(切 JSON 会得到非法 JSON),而是换成可渲染的标记:
`{"_truncated": true, "_full_chars": <原长度>}`。

## 8. v1 → v2 迁移与容错

`version: 1` 的线性文件(entry 无 `id`/`parentId`)在**读入时**于内存里补链:按文件顺序串成一条
链、补 `id`、把 header 的 `version` 改成 2(`SessionStore.migrate()`)。

- **读路径不写文件**:TUI 里的会话列表之类的只读操作不会改动老会话。
- 首次写入(`append` / `save`)时才会整文件落盘 —— 与 pi「加载时迁移」等价。
- `migrated` 标志就是为此存在:它让 `append()` 知道"必须重写"(见 §1)。

## 9. 写路径与落盘顺序的不变量

**关于写路径:**

- **`position` 不落盘**。它只在内存里;重启后 `current` 退回 `leaf`。要持久化"当前在哪"就得
  靠 `branch_summary` 这类 entry 本身挂在目标位置下(这也是 `summarize_branch_for_jump()`
  先 `set_position()` 再 `append()` 的原因)。
- **分叉(`fork_at`)复制分支到新文件,并保留原 `id`/`parentId`**。链在新文件里自洽,
  将来互相引用时不会指到另一个文件去。`entry_id=None` = 空历史(从第一条消息之前开始)。

**关于落盘时机**(§9.1 详述):新会话**预留时没有文件**,要到第一条助手回答才真正写出。

**关于落盘顺序**(回放器可以直接依赖这四条;它们都是为了"文件里的顺序 = 真实因果"):

- **用户消息立即落盘**,不等回合结束 —— 否则运行中刷新/断线就看不到自己说了什么。

### 9.1 会话的三态:落盘 / 预留 / 内存

`Session` 有两个独立标志位,组合出三种会话(不要拿 `path.exists()` 判 —— 那会在"文件刚被删掉"
时给出错误答案):

| | `ephemeral` | `unflushed` | 什么时候出现 | 行为 |
| --- | --- | --- | --- | --- |
| **已落盘** | False | False | `create()` / `--fork` / 从磁盘读入 | 正常追加/重写 |
| **预留** | False | True | 裸 `qi` 启动、`/new`(`reserve()`) | entries 照常攒;**`append` 只在出现第一条 assistant 时 `flush()`**;`set_title`(改名)也立刻 `flush()` —— 见下 |
| **内存** | True | — | `--no-session`(`ephemeral()`) | 永不写盘(append/save/set_title 全跳过) |

**为什么要"预留"而不是"启动就建文件"**:本机真实事故 —— `~/.qi/agent/sessions/` 攒了
3000+ 个只有一行的空会话,全部来自"进来看一眼就走"。同一根因在 web 端也有(见
[design/web.md](../design/web.md) §18.28 的"懒创建"),pi 的做法是 `newSession()` 只算路径、
`flushed=false`,真写盘在第一条 assistant 回答(它的 `_persist`)。

**为什么留住会话对象、只懒文件**:pi 的 `SessionManager` 构造时就 `newSession()`,
于是"当前会话"这个不变量始终成立 —— 历史、用量、扩展的 `ctx.session_manager`、`/session`
信息行、回合链路都照常工作。若改成"启动时没有会话",下游每一处都要判 None,而那些判空迟早漏一个。

**落盘用独占创建、拒绝覆盖**:`flush()` 以 `"x"`(即 `O_EXCL`)打开文件 —— 预留期间**不该**
存在文件,真有就说明路径撞了。会话是不可再生的用户数据,宁可报错也不能静默盖掉别人的会话。
(一次 `write` 写出全部 entry;中途崩溃会留下一个截断的文件,这与 `save()` 的风险相同,
这里没有额外加 fsync/临时文件 —— 对齐 pi 的 `_persist`,不为一个极罕见的窗口加一层复杂度。)

**只问不答不落盘**:判据是"有没有 assistant 回答"(`_has_assistant`),不是"有没有 entry"。
一个没有回答的会话在 `/resume` 列表里没有价值,而文件一出现就会进列表。

**改名会立刻落盘(唯一一个「没有回答也写文件」的口子)**:`/name`(以及扩展的
`ctx.setSessionName` / 会话选择器的 `ctrl+r`)走 `set_title()`,而它承诺"内存 + header +
落盘三处一起改" —— 打在一个**预留中**的会话上时,那个 `save()` 曾经是空操作(文件推迟到第一条
回答),于是名字只活在内存里:`/name` 之后 `/new`(或退出)会连**会话带名字**一起消失,
而界面刚说过"会话名已设为 X"。所以命名一律 `flush()`:`create()` 那条"显式意图立刻落盘"的
规矩同样适用于"我给这个会话起了名" —— 一个名字总得有文件可落(`qi -n <名字>` 本来就立刻落盘)。
自动命名不会走这条路:它在回合末尾才落标题,而那会儿第一条 assistant 回答已经把会话写出去了。

**显式创建照旧立即落盘**:`-c` / `--session` / `--fork` / `-n` / `--session-id` /
web 的 `POST /api/sessions` 都走 `create()` —— 那是调用方明确要的东西,懒建只针对"什么都没说"。

- **取上下文在用户消息落盘之前**(`history = self._history(session)` 在前,`append` 在后)。
  反过来写,本轮输入会在上下文里出现**两次**(两条一样的 user)。
- **思考先于回答落盘**:思考产生在这条助手消息之前,顺序反了回放就成了"先回答、再思考"。
- **宣布了工具调用的助手消息立刻落成 `custom`(叙述),不缓到下一轮**。缓冲的话它会被写在
  它触发的工具卡片**之后**,回放顺序变成"工具卡 → 叙述",与真实因果相反。不带工具调用那条
  由回合末尾的 `message` 代表,所以不会重复。

这四条与 §6 的上下文组装是两回事:那说的是"哪些 entry 进模型",这里说的是"它们按什么顺序
写进文件"。

## 10. 典型文件

```jsonc
{"type":"session","version":2,"id":"9f3a1c2b7d4e","title":"修一个解析 bug","created_at":"2026-09-20T10:28:03","cwd":"/Users/x/proj"}
{"type":"model_change","provider":"deepseek","model_id":"deepseek-chat","ts":"…","id":"9a1b…","parentId":null}
{"type":"thinking_level_change","thinking_level":"medium","ts":"…","id":"ab2c…","parentId":"9a1b…"}
{"type":"message","role":"user","content":"看看 src/parse.py 的 bug","agent_id":"general","ts":"2026-09-20T10:28:11","id":"a1b2c3d4e5f6","parentId":"ab2c…"}
{"type":"custom","custom_type":"assistant_thinking","agent":"general","content":"先读文件…","ts":"…","id":"b2c3…","parentId":"a1b2c3d4e5f6"}
{"type":"tool","agent":"general","tool":"read","args":{"path":"src/parse.py"},"status":"ok","duration_ms":12,"exit_code":null,"error":null,"details":null,"result":"…","ts":"…","id":"c3d4…","parentId":"b2c3…"}
{"type":"message","role":"assistant","content":"问题在偏移量计算。","agent_id":"general","usage":{"turns":1,"llm_calls":2,"prompt_tokens":3120,"completion_tokens":140,"total_tokens":3260,"context_tokens":3120},"ts":"…","id":"d4e5…","parentId":"c3d4…"}
{"type":"model_change","provider":"zhipu","model_id":"glm-4.6","ts":"…","id":"e5f6…","parentId":"d4e5…"}
```

（字段顺序与省略号仅为可读性;真实文件里 `append()` 补的三个字段位置由调用点决定,解析不要依赖顺序。）

## 11. 稳定性

**可以依赖的**:`type` 取值集合(§5)· header 的 `id`/`version`/`cwd` · entry 的 `id`/`parentId`/`ts`
· `message` 的 `role`/`content` · `compaction` 的 `summary`/`firstKeptEntryId`
· `model_change` 的 `provider`/`model_id` · `thinking_level_change` 的 `thinking_level`(§6.1)。

**不要依赖的**:字段顺序 · 未知 `type` 的存在与否(旧会话里有已废弃的类型,新类型也会加进来)
· `custom_type` 的完整清单(扩展可以自定义)· `usage` 里字段是否齐全(provider 差异大;
缺失就是缺失,不补估值 —— 见 [sessions.md](sessions.md) §5)。

**读到不认识的 `type` 时按"照原样保留、能显示就显示"处理,绝不丢弃** —— 这是 qi 各处一致的原则
(与 `details["ui"]` 同一条),旧会话在新版本里仍要能回放。
