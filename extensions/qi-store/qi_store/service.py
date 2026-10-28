"""给**别的扩展**用的稳定面(目前只有 qi-lab)。

这一层刻意不做 UI、不做 HTTP、不打印:每个函数收参数、返 `dict`、失败抛 `StoreError`。
两个理由:

1. `add_route` / `add_static` 在 qi 里**明确未实现**
   ([docs/extensions.md](../../../docs/extensions.md) §11),所以 qi-lab 只能像消费
   `qi_mcp.config` / `qi_agents.discovery` 那样**进程内 import** —— 那就需要一个
   "不是命令行"的入口;
2. 命令行已经长在 `cli.py` 上了(argparse、rich 表格、`input()`),那些搬进 web 宿主
   只会两边都难改。这里只保留**动作与数据**,共用的语义在 `ops.py`(两处必须一致)。

**约定**:所有函数**同步阻塞**(内部是 `httpx.Client`)。web 宿主自己用
`asyncio.to_thread` 包一层 —— 本模块不 import asyncio,也不猜调用方在什么事件循环里。

**没有 `login`**:密码不该经手浏览器。面板只读 `status()` 并提示"去终端 `qi store login`"。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import config, installer, ops, paths, planner
from .catalog import Catalog, StoreItem
from .client import token_exp
from .config import Source, StoreConfig
from .errors import Conflict, NotFound, StoreError, UsageError
from .kinds.base import PlanContext
from .ledger import Ledger

__all__ = ["status", "unavailable", "login", "logout", "search", "installed", "plan",
           "install", "uninstall", "check"]


# ── 状态 ────────────────────────────────────────────────


def status(cwd: Path | None = None) -> dict[str, Any]:
    """配置/登录状态。**不发请求**(断网也能画界面)。"""
    cfg = config.load(cwd)
    sources = []
    for name in sorted(cfg.sources):
        source = cfg.sources[name]
        exp = token_exp(source.access_token) if source.access_token else None
        sources.append({
            "name": name,
            "baseUrl": source.base_url,
            "default": name == cfg.default_store,
            "loggedIn": bool(source.access_token),
            # 空值一律给空串:Pydantic 的 `str` 字段不收 None,而"没登录"本来就没有名字
            "login": source.login_name or "",
            "role": str((source.auth or {}).get("role") or ""),
            "expiresAt": exp,
            "expiresIn": int(exp - time.time()) if exp else None,
        })
    return {
        "available": True,
        "sources": sources,
        "defaultScope": cfg.install.get("scope") or "global",
        "wanted": ops.wanted_specs(cfg),
        "warnings": cfg.warnings,
        "storeFile": str(paths.user_config_file()),
    }


def unavailable(reason: str) -> dict[str, Any]:
    """qi-store 没装 / 配置坏了时**仍然画得出来**的形状(qi-lab 的 `unavailable` 惯例)。"""
    return {"available": False, "sources": [], "error": reason}


# ── 登录 / 退出 ──────────────────────────────────────────
#
# **这两条是给 web 面板的**,也是"密码经手宿主进程"的唯一入口。三条约束:
#
# 1. 密码只在这一次调用里用,不落盘、不进日志、不回显;
# 2. 返回值是 `status()`(只有登录态),**令牌一个字节都不出去**;
# 3. 写凭证走 `config.save_auth`(用户级 + 0600),与命令行 `qi store login` 同一个函数 ——
#    两处写出来的文件必须一样。


def login(cwd: Path | None = None, *, login: str, password: str,
          base_url: str | None = None, store: str | None = None) -> dict[str, Any]:
    """调上游 `/auth/login` → 立刻用 `me()` 验证 → 写用户级 `store.json`。

    失败抛 `NotLoggedIn`(上游 401)或 `UsageError`(没给地址),**不写任何文件**。
    """
    cfg = config.load(cwd)
    name = store or cfg.default_store or config.DEFAULT_STORE_NAME
    existing = cfg.sources.get(name)
    url = (base_url or (existing.base_url if existing else None) or "").strip()
    if not url:
        raise UsageError(
            "不知道商店地址",
            hint=f"填一个后端地址(如 http://<host>:6091),会写进 {paths.user_config_file()}",
        )
    source = Source(
        name=name,
        base_url=url.rstrip("/"),
        api_prefix=existing.api_prefix if existing else config.DEFAULT_API_PREFIX,
    )
    if source.base_url.startswith("file:") or "://" not in source.base_url:
        raise UsageError(f"地址看着不对:{url}", hint="要 http(s)://host:port")

    # **自己给 on_auth_change**,不依赖 `ops.open_client` 的默认回调:落盘这件事
    # 不该藏在"建客户端"的副作用里(测试换掉那一层时,登录会看着成功、磁盘上什么都没有)。
    def _save(auth: dict[str, Any] | None) -> None:
        config.save_auth(name, auth, base_url=source.base_url)

    client = ops.open_client(source, None, on_auth_change=_save)
    try:
        client.login(login, password)
    finally:
        client.close()
    if not cfg.default_store:
        config.save_default(name)
    return status(cwd)


def logout(cwd: Path | None = None, *, store: str | None = None,
           all_stores: bool = False) -> dict[str, Any]:
    """删本地凭证(上游没有吊销接口)。幂等。"""
    cfg = config.load(cwd)
    names = sorted(cfg.sources) if all_stores else [
        store or cfg.default_store or config.DEFAULT_STORE_NAME]
    for name in names:
        if cfg.sources.get(name) is not None:
            config.save_auth(name, None)
    return status(cwd)


# ── 内部 ────────────────────────────────────────────────


def _cfg_and_source(cwd: Path | None, store: str | None = None) -> tuple[StoreConfig, Source]:
    cfg = config.load(cwd)
    return cfg, cfg.source(store)


def _catalog(cwd: Path | None, *, kind: str | None = None,
             offline: bool = False, store: str | None = None) -> tuple[Catalog, Source]:
    _cfg, source = _cfg_and_source(cwd, store)
    return ops.load_for(source, None, kinds=[kind] if kind else None, offline=offline), source


def _kb_modules():
    """qi-kb 装了吗？返回 `(config, providers, errors)`；没装返回 None。"""
    try:
        from qi_kb import config as kb_config
        from qi_kb import errors as kb_errors
        from qi_kb import paths as kb_paths
        from qi_kb import providers as kb_providers
    except Exception:  # noqa: BLE001
        return None
    return kb_config, kb_providers, kb_errors, kb_paths


def _kb_source(source: Source) -> dict[str, Any]:
    return {
        "baseUrl": source.base_url,
        "apiPrefix": source.api_prefix,
        "login": source.login_name,
        "role": str((source.auth or {}).get("role") or ""),
        "auth": source.auth,
    }


def _kb_error(exc: Exception):
    """把 qi-kb 的 KbError 映射成 qi-store 的错误（退出码同源）。"""
    from .errors import EXIT_AUTH, EXIT_CONFLICT, EXIT_NOT_FOUND, EXIT_USAGE, NotLoggedIn

    code = getattr(exc, "exit_code", None)
    message = getattr(exc, "message", str(exc))
    hint = getattr(exc, "hint", None)
    if code == EXIT_AUTH:
        return NotLoggedIn(message, hint=hint)
    if code == EXIT_CONFLICT:
        return Conflict(message, hint=hint)
    if code == EXIT_NOT_FOUND:
        return NotFound(message, hint=hint)
    if code == EXIT_USAGE:
        return UsageError(message, hint=hint)
    return StoreError(message, hint=hint)


def _ctx(cwd: Path | None, source: Source, scope: str) -> PlanContext:
    return PlanContext(root=planner.scope_root(scope, cwd), scope=scope,
                       store_name=source.name, base_url=source.base_url,
                       client=ops.open_client(source, None))


# ── 读 ──────────────────────────────────────────────────


def search(cwd: Path | None = None, *, q: str | None = None, kind: str | None = None,
           limit: int = 0, offline: bool = False, store: str | None = None) -> dict[str, Any]:
    from .kinds import NOT_IMPLEMENTED

    if kind in NOT_IMPLEMENTED:
        # "这一类还没实现"要**当成一种正常结果**回:界面要显示理由,而不是弹一个 400
        _cfg, source = _cfg_and_source(cwd, store)
        return {"available": True, "store": source.name, "items": [], "total": 0,
                "missing": [{"kind": kind, "reason": NOT_IMPLEMENTED[kind]}],
                "cachedAt": None}
    catalog, source = _catalog(cwd, kind=kind, offline=offline, store=store)
    items = catalog.filter(kind=kind, keyword=q)
    if limit:
        items = items[:limit]
    ledger = Ledger.load()
    return {
        "available": True,
        "store": source.name,
        "items": [_item_out(item, ledger) for item in items],
        "total": len(items),
        "missing": [{"kind": k, "reason": r} for k, r in catalog.missing],
        "cachedAt": int(catalog.fetched_at) if offline else None,
    }


def _item_out(item: StoreItem, ledger: Ledger) -> dict[str, Any]:
    """目录里的一条 → 界面用。`installed` **按磁盘算**,不按账本算。

    账本有记录、盘上却没有(别处删了)→ 视作**未装**:按钮该是「安装」而不是「已装」。
    """
    from .verify import presence

    record = ledger.find(item.id)
    live = bool(record is not None and presence(record)[0])
    return {
        "id": item.id, "kind": item.kind, "name": item.name,
        "description": item.description, "version": item.version,
        "updatedAt": item.updated_at, "requiresConfig": item.requires_config,
        "safety": item.safety, "badges": item.badges,
        "installed": live,
        "installedVersion": record.version if live and record is not None else None,
        "updateAvailable": bool(live and record is not None and record.version != item.version),
    }


def installed(cwd: Path | None = None) -> dict[str, Any]:
    """已装清单。**每条都去磁盘上验一遍**(见 `verify.presence`)。

    账本只记"商店装过什么":从设置面 A2A/MCP 两节删掉、手工改 json、`rm -rf` 目录都不
    经过商店。不复核的话页面会一直标"已安装"—— 用户看到的是假话。
    """
    from .verify import presence

    ledger = Ledger.load()
    items = []
    for r in ledger.items:
        ok, why = presence(r)
        items.append({
            "id": r.itemId, "kind": r.kind, "name": r.name, "version": r.version,
            "scope": r.scope, "target": r.target, "needsConfig": r.needs_config,
            "installedAt": r.installed_at, "upstreamUpdatedAt": r.upstream_updated_at,
            "present": ok, "missingReason": why,
        })
    # KB 的“已安装”以 qi-kb 自己的 kb.json 为准（手动 `qi kb install` 也能看见）
    kb = _kb_modules()
    if kb is not None:
        try:
            kb_config_mod = kb[0]
            kb_config = kb_config_mod.load(cwd)
            kb_paths = kb[3]
            for ref in kb_config_mod.bound(kb_config):
                item_id = f"kb:{ref.kb_id}"
                if any(row["id"] == item_id for row in items):
                    continue
                items.append({
                    "id": item_id, "kind": "kb", "name": ref.name, "version": "",
                    "scope": ref.scope, "target": str(kb_paths.user_config_file()),
                    "needsConfig": [], "installedAt": "",
                    "upstreamUpdatedAt": str(ref.entry.get("updatedAt") or ""),
                    "present": True, "missingReason": "",
                })
        except Exception:  # noqa: BLE001 配置坏了不该让商店已装列表 500
            pass
    return {"available": True, "items": items}


# ── 计划 / 写 ───────────────────────────────────────────


def plan(cwd: Path | None = None, *, ids: list[str], scope: str | None = None,
         force: bool = False, store: str | None = None) -> dict[str, Any]:
    """**只看不动**:把"会写哪里 / 会不会撞 / 要补什么"算出来给界面画。"""
    cfg, source = _cfg_and_source(cwd, store)
    resolved_scope = scope or cfg.install.get("scope") or "global"
    catalog, _ = _catalog(cwd, store=store)
    ledger = Ledger.load()
    items = [ops.resolve(catalog, key) for key in ids]
    kb_items = [item for item in items if item.kind == "kb"]
    other_items = [item for item in items if item.kind != "kb"]

    rows: list[dict[str, Any]] = []
    blocked = False
    if kb_items:
        kb = _kb_modules()
        if kb is None:
            raise UsageError(
                "知识库安装需要 qi-kb",
                hint="`qi install qi-kb` 后再试",
            )
        _kb_config, kb_providers, kb_errors, kb_paths = kb
        for item in kb_items:
            try:
                planned = kb_providers.plan_install(
                    cwd, source.name, item.meta["entry"],
                    scope=resolved_scope, rebind=force,
                    provider_data=_kb_source(source),
                )
            except kb_errors.KbError as exc:
                raise _kb_error(exc) from exc
            conflict = None
            if planned.get("blocked"):
                conflict = (f"已由 {planned.get('existingProvider')} 安装"
                            f"（同实例同 KB）")
                blocked = True
            rows.append({
                "id": item.id, "name": item.name,
                "note": f"provider={source.name} · {item.badges.get('docs', 0)} docs / "
                        f"{item.badges.get('chunks', 0)} chunks",
                "targets": [str(kb_paths.user_config_file()), str(kb_paths.auth_file())],
                "needsConfig": [], "conflict": conflict, "unsafe": False,
                "action": planned.get("action"),
                "existingProvider": planned.get("existingProvider"),
                "targetProvider": source.name,
            })

    if other_items:
        ctx = _ctx(cwd, source, resolved_scope)
        try:
            built = planner.build(other_items, ctx, ledger=ledger)
        finally:
            if ctx.client is not None:
                ctx.client.close()
        rows.extend({
            "id": p.item.id, "name": p.item.name, "note": p.note,
            "targets": list(dict.fromkeys(str(a.target) for a in p.actions)),
            "needsConfig": p.needs_config, "conflict": p.conflict,
            "unsafe": p.item.is_unsafe(),
        } for p in built.items)
        if built.conflicts and not force:
            blocked = True
        skipped = [{"id": item.id, "reason": reason} for item, reason in built.skipped]
        needs = built.needs_config
    else:
        skipped = []
        needs = []

    return {
        "available": True,
        "scope": resolved_scope,
        "items": rows,
        "skipped": skipped,
        "needsConfig": needs,
        "blocked": blocked,
    }


def install(cwd: Path | None = None, *, ids: list[str], scope: str | None = None,
            force: bool = False, allow_unsafe: bool = False,
            store: str | None = None) -> dict[str, Any]:
    """真的装。**没有交互确认** —— 确认归调用方(qi-lab 的 `X-Qi-Confirm`)。"""
    cfg, source = _cfg_and_source(cwd, store)
    resolved_scope = scope or cfg.install.get("scope") or "global"
    catalog, _ = _catalog(cwd, store=store)
    ledger = Ledger.load()
    items = [ops.resolve(catalog, key) for key in ids]
    kb_items = [item for item in items if item.kind == "kb"]
    other_items = [item for item in items if item.kind != "kb"]

    installed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    needs: list[str] = []

    if kb_items:
        kb = _kb_modules()
        if kb is None:
            raise UsageError("知识库安装需要 qi-kb", hint="`qi install qi-kb` 后再试")
        _kb_config, kb_providers, kb_errors, kb_paths = kb
        for item in kb_items:
            try:
                result = kb_providers.install_from_source(
                    cwd, source.name, _kb_source(source), item.meta["entry"],
                    scope=resolved_scope, rebind=force,
                )
            except kb_errors.KbError as exc:
                raise _kb_error(exc) from exc
            installed.append({
                "id": item.id, "kind": "kb", "name": item.name,
                "version": item.version, "scope": result.get("scope", resolved_scope),
                "target": str(kb_paths.user_config_file()), "needsConfig": [],
                "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "upstreamUpdatedAt": item.updated_at,
                "action": result.get("action"),
            })

    if other_items:
        unsafe = [item for item in other_items if item.is_unsafe()]
        if unsafe and not allow_unsafe:
            raise UsageError(
                "有条目被上游标记为高危:" + ", ".join(item.id for item in unsafe),
                hint="确认要装请显式允许(allow_unsafe=true)",
            )
        ctx = _ctx(cwd, source, resolved_scope)
        try:
            built = planner.build(other_items, ctx, ledger=ledger)
            if built.conflicts and not force:
                raise Conflict(
                    "目标已存在(不是本账本装的):"
                    + ";".join(p.conflict or "" for p in built.conflicts),
                    hint="确认要覆盖请 force=true(会先备份)",
                )
            records = installer.apply(built, ledger=ledger, store=source.name,
                                      scope=resolved_scope, force=force)
        finally:
            if ctx.client is not None:
                ctx.client.close()
        installed.extend({
            "id": r.itemId, "kind": r.kind, "name": r.name, "version": r.version,
            "scope": r.scope, "target": r.target, "needsConfig": r.needs_config,
            "installedAt": r.installed_at, "upstreamUpdatedAt": r.upstream_updated_at,
        } for r in records)
        skipped = [{"id": item.id, "reason": reason} for item, reason in built.skipped]
        needs = built.needs_config

    return {
        "available": True,
        "scope": resolved_scope,
        "installed": installed,
        "skipped": skipped,
        "needsConfig": needs,
    }


def uninstall(cwd: Path | None = None, *, id: str, scope: str | None = None) -> dict[str, Any]:
    ledger = Ledger.load()
    record = ledger.find(id, scope) or ledger.by_name(id, scope)
    if record is None or record.kind == "kb" or str(id).startswith("kb:"):
        kb = _kb_modules()
        if kb is None:
            if record is None:
                raise NotFound(f"账本里没有 `{id}`", hint="先看已装列表")
            removed = installer.uninstall(record, ledger=ledger)
            return {"available": True, "id": record.itemId, "kind": record.kind,
                    "removed": removed}
        _kb_config, kb_providers, kb_errors, _kb_paths = kb
        selector = str(id).split(":", 1)[1] if str(id).startswith("kb:") else str(id)
        try:
            data = kb_providers.uninstall_kb(cwd, selector)
        except kb_errors.KbError as exc:
            raise _kb_error(exc) from exc
        if record is not None:
            ledger.remove(record.itemId, record.scope)
            ledger.save()
        return {"available": True, "id": f"kb:{data['kbId']}", "kind": "kb",
                "removed": [f"kb.json → {data['provider']}/{data['kbId']}"]}
    kind = record.kind
    removed = installer.uninstall(record, ledger=ledger)
    return {"available": True, "id": record.itemId, "kind": kind, "removed": removed}


def _status_of(record: Any, item: Any, status: str) -> str:
    """`ops.diff` 的中文状态 → 界面用的枚举。**"本地已被移除"优先**于一切。"""
    if status.startswith("本地已被移除"):
        return "missing"
    if item is None:
        return "gone"
    return "up-to-date" if item.version == record.version else "update"


def check(cwd: Path | None = None, *, offline: bool = False,
          store: str | None = None) -> dict[str, Any]:
    cfg, _source = _cfg_and_source(cwd, store)
    ledger = Ledger.load()
    catalog, _ = _catalog(cwd, offline=offline, store=store)
    rows = [{
        "id": record.itemId, "name": record.name, "scope": record.scope,
        "version": record.version,
        "upstreamVersion": item.version if item else None,
        "status": _status_of(record, item, status),
        "detail": status if status.startswith("本地已被移除") else "",
    } for record, item, status in ops.diff(ledger, catalog)]

    kb = _kb_modules()
    if kb is not None:
        try:
            kb_config, _providers, _errors, _paths = kb
            kb_cfg = kb_config.load(cwd)
            index = {item.id: item for item in catalog.items}
            for ref in kb_config.bound(kb_cfg):
                item = index.get(f"kb:{ref.kb_id}")
                if item is None:
                    status = "gone"
                else:
                    same = (str(ref.entry.get("updatedAt") or "") == item.updated_at
                            and int(ref.entry.get("docCount") or 0) == int(item.badges.get("docs") or 0)
                            and int(ref.entry.get("chunkCount") or 0) == int(item.badges.get("chunks") or 0))
                    status = "up-to-date" if same else "update"
                rows.append({
                    "id": f"kb:{ref.kb_id}", "name": ref.name, "scope": ref.scope,
                    "version": "", "upstreamVersion": item.version if item else None,
                    "status": status, "detail": "",
                })
        except Exception:  # noqa: BLE001
            pass

    missing: list[dict[str, str]] = []
    for spec in ops.wanted_specs(cfg):
        try:
            item = catalog.find(spec)
        except StoreError as exc:
            missing.append({"spec": spec, "reason": str(exc)})
            continue
        if ledger.find(item.id) is None:
            missing.append({"spec": spec, "reason": "声明了但没装"})
    return {"available": True, "items": rows, "wantedMissing": missing}
