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

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..models import AgentEvent

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
            call_id = self._next("call")
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

    def finish_open(self) -> list[dict]:
        """流意外结束(出错/被取消)时补收尾,避免客户端停在 Start 状态。"""
        return self._close_open()


def _now_ms() -> int:
    """毫秒时间戳。`time_ns` 而不是 `time()*1000`:避免浮点误差,也少一次转换。"""
    return time.time_ns() // 1_000_000


# ── 会话快照 → AG-UI ─────────────────────────────────────────────────


def messages_snapshot(entries: list[dict]) -> dict:
    """qi 的会话 entries → `MESSAGES_SNAPSHOT`。

    只取会进对话的那一类(`message`),并映射到 AG-UI 的 message 形状。
    qi 的 `custom`(叙述)/`tool`/`dispatch`/`state` 不属于 messages —— 它们各自有
    `Custom` 事件表达,由前端在历史回放时另行处理(见 docs/web.md)。
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
