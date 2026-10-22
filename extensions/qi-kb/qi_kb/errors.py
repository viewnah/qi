"""错误与退出码。

退出码与 qi-store 保持同一套口径（脚本的接口）：

    0   成功
    1   网络 / 上游 5xx / 磁盘
    2   用法错误、非 tty 且未给显式意图、配置坏了
    3   未登录 / 凭证过期且续期失败
    4   条目不存在 / 选择器歧义
    5   已存在且需要用户确认覆盖
    130 用户取消
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_CANCELLED = 130


class KbError(Exception):
    """所有可预期失败。`hint` 是给用户的下一句（不重复 message）。"""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def lines(self) -> list[str]:
        return [self.message, self.hint] if self.hint else [self.message]


class UsageError(KbError):
    exit_code = EXIT_USAGE


class ConfigError(UsageError):
    """`kb.json` / `kb-auth.json` 坏了。消息里带路径。"""


class NotLoggedIn(KbError):
    exit_code = EXIT_AUTH


class NotFound(KbError):
    exit_code = EXIT_NOT_FOUND


class Conflict(KbError):
    """同一个 (instance, kbId) 已由别的 provider 安装，需要用户确认覆盖。"""

    exit_code = EXIT_CONFLICT

    def __init__(self, message: str, *, existing_provider: str = "",
                 target_provider: str = "", hint: str | None = None) -> None:
        super().__init__(message, hint)
        self.existing_provider = existing_provider
        self.target_provider = target_provider


class UpstreamError(KbError):
    """上游返回了错误（4xx/5xx 或 `{code,message}` 信封里的非 200）。"""

    def __init__(self, message: str, *, status: int | None = None,
                 hint: str | None = None) -> None:
        super().__init__(message, hint)
        self.status = status


class Cancelled(KbError):
    exit_code = EXIT_CANCELLED
