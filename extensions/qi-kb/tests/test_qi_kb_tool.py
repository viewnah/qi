from __future__ import annotations

import json
from pathlib import Path

import pytest

from qi_kb import config
from qi_kb import format as fmt
from qi_kb import service
from qi_kb.tools import _execute


def write_kb(home: Path) -> None:
    (home / "kb.json").write_text(json.dumps({
        "providers": {
            "alice": {
                "baseUrl": "http://hik:7091",
                "knowledgeBases": {
                    "kb1": {"id": "kb1", "name": "测试库",
                            "description": "一个测试知识库",
                            "docCount": 1, "chunkCount": 2},
                },
            }
        }
    }, ensure_ascii=False), encoding="utf-8")


def test_prompt_section_only_when_bound(kb_home):
    write_kb(kb_home)
    section = service.prompt_section(kb_home)
    assert "测试库@alice" in section
    assert "kb_id=kb1" in section


def test_format_search_truncates_content():
    data = {
        "query": "问", "provider": "alice", "kbIds": ["kb1"],
        "strategy": "hybrid", "rerankMethod": None,
        "results": [{"content": "x" * (fmt.MAX_CHUNK_CHARS + 50), "score": 0.5,
                     "kb_name": "测试库", "doc_name": "a.pdf",
                     "metadata": {"chunk_index": 3}}],
    }
    text = fmt.format_search(data)
    assert "本段截断" in text
    assert "KB_CONTENT" in text


def test_format_ask_sources():
    text = fmt.format_ask({
        "provider": "alice", "answer": "答案", "kbIdsUsed": ["kb1"],
        "sources": [{"doc_name": "a.pdf", "kb_name": "测试库", "score": 0.1}],
    })
    assert "答案" in text
    assert "alice / 测试库 / a.pdf" in text


class _Ctx:
    def __init__(self, cwd):
        self.workdir = cwd
        self.session_env = {"QI_SESSION_ID": "s1"}


@pytest.mark.asyncio
async def test_tool_search(kb_home, monkeypatch):
    write_kb(kb_home)

    def fake_search(cwd, query, **kwargs):
        return {"query": query, "provider": "alice", "kbIds": ["kb1"],
                "strategy": "hybrid", "rerankMethod": None,
                "results": [{"content": "片段", "score": 0.5, "kb_name": "测试库",
                             "doc_name": "a.pdf", "metadata": {}}]}

    monkeypatch.setattr(service, "search", fake_search)
    out = await _execute({"action": "search", "query": "问"}, _Ctx(kb_home))
    text = getattr(out, "result", str(out))
    assert "片段" in text


@pytest.mark.asyncio
async def test_tool_unknown_action():
    out = await _execute({"action": "nope"}, _Ctx(None))
    text = getattr(out, "result", str(out))
    assert "不认识的 action" in text


def test_session_scope_prompt_and_selectors(kb_home):
    write_kb(kb_home)
    # 同一个 provider 下再加一个库
    cfg = config.load()
    config.save_kb("global", "alice", "kb2", {"id": "kb2", "name": "另一个库"})
    service.set_session_scope("s1", ["kb2"], None)
    try:
        text = service.prompt_section(kb_home, session_id="s1")
        assert "另一个库" in text
        assert "测试库" not in text
        selectors, provider = service._scope_selectors("s1", [], None)
        assert selectors == ["kb2"] and provider is None
    finally:
        service.clear_session_scope("s1")
