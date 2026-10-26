"""召回：宁可多召回，也不要漏；最终选择交给模型。"""

from __future__ import annotations

from qi_assistants.catalog import Assistant
from qi_assistants.matching import rank, score, tokens


def _assistant(**overrides) -> Assistant:
    values = {
        "id": "as_00000001",
        "name": "评估方案生成助手",
        "description": "意图分析、评估要素确认，输出可执行的评估方案。",
        "tasks": ("意图分析", "分部分生成"),
        "outputs": ("评估方案文档",),
        "url": "https://eval.example.com/plan",
        "keywords": ("方案", "指标"),
        "enabled": True,
    }
    values.update(overrides)
    return Assistant(**values)


def test_chinese_bigrams():
    assert {"评估", "估方", "方案"} <= tokens("评估方案")
    assert "evaluation" in tokens("Evaluation Plan")


def test_name_and_keywords_outrank_description():
    name_hit = _assistant(name="评估报告助手", description="无关")
    keyword_hit = _assistant(name="另一个助手", keywords=("报告",), description="无关")
    weak = _assistant(name="另一个助手", description="这里提到报告")
    q = "报告"
    assert score(name_hit, q).score > score(keyword_hit, q).score > score(weak, q).score


def test_plan_and_report_are_separated():
    plan = _assistant()
    report = _assistant(id="as_00000002", name="评估报告生成助手",
                        description="套模板生成评估报告。",
                        tasks=("模板选择", "逐部分生成"),
                        outputs=("评估报告",), keywords=("报告", "模板"))
    candidates, matched = rank((plan, report), "先出评估方案")
    assert matched and candidates[0].assistant.name == "评估方案生成助手"
    candidates, matched = rank((plan, report), "生成评估报告")
    assert matched and candidates[0].assistant.name == "评估报告生成助手"


def test_disabled_never_rank():
    disabled = _assistant(enabled=False)
    candidates, matched = rank((disabled,), "评估方案")
    assert candidates == [] and matched is False


def test_empty_query_lists_in_file_order():
    first = _assistant(name="A")
    second = _assistant(id="as_00000002", name="B")
    candidates, matched = rank((first, second), "", limit=1)
    assert matched is False
    assert [c.assistant.name for c in candidates] == ["A"]


def test_no_match_is_empty_not_arbitrary():
    candidates, matched = rank((_assistant(),), "天气怎么样")
    assert candidates == [] and matched is False


def test_limit_is_respected():
    assistants = tuple(_assistant(id=f"as_{i:08x}", name=f"助手{i}") for i in range(10))
    candidates, _ = rank(assistants, "助手", limit=3)
    assert len(candidates) == 3
