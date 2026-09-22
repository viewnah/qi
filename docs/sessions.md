# 会话

会话是 qi 的工作单元:一次对话的历史、分支、工具往返、用量都在里面。**一条会话 = 一个 JSONL
文件**,格式与字段见 [session-format.md](session-format.md);本文讲怎么用它。

## 1. 存在哪

```text
~/.qi/agent/sessions/<YYYYmmddTHHMMSS>_<12位id>.jsonl
```

- **全局一份,不按项目分目录。** 按项目分组靠 header 里的 `cwd`(见 §4)。
- 目录可被 `settings.json` 的 `sessionDir` 覆盖。
- 会话 id = header 的 `id`,也是文件名的一部分。

## 2. 常用操作

| 目的 | 命令 |
| --- | --- |
| 新开一条(默认行为) | `qi` 或 `qi "第一个问题"` |
| 接着最近一条继续 | `qi -c` / `qi --continue` |
| 指定某条会话 | `qi --session <路径 / id 或前缀>` |
| 用精确 id(不存在则建) | `qi --session-id <id>` |
| 浏览并选一条恢复 | `qi -r` / `qi --resume`(**需要 TTY**;与 TUI 的 `/resume` 同一条路) |
| 从某条会话分叉出新会话 | `qi --fork <路径 / id 或前缀>` |
| 换会话目录 | `qi --session-dir <目录>`(优先于 `settings.sessionDir`) |
| 给会话起名 | `qi -n "名字"`(或 `--name`) |
| 不落盘(临时试一把) | `qi --no-session` |
| 导出成 JSONL 文件 | `qi --export out.jsonl`(可配 `--session`;省略则导出最近一条) |
| 列出 / 查看 / 重命名 / 删除 | TUI 的 `/resume` 选择器(`qi -r` 直接进)—— 与 pi 同一种分工,它也没有 `pi sessions` |

**`<id 或前缀>` 两种前缀都认**:header 的 `id` 前缀,以及文件名 stem 前缀(即含
`<时间戳>_` 的那一截)。**也接受文件路径** —— `qi --session ./some.jsonl` 会直接读那个文件。
`qi -c` 取的是**按文件修改时间**最新的一条,不是按 header 的 `created_at`。

## 3. 一条会话的边界

- `--no-session` 的会话**不落盘**:只在内存里跑完这一轮,不产生文件(拿它做一次性脚本很合适)。
- `qi --export` 是**导出后退出**:不会跑模型,只是把现有会话原样拷成一个可带走的 JSONL。
- 删除会**直接 unlink 文件**,不可恢复(网页端删工作区时是同一语义:目录不动,只删会话文件)。

## 4. 工作目录与按项目分组

会话在被创建时把**工作目录**(绝对路径)写进 header 的 `cwd`。这是恢复时选对目录、以及前端
按项目分组列表的依据。

老会话(早期版本)的 header 里没有 `cwd`:首次被使用时由 `ensure_cwd()` **回填**并整文件重写一次,
此后不再进这个分支。回填时若 header 不合法(空文件 / 类型不对)就**不猜**,直接跳过 ——
给一个历史会话硬塞一个错误的工作目录,比留着空更糟。

## 5. 用量统计

TUI 里回放会话、以及网页端显示的用量,都来自 `session.py` 的 `usage_summary()`,它**读当前分支**
计算。口径如下:

| 字段 | 含义 |
| --- | --- |
| `turns` | 用户轮数 = 当前分支上 `role=user` 的消息条数 |
| `tools` / `tool_failures` | 工具往返次数,以及其中 `status=error` 的次数 |
| `steps` / `llm_calls` | 累计值,**只统计带 `usage` 的助手消息** |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | 累计值,同上 |
| `context_tokens` | **最后一条**带该键的轮的值 —— 即"现在上下文里装着多少",**不是各轮相加** |

三条容易误解的地方,都是刻意的:

1. **`context_tokens` 不求和。** 它是那一轮最后一次 LLM 调用看到的 prompt 大小。把各轮相加会
   得到一个随轮数虚增的天文数字。
2. **没有 usage 就不算 token。** 写入 usage 之前的老会话、以及被硬取消的轮只计入 `turns`,
   token 记 0 而**不补估值** —— 没有就是没有。
3. **求和在后端做。** 前端一次只拿得到一个窗口(会话接口是分页的),长会话窗口前面还有几千条,
   前端求和会**静默少算**。后端手上有整条分支,所以由它算。

`total_tokens` 在 provider 没给时用 `prompt + completion` 兜底(好过显示 0)。

## 6. 分支、`/tree` 与 fork

同一个文件里可以并存多条分支:**回到旧节点继续提问**,新内容成为它的子节点,旧分支原样保留。
机制(节点、`parentId`、`position`)见 [session-format.md](session-format.md) §4。

- **TUI 里 `/tree`** 跳到任意节点:此后提问挂在那个节点下,形成新分支。
- **跳分支时**,被放弃的那段会被压成一条 `branch_summary` 挂到新位置 —— 这样"跳回来"之后模型
  仍知道另一条路上发生过什么,而不是凭空断掉。
- **`qi --fork <id>`** 把「源会话当前分支」**复制成一个新文件**,不是在同一文件里再开分支。
  复制过去的 entry 保留原 `id`/`parentId`,链在新文件里自洽。

## 6.1 模型与思考级别跟着会话走

会话文件里记着**模型是什么时候被换掉的**(`model_change` / `thinking_level_change`,
见 [session-format.md](session-format.md) §6.1),所以:

- **续接会话(`-c` / `--session` / `/resume`)会恢复那次切换** —— 上次切到 glm,再打开还是 glm,
  而不是回 `settings.json` 的默认模型。这来自 pi 的 `restoredModel` / `getSessionContextSettings`。
- **还原前先看凭证**:记的那个 provider 已经没密钥(退出登录 / 环境变量没了)时,退回默认并
  在启动提示里说明 —— 不会拿着一个注定 401 的模型继续跑。
- **CLI 显式给的赢**:`--model` / `--thinking`(含 `--model provider/id:级别`)是当次覆盖,
  不会被会话里的旧值顶掉。
- **换模型与换级别在界面上照旧是即时生效的**(下一回合用到请求里);落 entry 是为了"下次打开"
  与"回放里看得出切换点",不影响本回合行为。
- **界面显示的与发出去的一致**:TUI 的 footer 缓存(`_model` / `_thinking_level`)在选会话后
  会跟 runtime 真在用的那一份**同步**;启动路径尤其要注意 —— 绑定必须是同步的,不能只依赖
  `session_start` 那个 async worker(见 [session-format.md](session-format.md) §6.1)。

## 7. 自动命名

会话默认叫「未命名」。左栏一列「未命名」等于没有左栏 —— 分不清哪条是哪条。所以 qi 会用
**模型**给会话起一个短标题。机制如下(全在 `Runtime._stream_inner`):

| 项 | 行为 |
| --- | --- |
| 触发条件 | **这个会话还没有标题**(`not session.title.strip()`)—— 新建的、以及本功能上线前建的老会话都算 |
| 输入 | 会话**原本的第一句话**,不是这一轮说的话。否则老会话续聊时,一句「接着再补个测试」会把一个讲仓库结构的会话命名成「补充测试」 |
| 时机 | 与回合**并行**起(`asyncio.create_task`)—— **不拖首字延迟**;回合末尾才套用标题 |
| 被取消时 | 硬取消时连标题任务一起取消 —— 不能把它漏在后台,它还会往会话里写 |

标题的读写都走 `SessionStore.set_title()`:它同时改内存、header entry、磁盘,**写盘失败还会把
内存回滚**。只改一处会出现"列表里是新名、重开又变回旧的",而那种不一致最难查。

手动改名会发 `session_info_changed` 事件,`source` 区分 `user`(`/name`)与 `auto`(自动命名)——
前端可以据此决定要不要覆盖用户已经改过的名字。

## 8. 与 pi 的对应

| | pi | qi |
| --- | --- | --- |
| 文件位置 | `~/.pi/agent/sessions/` | `~/.qi/agent/sessions/`(同构;qi 多一个 `sessionDir` 覆盖) |
| 会话格式 | JSONL + 树(`id`/`parentId`) | 同构,见 [session-format.md](session-format.md) |
| 续接 / 指定 / 分叉 | `-c` / `--session` / `--fork` | 同名同义 |
| 分支交互 | `/tree` | `/tree` + 跳分支自动写 `branch_summary` |
| 模型/级别随会话 | `model_change` / `thinking_level_change` entry,续接时还原 | 同构(键名 `model_id`,见 [session-format.md](session-format.md) §6.1);还原前多一道凭证检查 |

qi 在这一层**刻意与 pi 保持一致**:字段名、命令名、`/tree` 语义都对齐,便于两边共享同一套认知。
差异集中在 qi 多出来的部分(扩展自定义 entry、`branch_summary` 的自动生成)。
