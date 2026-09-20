"""打包与注入链路:`docs/` 进 wheel + 手册索引进提示词 + `--append-system-prompt`。

对齐 pi 的两件事:

* pi 的默认 system prompt 里有 `Additional docs: <docsPath>` 加一张**主题 → 文件**的表,
  让模型先读手册再动手(`dist/core/system-prompt.js`)。qi 做同样的事,但表**由
  `docs/docs.json` 的 navigation 生成**(pi 是写死在源码里的字符串),所以手册增删不会漂。
* `--append-system-prompt <text>`(pi 同名义):追加到 system prompt 末尾。

三条边界必须钉住:

1. 索引只在**默认基座**分支出现 —— 自定义 `SYSTEM.md` 是"整体替换",作者自己决定要不要提;
2. 没有能读文件的工具(`read`/`bash`)就**整块不注入**(给了路径也读不到,和技能清单同规矩);
3. 路径必须是**实际解析出来的绝对路径**(wheel 里在 `qi_agent/docs/`,源码树里在仓库根),
   不能写死 `docs/`。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from qi_agent import paths, runtime as runtime_mod  # noqa: E402
from qi_agent.cli import app  # noqa: E402
from qi_agent.models import AgentEvent  # noqa: E402
from qi_agent.registry import ToolCatalog  # noqa: E402
from qi_agent.system_prompt import (  # noqa: E402
    build_system_prompt,
    default_base_prompt,
    docs_navigation,
    render_docs_index,
)
from qi_agent.tools import register_builtin_tools  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _tools(*names: str):
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    return catalog.resolve(list(names or sorted(catalog.names)))


# ── 路径解析(#4) ─────────────────────────────────────────────────────


def test_package_dir_is_the_qi_agent_package():
    assert paths.package_dir().name == "qi_agent"
    assert (paths.package_dir() / "cli.py").is_file()


def test_docs_dir_falls_back_to_the_repo_tree():
    """开发树里没有 `qi_agent/docs/`,要退到仓库根的 `docs/`。"""
    root = paths.docs_dir()
    assert root is not None, "源码树里应该能找到 docs/"
    assert (root / paths.DOCS_INDEX_FILE_NAME).is_file()


# ── 索引渲染(#6) ─────────────────────────────────────────────────────


def test_docs_navigation_reads_docs_json():
    navigation = docs_navigation()
    assert navigation, "docs.json 的 navigation 应该解析出内容"
    titles = [title for title, _items in navigation]
    assert any("参考" in t or "开始" in t for t in titles), titles
    names = [name for _t, items in navigation for _title, name in items]
    assert "cli.md" in names and "extensions.md" in names


def test_render_docs_index_uses_the_real_absolute_root():
    text = render_docs_index(["read"])
    assert text, "有 read 工具时应该注入索引"
    assert str(paths.docs_dir()) in text, "必须报出实际路径(不能写死 docs/)"
    assert "cli.md" in text
    assert "$" not in text, "别把目录当 shell 变量拼"


def test_render_docs_index_needs_a_reader_tool():
    """没有 read/bash 就整块不注入 —— 给了路径也读不到(与 <available_skills> 同规矩)。"""
    assert render_docs_index([]) == ""
    assert render_docs_index(["write", "edit"]) == ""
    assert render_docs_index(["bash"]) != ""


def test_default_base_prompt_includes_the_index():
    built = default_base_prompt(_tools("read"))
    assert "文档(" in built
    assert "cli.md" in built


def test_custom_base_prompt_replaces_the_index_too():
    """`SYSTEM.md` 是整体替换:作者自己决定要不要提手册。"""
    built = build_system_prompt("我自己的基座", cwd=REPO,
                                tools=_tools("read"), context_files=[])
    assert built.startswith("我自己的基座")
    assert "文档(" not in built


# ── `--append-system-prompt`(#7) ─────────────────────────────────────


def test_append_lands_at_the_end():
    built = build_system_prompt("基座", cwd=REPO, tools=_tools("read"),
                                context_files=[], append="MARKER-追加段")
    assert built.rstrip().endswith("MARKER-追加段")
    assert built.index("MARKER-追加段") > built.index("当前工作目录")


def test_blank_append_is_ignored():
    plain = build_system_prompt("基座", cwd=REPO, tools=_tools("read"), context_files=[])
    same = build_system_prompt("基座", cwd=REPO, tools=_tools("read"),
                               context_files=[], append="   \n  ")
    assert plain == same


class FakeRuntime:
    def __init__(self, *args, **kwargs):
        from qi_agent.session import SessionStore

        self.kwargs = dict(kwargs)
        self.sessions = SessionStore()
        self.cwd = Path.cwd()
        self.notes: list[str] = []
        self.flag_errors: list[str] = []
        self.prompts: list[str] = []
        CREATED.append(self)

    async def start_session(self, session, reason: str = "startup") -> None:
        return None

    async def stream(self, prompt, session, agent_override=None):
        self.prompts.append(prompt)
        yield AgentEvent(kind="text", agent="core", text=f"echo:{prompt}")


CREATED: list[FakeRuntime] = []
runner = CliRunner()


@pytest.fixture
def fake_runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    CREATED.clear()
    monkeypatch.setattr(runtime_mod, "QiRuntime", FakeRuntime)
    return FakeRuntime


def test_cli_append_flag_reaches_the_runtime(fake_runtime):
    res = runner.invoke(app, ["-p", "--no-session",
                              "--append-system-prompt", "A", "--append-system-prompt", "B", "hi"])
    assert res.exit_code == 0, res.output
    assert CREATED[-1].kwargs.get("append_system_prompt") == ["A", "B"]


# ── 打包(#5) ────────────────────────────────────────────────────────


def test_wheel_force_includes_docs():
    """手册必须跟包一起走:提示词报的路径得在用户机器上真实存在。"""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.hatch.build.targets.wheel.force-include]", 1)
    assert len(section) == 2, "wheel 少了 force-include 段"
    body = section[1].split("\n[", 1)[0]
    mappings = [line.strip() for line in body.splitlines()
                if line.strip() and not line.strip().startswith("#")]
    assert mappings == ['"docs" = "qi_agent/docs"'], mappings


def test_docs_json_matches_the_manuals_on_disk():
    """索引与实际手册不许漂(提示词报的路径必须是真文件)。"""
    root = paths.docs_dir()
    assert root is not None
    data = json.loads((root / paths.DOCS_INDEX_FILE_NAME).read_text(encoding="utf-8"))
    missing = [item["path"] for group in data["navigation"] for item in group["items"]
               if not (root / item["path"]).is_file()]
    assert missing == [], missing
