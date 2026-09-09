"""配置装载:qi_agent.toml 分层查找 + 深合并 + pydantic 校验。

查找顺序(优先级从高到低):
  $QI_AGENT_CONFIG(指定文件)→ <项目>/.qi/qi_agent.toml → ~/.qi/qi_agent.toml
合并:键级深合并,内层覆盖外层;文件不存在则跳过。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .paths import config_file_candidates

# 内置支持的 provider(扩展点:插件可注册更多)
SUPPORTED_PROVIDERS = ("openai", "anthropic", "deepseek", "ollama")

CONFIG_FILE_NAME = "qi_agent.toml"


class ConfigError(Exception):
    """配置缺失/校验失败,启动即报(对应 qi doctor 的检查项)。"""


class ModelSpec(BaseModel):
    provider: str
    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    temperature: float | None = None

    @field_validator("provider")
    @classmethod
    def _check_provider(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in SUPPORTED_PROVIDERS:
            raise ValueError(f"未知 provider {v!r},支持: {', '.join(SUPPORTED_PROVIDERS)}")
        return v


class ModelsConfig(BaseModel):
    default: ModelSpec | None = None
    router: ModelSpec | None = None


class QiConfig(BaseModel):
    """应用配置。未知顶层段(models 之外: mcp / runtime / web …)原样保留。

    实现:extra="allow" 保留整段 dict,便于后续主题渐进加 schema,
    但未知键不做校验(与 agent.md 未知 frontmatter 宽松处理一致)。
    """

    model_config = {"extra": "allow"}  # type: ignore[assignment]

    models: ModelsConfig = Field(default_factory=ModelsConfig)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """键级深合并:override 覆盖 base;嵌套 dict 递归合并。"""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_raw(cwd: Path | None = None) -> tuple[dict[str, Any], list[Path]]:
    """按优先级装载并深合并;返回 (merged_dict, 实际读取的文件列表)。"""
    merged: dict[str, Any] = {}
    loaded: list[Path] = []
    # 高优先级在前;从低到高合并,让高优先级覆盖
    for path in reversed(config_file_candidates(cwd)):
        if not path.is_file():
            continue
        merged = deep_merge(merged, _read_toml(path))
        loaded.append(path)
    return merged, list(reversed(loaded))


def load_config(cwd: Path | None = None) -> tuple[QiConfig, list[Path]]:
    raw, loaded = load_raw(cwd)
    try:
        cfg = QiConfig.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        raise ConfigError(f"配置校验失败: {exc}") from exc
    return cfg, loaded


def require_default_model(cfg: QiConfig) -> ModelSpec:
    """[models.default] 必须存在,否则启动失败(带指引)。"""
    if cfg.models.default is None:
        raise ConfigError(
            f"缺少 [models.default]。请创建 {CONFIG_FILE_NAME} 或在 qi_agent.toml 配置,示例:\n"
            "    [models.default]\n"
            "    provider = \"deepseek\"          # openai|anthropic|deepseek|ollama\n"
            "    model = \"deepseek-chat\"\n"
            "    api_key_env = \"DEEPSEEK_API_KEY\"   # 可选:省略则查 auth store/约定环境变量\n"
        )
    return cfg.models.default
