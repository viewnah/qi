"""请求发出去之前会经过的三件事:内置头、配置头、`openai-responses` 映射。

三条线上真实会踩的坑,各锁一条:

1. 不带 `x-opencode-session` 打 OpenCode(会话亲和 / prompt cache 失效)——
   以前要用户手装扩展补头,现在内置(`qi_agent/provider_headers.py`);
2. `openai-responses` 被当成 chat completions 发 —— litellm 只认
   `openai/responses/` 前缀才会把请求打到 `/v1/responses`;
3. models.json 的 `headers` 文档里写了、代码却没接线(`docs/models.md` 承诺过
   写法与 apiKey 一致:字面量 / `$ENV` / `!command`)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent.auth import AuthStore  # noqa: E402
from qi_agent.config import QiConfig, ResolvedModel, resolve_model  # noqa: E402
from qi_agent.llm import LiteLLMClient, ProviderAuthError, litellm_model_name  # noqa: E402
from qi_agent.provider_headers import builtin_provider_headers, is_opencode_endpoint  # noqa: E402


def _spec(**over) -> ResolvedModel:
    base = dict(provider="opencode", model="gpt-5.6-sol", api="openai-responses",
                base_url="https://opencode.ai/zen/v1", api_key_ref=None,
                reasoning=False, context_window=1_050_000, max_tokens=128_000)
    base.update(over)
    return ResolvedModel(**base)


# ── Responses 映射 ──────────────────────────────────────


def test_openai_responses_maps_to_the_litellm_responses_bridge():
    """`openai-responses` → `openai/responses/<model>`:litellm 拆出 provider=openai
    并把 mode 置为 responses(打 `/v1/responses`)。退回 `openai/<model>` 会变成
    chat completions —— OpenCode Zen 的 gpt-5.6-* 会直接不认。"""
    import litellm

    assert litellm_model_name(_spec()) == "openai/responses/gpt-5.6-sol"
    model, provider, _, _ = litellm.get_llm_provider(litellm_model_name(_spec()))
    assert provider == "openai"
    assert model.startswith("responses/")      # 前缀被抽掉、留成 responses 形态的模型名

    # 其它 api 类型不受影响(前缀表其余三行一个字没动)
    assert litellm_model_name(_spec(provider="deepseek", model="deepseek-v4.1-flash",
                                    api="openai-completions",
                                    base_url="https://api.deepseek.com")) == \
        "openai/deepseek-v4.1-flash"
    assert litellm_model_name(_spec(provider="anthropic", model="claude-sonnet-5-5",
                                    api="anthropic-messages",
                                    base_url="https://api.anthropic.com")) == \
        "anthropic/claude-sonnet-5-5"


# ── 内置 session 头 ─────────────────────────────────────


def test_builtin_session_header_hits_by_provider_name_or_hostname():
    assert builtin_provider_headers(_spec(), "sess-1") == {"x-opencode-session": "sess-1"}
    # provider 名不在名单、但地址是 opencode.ai 的(手建 provider 也要补头)
    by_url = builtin_provider_headers(
        _spec(provider="my-gateway", base_url="https://opencode.ai/zen/v1"), "s")
    assert by_url == {"x-opencode-session": "s"}
    assert is_opencode_endpoint(_spec(base_url="https://opencode.ai/zen/go/v1"))


def test_lookalike_host_does_not_get_the_session_header():
    """会话 id 是隐私:只按 **hostname 精确匹配**(或子域)发,不按子串。"""
    lookalike = _spec(provider="evil", base_url="https://opencode.ai.example.com/x/v1")
    assert builtin_provider_headers(lookalike, "sess-1") == {}
    other = _spec(provider="deepseek", model="deepseek-chat", api="openai-completions",
                  base_url="https://api.deepseek.com")
    assert builtin_provider_headers(other, "sess-1") == {}


@pytest.mark.asyncio
async def test_no_session_yet_falls_back_to_the_literal_qi():
    """没会话(冷启动/子运行)时回退字面量 `"qi"` —— 与 pi 的 opencode 实现同义。"""
    client = LiteLLMClient(_spec(), AuthStore())          # 不给 getter
    out = await client._prepare_headers({})
    assert out["extra_headers"] == {"x-opencode-session": "qi"}


@pytest.mark.asyncio
async def test_session_id_is_read_on_every_request():
    """getter **每次请求现取**:会话在运行期会换,构造时绑定会把旧会话的亲和头
    带给新会话(参考扩展当年正是这么修的)。"""
    current = {"id": "s1"}
    client = LiteLLMClient(_spec(), AuthStore(),
                           session_id_getter=lambda: current["id"])
    assert (await client._prepare_headers({}))["extra_headers"]["x-opencode-session"] == "s1"
    current["id"] = "s2"
    assert (await client._prepare_headers({}))["extra_headers"]["x-opencode-session"] == "s2"


# ── 配置头的接线与优先级 ────────────────────────────────


@pytest.mark.asyncio
async def test_config_headers_win_over_the_builtin_one(monkeypatch):
    """优先级:内置 < provider < 模型。配置里写过就听配置的(覆盖内置 session 头)。"""
    monkeypatch.setenv("QI_TRACE_VALUE", "trace-42")
    cfg = QiConfig.model_validate({
        "providers": {
            "opencode": {
                "baseUrl": "https://opencode.ai/zen/v1",
                "api": "openai-completions",
                "headers": {"x-opencode-session": "custom", "X-Extra": "$QI_TRACE_VALUE"},
                "models": [{"id": "gpt-5.6-sol", "headers": {"X-From-Model": "yes"}}],
            }
        }
    })
    spec = resolve_model(cfg, "opencode", "gpt-5.6-sol")
    client = LiteLLMClient(spec, AuthStore(), session_id_getter=lambda: "sess-1")
    out = await client._prepare_headers({})
    assert out["extra_headers"] == {
        "x-opencode-session": "custom",       # 配置覆盖内置
        "X-Extra": "trace-42",                # $ENV 在请求前解析
        "X-From-Model": "yes",                # 模型层也接上了
    }


@pytest.mark.asyncio
async def test_non_opencode_providers_get_no_extra_headers():
    spec = _spec(provider="deepseek", model="deepseek-chat", api="openai-completions",
                 base_url="https://api.deepseek.com")
    out = await LiteLLMClient(spec, AuthStore())._prepare_headers({})
    assert "extra_headers" not in out


@pytest.mark.asyncio
async def test_unresolvable_header_fails_before_the_request_without_the_value(monkeypatch):
    """`$ENV` 缺失 / `!command` 失败 → **请求前**报错,且消息里只有 header 名,
    没有值(与 API key 不泄漏的口径一致)。"""
    monkeypatch.delenv("QI_TEST_NO_SUCH_HEADER_ENV", raising=False)
    spec = _spec()
    spec.headers.update({"X-Trace": "$QI_TEST_NO_SUCH_HEADER_ENV"})
    client = LiteLLMClient(spec, AuthStore())
    with pytest.raises(ProviderAuthError) as excinfo:
        await client._prepare_headers({})
    msg = str(excinfo.value)
    assert "X-Trace" in msg and "opencode" in msg
    assert "$QI_TEST_NO_SUCH_HEADER_ENV" not in msg, "消息只该说哪个 header 坏了"


@pytest.mark.asyncio
async def test_non_ascii_header_value_is_rejected():
    """误粘贴的中文说明进 header → 请求前拦下(HTTP 头只许 latin-1)。"""
    spec = _spec()
    spec.headers.update({"X-Trace": "检测到角色目录(/x)但没有任何扩展读取它"})
    with pytest.raises(ProviderAuthError) as excinfo:
        await LiteLLMClient(spec, AuthStore())._prepare_headers({})
    assert "X-Trace" in str(excinfo.value)
    assert "非 ASCII" in str(excinfo.value)


# ── 扩展事件仍能压过一切 ────────────────────────────────


@pytest.mark.asyncio
async def test_extension_event_sees_and_can_replace_everything(tmp_path):
    """`before_provider_headers` 最后跑,看到的是已合好的完整 dict ——
    它能覆盖内置 session 头,也能用 None 删掉任意头。"""
    from qi_agent.extensions import ExtensionBus, ExtensionContext

    bus = ExtensionBus()

    async def _handler(payload, _ctx):
        headers = payload["headers"]
        headers["x-opencode-session"] = "from-extension"
        headers["X-Drop-Me"] = None            # 值 None = 删掉

    bus.on("before_provider_headers", _handler, source="test")
    spec = _spec()
    spec.headers.update({"X-Drop-Me": "configured"})
    client = LiteLLMClient(spec, AuthStore(),
                           session_id_getter=lambda: "sess-1",
                           bus=bus,
                           extension_ctx=lambda signal: ExtensionContext(cwd=tmp_path,
                                                                         signal=signal))
    out = await client._prepare_headers({})
    assert out["extra_headers"]["x-opencode-session"] == "from-extension"
    assert "X-Drop-Me" not in out["extra_headers"]


def test_runtime_wires_a_live_session_id_into_every_client(_isolate_user_state, tmp_path):
    """接线测试:`_make_client`(唯一建客户端入口)必须带上**现取**会话 id 的 getter。

    漏接的症状很隐蔽:内置头照发,但值永远是字面量 `"qi"` —— 会话亲和看着有、
    实际没有。这种"发了但发错了"的回归只能靠接线测试兜。
    """
    import json as _json
    from types import SimpleNamespace

    from qi_agent.runtime import QiRuntime

    agent = _isolate_user_state / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "models.json").write_text(_json.dumps({
        "providers": {"opencode": {
            "baseUrl": "https://opencode.ai/zen/v1",
            "api": "openai-completions",
            "apiKey": "$OPENCODE_API_KEY",
            "models": [{"id": "gpt-5.6-sol", "api": "openai-responses"}],
        }},
    }), encoding="utf-8")
    (agent / "settings.json").write_text(_json.dumps(
        {"defaultProvider": "opencode", "defaultModel": "gpt-5.6-sol"}),
        encoding="utf-8")

    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    runtime = QiRuntime(cwd=project, approve_project=True)

    client = runtime._make_client(resolve_model(runtime.cfg, "opencode", "gpt-5.6-sol"))
    assert client._session_id_getter is not None
    assert client._session_id_getter() is None          # 还没绑会话 → 层内回退 "qi"
    runtime._bound_session = SimpleNamespace(id="sess-live")
    assert client._session_id_getter() == "sess-live"   # 换会话后现取的就是新的
