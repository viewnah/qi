"""模型配置 `models.json`(对齐 pi 格式)+ 解析。

文件位置(优先级从高到低):
  $QI_AGENT_CONFIG(指定文件)→ <项目>/.qi/models.json → ~/.qi/models.json
合并:键级深合并,内层覆盖外层;providers 按 provider 名合并。

格式与 pi 的 models.json 相同:

    {
      "defaultProvider": "deepseek",          # qi 扩展(pi 放在 settings.json)
      "defaultModel": "deepseek-chat",
      "providers": {
        "deepseek": {
          "baseUrl": "https://api.deepseek.com/v1",
          "api": "openai-completions",
          "apiKey": "$DEEPSEEK_API_KEY",
          "models": [
            { "id": "deepseek-chat", "reasoning": false,
              "contextWindow": 128000, "maxTokens": 16384 }
          ]
        }
      }
    }

凭证明文永不写入本文件:只允许 `apiKey` 引用(字面量/`$ENV`/`!command`)或 auth store。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .paths import MODELS_FILE_NAME, models_file_candidates

# 支持 pi 的 api 类型(见 docs/models.md)
DEFAULT_API = "openai-completions"
SUPPORTED_APIS = (
    "openai-completions",
    "openai-responses",
    "anthropic-messages",
    "google-generative-ai",
)

DEFAULT_CONTEXT_WINDOW = 128000
DEFAULT_MAX_TOKENS = 16384


class ConfigError(Exception):
    """配置缺失/校验失败,启动即报(对应 qi doctor 的检查项)。"""


class ModelEntry(BaseModel):
    """pi 格式的单个模型定义(额外字段原样保留)。"""

    model_config = {"extra": "allow"}  # type: ignore[assignment]

    id: str
    name: str | None = None
    api: str | None = None
    reasoning: bool = False
    input: list[str] = Field(default_factory=lambda: ["text"])
    contextWindow: int = DEFAULT_CONTEXT_WINDOW
    maxTokens: int = DEFAULT_MAX_TOKENS
    cost: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    compat: dict[str, Any] | None = None


class ProviderConfig(BaseModel):
    """pi 格式的 provider 定义(额外字段原样保留)。"""

    model_config = {"extra": "allow"}  # type: ignore[assignment]

    baseUrl: str | None = None
    api: str | None = None
    apiKey: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    models: list[ModelEntry] = Field(default_factory=list)


class QiConfig(BaseModel):
    """应用配置。未知顶层段原样保留(extra=allow)。"""

    model_config = {"extra": "allow"}  # type: ignore[assignment]

    defaultProvider: str | None = None
    defaultModel: str | None = None
    routerProvider: str | None = None
    routerModel: str | None = None
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


@dataclass
class ResolvedModel:
    """解析后的运行期模型(provider 默认 + 模型条目合并)。"""

    provider: str
    model: str
    api: str
    base_url: str | None
    api_key_ref: str | None
    reasoning: bool
    context_window: int
    max_tokens: int
    entry: ModelEntry | None = None

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """键级深合并:override 覆盖 base;嵌套 dict 递归合并。"""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        raw = fh.read()
    if not raw.strip():
        return {}
    return json.loads(raw.decode("utf-8"))


def load_raw(cwd: Path | None = None) -> tuple[dict[str, Any], list[Path]]:
    """按优先级装载并深合并;返回 (merged_dict, 实际读取的文件列表)。"""
    merged: dict[str, Any] = {}
    loaded: list[Path] = []
    # 高优先级在前;从低到高合并,让高优先级覆盖
    for path in reversed(models_file_candidates(cwd)):
        if not path.is_file():
            continue
        try:
            merged = deep_merge(merged, _read_json(path))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"读取 {path} 失败: {exc}") from exc
        loaded.append(path)
    return merged, list(reversed(loaded))


def load_config(cwd: Path | None = None) -> tuple[QiConfig, list[Path]]:
    raw, loaded = load_raw(cwd)
    try:
        cfg = QiConfig.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        raise ConfigError(f"配置校验失败: {exc}") from exc
    return cfg, loaded


def resolve_model(cfg: QiConfig, provider: str, model: str) -> ResolvedModel:
    """按 provider 默认 + 模型条目合并出运行期模型(模型条目可缺省)。"""
    prov = cfg.providers.get(provider)
    entry: ModelEntry | None = None
    if prov is not None:
        for m in prov.models:
            if m.id == model:
                entry = m
                break
    api = (entry.api if entry and entry.api else None) or (prov.api if prov else None) or DEFAULT_API
    return ResolvedModel(
        provider=provider,
        model=model,
        api=api,
        base_url=prov.baseUrl if prov else None,
        api_key_ref=prov.apiKey if prov else None,
        reasoning=entry.reasoning if entry else False,
        context_window=entry.contextWindow if entry else DEFAULT_CONTEXT_WINDOW,
        max_tokens=entry.maxTokens if entry else DEFAULT_MAX_TOKENS,
        entry=entry,
    )


def require_default_model(cfg: QiConfig) -> ResolvedModel:
    """defaultProvider/defaultModel 必须存在,否则启动失败(带指引)。"""
    if not cfg.defaultProvider or not cfg.defaultModel:
        raise ConfigError(
            f"缺少默认模型。请运行 `qi init` 或创建 {MODELS_FILE_NAME},示例:\n"
            "    {\n"
            '      "defaultProvider": "deepseek",\n'
            '      "defaultModel": "deepseek-chat",\n'
            '      "providers": {\n'
            '        "deepseek": {\n'
            '          "baseUrl": "https://api.deepseek.com/v1",\n'
            '          "api": "openai-completions",\n'
            '          "apiKey": "$DEEPSEEK_API_KEY",\n'
            '          "models": [{ "id": "deepseek-chat" }]\n'
            "        }\n"
            "      }\n"
            "    }\n"
        )
    return resolve_model(cfg, cfg.defaultProvider, cfg.defaultModel)


def resolve_router_model(cfg: QiConfig) -> ResolvedModel:
    """router 模型;未单独配置则回退 default。"""
    if cfg.routerProvider and cfg.routerModel:
        return resolve_model(cfg, cfg.routerProvider, cfg.routerModel)
    return require_default_model(cfg)


# ── 读写原始 models.json(qi init 用) ─────────────────────

def load_models_file(path: Path) -> dict[str, Any]:
    """读取单个 models.json(不存在/损坏返回 {})。"""
    if not path.is_file():
        return {}
    try:
        return _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}


def save_models_file(path: Path, data: dict[str, Any]) -> None:
    """写入单个 models.json(0600;虽只存引用,也按敏感文件处理)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows 无 posix chmod
        pass
