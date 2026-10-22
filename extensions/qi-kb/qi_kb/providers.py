"""provider / KB 的安装、去重、rebind 与 provider 管理。

配置写入只在这里发生（store 的 `kb` kind 也调用这里），所以 `kb.json` 的格式
只有一份实现。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import auth, config
from .errors import Conflict, NotFound, UsageError

#: `kb.json.providers.<name>` 里允许由安装器写入的字段
PROVIDER_FROM_SOURCE = ("baseUrl", "apiPrefix", "instance", "login", "role")


def _entry(kb: dict[str, Any]) -> dict[str, Any]:
    kb_id = str(kb.get("id") or kb.get("kb_id") or "").strip()
    if not kb_id:
        raise UsageError("知识库条目缺少 id")
    return {
        "id": kb_id,
        "name": str(kb.get("name") or kb_id),
        "description": str(kb.get("description") or ""),
        "embeddingModel": str(kb.get("embeddingModel") or kb.get("embedding_model") or ""),
        "visibility": str(kb.get("visibility") or "private"),
        "docCount": int(kb.get("docCount") or kb.get("doc_count") or 0),
        "chunkCount": int(kb.get("chunkCount") or kb.get("chunk_count") or 0),
        "updatedAt": str(kb.get("updatedAt") or kb.get("updated_at") or ""),
        "icon": str(kb.get("icon") or ""),
    }


def _provider_data(provider_name: str, source: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    base_url = str(source.get("baseUrl") or source.get("base_url") or "").strip()
    if base_url:
        data["baseUrl"] = base_url.rstrip("/")
    api_prefix = source.get("apiPrefix") or source.get("api_prefix")
    if api_prefix:
        data["apiPrefix"] = str(api_prefix)
    instance = source.get("instance")
    if instance:
        data["instance"] = str(instance)
    if source.get("login"):
        data["login"] = str(source["login"])
    if source.get("role"):
        data["role"] = str(source["role"])
    return data


def _copy_auth(provider_name: str, source: dict[str, Any], *,
               auth_store: auth.AuthStore | None = None,
               copy_auth: bool = True) -> None:
    if not copy_auth:
        return
    payload = source.get("auth") if isinstance(source.get("auth"), dict) else {}
    access = payload.get("access") or source.get("access")
    refresh = payload.get("refresh") or source.get("refresh")
    if not access and not refresh:
        return
    entry = {
        "login": payload.get("login") or source.get("login"),
        "role": payload.get("role") or source.get("role"),
        "access": access,
        "refresh": refresh,
        "exp": payload.get("exp") or auth.token_exp(str(access or "")),
    }
    (auth_store or auth.AuthStore()).set(provider_name, entry)


def plan_install(cwd: Path | None, provider_name: str, kb: dict[str, Any], *,
                 scope: str = "global", rebind: bool = False,
                 provider_data: dict[str, Any] | None = None,
                 cfg: config.KbConfig | None = None) -> dict[str, Any]:
    """只算不写。返回 install / update / rebind / blocked。"""
    cfg = cfg or config.load(cwd)
    provider_name = config.validate_provider_name(provider_name)
    entry = _entry(kb)
    kb_id = entry["id"]

    if provider_name in cfg.providers:
        provider = cfg.providers[provider_name]
    elif provider_data:
        provider = dict(provider_data)
    else:
        known = ", ".join(cfg.provider_names()) or "(无)"
        raise NotFound(f"没有 provider `{provider_name}`", hint=f"已配置：{known}")

    target_instance = config.provider_instance(provider)
    existing = next(
        (ref for ref in config.iter_kbs(cfg)
         if ref.instance == target_instance and ref.kb_id == kb_id),
        None,
    )
    if existing is None:
        return {
            "action": "install", "kbId": kb_id, "provider": provider_name,
            "scope": scope, "blocked": False, "existingProvider": None,
        }
    if existing.provider == provider_name:
        return {
            "action": "update", "kbId": kb_id, "provider": provider_name,
            "scope": existing.scope, "blocked": False,
            "existingProvider": provider_name,
        }
    return {
        "action": "rebind", "kbId": kb_id, "provider": provider_name,
        "scope": scope, "blocked": not rebind,
        "existingProvider": existing.provider,
        "existingScope": existing.scope,
        "instance": target_instance,
    }


def install_kb(cwd: Path | None, provider_name: str, kb: dict[str, Any], *,
               scope: str = "global", rebind: bool = False,
               provider_data: dict[str, Any] | None = None,
               cfg: config.KbConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config.load(cwd)
    plan = plan_install(cwd, provider_name, kb, scope=scope, rebind=rebind,
                        provider_data=provider_data, cfg=cfg)
    if plan["blocked"]:
        existing = plan.get("existingProvider") or "另一个 provider"
        raise Conflict(
            f"该 KB 已由 {existing} 安装（同实例同 KB）",
            existing_provider=str(existing), target_provider=provider_name,
            hint=f"覆盖用 `--rebind`（迁移到 {provider_name}），保留用 `--keep`",
        )

    entry = _entry(kb)
    kb_id = entry["id"]
    if provider_data:
        config.save_provider(scope, provider_name, provider_data, cwd=cwd)

    action = plan["action"]
    if action == "rebind":
        existing = next(
            (ref for ref in config.iter_kbs(cfg)
             if ref.kb_id == kb_id
             and ref.provider == plan.get("existingProvider")
             and ref.instance == plan.get("instance")),
            None,
        )
        if existing is not None:
            config.remove_kb(existing.scope, existing.provider, kb_id, cwd=cwd)

    if action == "update":
        # 同 provider 重复安装：更新元数据，留在原来的层，避免全局/项目各一份
        target_scope = plan.get("scope") or scope
        config.save_kb(target_scope, provider_name, kb_id, entry,
                       provider=provider_data, cwd=cwd)
    else:
        config.save_kb(scope, provider_name, kb_id, entry,
                       provider=provider_data, cwd=cwd)
    return {**plan, "action": action, "blocked": False}


def uninstall_kb(cwd: Path | None, selector: str, *,
                 cfg: config.KbConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config.load(cwd)
    ref = config.find_kb(cfg, selector)
    config.remove_kb(ref.scope, ref.provider, ref.kb_id, cwd=cwd)
    return {
        "removed": True, "kbId": ref.kb_id, "name": ref.name,
        "provider": ref.provider, "scope": ref.scope,
    }


def rebind_kb(cwd: Path | None, selector: str, target_provider: str, *,
              scope: str = "global", cfg: config.KbConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config.load(cwd)
    ref = config.find_kb(cfg, selector)
    target = cfg.provider(target_provider)
    if config.provider_instance(target) != ref.instance:
        raise UsageError(
            f"`{ref.provider}` 与 `{target_provider}` 不是同一个 hikqin 实例，不能 rebind",
            hint="不同实例下同 kbId 是不同知识库，请直接 install",
        )
    config.remove_kb(ref.scope, ref.provider, ref.kb_id, cwd=cwd)
    target_scope = cfg.provider_scope.get(target_provider, scope)
    config.save_kb(target_scope, target_provider, ref.kb_id, ref.entry, cwd=cwd)
    return {
        "action": "rebind", "kbId": ref.kb_id, "name": ref.name,
        "from": ref.provider, "to": target_provider, "scope": target_scope,
    }


# ── provider ──────────────────────────────────────────────


def add_provider(cwd: Path | None, name: str, *, base_url: str,
                 api_prefix: str = "/api", login: str | None = None,
                 role: str | None = None, instance: str | None = None,
                 scope: str = "global") -> dict[str, Any]:
    name = config.validate_provider_name(name)
    if not str(base_url or "").strip():
        raise UsageError("baseUrl 不能为空")
    provider: dict[str, Any] = {
        "baseUrl": str(base_url).strip().rstrip("/"),
        "apiPrefix": str(api_prefix or "/api"),
    }
    if login:
        provider["login"] = login
    if role:
        provider["role"] = role
    if instance:
        provider["instance"] = instance
    config.save_provider(scope, name, provider, cwd=cwd)
    return {"name": name, "scope": scope, **provider}


def login_provider(cwd: Path | None, name: str, login: str, password: str, *,
                   base_url: str | None = None, api_prefix: str = "/api",
                   scope: str = "global", instance: str | None = None,
                   auth_store: auth.AuthStore | None = None,
                   http: Any = None) -> dict[str, Any]:
    name = config.validate_provider_name(name)
    cfg = config.load(cwd)
    if name not in cfg.providers:
        if not base_url:
            known = ", ".join(cfg.provider_names()) or "(无)"
            raise NotFound(f"没有 provider `{name}`", hint=f"先 add 或给 --base-url；已配置：{known}")
        add_provider(cwd, name, base_url=base_url, api_prefix=api_prefix,
                     login=login, instance=instance, scope=scope)
        cfg = config.load(cwd)
    entry = auth.login(cfg, name, login, password, auth_store=auth_store, http=http)
    provider_scope = cfg.provider_scope.get(name, scope)
    config.save_provider(provider_scope, name, {
        "login": entry.get("login") or login,
        **({"role": entry["role"]} if entry.get("role") else {}),
    }, cwd=cwd)
    return {"name": name, "login": entry.get("login"), "role": entry.get("role"),
            "exp": entry.get("exp")}


def remove_provider(cwd: Path | None, name: str, *, cascade: bool = False,
                    auth_store: auth.AuthStore | None = None) -> dict[str, Any]:
    cfg = config.load(cwd)
    refs = [ref for ref in config.iter_kbs(cfg) if ref.provider == name]
    if refs and not cascade:
        raise Conflict(
            f"provider `{name}` 下还有 {len(refs)} 个知识库",
            hint="先 `qi kb uninstall`，或 `--cascade` 连知识库一起删",
        )
    removed = []
    for ref in refs:
        config.remove_kb(ref.scope, ref.provider, ref.kb_id, cwd=cwd)
        removed.append(ref.kb_id)
    for scope in ("global", "project"):
        config.remove_provider(scope, name, cwd=cwd)
    (auth_store or auth.AuthStore()).remove(name)
    return {"removed": True, "provider": name, "knowledgeBases": removed}


def provider_rows(cfg: config.KbConfig, *, auth_store: auth.AuthStore | None = None,
                  clock: Any = time.time) -> list[dict[str, Any]]:
    store = auth_store or auth.AuthStore()
    rows = []
    for name in cfg.provider_names():
        provider = cfg.providers[name]
        entry = store.get(name) or {}
        exp = entry.get("exp")
        exp_int = int(exp) if isinstance(exp, (int, float)) else auth.token_exp(
            str(entry.get("access") or ""))
        rows.append({
            "name": name,
            "baseUrl": provider.get("baseUrl"),
            "apiPrefix": provider.get("apiPrefix") or config.DEFAULT_API_PREFIX,
            "instance": config.provider_instance(provider),
            "login": entry.get("login") or provider.get("login") or "",
            "role": entry.get("role") or provider.get("role") or "",
            "loggedIn": bool(entry.get("access")),
            "expiresAt": exp_int,
            "expiresIn": int(exp_int - clock()) if exp_int else None,
            "kbCount": len(provider.get("knowledgeBases") or {}),
            "scope": cfg.provider_scope.get(name, "global"),
        })
    return rows


def install_from_source(cwd: Path | None, provider_name: str,
                        source: dict[str, Any], kb: dict[str, Any], *,
                        scope: str = "global", rebind: bool = False,
                        copy_auth: bool = True,
                        auth_store: auth.AuthStore | None = None) -> dict[str, Any]:
    """qi-store 的入口：把 store source 的账号复制到 qi-kb 自己的配置。"""
    provider_data = _provider_data(provider_name, source)
    cfg = config.load(cwd)
    if provider_name in cfg.providers:
        # 不覆盖用户已经手写/改过的 provider，只补缺
        current = cfg.providers[provider_name]
        merged = {**provider_data, **{
            k: current[k] for k in PROVIDER_FROM_SOURCE if k in current
        }}
        merged["baseUrl"] = current.get("baseUrl") or provider_data.get("baseUrl")
        merged["apiPrefix"] = current.get("apiPrefix") or provider_data.get("apiPrefix") or "/api"
        provider_data = merged

    # 先查重：冲突且不覆盖时**不写 auth**（一次失败的安装不留副作用）
    planned = plan_install(cwd, provider_name, kb, scope=scope, rebind=rebind,
                           provider_data=provider_data, cfg=cfg)
    if planned.get("blocked"):
        existing = planned.get("existingProvider") or "另一个 provider"
        raise Conflict(
            f"该 KB 已由 {existing} 安装（同实例同 KB）",
            existing_provider=str(existing), target_provider=provider_name,
            hint=f"覆盖用 `--rebind`（迁移到 {provider_name}），保留用 `--keep`",
        )
    _copy_auth(provider_name, source, auth_store=auth_store, copy_auth=copy_auth)
    return install_kb(cwd, provider_name, kb, scope=scope, rebind=rebind,
                      provider_data=provider_data, cfg=cfg)
