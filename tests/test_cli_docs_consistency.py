"""文档与真实 CLI 的一致性:两条方向都钉住。

这一路反复撞见同一类故障:**文档承诺了一个旗标,而它不是不存在、就是行为不同**
(`disallow_tools` 被当现成手段教、`add_route` 全仓零实现、`--provider/--api-key/--models`
被记进"不保留"却又列在别处、`-r/--session-id/--session-dir` 标着 ✅ 而代码里 0 命中)。
人工核对会漏 —— 所以把它变成测试:

1. **cli.md 表格首列写了的旗标,CLI 必须真的有**(写了却没有 = 虚假承诺);
2. **CLI 真的有的旗标,`docs/` 里必须提到过**(有了却没写 = 用户无从知道)。

两个方向的扫法故意不同:
  - 方向 1 只看**表格首列** —— 正文里出现的旗标常常是在讲 *pi*(如 `pi install`、`--plan`),
    把它们算进来会误报;
  - 方向 2 扫**整个 docs/** —— "有没有文档"这件事不该因为写在了 `model-config.md`
    而不是 `cli.md` 就算没写(`qi init` 的那批选项正是如此)。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import typer.main  # noqa: E402

from qi_agent.cli import app  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
#: typer / click 自带的,不算 qi 的面
BUILTIN = {"--help", "--install-completion", "--show-completion"}
_FLAG = re.compile(r"--[a-z][a-z0-9-]*")


def _cli_flags() -> set[str]:
    """真实 CLI 的全部长旗标(**含子命令** —— `qi auth check --credentials` 那种也算)。"""

    def walk(command) -> set[str]:
        found: set[str] = set()
        for param in command.params:
            for opt in (*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ())):
                if str(opt).startswith("--"):
                    found.add(str(opt))
        for sub in getattr(command, "commands", {}).values():
            found |= walk(sub)
        return found

    return walk(typer.main.get_command(app))


def _documented_in_tables(text: str) -> set[str]:
    """表格**首列**里写的旗标;划掉的(~~…~~)不算 —— 那些是"已删"。"""
    found: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 2 or "~~" in cells[1]:
            continue
        found |= set(_FLAG.findall(cells[1]))
    return found


def test_every_documented_flag_exists():
    """方向 1:cli.md 表格首列写了的,必须真的存在。"""
    documented = _documented_in_tables((DOCS / "cli.md").read_text(encoding="utf-8"))
    missing = sorted(documented - _cli_flags() - BUILTIN)
    assert missing == [], f"cli.md 承诺了这些旗标,但 CLI 里没有: {missing}"


def test_every_flag_is_documented_somewhere():
    """方向 2:CLI 有的,`docs/` 里必须提过(否则用户无从知道)。"""
    text = "\n".join(p.read_text(encoding="utf-8") for p in DOCS.glob("*.md"))
    documented = set(_FLAG.findall(text))
    undocumented = sorted(_cli_flags() - documented - BUILTIN)
    assert undocumented == [], f"这些旗标没有出现在 docs/ 里: {undocumented}"
