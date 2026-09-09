"""凭证三源解析 + auth store(~/.qi/auth.json,0600)。

解析顺序:
  1. 配置显式 api_key_env(该环境变量)
  2. ~/.qi/auth.json 按 provider
  3. 约定环境变量(如 DEEPSEEK_API_KEY)
本地 provider(ollama)免 key。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import ModelSpec
from .paths import global_home

AUTH_FILE_NAME = "auth.json"

# provider → 约定环境变量名(与 auth store 键一一对应)
DEFAULT_API_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

KEYLESS_PROVIDERS = ("ollama",)


@dataclass
class ResolvedKey:
    key: str | None        # None = 未找到
    source: str            # "env:api_key_env" | "auth" | "env:default" | "none" | "missing"
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


def resolve_key(spec: ModelSpec, store: AuthStore | None = None) -> ResolvedKey:
    """按三源解析某模型配置的密钥。"""
    if spec.provider in KEYLESS_PROVIDERS:
        return ResolvedKey(key=None, source="none", ok=True)
    store = store or AuthStore()
    # 1. 显式 api_key_env
    if spec.api_key_env:
        value = os.environ.get(spec.api_key_env)
        if value:
            return ResolvedKey(key=value, source=f"env:{spec.api_key_env}", ok=True)
        return ResolvedKey(key=None, source=f"env:{spec.api_key_env}(未设置)", ok=False)
    # 2. auth store
    stored = store.get(spec.provider)
    if stored:
        return ResolvedKey(key=stored, source="auth", ok=True)
    # 3. 约定环境变量
    default_env = DEFAULT_API_KEY_ENV.get(spec.provider)
    if default_env:
        value = os.environ.get(default_env)
        if value:
            return ResolvedKey(key=value, source="env:default", ok=True)
        return ResolvedKey(key=None, source=f"env:{default_env}(未设置)", ok=False)
    return ResolvedKey(key=None, source="无密钥来源", ok=False)
