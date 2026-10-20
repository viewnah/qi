"""A2A 客户端:**官方 `a2a-sdk`** 之上的薄封装。

不再自己拼 JSON-RPC / 解析 SSE —— 这些正是 SDK 的职责,而且不同版本的服务端在
方法名(PascalCase `SendStreamingMessage` vs 规范的 `message/stream`)、`A2A-Version`
请求头、proto oneof 包装上并不一致。自己适配等于跟着 SDK 的版本表走一遍。

这里只做三件事:

1. 用 httpx 抓名片(Hikqin 的**列表**端点返回数组,SDK 的 resolver 只认单卡);
2. 把 SDK 的流式事件聚合成一个 `SendResult`(正文 / 终态 / taskId / HITL 载荷);
3. 把 SDK 的异常翻译成 `RpcError`(带条目名,模型能读懂)。

依赖方向:**sdk 在卡片的 proto 上选传输**,所以 `tenant`、`protocolVersion`、
`A2A-Version` 头都由 SDK 自己处理,配置里不需要再有开关。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.errors import A2AError
from google.protobuf import json_format, struct_pb2

from .card import AgentCard
from .config import A2AEntry

#: 协议终态。`input-required` 也算"这一轮结束了"(等外部回传决策)。
TERMINAL_STATES = frozenset({
    "completed", "failed", "canceled", "rejected", "input-required", "auth-required",
})

_TERMINAL_ENUMS = frozenset({
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
    TaskState.TASK_STATE_INPUT_REQUIRED,
    TaskState.TASK_STATE_AUTH_REQUIRED,
})


class RpcError(Exception):
    """协议 / HTTP / 解析错误。消息面向模型,**带条目名**。"""

    def __init__(self, message: str, *, code: Any = None,
                 status: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.data = data


@dataclass
class SendResult:
    """一次 `send` / `resume` 的聚合结果。"""

    text: str = ""
    state: str = ""
    task_id: str = ""
    context_id: str = ""
    #: `input-required` 时携带 `action_requests` / `review_configs`(Hikqin HITL)
    interrupt: dict[str, Any] | None = None
    events: int = 0


# ── proto 读视图 ────────────────────────────────────────

def state_name(value: int) -> str:
    """`TaskState.TASK_STATE_INPUT_REQUIRED` → `input-required`(界面与结果里用它)。"""
    try:
        return TaskState.Name(value).removeprefix("TASK_STATE_").lower().replace("_", "-")
    except ValueError:
        return ""


def text_of(parts: Any) -> str:
    """拼接 TextPart;FilePart / DataPart 不转文本(单独处理)。"""
    return "".join(part.text for part in parts if part.WhichOneof("content") == "text")


def data_of(parts: Any) -> list[dict]:
    """取出 DataPart 里的对象(审批 `action_requests` 走这里)。"""
    return [json_format.MessageToDict(part.data) for part in parts
            if part.WhichOneof("content") == "data"]


class _Aggregate:
    """把 `StreamResponse` 事件累积成一个结果。"""

    def __init__(self) -> None:
        self.text = ""
        self.status_text = ""
        self.state = ""
        self.task_id = ""
        self.context_id = ""
        self.interrupt: dict[str, Any] | None = None
        self.terminal = False
        self.events = 0

    def consume(self, event: Any) -> None:
        kind = event.WhichOneof("payload")
        if kind is None:
            return
        self.events += 1

        if kind == "task":
            self._from_task(event.task)
        elif kind == "status_update":
            update = event.status_update
            self._ids(update.task_id, update.context_id)
            self._from_status(update.status)
        elif kind == "artifact_update":
            update = event.artifact_update
            self._ids(update.task_id, update.context_id)
            self._apply_artifact(update.artifact, bool(update.append))
        elif kind == "message":
            text = text_of(event.message.parts)
            if text:
                self.text += text
            self._ids("", event.message.context_id)

        if self.state and self._enum_terminal():
            self.terminal = True

    def _enum_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def _ids(self, task_id: str, context_id: str) -> None:
        if task_id:
            self.task_id = task_id
        if context_id:
            self.context_id = context_id

    def _from_task(self, task: Any) -> None:
        self._ids(task.id, task.context_id)
        self._from_status(task.status)
        if not self.text:
            self.text = "".join(text_of(artifact.parts) for artifact in task.artifacts)

    def _from_status(self, status: Any) -> None:
        state = state_name(status.state)
        if state:
            self.state = state
        parts = status.message.parts if status.HasField("message") else []
        text = text_of(parts)
        if text:
            self.status_text = text
        if state == "input-required":
            merged: dict[str, Any] = {}
            for data in data_of(parts):
                merged.update(data)
            self.interrupt = merged

    def _apply_artifact(self, artifact: Any, append: bool) -> None:
        text = text_of(artifact.parts)
        # proto 语义:首个分片不带 `append`(= 替换),后续分片带 `append: true`。
        # 实测 Hikqin 就是这样发的,所以直接照 proto 的值来。
        self.text = self.text + text if append else text

    def result(self) -> SendResult:
        state = self.state
        if not state:
            state = "completed" if self.text else "unknown"
        return SendResult(
            text=self.text or self.status_text,
            state=state,
            task_id=self.task_id,
            context_id=self.context_id,
            interrupt=self.interrupt if state == "input-required" else None,
            events=self.events,
        )


class A2AClient:
    """一个条目的客户端(每次操作一个;由调用方 `async with` 或 `aclose()` 收尾)。"""

    def __init__(self, entry: A2AEntry, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.entry = entry
        self._http = httpx.AsyncClient(
            transport=transport,
            headers=dict(entry.headers),
            timeout=entry.timeout,
            follow_redirects=False,
        )
        # `ClientFactory` 会往这个 httpx client 上补 `A2A-Version` 头,
        # 并按卡片里的 interface 选传输、套 tenant 装饰器。
        self._factory = ClientFactory(ClientConfig(
            streaming=entry.streaming,
            polling=False,
            httpx_client=self._http,
        ))

    async def __aenter__(self) -> "A2AClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── 错误翻译 ──
    def _wrap(self, exc: BaseException) -> RpcError:
        code = getattr(exc, "code", None)
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        return RpcError(f"条目 `{self.entry.name}`:{exc}", code=code, status=status)

    # ── 名片发现 ──
    async def get_json(self, url: str) -> Any:
        """GET 一个 JSON 文档。Hikqin 的列表端点返回数组,SDK 的 resolver 认不了。"""
        try:
            response = await self._http.get(url, headers={"accept": "application/json"})
        except httpx.HTTPError as exc:
            raise RpcError(f"条目 `{self.entry.name}`:请求 {url} 失败:{exc}") from exc
        if response.status_code >= 400:
            snippet = " ".join(response.text.split())[:300]
            raise RpcError(
                f"条目 `{self.entry.name}`:{url} 返回 HTTP {response.status_code}"
                + (f" — {snippet}" if snippet else ""),
                status=response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise RpcError(f"条目 `{self.entry.name}`:{url} 返回的不是 JSON") from exc

    # ── 调用 ──
    def _sdk(self, card: AgentCard) -> Any:
        try:
            return self._factory.create(card.proto)
        except (ValueError, KeyError) as exc:
            raise RpcError(
                f"条目 `{self.entry.name}`:无法与 {card.name!r} 建立传输({exc})") from exc

    @staticmethod
    def _message(text: str, *, context_id: str, task_id: str,
                 decisions: list[dict] | None) -> Message:
        message = Message(message_id=uuid.uuid4().hex, role=Role.ROLE_USER)
        if context_id:
            message.context_id = context_id
        if task_id:
            message.task_id = task_id
        if decisions:
            # `Part.data` 是 `google.protobuf.Value`(不是 Struct),用 json_format 转最稳。
            data = struct_pb2.Value()
            json_format.ParseDict({"decisions": decisions}, data)
            message.parts.append(Part(data=data))
        if text:
            message.parts.append(Part(text=text))
        return message

    async def send(self, card: AgentCard, text: str = "", *, context_id: str = "",
                   task_id: str = "", decisions: list[dict] | None = None) -> SendResult:
        """发消息或回传审批决策,聚合到终态(或 `input-required`)。"""
        request = SendMessageRequest(
            message=self._message(text, context_id=context_id, task_id=task_id,
                                  decisions=decisions))
        if card.tenant:
            request.tenant = card.tenant
        aggregate = _Aggregate()
        try:
            async for event in self._sdk(card).send_message(request):
                aggregate.consume(event)
                if aggregate.terminal:
                    break
        except (A2AError, httpx.HTTPError, json_format.ParseError, ValueError) as exc:
            raise self._wrap(exc) from exc
        result = aggregate.result()
        result.task_id = result.task_id or task_id
        result.context_id = result.context_id or context_id
        return result

    async def get_task(self, card: AgentCard, task_id: str) -> dict:
        request = GetTaskRequest(id=task_id)
        if card.tenant:
            request.tenant = card.tenant
        return await self._call(card, lambda client: client.get_task(request))

    async def cancel_task(self, card: AgentCard, task_id: str) -> dict:
        request = CancelTaskRequest(id=task_id)
        if card.tenant:
            request.tenant = card.tenant
        return await self._call(card, lambda client: client.cancel_task(request))

    async def _call(self, card: AgentCard, fn: Callable[[Any], Any]) -> dict:
        """任务类调用:结果转成 dict(界面上直接 `json.dumps`)。"""
        try:
            proto = await fn(self._sdk(card))
        except (A2AError, httpx.HTTPError, json_format.ParseError, ValueError) as exc:
            raise self._wrap(exc) from exc
        return json_format.MessageToDict(proto, preserving_proto_field_name=False)
