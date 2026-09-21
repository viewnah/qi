"""`/share`:把会话传成**私有** GitHub gist(pi 的 `/share`)。

逐项核过 pi 的实现后对齐:`Authorization: Bearer <token>`、`public: false`、gist 里一个
`.html` 文件、**直调 API 而非 `gh` CLI**。

测试全部注入 `opener` —— **绝不联网**。
"""

from __future__ import annotations

import email.message
import io
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent.share import ShareError, resolve_token, share_session  # noqa: E402


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        title="我的会话", id="abc", created_at="2026", cwd="/p",
        branch=lambda: [{"type": "message", "role": "user", "content": "你好"}])


def _capture(url: str = "https://gist.github.com/u/1"):
    seen: list = []

    def opener(request):
        seen.append(request)
        return _Resp({"html_url": url})

    return seen, opener


def _no_env(monkeypatch) -> None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


# ── 请求的形状(与 pi 对齐的那几条) ────────────────────────────────────


def test_payload_is_a_private_single_html_gist(monkeypatch):
    _no_env(monkeypatch)
    seen, opener = _capture()
    url = share_session(_session(), token="t0ken", opener=opener)

    assert url == "https://gist.github.com/u/1"
    request = seen[0]
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == "Bearer t0ken"      # pi 同款头
    body = json.loads(request.data.decode("utf-8"))
    assert body["public"] is False                                 # **私有** —— pi 同
    assert list(body["files"]) == ["session.html"]
    assert "你好" in body["files"]["session.html"]["content"]
    assert body["files"]["session.html"]["content"].startswith("<!doctype html>")


def _isolated_home(tmp_path, monkeypatch):
    """把 `QI_AGENT_HOME` 指到临时目录 —— 否则 `AuthStore()` 会读用户真实的 auth.json。"""
    from qi_agent import paths

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return home


def test_token_order_is_auth_store_then_env(tmp_path, monkeypatch):
    """顺序照 qi 的凭证总原则:auth store → 环境变量(pi 也是从自己的凭证库取)。"""
    home = _isolated_home(tmp_path, monkeypatch)
    _no_env(monkeypatch)
    assert resolve_token() is None

    monkeypatch.setenv("GH_TOKEN", "from-gh")
    assert resolve_token() == ("from-gh", "环境变量 GH_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "from-github")
    preferred = resolve_token()
    assert preferred is not None and preferred[0] == "from-github", "GITHUB_TOKEN 优先于 GH_TOKEN"

    # auth store 存在时它赢(更具体的那一源)。用**真实写入接口** —— 第一版我手写
    # `{"github": {"key": ...}}`,少了 `type` 字段,于是 store 没命中、环境变量赢了,
    # 而那是我的 fixture 写错,不是实现的问题。
    from qi_agent.auth import AuthStore

    AuthStore().set_key("github", "from-store")
    stored = resolve_token()
    assert stored is not None and stored[0] == "from-store", stored


# ── 失败的可见性 ───────────────────────────────────────────────────────


def test_missing_token_says_what_to_do(monkeypatch):
    _no_env(monkeypatch)
    with pytest.raises(ShareError) as exc:
        share_session(_session())
    assert "GITHUB_TOKEN" in str(exc.value) and "gist" in str(exc.value)


def test_401_mentions_the_permission():
    def opener(request):
        raise urllib.error.HTTPError(
            "https://api.github.com/gists", 401, "Unauthorized",
            email.message.Message(), io.BytesIO(b'{"message": "Bad credentials"}'))

    with pytest.raises(ShareError) as exc:
        share_session(_session(), token="bad", opener=opener)
    assert "401" in str(exc.value) and "gist" in str(exc.value)
    assert "Bad credentials" in str(exc.value)


def test_offline_is_a_readable_error():
    def opener(request):
        raise urllib.error.URLError("nodename nor servname provided")

    with pytest.raises(ShareError) as exc:
        share_session(_session(), token="t", opener=opener)
    assert "连不上 GitHub" in str(exc.value)


def test_response_without_url_is_reported():
    def opener(request):
        return _Resp({})                     # 200 但没有 html_url

    with pytest.raises(ShareError, match="html_url"):
        share_session(_session(), token="t", opener=opener)


def test_share_is_a_real_command_now():
    from qi_agent.tui import PLANNED_COMMANDS, TUI_COMMANDS

    assert "/share" in TUI_COMMANDS
    assert PLANNED_COMMANDS == frozenset(), "pi 的命令面已全部对齐,不该再有「计划中」"
