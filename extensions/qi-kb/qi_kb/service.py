"""给 qi-lab / CLI 的稳定面。

约定：同步阻塞、返回 dict、失败抛 `KbError`。qi-lab 自己用 `asyncio.to_thread` 包一层。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import auth, config, format as fmt, providers
from .client import KbClient
from .errors import KbError, NotFound, UsageError

#: 会话级 KB 选择（qi-lab 的 forwardedProps.kbIds 写进来）。
#: 进程内、不落盘；会话结束/下一轮开始前由调用方清掉。
_SESSION_SCOPES: dict[str, dict[str, Any]] = {}

DEFAULT_KEYS = {
    "topK": int,
    "retrievalStrategy": str,
    "hybridAlpha": float,
    "useRerank": bool,
    "rerankTopK": int,
    "rerankScoreThreshold": float,
    "maxChars": int,
    "evidenceScoreThreshold": float,
    "reuseSession": bool,
}


def set_session_scope(session_id: str | None, selectors: list[str] | None = None,
                      provider: str | None = None) -> None:
    if not session_id:
        return
    _SESSION_SCOPES[str(session_id)] = {
        "selectors": [str(s).strip() for s in (selectors or []) if str(s).strip()],
        "provider": provider or None,
    }


def clear_session_scope(session_id: str | None) -> None:
    if session_id:
        _SESSION_SCOPES.pop(str(session_id), None)


def session_scope(session_id: str | None) -> dict[str, Any] | None:
    return _SESSION_SCOPES.get(str(session_id)) if session_id else None


def unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "providers": [], "knowledgeBases": [],
            "defaults": {}, "files": [], "warnings": [], "error": reason}


def status(cwd: Path | None = None) -> dict[str, Any]:
    try:
        cfg = config.load(cwd)
    except KbError as exc:
        return unavailable(fmt.error_text(exc))
    store = auth.AuthStore()
    rows = providers.provider_rows(cfg, auth_store=store)
    bound = [_bound_item(ref) for ref in config.bound(cfg)]
    return {
        "available": True,
        "providers": rows,
        "knowledgeBases": bound,
        "defaults": config_defaults(cfg),
        "defaultProvider": cfg.default_provider_name() or "",
        "defaultKnowledgeBase": cfg.default_knowledge_base or "",
        "files": [str(path) for path in cfg.files],
        "warnings": list(cfg.warnings),
        "error": "",
    }


def providers_list(cwd: Path | None = None) -> dict[str, Any]:
    cfg = config.load(cwd)
    return {"providers": providers.provider_rows(cfg, auth_store=auth.AuthStore()),
            "warnings": list(cfg.warnings)}


def provider_add(cwd: Path | None, name: str, *, base_url: str,
                 api_prefix: str = "/api", login: str | None = None,
                 role: str | None = None, instance: str | None = None,
                 scope: str = "global") -> dict[str, Any]:
    return providers.add_provider(cwd, name, base_url=base_url,
                                  api_prefix=api_prefix, login=login, role=role,
                                  instance=instance, scope=scope)


def provider_login(cwd: Path | None, name: str, login: str, password: str, *,
                   base_url: str | None = None, api_prefix: str = "/api",
                   scope: str = "global", instance: str | None = None) -> dict[str, Any]:
    return providers.login_provider(cwd, name, login, password,
                                    base_url=base_url, api_prefix=api_prefix,
                                    scope=scope, instance=instance)


def provider_remove(cwd: Path | None, name: str, *, cascade: bool = False) -> dict[str, Any]:
    return providers.remove_provider(cwd, name, cascade=cascade)


def config_defaults(cfg: config.KbConfig) -> dict[str, Any]:
    raw = cfg.defaults if isinstance(cfg.defaults, dict) else {}
    out = {
        "topK": 5,
        "retrievalStrategy": "hybrid",
        "hybridAlpha": 0.7,
        "useRerank": True,
        "rerankTopK": 3,
        "rerankScoreThreshold": 0.0,
        "maxChars": 12000,
        "evidenceScoreThreshold": 0.35,
        "reuseSession": True,
    }
    out.update({key: raw[key] for key in DEFAULT_KEYS if key in raw})
    return out


def defaults(cwd: Path | None = None) -> dict[str, Any]:
    return config_defaults(config.load(cwd))


def set_defaults(cwd: Path | None, values: dict[str, Any], *,
                 scope: str = "global") -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, cast in DEFAULT_KEYS.items():
        if key not in values or values[key] is None:
            continue
        value = values[key]
        try:
            if cast is bool:
                if isinstance(value, str):
                    value = value.strip().lower() in ("1", "true", "yes", "on")
                else:
                    value = bool(value)
            else:
                value = cast(value)
        except (TypeError, ValueError) as exc:
            raise UsageError(f"defaults.{key} 类型不对：{value!r}") from exc
        cleaned[key] = value
    if not cleaned:
        raise UsageError("没有可写的默认参数")
    config.save_defaults(scope, cleaned, cwd=cwd)
    return config_defaults(config.load(cwd))


# ── 可用 / 文档 ──────────────────────────────────────────


def available(cwd: Path | None = None, *, provider: str | None = None,
              q: str | None = None, bound_only: bool = False) -> dict[str, Any]:
    cfg = config.load(cwd)
    if bound_only:
        refs = config.bound(cfg)
        if provider:
            refs = [ref for ref in refs if ref.provider == provider]
        items = [_bound_item(ref) for ref in refs]
        return _filter_items(items, q, provider, warnings=cfg.warnings)

    names = [provider] if provider else cfg.provider_names()
    if provider and provider not in cfg.providers:
        known = ", ".join(cfg.provider_names()) or "(无)"
        raise NotFound(f"没有 provider `{provider}`", hint=f"已配置：{known}")
    if not names:
        return {
            "available": True, "items": [], "total": 0,
            "warnings": ["尚未配置任何 provider：`qi kb provider add …`"],
            "error": "",
        }

    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    store = auth.AuthStore()
    for name in names:
        try:
            with KbClient(cfg, name, auth_store=store) as client:
                raw_items = client.list_available()
        except KbError as exc:
            warnings.append(f"{name}: {fmt.error_text(exc)}")
            continue
        refs = config.iter_kbs(cfg)
        instance = config.provider_instance(cfg.providers[name])
        for raw in raw_items:
            item = _raw_item(raw, name)
            match = next(
                (ref for ref in refs
                 if ref.instance == instance and ref.kb_id == item["id"]),
                None,
            )
            item["bound"] = match is not None
            item["boundProvider"] = match.provider if match else None
            items.append(item)
    return _filter_items(items, q, provider, warnings=warnings + cfg.warnings)


def documents(cwd: Path | None, kb: str, *, provider: str | None = None) -> dict[str, Any]:
    cfg = config.load(cwd)
    ref = config.find_kb(cfg, f"{kb}@{provider}" if provider and "@" not in kb else kb)
    with KbClient(cfg, ref.provider, auth_store=auth.AuthStore()) as client:
        docs = client.documents(ref.kb_id)
    return {"available": True, "provider": ref.provider, "kbId": ref.kb_id,
            "name": ref.name, "documents": docs, "total": len(docs),
            "warnings": []}


# ── 安装 / 卸载 ──────────────────────────────────────────


def plan_install(cwd: Path | None, kb: str, provider: str, *,
                 scope: str = "global", rebind: bool = False) -> dict[str, Any]:
    cfg = config.load(cwd)
    provider_cfg = cfg.provider(provider)
    entry = _fetch_kb_entry(cfg, provider_cfg, provider, kb)
    plan = providers.plan_install(cwd, provider, entry, scope=scope,
                                  rebind=rebind, cfg=cfg)
    plan["kb"] = entry
    return plan


def install(cwd: Path | None, kb: str, provider: str, *,
            scope: str = "global", rebind: bool = False) -> dict[str, Any]:
    cfg = config.load(cwd)
    provider_cfg = cfg.provider(provider)
    entry = _fetch_kb_entry(cfg, provider_cfg, provider, kb)
    result = providers.install_kb(cwd, provider, entry, scope=scope,
                                  rebind=rebind, cfg=cfg)
    result["kb"] = entry
    return result


def install_from_source(cwd: Path | None, provider: str, source: dict[str, Any],
                        kb: dict[str, Any], *, scope: str = "global",
                        rebind: bool = False, copy_auth: bool = True) -> dict[str, Any]:
    return providers.install_from_source(cwd, provider, source, kb, scope=scope,
                                         rebind=rebind, copy_auth=copy_auth)


def uninstall(cwd: Path | None, kb: str) -> dict[str, Any]:
    return providers.uninstall_kb(cwd, kb)


def rebind(cwd: Path | None, kb: str, provider: str, *,
           scope: str = "global") -> dict[str, Any]:
    return providers.rebind_kb(cwd, kb, provider, scope=scope)


# ── 检索 / 问答 ──────────────────────────────────────────


def search(cwd: Path | None, query: str, *, kb_ids: Iterable[str] | None = None,
           provider: str | None = None, top_k: int | None = None,
           strategy: str | None = None, use_rerank: bool | None = None,
           rerank_top_k: int | None = None,
           rerank_score_threshold: float | None = None,
           kb_hit_threshold: float | None = None,
           session_id: str | None = None) -> dict[str, Any]:
    cfg = config.load(cwd)
    selectors, provider = _scope_selectors(session_id, list(kb_ids or []), provider)
    refs = config.resolve_many(cfg, selectors, provider=provider)
    defaults = config_defaults(cfg)
    kwargs = {
        "top_k": int(top_k or defaults["topK"]),
        "strategy": strategy or defaults["retrievalStrategy"],
        "hybrid_alpha": float(defaults.get("hybridAlpha", 0.7)),
        "use_rerank": defaults["useRerank"] if use_rerank is None else bool(use_rerank),
        "rerank_top_k": int(rerank_top_k or defaults["rerankTopK"]),
        "rerank_score_threshold": float(
            defaults["rerankScoreThreshold"] if rerank_score_threshold is None
            else rerank_score_threshold),
        "kb_hit_threshold": kb_hit_threshold,
    }
    results: list[dict[str, Any]] = []
    warnings: list[str] = []
    used_ids: list[str] = []
    strategy_used = ""
    rerank_used: Any = None
    store = auth.AuthStore()
    for name, group in _group_by_provider(refs):
        ids = [ref.kb_id for ref in group]
        used_ids.extend(ids)
        try:
            with KbClient(cfg, name, auth_store=store) as client:
                data = client.search(query, ids, **kwargs)
        except KbError as exc:
            warnings.append(f"{name}: {fmt.error_text(exc)}")
            continue
        strategy_used = strategy_used or str(data.get("strategy") or kwargs["strategy"])
        rerank_used = rerank_used or data.get("rerank_method")
        for item in data.get("results") or []:
            if isinstance(item, dict):
                item = dict(item)
                item["provider"] = name
                item.setdefault("kbName", item.get("kb_name"))
                item.setdefault("docName", item.get("doc_name")
                                or (item.get("metadata") or {}).get("filename"))
                item.setdefault("kbId", item.get("kb_id"))
                item.setdefault("docId", item.get("doc_id"))
                results.append(item)
    if not results and warnings:
        raise _first_warning(warnings)
    return {
        "provider": _provider_label(refs),
        "query": query,
        "results": results,
        "total": len(results),
        "strategy": strategy_used or kwargs["strategy"],
        "rerankMethod": rerank_used,
        "kbIds": list(dict.fromkeys(used_ids)),
        "warnings": warnings,
    }


def ask(cwd: Path | None, question: str, *, kb_ids: Iterable[str] | None = None,
        provider: str | None = None, top_k: int | None = None,
        strategy: str | None = None, use_rerank: bool | None = None,
        rerank_top_k: int | None = None,
        rerank_score_threshold: float | None = None,
        kb_hit_threshold: float | None = None,
        session_id: str | None = None,
        evidence_score_threshold: float | None = None) -> dict[str, Any]:
    cfg = config.load(cwd)
    selectors, provider = _scope_selectors(session_id, list(kb_ids or []), provider)
    refs = config.resolve_many(cfg, selectors, provider=provider)
    defaults = config_defaults(cfg)
    reuse = bool(defaults.get("reuseSession", True))
    threshold = float(defaults["evidenceScoreThreshold"] if evidence_score_threshold is None
                      else evidence_score_threshold)
    kwargs = {
        "top_k": int(top_k or defaults["topK"]),
        "strategy": strategy or defaults["retrievalStrategy"],
        "hybrid_alpha": float(defaults.get("hybridAlpha", 0.7)),
        "use_rerank": defaults["useRerank"] if use_rerank is None else bool(use_rerank),
        "rerank_top_k": int(rerank_top_k or defaults["rerankTopK"]),
        "rerank_score_threshold": float(
            defaults["rerankScoreThreshold"] if rerank_score_threshold is None
            else rerank_score_threshold),
        "kb_hit_threshold": kb_hit_threshold,
    }
    answers: list[str] = []
    sources: list[dict[str, Any]] = []
    used_ids: list[str] = []
    warnings: list[str] = []
    store = auth.AuthStore()
    for name, group in _group_by_provider(refs):
        ids = [ref.kb_id for ref in group]
        used_ids.extend(ids)
        qa_session_id = _qa_session_id(session_id, name, ids) if reuse else None
        try:
            with KbClient(cfg, name, auth_store=store) as client:
                data = client.ask(question, ids, qa_session_id=qa_session_id, **kwargs)
        except KbError as exc:
            warnings.append(f"{name}: {fmt.error_text(exc)}")
            continue
        answer = str(data.get("answer") or "").strip()
        if answer:
            if len(refs) > 1:
                answers.append(f"### {name}\n\n{answer}")
            else:
                answers.append(answer)
        for source in data.get("sources") or []:
            if isinstance(source, dict):
                source = dict(source)
                source["provider"] = name
                sources.append(source)
    if not answers and warnings:
        raise _first_warning(warnings)
    top_score = max((_number(s.get("score")) for s in sources), default=0.0)
    return {
        "provider": _provider_label(refs),
        "question": question,
        "answer": "\n\n---\n\n".join(answers),
        "sources": sources,
        "kbIdsUsed": list(dict.fromkeys(used_ids)),
        "evidenceScoreThreshold": threshold,
        "evidenceSufficient": bool(sources) and top_score >= threshold,
        "warnings": warnings,
    }


def ask_stream(cwd: Path | None, question: str, *, kb_ids: Iterable[str] | None = None,
               provider: str | None = None, top_k: int | None = None,
               strategy: str | None = None, use_rerank: bool | None = None,
               rerank_top_k: int | None = None,
               rerank_score_threshold: float | None = None,
               kb_hit_threshold: float | None = None,
               session_id: str | None = None) -> Iterator[dict[str, Any]]:
    """流式问答（一次只允许一个 provider）。"""
    cfg = config.load(cwd)
    selectors, provider = _scope_selectors(session_id, list(kb_ids or []), provider)
    refs = config.resolve_many(cfg, selectors, provider=provider)
    groups = _group_by_provider(refs)
    if len(groups) > 1:
        names = "、".join(name for name, _ in groups)
        raise UsageError(
            "流式问答一次只能选一个 provider",
            hint=f"当前命中 {names}；用 `--provider` 或 `名称@provider` 收窄",
        )
    name, group = groups[0]
    ids = [ref.kb_id for ref in group]
    defaults = config_defaults(cfg)
    reuse = bool(defaults.get("reuseSession", True))
    kwargs = {
        "top_k": int(top_k or defaults["topK"]),
        "strategy": strategy or defaults["retrievalStrategy"],
        "hybrid_alpha": float(defaults.get("hybridAlpha", 0.7)),
        "use_rerank": defaults["useRerank"] if use_rerank is None else bool(use_rerank),
        "rerank_top_k": int(rerank_top_k or defaults["rerankTopK"]),
        "rerank_score_threshold": float(defaults["rerankScoreThreshold"]),
        "kb_hit_threshold": kb_hit_threshold,
        "qa_session_id": _qa_session_id(session_id, name, ids) if reuse else None,
    }
    with KbClient(cfg, name, auth_store=auth.AuthStore()) as client:
        for event in client.ask_stream(question, ids, **kwargs):
            kind = str(event.get("type") or "")
            if kind == "sources":
                sources = []
                for source in event.get("content") or []:
                    if isinstance(source, dict):
                        source = dict(source)
                        source["provider"] = name
                        sources.append(source)
                yield {"type": "sources", "content": sources, "provider": name}
            elif kind == "done":
                yield {"type": "done", "provider": name, "kbIds": ids}
            else:
                yield {**event, "provider": name}


def prompt_section(cwd: Path | None = None, session_id: str | None = None) -> str:
    try:
        cfg = config.load(cwd)
        refs = config.bound(cfg)
        scope = _SESSION_SCOPES.get(str(session_id)) if session_id else None
        if scope:
            selectors = scope.get("selectors") or []
            if selectors:
                refs = [config.find_kb(cfg, name) for name in selectors]
            provider = scope.get("provider")
            if provider:
                refs = [ref for ref in refs if ref.provider == provider]
        return fmt.prompt_section(cfg, refs)
    except KbError:
        return ""


# ── 内部 ──────────────────────────────────────────────────


def _fetch_kb_entry(cfg: config.KbConfig, provider_cfg: dict[str, Any],
                    provider: str, selector: str) -> dict[str, Any]:
    key = str(selector or "").strip()
    if not key:
        raise UsageError("知识库 id / 名称不能为空")
    with KbClient(cfg, provider, auth_store=auth.AuthStore()) as client:
        items = client.list_available()
    exact = [item for item in items if str(item.get("kb_id") or item.get("id")) == key]
    if exact:
        return exact[0]
    named = [item for item in items if str(item.get("name") or "").lower() == key.lower()]
    if len(named) == 1:
        return named[0]
    partial = [item for item in items if key.lower() in str(item.get("name") or "").lower()]
    if len(partial) == 1:
        return partial[0]
    if partial or named:
        names = "、".join(str(item.get("name")) for item in (partial or named)[:8])
        raise NotFound(f"`{selector}` 有歧义", hint=f"候选：{names}")
    raise NotFound(
        f"provider `{provider}` 的可见知识库里没有 `{selector}`",
        hint="先 `qi kb list --provider …` 看看",
    )


def _raw_item(raw: dict[str, Any], provider: str) -> dict[str, Any]:
    kb_id = str(raw.get("kb_id") or raw.get("id") or "")
    return {
        "id": kb_id,
        "provider": provider,
        "name": str(raw.get("name") or kb_id),
        "description": str(raw.get("description") or ""),
        "embeddingModel": str(raw.get("embedding_model") or raw.get("embeddingModel") or ""),
        "visibility": str(raw.get("visibility") or "private"),
        "docCount": int(raw.get("doc_count") or raw.get("docCount") or 0),
        "chunkCount": int(raw.get("chunk_count") or raw.get("chunkCount") or 0),
        "updatedAt": str(raw.get("updated_at") or raw.get("updatedAt") or ""),
        "icon": str(raw.get("icon") or ""),
        "bound": False,
        "boundProvider": None,
    }


def _bound_item(ref: config.KbRef) -> dict[str, Any]:
    entry = ref.entry
    return {
        "id": ref.kb_id,
        "provider": ref.provider,
        "boundProvider": ref.provider,
        "name": ref.name,
        "description": str(entry.get("description") or ""),
        "embeddingModel": str(entry.get("embeddingModel") or ""),
        "visibility": str(entry.get("visibility") or "private"),
        "docCount": int(entry.get("docCount") or 0),
        "chunkCount": int(entry.get("chunkCount") or 0),
        "updatedAt": str(entry.get("updatedAt") or ""),
        "icon": str(entry.get("icon") or ""),
        "bound": True,
        "scope": ref.scope,
    }


def _filter_items(items: list[dict[str, Any]], q: str | None,
                  provider: str | None, *, warnings: Iterable[str]) -> dict[str, Any]:
    if provider:
        items = [item for item in items if item.get("provider") == provider]
    if q:
        needle = q.strip().lower()
        items = [item for item in items
                 if needle in str(item.get("name", "")).lower()
                 or needle in str(item.get("description", "")).lower()]
    return {"available": True, "items": items, "total": len(items),
            "warnings": list(warnings), "error": ""}


def _group_by_provider(refs: Iterable[config.KbRef]) -> list[tuple[str, list[config.KbRef]]]:
    groups: dict[str, list[config.KbRef]] = {}
    for ref in refs:
        groups.setdefault(ref.provider, []).append(ref)
    return list(groups.items())


def _provider_label(refs: Iterable[config.KbRef]) -> str | None:
    names = list(dict.fromkeys(ref.provider for ref in refs))
    if not names:
        return None
    return names[0] if len(names) == 1 else ", ".join(names)


def _qa_session_id(session_id: str | None, provider: str, ids: list[str]) -> str | None:
    if not session_id:
        return None
    scope = ",".join(sorted(ids))
    return hashlib.md5(f"{session_id}:{provider}:{scope}".encode("utf-8")).hexdigest()


def _scope_selectors(session_id: str | None, selectors: list[str],
                     provider: str | None) -> tuple[list[str], str | None]:
    if selectors:
        return selectors, provider
    scope = _SESSION_SCOPES.get(str(session_id)) if session_id else None
    if not scope:
        return selectors, provider
    return list(scope.get("selectors") or []), (provider or scope.get("provider"))


def _first_warning(warnings: list[str]) -> KbError:
    text = warnings[0] if warnings else "知识库调用失败"
    return KbError(text, hint="检查 provider 登录与网络；其它 provider 的结果可能仍可用")


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
