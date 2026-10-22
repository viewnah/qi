"""AG-UI 协议适配层:把 qi 的 `AgentEvent` 翻译成 AG-UI 事件,并按官方编码器组帧。

参照物是**官方编码器**(不是文档转述):`@ag-ui/encoder@0.0.59`(发布自
`github.com/ag-ui-protocol/ag-ui`):

    encodeSSE(event) { return `data: ${JSON.stringify(event)}\\n\\n`; }
    getContentType() { return "text/event-stream"; }   // 或 protobuf

三条由此确定的硬约束:

1. **只有 `data:` 行,没有 `event:`、没有 `id:`。** 事件类型在 JSON 体的 `type` 里。
   所以消费方不能用 `EventSource.addEventListener("TEXT_MESSAGE_CONTENT")`,
   只能用 `onmessage` + 解析 `type`。qi 自己的前端也因此要从"读 event:"改成"读 type"。
2. **`RunAgentInput.messages` 带完整历史** —— AG-UI 天然是"客户端拥有会话"的模型,
   与 qi 的「session = 本地 JSONL 树」一致,不冲突。所以断线重连的语义在 AG-UI 里
   就是"重发一个 RunAgentInput",而不是 `id:`/`Last-Event-ID` 续传。
3. **qi 独有概念走 `Custom{name, value}`**(AG-UI 的协议级扩展点),不污染标准事件。

本模块**只做映射,不做 IO**:`AguiTranslator.feed()` 是纯函数式的状态机(持有"当前
文本/思考消息开没开"这点状态),可以脱离 FastAPI 单独测。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from qi_agent.models import AgentEvent

from . import activity

# ── 组帧(与官方 encodeSSE 逐字节一致)────────────────────────────────


def encode(event: dict) -> str:
    """一个事件 → 一个 SSE 帧。官方编码器就是 `data: ${JSON.stringify(e)}\\n\\n`。

    `ensure_ascii=False` 是 qi 的既有选择(中文不转义,省带宽);JSON 本身保证单行,
    所以不会出现需要拆成多个 `data:` 行的情况。
    """
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ── 输入模型(`RunAgentInput`)─────────────────────────────────────────
#
# 只声明 qi 真正会读的字段,其余(paremtRunId/context/resume/…)用 extra 收下不报错 ——
# AG-UI 的字段表还在长(0.0.59),严格校验会让新客户端直接 422。


@dataclass
class AguiMessage:
    """`RunAgentInput.messages` 的一项。只取 qi 需要的 role/content。"""

    role: str = "user"
    content: Any = ""
    id: str | None = None

    @classmethod
    def parse(cls, raw: Any) -> "AguiMessage | None":
        if not isinstance(raw, dict):
            return None
        role = str(raw.get("role") or "user")
        content = raw.get("content", "")
        # AG-UI 允许 content 是 parts 数组;qi 只取文本部分
        if isinstance(content, list):
            content = "".join(
                str(p.get("text", "")) for p in content
                if isinstance(p, dict) and p.get("type") in (None, "text")
            )
        return cls(role=role, content=content, id=raw.get("id"))


@dataclass
class RunAgentInput:
    """AG-UI 的一次运行输入(POST 的 body)。

    `forwardedProps` 是 AG-UI 自带的逃生舱,qi 用它传自己的两个旋钮:`agent`(点名)
    与 `session_id`(`threadId` 已表达会话,这里只作为 cross-check,不覆盖)。
    """

    thread_id: str = ""
    run_id: str = ""
    messages: list[AguiMessage] = field(default_factory=list)
    state: Any = None
    tools: list[Any] = field(default_factory=list)
    forwarded: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> "RunAgentInput":
        if not isinstance(raw, dict):
            raise ValueError("RunAgentInput 必须是 JSON 对象")
        forwarded = raw.get("forwardedProps")
        known = {"threadId", "runId", "messages", "state", "tools", "context",
                 "forwardedProps", "parentRunId", "resume"}
        msgs = [m for m in (AguiMessage.parse(x) for x in (raw.get("messages") or [])) if m]
        return cls(
            thread_id=str(raw.get("threadId") or ""),
            run_id=str(raw.get("runId") or ""),
            messages=msgs,
            state=raw.get("state"),
            tools=list(raw.get("tools") or []),
            forwarded=forwarded if isinstance(forwarded, dict) else {},
            extra={k: v for k, v in raw.items() if k not in known},
        )

    @property
    def agent(self) -> str | None:
        v = self.forwarded.get("agent")
        return str(v) if isinstance(v, str) and v else None

    @property
    def kb_ids(self) -> list[str]:
        """会话级知识库选择（可选）。空列表 = 用全局/项目绑定。"""
        v = self.forwarded.get("kbIds") or self.forwarded.get("kb_ids")
        if not isinstance(v, list):
            return []
        return [str(item) for item in v if str(item).strip()]

    @property
    def kb_provider(self) -> str | None:
        v = self.forwarded.get("kbProvider") or self.forwarded.get("kb_provider")
        return str(v) if isinstance(v, str) and v else None

    def last_user_text(self) -> str:
        """取最后一条 user 消息作为本轮输入。

        qi 的 `runtime.stream()` 只接受一段文本,而后端**自己**从会话 JSONL 取上下文
        (`_history()`)。所以这里只用 `messages` 的**最后一条 user**(而不是把它们
        全灌进上下文)——否则本轮输入会进两次。这与 AG-UI 不矛盾:AG-UI 允许 agent
        自己决定如何使用 `messages`。
        """
        for m in reversed(self.messages):
            if m.role == "user":
                return str(m.content)
        return ""


# ── 映射状态机 ────────────────────────────────────────────────────────


@dataclass
class _OpenMessage:
    """一个已 `Start` 未 `End` 的流式消息(文本或思考)。"""

    message_id: str
    kind: str          # "text" | "reasoning"


class AguiTranslator:
    """qi `AgentEvent` → AG-UI 事件序列。

    有状态,因为 AG-UI 的 `TextMessageStart → Content* → End` 是一个**三段式**,
    而 qi 只发无归属的 `text_delta`。所以这里要:
      * 为每条流式消息**合成 `messageId`**;
      * 在流的边界(下一个非同类事件、工具调用、run 结束)自动补 `End`。

    `toolCallId` 同理:qi 的 `tool_start`/`tool_end` 靠 `tool` 名字配对(同一轮内同名工具
    串行,不并发),这里合成一个自增 id 并记住,供 `tool_end` 复用。
    """

    def __init__(self, thread_id: str, run_id: str):
        self.thread_id = thread_id
        self.run_id = run_id
        self._n = 0
        self._open: _OpenMessage | None = None
        self._pending_tool: tuple[str, str, dict] | None = None   # (toolCallId, name, args)
        self._message_id: str | None = None                       # 当前 assistant 消息

    # ── 内部工具 ──
    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n}"

    def _base(self, type_: str) -> dict:
        return {"type": type_, "timestamp": _now_ms()}

    def _close_open(self) -> list[dict]:
        """把当前开着的流式消息收尾(必要时补 End)。"""
        if self._open is None:
            return []
        opened, self._open = self._open, None
        if opened.kind == "reasoning":
            return [
                {**self._base("REASONING_MESSAGE_END"), "messageId": opened.message_id},
                {**self._base("REASONING_END"), "messageId": opened.message_id},
            ]
        return [{**self._base("TEXT_MESSAGE_END"), "messageId": opened.message_id}]

    def _open_text(self) -> list[dict]:
        """开一个文本消息的 Start(不改内容)。"""
        self._message_id = self._next("msg")
        self._open = _OpenMessage(self._message_id, "text")
        return [
            {**self._base("TEXT_MESSAGE_START"),
             "messageId": self._message_id, "role": "assistant"},
        ]

    def _emit_text(self, delta: str) -> list[dict]:
        """确保有一个开着的文本消息,并把这块内容发出去。

        刻意不写 `assert self._open is not None`:assert 在 `python -O` 下会被剥掉,
        那种"只在优化模式下才坏"的写法不值得。这里把 id 取进局部变量,
        类型窄化与运行期安全一次解决。
        """
        out: list[dict] = []
        if self._open is not None and self._open.kind == "text":
            mid = self._open.message_id
        else:
            out += self._close_open()
            out += self._open_text()
            mid = self._message_id or ""
        out.append({**self._base("TEXT_MESSAGE_CONTENT"),
                    "messageId": mid, "delta": delta})
        return out

    def _emit_reasoning(self, delta: str) -> list[dict]:
        """思考内容:AG-UI 的 `ReasoningStart → MessageStart → Content* → MessageEnd → End`。

        注意:`THINKING_*` 在 AG-UI 里**已弃用**,替代是 `REASONING_*`(官方迁移表写明的),
        所以 qi 的 `thinking_delta` 映射到后者。
        """
        out: list[dict] = []
        if self._open is not None and self._open.kind == "reasoning":
            mid = self._open.message_id
        else:
            out += self._close_open()
            mid = self._next("rsn")
            self._open = _OpenMessage(mid, "reasoning")
            out.append({**self._base("REASONING_START"), "messageId": mid})
            out.append({**self._base("REASONING_MESSAGE_START"),
                        "messageId": mid, "role": "reasoning"})
        out.append({**self._base("REASONING_MESSAGE_CONTENT"),
                    "messageId": mid, "delta": delta})
        return out

    # ── 入口 ──
    def start(self) -> list[dict]:
        """流的第一帧:`RUN_STARTED`。"""
        return [{**self._base("RUN_STARTED"),
                 "threadId": self.thread_id, "runId": self.run_id}]

    def run_error(self, message: str, code: str = "error") -> dict:
        """公开的 `RUN_ERROR` 构造器。

        之所以不写成 `RUN_ERROR` 字面量而要走这里:错误分支需要和正常事件**同源**的
        `timestamp` 与字段形状,让调用方去碰 `_base()` 这种私有方法只会很快腐化。
        """
        return {**self._base("RUN_ERROR"), "message": message, "code": code}

    def feed(self, ev: AgentEvent) -> list[dict]:
        """把一个 qi 事件翻成 0+ 个 AG-UI 事件。"""
        kind = ev.kind

        if kind == "dispatch":
            # qi 独有:分派决策 → AG-UI 的协议级扩展点 Custom
            return [{**self._base("CUSTOM"), "name": "qi.dispatch",
                     "value": dict(ev.data or {}, text=ev.text, agent=ev.agent)}]

        if kind == "opening":
            # qi 独有:agent 开场白(不进 LLM 上下文,所以不做成 TextMessage)
            return [{**self._base("CUSTOM"), "name": "qi.opening",
                     "value": {"agent": ev.agent, "text": ev.text,
                               "suggestions": (ev.data or {}).get("suggestions", [])}}]

        if kind == "agent_start":
            # RUN_STARTED 已经在流首发过,这里不重复
            return []

        if kind == "thinking_delta":
            return self._emit_reasoning(ev.text)

        if kind == "text_delta":
            return self._emit_text(ev.text)

        if kind == "retry":
            # 一次 LLM 调用失败、即将重跑它(pi 的 agent 级重试)。前端据此**撤回半截输出** ——
            # 失败那次的消息不会进历史,屏幕上留着它,重试成功后同一段回答就会出现两遍。
            return [{**self._base("CUSTOM"), "name": "qi.retry",
                     "value": dict(ev.data or {})}]

        if kind == "assistant_message":
            # 每轮 LLM 回复完成一次:这是"这段文本到此为止"的权威边界。
            calls = [str(c) for c in (ev.data or {}).get("tool_calls") or []]
            out = []
            if self._open is not None and self._open.kind == "text":
                # 流式已经开过:只收尾(不重复发 content —— TextMessageContent 已送过)
                out += self._close_open()
            elif ev.text:
                # 没有流式(实现方只有 chat,或 provider 不支持流):补一个完整消息
                out += self._emit_text(ev.text)
                out += self._close_open()
            if calls:
                # "过程"叙述还是"最终回答",由 qi 的 tool_calls 决定 —— AG-UI 没有这个区分,
                # 用 Custom 带出去,免得前端丢信息(轨迹/折叠要用)。
                out.append({**self._base("CUSTOM"), "name": "qi.narration",
                            "value": {"step": (ev.data or {}).get("step"),
                                      "tool_calls": calls,
                                      "is_final": not calls,
                                      "thinking": (ev.data or {}).get("thinking", "")}})
            return out

        if kind == "text":
            # qi 的末尾权威完整文本。AG-UI 没有对应事件(内容已由 delta 送过),
            # 所以只用于**纠偏**:若流式曾丢帧,这里补一条 Custom 让前端有机会校准。
            return []

        if kind == "tool_start":
            out = self._close_open()
            # **必须用事件自带的 `tool_call_id`**:活动帧(`ACTIVITY_SNAPSHOT.content.toolCallId`)
            # 与落盘都按 runner 的真实 `call.id` 认卡 —— 这里自编号会让前端归不了卡,
            # 现象是「工具卡在闪、里面一片空白」(帧进了待用暂存,永远等不到卡建成)。
            call_id = str((ev.data or {}).get("tool_call_id") or "") or self._next("call")
            args = (ev.data or {}).get("args") or {}
            self._pending_tool = (call_id, str(ev.tool or "tool"), args)
            out += [
                {**self._base("TOOL_CALL_START"), "toolCallId": call_id,
                 "toolCallName": str(ev.tool or "tool"),
                 **({"parentMessageId": self._message_id} if self._message_id else {})},
            ]
            # AG-UI 的三段式要求 Start 之后有"一个或多个" Args。qi 在 tool_start 时
            # 已经拿到**完整**参数(不是增量),所以一次发完,然后立刻 End。
            out.append({**self._base("TOOL_CALL_ARGS"), "toolCallId": call_id,
                        "delta": json.dumps(args, ensure_ascii=False)})
            out.append({**self._base("TOOL_CALL_END"), "toolCallId": call_id})
            return out

        if kind == "tool_end":
            # 用事件自带的 id 配对(同一轮多个工具并行时,单个槽位会配错);
            # 老来源不给 id 时才退回槽位/自编号。
            call_id = str((ev.data or {}).get("tool_call_id") or "")
            if not call_id:
                call_id, _name, _args = self._pending_tool or (self._next("call"), "", {})
            self._pending_tool = None
            data = dict(ev.data or {})
            return [{**self._base("TOOL_CALL_RESULT"),
                     "messageId": self._next("toolmsg"),
                     "toolCallId": call_id,
                     "content": ev.text or "",
                     "role": "tool",
                     # qi 的结构化结果(status/duration_ms/exit_code/error)挂在 metadata:
                     # AG-UI 的 base event 自带 open-by-key 的 metadata,正好放这些
                     "metadata": {"qi.tool": data}}]

        if kind == "compaction_start":
            return [{**self._base("CUSTOM"), "name": "qi.compaction",
                     "value": {"phase": "start"}}]

        if kind == "compaction_end":
            return [{**self._base("CUSTOM"), "name": "qi.compaction",
                     "value": {"phase": "end", **(ev.data or {})}}]

        if kind == "branch":
            return [{**self._base("CUSTOM"), "name": "qi.branch",
                     "value": dict(ev.data or {})}]

        if kind == "error":
            out = self._close_open()
            out.append({**self._base("RUN_ERROR"), "message": ev.text or "未知错误",
                        "code": str((ev.data or {}).get("error") or "error")})
            return out

        if kind == "agent_end":
            out = self._close_open()
            usage = dict((ev.data or {}).get("usage") or {})
            out.append({**self._base("RUN_FINISHED"),
                        "outcome": {"type": "success"},
                        "result": ev.text or "",
                        **({"metadata": {"qi.usage": usage}} if usage else {})})
            return out

        # 未知 kind:按 AG-UI 的 Raw 透传,而不是静默丢弃
        return [{**self._base("RAW"), "event": ev.kind,
                 "source": "qi", "data": {"text": ev.text, "data": ev.data}}]

    def feed_activity(self, content: dict) -> list[dict]:
        """委派直播 → AG-UI 帧(**按 phase 分两路**,design/web-lab.md 二期)。

        `start` / `done` 是子智能体的**生命**:用 AG-UI 本来就有的
        `SUBAGENT_STARTED` / `SUBAGENT_FINISHED` / `SUBAGENT_ERROR`(0.0.59 的
        `EventType` 里就有),`parentToolCallId` 正好指回发起它的那张工具卡;
        `stream` / `act` / `think` 是**内容**:`ACTIVITY_SNAPSHOT`,每 run 一条稳定
        `messageId` + `replace: true`(这条活动的最新全量)。

        分两路的意义:前端先能把“谁被派了单”画成一张卡(名字 / 父 / 深度),
        内容再往这张卡里长 —— 而不是从内容帧里倒推“好像多了个人”。
        严格 AG-UI 客户端也认得生与死。
        """
        run = content.get("run")
        if not isinstance(run, str) or not run:
            return []
        meta = _subagent_meta(content)
        phase = content.get("phase")
        if phase == "start":
            name = _text(content.get("role")) or run
            ev: dict = {**self._base("SUBAGENT_STARTED"),
                        "subagentRunId": run, "name": name}
            task = _text(content.get("task"))
            if task:
                ev["description"] = task
            depth = content.get("depth")
            parent = _text(content.get("parent"))
            # 顶层那次由**主 agent** 派出:主 agent 不是子运行,没有父 run id
            if isinstance(depth, int) and depth > 1 and parent:
                ev["parentSubagentRunId"] = parent
            call_id = content.get("toolCallId")
            if isinstance(call_id, str) and call_id:
                ev["parentToolCallId"] = call_id
            if meta:
                ev["metadata"] = meta
            return [ev]
        if phase == "done":
            base: dict = {"subagentRunId": run}
            if meta:
                base["metadata"] = meta
            if content.get("ok") is not False:
                return [{**self._base("SUBAGENT_FINISHED"),
                         **base, "outcome": {"type": "success"}}]
            return [{**self._base("SUBAGENT_ERROR"), **base,
                     "message": _text(content.get("error")) or "子运行失败",
                     "code": "subagent_failed"}]
        return [{**self._base("ACTIVITY_SNAPSHOT"),
                 "messageId": f"qi-act-{run}",
                 "activityType": activity.ACTIVITY_TYPE,
                 "subagentRunId": run,
                 "replace": True,
                 "content": content}]

    def finish_open(self) -> list[dict]:
        """流意外结束(出错/被取消)时补收尾,避免客户端停在 Start 状态。"""
        return self._close_open()

    # ── 远端角色:边收边发 ──────────────────────────────
    #
    # 远端角色走的是 A2A 的 `artifactUpdate`,本来就是增量的 —— 所以这里不复用
    # "整段一次发完"的老写法(那会让界面在远端跑完之前一直空着)。
    # 三个方法对应 AG-UI 的三段式,懒开:第一次有内容时才 `TEXT_MESSAGE_START`。

    def remote_reset(self) -> list[dict]:
        """远端重来了一段(`append=false`):把已经发出去的那条收尾。

        下一次 `remote_delta` 会自己开一条新的。不收尾就会把"重放"的内容接在旧内容
        后面 —— 界面上看是同一句话出现两遍。
        """
        return self._close_open()

    def remote_delta(self, delta: str) -> list[dict]:
        """远端正文增量。`_emit_text` 已经处理"没开就开",这里只是给它一个公开名字。"""
        return self._emit_text(delta)

    def remote_message(self, text: str) -> list[dict]:
        """把整段文本一次发完(**非流式**远端 / 流式没吐过任何增量时兜底)。"""
        if not text:
            return []
        return self._emit_text(text) + self._close_open()

    def remote_finished(self, text: str) -> dict:
        """远端那一轮的 `RUN_FINISHED`(与本地一样带 `result`)。

        **不带 `metadata["qi.usage"]`**:远端这一轮没有 token 计量 —— 本地那条路带它
        是因为 runner 会报 usage。前端见到 "没有 usage" 会显示“用量未记录”,那句提示
        是为“本地轮跑了但没写 usage”的旧会话准备的,套在远端轮上是**错的**。
        """
        return {**self._base("RUN_FINISHED"),
                "outcome": {"type": "success"}, "result": text}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _subagent_meta(content: dict) -> dict:
    """`SUBAGENT_*` 的归卡与展示元数据(AG-UI 的 `metadata` 自由字段)。

    `SUBAGENT_STARTED` 有 `parentToolCallId`,而 `SUBAGENT_FINISHED` /
    `SUBAGENT_ERROR` **没有**这个字段(协议把死挂在 run id 上)。qi 的卡是按
    “哪次工具调用”归的,所以三条事件都额外带一份 `metadata`,前端用同一把钥匙
    (`toolCallId`)就能找到卡 —— 不必自己维护 run → 卡 的映射表。
    """
    out: dict = {}
    call_id = content.get("toolCallId")
    if isinstance(call_id, str) and call_id:
        out["toolCallId"] = call_id
    depth = content.get("depth")
    if isinstance(depth, int):
        out["depth"] = depth
    parent = _text(content.get("parent"))
    if parent:
        out["parent"] = parent
    return out


def _now_ms() -> int:
    """毫秒时间戳。`time_ns` 而不是 `time()*1000`:避免浮点误差,也少一次转换。"""
    return time.time_ns() // 1_000_000


#: 心跳哨兵:队列空闲超过间隔时 yield 它,`app` 把它编成 SSE 注释行(`: ping`)。
#: 存在的理由:中间层(反向代理 / **内网穿透 / 远程端口映射**)常常“攒够一定字节
#: 或时间才转发”,于是 SSE 帧全部堆到响应结束 —— 用户看到的就是“过程要等结束才
#: 展示”,而且刷新/重启都没用。周期性输出是唯一能让这类层乖乖 flush 的东西。
HEARTBEAT = object()


async def mixed_frames(feed: Any, translator: AguiTranslator,
                       *, heartbeat_s: float | None = None) -> Any:
    """直播队列 → AG-UI 帧序列(**单一队列**,design/web-lab.md §5.2)。

    队列里混着三种消息:`KIND_ACTIVITY`(直播内容,翻译成 `ACTIVITY_SNAPSHOT`)、
    `KIND_FRAME`(主流的已成型帧)、`KIND_END`(哨兵)。同队列 FIFO 是这里最重要
    的性质:子运行期间主流是静默的(LLM 调用不产事件),活动帧与工具卡帧不排队,
    顺序就会错乱 —— 卡还没建、活动先到了。

    `heartbeat_s` 给了就在空闲超过它时 yield `HEARTBEAT`(见上面那个哨兵的用途)。
    超时只取消 `Queue.get`(取消它不会丢队列里的元素),所以心跳不会吃掉真帧。

    纯消费逻辑:不碰 FastAPI、不碰 runtime,给一个假队列就能测。
    """
    while True:
        if heartbeat_s is None:
            kind, payload = await feed.get()
        else:
            try:
                kind, payload = await asyncio.wait_for(feed.get(), heartbeat_s)
            except asyncio.TimeoutError:
                yield HEARTBEAT
                continue
        if kind == activity.KIND_END:
            return
        if kind == activity.KIND_ACTIVITY:
            for frame in translator.feed_activity(payload if isinstance(payload, dict) else {}):
                yield frame
            continue
        yield payload


# ── 会话快照 → AG-UI ─────────────────────────────────────────────────


def messages_snapshot(entries: list[dict]) -> dict:
    """qi 的会话 entries → `MESSAGES_SNAPSHOT`。

    只取会进对话的那一类(`message`),并映射到 AG-UI 的 message 形状。
    qi 的 `custom`(叙述)/`tool`/`dispatch`/`state` 不属于 messages —— 它们各自有
    `Custom` 事件表达,由前端在历史回放时另行处理(见 design/web.md)。
    """
    out = []
    for e in entries:
        if e.get("type") != "message":
            continue
        role = str(e.get("role") or "user")
        out.append({
            "id": str(e.get("id") or ""),
            "role": role,
            "content": str(e.get("content") or ""),
            **( {"name": str(e.get("agent_id"))} if e.get("agent_id") else {} ),
        })
    return {"type": "MESSAGES_SNAPSHOT", "timestamp": _now_ms(), "messages": out}


def state_snapshot(state: Any) -> dict:
    return {"type": "STATE_SNAPSHOT", "timestamp": _now_ms(), "snapshot": state or {}}


def history_snapshot(entries: list[dict]) -> dict:
    """qi 自己的**完整 entry 列表**。

    为什么需要它:`MESSAGES_SNAPSHOT` 是 AG-UI 的形状,只能装"会进对话的消息"
    (`role` 为 user/assistant 的那些)。而 qi 的轨迹还包括 `dispatch` / `tool` /
    `custom(assistant_narration)` / `compaction` —— 它们**不在 messages 里**。

    不给这一份,历史回放就会丢:刷新之后分派卡、工具卡、叙述全部消失,
    只剩两段干巴巴的对话 —— 而这正是 qi 一直要避免的"直播与回放不一致"。

    走 `CUSTOM`(AG-UI 的协议级扩展点):qi 自己的前端靠它重建整屏,
    严格 AG-UI 客户端会直接忽略这个事件,不会报错。
    """
    return {"type": "CUSTOM", "timestamp": _now_ms(), "name": "qi.history",
            "value": {"entries": entries}}
