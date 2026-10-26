"""召回：把任务描述与目录条目对上号。

**这一步只负责“别漏”**，不负责最终选择 —— 选谁由模型在候选里判断。
所以打分不需要很聪明，但必须确定、可解释、无依赖：

* 中文按**相邻二字**（bigram）切：`评估方案` → 评估 / 估方 / 方案；
* 英文/数字按词切；
* 名称权重最高，其次是 keywords、典型任务、核心产出、描述；
* 空 query = 列出全部（`assistant_search` 的“有哪些助手”）。

为什么不用 embedding：条目是几十条，领域词靠人工 keywords 就能覆盖；
多一个索引就多一份失效面，而且“为什么召回这条”会变得不可解释。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .catalog import Assistant

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD = re.compile(r"[a-z0-9_+#.-]+")

#: 字段权重。名称是“它叫什么”，匹配上通常就是对的。
_WEIGHTS = {
    "name": 5.0,
    "keywords": 3.0,
    "tasks": 2.0,
    "outputs": 1.5,
    "description": 1.0,
}


def normalize(text: str) -> str:
    return " ".join((text or "").casefold().split())


def tokens(text: str) -> set[str]:
    """中文 bigram + 英文/数字词。单字成词时保留该字。"""
    text = normalize(text)
    out: set[str] = set()
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    out.update(_WORD.findall(text))
    return out


def _field_coverage(query: str, query_tokens: set[str], value: str) -> float:
    field = normalize(value)
    if not field:
        return 0.0
    field_tokens = tokens(field)
    if not field_tokens:
        return 0.0
    coverage = len(query_tokens & field_tokens) / len(query_tokens)
    # 很短的 query（1–3 字）在长字段里按整串找一次：bigram 对“评”这类单字无能为力。
    if len(query) <= 3 and query in field:
        coverage = max(coverage, 0.6)
    return coverage


@dataclass(frozen=True)
class Candidate:
    assistant: Assistant
    score: float
    hits: tuple[str, ...]


def score(assistant: Assistant, query: str) -> Candidate:
    query = normalize(query)
    if not query:
        return Candidate(assistant=assistant, score=0.0, hits=())
    query_tokens = tokens(query)
    if not query_tokens:
        return Candidate(assistant=assistant, score=0.0, hits=())

    values = {
        "name": assistant.name,
        "keywords": " ".join(assistant.keywords),
        "tasks": " ".join(assistant.tasks),
        "outputs": " ".join(assistant.outputs),
        "description": assistant.description,
    }
    total = 0.0
    hits: set[str] = set()
    for field, weight in _WEIGHTS.items():
        value = values[field]
        if not value:
            continue
        total += weight * _field_coverage(query, query_tokens, value)
        hits.update(query_tokens & tokens(value))

    # 整串命中名称是强信号（比如用户直接打了助手名）。
    if len(query) >= 2 and query in normalize(assistant.name):
        total += 2.0
    return Candidate(assistant=assistant, score=round(total, 3),
                     hits=tuple(sorted(hits)))


def rank(assistants: tuple[Assistant, ...] | list[Assistant], query: str,
         limit: int = 8) -> tuple[list[Candidate], bool]:
    """返回 `(候选, 是否有命中)`。空 query = 按文件顺序列全部（`matched=False`）。"""
    active = [a for a in assistants if a.enabled]
    query = normalize(query)
    if not query:
        return ([Candidate(assistant=a, score=0.0, hits=()) for a in active[:limit]],
                False)
    scored = [c for c in (score(a, query) for a in active) if c.score > 0]
    scored.sort(key=lambda c: (-c.score, len(c.assistant.name), c.assistant.name))
    return scored[:limit], bool(scored)
