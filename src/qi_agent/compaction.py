"""上下文压缩与分支摘要(移植 pi 的 `core/compaction`)。

两种机制共用同一套结构化摘要格式与同一条 LLM 调用路径:

  · **压缩(compaction)**:上下文超过「contextWindow - reserveTokens」或用户 `/compact` 时,
    把旧消息压成一段结构化摘要,保留最近约 `keepRecentTokens` 的内容;摘要落成
    `type=compaction` entry,带 `firstKeptEntryId`(从哪条 entry 开始保留)。
  · **分支摘要(branch summary)**:`/tree` 跳到别的分支时,把「被放弃的那段」压成摘要挂到
    新位置(`type=branch_summary`),切回来继续时上下文不丢。

与 pi 一致的两个细节:
  · 切点只落在「turn 边界」或 assistant 消息上,**绝不在 tool 结果上**(它必须跟着它的调用);
  · 单个 turn 就超过预算时做 **split turn**:前缀单独摘要,与历史摘要拼在一起。

qi 的差异:`custom`(助手叙述)在 qi 里**不进上下文**(见 runtime._history),所以压缩也当它
不可见 —— 与上下文语义保持一致,不额外发明规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .llm import ChatMessage, LLMClient, usage_to_dict

DEFAULT_RESERVE_TOKENS = 16384
"""给模型回答留的余量(pi 默认)。"""

DEFAULT_KEEP_RECENT_TOKENS = 20000
"""压缩后至少保留的近期内容(pi 默认)。"""

SUMMARIZATION_SYSTEM_PROMPT = """You are a context summarization assistant. Your task is to read a conversation between a user and an AI assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary."""

SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

UPDATE_SUMMARIZATION_INSTRUCTIONS = """Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new if discovered]

## Progress
### Done
- [x] [Include previously done items and newly completed items]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Updated list of what should happen next]

## Critical Context
- [Preserve existing and add new important details]"""

UPDATE_SUMMARIZATION_PROMPT = f"""The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

{UPDATE_SUMMARIZATION_INSTRUCTIONS}"""

TURN_PREFIX_SUMMARIZATION_PROMPT = """This is the PREFIX of a turn that was too large to keep. The SUFFIX (recent work) is retained.

Summarize the prefix to provide context for the retained suffix:

## Original Request
[What did the user ask for in this turn?]

## Early Progress
- [Key decisions and work done in the prefix]

## Context for Suffix
- [Information needed to understand the retained recent work]

Be concise. Focus on what's needed to understand the kept suffix."""

BRANCH_SUMMARY_PROMPT = """Create a structured summary of this conversation branch for context when returning later.

Use this EXACT format:

## Goal
[What was the user trying to accomplish in this branch?]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Work that was started but not finished]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [What should happen next to continue this work]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

BRANCH_SUMMARY_PREAMBLE = """The user explored a different conversation branch before returning here.
Summary of that exploration:

"""


# ── 估算(与 pi 同口径:chars/4)──────────────────────────────


def estimate_tokens(text: str) -> int:
    """`chars/4` 保守估计(pi `estimateTokens`)。"""
    return -(-len(text) // 4)


def entry_tokens(entry: dict) -> int:
    """单条 entry 的**上下文** token 估算。

    注意 qi 的 tool 结果是 `type=tool` entry,而 `_history` 只把 `message` 送进上下文
    (跨轮不带工具结果 —— 那是 qi 现有设计,不是这里改的),所以工具算 0。
    摘要输入会带上工具结果(见 `is_summarizable_entry`),因为那是「发生过什么」的事实。
    """
    kind = entry.get("type")
    if kind == "message":
        role = entry.get("role")
        if role == "tool":
            return estimate_tokens(str(entry.get("result") or ""))
        return estimate_tokens(str(entry.get("content") or ""))
    if kind in ("compaction", "branch_summary"):
        return estimate_tokens(str(entry.get("summary") or ""))
    return 0        # tool / dispatch / state / custom(叙述):不进上下文


def is_summarizable_entry(entry: dict) -> bool:
    """会不会被送进**摘要模型**的输入(比上下文多一类:工具结果)。"""
    return entry.get("type") == "tool" or is_context_entry(entry)


def context_tokens(entries: list[dict]) -> int:
    """一段 entry 序列的上下文体积(按 entry 估算)。"""
    return sum(entry_tokens(e) for e in entries)


def messages_tokens(messages: list) -> int:
    """已经组好的上下文消息体积 —— 触发压缩时要用**这个**,而不是原始 entry:
    压缩过的内容不该再计入(否则会每轮都重复触发)。"""
    return sum(estimate_tokens(str(getattr(m, "content", "") or "")) for m in messages)


def should_compact(tokens: int, context_window: int, *, enabled: bool = True,
                   reserve_tokens: int = DEFAULT_RESERVE_TOKENS) -> bool:
    """超了「窗口 - 余量」才压缩(pi `shouldCompact`)。"""
    if not enabled or context_window <= 0:
        return False
    return tokens > context_window - reserve_tokens


# ── 切点(pi `findCutPoint`)────────────────────────────────


def is_context_entry(entry: dict) -> bool:
    """这条 entry 会不会变成**上下文消息**(qi:message / compaction / branch_summary)。"""
    kind = entry.get("type")
    if kind == "message":
        return True
    return kind in ("compaction", "branch_summary")


def is_cut_point(entry: dict) -> bool:
    """可作为「开始保留」的位置:user/assistant 消息,compaction/branch_summary。

    绝不在 tool 结果上切 —— 它必须跟在它的调用后面。
    """
    kind = entry.get("type")
    if kind in ("compaction", "branch_summary"):
        return True
    if kind != "message":
        return False
    return entry.get("role") in ("user", "assistant")


def is_turn_start(entry: dict) -> bool:
    """turn 起点:user 消息 / branch_summary / compaction(assistant 与 tool 不是)。"""
    kind = entry.get("type")
    if kind in ("compaction",):
        return False
    if kind == "branch_summary":
        return True
    return kind == "message" and entry.get("role") == "user"


@dataclass
class Preparation:
    """压缩前的计划(pi `prepareCompaction` 的返回)。"""

    first_kept_entry_id: str
    messages_to_summarize: list[dict] = field(default_factory=list)
    turn_prefix_messages: list[dict] = field(default_factory=list)
    is_split_turn: bool = False
    tokens_before: int = 0
    previous_summary: str | None = None

    @property
    def empty(self) -> bool:
        return not self.messages_to_summarize and not self.turn_prefix_messages


def find_cut_point(entries: list[dict], start: int, end: int,
                   keep_recent_tokens: int) -> tuple[int, int, bool]:
    """返回 `(first_kept_index, turn_start_index, is_split_turn)`。

    算法(pi 同款):从最新往回累加,累计到 `keepRecentTokens` 时,把切点放到**不早于**
    当前位置的最近一个合法切点;再向前吞掉相邻的不可见 entry(dispatch/state/叙述)。
    """
    cut_points = [i for i in range(start, end) if is_cut_point(entries[i])]
    if not cut_points:
        return start, -1, False

    accumulated = 0
    cut_index = cut_points[0]
    for i in range(end - 1, start - 1, -1):
        tokens = entry_tokens(entries[i])
        if tokens == 0:
            continue
        accumulated += tokens
        if accumulated >= keep_recent_tokens:
            cut_index = next((c for c in cut_points if c >= i), cut_points[-1])
            break

    # 向前吞掉不影响上下文的相邻 entry(dispatch/state/叙述),让切点更贴近预算。
    # 注意:工具结果在 qi 里虽然不进上下文,但**不是元数据** —— 不能被吞掉,
    # 否则切点会落在 tool 结果上(它必须跟在它的调用后面)。
    while cut_index > start:
        prev = entries[cut_index - 1]
        if prev.get("type") == "compaction" or is_summarizable_entry(prev):
            break
        cut_index -= 1

    if is_turn_start(entries[cut_index]):
        return cut_index, -1, False
    turn_start = next((i for i in range(cut_index, start - 1, -1)
                       if is_turn_start(entries[i])), -1)
    return cut_index, turn_start, turn_start != -1


def prepare_compaction(branch: list[dict], *, keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS
                       ) -> Preparation | None:
    """算出「要摘要哪些、从哪开始保留」;没什么可压时返回 None。"""
    if not branch:
        return None
    if branch[-1].get("type") == "compaction":
        return None                      # 刚压过,且之后没有新内容

    previous_summary: str | None = None
    boundary_start = 0
    prev_index = -1
    for i in range(len(branch) - 1, -1, -1):
        if branch[i].get("type") == "compaction":
            prev_index = i
            break
    if prev_index >= 0:
        previous_summary = str(branch[prev_index].get("summary") or "")
        first_kept = branch[prev_index].get("firstKeptEntryId")
        found = next((i for i, e in enumerate(branch) if str(e.get("id")) == str(first_kept)), -1)
        boundary_start = found if found >= 0 else prev_index + 1

    cut_index, turn_start, is_split = find_cut_point(
        branch, boundary_start, len(branch), keep_recent_tokens)
    first_kept_entry = branch[cut_index]
    if not first_kept_entry.get("id"):
        return None                      # 没迁移过的老 entry:不给 id 就没法引用

    history_end = turn_start if is_split else cut_index
    to_summarize = [e for e in branch[boundary_start:history_end] if is_summarizable_entry(e)]
    turn_prefix: list[dict] = []
    if is_split and turn_start >= 0:
        turn_prefix = [e for e in branch[turn_start:cut_index] if is_summarizable_entry(e)]

    prep = Preparation(
        first_kept_entry_id=str(first_kept_entry["id"]),
        messages_to_summarize=to_summarize,
        turn_prefix_messages=turn_prefix,
        is_split_turn=is_split,
        tokens_before=context_tokens(branch),
        previous_summary=previous_summary,
    )
    return None if prep.empty else prep


# ── 摘要生成(pi `generateSummary` / `compact`)───────────────


def serialize_conversation(entries: list[dict]) -> str:
    """把 entry 序列摊成纯文本给摘要模型(避免它以为要接着对话)。"""
    parts: list[str] = []
    for entry in entries:
        kind = entry.get("type")
        if kind == "message":
            role = entry.get("role")
            body = str(entry.get("result") if role == "tool" else entry.get("content") or "")
            if not body.strip():
                continue
            who = {"user": "User", "assistant": "Assistant", "tool": "Tool"}.get(str(role), str(role))
            parts.append(f"[{who}]: {body}")
        elif kind == "tool":
            body = str(entry.get("result") or "")
            if body.strip():
                label = str(entry.get("tool") or "tool")
                parts.append(f"[Tool {label}]: {body}")
        elif kind == "compaction":
            parts.append(f"[Previous summary]: {entry.get('summary')}")
        elif kind == "branch_summary":
            parts.append(f"[Branch summary]: {entry.get('summary')}")
    return "\n\n".join(parts)


def summary_context_message(summary: str, *, kind: str = "compaction") -> ChatMessage:
    """把摘要包成一条**上下文消息**(pi 把它放在 system 之后、kept 消息之前)。

    用 `user` 角色而不是 system:provider 对「多个 system」支持不一,而摘要本来就是
    「过去发生的对话」的替身。
    """
    label = "上下文已压缩" if kind == "compaction" else "另一条分支的摘要"
    return ChatMessage(role="user",
                       content=f"[{label}]\n{summary}\n[摘要结束,以下是其后的消息]")


async def _summarize(llm: LLMClient, entries: list[dict], *, previous_summary: str | None,
                     instructions: str | None, prompt: str) -> tuple[str, dict]:
    text = serialize_conversation(entries)
    body = f"<conversation>\n{text}\n</conversation>\n\n"
    if previous_summary:
        body += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
    base = prompt
    if instructions:
        base = f"{base}\n\nAdditional focus: {instructions}"
    body += base
    resp = await llm.chat([ChatMessage(role="system", content=SUMMARIZATION_SYSTEM_PROMPT),
                           ChatMessage(role="user", content=body)])
    if resp.tool_calls:
        raise RuntimeError("摘要模型试图调用工具")
    return (resp.text or "").strip(), resp.usage


async def compact(llm: LLMClient, prep: Preparation, *,
                  instructions: str | None = None) -> dict:
    """执行压缩,返回要落盘的 compaction entry(不含 id/parentId/ts)。

    split turn 时生成两段摘要并拼在一起(pi 同款)。
    """
    if prep.is_split_turn and prep.turn_prefix_messages:
        if prep.messages_to_summarize:
            history, usage = await _summarize(llm, prep.messages_to_summarize,
                                             previous_summary=prep.previous_summary,
                                             instructions=instructions,
                                             prompt=(UPDATE_SUMMARIZATION_PROMPT
                                                     if prep.previous_summary
                                                     else SUMMARIZATION_PROMPT))
        else:
            history, usage = "No prior history.", {}
        prefix, prefix_usage = await _summarize(llm, prep.turn_prefix_messages,
                                               previous_summary=None,
                                               instructions=None,
                                               prompt=TURN_PREFIX_SUMMARIZATION_PROMPT)
        summary = f"{history}\n\n---\n\n**Turn Context (split turn):**\n\n{prefix}"
        merged = dict(prefix_usage)
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = merged[key] + value
            else:
                merged[key] = value
        usage = merged
    else:
        summary, usage = await _summarize(llm, prep.messages_to_summarize,
                                         previous_summary=prep.previous_summary,
                                         instructions=instructions,
                                         prompt=(UPDATE_SUMMARIZATION_PROMPT
                                                 if prep.previous_summary
                                                 else SUMMARIZATION_PROMPT))
    return {"type": "compaction", "summary": summary,
            "firstKeptEntryId": prep.first_kept_entry_id,
            "tokensBefore": prep.tokens_before,
            "usage": usage_to_dict(usage)}


async def summarize_branch(llm: LLMClient, entries: list[dict]) -> tuple[str, dict]:
    """分支摘要:把「被放弃的那条分支」压成一段摘要。"""
    summary, usage = await _summarize(llm, entries, previous_summary=None, instructions=None,
                                      prompt=BRANCH_SUMMARY_PROMPT)
    return summary, usage_to_dict(usage)


def common_ancestor(branch_a: list[dict], branch_b: list[dict]) -> str | None:
    """两条分支（正序 entry 列表）最深的共同祖先 id。"""
    ids_a = [str(e.get("id")) for e in branch_a]
    ids_b = {str(e.get("id")) for e in branch_b}
    for entry_id in reversed(ids_a):
        if entry_id in ids_b:
            return entry_id
    return None


def branch_to_summarize(branch: list[dict], from_id: str | None,
                        target_branch: list[dict]) -> list[dict]:
    """要摘要的「被放弃段」:从 `from_id` 往回走到共同祖先(不含祖先)。"""
    ancestor = common_ancestor(branch, target_branch)
    out: list[dict] = []
    by_id = {str(e.get("id")): e for e in branch}
    node = by_id.get(str(from_id)) if from_id else None
    while node is not None:
        node_id = str(node.get("id"))
        if node_id == ancestor:
            break
        if is_context_entry(node):
            out.append(node)
        parent = node.get("parentId")
        node = by_id.get(str(parent)) if parent else None
    out.reverse()
    return out


def usage_int(usage: dict[str, Any] | None, key: str) -> int:
    """usage 字段容错读取(摘要调用也计入会话用量)。"""
    value = (usage or {}).get(key)
    return value if isinstance(value, int) else 0
