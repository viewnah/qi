"""`store.json` 的两层读取与写入。

两层(近者胜):

    用户级  `~/.qi/agent/store.json`  全部字段,**含 `auth`**(账号就在这儿)
    项目级  `<cwd>/.qi/store.json`     只允许非敏感字段 → 这份能提交进仓库

**核心不变量:`auth` 只能来自用户级。** 项目级出现 `auth` 一律忽略并记一条警告 ——
否则"把 token 提交进仓库"就从一个能防御的错误变成可无意触发的事故。
这条有测试钉住(`tests/test_config.py`)。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import paths
from .errors import ConfigError

#: 项目级允许出现的源字段(非敏感)。不在表里的一律丢弃并警告 —— 白名单比黑名单好,
#: 因为漏掉的"敏感字段"是随上游长出来的。
PROJECT_SOURCE_KEYS = ("baseUrl", "apiPrefix", "priority", "disabled")

#: 秘密字段:只准用户级
SECRET_SOURCE_KEYS = ("auth",)

DEFAULT_API_PREFIX = "/api"
DEFAULT_STORE_NAME = "hikqin"


@dataclass
class Source:
    """一个商店源。`auth` 是账号 + 令牌(用户级才有)。"""

    name: str
    base_url: str
    api_prefix: str = DEFAULT_API_PREFIX
    priority: int = 0
    disabled: bool = False
    auth: dict[str, Any] | None = None

    @property
    def access_token(self) -> str | None:
        value = (self.auth or {}).get("access")
        return str(value) if value else None

    @property
    def refresh_token(self) -> str | None:
        value = (self.auth or {}).get("refresh")
        return str(value) if value else None

    @property
    def login_name(self) -> str | None:
        value = (self.auth or {}).get("login")
        return str(value) if value else None

    def as_public(self) -> dict[str, Any]:
        """给 `--json` 与诊断用:**去掉 auth**。"""
        return {"name": self.name, "baseUrl": self.base_url,
                "apiPrefix": self.api_prefix, "priority": self.priority,
                "disabled": self.disabled,
                "loggedIn": bool(self.auth and self.access_token)}


@dataclass
class StoreConfig:
    sources: dict[str, Source] = field(default_factory=dict)
    default_store: str | None = None
    install: dict[str, Any] = field(default_factory=dict)
    wanted: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)

    def source(self, name: str | None = None) -> Source:
        """取一个源(默认 `defaultStore`,再退唯一的那个)。`QI_STORE_URL` 在这里生效。"""
        wanted = name or self.default_store
        if wanted is None:
            if len(self.sources) == 1:
                wanted = next(iter(self.sources))
            elif not self.sources:
                raise ConfigError(
                    "没有配置任何商店源",
                    hint=f"配一个:`qi store login http://<host>:6091`(写进 "
                         f"{paths.user_config_file()})",
                )
            else:
                raise ConfigError(
                    f"有多个源({', '.join(sorted(self.sources))}),请用 `@源名` 或配置 defaultStore"
                )
        found = self.sources.get(wanted)
        if found is None:
            known = ", ".join(sorted(self.sources)) or "(无)"
            raise ConfigError(f"未知的商店源:{wanted}", hint=f"已配置:{known}")
        override = os.environ.get(paths.QI_STORE_URL)
        if override:
            # 一次性覆盖:指向别的实例,但**不写文件**(CI / 临时验证用)
            return replace(found, base_url=override.rstrip("/"))
        return found


def _read_json(file: Path) -> dict[str, Any]:
    if not file.is_file():
        return {}
    try:
        raw = json.loads(file.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"配置文件不是合法 JSON:{file}", hint=str(exc)) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是对象:{file}")
    return raw


def _source_from(name: str, raw: Any, *, allow_secret: bool) -> Source | None:
    if not isinstance(raw, dict):
        return None
    base_url = raw.get("baseUrl")
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    auth = raw.get("auth")
    return Source(
        name=name,
        base_url=base_url.strip().rstrip("/"),
        api_prefix=str(raw.get("apiPrefix") or DEFAULT_API_PREFIX),
        priority=int(raw.get("priority") or 0),
        disabled=bool(raw.get("disabled") is True),
        auth=dict(auth) if (allow_secret and isinstance(auth, dict)) else None,
    )


def load(cwd: Path | None = None) -> StoreConfig:
    """读两层并合并。坏文件 → `ConfigError`(带路径),不静默重建。"""
    user_file = paths.user_config_file()
    project_file = paths.project_config_file(cwd)
    user_raw = _read_json(user_file)
    project_raw = _read_json(project_file)

    cfg = StoreConfig()
    cfg.files = [p for p in (user_file, project_file) if p.is_file()]

    for name, raw in (user_raw.get("stores") or {}).items():
        source = _source_from(str(name), raw, allow_secret=True)
        if source is None:
            cfg.warnings.append(f"用户级配置里的源 `{name}` 缺少 baseUrl,已忽略")
            continue
        cfg.sources[str(name)] = source

    project_stores = project_raw.get("stores") or {}
    if isinstance(project_stores, dict):
        for name, raw in project_stores.items():
            if not isinstance(raw, dict):
                continue
            stripped = [k for k in SECRET_SOURCE_KEYS if k in raw]
            if stripped:
                # **硬规则**:项目级不许有凭证 —— 忽略并说出来(不静默,也不当回退)
                cfg.warnings.append(
                    f"项目级配置里的 `stores.{name}.auth` 已忽略:凭证只能放用户级"
                    f"({user_file})"
                )
            allowed = {k: v for k, v in raw.items() if k in PROJECT_SOURCE_KEYS}
            if not allowed:
                continue
            if name in cfg.sources:
                current = cfg.sources[str(name)]
                cfg.sources[str(name)] = replace(
                    current,
                    base_url=str(allowed.get("baseUrl", current.base_url)).rstrip("/"),
                    api_prefix=str(allowed.get("apiPrefix", current.api_prefix)),
                    priority=int(allowed.get("priority", current.priority) or 0),
                    disabled=bool(allowed.get("disabled", current.disabled) is True),
                )
            else:
                source = _source_from(str(name), allowed, allow_secret=False)
                if source is not None:
                    cfg.sources[str(name)] = source

    cfg.default_store = project_raw.get("defaultStore") or user_raw.get("defaultStore")
    install = {}
    for raw in (user_raw.get("install"), project_raw.get("install")):
        if isinstance(raw, dict):
            install.update(raw)
    cfg.install = install
    wanted = project_raw.get("wanted")
    if not isinstance(wanted, list):
        wanted = user_raw.get("wanted")
    cfg.wanted = list(wanted) if isinstance(wanted, list) else []
    return cfg


def save_auth(name: str, auth: dict[str, Any] | None, *, base_url: str | None = None) -> Path:
    """把 `auth` 写进**用户级**配置(传 `None` = 删掉)。返回被写的文件。

    写时 `chmod 0600`:这份文件可能含 access/refresh 令牌。Windows 上没有等价的 ACL
    语义,那里的 chmod 只落到只读位 —— `login` 的输出会提示该目录应仅本人可读。
    """
    file = paths.user_config_file()
    raw = _read_json(file)
    stores = raw.get("stores")
    if not isinstance(stores, dict):
        stores = {}
        raw["stores"] = stores
    entry = stores.get(name)
    if not isinstance(entry, dict):
        entry = {}
        stores[name] = entry
    if base_url:
        entry["baseUrl"] = base_url.rstrip("/")
    if auth is None:
        entry.pop("auth", None)
    else:
        entry["auth"] = auth
    if not entry.get("baseUrl") and "auth" not in entry:
        stores.pop(name, None)      # 空壳不留:删干净,`sources` 才不显示幽灵源

    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(file, 0o600)
    except OSError:                  # Windows / 只读挂载:尽力而为,别让登录失败
        pass
    return file


def save_default(name: str) -> None:
    """把某个源设为 defaultStore(用户级)。"""
    file = paths.user_config_file()
    raw = _read_json(file)
    raw["defaultStore"] = name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
