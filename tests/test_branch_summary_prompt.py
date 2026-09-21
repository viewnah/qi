"""`/tree` 跳转前先问要不要摘要(pi 的 `branchSummary` 段)。

pi 的原文(`docs/settings.md`):

    branchSummary.skipPrompt | false | Skip "Summarize branch?" prompt on /tree navigation
                                       (**defaults to no summary**)

所以它有两个方向,qi 以前**两个都不对**(qi 是直接摘要、不问):

* 默认:**问一句**,默认答案"不摘要";
* `skipPrompt: true`:**不问也不摘要** —— 不是"自动摘要"(那是很容易搞反的一处);
* 无前端时 `ui.confirm` 返回调用方给的 `default`,正好就是 pi 那句 "defaults to no summary"。

门放在 **runtime**(`summarize_branch_for_jump`)而不是 TUI:一处生效,不必给 `/tree` 那条
交互路径做手术。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.settings import QiSettings, branch_summary_skip_prompt  # noqa: E402

_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'
_MODELS = json.dumps({"providers": {"ollama": {"api": "openai-completions",
                                               "models": [{"id": "x"}]}}})


class _Ui:
    """最小前端:只记"被问了几次"并按设定回答。"""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked = 0

    async def confirm(self, message, *, title=None, default=False) -> bool:
        self.asked += 1
        return self.answer

    async def select(self, *a, **k):
        return None

    async def input(self, *a, **k):
        return None

    def notify(self, message, *, level="info") -> None:
        return None


def _runtime(tmp_path, monkeypatch, ui: _Ui, settings: dict | None = None):
    from qi_agent.runtime import QiRuntime

    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({**json.loads(_SETTINGS), **(settings or {})}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    return QiRuntime(cwd=project, approve_project=True, ui_frontend=ui)


# ── 纯函数(默认 false,且**严格**布尔) ────────────────────────────────


def test_default_is_ask():
    assert branch_summary_skip_prompt(QiSettings()) is False


@pytest.mark.parametrize("value, expected", [
    (True, True),
    (False, False),
    ("yes", False),      # 严格:写歪的值当 false(照常问),不静默关掉
    (1, False),
])
def test_strict_boolean(value, expected):
    assert branch_summary_skip_prompt(QiSettings(branchSummary={"skipPrompt": value})) is expected


def test_missing_or_empty_section_is_false():
    """没配 / 配成空对象 → 照常问(`false`)。

    函数里那条"非 dict 就当 false"的分支是**防御性**的:pydantic 把 `branchSummary` 校验成
    dict,所以它到不了那里 —— 不为了让分支有覆盖去伪造一个走不通的输入。
    """
    assert branch_summary_skip_prompt(QiSettings(branchSummary={})) is False
    assert branch_summary_skip_prompt(QiSettings(branchSummary={"other": 1})) is False


# ── 运行期:问 / 不问 / 答"不摘要" ──────────────────────────────────────


async def test_declining_returns_none_without_summarizing(tmp_path, monkeypatch):
    ui = _Ui(answer=False)
    rt = _runtime(tmp_path, monkeypatch, ui)
    session = rt.sessions.create("t", cwd=rt.cwd)

    assert await rt.summarize_branch_for_jump(session, session.branch(), None, None) is None
    assert ui.asked == 1, "默认要问一句"
    assert not [e for e in session.entries if e.get("type") == "branch_summary"]


async def test_skip_prompt_does_not_even_ask(tmp_path, monkeypatch):
    """`skipPrompt: true` = 不问**也不**摘要(pi 的 "defaults to no summary")。"""
    ui = _Ui(answer=True)
    rt = _runtime(tmp_path, monkeypatch, ui, {"branchSummary": {"skipPrompt": True}})
    session = rt.sessions.create("t", cwd=rt.cwd)

    assert await rt.summarize_branch_for_jump(session, session.branch(), None, None) is None
    assert ui.asked == 0, "skipPrompt 时连问都不该问"
    assert not [e for e in session.entries if e.get("type") == "branch_summary"]


async def test_no_frontend_defaults_to_no_summary(tmp_path, monkeypatch):
    """嵌入方没有前端时:`confirm` 返回 default(false)= pi 的 "defaults to no summary"。"""
    from qi_agent.runtime import QiRuntime

    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)

    rt = QiRuntime(cwd=project, approve_project=True)          # 没有 ui_frontend
    session = rt.sessions.create("t", cwd=rt.cwd)
    assert await rt.summarize_branch_for_jump(session, session.branch(), None, None) is None
