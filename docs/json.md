# JSON 事件流模式

`qi --mode json "问题"` 不走 TUI,而是把**每个事件**作为一个 JSON 对象打到 **stdout**,一行一个
(JSONL)。给脚本、管道、上游服务消费用。

```bash
qi --mode json "总结这个仓库" | jq -r 'select(.kind=="text") | .text'
```

- **`--mode json` 隐含无头**:即使不加 `-p` 也不进 TUI(判定见 `cli.py` 的 `interactive`)。
- **stdout 只有事件流**。启动提示、警告、`--verbose` 的进度都走 **stderr** —— 所以 `| jq` 不用
  过滤杂音。(`--verbose` 对这条流没有影响:JSON 模式本就输出全部事件。)
- **一个事件一行**。这是刻意的:多行美化会让消费端无法按行切分。契约由
  `tests/test_cli_run.py::test_mode_json_emits_one_object_per_line` 钉住。

## 1. 每个对象的形状

固定 5 个键,顺序不保证,解析请按名取:

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | string | 事件类型(见 §2) |
| `agent` | string \| null | 产出该事件的运行单元名(core 默认 `general`,装了 qi-agents 后可能是角色名) |
| `tool` | string \| null | 仅工具事件有值 |
| `text` | string | 该事件的主文本;没有则为 `""` |
| `data` | object | 结构化补充信息;没有则为 `{}` |

```jsonc
{"kind": "agent_start", "agent": "general", "tool": null, "text": "", "data": {}}
{"kind": "text_delta", "agent": "general", "tool": null, "text": "问题在", "data": {}}
{"kind": "tool_start", "agent": "general", "tool": "read", "text": "", "data": {"args": {"path": "src/parse.py"}}}
{"kind": "tool_end", "agent": "general", "tool": "read", "text": "…文件内容…", "data": {"status": "ok", "duration_ms": 12, "exit_code": null, "error": null}}
{"kind": "text", "agent": "general", "tool": null, "text": "问题在偏移量计算。", "data": {}}
{"kind": "agent_end", "agent": "general", "tool": null, "text": "问题在偏移量计算。", "data": {"messages": [...], "usage": {"prompt_tokens": 3120, "completion_tokens": 140, "total_tokens": 3260, "llm_calls": 2, "turns": 1}}}
```

## 2. 事件类型

**core 今天会发的事件**(发射点在 `runner.py` / `runtime.py`):

| `kind` | 时机 | 关键 `data` 键 |
| --- | --- | --- |
| `agent_start` | 回合开始 | — |
| `text_delta` | 逐字增量(打字机效果) | — |
| `thinking_delta` | 思维链增量 | — |
| `assistant_message` | 每轮 LLM 回复完成一次 | `{"step": int, "tool_calls": [名字]}` |
| `tool_start` | 工具调用前 | `{"args": {…}}` |
| `tool_end` | 工具调用后 | `{"status": "ok"\|"error", "duration_ms": int, "exit_code": int\|null, "error": str\|null}` |
| `text` | 本回合**最终回答**(整段) | — |
| `agent_end` | 回合结束 | `{"messages": [...], "usage": {…}}` |
| `error` | 回合内的失败(模型拒答、工具异常等) | — |
| `compaction_start` / `compaction_end` | 自动压缩前后 | `{"auto": true, "tokens": int, …}` / `{"auto": true, "tokensBefore": int, …}` |

**声明了但 core 已不再发射**(`AgentEvent` 的注释里还留着,消费者仍会看到旧会话相关的处理代码):

| `kind` | 状态 |
| --- | --- |
| `dispatch` | v3 取消 auto 分派后不再产生。`cli.py` / `tui.py` / qi-web 仍**读取**它以便旧会话回放 |
| `opening` | 角色的开场白已改由 qi-agents 用自定义 entry 表达 |

> **扩展之间的事件(`api.events`)不在这条流里。** 那是扩展对扩展的频道,与这里的宿主事件是
> 两套 API(见 [extensions.md](extensions.md) §3.2)。

## 3. 两个必须知道的坑

### `text_delta` 和 `text` 都会出现 —— 别把两者相加

`text_delta` 是逐字增量,`text` 是本回合的**完整回答**。同一条流里两者都有,所以:

- 想要打字机效果 → 只读 `text_delta`;
- 想要"这一轮的答案" → **只读 `text`**(CLI 与 TUI 就是这么做的);
- 两者混着累加会得到重复内容。

### 顺序就是因果顺序,不要重排

一轮里 `text_delta*` 与 `tool_start`/`tool_end` 按**真实发生顺序**穿插(工具调用之间的叙述不会被
丢弃或后置)。把所有文本提到开头、把工具行推到末尾会失去因果 —— 那就等于把这条流降级成
"两堆东西"。

## 4. 消费示例

```bash
# 只要最终答案(忽略增量与工具)
qi --mode json "解释 src/parse.py" | jq -r 'select(.kind=="text") | .text'

# 跟踪工具调用(名字 + 状态 + 耗时)
qi --mode json "跑一下测试" | jq -r 'select(.kind=="tool_end") | "\(.tool) \(.data.status) \(.data.duration_ms)ms"'

# 取用量
qi --mode json "你好" | jq -c 'select(.kind=="agent_end") | .data.usage'
```

```python
# Python:一次一行读,不必缓冲(JSONL 的用处就在这)
import json, subprocess
proc = subprocess.Popen(["qi", "--mode", "json", "总结这个仓库"],
                        stdout=subprocess.PIPE, text=True)
for line in proc.stdout:
    event = json.loads(line)
    if event["kind"] == "text":
        print(event["text"])
```

## 5. 稳定性

**可以依赖**:5 个键的名字 · §2 里"今天会发"的事件类型与它们的 `data` 键 · 一事件一行 · 因果顺序。

**不要依赖**:键的输出顺序 · 未知 `kind` 的存在与否(新事件会加进来)· `data` 里字段是否齐全
(不同 provider 给的信息不同)· 同一条流里"某类事件恰好出现几次"。

**读到不认识的 `kind` 时跳过即可,不要报错** —— 这与 qi 各处一致的原则相同(旧会话里也有已废弃
的类型,新版本仍要能回放)。

## 6. 与 pi 的对应

| | pi | qi |
| --- | --- | --- |
| 形状 | 事件流(JSONL) | 同 |
| 用途 | 脚本 / 上游服务 | 同 |
| stdout 纯净 | 是 | 是(提示走 stderr) |
| 事件名 | pi 的命名 | qi 自己的(见 §2);`dispatch`/`opening` 是 qi 遗留 |

**与 pi 的差别集中在事件清单本身**:qi 有 `compaction_start`/`compaction_end`、`thinking_delta`
这类 pi 没有(或命名不同)的事件,`dispatch`/`opening` 则是 qi 去掉 auto 分派后留下的兼容读取面。
消费端应当**按 §2 的表实现,并对未知 `kind` 容忍**,而不是照 pi 的清单写死。
