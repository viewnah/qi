"""错误与退出码。

退出码是**脚本的接口**(README §5),所以它必须只有一个来源 —— 命令层不做 if/else 映射,
只把异常翻译成 `exit_code`。130 是 SIGINT 的惯例值,单独留着。
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1        # 网络 / 上游 5xx / 磁盘
EXIT_USAGE = 2        # 用法错误、非 tty 且未 -y、项目未信任、配置坏了
EXIT_AUTH = 3         # 未登录 / 凭证过期且续期失败
EXIT_NOT_FOUND = 4    # 条目不存在 / id 歧义
EXIT_CONFLICT = 5     # 已存在且未 --force
EXIT_CANCELLED = 130  # 用户取消


class StoreError(Exception):
    """所有可预期失败。`hint` 是给用户的下一句(不重复 message)。"""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def lines(self) -> list[str]:
        return [self.message, self.hint] if self.hint else [self.message]


class UsageError(StoreError):
    """命令用错了(或环境不允许这么做)。"""

    exit_code = EXIT_USAGE


class ConfigError(UsageError):
    """`store.json` 坏了 / 缺必填。

    带上**路径**——现场只看到一个 JSON 报错是没法修的(qi-mcp 的 `ConfigError` 同一口径)。
    """


class NotLoggedIn(StoreError):
    """没有凭证,或续期失败。"""

    exit_code = EXIT_AUTH


class NotFound(StoreError):
    """上游没有这个条目,或者 id 有歧义。"""

    exit_code = EXIT_NOT_FOUND


class Conflict(StoreError):
    """目标已存在且不是本账本装的。"""

    exit_code = EXIT_CONFLICT


class UpstreamError(StoreError):
    """上游返回了错误(4xx/5xx 或 `{code,message}` 信封里的非 200)。"""

    def __init__(self, message: str, *, status: int | None = None,
                 hint: str | None = None) -> None:
        super().__init__(message, hint)
        self.status = status


class Cancelled(StoreError):
    """用户取消(回车 / Ctrl-C / EOF)。一个文件都不写。"""

    exit_code = EXIT_CANCELLED
