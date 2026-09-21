"""`thinkingBudgets`:按思考级别给 Anthropic 形态的模型带 token 预算(pi 同名段)。

pi 分三路处理它(`docs/settings.md`):Anthropic / Google / Bedrock **原生**用;OpenAI 兼容形态
**只在**模型配了 `compat.thinkingTokenBudgetField`(指定预算写进哪个请求字段)时才用;
并且**钳制到至少留 1024 token 给答案**。

qi 只做**原生那一路** —— litellm 在 Anthropic 形态上有对应参数(`thinking.budget_tokens`);
OpenAI 兼容那半不做,因为 qi 没有 compat 层(加那层是独立的一件事,见 `docs/cli.md` §10)。

两条刻意的选择:

* **没配就不带**:pi 另有内置默认表,qi 不抄 —— 那会让每个 Anthropic 请求凭空带上预算,
  默认行为就变了;
* **钳制**:`maxTokens` 不够时把预算压到"留 1024 给答案"(pi 的原话如此)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qi_agent import paths  # noqa: E402


def _env(tmp_path: Path, monkeypatch, *, api: str, max_tokens: int | None,
         extra: dict | None = None) -> None:
    model: dict = {"id": "m", "reasoning": True}
    if max_tokens:
        model["maxTokens"] = max_tokens
    (tmp_path / "models.json").write_text(json.dumps({"providers": {
        "anth": {"api": api, "models": [model]}}}), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({
        "defaultProvider": "anth", "defaultModel": "m",
        "defaultThinkingLevel": "high", **(extra or {})}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))


def _params(tmp_path, monkeypatch, **kw) -> dict:
    from qi_agent.runtime import QiRuntime

    _env(tmp_path, monkeypatch, **kw)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    rt = QiRuntime(cwd=project, approve_project=True)
    # 这就是拼请求参数的那个方法(`chat` / `astream` 共用,所以测它等于测真实请求)
    return rt.llm_exec._reasoning_params()


def test_budget_is_sent_for_anthropic(tmp_path, monkeypatch):
    params = _params(tmp_path, monkeypatch, api="anthropic-messages", max_tokens=8000,
                     extra={"thinkingBudgets": {"high": 4096}})
    assert params["reasoning_effort"] == "high"
    assert params["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_unconfigured_sends_nothing(tmp_path, monkeypatch):
    """没配就不带 —— 默认行为不变(pi 另有内置默认表,qi 不抄)。"""
    params = _params(tmp_path, monkeypatch, api="anthropic-messages", max_tokens=8000)
    assert "thinking" not in params
    assert params == {"reasoning_effort": "high"}


def test_not_sent_for_openai_shaped_models(tmp_path, monkeypatch):
    """OpenAI 兼容形态没有统一参数 —— qi 不做那半(pi 靠每模型 compat 字段名)。"""
    params = _params(tmp_path, monkeypatch, api="openai-completions", max_tokens=8000,
                     extra={"thinkingBudgets": {"high": 4096}})
    assert "thinking" not in params


def test_budget_is_clamped_to_leave_room_for_the_answer(tmp_path, monkeypatch):
    """pi 的原话:"clamped so at least 1024 tokens remain for the answer"。"""
    params = _params(tmp_path, monkeypatch, api="anthropic-messages", max_tokens=3000,
                     extra={"thinkingBudgets": {"high": 999999}})
    assert params["thinking"]["budget_tokens"] == 3000 - 1024


def test_junk_budget_is_ignored(tmp_path, monkeypatch):
    """写歪的值(非正整数)当"没配" —— 不因为一个错字让请求带上怪参数。"""
    for value in ("4096", 0, -5, None):
        params = _params(tmp_path, monkeypatch, api="anthropic-messages", max_tokens=8000,
                         extra={"thinkingBudgets": {"high": value}})
        assert "thinking" not in params, value
