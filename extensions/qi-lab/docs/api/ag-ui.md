# 运行入口:`POST /api/ag-ui`

**一次 POST 就是一次运行**:请求体是 AG-UI 的 `RunAgentInput`,响应是 `text/event-stream`,
流就是这次响应本身。通用约定见 [`README.md`](README.md)。

```
POST /api/ag-ui
Accept: text/event-stream
Content-Type: application/json
```

带 `guard`(Host + 口令);**不需要**确认头(它不写配置,只写会话文件)。

## 为什么只有一个端点

旧设计是三件套(`POST /turn` 202 + `POST /cancel` + `GET /events` 两跳),**已整体移除**:
按官方编码器 `@ag-ui/encoder@0.0.59` 的 `encodeSSE`,流就是这次响应,于是:

- `?run_id=` 重放已完成轮 → 没有"晚连上来"的客户端;
- `?from=` 断线续传 → 没有可续的流;AG-UI 的答案本就是"**重发一个 RunAgentInput**";
- `id:` 游标 → 没有消费者(实测原生 `EventSource` 会回传 `Last-Event-ID`,而 qi 不读它);
- 取消 → **没有端点**:客户端 abort 请求 → ASGI 取消生成器 → `runtime.stream()` 被打断。

## 请求体 `RunAgentInput`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `threadId` | `string` | **是** | = qi 的**会话 id**。空 → 422;不存在 → 404。qi 的 fork 会新建会话文件,所以每个分支天然是一条独立 thread |
| `runId` | `string` | 否 | 不给则宿主生成 12 位 hex;原样回显在 `RUN_STARTED` 里 |
| `messages` | `object[]` | **是** | 取**最后一条 `role:"user"`** 作为本轮输入。空/没有 user 内容 → 422 |
| `forwardedProps` | `object` | 否 | qi 的逃生舱:`agent`(钉住角色)与 `session_id`(只做 cross-check,不覆盖 `threadId`) |
| `state` / `tools` | any | 否 | 收下不报错(AG-UI 自带字段) |
| `context` / `parentRunId` / `resume` | any | 否 | 认识但**不用**(字段表还在长,严格校验会让新客户端直接 422) |
| 其余键 | any | 否 | 收进 `extra`,不报错 |

`messages[].content` 可以是字符串,也可以是 AG-UI 的 parts 数组(只取 `type` 为
`"text"` 或缺省的那部分文本)。

**为什么只取最后一条 user**:`runtime.stream()` 只接受一段文本,而上下文是宿主自己
从会话 JSONL 取的 —— 把 `messages` 全灌进去会让本轮输入进两次。

```json
{
  "threadId": "<会话 id>",
  "runId": "run-1",
  "messages": [{"role": "user", "content": "跑一下测试"}],
  "forwardedProps": {"agent": "tester"}
}
```

## 响应:`text/event-stream`

**帧格式(逐字节对齐官方编码器)**:

```
data: {"type":"RUN_STARTED","timestamp":1730000000000,...}\n\n
```

- **只有 `data:` 行,没有 `event:`,没有 `id:`。** 事件类型在 JSON 的 `type` 里 ——
  所以消费方**不能** `EventSource.addEventListener("TEXT_MESSAGE_CONTENT")`,
  只能 `onmessage` + 解析 `type`;
- `ensure_ascii=False`:中文不转义(省带宽),JSON 保证单行,不会拆成多个 `data:` 行。

响应头:`Cache-Control: no-cache, no-transform`、`X-Accel-Buffering: no`、`Connection: keep-alive`。

### 流的顺序

1. `RUN_STARTED`(第一帧,带 `threadId` / `runId`)
2. `MESSAGES_SNAPSHOT` —— 历史里的对话(`role`/`content` 只有 `type:"message"` 那一类)
3. `STATE_SNAPSHOT` —— `{cwd, title}`(AG-UI 不强制快照,但 qi 的 UI 靠它重建整屏)
4. `CUSTOM` name=`qi.history` —— **qi 自己的完整 entry 列表**(分派/工具/叙述不在 messages 里,
   不给这一份,刷新后直播与回放就不一致了)
5. 本轮的运行时事件(见下表),正常收尾 `RUN_FINISHED`

在第 4 步之后、开跑之前,宿主还会做两件事(所以它们**在流开始后**才生效):
`bind_session()`(绑定会话:换模型/级别的落盘 + 续会话还原)与 `pin_role(agent)`。

### 事件表

| `type` | 什么时候 | 关键字段 |
| --- | --- | --- |
| `RUN_STARTED` | 流的第一帧 | `threadId`, `runId` |
| `MESSAGES_SNAPSHOT` | 紧接着的历史 | `messages[]` |
| `STATE_SNAPSHOT` | 紧接着的状态 | `snapshot: {cwd, title}` |
| `CUSTOM` | qi 扩展点(严格 AG-UI 客户端会忽略它,不报错) | `name` + `value`,见下 |
| `TEXT_MESSAGE_START` / `TEXT_MESSAGE_CONTENT` / `TEXT_MESSAGE_END` | 助手正文的三段式 | `messageId`, `delta` |
| `REASONING_MESSAGE_START` / `_CONTENT` / `_END` | 思考过程的三段式 | `messageId`, `delta` |
| `REASONING_START` / `REASONING_END` | 思考块边界 | |
| `TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_END` | 工具调用三段式。qi 在 `tool_start` 时已有**完整**参数,所以 Args 一次发完 | `toolCallId`, `toolCallName`, `delta` |
| `TOOL_CALL_RESULT` | 工具结果 | `toolCallId`, `content`, `metadata["qi.tool"]` |
| `RUN_FINISHED` | 正常结束 | `outcome:{type:"success"}`, `result`, `metadata["qi.usage"]` |
| `RUN_ERROR` | 出错(**不断连**,帧内报错) | `message`, `code` |
| `RAW` | 未知事件类型按 AG-UI 的 Raw 透传(不静默丢弃) | `event`, `data` |

`CUSTOM` 的 `name`:

| name | 含义 |
| --- | --- |
| `qi.history` | 完整 entry 列表(历史回放的权威来源) |
| `qi.dispatch` | 分派决策(`text` / `agent`) |
| `qi.opening` | agent 开场白(不进 LLM 上下文)+ `suggestions` |
| `qi.narration` | 这段文本是"过程叙述"还是"最终回答"(`tool_calls` / `is_final` / `thinking`) |
| `qi.compaction` | 压缩的 `start` / `end` 阶段 |
| `qi.branch` | 分支变化 |

### 取消与错误

- **取消**:客户端断开(Stop 按钮 / abort)→ 生成器收到 `CancelledError` →
  补发收尾帧(能发就发)→ **re-raise**(不能吞:要让 ASGI 看见取消,否则连接不释放)。
- **运行出错**:一律转成一帧 `RUN_ERROR`,**不断开连接**;
  `finally` 里 `web.end(session.id)` —— 不放这儿,一次异常就把会话永久卡在"运行中"。

### 状态码(开流之前)

| 码 | 什么时候 |
| --- | --- |
| 422 | body 不是对象 / `threadId` 为空 / `messages` 里没有 user 内容 |
| 404 | `threadId` 不是存在的会话 |
| 409 | 该会话已有活跃 run(会话级串行:两个 POST 不得往同一文件追加) |
| 401 / 400 | 口令 / Host(见通用约定) |

```bash
# 最小可跑示例(会话得先存在)
curl -N http://127.0.0.1:3456/api/ag-ui \
  -H 'Content-Type: application/json' \
  -d "{\"threadId\":\"$SID\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

---

## 配套:角色钉住

`forwardedProps.agent` 走 `serve.pin_role()` → qi-agents 的 `before_agent_start`,
把该角色的正文拼进本轮系统提示词。

- **不**走 `stream(agent_override=…)`:那是分派器还在时的接口,core 收窄成单 agent 之后
  流水线里没人读它 —— 传了不报错、只是没生效,那是个会说谎的控件;
- **项目级角色**(仓库控制的提示词)在项目未信任时**不注入**,与 `--ext agent=` 同一道闸;
- 粒度是 runtime(按 cwd 缓存):同一 cwd 下多个浏览器客户端互相覆盖,取最后一次 ——
  与该功能"不落盘、刷新回 auto"的口径一致。
