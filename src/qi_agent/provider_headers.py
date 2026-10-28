"""provider 级内置请求头(目前只有 OpenCode 一家有此需求)。

OpenCode(Zen / Go)要求每个请求带 `x-opencode-session` 作为会话标识(prompt-cache /
路由亲和用)。qi 把它做成**默认行为**:判定命中就自动注入,值 = 当前会话 id
(没有会话时回退字面量 `"qi"`,与 pi 的 `opencode-headers.js` 同义)。之前这要靠用户
手装扩展(`before_provider_headers` 里补头),现在内置了,扩展可以不装。

判定条件(比扩展里的 `in base_url` 子串判断严格):

* provider 名 ∈ {opencode, opencode-zen, opencode-go};或
* `base_url` 的**主机名**等于 `opencode.ai` 或其子域。

用 urlsplit 取 hostname 而不是子串匹配,是为了不让会话 id 泄漏给
`https://evil-opencode.ai.example.com` 这种地址。

用户覆盖:`models.json` 里给 provider / 模型写 `headers` 即可改掉 —— 优先级见
`llm.LiteLLMClient._prepare_headers`:内置 < provider < 模型 < 扩展事件。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .config import ResolvedModel

OPENCODE_SESSION_HEADER = "x-opencode-session"

#: 认得的 OpenCode provider 名(`opencode-zen` 是 pi/旧扩展里的叫法,见 presets.ALIASES)
OPENCODE_PROVIDER_IDS = frozenset({"opencode", "opencode-zen", "opencode-go"})

#: 信任的主机(精确或子域;不信任裸子串)
OPENCODE_HOSTS = ("opencode.ai",)


def is_opencode_endpoint(spec: ResolvedModel) -> bool:
    """这个 spec 打向 OpenCode 吗?(provider 名或 baseUrl 主机名命中其一即可)。"""
    if spec.provider.lower() in OPENCODE_PROVIDER_IDS:
        return True
    if not spec.base_url:
        return False
    host = (urlsplit(str(spec.base_url)).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in OPENCODE_HOSTS)


def builtin_provider_headers(spec: ResolvedModel, session_id: str | None) -> dict[str, str]:
    """内置默认头(空 dict = 与这个 provider 无关,什么都不加)。"""
    if not is_opencode_endpoint(spec):
        return {}
    return {OPENCODE_SESSION_HEADER: session_id or "qi"}
