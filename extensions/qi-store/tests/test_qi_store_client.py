"""客户端:登录、只读白名单、信封拆包、续期。

全部用 `httpx.MockTransport` —— 测试不碰网络,也不依赖那台实例活着。
"""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from qi_store.client import StoreClient, is_readable, token_claims, token_exp, unwrap
from qi_store.config import Source
from qi_store.errors import NotLoggedIn, UpstreamError


def jwt(exp: int | None = None, username: str = "admin", role: str = "admin") -> str:
    payload = {"username": username, "role": role}
    if exp is not None:
        payload["exp"] = exp
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{raw}.sig"


def client_for(handler, *, base_url="http://h:6091", auth=None, **kwargs) -> StoreClient:
    return StoreClient(
        Source(name="hikqin", base_url=base_url, auth=auth),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_whitelist_rejects_unlisted_paths():
    assert is_readable("/skills")
    assert is_readable("/skills/4/files")
    assert is_readable("/mcp/servers/3")
    assert not is_readable("/agents")
    assert not is_readable("/skillsx")          # 前缀相似但不同段,不算
    assert not is_readable("/a2ax/1")


def test_get_refuses_non_whitelisted_path():
    def handler(request):                       # pragma: no cover - 不该被调用
        raise AssertionError("不该发请求")

    with client_for(handler) as client:
        with pytest.raises(UpstreamError) as excinfo:
            client.get("/admin/overview")
    assert "只读白名单" in str(excinfo.value)


def test_post_is_limited_to_auth_endpoints():
    with client_for(lambda request: httpx.Response(200, json={})) as client:
        with pytest.raises(UpstreamError):
            client._post_json("/skills", {})


def test_no_token_asks_to_login():
    with client_for(lambda request: httpx.Response(200, json={})) as client:
        with pytest.raises(NotLoggedIn) as excinfo:
            client.get("/skills")
    assert "qi store login" in (excinfo.value.hint or "")


def test_login_saves_auth_and_verifies_with_me():
    calls: list[str] = []
    saved: list[dict | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/auth/login":
            assert json.loads(request.read()) == {"login": "admin", "password": "pw"}
            return httpx.Response(200, json={
                "access_token": jwt(int(time.time()) + 3600),
                "refresh_token": "refresh-1",
                "token_type": "bearer",
                "user": {"username": "admin", "role": "admin"},
            })
        if request.url.path == "/api/auth/me":
            assert request.headers["Authorization"].startswith("Bearer ")
            return httpx.Response(200, json={"username": "admin", "role": "admin"})
        return httpx.Response(404, json={"code": 404, "message": "no"})

    with client_for(handler, on_auth_change=saved.append) as client:
        auth = client.login("admin", "pw")

    assert auth["login"] == "admin"
    assert auth["refresh"] == "refresh-1"
    assert auth["role"] == "admin"
    assert calls == ["POST /api/auth/login", "GET /api/auth/me"]   # 先验后记:两步都真跑了
    assert saved and saved[-1] == auth


def test_failed_verification_leaves_no_credentials():
    saved: list[dict | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": jwt(int(time.time()) + 60)})
        return httpx.Response(401, json={"code": 401, "message": "无效的认证凭据"})

    with client_for(handler, on_auth_change=saved.append) as client:
        with pytest.raises(NotLoggedIn):
            client.login("admin", "pw")
    assert saved[-1] is None        # 验证没过 → 不留下半条凭证


def test_expired_token_is_refreshed_before_the_request():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/auth/refresh":
            assert json.loads(request.read())["refresh_token"] == "r1"
            return httpx.Response(200, json={"access_token": jwt(int(time.time()) + 3600),
                                             "refresh_token": "r2"})
        assert request.headers["Authorization"].endswith(jwt(int(time.time()) + 3600))
        return httpx.Response(200, json={"total": 0, "page": 1, "page_size": 20, "items": []})

    source_auth = {"access": jwt(int(time.time()) - 10), "refresh": "r1", "login": "admin"}
    with client_for(handler, auth=source_auth) as client:
        client.get("/skills")
    assert calls == ["/api/auth/refresh", "/api/skills"]


def test_refresh_without_refresh_token_is_a_login_problem():
    with client_for(lambda request: httpx.Response(200, json={}),
                    auth={"access": jwt(int(time.time()) - 10)}) as client:
        with pytest.raises(NotLoggedIn) as excinfo:
            client.get("/skills")
    assert "refresh token" in str(excinfo.value)


def test_401_on_read_refreshes_once_then_gives_up():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/auth/refresh":
            return httpx.Response(200, json={"access_token": jwt(int(time.time()) + 3600)})
        return httpx.Response(401, json={"code": 401, "message": "无效的认证凭据"})

    auth = {"access": jwt(int(time.time()) + 3600), "refresh": "r1"}
    with client_for(handler, auth=auth) as client:
        with pytest.raises(NotLoggedIn) as excinfo:
            client.get("/skills")
    assert calls.count("/api/skills") == 2         # 只重试一次
    assert "无效的认证凭据" in str(excinfo.value)


def test_envelope_is_unwrapped_but_bare_bodies_are_not():
    assert unwrap({"code": 200, "message": "ok", "data": {"total": 1}}) == {"total": 1}
    bare = {"total": 0, "page": 1, "page_size": 20, "items": []}
    assert unwrap(bare) is bare                    # 裸体原样:拆错了症状是"列表永远空"
    assert unwrap([{"name": "x"}]) == [{"name": "x"}]
    with pytest.raises(UpstreamError):
        unwrap({"code": 400, "message": "参数错误", "data": None})


def test_upstream_5xx_is_reported_with_body_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": 500, "message": "服务器内部错误"})

    with client_for(handler, auth={"access": jwt()}) as client:
        with pytest.raises(UpstreamError) as excinfo:
            client.get("/skills")
    assert excinfo.value.status == 500
    assert "服务器内部错误" in str(excinfo.value)


def test_public_a2a_needs_no_token_and_no_api_prefix():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json=[{"name": "技能调试"}])

    with client_for(handler) as client:            # 注意:没给 auth
        cards = client.get_public("/a2a/agents")
    assert cards == [{"name": "技能调试"}]
    assert seen == {"path": "/a2a/agents", "auth": ""}


def test_network_failure_is_an_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with client_for(handler, auth={"access": jwt()}) as client:
        with pytest.raises(UpstreamError) as excinfo:
            client.get("/skills")
    assert "连不上" in str(excinfo.value)


def test_token_claims_tolerates_garbage():
    assert token_claims("not-a-jwt") == {}
    assert token_claims("a.!!!.c") == {}
    assert token_exp(jwt(1234)) == 1234
    assert token_exp("x") is None
