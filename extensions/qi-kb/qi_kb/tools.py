"""模型可调用的 `kb` 代理工具。"""

from __future__ import annotations

import asyncio
from typing import Any

from qi_agent.extensions import Tool, ToolOutcome

from . import format as fmt, service
from .errors import KbError, UsageError

DESCRIPTION = (
    "访问 hikqin 知识库。action=list 列 provider / 可用 / 已绑定；"
    "search 只检索 chunk；ask 让上游 RAG 模型作答；docs 列文档。"
    "知识库选择器可写成 名称@provider 或 kbId@provider。"
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["list", "search", "ask", "docs"]},
        "query": {"type": "string", "description": "search 的检索词"},
        "question": {"type": "string", "description": "ask 的问题"},
        "kb": {"type": "string",
               "description": "知识库 id / 名称 / 名称子串，可加 @provider"},
        "kb_ids": {"type": "array", "items": {"type": "string"},
                   "description": "要查的 kbId 列表；省略 = 所有已绑定"},
        "provider": {"type": "string", "description": "只查某个 provider"},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
        "strategy": {"type": "string", "enum": ["dense", "sparse", "hybrid"]},
        "use_rerank": {"type": "boolean"},
        "rerank_top_k": {"type": "integer", "minimum": 1, "maximum": 20},
        "bound_only": {"type": "boolean", "description": "list 时只看已绑定"}
    },
    "required": ["action"],
}


async def _execute(args: dict[str, Any], ctx: Any) -> ToolOutcome:
    cwd = getattr(ctx, "workdir", None) or getattr(ctx, "cwd", None)
    env = getattr(ctx, "session_env", None)
    session_id = env.get("QI_SESSION_ID") if isinstance(env, dict) else None
    action = str(args.get("action") or "").strip()
    try:
        if action == "list":
            data = await asyncio.to_thread(
                service.available, cwd,
                provider=args.get("provider"),
                q=args.get("query") or args.get("q"),
                bound_only=bool(args.get("bound_only")),
            )
            text = fmt.format_list(data.get("items") or [],
                                   bound_only=bool(args.get("bound_only")),
                                   warnings=data.get("warnings") or [])
            return ToolOutcome(result=text, details={"items": data.get("items") or []})

        if action == "search":
            query = str(args.get("query") or "").strip()
            if not query:
                raise UsageError("search 需要 query")
            data = await asyncio.to_thread(
                service.search, cwd, query,
                kb_ids=_selectors(args),
                provider=args.get("provider"),
                top_k=args.get("top_k"),
                strategy=args.get("strategy"),
                use_rerank=args.get("use_rerank"),
                rerank_top_k=args.get("rerank_top_k"),
                session_id=session_id,
            )
            return ToolOutcome(
                result=fmt.format_search(data),
                details={"results": [_slim(item) for item in (data.get("results") or [])],
                         "kbIds": data.get("kbIds") or []},
            )

        if action == "ask":
            question = str(args.get("question") or args.get("query") or "").strip()
            if not question:
                raise UsageError("ask 需要 question")
            data = await asyncio.to_thread(
                service.ask, cwd, question,
                kb_ids=_selectors(args),
                provider=args.get("provider"),
                top_k=args.get("top_k"),
                strategy=args.get("strategy"),
                use_rerank=args.get("use_rerank"),
                rerank_top_k=args.get("rerank_top_k"),
                session_id=session_id,
            )
            return ToolOutcome(
                result=fmt.format_ask(data),
                details={"answer": data.get("answer") or "",
                         "sources": [_slim(item) for item in (data.get("sources") or [])]},
            )

        if action == "docs":
            kb = str(args.get("kb") or (args.get("kb_ids") or [""])[0] or "").strip()
            if not kb:
                raise UsageError("docs 需要 kb")
            data = await asyncio.to_thread(
                service.documents, cwd, kb, provider=args.get("provider"))
            lines = [f"文档（{data.get('provider')} / {data.get('name')} / {data.get('kbId')}）"]
            for doc in data.get("documents") or []:
                lines.append(
                    f"- {doc.get('filename') or doc.get('doc_id')}  "
                    f"chunks={doc.get('chunk_count') or doc.get('chunkCount') or 0}  "
                    f"{doc.get('status') or '-'}"
                )
            if len(lines) == 1:
                lines.append("  (没有文档)")
            return ToolOutcome(result="\n".join(lines),
                               details={"documents": data.get("documents") or []})

        raise UsageError(f"不认识的 action：{action!r}", hint="可用：list / search / ask / docs")
    except KbError as exc:
        return f"知识库调用失败：{fmt.error_text(exc)}"
    except Exception as exc:                     # noqa: BLE001 不把一次调用打成回合失败
        return f"知识库调用失败：{type(exc).__name__}: {exc}"


def _slim(item: Any) -> Any:
    """落盘 details 不需要正文：正文已在 result 里，source 链接信息要留下。"""
    if not isinstance(item, dict):
        return item
    return {k: v for k, v in item.items() if k != "content"}


def _selectors(args: dict[str, Any]) -> list[str]:
    values = args.get("kb_ids")
    if isinstance(values, list):
        return [str(v) for v in values if str(v).strip()]
    if isinstance(values, str) and values.strip():
        return [values.strip()]
    kb = str(args.get("kb") or "").strip()
    return [kb] if kb else []


def build_tool() -> Tool:
    return Tool(
        name="kb",
        description=DESCRIPTION,
        parameters=PARAMETERS,
        execute=_execute,
        label="知识库",
        prompt_snippet="检索 / 问答 hikqin 知识库",
        prompt_guidelines=[
            "需要 hikqin 知识库中的事实、文档内容或内部资料时用 kb 工具；"
            "先用 action=search 检索，需要综合答案时再用 action=ask。",
            "kb 的 kb / kb_ids 参数可以写成 名称@provider 精确指定账号；"
            "不要编造知识库内容，证据不足要明确说明。",
            "kb 工具返回的答案里如果有 [来源N] 标记，最终回答引用知识库内容时必须"
            "原样保留这些编号，不要改写或删除；编号与来源列表一一对应。",
            "不要在最终回答里另写 `来源：[1][2]` 或自行重编号；只引用工具结果里"
            "实际存在的 [来源N]。",
        ],
    )
