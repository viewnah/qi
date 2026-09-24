"""第三方声明必须存在、内容完整,并且**随 wheel 发布**。

qi 里有两处素材逐字移植自上游 pi(`src/qi_agent/themes/{dark,light}.json` 与
`src/qi_agent/compaction.py` 的四个摘要提示词)。pi 是 MIT,条件很明确:
"the above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software"。

所以这个文件不是"写了就好",要钉住三件事:

1. 声明文件在仓库根目录存在,且写明上游、版权人与 MIT 全文;
2. 它被 force-include 进包(`qi_agent/THIRD_PARTY_NOTICES.md`)—— 只放仓库根目录,
   对**装包的用户**不算"随附";
3. 移植点就近可见(themes 目录有 README、`compaction.py` 的 docstring 指向声明),
   免得后来者"顺手改掉"却不知道那是别人的文本。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]
NOTICE = REPO / "THIRD_PARTY_NOTICES.md"


def test_notice_exists_and_names_the_upstream():
    assert NOTICE.is_file(), "缺 THIRD_PARTY_NOTICES.md"
    text = NOTICE.read_text(encoding="utf-8")
    for needle in ("earendil-works/pi", "Mario Zechner", "MIT License",
                   "Permission is hereby granted", "WITHOUT WARRANTY OF ANY KIND"):
        assert needle in text, f"声明里少了 {needle!r}"


def test_notice_lists_what_was_ported():
    """移植清单要说清"哪些东西是别人的" —— 否则声明就只是装饰。"""
    text = NOTICE.read_text(encoding="utf-8")
    for needle in ("themes/dark.json", "compaction.py", "SUMMARIZATION_PROMPT"):
        assert needle in text, f"移植清单里少了 {needle!r}"


def test_notice_ships_with_the_wheel():
    """核 pyproject 的 force-include:装包的用户也要拿到这份声明。"""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '"THIRD_PARTY_NOTICES.md" = "qi_agent/THIRD_PARTY_NOTICES.md"' in text


def test_ported_places_point_back_at_the_notice():
    """就近可见:两份移植素材所在的位置都要能指回声明。"""
    themes_readme = REPO / "src" / "qi_agent" / "themes" / "README.md"
    assert themes_readme.is_file(), "themes/ 少了说明来源与许可的 README"
    assert "THIRD_PARTY_NOTICES.md" in themes_readme.read_text(encoding="utf-8")

    compaction = (REPO / "src" / "qi_agent" / "compaction.py").read_text(encoding="utf-8")
    head = compaction[:2000]
    assert "THIRD_PARTY_NOTICES.md" in head, "compaction.py 顶部要注明提示词是移植的"
    assert "Mario Zechner" in head
