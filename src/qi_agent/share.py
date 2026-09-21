"""把会话的 HTML 传成**私有** GitHub gist(pi 的 `/share` 的**一条**路径)。

**读完全部 183 行后修正的两处**(第一版我写错了,已改):

1. pi 的 gist 路径**就是 shell 出 `gh`**(`spawnSync("gh", ["auth","status"])` 先查登录,
   再 `gh gist create --public=false <file>`);它用 `getAuthCredential` 取的 token 是给
   **另一条路径**(pi 自建的 Radius 服务)的,不是给 gist 的。
2. pi 的 `/share` 有**两条**路径:先试 Radius(上传 **JSONL** + 注入分享元数据
   `systemPrompt` / 工具定义),失败才退 gist(上传 **HTML**)。半径那条是 pi 的托管服务,
   qi 没有对应物 —— 所以 qi 只做 gist 这条,并且在 §10 记档。

qi 与 pi 的**刻意偏离**:qi **直调 API**(stdlib `urllib`),不 shell 出 `gh` —— 少一个外部
依赖,也不用要求用户先 `gh auth login`。代价是没有 pi 的 viewer 预览链接(那需要 pi 的
服务),qi 只能给 gist 链接本身。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

GIST_API = "https://api.github.com/gists"
#: 环境变量顺序(与 qi 的凭证总原则一致:先约定环境变量)
_TOKEN_ENVS = ("GITHUB_TOKEN", "GH_TOKEN")


class ShareError(Exception):
    """分享失败 —— 消息是给用户看的(说清下一步该做什么)。"""


def resolve_token(store: Any = None) -> tuple[str, str] | None:
    """→ `(token, 来源)`;都没有则 None。来源要报出来(用户需要知道用的是哪一个)。

    **顺序照 qi 的凭证总原则**(`docs/security.md`):auth store → 约定环境变量。
    (pi 也是从自己的凭证库取 —— 这一点是读它的实现确认的,不是猜的。)
    """
    try:
        from .auth import AuthStore

        key = (store or AuthStore()).get("github")
    except Exception:                      # noqa: BLE001 凭证库读不了不该让分享崩在别处
        key = None
    if key:
        return str(key), "auth store(qi auth login github)"
    for name in _TOKEN_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, f"环境变量 {name}"
    return None


def share_session(session: Any, *, token: str | None = None, title: str | None = None,
                  opener: Callable[[Any], Any] | None = None) -> str:
    """把会话的当前分支传成私有 gist,返回可分享的 URL。

    `opener` 只为测试可注入(默认 `urllib.request.urlopen`)—— **测试里绝不联网**。
    """
    from .export_html import render_session_html

    if token is None:
        found = resolve_token()
        if found is None:
            raise ShareError(
                "没有 GitHub token:设 `GITHUB_TOKEN`(需要一个带 `gist` 权限的 token),"
                "或 `qi auth login github`")
        token = found[0]
    body = json.dumps({
        "description": title or f"qi 会话 {getattr(session, 'id', '') or ''}".strip(),
        "public": False,                     # 私有 —— pi 同
        "files": {"session.html": {"content": render_session_html(session, title=title)}},
    }).encode("utf-8")
    request = urllib.request.Request(
        GIST_API, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "qi"})
    try:
        with (opener or urllib.request.urlopen)(request) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        # 这段里**不用** `or` / `and`(ast-grep 的 no-boolean-in-except 会拦;而且
        # 三元表达式把意图写得更直白)
        detail = ""
        try:
            message = json.loads(exc.read().decode("utf-8")).get("message")
            detail = str(message) if message else ""
        except Exception:                  # noqa: BLE001 响应体不是 JSON 时只留状态码
            detail = ""
        if exc.code in (401, 403):
            why = detail if detail else "权限不足"
            raise ShareError(f"GitHub 拒绝了({exc.code} {why}):"
                             "token 需要 `gist` 权限") from exc
        suffix = f" {detail}" if detail else ""
        raise ShareError(f"GitHub 返回 {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise ShareError(f"连不上 GitHub(离线或代理问题):{exc.reason}") from exc
    url = str(payload.get("html_url") or "")
    if not url:
        raise ShareError("GitHub 没回可分享的链接(响应里没有 html_url)")
    return url


def share_path(session: Any, out: Path, *, title: str | None = None, **kw: Any) -> str:
    """先写一份本地 HTML 再把**它**传上去(便于用户留档与分享同一份内容)。"""
    from .export_html import write_session_html

    write_session_html(session, out, title=title)
    return share_session(session, title=title, **kw)
