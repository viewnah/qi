"""凭证解析 + auth store(`~/.qi/agent/auth.json`,0600,对齐 pi)。

解析顺序(对齐 pi):
  1. `~/.qi/agent/auth.json` 按 provider
  2. 约定环境变量(如 DEEPSEEK_API_KEY)
  3. provider 的 apiKey 引用(models.json 中字面量 / $ENV / !command)
本地 provider(ollama)免 key。

`apiKey` 值语法(与 pi 一致):
  - `!command`           执行命令取 stdout(不经 shell,见下)
  - `$ENV` / `${ENV}`    环境变量插值;缺失则视为未解析
  - `$$` / `$!`          转义为字面量 `$` / `!`
  - 其它                  字面量

安全边界:`!command` 的内容来自**用户自己的配置文件**(与 auth.json 同一信任域),
不会被 agent 或模型内容触发 —— 只在解析用户写的 `apiKey` 时求值。它按
`shlex` 拆成参数后以 `shell=False` 执行,所以没有 shell 注入面:

    apiKey: "!security find-generic-password -ws 'anthropic'"   # ✅ 引号由 shlex 处理
    apiKey: "!op read 'op://vault/item/credential'"             # ✅
    apiKey: "!bash -lc 'cat /tmp/k | tr -d \\n'"                # ✅ 需要管道时显式起 shell

代价:不能直接写 `!cat a | jq -r .key`(管道会被当成普通参数)。这是有意的 ——
要 shell 就把它写出来。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .paths import global_home

AUTH_FILE_NAME = "auth.json"

# provider → 约定环境变量名(与 auth store 键一一对应)
# 国产 provider 那批与 `presets.py` 的 `api_key_env` 一一对应(有测试锁住两边一致)。
DEFAULT_API_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GEMINI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "xai": "XAI_API_KEY",
    # 预置的国产 provider(见 presets.py)
    "dashscope": "DASHSCOPE_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "zhipu": "ZHIPUAI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "stepfun": "STEP_API_KEY",
    "hunyuan": "HUNYUAN_API_KEY",
    "volcengine": "ARK_API_KEY",
    "mimo": "MIMO_API_KEY",
    "qianfan": "QIANFAN_API_KEY",
}

KEYLESS_PROVIDERS = ("ollama",)

_ENV_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def resolve_value(raw: str, env: Mapping[str, str] | None = None) -> str | None:
    """解析 apiKey / header 值;返回 None 表示未解析(环境变量缺失/命令失败)。"""
    env_vars: Mapping[str, str] = env if env is not None else os.environ
    if raw.startswith("$$"):          # 转义:字面量 $
        return raw[1:]
    if raw.startswith("$!"):          # 转义:字面量 !
        return raw[1:]
    if raw.startswith("!"):           # 执行命令
        # 不走 shell:凭证命令是「单条命令 + 参数」形态,`shlex` 负责引号,
        # `shell=False` 从根上消掉注入面。需要管道/重定向时显式写
        # `!bash -lc "…"` —— 把 shell 变成一个看得见的选择,而不是默认。
        try:
            argv = shlex.split(raw[1:])
        except ValueError:            # 引号不配对
            return None
        if not argv:
            return None
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        out = proc.stdout.strip()
        return out or None

    missing = False

    def _repl(match: re.Match[str]) -> str:
        nonlocal missing
        name = match.group(1) or match.group(2)
        value = env_vars.get(name)
        if value is None:
            missing = True
            return ""
        return value

    result = _ENV_RE.sub(_repl, raw)
    if missing:
        return None
    return result


@dataclass
class ResolvedKey:
    key: str | None        # None = 未找到
    source: str
    ok: bool
    #: 密钥**明显不像密钥**的原因(`key_problem()` 给的),否则 None。
    #: 保留 `ok=True`(密钥来源存在、provider 仍可被选中),但请求前会据此报清楚。
    problem: str | None = None

    def describe(self) -> str:
        if self.source == "none":
            return "无需密钥(本地 provider)"
        if self.problem:
            return f"无效:{self.problem}({self.source})"
        if self.ok:
            return f"{self.source}({self.key[:6]}…)" if self.key else self.source
        return f"缺失({self.source})"


def key_problem(key: str) -> str | None:
    """显然不能当 API key 的值 → 一句人话(否则 None)。

    HTTP 头只能装 ASCII,所以含非 ASCII 的密钥一定会在请求时炸成
    `'ascii' codec can't encode …: InternalServerError` —— 而那时离“输入”已经很远了。
    空白/换行同理(真密钥里不会有空格)。最常见的来源是**误粘贴**:把一段提示/日志
    粘进了密钥输入框。所以宁在入口就拦下。
    """
    if not key or not key.strip():
        return "是空的"
    if any(ord(ch) > 127 for ch in key):
        return "含非 ASCII 字符(多半是把别的内容粘贴进来了)"
    if any(ch.isspace() for ch in key):
        return "含空白字符"
    return None


class AuthStore:
    """`~/.qi/agent/auth.json`:按 provider 存 {type:api_key, key:…},权限 0600。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (global_home() / AUTH_FILE_NAME)

    def load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, provider: str) -> str | None:
        entry = self.load().get(provider)
        if isinstance(entry, dict) and entry.get("type") == "api_key":
            return entry.get("key")
        return None

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):   # Windows 无 posix chmod
            os.chmod(self.path, 0o600)

    def set_key(self, provider: str, key: str) -> None:
        """存一个 API key。**显然不是密钥的值直接拒绝**(`ValueError`)。

        在入口拦(而不是等到请求时):非 ASCII / 带空格的密钥不可能是真的,而它一旦落盘,
        失败会拖到某个回合才以 `InternalServerError: 'ascii' codec …` 的形式暴露。
        """
        problem = key_problem(key)
        if problem:
            raise ValueError(f"{provider} 的 API key {problem}")
        data = self.load()
        data[provider] = {"type": "api_key", "key": key}
        self.save(data)

    def remove(self, provider: str) -> bool:
        data = self.load()
        if provider not in data:
            return False
        del data[provider]
        self.save(data)
        return True

    def providers(self) -> list[str]:
        data = self.load()
        return sorted(
            k for k, v in data.items() if isinstance(v, dict) and v.get("type") == "api_key"
        )


class AuthOverride(AuthStore):
    """CLI `--api-key` 的运行期覆盖:本次运行用的密钥优先于 auth store / env / models.json。

    只覆盖 `get()`,不写盘 —— 命令行给的凭证不该静默落进 `auth.json`
    (要存就显式 `qi auth login`)。`providers()` 也不报它:它不是一个"已登录的 provider"。
    """

    def __init__(self, key: str, base: AuthStore | None = None):
        super().__init__(base.path if base is not None else None)
        self._key = key

    def get(self, provider: str) -> str | None:      # noqa: ARG002 对所有 provider 生效
        return self._key


def resolve_key(provider: str, api_key_ref: str | None = None,
                store: AuthStore | None = None) -> ResolvedKey:
    """按 pi 顺序解析某 provider 的密钥。

    1. auth store → 2. 约定环境变量 → 3. provider apiKey 引用。
    """
    store = store or AuthStore()

    def _found(key: str, source: str) -> ResolvedKey:
        # 来源存在 → `ok=True`(provider 仍可被选中);但“不像密钥”要在 `problem` 里留下,
        # 让请求前能报“你的 key 有问题”,而不是拖到 HTTP 头编码失败。
        return ResolvedKey(key=key, source=source, ok=True, problem=key_problem(key))

    stored = store.get(provider)
    if stored:
        return _found(stored, "auth")
    default_env = DEFAULT_API_KEY_ENV.get(provider)
    if default_env:
        value = os.environ.get(default_env)
        if value:
            return _found(value, f"env:{default_env}")
    if api_key_ref:
        value = resolve_value(api_key_ref)
        if value:
            return _found(value, f"config:{api_key_ref}")
        return ResolvedKey(key=None, source=f"config:{api_key_ref}(未解析)", ok=False)
    if default_env:
        return ResolvedKey(key=None, source=f"env:{default_env}(未设置)", ok=False)
    if provider in KEYLESS_PROVIDERS:
        return ResolvedKey(key=None, source="none", ok=True)
    return ResolvedKey(key=None, source="无密钥来源", ok=False)
