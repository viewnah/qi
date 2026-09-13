"""LLM 客户端(llm.py):协议 + litellm 实现(A4)。

一次 chat 返回:文本 或 工具调用列表。Router 与 Runner 共用(按 models.json 的 default/router)。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from .config import ResolvedModel
from .auth import AuthStore, resolve_key


@dataclass
class ToolCallOut:
    id: str
    name: str
    args: dict


@dataclass
class ChatMessage:
    role: str                    # system|user|assistant|tool
    content: str = ""
    tool_calls: list[ToolCallOut] = field(default_factory=list)
    tool_call_id: str | None = None

    def to_dict(self) -> dict:
        d: dict = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}}
                for c in self.tool_calls
            ]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d


class ChatResponse:
    def __init__(self, text: str = "", tool_calls: list[ToolCallOut] | None = None,
                 usage: dict | None = None):
        self.text = text
        self.tool_calls = tool_calls or []
        self.usage = usage or {}


class LLMClient(Protocol):
    """**最小**契约:只要求 `chat()`。

    流式是**可选增强**(见 `StreamingLLMClient` 与 `stream_llm`):第三方实现与测试替身
    只写 chat 也能跑,代价是前端拿到单块文本而非逐字。
    """

    async def chat(self, messages: list[ChatMessage], tools: list[dict] | None = None,
                   temperature: float | None = None) -> ChatResponse: ...


class StreamingLLMClient(Protocol):
    """可选增强:提供 `astream()` 即可逐字流式。

    实现方**不要**在工厂函数里起后台资源;它是一个普通的异步生成器。
    """

    def astream(self, messages: list[ChatMessage], tools: list[dict] | None = None,
                temperature: float | None = None) -> AsyncIterator[LLMDelta]: ...


@dataclass
class LLMDelta:
    """流式增量。契约:**0+ 个文本块,然后恰好一个 `finished` 块**。

    最终文本 = 所有文本块拼接;`tool_calls` 与 `usage` **只在** `finished` 块上。
    这一形状让消费者(AgentRunner)只需处理一种情况,而不是"有的实现流式、有的不流式"。
    """

    text: str = ""
    finished: bool = False
    tool_calls: list[ToolCallOut] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


async def chat_as_stream(llm: LLMClient, messages: list[ChatMessage],
                         tools: list[dict] | None = None,
                         temperature: float | None = None) -> AsyncIterator[LLMDelta]:
    """把一次非流式 `chat()` 合成为流:整段文本作为**单块** + 一个终止块。

    两处用途:(a) 实现方只有 chat(第三方/测试替身);(b) 流式打不开时的降级。
    """
    resp = await llm.chat(messages, tools=tools, temperature=temperature)
    if resp.text:
        yield LLMDelta(text=resp.text)
    yield LLMDelta(finished=True, tool_calls=resp.tool_calls, usage=resp.usage)


async def stream_llm(llm: LLMClient, messages: list[ChatMessage],
                     tools: list[dict] | None = None,
                     temperature: float | None = None) -> AsyncIterator[LLMDelta]:
    """统一调用入口:实现方有 `astream()` 就流式,否则用 chat 合成单块。"""
    astream = getattr(llm, "astream", None)
    if astream is None:
        async for delta in chat_as_stream(llm, messages, tools, temperature):
            yield delta
        return
    async for delta in astream(messages, tools=tools, temperature=temperature):
        yield delta


# litellm 前缀按 api 类型推导(不预置 provider 名录)
_API_PREFIX = {
    "anthropic-messages": "anthropic",
    "google-generative-ai": "gemini",
    "openai-responses": "openai",
    "openai-completions": "openai",
}


def litellm_model_name(spec: ResolvedModel) -> str:
    """把 provider + api 映射为 litellm 的 model 字符串(不依赖 provider 名录)。"""
    prefix = _API_PREFIX.get(spec.api, "openai")
    return f"{prefix}/{spec.model}"


_USAGE_FALLBACK_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def usage_to_dict(usage: object) -> dict:
    """把 provider 的 usage 对象归一为纯 dict。

    必要性:litellm 返回的是 pydantic 对象。直接放进 `AgentEvent.data` 会让
    `json.dumps` 失败(SSE 与 `--mode json` 都要序列化它),也取不到字段。
    """
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    for attr in ("model_dump", "dict"):        # pydantic v2 / v1
        dump = getattr(usage, attr, None)
        if callable(dump):
            with contextlib.suppress(Exception):
                data = dump()
                if isinstance(data, dict):
                    return data
    return {key: getattr(usage, key) for key in _USAGE_FALLBACK_KEYS
            if getattr(usage, key, None) is not None}


def merge_tool_call_delta(merged: dict[int, dict], tc: object) -> None:
    """把一片 `tool_call` 增量并入累加器(按 `index` 归并)。

    流式下工具调用是被拆开发来的:首片带 `id` + `name`,后续片只带 `arguments` 的碎片,
    而且多个并行调用会**交错的**发。因此必须按 index 归并、参数**拼接**。
    """
    idx = getattr(tc, "index", None)
    key = idx if isinstance(idx, int) else len(merged)
    slot = merged.setdefault(key, {"id": None, "name": "", "args": ""})
    tc_id = getattr(tc, "id", None)
    if tc_id:
        slot["id"] = tc_id
    fn = getattr(tc, "function", None)
    if fn is None:
        return
    name = getattr(fn, "name", None)
    if name:
        slot["name"] = name
    args = getattr(fn, "arguments", None)
    if args:
        slot["args"] = slot["args"] + args


def merged_tool_calls(merged: dict[int, dict]) -> list[ToolCallOut]:
    """把累加器收成 ToolCallOut 列表。

    无名片段(部分 provider 会发空壳)跳过;参数 JSON 坏了退化为 `{}`,
    而不是把整轮打挂——模型下一轮会看到 "未知参数" 并自行调整。
    """
    calls: list[ToolCallOut] = []
    for i, slot in sorted(merged.items()):
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append(ToolCallOut(id=slot["id"] or f"call_{i}", name=slot["name"], args=args))
    return calls


class LiteLLMClient:
    """基于 litellm 的实现。spec 来自 resolve_model()(经 resolve_key 取 key)。"""

    def __init__(self, spec: ResolvedModel, auth_store: AuthStore | None = None):
        self.spec = spec
        self._resolved = resolve_key(spec.provider, spec.api_key_ref, auth_store or AuthStore())
        self.model_name = litellm_model_name(spec)
        # 该 provider 是否已拒绝过 stream_options(见 astream 的容错 1)
        self._no_usage_opt = False
        # 流式是否已确认打不开(容错 2):钉住后不再每轮重试一次注定失败的请求
        self._stream_broken = False

    @property
    def ready(self) -> bool:
        return self._resolved.ok

    def _base_kwargs(self, messages: list[ChatMessage], tools: list[dict] | None,
                     temperature: float | None) -> dict:
        """chat 与 astream 共用的请求参数(避免两处漂移)。"""
        kwargs: dict = {
            "model": self.model_name,
            "messages": [m.to_dict() for m in messages],
        }
        if tools:
            kwargs["tools"] = tools
        if self._resolved.key:
            kwargs["api_key"] = self._resolved.key
        if self.spec.base_url:
            kwargs["api_base"] = self.spec.base_url
        if self.spec.max_tokens:
            kwargs["max_tokens"] = self.spec.max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        return kwargs

    async def chat(self, messages: list[ChatMessage], tools: list[dict] | None = None,
                   temperature: float | None = None) -> ChatResponse:
        import litellm

        resp = await litellm.acompletion(**self._base_kwargs(messages, tools, temperature))
        # litellm 的返回类型是 "流式包装器 | 补全对象" 的联合,
        # 这里只走非流式分支,按实际形状收窄。
        msg = cast(Any, resp).choices[0].message
        text = msg.content or ""
        if isinstance(text, list):  # 多模态块:取文本
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        tool_calls = []
        for tc in msg.tool_calls or []:
            fn = getattr(tc, "function", None)   # 自定义工具调用无 function,跳过
            if fn is None:
                continue
            try:
                args = json.loads(fn.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCallOut(id=tc.id or f"call_{len(tool_calls)}",
                                          name=fn.name, args=args))
        return ChatResponse(text=text or "", tool_calls=tool_calls,
                            usage=usage_to_dict(getattr(resp, "usage", None)))

    async def _open_stream(self, messages: list[ChatMessage], tools: list[dict] | None,
                           temperature: float | None, with_usage: bool) -> AsyncIterator[Any]:
        import litellm

        kwargs = self._base_kwargs(messages, tools, temperature)
        kwargs["stream"] = True
        if with_usage:
            kwargs["stream_options"] = {"include_usage": True}
        # litellm 的返回类型是 "流式包装器 | 补全对象" 的联合:这里只走流式分支,
        # 按实际形状收窄(与 chat() 的处理对称)。
        return cast(AsyncIterator[Any], await litellm.acompletion(**kwargs))

    async def astream(self, messages: list[ChatMessage], tools: list[dict] | None = None,
                      temperature: float | None = None) -> AsyncIterator[LLMDelta]:
        """litellm 流式:逐块 yield 文本,最后 yield 合并好的 tool_calls 与 usage。

        两处容错(均为**粘性**的,不会每轮重试):
          1. `stream_options.include_usage` 不被某些 provider 接受 → 去掉该参数重试一次。
             不加这个,流式下拿不到 usage,成本/token 显示会长期为空。
          2. 流式本身打不开(网关/代理不支持)→ 永久退回非流式,整段文本作为单块发出。
             消费者形状不变,只是不再逐字。

        限制(写明白而不是假装支持):若流**已吐出部分文本后**才失败,不重试、直接抛出——
        重试会导致文本重复。这种情况需上层处理(Web 层应转成 error 帧)。
        """
        if self._stream_broken:
            async for delta in chat_as_stream(self, messages, tools, temperature):
                yield delta
            return

        stream: AsyncIterator[Any] | None = None
        for with_usage in ((False,) if self._no_usage_opt else (True, False)):
            try:
                stream = await self._open_stream(messages, tools, temperature, with_usage)
                break
            except Exception:  # noqa: BLE001 打开流失败:区分"参数被拒"与"不支持流式"
                if with_usage:
                    self._no_usage_opt = True
                else:
                    stream = None
        if stream is None:
            self._stream_broken = True
            async for delta in chat_as_stream(self, messages, tools, temperature):
                yield delta
            return

        merged: dict[int, dict] = {}
        usage: dict = {}
        text_seen = False
        try:
            async for chunk in stream:
                got = usage_to_dict(getattr(chunk, "usage", None))
                if got:
                    usage = got                      # 末块带 usage(已请求 include_usage)
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta_obj = getattr(choices[0], "delta", None)
                if delta_obj is None:
                    continue
                text = getattr(delta_obj, "content", "") or ""
                if isinstance(text, list):           # 多模态块:取文本
                    text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
                if text:
                    text_seen = True
                    yield LLMDelta(text=text)
                for tc in getattr(delta_obj, "tool_calls", None) or []:
                    merge_tool_call_delta(merged, tc)
        except Exception:
            # 首片就失败 → 可安全降级;已吐过字 → 不能重试(会重复输出)
            if text_seen:
                raise
            self._stream_broken = True
            async for delta in chat_as_stream(self, messages, tools, temperature):
                yield delta
            return
        yield LLMDelta(finished=True, tool_calls=merged_tool_calls(merged), usage=usage)


def chat_message_from_dict(d: dict) -> ChatMessage:
    """从会话 JSONL 反序列化(与 ChatMessage.to_dict 对应)。"""
    msg = ChatMessage(role=str(d.get("role", "user")), content=str(d.get("content", "")))
    if d.get("tool_call_id"):
        msg.tool_call_id = d["tool_call_id"]
    for tc in d.get("tool_calls") or []:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        msg.tool_calls.append(ToolCallOut(
            id=tc.get("id", "call"), name=fn.get("name", "?"), args=args))
    return msg
