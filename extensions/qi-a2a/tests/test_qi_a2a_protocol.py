"""qi-a2a:名片解析与 SDK 客户端聚合。

协议本身**不在这里测**(那是官方 `a2a-sdk` 的职责),这里钉的是:
* 名片 JSON → proto 的归一(驼峰 / tenant / 技能);
* SDK 的请求线格式(方法名、`A2A-Version` 头、`params.tenant`)—— 用 MockTransport 看;
* 流式事件 → `SendResult` 的聚合语义(尤其 `append`);
* 错误翻译(HITL / JSON-RPC error / HTTP error)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # tests/ → qi-a2a/ → extensions/ → 仓库根
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-a2a"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from qi_a2a.card import (  # noqa: E402
    AgentCard,
    CardError,
    card_from_dict,
    card_from_entry,
    cards_from_payload,
    discovery_url,
    select_card,
)
from qi_a2a.client import A2AClient, RpcError  # noqa: E402
from qi_a2a.config import A2AEntry  # noqa: E402


def entry(**cfg) -> A2AEntry:
    return A2AEntry(name="gw", config={"baseUrl": "http://gw:8080", **cfg})


def card(**overrides) -> dict:
    base = {
        "name": "数据分析助手",
        "description": "跑 SQL 并给结论",
        "version": "1.0.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [{"id": "s1", "name": "数据分析", "description": "SQL", "tags": ["分析"]}],
        "supportedInterfaces": [{
            "url": "http://gw:8080/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
            "tenant": "agent-1",
        }],
    }
    base.update(overrides)
    return base


# ── 名片 ────────────────────────────────────────────────

def test_discovery_url_prefers_card_url():
    assert discovery_url(entry(cardUrl="http://x/card.json")) == "http://x/card.json"
    assert discovery_url(entry(tenant="a1")) == "http://gw:8080/a2a/agents/a1/card.json"
    assert discovery_url(entry()) == "http://gw:8080/a2a/agents"


def test_card_parses_the_shape_hikqin_actually_serves():
    """实测:Hikqin 用驼峰 + `supportedInterfaces[].tenant`,SDK 的 proto 能直接吃。"""
    parsed = card_from_dict(card(), entry())
    assert parsed.name == "数据分析助手"
    assert parsed.tenant == "agent-1"
    assert parsed.rpc_url == "http://gw:8080/a2a"
    assert parsed.skills[0]["name"] == "数据分析"
    assert parsed.skills[0]["tags"] == ["分析"]


def test_card_without_interfaces_falls_back_to_config():
    parsed = card_from_dict({"name": "x"}, entry(tenant="fixed", rpcUrl="http://gw/rpc"))
    assert parsed.rpc_url == "http://gw/rpc"
    assert parsed.tenant == "fixed"


def test_card_without_endpoint_anywhere_is_a_readable_error():
    with pytest.raises(CardError, match="supportedInterfaces"):
        card_from_dict({"name": "x"}, A2AEntry(name="gw", config={"cardUrl": "http://x/c"}))


def test_synthetic_card_carries_protocol_binding_and_streaming():
    """`rpcUrl` 条目不取名片 —— 合成卡必须自带 SDK 建客户端要的两样。

    `protocolBinding` 空着会让 `ClientFactory.create` 报
    `no compatible transports found`;`capabilities.streaming` 缺省 False
    会让 SDK 安静地改用非流式 `SendMessage`。
    """
    synthetic = card_from_entry(entry(rpcUrl="http://gw/rpc", tenant="a1"))
    assert synthetic.tenant == "a1"
    assert synthetic.proto.supported_interfaces[0].protocol_binding == "JSONRPC"
    assert synthetic.proto.capabilities.streaming is True

    # 显式关掉流式就照配置走
    assert card_from_entry(entry(rpcUrl="http://gw/rpc", streaming=False)
                           ).proto.capabilities.streaming is False


def test_card_without_capabilities_inherits_streaming_intent():
    """卡片省略 capabilities 时按配置补 —— 否则会静默降级为非流式。"""
    raw = {"name": "x", "supportedInterfaces": [{"url": "http://gw/a2a"}]}
    assert card_from_dict(raw, entry()).proto.capabilities.streaming is True
    assert card_from_dict(raw, entry(streaming=False)).proto.capabilities.streaming is False


def test_card_keeps_capabilities_declared_by_the_server():
    """服务端明确声明不支持流式时,不被配置盖掉。"""
    raw = card(capabilities={"streaming": False})
    assert card_from_dict(raw, entry()).proto.capabilities.streaming is False


def test_list_payload_expands_to_many_cards():
    cards = cards_from_payload([card(), card(
        name="另一个", supportedInterfaces=[{"url": "http://gw:8080/a2a",
                                             "tenant": "agent-2"}])], entry())
    assert [c.tenant for c in cards] == ["agent-1", "agent-2"]


def test_configured_tenant_filters_the_list():
    payload = [card(), card(name="另一个",
                            supportedInterfaces=[{"url": "http://gw/a2a", "tenant": "agent-2"}])]
    cards = cards_from_payload(payload, entry(tenant="agent-2"))
    assert [c.tenant for c in cards] == ["agent-2"]


def test_missing_tenant_is_a_readable_error():
    payload = [card(supportedInterfaces=[{"url": "http://gw/a2a", "tenant": "agent-2"}])]
    with pytest.raises(CardError, match="agent-1"):
        cards_from_payload(payload, entry(tenant="agent-1"))


def test_single_mode_rejects_multiple_cards():
    payload = [card(), card(name="b", supportedInterfaces=[{"url": "http://u", "tenant": "b"}])]
    with pytest.raises(CardError, match="discovery=single"):
        cards_from_payload(payload, entry(discovery="single"))


def test_select_card_by_name_or_tenant():
    cards = cards_from_payload([card(), card(
        name="另一个", supportedInterfaces=[{"url": "http://u", "tenant": "agent-2"}])], entry())
    assert select_card(cards, "agent-2").name == "另一个"
    assert select_card(cards, "数据分析助手").tenant == "agent-1"
    with pytest.raises(CardError, match="agent"):
        select_card(cards)
    with pytest.raises(CardError, match="找不到"):
        select_card(cards, "nope")


# ── 客户端(SDK 之上)────────────────────────────────────

def _sse(*results: dict) -> bytes:
    frames = [json.dumps({"jsonrpc": "2.0", "id": "1", "result": r}) for r in results]
    return b"".join(b"data: " + frame.encode() + b"\n\n" for frame in frames)


def _client(handler, *, target: A2AEntry | None = None) -> A2AClient:
    return A2AClient(target or entry(), transport=httpx.MockTransport(handler))


# 实测的帧序列:`append` 缺省 = 替换,后续 `append: true` = 追加
STREAM = [
    {"task": {"id": "t1", "contextId": "c1",
              "status": {"state": "TASK_STATE_SUBMITTED"}}},
    {"statusUpdate": {"taskId": "t1", "contextId": "c1",
                      "status": {"state": "TASK_STATE_WORKING"}}},
    {"artifactUpdate": {"taskId": "t1", "contextId": "c1",
                        "artifact": {"artifactId": "reply", "parts": [{"text": "你"}]}}},
    {"artifactUpdate": {"taskId": "t1", "contextId": "c1",
                        "artifact": {"artifactId": "reply", "parts": [{"text": "好"}]},
                        "append": True, "lastChunk": True}},
    {"statusUpdate": {"taskId": "t1", "contextId": "c1",
                      "status": {"state": "TASK_STATE_COMPLETED"}}},
]


@pytest.mark.asyncio
async def test_send_aggregates_stream_and_speaks_sdk_wire_format():
    seen: list[dict] = []
    headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        headers.append(dict(request.headers))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse(*STREAM))

    async with _client(handler) as client:
        result = await client.send(card_from_dict(card(), entry()), "分析一下")

    assert result.text == "你好"
    assert result.state == "completed"
    assert (result.task_id, result.context_id) == ("t1", "c1")

    # SDK 的方法名 / 版本头 / tenant 注入 —— 这三样以前是我们手写的
    assert seen[0]["method"] == "SendStreamingMessage"
    assert headers[0]["a2a-version"] == "1.0"
    assert seen[0]["params"]["tenant"] == "agent-1"
    assert seen[0]["params"]["message"]["parts"] == [{"text": "分析一下"}]


@pytest.mark.asyncio
async def test_first_artifact_without_append_replaces_later_ones_append():
    """`append` 缺省 = 替换(proto 语义);真实 Hikqin 正是这样发首片的。"""
    frames = [
        {"artifactUpdate": {"taskId": "t1", "artifact": {"parts": [{"text": "第一段"}]}}},
        {"artifactUpdate": {"taskId": "t1", "artifact": {"parts": [{"text": "第二段"}]},
                            "append": True}},
        {"statusUpdate": {"taskId": "t1", "status": {"state": "TASK_STATE_COMPLETED"}}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse(*frames))

    async with _client(handler) as client:
        result = await client.send(card_from_dict(card(), entry()), "x")

    assert result.text == "第一段第二段"


@pytest.mark.asyncio
async def test_non_streaming_uses_send_message():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {
            "message": {"messageId": "m1", "parts": [{"text": "hi"}]}}})

    target = entry(streaming=False)
    async with _client(handler, target=target) as client:
        result = await client.send(card_from_dict(card(), target), "x")

    assert seen[0]["method"] == "SendMessage"
    assert (result.text, result.state) == ("hi", "completed")


@pytest.mark.asyncio
async def test_input_required_carries_hitl_payload():
    event = {"statusUpdate": {"taskId": "t1", "contextId": "c1", "status": {
        "state": "TASK_STATE_INPUT_REQUIRED",
        "message": {"parts": [{"data": {
            "action_requests": [{"id": "a1", "name": "delete_file",
                                 "args": {"path": "t.txt"}}],
            "review_configs": [{"allowed_decisions": ["approve", "reject"]}],
        }}]},
    }}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse(event))

    async with _client(handler) as client:
        result = await client.send(card_from_dict(card(), entry()), "删文件")

    assert result.state == "input-required"
    assert result.interrupt["action_requests"][0]["name"] == "delete_file"
    assert result.interrupt["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]


@pytest.mark.asyncio
async def test_resume_sends_decisions_as_a_data_part():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse(*STREAM))

    async with _client(handler) as client:
        await client.send(card_from_dict(card(), entry()), "",
                          task_id="t1", decisions=[{"type": "approve"}])

    message = seen[0]["params"]["message"]
    assert message["taskId"] == "t1"
    assert message["parts"] == [{"data": {"decisions": [{"type": "approve"}]}}]


@pytest.mark.asyncio
async def test_jsonrpc_error_becomes_readable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1",
                                         "error": {"code": -32002,
                                                   "message": "Task not resumable"}})

    async with _client(handler) as client:
        with pytest.raises(RpcError, match="not resumable"):
            await client.send(card_from_dict(card(), entry()), "x")


@pytest.mark.asyncio
async def test_http_error_includes_status_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async with _client(handler) as client:
        with pytest.raises(RpcError, match="404"):
            await client.get_json("http://gw/missing")


@pytest.mark.asyncio
async def test_get_task_returns_a_dict():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {
            "id": "t1", "contextId": "c1", "status": {"state": "TASK_STATE_WORKING"}}})

    async with _client(handler) as client:
        result = await client.get_task(card_from_dict(card(), entry()), "t1")

    assert seen[0]["method"] == "GetTask"
    assert seen[0]["params"]["id"] == "t1"
    assert result["status"]["state"] == "TASK_STATE_WORKING"
