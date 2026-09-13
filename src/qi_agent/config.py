"""模型配置 `models.json`(对齐 pi 格式)+ 解析。

文件位置(优先级从高到低):
  $QI_AGENT_CONFIG(指定文件)→ <项目>/.qi/models.json → ~/.qi/agent/models.json
合并:键级深合并,内层覆盖外层;providers 按 provider 名合并。

默认模型 `defaultProvider` / `defaultModel` **只属于 settings.json**(对齐 pi):

    <项目>/.qi/settings.json  >  ~/.qi/agent/settings.json

models.json 里的这两个键**已不再被读取**(不是"优先级更低",是彻底不参与);
启动时若发现残留,报错/诊断里会给出可照抄的迁移命令。

格式与 pi 的 models.json 相同:

    {
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

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .paths import MODELS_FILE_NAME, SETTINGS_FILE_NAME, models_file_candidates
from .settings import SettingsError, deep_merge, load_settings, read_json

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
    """models.json 的配置面。未知顶层段原样保留(extra=allow)。

    注:`defaultProvider` / `defaultModel` **不在这里** —— 默认模型属于
    `settings.json`(见 settings.QiSettings);models.json 里写了也会被剔除。
    """

    model_config = {"extra": "allow"}  # type: ignore[assignment]

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


def deep_merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """兼容旧调用名;实现已统一到 settings.deep_merge。"""
    return deep_merge(base, override)


def _read_json(path: Path) -> dict[str, Any]:
    """读取单个 JSON 文件;失败转 ConfigError(settings.read_json 报路径)。"""
    try:
        return read_json(path)
    except SettingsError as exc:
        raise ConfigError(str(exc)) from exc


def load_raw(cwd: Path | None = None) -> tuple[dict[str, Any], list[Path]]:
    """按优先级装载并深合并;返回 (merged_dict, 实际读取的文件列表)。"""
    merged: dict[str, Any] = {}
    loaded: list[Path] = []
    # 高优先级在前;从低到高合并,让高优先级覆盖
    for path in reversed(models_file_candidates(cwd)):
        if not path.is_file():
            continue
        merged = deep_merge(merged, _read_json(path))
        loaded.append(path)
    return merged, list(reversed(loaded))


def default_model_spec(cwd: Path | None = None) -> tuple[str | None, str | None, str]:
    """默认模型及其来源,供 `qi doctor` 诊断。

    **只从 settings.json 读**(对齐 pi):

        <项目>/.qi/settings.json  >  ~/.qi/agent/settings.json

    `models.json` 里的 `defaultProvider` / `defaultModel` **不再被读取** ——
    如果旧文件里还留着,`legacy_default_keys()` 会报出来。
    """
    try:
        settings, files = load_settings(cwd)
    except SettingsError as exc:
        raise ConfigError(str(exc)) from exc
    if settings.defaultProvider and settings.defaultModel:
        return settings.defaultProvider, settings.defaultModel, (
            f"settings:{files[0]}" if files else "settings"
        )
    return None, None, "未配置"


def legacy_default_keys(cwd: Path | None = None) -> list[tuple[Path, str | None, str | None]]:
    """找出 models.json 里残留的 `defaultProvider`/`defaultModel`。

    返回 `(文件, provider, model)`;空列表表示没有残留。这两个键已不再生效,
    仅用于在报错/诊断里给出可照抄的迁移指令。
    """
    hits: list[tuple[Path, str | None, str | None]] = []
    for path in models_file_candidates(cwd):
        if not path.is_file():
            continue
        try:
            data = read_json(path)
        except SettingsError:
            continue
        if "defaultProvider" not in data and "defaultModel" not in data:
            continue
        provider = data.get("defaultProvider")
        model = data.get("defaultModel")
        hits.append((
            path,
            str(provider) if provider else None,
            str(model) if model else None,
        ))
    return hits


def _legacy_hint(cwd: Path | None) -> str:
    """残留老的默认模型键时,拼一段可照抄的迁移提示。"""
    try:
        hits = legacy_default_keys(cwd)
    except Exception:  # noqa: BLE001 - 提示文本不该反过来炸掉启动
        return ""
    if not hits:
        return ""
    path, provider, model = hits[0]
    cmd = "qi config"
    if provider:
        cmd += f" --set defaultProvider={provider}"
    if model:
        cmd += f" --set defaultModel={model}"
    body = (
        f"\n已从 {path} 读到 `defaultProvider`/`defaultModel`,但这两个键已不再从 "
        f"{MODELS_FILE_NAME} 读取(默认模型属于 {SETTINGS_FILE_NAME})。\n"
        f"    {cmd}\n"
        f"然后删掉 {path} 里的这两行。"
    )
    if len(hits) > 1:
        body += "\n(另有残留:" + ", ".join(str(p) for p, _x, _y in hits[1:]) + ")"
    return body


def load_config(cwd: Path | None = None) -> tuple[QiConfig, list[Path]]:
    """装载 models.json。

    注:默认模型**不在**这里 —— 它属于 settings.json,
    由 `resolve_default_model()` / `default_model_spec()` 单独解析。
    """
    raw, loaded = load_raw(cwd)
    # models.json 里的 default* 不再是有效字段:剔掉,避免被 pydantic 当成未知键静默收下
    raw.pop("defaultProvider", None)
    raw.pop("defaultModel", None)
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


def resolve_default_model(cfg: QiConfig, cwd: Path | None = None) -> ResolvedModel:
    """解析默认模型;未配置则启动失败(带可照抄的指引)。

    默认模型只从 `settings.json` 读(对齐 pi),`models.json` 不参与。
    """
    provider, model, _source = default_model_spec(cwd)
    if not provider or not model:
        raise ConfigError(
            f"缺少默认模型:{SETTINGS_FILE_NAME} 里需要 defaultProvider + defaultModel。\n"
            "    qi config --set defaultProvider=<name> --set defaultModel=<id>\n"
            "    （或 `qi init` 引导写入）\n"
            f"    ~/.qi/agent/{SETTINGS_FILE_NAME}    ← 默认模型(对齐 pi)\n"
            f"    ~/.qi/agent/{MODELS_FILE_NAME}      ← 只放 provider / 模型定义\n"
            "示例:\n"
            "    {\n"
            '      "defaultProvider": "deepseek",\n'
            '      "defaultModel": "deepseek-chat"\n'
            "    }\n"
            + _legacy_hint(cwd)
        )
    return resolve_model(cfg, provider, model)


def resolve_router_model(cfg: QiConfig, cwd: Path | None = None) -> ResolvedModel:
    """router 模型;未单独配置则回退 default。"""
    if cfg.routerProvider and cfg.routerModel:
        return resolve_model(cfg, cfg.routerProvider, cfg.routerModel)
    return resolve_default_model(cfg, cwd)


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
    with contextlib.suppress(OSError):   # Windows 无 posix chmod
        os.chmod(path, 0o600)
