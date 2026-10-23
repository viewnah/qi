"""暴露面控制:可选口令。

默认仍绑 `127.0.0.1`,但跨回环**不再强制口令** —— 绑 `0.0.0.0` 时是否设口令由调用方决定;
设了 `--password` / `QI_LAB_PASSWORD` 就校验,不设就全部放行。

口令**不加密传输**:远端访问应经 HTTPS 反代或 VPN。
"""

from __future__ import annotations

import base64
import secrets

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "127.0.0.2"}


def is_loopback(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def check_credentials(header: str | None, password: str | None) -> bool:
    """接受 `Authorization: Bearer <pw>` 或 Basic(用户名任意,口令同 pw)。

    用 `secrets.compare_digest` 做定长比较,避免按字符提前返回。
    """
    if not password:
        return True
    if not header:
        return False
    scheme, _, value = header.partition(" ")
    scheme = scheme.strip().lower()
    if scheme == "bearer":
        return secrets.compare_digest(value.strip(), password)
    if scheme == "basic":
        try:
            decoded = base64.b64decode(value.strip(), validate=False).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return False
        _, _, supplied = decoded.partition(":")
        return secrets.compare_digest(supplied, password)
    return False


def check_host(allowed: list[str], host_header: str | None, bind_host: str) -> bool:
    """反代场景:外部域名必须显式列进允许表(防止 Host 头被伪造)。

    允许表为空时只放行回环与绑定地址本身。
    """
    if not host_header:
        return True
    hostname = host_header.split(":")[0].strip().lower()
    if is_loopback(hostname) or hostname == bind_host.strip().lower():
        return True
    return hostname in {h.strip().lower() for h in allowed if h.strip()}


def mask_key(key: str | None) -> str:
    """只回尾 4 位:凭证永远不以明文出现在响应里(design/web.md §4)。"""
    if not key:
        return ""
    return f"…{key[-4:]}" if len(key) > 4 else "…"
