"""hikqin 的 HTTP 客户端:登录 / 续期 / 只读请求。

两条设计约束,都是为了把"会不会误用"变成结构问题而不是纪律问题:

1. **只读白名单**。`get()` 只接受 `READ_PATHS` 里的路径,`http.Client` 在本类里**只发 GET**
   和三个认证端点(POST `/auth/login`、`/auth/refresh`)。即使登录的是超管账号,
   这份代码也写不出破坏性请求。
2. **响应包装逐端点拆,不猜**。上游成功响应有三套形状(裸体 / `{code,message,data}` / 裸数组),
   见 `docs/hikqin-api.md` §2。`unwrap()` 只处理信封那一种,裸体与数组原样返回 ——
   一个"通吃"的拆包器会把裸体当信封拆掉,症状是"列表永远是空的"。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Callable

import httpx

from .config import Source
from .errors import NotLoggedIn, UpstreamError

#: 允许 GET 的路径前缀(逻辑路径,**不含** api_prefix)。白名单写成常量而不是在每个调用点判断,
#: 这样"新增一个会读的端点"必须显式改这里 —— 也就必须再过一遍"它会不会写"。
READ_PATHS = (
    "/auth/me",
    "/skills",
    "/mcp/servers",
    "/agents",
    "/a2a",
    "/.well-known",
    "/rag/kb",                      # /rag/kb/available、/{kb_id}/documents
)

TIMEOUT = 20.0


def is_readable(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in READ_PATHS)


def token_claims(token: str) -> dict[str, Any]:
    """**不验签**地解 JWT payload。

    只用于两件本地事:`whoami` 离线显示身份、自动续期前判断 `exp`。
    验签是服务端的事 —— 我们不拿它做权限判断,所以不引入密钥。
    """
    try:
        payload = token.split(".")[1]
    except IndexError:
        return {}
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def token_exp(token: str) -> int | None:
    value = token_claims(token).get("exp")
    return int(value) if isinstance(value, (int, float)) else None


def _auth_from_login(payload: dict[str, Any], login: str | None = None) -> dict[str, Any]:
    """把上游的登录响应变成我们的 `auth` 段(键名保持 snake 端点的形状,值是本地字段)。"""
    access = payload.get("access_token") or payload.get("access")
    refresh = payload.get("refresh_token") or payload.get("refresh")
    if not access:
        raise UpstreamError("登录响应里没有 access_token")
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    return {
        "login": user.get("username") or login,
        "role": user.get("role"),
        "access": str(access),
        "refresh": str(refresh) if refresh else None,
        "exp": token_exp(str(access)),
    }


def unwrap(payload: Any) -> Any:
    """拆 `{code,message,data}` 信封。裸体与列表**原样返回**(见模块 docstring)。"""
    if isinstance(payload, dict) and "code" in payload and "message" in payload:
        code = payload.get("code")
        if code not in (200, 0, None):
            raise UpstreamError(str(payload.get("message") or f"上游返回 code={code}"))
        return payload.get("data")
    return payload


class StoreClient:
    """一个源的客户端。`http` 可注入(`httpx.MockTransport`)—— 测试因此不碰网络。"""

    def __init__(
        self,
        source: Source,
        *,
        http: httpx.Client | None = None,
        token_override: str | None = None,
        on_auth_change: Callable[[dict[str, Any] | None], None] | None = None,
        timeout: float = TIMEOUT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.source = source
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout, follow_redirects=True)
        self._token_override = token_override
        self._on_auth_change = on_auth_change
        self._clock = clock

    # ── 生命周期 ──────────────────────────────────────────

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "StoreClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── 令牌 ──────────────────────────────────────────────

    @property
    def token(self) -> str | None:
        return self._token_override or self.source.access_token

    def _set_auth(self, auth: dict[str, Any] | None) -> None:
        self.source.auth = auth
        if self._on_auth_change is not None:
            self._on_auth_change(auth)

    # ── URL ──────────────────────────────────────────────

    def _url(self, path: str, *, prefixed: bool = True) -> str:
        base = self.source.base_url.rstrip("/")
        if not prefixed:
            return f"{base}{path}"
        prefix = self.source.api_prefix.rstrip("/")
        return f"{base}{prefix}{path}"

    # ── 底层请求 ──────────────────────────────────────────

    def _send(self, method: str, url: str, **kwargs: Any) -> tuple[httpx.Response, Any]:
        """发一次请求,返回 `(response, 解析后的 body)`。

        回 response 本身是为了 `get_bytes`（zip）—— 它要 `content`，但仍要同一套
        401/续期/错误映射。
        """
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"连不上 {url}:{exc}",
                                hint="检查 baseUrl 与网络;离线可用 --offline 读缓存") from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        return response, body

    def _fail(self, status: int, body: Any, *, url: str) -> None:
        message = None
        if isinstance(body, dict):
            message = body.get("message")
        raise UpstreamError(
            f"上游返回 {status}:{message or url}",
            status=status,
        )

    def _post_json(self, path: str, payload: dict[str, Any]) -> Any:
        """只给认证端点用(`/auth/login`、`/auth/refresh`)。"""
        if path not in ("/auth/login", "/auth/refresh"):
            raise UpstreamError(f"只读客户端不允许 POST {path}")
        response, body = self._send("POST", self._url(path), json=payload)
        if response.status_code >= 400:
            self._fail(response.status_code, body, url=path)
        return unwrap(body)

    def _read(self, path: str, params: dict[str, Any] | None = None, *,
              auth: bool = True, prefixed: bool = True) -> tuple[httpx.Response, Any]:
        """唯一的读口。先过白名单,再保证令牌新鲜,401 时续期重试**一次**。"""
        if not is_readable(path):
            # 白名单是**结构**约束:新增读端点必须显式改 READ_PATHS,顺便再审一遍"它会不会写"
            raise UpstreamError(f"内部错误:{path} 不在只读白名单里")
        headers = {}
        if auth:
            self._ensure_token()
            headers["Authorization"] = f"Bearer {self.token}"
        url = self._url(path, prefixed=prefixed)
        response, body = self._send("GET", url, params=params, headers=headers)
        if response.status_code == 401 and auth:
            self._refresh()
            headers["Authorization"] = f"Bearer {self.token}"
            response, body = self._send("GET", url, params=params, headers=headers)
            if response.status_code == 401:
                raise self._not_logged_in(body)
        if response.status_code >= 400:
            self._fail(response.status_code, body, url=path)
        return response, body

    def _get(self, path: str, params: dict[str, Any] | None = None, *,
             auth: bool = True, prefixed: bool = True) -> Any:
        return unwrap(self._read(path, params, auth=auth, prefixed=prefixed)[1])

    def _not_logged_in(self, body: Any) -> NotLoggedIn:
        detail = body.get("message") if isinstance(body, dict) else None
        return NotLoggedIn(
            f"认证失败:{detail or '上游拒绝了这个令牌'}",
            hint="重新登录:`qi store login`",
        )

    # ── 读口 ──────────────────────────────────────────────

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """读 `{api_prefix}/…` 下的端点(需要令牌)。"""
        return self._get(path, params)

    def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        """读二进制端点(目前只有 skill 的 zip 下载)。与 `get` 同一套白名单与续期。"""
        return self._read(path, params)[0].content

    def get_public(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """读**根路径**下的公开端点(目前只有 a2a;它不需要令牌)。"""
        return self._get(path, params, auth=False, prefixed=False)

    def me(self) -> dict[str, Any]:
        data = self._get("/auth/me")
        return data if isinstance(data, dict) else {}

    # ── 认证 ──────────────────────────────────────────────

    def login(self, login: str, password: str) -> dict[str, Any]:
        """登录 → 保存 → **立刻验证一次 `me()`**(两步都成功才算登录)。"""
        try:
            payload = self._post_json("/auth/login", {"login": login, "password": password})
        except UpstreamError as exc:
            # 密码错在退出码上不是"上游故障",而是"你该重新登"(3)—— 否则脚本分不清
            if exc.status in (401, 403):
                raise NotLoggedIn(str(exc), hint="检查用户名与密码") from exc
            raise
        if not isinstance(payload, dict):
            raise UpstreamError("登录响应不是对象")
        auth = _auth_from_login(payload, login)
        self._set_auth(auth)
        try:
            user = self.me()
        except (UpstreamError, NotLoggedIn):
            self._set_auth(None)      # 验证失败 → 不留下半条凭证
            raise
        if user.get("role"):
            auth["role"] = user["role"]
            self._set_auth(auth)
        return auth

    def use_token(self, token: str) -> dict[str, Any]:
        """CI 通道:`--token` 直接给令牌,不调登录端点(没有 refresh,过期要重新给)。"""
        auth = {"login": token_claims(token).get("username"), "role": None,
                "access": token, "refresh": None, "exp": token_exp(token)}
        self._token_override = None
        self._set_auth(auth)
        user = self.me()
        if user.get("role"):
            auth["role"] = user["role"]
            self._set_auth(auth)
        return auth

    def _refresh(self) -> None:
        refresh = self.source.refresh_token
        if not refresh:
            raise NotLoggedIn("凭证已过期,且这条凭证没有 refresh token",
                              hint="重新登录:`qi store login`")
        try:
            payload = self._post_json("/auth/refresh", {"refresh_token": refresh})
        except UpstreamError as exc:
            raise NotLoggedIn(
                "凭证已过期(7 天未使用)",
                hint="重新登录:`qi store login`",
            ) from exc
        if not isinstance(payload, dict):
            raise NotLoggedIn("续期响应不是对象", hint="重新登录:`qi store login`")
        merged = dict(self.source.auth or {})
        merged.update({k: v for k, v in _auth_from_login(
            {**payload, "user": None}, merged.get("login")).items() if v is not None})
        self._set_auth(merged)

    def _ensure_token(self) -> None:
        if not self.token:
            raise NotLoggedIn("还没有登录", hint="`qi store login`(a2a 不需要登录)")
        exp = token_exp(self.token)
        if exp is not None and exp - 60 <= self._clock():
            self._refresh()
