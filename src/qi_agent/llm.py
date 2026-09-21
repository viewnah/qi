"""LLM 客户端(llm.py):协议 + litellm 实现(A4)。

一次 chat 返回:文本 或 工具调用列表。Router 与 Runner 共用(按 models.json 的 default/router)。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable

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


@dataclass
class ChatResponse:
    """一次非流式回复。`reasoning` = 思考内容(pi 的 thinking block),不算回答。"""

    text: str = ""
    tool_calls: list[ToolCallOut] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    reasoning: str = ""


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


@runtime_checkable
class ThinkingLLMClient(Protocol):
    """可选增强:支持思考级别(pi 的 thinkingLevel → `reasoning_effort`)。

    单独一个协议而不是塞进 `LLMClient`:很多实现(测试替身/第三方)没有思考概念,
    不该被强迫实现一个空属性。用 `isinstance` 在运行期判定是否真的可设。
    """

    thinking_level: str
    reasoning_dropped: bool
    """provider 拒过 reasoning_effort 吗(UI 据此提示一次)。"""


@dataclass
class LLMDelta:
    """流式增量。契约:**0+ 个文本块,然后恰好一个 `finished` 块**。

    最终文本 = 所有文本块拼接;`tool_calls` 与 `usage` **只在** `finished` 块上。
    这一形状让消费者(AgentRunner)只需处理一种情况,而不是"有的实现流式、有的不流式"。

    `reasoning` 是思考内容(pi 的 thinking block):与 `text` 分开流式,**不算**最终回答。
    """

    text: str = ""
    reasoning: str = ""
    finished: bool = False
    tool_calls: list[ToolCallOut] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
"""思考级别(对齐 pi 的 thinkingLevel)。`off` = 不请求思考。"""

# qi 经 litellm 传 reasoning_effort;litellm / 各 provider 只认 minimal/low/medium/high,
# 所以 xhigh / max 收敛到 high(pi 的 provider 侧有原生 xhigh/max,litellm 没有)。
_EFFORT_MAP = {"minimal": "minimal", "low": "low", "medium": "medium",
               "high": "high", "xhigh": "high", "max": "high"}


def normalize_thinking_level(value: str | None) -> str:
    """级别归一:未知值一律当 `off`(不能因为 settings 里写错就让请求带上怪参数)。"""
    level = (value or "").strip().lower()
    return level if level in THINKING_LEVELS else "off"


def reasoning_text_of(obj: object) -> str:
    """从 litellm 的 delta / message 里取思考内容。

    各 provider 字段名不一:`reasoning_content`(DeepSeek/Qwen 系)、`reasoning`
    (OpenAI 兼容网关)、`thinking`(Anthropic 系,可能是块列表)。统一成字符串。
    """
    for attr in ("reasoning_content", "reasoning", "thinking"):
        value = getattr(obj, attr, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(attr)
        if not value:
            continue
        if isinstance(value, str):
            return value
        if isinstance(value, list):     # Anthropic 风格块列表
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("thinking") or item.get("text") or ""))
                else:
                    parts.append(str(getattr(item, "thinking", "") or ""))
            return "".join(parts)
        return str(value)
    return ""


async def chat_as_stream(llm: LLMClient, messages: list[ChatMessage],
                         tools: list[dict] | None = None,
                         temperature: float | None = None) -> AsyncIterator[LLMDelta]:
    """把一次非流式 `chat()` 合成为流:整段文本作为**单块** + 一个终止块。

    两处用途:(a) 实现方只有 chat(第三方/测试替身);(b) 流式打不开时的降级。
    """
    resp = await llm.chat(messages, tools=tools, temperature=temperature)
    if resp.reasoning:
        yield LLMDelta(reasoning=resp.reasoning)
    if resp.text:
        yield LLMDelta(text=resp.text)
    yield LLMDelta(finished=True, tool_calls=resp.tool_calls or [], usage=resp.usage or {})


async def stream_llm(llm: LLMClient, messages: list[ChatMessage],
                     tools: list[dict] | None = None,
                     temperature: float | None = None) -> AsyncGenerator[LLMDelta, None]:
    """统一调用入口:实现方有 `astream()` 就流式,否则用 chat 合成单块。

    返回**异步生成器**(而非裸 AsyncIterator):调用方需要在中断时 `aclose()` 它,
    把底层请求就地关掉。
    """
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


def provider_retry_params(retry: dict | None) -> dict:
    """`settings.json` 的 `retry.provider` → litellm 的**请求级**参数(对齐 pi 的形态)。

    pi 把超时与客户端重试放在 **provider/SDK 那一层**(`retry.provider.timeoutMs` /
    `maxRetries`),而不是套在整个回合外面 —— 一次慢请求只重试它自己,不会把整轮打成失败。
    qi 的 settings.json 与 pi 同名同形,所以直接读那两个键。

    未映射:pi 的 `maxRetryDelayMs`(litellm 没有对应参数)。
    """
    if not isinstance(retry, dict):
        return {}
    provider = retry.get("provider")
    if not isinstance(provider, dict):
        return {}
    out: dict = {}
    timeout_ms = provider.get("timeoutMs")
    if isinstance(timeout_ms, (int, float)) and not isinstance(timeout_ms, bool) and timeout_ms > 0:
        out["timeout"] = timeout_ms / 1000          # pi 用毫秒,litellm 用秒
    retries = provider.get("maxRetries")
    if isinstance(retries, int) and not isinstance(retries, bool) and retries >= 0:
        out["num_retries"] = retries
    return out


class LiteLLMClient:
    """基于 litellm 的实现。spec 来自 resolve_model()(经 resolve_key 取 key)。"""

    def __init__(self, spec: ResolvedModel, auth_store: AuthStore | None = None,
                 thinking_level: str = "off", retry: dict | None = None,
                 thinking_budgets: dict[str, int] | None = None):
        self.spec = spec
        # 请求级超时/重试:归 provider SDK,不归 agent loop(见 provider_retry_params)
        self._provider_retry = provider_retry_params(retry)
        self._resolved = resolve_key(spec.provider, spec.api_key_ref, auth_store or AuthStore())
        self.model_name = litellm_model_name(spec)
        self.thinking_level = normalize_thinking_level(thinking_level)
        #: `settings.thinkingBudgets`(按级别的 token 预算)—— 只对 Anthropic 形态生效
        self._thinking_budgets = dict(thinking_budgets or {})
        """思考级别(可在运行期改:TUI 的 shift+tab / `/thinking`)。"""
        # provider 明确拒过 reasoning_effort 后粘住这个事实,后续不再带(与 _no_usage_opt 同模式)
        self._no_reasoning_effort = False
        self.reasoning_dropped = False
        """曾因 provider 不接受而丢掉思考参数吗(供 UI 提示一次)。"""
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
        kwargs.update(self._provider_retry)         # timeout / num_retries(若配了)
        kwargs.update(self._reasoning_params())
        return kwargs

    def _reasoning_params(self) -> dict:
        """思考参数:只有模型声明 reasoning、级别非 off、且 provider 没拒过时才带。

        **Anthropic 形态**额外带 `thinking: {type, budget_tokens}`(pi 的 `thinkingBudgets`)。
        pi 对它分三路:Anthropic / Google / Bedrock **原生**用,OpenAI 兼容形态**只在**模型配了
        `compat.thinkingTokenBudgetField`(指定"预算写进哪个请求字段")时才用。qi 只做**原生那一路**
        (litellm 在 Anthropic 形态上有对应参数);OpenAI 兼容那半不做 —— qi 没有 compat 层,
        而加那层是独立的一件事(见 docs/cli.md §10)。
        """
        effort = _EFFORT_MAP.get(self.thinking_level)
        if not (effort and self.spec.reasoning and not self._no_reasoning_effort):
            return {}
        params: dict = {"reasoning_effort": effort}
        budget = self._thinking_budget()
        if budget is not None:
            params["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return params

    def _thinking_budget(self) -> int | None:
        """`settings.thinkingBudgets[当前级别]` → 要带的预算;不带则 None。

        三条规矩:

        * **只对 Anthropic 形态**(其余形态 litellm 没有统一参数);
        * **没配就不带** —— 不改变默认行为(pi 另有内置默认表,qi 刻不抄:那会让每个
          Anthropic 请求都凭空带上预算);
        * **钳制:至少留 1024 token 给答案** —— pi 的原话是 "clamped so at least 1024 tokens
          remain for the answer"。
        """
        if self.spec.api != "anthropic-messages":
            return None
        raw = self._thinking_budgets.get(self.thinking_level)
        if not isinstance(raw, int) or raw <= 0:
            return None
        if self.spec.max_tokens:
            return max(1024, min(raw, self.spec.max_tokens - 1024))
        return raw

    def _consider_reasoning_rejection(self, exc: BaseException) -> bool:
        """看这次失败是不是「provider 不接受 reasoning_effort」;是则降级并返回 True。

        真实场景:自建 LiteLLM 代理默认 `drop_params` 会把未知参数丢掉并**直接报错**。
        用户只是想调思考级别,不应该因此整轮失败——所以丢掉参数重试一次并记下来。
        """
        if self._no_reasoning_effort or "reasoning_effort" not in str(exc):
            return False
        self._no_reasoning_effort = True
        self.reasoning_dropped = True
        return True

    async def chat(self, messages: list[ChatMessage], tools: list[dict] | None = None,
                   temperature: float | None = None) -> ChatResponse:
        import litellm

        try:
            resp = await litellm.acompletion(**self._base_kwargs(messages, tools, temperature))
        except Exception as exc:
            if not self._consider_reasoning_rejection(exc):
                raise
            # 去掉 reasoning_effort 重试一次(用户只想调级别,不该因此整轮失败)
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
                            usage=usage_to_dict(getattr(resp, "usage", None)),
                            reasoning=reasoning_text_of(msg))

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

    async def _open_stream_with_fallbacks(self, messages: list[ChatMessage],
                                          tools: list[dict] | None,
                                          temperature: float | None) -> AsyncIterator[Any] | None:
        """开流,依次容忍两种“参数不被接受”:reasoning_effort → stream_options。

        顺序上先处理 reasoning(它是我们主动加的可选参数),再处理 usage 选项;
        两次都失败就返回 None,交给上层退回非流式。
        """
        for _ in range(3):
            with_usage = not self._no_usage_opt
            try:
                return await self._open_stream(messages, tools, temperature, with_usage)
            except Exception as exc:  # noqa: BLE001 打开流失败:逐项降级
                if self._consider_reasoning_rejection(exc):
                    continue
                if with_usage:
                    self._no_usage_opt = True     # 某些 provider 不认 stream_options
                    continue
                return None
        return None

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

        stream: AsyncIterator[Any] | None = await self._open_stream_with_fallbacks(
            messages, tools, temperature)
        if stream is None:
            self._stream_broken = True
            async for delta in chat_as_stream(self, messages, tools, temperature):
                yield delta
            return

        merged: dict[int, dict] = {}
        usage: dict = {}
        content_seen = False        # 文本或思考已吐出:此时失败不能重试(会重复输出)
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
                reasoning = reasoning_text_of(delta_obj)
                if reasoning:
                    content_seen = True
                    yield LLMDelta(reasoning=reasoning)
                if text:
                    content_seen = True
                    yield LLMDelta(text=text)
                for tc in getattr(delta_obj, "tool_calls", None) or []:
                    merge_tool_call_delta(merged, tc)
        except Exception:
            # 首片就失败 → 可安全降级;已吐过字/思考 → 不能重试(会重复输出)
            if content_seen:
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
