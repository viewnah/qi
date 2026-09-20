"""暴露面控制:默认只回环 + 可选密码。

对齐 docs/web.md §3:默认 `127.0.0.1`;跨回环必须**显式**指定 hostname **且**设密码,
否则拒绝启动——暴露出去的是一个能执行高权限操作的 agent,不是一个静态站点。

密码**不加密传输**:远端访问应经 HTTPS 反代或 VPN(与 pi-web 的告警语义一致)。
"""

from __future__ import annotations

import base64
import secrets

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "127.0.0.2"}


def is_loopback(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def require_safe_config(hostname: str, password: str | None) -> None:
    """不安全的组合直接拒启,并给出可执行的修复步骤(而不是只报错)。"""
    if is_loopback(hostname) or password:
        return
    raise SystemExit(
        f"拒绝启动:--hostname {hostname} 会把一个能执行高权限操作的 agent 暴露到回环之外。\n"
        f"  修复 1(推荐):只在本机用 → qi web          # 默认 127.0.0.1\n"
        f"  修复 2(局域网):设长随机口令 → QI_WEB_PASSWORD='...' qi web -H {hostname}\n"
        "  注意:口令不加密传输;经反代请用 HTTPS,并把外部域名加进 QI_WEB_ALLOWED_HOSTS。"
    )


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
    """只回尾 4 位:凭证永远不以明文出现在响应里(docs/web.md §4)。"""
    if not key:
        return ""
    return f"…{key[-4:]}" if len(key) > 4 else "…"
