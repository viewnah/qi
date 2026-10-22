"""`kb-auth.json`（等价于 qi 的 `auth.json`）+ 凭证解析 / 刷新 / 登录。

一个 provider 的凭证解析顺序：

1. `QI_KB_TOKEN_<PROVIDER>`（只有一个 provider 时也认 `QI_KB_TOKEN`）
2. `kb.json.providers.<p>.apiKey` 引用（字面量 / `$ENV` / `!command`）
3. `kb-auth.json.providers.<p>.access`
4. 都没有 → NotLoggedIn

`kb-auth.json` 只放用户级、写时 0600。刷新会**同时覆盖 access 与 refresh**
（上游旋转 refresh；只更新 access 会让凭证在 7 天后突然失效）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from . import config, paths
from .errors import ConfigError, NotLoggedIn, UpstreamError

TIMEOUT = 20.0
_ENV_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


# ── JWT / 信封 ────────────────────────────────────────────


def token_claims(token: str) -> dict[str, Any]:
    """不验签地解 JWT payload；只用于本地 exp / 身份显示。"""
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


def unwrap(payload: Any) -> Any:
    """拆 `{code,message,data}` 信封；裸体 / 数组原样返回。"""
    if isinstance(payload, dict) and "code" in payload and "message" in payload:
        code = payload.get("code")
        if code not in (200, 0, None):
            raise UpstreamError(str(payload.get("message") or f"上游返回 code={code}"),
                                status=code if isinstance(code, int) else None)
        return payload.get("data")
    return payload


def api_url(provider: dict[str, Any], path: str) -> str:
    base = config.provider_base_url(provider)
    prefix = config.provider_api_prefix(provider).strip("/")
    suffix = "/" + path.lstrip("/")
    return f"{base}/{prefix}{suffix}" if prefix else f"{base}{suffix}"


def resolve_value(raw: str, env: Mapping[str, str] | None = None) -> str | None:
    """解析 apiKey / header 值：字面量 / `$ENV` / `!command`。"""
    env_vars = env if env is not None else os.environ
    if raw.startswith("$$"):
        return raw[1:]
    if raw.startswith("$!"):
        return raw[1:]
    if raw.startswith("!"):
        try:
            argv = shlex.split(raw[1:])
        except ValueError:
            return None
        if not argv:
            return None
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None
    missing = False

    def repl(match: re.Match[str]) -> str:
        nonlocal missing
        name = match.group(1) or match.group(2)
        value = env_vars.get(name)
        if value is None:
            missing = True
            return ""
        return value

    result = _ENV_RE.sub(repl, raw)
    return None if missing else result


# ── auth store ────────────────────────────────────────────


class AuthStore:
    """`~/.qi/agent/kb-auth.json`：按 provider 存 access / refresh。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.auth_file()

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"kb-auth.json 不是合法 JSON：{self.path}", hint=str(exc)) from exc
        return raw if isinstance(raw, dict) else {}

    def providers(self) -> dict[str, dict[str, Any]]:
        raw = self.load().get("providers")
        return raw if isinstance(raw, dict) else {}

    def get(self, name: str) -> dict[str, Any] | None:
        entry = self.providers().get(name)
        return dict(entry) if isinstance(entry, dict) else None

    def save(self, data: dict[str, Any]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self.path

    def set(self, name: str, entry: dict[str, Any]) -> Path:
        raw = self.load()
        providers = raw.get("providers")
        if not isinstance(providers, dict):
            providers = {}
            raw["providers"] = providers
        providers[name] = entry
        return self.save(raw)

    def remove(self, name: str) -> Path:
        raw = self.load()
        providers = raw.get("providers")
        if isinstance(providers, dict):
            providers.pop(name, None)
            if not providers:
                raw.pop("providers", None)
        if not raw:
            try:
                self.path.unlink()
            except OSError:
                pass
            return self.path
        return self.save(raw)


# ── 解析 / 刷新 ───────────────────────────────────────────


@dataclass
class TokenInfo:
    access: str
    source: str          # env:<VAR> / config:apiKey / kb-auth / none
    exp: int | None = None

    @property
    def label(self) -> str:
        if self.source.startswith("env:"):
            return self.source
        return {"config:apiKey": "config:apiKey", "kb-auth": "kb-auth.json"}.get(
            self.source, self.source)


def env_names(provider_name: str, cfg: config.KbConfig) -> list[str]:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", provider_name).strip("_").upper()
    names = [f"QI_KB_TOKEN_{safe}"] if safe else []
    default = cfg.default_provider_name()
    if len(cfg.providers) == 1 or provider_name == default:
        names.append("QI_KB_TOKEN")
    return names


def resolve_access(cfg: config.KbConfig, name: str, *,
                   auth_store: AuthStore | None = None,
                   env: Mapping[str, str] | None = None,
                   clock: Callable[[], float] = time.time) -> TokenInfo:
    provider = cfg.provider(name)
    env_vars = env if env is not None else os.environ
    for var in env_names(name, cfg):
        value = env_vars.get(var)
        if value and value.strip():
            token = value.strip()
            return TokenInfo(token, f"env:{var}", token_exp(token))

    ref = provider.get("apiKey")
    if isinstance(ref, str) and ref.strip():
        value = resolve_value(ref.strip(), env_vars)
        if value:
            return TokenInfo(value, "config:apiKey", token_exp(value))

    store = auth_store or AuthStore()
    entry = store.get(name) or {}
    access = entry.get("access")
    if isinstance(access, str) and access.strip():
        exp = entry.get("exp")
        return TokenInfo(access.strip(), "kb-auth",
                         int(exp) if isinstance(exp, (int, float)) else token_exp(access))

    raise NotLoggedIn(
        f"provider `{name}` 未登录",
        hint=f"`qi kb provider login {name}`；或设置 QI_KB_TOKEN_{name.upper().replace('-', '_')}",
    )


def _post_json(url: str, payload: dict[str, Any], *,
               http: httpx.Client | None = None) -> Any:
    client = http or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    owns = http is None
    try:
        response = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"连不上 {url}：{exc}", hint="检查 provider 的 baseUrl 与网络") from exc
    finally:
        if owns:
            client.close()
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400:
        message = body.get("message") if isinstance(body, dict) else None
        raise UpstreamError(f"上游返回 {response.status_code}：{message or url}",
                            status=response.status_code)
    return unwrap(body)


def login(cfg: config.KbConfig, name: str, login_name: str, password: str, *,
          auth_store: AuthStore | None = None,
          http: httpx.Client | None = None) -> dict[str, Any]:
    """调 `/auth/login` → `me()` 验证 → 写 kb-auth.json。"""
    provider = cfg.provider(name)
    payload = _post_json(api_url(provider, "/auth/login"),
                         {"login": login_name, "password": password}, http=http)
    if not isinstance(payload, dict):
        raise UpstreamError("登录响应不是对象")
    access = payload.get("access_token") or payload.get("access")
    refresh = payload.get("refresh_token") or payload.get("refresh")
    if not access:
        raise UpstreamError("登录响应里没有 access_token")
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    entry = {
        "login": user.get("username") or login_name,
        "role": user.get("role"),
        "access": str(access),
        "refresh": str(refresh) if refresh else None,
        "exp": token_exp(str(access)),
    }
    store = auth_store or AuthStore()
    store.set(name, entry)
    return entry


def me(cfg: config.KbConfig, name: str, *, auth_store: AuthStore | None = None,
       http: httpx.Client | None = None) -> dict[str, Any]:
    provider = cfg.provider(name)
    token = ensure_access(cfg, name, auth_store=auth_store, http=http)
    client = http or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    owns = http is None
    try:
        response = client.get(api_url(provider, "/auth/me"),
                              headers={"Authorization": f"Bearer {token.access}"})
    except httpx.HTTPError as exc:
        raise UpstreamError(f"连不上 {provider.get('baseUrl')}：{exc}") from exc
    finally:
        if owns:
            client.close()
    body = response.json() if response.content else None
    if response.status_code >= 400:
        message = body.get("message") if isinstance(body, dict) else None
        raise UpstreamError(f"上游返回 {response.status_code}：{message or ''}",
                            status=response.status_code)
    data = unwrap(body)
    return data if isinstance(data, dict) else {}


def refresh(cfg: config.KbConfig, name: str, *, auth_store: AuthStore | None = None,
            http: httpx.Client | None = None) -> TokenInfo:
    provider = cfg.provider(name)
    store = auth_store or AuthStore()
    entry = store.get(name) or {}
    refresh_token = entry.get("refresh")
    if not refresh_token:
        raise NotLoggedIn(
            f"provider `{name}` 的凭证已过期，且没有 refresh token",
            hint=f"`qi kb provider login {name}`",
        )
    payload = _post_json(api_url(provider, "/auth/refresh"),
                         {"refresh_token": refresh_token}, http=http)
    if not isinstance(payload, dict):
        raise NotLoggedIn(f"provider `{name}` 续期响应不是对象",
                          hint=f"`qi kb provider login {name}`")
    access = payload.get("access_token") or payload.get("access")
    new_refresh = payload.get("refresh_token") or payload.get("refresh")
    if not access:
        raise NotLoggedIn(f"provider `{name}` 续期响应里没有 access_token",
                          hint=f"`qi kb provider login {name}`")
    entry.update({
        "access": str(access),
        "refresh": str(new_refresh) if new_refresh else refresh_token,
        "exp": token_exp(str(access)),
    })
    store.set(name, entry)
    return TokenInfo(str(access), "kb-auth", token_exp(str(access)))


def ensure_access(cfg: config.KbConfig, name: str, *, auth_store: AuthStore | None = None,
                  http: httpx.Client | None = None, force_refresh: bool = False,
                  clock: Callable[[], float] = time.time) -> TokenInfo:
    info = resolve_access(cfg, name, auth_store=auth_store, clock=clock)
    if not force_refresh and (info.exp is None or info.exp - 60 > clock()):
        return info
    if info.source != "kb-auth":
        if info.exp is not None and info.exp <= clock():
            raise NotLoggedIn(
                f"provider `{name}` 的静态 token 已过期（来源：{info.source}）",
                hint=f"`qi kb provider login {name}`，或换一个新鲜的环境变量",
            )
        return info
    return refresh(cfg, name, auth_store=auth_store, http=http)
