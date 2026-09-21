"""把会话的 HTML 传成**私有** GitHub gist(pi 的 `/share`)。

逐项核过 pi 的实现(`dist/modes/interactive/session-share.js`)后对齐的:

* **直调 API,不走 `gh` CLI** —— pi 就是这么做的,所以 qi 不引入对 `gh` 的依赖;
* 端点 `https://api.github.com/gists`,头是 `Authorization: Bearer <token>`;
* **`public: false`**(私有 gist),里面放一个 `.html` 文件 —— 内容就是
  `export_html.render_session_html()` 那份自包含 HTML。

token **顺序照 qi 的凭证总原则**(`docs/security.md`:auth store → 约定环境变量)—— 这也正是
pi 的做法:它从自己的凭证库取(`getAuthCredential`)。环境变量兜底,依次
`GITHUB_TOKEN` → `GH_TOKEN`。用 stdlib 的 `urllib.request`,不引新依赖。

**核过的 pi 流程**(`session-share.js`):导出 JSONL → 转 HTML → 读回 → 以
`public: false` + 一个 `.html` 文件 POST 到 `api.github.com/gists`。qi 少了前两步:
导出器直接吃 session 对象,产物同样是那份自包含 HTML(结果一致,少两个临时文件)。
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
