"""两个工具：召回不带链接；推荐只认目录里的 id，链接由目录回填。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qi_agent.extensions import ToolError
from qi_assistants import catalog, tools


@pytest.fixture
def ctx(tmp_path):
    catalog.import_seed(tmp_path)
    return SimpleNamespace(cwd=tmp_path)


async def test_search_returns_candidates_without_url(ctx):
    payload = json.loads(await tools.search({"query": "先出评估方案"}, ctx))
    assert payload["matched"] is True
    assert payload["total"] == 3
    assert payload["candidates"]
    first = payload["candidates"][0]
    assert first["name"] == "评估方案生成助手"
    assert "url" not in first
    assert "https://" not in json.dumps(payload, ensure_ascii=False)


async def test_search_empty_query_lists_all(ctx):
    payload = json.loads(await tools.search({"query": ""}, ctx))
    assert payload["mode"] == "list"
    assert len(payload["candidates"]) == 3


async def test_search_no_match_reports_names(ctx):
    payload = json.loads(await tools.search({"query": "天气"}, ctx))
    assert payload["matched"] is False
    assert payload["candidates"] == []
    assert len(payload["names"]) == 3


async def test_recommend_fills_url_and_ui_cards(ctx):
    plan = next(a for a in catalog.load(ctx.cwd).assistants
                if a.name == "评估方案生成助手")
    outcome = await tools.recommend(
        {"ids": [plan.id], "reasons": {plan.id: "任务需要先产出评估方案"}}, ctx)
    assert plan.url in outcome.result
    assert outcome.details is not None
    (node,) = outcome.details["ui"]
    assert node["type"] == "assistant_cards"
    item = node["items"][0]
    assert item["id"] == plan.id
    assert item["url"] == plan.url
    assert item["reason"] == "任务需要先产出评估方案"


async def test_recommend_rejects_unknown_id_and_lists_valid_ones(ctx):
    with pytest.raises(ToolError) as exc:
        await tools.recommend({"ids": ["as_deadbeef"]}, ctx)
    assert "不存在" in str(exc.value)
    assert "评估方案生成助手" in str(exc.value)


async def test_recommend_rejects_more_than_three(ctx):
    ids = [a.id for a in catalog.load(ctx.cwd).assistants] + ["as_00000000"]
    with pytest.raises(ToolError):
        await tools.recommend({"ids": ids}, ctx)


async def test_recommend_rejects_empty_ids(ctx):
    with pytest.raises(ToolError):
        await tools.recommend({"ids": []}, ctx)


async def test_reason_falls_back_to_description_first_sentence(ctx):
    plan = next(a for a in catalog.load(ctx.cwd).assistants
                if a.name == "评估方案生成助手")
    outcome = await tools.recommend({"ids": [plan.id]}, ctx)
    assert outcome.details is not None
    reason = outcome.details["ui"][0]["items"][0]["reason"]
    assert reason
    assert reason in plan.description


async def test_disabled_cannot_be_recommended(ctx):
    data = catalog.create(ctx.cwd, {
        "name": "停用助手",
        "description": "不在推荐里",
        "tasks": ["t"],
        "outputs": ["o"],
        "url": "https://example.com/disabled",
        "enabled": False,
    })
    disabled = data.assistants[-1]
    with pytest.raises(ToolError):
        await tools.recommend({"ids": [disabled.id]}, ctx)
    payload = json.loads(await tools.search({"query": "停用"}, ctx))
    assert disabled.id not in [c["id"] for c in payload["candidates"]]


async def test_recommend_dedupes_ids(ctx):
    plan = next(a for a in catalog.load(ctx.cwd).assistants
                if a.name == "评估方案生成助手")
    outcome = await tools.recommend({"ids": [plan.id, plan.id]}, ctx)
    assert outcome.details is not None
    assert len(outcome.details["ui"][0]["items"]) == 1
