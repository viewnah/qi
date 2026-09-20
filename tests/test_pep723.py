"""PEP 723 内联声明:目录通道的依赖声明解析 + 它接上的那个检查。

为什么需要它:目录通道的扩展**没有 dist 元数据**(它不是装上去的),所以 §5.5 / E11 那条
"别把宿主写进依赖"的检查以前对它**直接跳过**(`dist is None` → return)。PEP 723 的
`# /// script` 块补上这一半,让两个通道对称。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from qi_agent import cli, paths  # noqa: E402
from qi_agent.pep723 import dependencies_of, parse_inline_metadata  # noqa: E402

ENTRY = "extension.py"

BLOCK = '''# /// script
# dependencies = ["qi-agent", "requests>=2"]
# requires-python = ">=3.12"
# ///
from qi_agent.extensions import Tool   # noqa: F401


def register(api):
    pass
'''


# ── 解析 ────────────────────────────────────────────────

def test_parses_the_script_block():
    meta = parse_inline_metadata(BLOCK)
    assert meta["script"]["dependencies"] == ["qi-agent", "requests>=2"]
    assert meta["script"]["requires-python"] == ">=3.12"


def test_no_block_is_empty_not_an_error():
    assert parse_inline_metadata("print(1)\n") == {}
    assert parse_inline_metadata("") == {}


def test_malformed_toml_only_skips_that_block():
    """这层是辅助信息:一块 TOML 写坏不该让扩展装载失败(装载有自己的报错路径)。"""
    broken = '# /// script\n# dependencies = [\n# ///\n'
    assert parse_inline_metadata(broken) == {}

    mixed = broken + '\n# /// other\n# x = 1\n# ///\n'
    assert parse_inline_metadata(mixed) == {"other": {"x": 1}}


def test_other_block_types_are_kept_separately():
    text = '# /// script\n# dependencies = ["a"]\n# ///\n# /// custom\n# n = 2\n# ///\n'
    meta = parse_inline_metadata(text)
    assert set(meta) == {"script", "custom"}
    assert meta["custom"] == {"n": 2}


def test_dependencies_of_accepts_a_file_or_a_directory(tmp_path):
    (tmp_path / ENTRY).write_text(BLOCK, encoding="utf-8")
    assert dependencies_of(tmp_path / ENTRY) == ["qi-agent", "requests>=2"]
    assert dependencies_of(tmp_path) == ["qi-agent", "requests>=2"]     # 目录 → 找入口文件


def test_dependencies_of_tolerates_missing_or_odd_shapes(tmp_path):
    assert dependencies_of(tmp_path / "不存在.py") == []
    odd = tmp_path / "odd.py"
    odd.write_text('# /// script\n# dependencies = "字符串不是列表"\n# ///\n', encoding="utf-8")
    assert dependencies_of(odd) == []


# ── 接上的检查:目录通道也要查宿主依赖 ───────────────────

def _install(home: Path, name: str, body: str) -> None:
    d = home / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / ENTRY).write_text(body, encoding="utf-8")


def _report(tmp_path, monkeypatch, body: str) -> list[str]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    _install(home, "sneaky", body)
    _lines, warnings = cli._extension_report(project)
    return warnings


def test_directory_extension_declaring_the_host_is_reported(tmp_path, monkeypatch):
    """**这就是这条检查的意义**:目录通道的扩展把宿主写进依赖,以前查不到。"""
    warnings = _report(tmp_path, monkeypatch, BLOCK)
    assert any("qi-agent" in w and "script" in w for w in warnings), warnings
    assert any("别" in w or "去掉" in w for w in warnings), warnings      # 给的是可执行动作


def test_a_normal_dependency_is_not_reported(tmp_path, monkeypatch):
    body = '# /// script\n# dependencies = ["requests>=2"]\n# ///\n\n\ndef register(api):\n    pass\n'
    assert _report(tmp_path, monkeypatch, body) == []


def test_no_declaration_means_no_check_and_no_noise(tmp_path, monkeypatch):
    assert _report(tmp_path, monkeypatch, "def register(api):\n    pass\n") == []
