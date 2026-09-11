"""凭证解析 + auth store(~/.qi/auth.json,0600,对齐 pi)。

解析顺序(对齐 pi):
  1. ~/.qi/auth.json 按 provider
  2. 约定环境变量(如 DEEPSEEK_API_KEY)
  3. provider 的 apiKey 引用(models.json 中字面量 / $ENV / !command)
本地 provider(ollama)免 key。

`apiKey` 值语法(与 pi 一致):
  - `!command`           执行命令取 stdout
  - `$ENV` / `${ENV}`    环境变量插值;缺失则视为未解析
  - `$$` / `$!`          转义为字面量 `$` / `!`
  - 其它                  字面量
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import global_home

AUTH_FILE_NAME = "auth.json"

# provider → 约定环境变量名(与 auth store 键一一对应)
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
}

KEYLESS_PROVIDERS = ("ollama",)

_ENV_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def resolve_value(raw: str, env: dict[str, str] | None = None) -> str | None:
    """解析 apiKey / header 值;返回 None 表示未解析(环境变量缺失/命令失败)。"""
    env = env if env is not None else os.environ
    if raw.startswith("$$"):          # 转义:字面量 $
        return raw[1:]
    if raw.startswith("$!"):          # 转义:字面量 !
        return raw[1:]
    if raw.startswith("!"):           # 执行命令
        try:
            proc = subprocess.run(
                raw[1:], shell=True, capture_output=True, text=True, timeout=30
            )
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
        value = env.get(name)
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

    def describe(self) -> str:
        if self.source == "none":
            return "无需密钥(本地 provider)"
        if self.ok:
            return f"{self.source}({self.key[:6]}…)" if self.key else self.source
        return f"缺失({self.source})"


class AuthStore:
    """~/.qi/auth.json:按 provider 存 {type:api_key, key:…},权限 0600。"""

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
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # Windows 无 posix chmod
            pass

    def set_key(self, provider: str, key: str) -> None:
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


def resolve_key(provider: str, api_key_ref: str | None = None,
                store: AuthStore | None = None) -> ResolvedKey:
    """按 pi 顺序解析某 provider 的密钥。

    1. auth store → 2. 约定环境变量 → 3. provider apiKey 引用。
    """
    store = store or AuthStore()
    stored = store.get(provider)
    if stored:
        return ResolvedKey(key=stored, source="auth", ok=True)
    default_env = DEFAULT_API_KEY_ENV.get(provider)
    if default_env:
        value = os.environ.get(default_env)
        if value:
            return ResolvedKey(key=value, source=f"env:{default_env}", ok=True)
    if api_key_ref:
        value = resolve_value(api_key_ref)
        if value:
            return ResolvedKey(key=value, source=f"config:{api_key_ref}", ok=True)
        return ResolvedKey(key=None, source=f"config:{api_key_ref}(未解析)", ok=False)
    if default_env:
        return ResolvedKey(key=None, source=f"env:{default_env}(未设置)", ok=False)
    if provider in KEYLESS_PROVIDERS:
        return ResolvedKey(key=None, source="none", ok=True)
    return ResolvedKey(key=None, source="无密钥来源", ok=False)
