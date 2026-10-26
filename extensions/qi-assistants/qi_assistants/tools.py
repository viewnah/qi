"""给模型的两个工具。

分工（刻意不把选择塞进工具里）：

* `assistant_search`：**召回**。返回候选（含 id / 名称 / 描述 / 任务 / 产出），
  **不含 URL**；
* `assistant_recommend`：**提交选择**。校验 id 是否存在、把目录里的 URL 与名称回填，
  同时产出卡片数据给前端。

选谁、为什么选，由主模型判断 —— 工具里再套一次 LLM 只会多一份提示词和一次延迟。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qi_agent.extensions import Tool, ToolError
from qi_agent.models import ToolOutcome

from . import catalog, matching

#: 候选里描述的截断长度：8 条 × 400 字会把工具结果撑得比目录还长。
_DESCRIPTION_LIMIT = 160
#: 推荐理由的上限。
_REASON_LIMIT = 80


def _cwd(ctx: Any) -> Path:
    cwd = getattr(ctx, "cwd", None) or getattr(ctx, "workdir", None)
    if cwd is None:
        raise ToolError("拿不到当前工作目录，读不了 .qi/assistants.json")
    return Path(cwd)


def _limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 8
    return max(1, min(8, limit))


def _candidate(candidate: matching.Candidate) -> dict:
    assistant = candidate.assistant
    return {
        "id": assistant.id,
        "name": assistant.name,
        "description": assistant.description[:_DESCRIPTION_LIMIT],
        "tasks": list(assistant.tasks),
        "outputs": list(assistant.outputs),
        "score": candidate.score,
    }


async def search(args: dict, ctx: Any) -> str:
    """召回候选。空 query = 列出全部已发布助手。"""
    query = str(args.get("query") or "").strip()
    data = catalog.load(_cwd(ctx))
    active = data.active()
    candidates, matched = matching.rank(active, query, _limit(args.get("limit")))
    payload: dict[str, Any] = {
        "mode": "search" if query else "list",
        "query": query,
        "matched": matched or (not query and bool(candidates)),
        "total": len(active),
        "candidates": [_candidate(c) for c in candidates],
    }
    if query and not candidates:
        # 一条都没召回时给名字清单：模型可以照实说“目录里有这些”，而不是硬推。
        payload["names"] = [a.name for a in active[:20]]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _reason(value: Any, assistant: catalog.Assistant) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        # 模型没给理由时用描述首句兜底：卡片上不能空着一行。
        for sep in ("。", "！", "？", ".", "!", "?"):
            cut = assistant.description.find(sep)
            if cut > 0:
                text = assistant.description[:cut]
                break
        else:
            text = assistant.description
    return text[:_REASON_LIMIT]


def _render(items: list[dict]) -> str:
    lines = [f"已推荐 {len(items)} 个应用助手："]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {item['name']}（{item['id']}）")
        lines.append(f"   理由：{item['reason']}")
        lines.append(f"   典型任务：{'、'.join(item['tasks'])}")
        lines.append(f"   核心产出：{'、'.join(item['outputs'])}")
        lines.append(f"   链接：{item['url']}")
    return "\n".join(lines)


async def recommend(args: dict, ctx: Any) -> ToolOutcome:
    """校验选中的 id，回填目录里的链接，产出卡片。"""
    raw_ids = args.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ToolError("ids 必须是非空数组（1–3 个助手 id）")
    ids: list[str] = []
    for value in raw_ids:
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)
    if not ids:
        raise ToolError("ids 里没有有效的助手 id")
    if len(ids) > 3:
        raise ToolError("一次最多推荐 3 个助手")

    data = catalog.load(_cwd(ctx))
    by_id = {a.id: a for a in data.active()}
    unknown = [value for value in ids if value not in by_id]
    if unknown:
        available = "、".join(f"{a.name}({a.id})" for a in by_id.values()) or "（目录为空）"
        raise ToolError(
            f"不存在的助手 id：{', '.join(unknown)}。"
            f"只能从 assistant_search 的结果里选。合法 id：{available}"
        )

    raw_reasons = args.get("reasons")
    reasons = raw_reasons if isinstance(raw_reasons, dict) else {}
    items: list[dict] = []
    for assistant_id in ids:
        assistant = by_id[assistant_id]
        items.append({
            "id": assistant.id,
            "name": assistant.name,
            "description": assistant.description,
            "tasks": list(assistant.tasks),
            "outputs": list(assistant.outputs),
            # URL 只从目录取 —— 模型没有这个字段，也就编不出来。
            "url": assistant.url,
            "reason": _reason(reasons.get(assistant_id), assistant),
        })

    return ToolOutcome(
        result=_render(items),
        details={
            "ui_version": 1,
            "ui": [{"type": "assistant_cards", "items": items}],
        },
    )


def build_search_tool() -> Tool:
    return Tool(
        name="assistant_search",
        label="查找应用助手",
        description=(
            "从项目级应用助手目录（.qi/assistants.json）里按任务描述召回候选。"
            "返回 id、名称、描述、典型任务、核心产出；**不返回链接**。"
            "空 query 表示列出全部已发布助手。推荐前必须先调用它，不要凭记忆推荐。"
        ),
        prompt_snippet="按任务描述召回应用助手候选（不含链接）",
        prompt_guidelines=[
            "assistant_search 只负责召回；选谁由你判断，选完必须用 assistant_recommend 提交 id。",
            "assistant_search 的结果里没有 url，不要在回答里自己拼链接。",
        ],
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "任务描述或关键词；空字符串 = 列出全部已发布助手",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回几条，1–8，默认 8",
                    "minimum": 1,
                    "maximum": 8,
                },
            },
            "required": ["query"],
        },
        execute=search,
    )


def build_recommend_tool() -> Tool:
    return Tool(
        name="assistant_recommend",
        label="推荐应用助手",
        description=(
            "提交最终选中的应用助手 id（1–3 个），返回带链接的卡片数据。"
            "id 必须来自 assistant_search 的结果；不存在的 id 会被拒绝。"
            "每个 id 给一句推荐理由，理由要引用该助手的典型任务或核心产出。"
        ),
        prompt_snippet="提交选中的应用助手 id，生成可点击的推荐卡片",
        prompt_guidelines=[
            "assistant_recommend 的链接由目录回填；不要自己生成或改写 URL。",
            "一次最多 3 个助手；没有匹配时不要调用它，直接告诉用户暂无匹配。",
        ],
        parameters={
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 3,
                    "description": "assistant_search 结果里的助手 id",
                },
                "reasons": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "id → 一句推荐理由（≤80 字）",
                },
            },
            "required": ["ids"],
        },
        execute=recommend,
    )
