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
- **写入有两种**,语义不同:
  - `append()` —— 追加一行,不重写文件;
  - `save()` —— 整文件重写(改名、改标题、迁移、分叉都走它)。
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

> **标题有两处**:`Session.title`(运行时读的)与 header entry 的 `title`(磁盘上的真相)。
> 只改一处会出现「列表里是新名、重开又变回旧的」,所以改名统一走 `SessionStore.set_title()`
> —— 它同时改内存、header、磁盘,写盘失败还会把内存**回滚**。

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

qi 目前**写入** 6 类,另有 1 类**只读**。

| `type` | 谁写 | 关键键 | 说明 |
| --- | --- | --- | --- |
| `session` | `SessionStore.create()` | 见 §2 | header,不参与树 |
| `message` | `runtime._persist_final()` / 用户消息落盘 | `role` · `content` · `agent_id` · `usage`(仅助手) | 唯一**进 LLM 上下文**的类型 |
| `tool` | `runtime._persist_tool()` | `tool` · `args` · `status` · `duration_ms` · `exit_code` · `error` · `result` · `details` | 一次工具往返;结构化字段来自 `AgentRunner`,前端不必解析 `result` 字符串 |
| `custom` | `runtime._persist_thinking()` / `_persist_narration()` / `append_extension_entry()` | `custom_type` · `content` 或 `data` · `source`+`agent`(扩展写) | **不进上下文**(见 §6) |
| `compaction` | `compaction.compact()` → `Runtime.compact_session()` | `summary` · `firstKeptEntryId` · `tokensBefore` · `usage` | 一次压缩的产物;`firstKeptEntryId` 决定窗口起点 |
| `branch_summary` | `Runtime.summarize_branch_for_jump()` | `summary` · `fromId` · `usage` | `/tree` 跳分支时,把「被放弃的那段」压成摘要挂到新位置 |
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

**关于落盘顺序**(回放器可以直接依赖这四条;它们都是为了"文件里的顺序 = 真实因果"):

- **用户消息立即落盘**,不等回合结束 —— 否则运行中刷新/断线就看不到自己说了什么。
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
{"type":"message","role":"user","content":"看看 src/parse.py 的 bug","agent_id":"general","ts":"2026-09-20T10:28:11","id":"a1b2c3d4e5f6","parentId":null}
{"type":"custom","custom_type":"assistant_thinking","agent":"general","content":"先读文件…","ts":"…","id":"b2c3…","parentId":"a1b2c3d4e5f6"}
{"type":"tool","agent":"general","tool":"read","args":{"path":"src/parse.py"},"status":"ok","duration_ms":12,"exit_code":null,"error":null,"details":null,"result":"…","ts":"…","id":"c3d4…","parentId":"b2c3…"}
{"type":"message","role":"assistant","content":"问题在偏移量计算。","agent_id":"general","usage":{"turns":1,"llm_calls":2,"prompt_tokens":3120,"completion_tokens":140,"total_tokens":3260,"context_tokens":3120},"ts":"…","id":"d4e5…","parentId":"c3d4…"}
```

（字段顺序与省略号仅为可读性;真实文件里 `append()` 补的三个字段位置由调用点决定,解析不要依赖顺序。）

## 11. 稳定性

**可以依赖的**:`type` 取值集合(§5)· header 的 `id`/`version`/`cwd` · entry 的 `id`/`parentId`/`ts`
· `message` 的 `role`/`content` · `compaction` 的 `summary`/`firstKeptEntryId`。

**不要依赖的**:字段顺序 · 未知 `type` 的存在与否(旧会话里有已废弃的类型,新类型也会加进来)
· `custom_type` 的完整清单(扩展可以自定义)· `usage` 里字段是否齐全(provider 差异大;
缺失就是缺失,不补估值 —— 见 [sessions.md](sessions.md) §5)。

**读到不认识的 `type` 时按"照原样保留、能显示就显示"处理,绝不丢弃** —— 这是 qi 各处一致的原则
(与 `details["ui"]` 同一条),旧会话在新版本里仍要能回放。
