# 上下文压缩

长会话迟早会超出模型窗口。qi 把「太久以前的部分」压成一段**结构化摘要**再继续跑 —— 不是丢弃,
也不是简单截断。

两种机制共用同一套摘要格式与同一条 LLM 调用路径:

| 机制 | 触发 | 压什么 | 落成 |
| --- | --- | --- | --- |
| **压缩** compaction | 上下文超阈值,或用户 `/compact` | 旧消息 | `type=compaction` entry(+ `firstKeptEntryId`) |
| **分支摘要** branch summary | `/tree` 跳到别的分支 | 「被放弃的那段」 | `type=branch_summary` entry |

**事实源:`src/qi_agent/compaction.py` + `runtime.py` 的 `_history()` / `_maybe_auto_compact()`。**

## 1. 什么时候自动压

开新一回合**之前**检查一次(`_maybe_auto_compact`):

```text
tokens > contextWindow - reserveTokens        →  压
```

- **`reserveTokens` 是留给模型回答的余量**(默认 16384,pi 默认值),所以阈值不是窗口本身。
- **预算口径是"重建后的上下文 + 基座提示词",不是原始 entry 之和**:
  `messages_tokens(_history(session)) + estimate_tokens(base_prompt)`。
  用原始 entry 求和会把**已经压掉的内容再算一遍** —— 于是压完还超,反复压。
- `contextWindow` 来自当前模型的 spec;拿不到窗口(`<= 0`)或 `compaction.enabled=false` 就整段跳过。

相关设置(`settings.json` 的 `compaction`,见 [settings.md](settings.md)):

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 关掉后不再自动压(手动 `/compact` 仍可用) |
| `reserveTokens` | `16384` | 给回答留的余量 |
| `keepRecentTokens` | `20000` | 压完之后**至少保留**的近期内容 |

非法值(非正整数)一律**回落到默认**,不报错 —— 一个写错的数字不该让会话跑不起来。

## 2. 压什么、留什么:切点规则

从**最新往回**累加 token,累到 `keepRecentTokens` 时把切点放在**不早于**该位置的最近一个合法切点。
合法切点是:

- `user` / `assistant` 消息
- `compaction` / `branch_summary` entry

**绝不在工具结果上切** —— 工具结果必须跟在它的调用后面,切在那里会让模型看到一次没有调用的结果。

配套的一个细节:切点会**向前吞掉相邻的不进上下文的 entry**(旧 `dispatch`、叙述、思考),让切点更贴近
预算。但工具结果虽然也不进上下文,**不属于可吞的元数据** —— 吞掉它等于把切点挪到工具结果上。

### 单个 turn 就超预算:split turn

如果切点落在一个 turn 中间(前面还有 `user` 消息),说明**这一个回合本身就超了预算**。这时做
**split turn**(pi 同款):

1. 历史部分照常摘要;
2. 这个 turn 的**前缀**(从 turn 起点到切点)单独摘要一次;
3. 两段用 `**Turn Context (split turn):**` 分隔后拼成一条摘要。

好处是模型不会看到"半个回合"——要么整段都在摘要里,要么整段都还在上下文里。

## 3. 摘要长什么样

固定 7 段的结构化格式(`SUMMARIZATION_PROMPT`):`Goal` · `Constraints & Preferences` · `Progress`
(`Done`/`In Progress`/`Blocked`)· `Key Decisions` · `Next Steps` · `Critical Context`。

提示词里两条硬要求:**保持每段简洁**、**原样保留文件路径 / 函数名 / 错误信息**。

**第二次之后的压缩用 `UPDATE_SUMMARIZATION_PROMPT`**:它要求保留旧摘要里的全部信息,只增改
(把 `In Progress` 里完成的挪进 `Done`、更新 `Next Steps`)。否则每次压缩都会把更早的决策洗掉 ——
长会话跑到第十次压缩时,模型就不知道最初的目标了。

摘要调用本身也是**一次普通 LLM 调用**,但:

- 用 `system` 提示词明确禁止"接着对话"(否则模型会把摘要写成回答);
- 会话内容用 `<conversation>` 包裹、旧摘要用 `<previous-summary>` 包裹;
- **模型试图调用工具就直接报错**(`RuntimeError`):摘要请求必须是一次纯文本输出。

## 4. 摘要怎么回到上下文

`_history()` 的装配规则(pi 语义):

```text
[system 提示词]
[上下文已压缩] <summary> [摘要结束,以下是其后的消息]     ← 用 user 角色,不是 system
从 firstKeptEntryId 开始的消息 …
沿途的 branch_summary(用「另一条分支的摘要」标签)
```

三个刻意的选择:

1. **摘要包成 `user` 消息而不是 `system`**:各 provider 对"多个 system"支持不一,而摘要本来就是
   "过去那段对话"的替身。
2. **切点由落盘的 `firstKeptEntryId` 决定,不由摘要文本决定**。摘要只负责"讲清楚",保留范围是数据,
   所以压缩后不会因为模型摘要写得长短而改变窗口。
3. **没压过时退回「最近 40 条」的简易窗口**。那是压缩接管窗口之前的旧行为;一旦有过压缩,窗口就
   完全由摘要 + 切点决定。

## 5. 落盘的 entry

```jsonc
// 压缩
{"type":"compaction","summary":"…","firstKeptEntryId":"a1b2c3d4e5f6",
 "tokensBefore":41230,"usage":{…}}

// 分支摘要
{"type":"branch_summary","summary":"…","fromId":"…","usage":{…}}
```

`tokensBefore` 是**压缩前**的估算值(前端用它显示"从多少压到多少")。字段全集见
[session-format.md](session-format.md) §5。

`compact_session()` 在**没什么可压时返回 `None`**,调用方据此提示用户,而不是硬写一条空摘要。
同一分支上已经压过一次、之后没有任何新内容时也返回 `None`(避免连点两次 `/compact` 压出两条)。

## 6. 分支摘要:`/tree` 跳分支时

`/tree` 跳到旧节点继续时,原来那条分支上的内容会离开上下文。为了让"切回来"之后不丢,qi 把
**被放弃的那段**压成一条 `branch_summary`,挂到跳过去的位置下:

- 要摘要的范围 = 从 `from_id` **沿 parentId 往回走到共同祖先(不含祖先)**;
- 只收"会进上下文"的 entry(`is_context_entry`:消息、压缩、分支摘要);
- **必须在移动 `position` 之前**取好源分支(移完再取拿到的就是目标分支,摘要会静默变成空);
- 先 `set_position(目标)` 再 `append(摘要)`,于是新 leaf 就是这条摘要。

## 7. 失败与边界

| 情况 | 行为 |
| --- | --- |
| 自动压缩失败(模型报错/超时) | **不卡住这一轮**:发一个 error 事件后继续跑。压缩是优化,不是前置条件 |
| 没什么可压 | `compact_session()` 返回 `None`,前端提示"没有可压缩的内容" |
| 老 entry 没有 `id` | 不给 id 就没法被 `firstKeptEntryId` 引用 → 直接不压(不猜) |
| 摘要模型试图调工具 | 抛错(见 §3) |
| `settings.compaction` 值非法 | 回落到默认,不报错 |

## 8. 与 pi 的对应

| | pi | qi |
| --- | --- | --- |
| 阈值公式 | `contextWindow - reserveTokens` | 同 |
| 默认值 | `reserve 16384` / `keepRecent 20000` | 同 |
| 切点 | turn 边界或 assistant,**不在 tool 上** | 同 |
| split turn | 有 | 同 |
| 摘要格式 | 同一套 7 段结构 | 同(提示词直接移植) |
| 摘要进上下文的方式 | `system \| summary \| kept 消息` | 同位置,但摘要用 **user** 角色(理由见 §4) |
| `custom`(叙述/思考) | 无此类型 | **压缩时当它不可见** —— 因为 `_history()` 本来就不读它,保持一致,不额外发明规则 |

## 9. 扩展能干预压缩吗

能,三个事件(见 [extensions.md](extensions.md) §3.1)。压缩**只有一个入口**
(`QiRuntime.compact_session`:TUI 的 `/compact` 与自动压缩都走它),所以三个事件都在那一处发:

| 事件 | 时机 | 契约 |
| --- | --- | --- |
| `session_before_compact` | 已经算出怎么切、还没调模型 | `{cancel: true}` 拦下这次压缩;或 `{summary: "…"}` 自带摘要 —— **自带时不调模型**(扩展可能比模型更清楚该记住什么) |
| `session_compact` | 成功落盘后 | `{entry, summary, provided}`(`provided=true` 表示摘要是扩展给的) |
| `session_compact_failed` | 摘要调用/落盘失败 | `{error}` —— **先发事件,再把异常抛给调用方**(失败必须传出去,但扩展也该看得见) |

handler 抛异常不影响压缩本身:记一条 note 后继续(与其它事件同一条规矩)。
