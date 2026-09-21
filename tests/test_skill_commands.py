"""`/skill:<名>`:技能的命令入口(pi 的 `enableSkillCommands`)。

技能平时是**渐进披露**(提示词里只有名字/描述/路径,正文用时才读),而 `/skill:<名>` 是
**强制加载**的入口 —— pi 的 `docs/skills.md` 原话:"use prompting or `/skill:name` to force it",
示例 `/skill:brave-search`(加载并执行)与 `/skill:pdf-tools extract`(带参数)。

qi 这边以前是个**没人读的字段**:`settings.enableSkillCommands` 在模型里,而全 `src/` 没有一处
读它。现在它真的管事了 —— 关掉时这批命令不存在。

两个可测的部分都做成了纯函数(`skill_command_candidates` / `skill_invocation`),所以不必
搭 TUI 夹具。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qi_agent.models import Skill  # noqa: E402
from qi_agent.tui import skill_command_candidates, skill_invocation  # noqa: E402


def _skill(tmp_path: Path, name: str = "brave-search", body: str = "正文:怎么搜") -> Skill:
    path = tmp_path / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return Skill(name=name, description="搜网页", path=path, source="qi-global")


def _rt(skills, *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(top_skills=skills,
                           settings=SimpleNamespace(enableSkillCommands=enabled))


def test_candidates_are_registered_as_commands(tmp_path):
    candidates = skill_command_candidates(_rt([_skill(tmp_path)]))
    assert candidates == [("/skill:brave-search", "搜网页")]


def test_disabled_setting_removes_the_commands(tmp_path):
    """关掉时这批命令**不存在**(而不是存在但报错)—— pi 的字段就是这个意思。"""
    assert skill_command_candidates(_rt([_skill(tmp_path)], enabled=False)) == []


def test_no_runtime_or_no_skills_is_empty():
    assert skill_command_candidates(None) == []
    assert skill_command_candidates(_rt([])) == []


def test_invocation_carries_the_full_text(tmp_path):
    """加载 = 把 SKILL.md **全文**提交给模型(与用户手贴等价,只是不用手抄)。"""
    text = skill_invocation(_skill(tmp_path, body="第一行\n第二行"), "")
    assert "按技能 `brave-search` 执行" in text
    assert "第一行\n第二行" in text


def test_invocation_passes_arguments(tmp_path):
    """`/skill:pdf-tools extract` 的参数要带上(pi 的示例就是这个形状)。"""
    text = skill_invocation(_skill(tmp_path, name="pdf-tools"), "extract")
    assert "按技能 `pdf-tools` 执行:extract" in text


def test_missing_file_is_a_readable_error(tmp_path):
    """技能文件被删了 → 报得出来(pure 函数抛 OSError,调用方转成人话)。"""
    import pytest

    skill = _skill(tmp_path)
    skill.path.unlink()
    with pytest.raises(OSError):
        skill_invocation(skill, "")
