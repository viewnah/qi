"""`kb.json` 的读取、合并、校验与原子写入。

形状（provider ↔ model）：

    {
      "providers": {
        "alice": {
          "baseUrl": "http://…", "apiPrefix": "/api", "instance": "hikqin-7091",
          "login": "alice", "role": "user",
          "knowledgeBases": {
            "<kbId>": {"id": "<kbId>", "name": "…", …}
          }
        }
      },
      "defaults": {…},
      "defaultProvider": "alice",
      "defaultKnowledgeBase": null
    }

两条硬规则：

1. **`kb.json` 里不存令牌**。出现 `auth` / `access` / `refresh` 一律忽略并 warning。
2. **同一个 (instance, kbId) 只保留一份**。合并与选择器都按这个身份去重。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from . import paths
from .errors import ConfigError, NotFound, UsageError

DEFAULT_API_PREFIX = "/api"
DEFAULT_PROVIDER = "hikqin"
PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
KB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SECRET_KEYS = ("auth", "access", "refresh", "password", "api_secret")
PROVIDER_KEYS = ("baseUrl", "apiPrefix", "instance", "login", "role", "apiKey")
KB_KEYS = ("id", "name", "description", "embeddingModel", "visibility",
           "docCount", "chunkCount", "updatedAt", "icon")


class _Missing:
    pass


MISSING = _Missing()


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """键级深合并；数组整体替换（与 qi settings / models.json 同一口径）。"""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{path}", hint=str(exc)) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是对象：{path}")
    return raw


def _atomic_write(path: Path, data: dict[str, Any], *, private: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600 if private else 0o644)
    except OSError:                              # Windows / 只读挂载
        pass
    return path


# ── 实例身份 ──────────────────────────────────────────────


def normalize_instance(base_url: str, api_prefix: str = DEFAULT_API_PREFIX) -> str:
    """把 baseUrl + apiPrefix 归一化成实例 key。

    scheme/host 小写、去掉末尾 `/`、路径去掉重复斜杠。这样
    `HTTP://Host:7091/` 与 `http://host:7091` 会被认成同一个实例。
    """
    raw = f"{base_url.rstrip('/')}/{api_prefix.strip('/')}" if api_prefix.strip("/") else base_url
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/")
    path = re.sub(r"/+", "/", parsed.path or "").rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def provider_instance(provider: dict[str, Any]) -> str:
    explicit = provider.get("instance")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    base_url = str(provider.get("baseUrl") or "").strip()
    api_prefix = str(provider.get("apiPrefix") or DEFAULT_API_PREFIX).strip()
    return normalize_instance(base_url, api_prefix)


def provider_base_url(provider: dict[str, Any]) -> str:
    base = str(provider.get("baseUrl") or "").strip().rstrip("/")
    if not base:
        raise ConfigError("provider 缺少 baseUrl", hint="`qi kb provider add <名> --base-url …`")
    return base


def provider_api_prefix(provider: dict[str, Any]) -> str:
    return str(provider.get("apiPrefix") or DEFAULT_API_PREFIX).strip()


# ── 形状校验 ──────────────────────────────────────────────


def validate_provider_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        raise UsageError("provider 名不能为空")
    if not PROVIDER_NAME_RE.match(cleaned):
        raise UsageError(
            f"provider 名不合法：{name!r}",
            hint="只允许字母、数字、点、下划线、短横线；不能含 `:` 或 `/`",
        )
    return cleaned


def _clean_provider(name: str, raw: Any, warnings: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        warnings.append(f"provider `{name}` 不是对象，已忽略")
        return None
    for secret in SECRET_KEYS:
        if secret in raw:
            warnings.append(
                f"provider `{name}` 里的 `{secret}` 已忽略：凭证只能放 kb-auth.json"
            )
    out: dict[str, Any] = {}
    for key in PROVIDER_KEYS:
        if key in raw:
            out[key] = raw[key]
    kbs = raw.get("knowledgeBases")
    if kbs is not None and not isinstance(kbs, dict):
        warnings.append(f"provider `{name}.knowledgeBases` 不是对象，已忽略")
        kbs = None
    cleaned_kbs: dict[str, dict[str, Any]] = {}
    for kb_id, entry in (kbs or {}).items():
        key = str(kb_id)
        if not isinstance(entry, dict):
            warnings.append(f"`{name}.knowledgeBases.{key}` 不是对象，已忽略")
            continue
        if not KB_ID_RE.match(key):
            warnings.append(f"kbId 不合法：{key!r}，已忽略")
            continue
        value = {k: entry[k] for k in KB_KEYS if k in entry}
        declared = str(value.get("id") or key)
        if declared != key:
            warnings.append(
                f"`{name}.knowledgeBases.{key}` 的 id 是 `{declared}`，与 key 不一致；已按 key 修正"
            )
        value["id"] = key
        cleaned_kbs[key] = value
    if kbs is not None:
        out["knowledgeBases"] = cleaned_kbs
    return out


# ── 配置对象 ──────────────────────────────────────────────


@dataclass
class KbConfig:
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    default_provider: str | None = None
    default_knowledge_base: str | None = None
    files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: provider / kb 来自哪一层（global / project）—— 写回时用
    provider_scope: dict[str, str] = field(default_factory=dict)
    kb_scope: dict[tuple[str, str], str] = field(default_factory=dict)

    def provider_names(self) -> list[str]:
        return sorted(self.providers)

    def provider(self, name: str) -> dict[str, Any]:
        found = self.providers.get(name)
        if found is None:
            known = ", ".join(self.provider_names()) or "(无)"
            raise NotFound(f"没有 provider `{name}`", hint=f"已配置：{known}")
        return found

    def default_provider_name(self) -> str | None:
        if self.default_provider and self.default_provider in self.providers:
            return self.default_provider
        if len(self.providers) == 1:
            return next(iter(self.providers))
        return None


def load(cwd: Path | None = None) -> KbConfig:
    """读全局 + 项目两层，合并成有效视图。"""
    user_file = paths.user_config_file()
    project_file = paths.project_config_file(cwd)
    user_raw = _read_json(user_file)
    project_raw = _read_json(project_file)

    cfg = KbConfig()
    cfg.files = [p for p in (user_file, project_file) if p.is_file()]

    merged_providers: dict[str, dict[str, Any]] = {}
    user_providers = user_raw.get("providers") or {}
    project_providers = project_raw.get("providers") or {}
    if not isinstance(user_providers, dict):
        cfg.warnings.append("用户级 `providers` 不是对象，已忽略")
        user_providers = {}
    if not isinstance(project_providers, dict):
        cfg.warnings.append("项目级 `providers` 不是对象，已忽略")
        project_providers = {}

    for source, scope in ((user_providers, "global"), (project_providers, "project")):
        for name, raw in source.items():
            try:
                cleaned_name = validate_provider_name(str(name))
            except UsageError as exc:
                cfg.warnings.append(str(exc))
                continue
            cleaned = _clean_provider(cleaned_name, raw, cfg.warnings)
            if cleaned is None:
                continue
            base = merged_providers.get(cleaned_name, {})
            merged_providers[cleaned_name] = deep_merge(base, cleaned)
            cfg.provider_scope[cleaned_name] = scope
            kbs = cleaned.get("knowledgeBases") or {}
            for kb_id in kbs:
                cfg.kb_scope[(cleaned_name, kb_id)] = scope

    # 校验合并后的 provider（项目级可能只给了 login，baseUrl 来自全局）
    for name, provider in list(merged_providers.items()):
        base_url = str(provider.get("baseUrl") or "").strip()
        if not base_url:
            cfg.warnings.append(f"provider `{name}` 缺少 baseUrl，已忽略")
            merged_providers.pop(name, None)
            cfg.provider_scope.pop(name, None)
            for key in [k for k in cfg.kb_scope if k[0] == name]:
                cfg.kb_scope.pop(key, None)
            continue
        provider.setdefault("apiPrefix", DEFAULT_API_PREFIX)
        provider.setdefault("knowledgeBases", {})

    cfg.providers = merged_providers
    user_defaults = user_raw.get("defaults")
    project_defaults = project_raw.get("defaults")
    cfg.defaults = deep_merge(
        user_defaults if isinstance(user_defaults, dict) else {},
        project_defaults if isinstance(project_defaults, dict) else {},
    )
    cfg.default_provider = project_raw.get("defaultProvider") or user_raw.get("defaultProvider")
    cfg.default_knowledge_base = (
        project_raw.get("defaultKnowledgeBase") or user_raw.get("defaultKnowledgeBase")
    )
    return cfg


# ── 遍历 / 去重 / 选择器 ──────────────────────────────────


@dataclass(frozen=True)
class KbRef:
    provider: str
    provider_cfg: dict[str, Any]
    kb_id: str
    entry: dict[str, Any]
    scope: str
    instance: str

    @property
    def name(self) -> str:
        return str(self.entry.get("name") or self.kb_id)


def iter_kbs(cfg: KbConfig) -> list[KbRef]:
    """按 (instance, kbId) 去重后的 KB 列表（运行时的“只有一份”）。"""
    seen: dict[tuple[str, str], KbRef] = {}
    for provider_name in sorted(cfg.providers):
        provider = cfg.providers[provider_name]
        instance = provider_instance(provider)
        for kb_id, entry in (provider.get("knowledgeBases") or {}).items():
            key = (instance, kb_id)
            if key in seen:
                cfg.warnings.append(
                    f"同一 KB 出现在多个 provider：{seen[key].provider} 与 {provider_name}"
                    f"（{instance} / {kb_id}）；运行时只使用 {seen[key].provider}"
                )
                continue
            seen[key] = KbRef(
                provider=provider_name,
                provider_cfg=provider,
                kb_id=kb_id,
                entry=entry,
                scope=cfg.kb_scope.get((provider_name, kb_id), "global"),
                instance=instance,
            )
    return sorted(seen.values(), key=lambda r: (r.provider, r.name, r.kb_id))


def bound(cfg: KbConfig) -> list[KbRef]:
    return [ref for ref in iter_kbs(cfg) if not ref.entry.get("disabled")]


def find_kb(cfg: KbConfig, selector: str) -> KbRef:
    """解析 `id` / `名称` / `id@provider` / `名称@provider`。

    歧义报错并列候选，不猜。
    """
    raw = str(selector or "").strip()
    if not raw:
        raise UsageError("知识库选择器不能为空")
    provider_name: str | None = None
    key = raw
    if "@" in raw:
        left, _, right = raw.rpartition("@")
        if right in cfg.providers:
            key, provider_name = left.strip(), right.strip()

    pool = [ref for ref in iter_kbs(cfg) if provider_name is None or ref.provider == provider_name]
    if not pool:
        hint = f"已绑定：{', '.join(ref.name for ref in iter_kbs(cfg)) or '(无)'}"
        raise NotFound(f"没有可用的知识库：{raw}", hint=hint)

    exact_id = [ref for ref in pool if ref.kb_id == key]
    if exact_id:
        return _one(exact_id, raw)
    exact_name = [ref for ref in pool if ref.name.lower() == key.lower()]
    if exact_name:
        return _one(exact_name, raw)
    partial = [ref for ref in pool if key.lower() in ref.name.lower()]
    if partial:
        return _one(partial, raw)
    raise NotFound(f"没有找到知识库 `{raw}`", hint=_candidates(pool))


def resolve_many(cfg: KbConfig, selectors: Iterable[str] | None,
                 provider: str | None = None) -> list[KbRef]:
    names = [str(s).strip() for s in (selectors or []) if str(s).strip()]
    if names:
        refs = [find_kb(cfg, name) for name in names]
    else:
        refs = bound(cfg)
    if provider:
        refs = [ref for ref in refs if ref.provider == provider]
    # 同一个 KB 只算一次
    seen: set[tuple[str, str]] = set()
    out: list[KbRef] = []
    for ref in refs:
        key = (ref.instance, ref.kb_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    if not out:
        hint = "先 `qi kb install <id> --provider <p>`"
        raise NotFound("没有匹配的已绑定知识库", hint=hint)
    return out


def _one(refs: list[KbRef], raw: str) -> KbRef:
    if len(refs) == 1:
        return refs[0]
    raise NotFound(f"`{raw}` 有歧义", hint=_candidates(refs))


def _candidates(refs: list[KbRef]) -> str:
    shown = refs[:10]
    lines = [f"  {ref.kb_id}  {ref.name}@{ref.provider}" for ref in shown]
    more = "" if len(refs) <= len(shown) else f"\n  …还有 {len(refs) - len(shown)} 条"
    return "\n".join(lines) + more


# ── 写入 ──────────────────────────────────────────────────


def _scope_file(scope: str, cwd: Path | None = None) -> Path:
    if scope == "global":
        return paths.user_config_file()
    if scope == "project":
        return paths.project_config_file(cwd)
    raise UsageError(f"未知的作用域：`{scope}`", hint="可用：global / project")


def save_provider(scope: str, name: str, provider: dict[str, Any], *,
                  cwd: Path | None = None) -> Path:
    name = validate_provider_name(name)
    file = _scope_file(scope, cwd)
    raw = _read_json(file)
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        raw["providers"] = providers
    current = providers.get(name)
    current = current if isinstance(current, dict) else {}
    merged = deep_merge(current, provider)
    for secret in SECRET_KEYS:
        merged.pop(secret, None)
    providers[name] = merged
    return _atomic_write(file, raw, private=(scope == "global"))


def remove_provider(scope: str, name: str, *, cwd: Path | None = None) -> Path | None:
    file = _scope_file(scope, cwd)
    raw = _read_json(file)
    providers = raw.get("providers")
    if not isinstance(providers, dict) or name not in providers:
        return None
    providers.pop(name, None)
    if not providers:
        raw.pop("providers", None)
    if not raw:
        try:
            file.unlink()
        except OSError:
            pass
        return file
    return _atomic_write(file, raw, private=(scope == "global"))


def save_kb(scope: str, provider_name: str, kb_id: str, entry: dict[str, Any], *,
            provider: dict[str, Any] | None = None,
            cwd: Path | None = None) -> Path:
    if not KB_ID_RE.match(kb_id):
        raise UsageError(f"kbId 不合法：{kb_id!r}")
    file = _scope_file(scope, cwd)
    raw = _read_json(file)
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        raw["providers"] = providers
    current = providers.get(provider_name)
    current = current if isinstance(current, dict) else {}
    if provider:
        current = deep_merge(current, provider)
    for secret in SECRET_KEYS:
        current.pop(secret, None)
    kbs = current.get("knowledgeBases")
    if not isinstance(kbs, dict):
        kbs = {}
        current["knowledgeBases"] = kbs
    value = {k: entry[k] for k in KB_KEYS if k in entry}
    value["id"] = kb_id
    kbs[kb_id] = value
    providers[provider_name] = current
    return _atomic_write(file, raw, private=(scope == "global"))


def remove_kb(scope: str, provider_name: str, kb_id: str, *,
              cwd: Path | None = None) -> Path | None:
    file = _scope_file(scope, cwd)
    raw = _read_json(file)
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return None
    provider = providers.get(provider_name)
    if not isinstance(provider, dict):
        return None
    kbs = provider.get("knowledgeBases")
    if not isinstance(kbs, dict) or kb_id not in kbs:
        return None
    kbs.pop(kb_id, None)
    if not kbs:
        provider.pop("knowledgeBases", None)
    if not providers:
        raw.pop("providers", None)
    if not raw:
        try:
            file.unlink()
        except OSError:
            pass
        return file
    return _atomic_write(file, raw, private=(scope == "global"))


def save_defaults(scope: str, defaults: dict[str, Any], *,
                  cwd: Path | None = None) -> Path:
    file = _scope_file(scope, cwd)
    raw = _read_json(file)
    current = raw.get("defaults")
    current = current if isinstance(current, dict) else {}
    raw["defaults"] = deep_merge(current, defaults)
    return _atomic_write(file, raw, private=(scope == "global"))


def save_default_provider(name: str, *, cwd: Path | None = None,
                          scope: str = "global") -> Path:
    file = _scope_file(scope, cwd)
    raw = _read_json(file)
    raw["defaultProvider"] = name
    return _atomic_write(file, raw, private=(scope == "global"))


def provider_is_valid(provider: dict[str, Any]) -> bool:
    return bool(str(provider.get("baseUrl") or "").strip())
