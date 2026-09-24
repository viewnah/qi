"""API key 入口校验:显然不是密钥的值在**写入时**就拒绝。

真实事故:把一段中文提示误粘贴进了 `/login` 的密钥框、落进 `auth.json`;之后某个回合
才以 `InternalServerError: 'ascii' codec can't encode characters in position 7-13`
暴露 —— 离“输入”已经很远(litellm 拼出 `Authorization: Bearer 检测到角色目录…`)。
所以校验放在 `AuthStore.set_key`,而不是等请求时。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent.auth import AuthStore, key_problem  # noqa: E402


def test_key_problem_accepts_real_looking_keys():
    assert key_problem("sk-abc123-XYZ_09.+/=") is None
    assert key_problem("ghp_" + "a" * 40) is None


@pytest.mark.parametrize("bad", [
    "", "   ",
    "sk-a b",                                   # 空格
    "sk-a\nb",                                  # 换行
    "检测到角色目录(/x)但没有任何扩展读取它",     # 误粘贴的中文提示
    "sk-键",                                    # 单个非 ASCII 字符
])
def test_key_problem_rejects_obvious_non_keys(bad):
    assert key_problem(bad)


def test_set_key_refuses_a_pasted_warning(tmp_path):
    store = AuthStore(tmp_path / "auth.json")
    with pytest.raises(ValueError):
        store.set_key("deepseek", "检测到角色目录(/x)但没有任何扩展读取它")
    assert not (tmp_path / "auth.json").exists(), "被拒的值不该落盘"


def test_set_key_still_stores_a_real_looking_key(tmp_path):
    store = AuthStore(tmp_path / "auth.json")
    store.set_key("deepseek", "sk-real-key")
    assert store.get("deepseek") == "sk-real-key"


def test_resolve_key_flags_an_already_stored_bad_key(tmp_path):
    """旧版本/手工编辑落下的坏值:解析时不隐藏 provider,但把问题标出来。"""
    from qi_agent.auth import resolve_key

    store = AuthStore(tmp_path / "auth.json")
    store.save({"deepseek": {"type": "api_key", "key": "sk-中文"}})
    rk = resolve_key("deepseek", store=store)
    assert rk.ok and rk.key == "sk-中文"          # 来源还在 → 仍可被选中/诊断
    assert rk.problem and "非 ASCII" in rk.problem
    assert "无效" in rk.describe()


@pytest.mark.asyncio
async def test_client_refuses_a_bad_key_before_the_request(tmp_path):
    """请求**之前**就报“你的 key 有问题”,而不是等 provider 侧的 ascii 错误。"""
    from qi_agent.config import ResolvedModel
    from qi_agent.llm import ChatMessage, LiteLLMClient, ProviderAuthError

    store = AuthStore(tmp_path / "auth.json")
    store.save({"deepseek": {"type": "api_key", "key": "检测到角色目录(误粘贴)"}})
    spec = ResolvedModel(provider="deepseek", model="deepseek-chat", api="openai-completions",
                         base_url=None, api_key_ref=None, reasoning=False,
                         context_window=8192, max_tokens=1024)
    client = LiteLLMClient(spec, store)
    with pytest.raises(ProviderAuthError) as excinfo:
        await client.chat([ChatMessage(role="user", content="hi")])
    msg = str(excinfo.value)
    assert "API key" in msg and "deepseek" in msg and "auth" in msg


def test_friendly_provider_error_translates_the_ascii_codec_failure():
    """底层那句 `'ascii' codec …` 要翻成直接指向凭证的话。"""
    from qi_agent.llm import ProviderAuthError, friendly_provider_error

    raw = RuntimeError(
        "litellm.InternalServerError: InternalServerError: OpenAIException - "
        "'ascii' codec can't encode characters in position 7-13: ordinal not in range(128)")
    friendly = friendly_provider_error(raw)
    assert isinstance(friendly, ProviderAuthError)
    assert "API key" in str(friendly)


def test_friendly_provider_error_leaves_other_errors_alone():
    from qi_agent.llm import friendly_provider_error

    raw = RuntimeError("connection reset")
    assert friendly_provider_error(raw) is raw
