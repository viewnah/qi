"""hikqin RAG 的 HTTP 客户端。

只允许少量路径：
  GET  /rag/kb/available
  GET  /rag/kb/{kb_id}/documents
  POST /rag/qa/retrieval/search
  POST /rag/qa/ask

`ask` 会在上游写 QA 历史（不是纯读）；其余都是只读。白名单写成常量，
新增路径必须显式改这里。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx

from . import auth, config
from .errors import NotLoggedIn, UpstreamError

LIST_TIMEOUT = 20.0
SEARCH_TIMEOUT = 60.0
ASK_TIMEOUT = 180.0

GET_PATHS = ("/rag/kb/available", "/rag/kb/")
POST_PATHS = ("/rag/qa/retrieval/search", "/rag/qa/ask", "/rag/qa/ask/stream")


def _is_allowed(method: str, path: str) -> bool:
    if method.upper() == "GET":
        return any(path == p or path.startswith(p) for p in GET_PATHS)
    if method.upper() == "POST":
        return path in POST_PATHS
    return False


def _unwrap_error(status: int, body: Any, url: str) -> None:
    message = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("detail")
    raise UpstreamError(f"上游返回 {status}：{message or url}", status=status)


def _unwrap(payload: Any) -> Any:
    return auth.unwrap(payload)


class KbClient:
    """一个 provider 的客户端。`http` 可注入（测试用 MockTransport）。"""

    def __init__(self, cfg: config.KbConfig, provider_name: str, *,
                 auth_store: auth.AuthStore | None = None,
                 http: httpx.Client | None = None,
                 timeout: float = LIST_TIMEOUT,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.provider_name = provider_name
        self.provider = cfg.provider(provider_name)
        self._auth_store = auth_store
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout, follow_redirects=True)
        self._timeout = timeout
        self._clock = clock

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "KbClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── HTTP ──────────────────────────────────────────────

    def _request(self, method: str, path: str, *,
                 params: dict[str, Any] | None = None,
                 payload: dict[str, Any] | None = None,
                 timeout: float | None = None) -> Any:
        if not _is_allowed(method, path):
            raise UpstreamError(f"内部错误：{method} {path} 不在白名单里")
        url = auth.api_url(self.provider, path)
        last_body: Any = None
        for attempt in (1, 2):
            info = auth.ensure_access(
                self.cfg, self.provider_name, auth_store=self._auth_store,
                http=self._http, force_refresh=(attempt == 2), clock=self._clock,
            )
            headers = {"Authorization": f"Bearer {info.access}"}
            try:
                response = self._http.request(
                    method, url, params=params, json=payload, headers=headers,
                    timeout=timeout or self._timeout,
                )
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    f"连不上 {self.provider.get('baseUrl')}：{exc}",
                    hint="检查 provider 的 baseUrl / apiPrefix 与网络",
                ) from exc
            try:
                body = response.json()
            except ValueError:
                body = None
            if response.status_code == 401 and attempt == 1:
                last_body = body
                continue
            if response.status_code >= 400:
                _unwrap_error(response.status_code, body, url)
            return _unwrap(body)
        message = last_body.get("message") if isinstance(last_body, dict) else None
        raise NotLoggedIn(
            f"provider `{self.provider_name}` 认证失败：{message or '令牌无效'}",
            hint=f"`qi kb provider login {self.provider_name}`",
        )

    # ── 读 ────────────────────────────────────────────────

    def list_available(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/rag/kb/available")
        if isinstance(data, dict):
            items = data.get("knowledge_bases") or data.get("items") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return [item for item in items if isinstance(item, dict)]

    def documents(self, kb_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/rag/kb/{kb_id}/documents")
        if isinstance(data, dict):
            items = data.get("documents") or data.get("items") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return [item for item in items if isinstance(item, dict)]

    # ── 检索 / 问答 ───────────────────────────────────────

    def search(self, query: str, kb_ids: list[str], *,
               top_k: int = 5, strategy: str = "hybrid",
               hybrid_alpha: float = 0.7, use_rerank: bool = True,
               rerank_top_k: int = 3, rerank_score_threshold: float = 0.0,
               rerank_method: str = "user_config",
               kb_hit_threshold: float | None = None) -> dict[str, Any]:
        payload = _qa_payload(
            "question", str(query), kb_ids, top_k=top_k, strategy=strategy,
            hybrid_alpha=hybrid_alpha, use_rerank=use_rerank,
            rerank_top_k=rerank_top_k,
            rerank_score_threshold=rerank_score_threshold,
            rerank_method=rerank_method, kb_hit_threshold=kb_hit_threshold,
        )
        data = self._request("POST", "/rag/qa/retrieval/search", payload=payload,
                             timeout=SEARCH_TIMEOUT)
        if not isinstance(data, dict):
            return {"results": [], "total": 0}
        return data

    def ask(self, question: str, kb_ids: list[str], *,
            top_k: int = 5, strategy: str = "hybrid",
            hybrid_alpha: float = 0.7, use_rerank: bool = True,
            rerank_top_k: int = 3, rerank_score_threshold: float = 0.0,
            rerank_method: str = "user_config",
            kb_hit_threshold: float | None = None,
            qa_session_id: str | None = None,
            enable_query_rewrite: bool = False) -> dict[str, Any]:
        payload = _qa_payload(
            "question", str(question), kb_ids, top_k=top_k, strategy=strategy,
            hybrid_alpha=hybrid_alpha, use_rerank=use_rerank,
            rerank_top_k=rerank_top_k,
            rerank_score_threshold=rerank_score_threshold,
            rerank_method=rerank_method, kb_hit_threshold=kb_hit_threshold,
        )
        payload["qa_session_id"] = qa_session_id
        payload["enable_query_rewrite"] = bool(enable_query_rewrite)
        data = self._request("POST", "/rag/qa/ask", payload=payload,
                             timeout=ASK_TIMEOUT)
        return data if isinstance(data, dict) else {}


    def ask_stream(self, question: str, kb_ids: list[str], *,
                   top_k: int = 5, strategy: str = "hybrid",
                   hybrid_alpha: float = 0.7, use_rerank: bool = True,
                   rerank_top_k: int = 3, rerank_score_threshold: float = 0.0,
                   rerank_method: str = "user_config",
                   kb_hit_threshold: float | None = None,
                   qa_session_id: str | None = None,
                   enable_query_rewrite: bool = False):
        """流式问答：逐条 yield `{type: token|sources|done}`。

        这是一个同步生成器（宿主在 thread 里迭代）；路径与 `ask` 同一个白名单。
        """
        payload = _qa_payload(
            "question", str(question), kb_ids, top_k=top_k, strategy=strategy,
            hybrid_alpha=hybrid_alpha, use_rerank=use_rerank,
            rerank_top_k=rerank_top_k,
            rerank_score_threshold=rerank_score_threshold,
            rerank_method=rerank_method, kb_hit_threshold=kb_hit_threshold,
        )
        payload["qa_session_id"] = qa_session_id
        payload["enable_query_rewrite"] = bool(enable_query_rewrite)
        path = "/rag/qa/ask/stream"
        if not _is_allowed("POST", path):
            raise UpstreamError(f"内部错误：POST {path} 不在白名单里")
        url = auth.api_url(self.provider, path)
        last_body: Any = None
        for attempt in (1, 2):
            info = auth.ensure_access(
                self.cfg, self.provider_name, auth_store=self._auth_store,
                http=self._http, force_refresh=(attempt == 2), clock=self._clock,
            )
            headers = {"Authorization": f"Bearer {info.access}",
                       "Accept": "text/event-stream"}
            try:
                with self._http.stream(
                    "POST", url, json=payload, headers=headers,
                    timeout=ASK_TIMEOUT,
                ) as response:
                    if response.status_code == 401 and attempt == 1:
                        response.read()
                        try:
                            last_body = response.json()
                        except ValueError:
                            last_body = None
                        continue
                    if response.status_code >= 400:
                        response.read()
                        try:
                            body = response.json()
                        except ValueError:
                            body = None
                        _unwrap_error(response.status_code, body, url)
                    for line in response.iter_lines():
                        if not line or line.startswith(":"):
                            continue
                        raw = line[5:].strip() if line.startswith("data:") else line.strip()
                        if not raw or raw == "[DONE]":
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            yield event
                    return
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    f"连不上 {self.provider.get('baseUrl')}：{exc}",
                    hint="检查 provider 的 baseUrl / apiPrefix 与网络",
                ) from exc
        message = last_body.get("message") if isinstance(last_body, dict) else None
        raise NotLoggedIn(
            f"provider `{self.provider_name}` 认证失败：{message or '令牌无效'}",
            hint=f"`qi kb provider login {self.provider_name}`",
        )


def _qa_payload(field: str, value: str, kb_ids: list[str], *,
                top_k: int, strategy: str, hybrid_alpha: float,
                use_rerank: bool, rerank_top_k: int,
                rerank_score_threshold: float, rerank_method: str,
                kb_hit_threshold: float | None) -> dict[str, Any]:
    ids = [str(k) for k in kb_ids if str(k).strip()]
    if not ids:
        raise UpstreamError("kb_ids 不能为空")
    top = max(1, min(int(top_k), 50))
    rerank_top = max(1, min(int(rerank_top_k), 20, top))
    strat = strategy if strategy in ("dense", "sparse", "hybrid") else "hybrid"
    payload: dict[str, Any] = {
        field: value,
        "kb_ids": ids,
        "top_k": top,
        "retrieval_strategy": strat,
        "hybrid_alpha": max(0.0, min(float(hybrid_alpha), 1.0)),
        "use_rerank": bool(use_rerank),
        "rerank_method": rerank_method or "user_config",
        "rerank_top_k": rerank_top,
        "rerank_score_threshold": max(0.0, min(float(rerank_score_threshold), 1.0)),
    }
    if kb_hit_threshold is not None:
        payload["kb_hit_threshold"] = max(0.0, min(float(kb_hit_threshold), 1.0))
    return payload
