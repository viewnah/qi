"""LLM 客户端(llm.py):协议 + litellm 实现(A4)。

一次 chat 返回:文本 或 工具调用列表。Router 与 Runner 共用(按 models.json 的 default/router)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

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
    async def chat(self, messages: list[ChatMessage], tools: list[dict] | None = None,
                   temperature: float | None = None) -> ChatResponse: ...


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


class LiteLLMClient:
    """基于 litellm 的实现。spec 来自 resolve_model()(经 resolve_key 取 key)。"""

    def __init__(self, spec: ResolvedModel, auth_store: AuthStore | None = None):
        self.spec = spec
        self._resolved = resolve_key(spec.provider, spec.api_key_ref, auth_store or AuthStore())
        self.model_name = litellm_model_name(spec)

    @property
    def ready(self) -> bool:
        return self._resolved.ok

    async def chat(self, messages: list[ChatMessage], tools: list[dict] | None = None,
                   temperature: float | None = None) -> ChatResponse:
        import litellm

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
        resp = await litellm.acompletion(**kwargs)
        msg = resp.choices[0].message
        text = msg.content or ""
        if isinstance(text, list):  # 多模态块:取文本
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        tool_calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCallOut(id=tc.id or f"call_{len(tool_calls)}",
                                          name=tc.function.name, args=args))
        return ChatResponse(text=text or "", tool_calls=tool_calls,
                            usage=getattr(resp, "usage", None))


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
