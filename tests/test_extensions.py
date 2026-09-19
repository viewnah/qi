"""P-E1:扩展宿主骨架 —— 信任门控 / settings.extensions / 旧目录迁移 / entry point 组。

四样一起测的理由:它们决定的都是「装载**什么**」。判错的后果不是报错,而是
**静默执行了不该执行的代码**(扩展 = 全权限),或者反过来 —— 配置写了却不生效。

装载优先级(先到先得,同名跳过):项目 `.qi/extensions/` → 全局 `<agent>/extensions/`
→ `settings.extensions[]` → entry point。见 docs/extensions.md §2 / §5.3。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.extensions import ExtensionBus, Tool  # noqa: E402
from qi_agent.registry import (  # noqa: E402
    EXTENSION_ENTRY_FILE,
    EXTENSION_ENTRY_POINT_GROUP,
    CapabilityRegistry,
    ExtensionBus,
    ToolCatalog,
    discover_extensions,
)
from qi_agent.settings import (  # noqa: E402
    QiSettings,
    extension_dirs,
    resolve_project_trust,
)
from qi_agent.tools import register_builtin_tools  # noqa: E402


def _source(*tool_names: str) -> str:
    """一个最小扩展:注册给定名字的工具(用工具名区分是"谁"被装载了)。"""
    lines = ["from qi_agent.registry import Tool", "", "def register(api):"]
    for name in tool_names:
        lines.append(
            f"    api.add_tool(Tool({name!r}, '探针', "
            "{'type': 'object', 'properties': {}}, None))")
    return "\n".join(lines) + "\n"


def _write_extension(root: Path, name: str, *tool_names: str) -> Path:
    """在 `root`(父目录)下建一个扩展目录 `<name>/extension.py`。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(_source(*(tool_names or (name,))),
                                          encoding="utf-8")
    return d


def _catalog() -> tuple[ToolCatalog, CapabilityRegistry]:
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    return catalog, CapabilityRegistry()


def _tool(catalog: ToolCatalog, name: str) -> Tool:
    """`ToolCatalog.get()` 返回 `Tool | None`:显式收窄(与本仓其它测试同一写法)。"""
    tool = catalog.get(name)
    assert tool is not None, f"工具未注册: {name}"
    return tool


def _source_info(catalog: ToolCatalog, name: str) -> dict:
    """取某个工具的 `source_info`(同样要收窄两次)。

    注意不要叫 `_source`:本文件已有一个 `_source(*tool_names)` 用来**生成扩展源码**,
    同名会把那个的调用点全部改解析到这里。
    """
    info = _tool(catalog, name).source_info
    assert info is not None, f"工具未盖来源: {name}"
    return info


def _project(tmp_path: Path, name: str = "proj") -> Path:
    """建一个被 `.git` 锚定的项目目录(`project_home` 靠它定位,否则会一路向上找真实仓库)。"""
    project = tmp_path / name
    (project / ".git").mkdir(parents=True)
    return project


# ── 信任门控 ────────────────────────────────────────────

def test_project_extensions_are_skipped_when_untrusted(tmp_path, monkeypatch):
    """未信任 → 项目扩展**一个字节都不执行**(不是扫了再丢)。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    project = _project(tmp_path)
    _write_extension(paths.project_home(project) / paths.EXTENSIONS_DIR_NAME, "probe")

    catalog, caps = _catalog()
    assert discover_extensions(catalog, caps, project, bus=ExtensionBus(), project_trusted=False) == []
    assert catalog.get("probe") is None

    catalog2, caps2 = _catalog()
    assert discover_extensions(catalog2, caps2, project, bus=ExtensionBus(), project_trusted=True) == ["probe"]
    assert catalog2.get("probe") is not None


def test_global_extensions_load_without_project_trust(tmp_path, monkeypatch):
    """全局目录永远可信:未信任项目不等于不用自己的扩展。"""
    home = tmp_path / "home"
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    _write_extension(home / paths.EXTENSIONS_DIR_NAME, "glob")

    catalog, caps = _catalog()
    assert discover_extensions(catalog, caps, tmp_path, bus=ExtensionBus(), project_trusted=False) == ["glob"]


def test_project_extension_wins_over_global_on_name_collision(tmp_path, monkeypatch):
    """同名时项目版胜(与 agent / skills 的分层规则一致)。"""
    home = tmp_path / "home"
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    _write_extension(home / paths.EXTENSIONS_DIR_NAME, "dup", "from_global")
    project = _project(tmp_path)
    _write_extension(paths.project_home(project) / paths.EXTENSIONS_DIR_NAME, "dup",
                     "from_project")

    catalog, caps = _catalog()
    assert discover_extensions(catalog, caps, project, bus=ExtensionBus(), project_trusted=True) == ["dup"]
    assert catalog.get("from_project") is not None
    assert catalog.get("from_global") is None      # 后到的同名被跳过


# ── settings.extensions ─────────────────────────────────

def test_settings_extensions_is_trust_gated_per_scope(tmp_path, monkeypatch):
    """项目级 settings 的 `extensions[]` 随仓库走 → 未信任时不算数;用户级照常。

    返回值带**作用域标签**:它进工具的 `source_info.scope`,一律记 temporary 会让
    `getAllTools()` 的诊断面擒谎 —— 所以标签也一并钉住。
    """
    home = tmp_path / "home"
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    project = _project(tmp_path)
    user_dir = _write_extension(tmp_path / "extra-user", "u").parent
    proj_dir = _write_extension(tmp_path / "extra-proj", "p").parent

    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"extensions": [str(user_dir)]}), encoding="utf-8")
    paths.project_home(project).mkdir(parents=True, exist_ok=True)
    (paths.project_home(project) / "settings.json").write_text(
        json.dumps({"extensions": [str(proj_dir)]}), encoding="utf-8")

    untrusted = extension_dirs(project, trusted=False)
    assert (user_dir, "user") in untrusted
    assert proj_dir not in [p for p, _ in untrusted]
    assert (proj_dir, "project") in extension_dirs(project, trusted=True)


def test_settings_extensions_accepts_a_single_extension_dir(tmp_path, monkeypatch):
    """一条附加路径可以直接指向**单个扩展目录**(对齐 pi 的 `extensions` 写法)。

    这是为什么不复用"内建目录的子目录 = 扩展"那一套:两种写法都必须成立。
    """
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    single = _write_extension(tmp_path / "solo-parent", "solo", "solo_tool")

    catalog, caps = _catalog()
    names = discover_extensions(catalog, caps, tmp_path, bus=ExtensionBus(),
                                extra_dirs=[single], project_trusted=False)
    assert names == ["solo"]
    assert catalog.get("solo_tool") is not None


def test_settings_extensions_accepts_a_parent_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    parent = tmp_path / "many-parent"
    _write_extension(parent, "a", "tool_a")
    _write_extension(parent, "b", "tool_b")

    catalog, caps = _catalog()
    names = discover_extensions(catalog, caps, tmp_path, bus=ExtensionBus(),
                                extra_dirs=[parent], project_trusted=False)
    assert names == ["a", "b"]


# ── 信任判定矩阵 ────────────────────────────────────────

@pytest.mark.parametrize("approve,default,expected", [
    (True, "never", True),        # CLI -a 压过 settings
    (False, "always", False),     # CLI -na 压过 settings
    (None, "always", True),
    (None, "never", False),
    (None, "ask", False),         # 询问未实现 → 保守不信任
])
def test_resolve_project_trust_matrix(approve, default, expected):
    trusted, _reason = resolve_project_trust(QiSettings(defaultProjectTrust=default),
                                            approve=approve)
    assert trusted is expected


def test_resolve_project_trust_without_settings_is_untrusted():
    assert resolve_project_trust(None)[0] is False


def test_resolve_project_trust_unknown_value_falls_back_to_ask():
    """未知取值按 ask(保守)—— 不是按 always。"""
    trusted, reason = resolve_project_trust(QiSettings(defaultProjectTrust="yolo"))
    assert trusted is False
    assert "ask" in reason


def test_headless_ask_is_untrusted_and_says_why():
    """E16:无 UI 的 `ask` 必须 fail-safe,而且理由要能指导用户(`-a`)。"""
    trusted, reason = resolve_project_trust(QiSettings(defaultProjectTrust="ask"),
                                            has_ui=False)
    assert trusted is False
    assert "-a" in reason


# ── 旧目录迁移 ──────────────────────────────────────────

def test_legacy_global_plugins_dir_migrates_and_becomes_loadable(tmp_path, monkeypatch):
    """`<agent>/plugins/old/plugin.py` → `extensions/old/extension.py`,且**能装载**。

    只搬目录不搬入口名 = 搬了个没人读的目录(目录名对了、入口名还是旧的),
    所以这条断言必须包含"装载成功",不能只看目录在不在。
    """
    home = tmp_path / "home"
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    legacy = home / paths.PLUGINS_DIR_NAME / "old"
    legacy.mkdir(parents=True)
    (legacy / "plugin.py").write_text(_source("legacy_tool"), encoding="utf-8")

    moved = paths.migrate_extensions_dir()
    assert moved, "应当报告搬迁"
    assert not (home / paths.PLUGINS_DIR_NAME).exists()
    assert (home / paths.EXTENSIONS_DIR_NAME / "old" / EXTENSION_ENTRY_FILE).is_file()

    catalog, caps = _catalog()
    assert discover_extensions(catalog, caps, tmp_path, bus=ExtensionBus(), project_trusted=False) == ["old"]
    assert catalog.get("legacy_tool") is not None


def test_migration_never_overwrites_an_existing_extensions_dir(tmp_path, monkeypatch):
    """目标已存在 → 不动(v0.1 迁移的同一条规矩)。"""
    home = tmp_path / "home"
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    (home / paths.PLUGINS_DIR_NAME / "old").mkdir(parents=True)
    kept = _write_extension(home / paths.EXTENSIONS_DIR_NAME, "new", "new_tool")

    assert paths.migrate_extensions_dir() == []
    assert (home / paths.PLUGINS_DIR_NAME).is_dir()      # 原地不动
    assert kept.is_dir()


def test_project_legacy_plugins_dir_is_only_reported(tmp_path, monkeypatch):
    """项目侧的旧目录**只提示不搬** —— 仓库内容不擅自改(会动 git 状态)。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    project = _project(tmp_path)
    legacy = paths.project_home(project) / paths.PLUGINS_DIR_NAME
    (legacy / "old").mkdir(parents=True)

    assert paths.legacy_project_extensions_dir(project) == legacy
    assert paths.global_home() == tmp_path / "home"      # ensure_layout 只动全局侧
    paths.ensure_layout()
    assert legacy.is_dir()


# ── entry point 组 ──────────────────────────────────────

def test_entry_point_group_is_qi_extensions(monkeypatch):
    """cold cut:`qi.plugins` 不再被读,也不留别名(见 extensions.md E1)。"""
    seen: list[str | None] = []

    def fake_entry_points(*, group=None, **kwargs):
        seen.append(group)
        return []

    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)
    catalog, caps = _catalog()
    discover_extensions(catalog, caps, None, bus=ExtensionBus(), project_trusted=False)
    assert EXTENSION_ENTRY_POINT_GROUP == "qi.extensions"
    assert seen == ["qi.extensions"]


def test_extension_failure_is_loud(tmp_path, monkeypatch):
    """扩展坏 → 启动报错,不静默跳过(装了却没生效是最难诊断的一类问题)。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    bad = tmp_path / "bad" / "boom"
    bad.mkdir(parents=True)
    (bad / EXTENSION_ENTRY_FILE).write_text("raise RuntimeError('炸了')\n",
                                           encoding="utf-8")

    catalog, caps = _catalog()
    with pytest.raises(RuntimeError, match="boom"):
        discover_extensions(catalog, caps, tmp_path, bus=ExtensionBus(),
                            extra_dirs=[tmp_path / "bad"], project_trusted=False)


# ── 工具注册与来源(P-E2a)────────────────────────────────

_TOOL_TEMPLATE = """
from qi_agent.extensions import Tool

def register(api):
    api.registerTool(Tool(
        {name!r}, "描述", {{"type": "object", "properties": {{}}}}, None,
        prompt_snippet="一行摘要",
        prompt_guidelines=["用 {name} 做某件事。"],
    ))
    api.add_tool(Tool("alias_tool", "靠 add_tool 注册",
                      {{"type": "object", "properties": {{}}}}, None))
"""


def test_register_tool_stamps_source_info(tmp_path, monkeypatch):
    """`source_info` 在注册那一刻盖上 —— “这个工具谁装的”不靠事后回查。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    project = _project(tmp_path)
    root = paths.project_home(project) / paths.EXTENSIONS_DIR_NAME
    d = root / "stamper"
    d.mkdir(parents=True)
    (d / EXTENSION_ENTRY_FILE).write_text(_TOOL_TEMPLATE.format(name="stamped_tool"),
                                         encoding="utf-8")

    catalog, caps = _catalog()
    discover_extensions(catalog, caps, project, bus=ExtensionBus(),
                        project_trusted=True)

    info = _source_info(catalog, "stamped_tool")
    assert info["source"] == "stamper"          # 扩展名
    assert info["path"].endswith(f"stamper/{EXTENSION_ENTRY_FILE}")
    assert info["scope"] == "project"           # 来自项目目录
    assert info["origin"] == "top-level"
    # add_tool 是 registerTool 的别名,走同一条盖章路径
    assert _source_info(catalog, "alias_tool")["source"] == "stamper"


def test_builtin_tools_are_stamped_as_builtin(tmp_path):
    """内置工具用同一把尺子标记,`getAllTools()` 才能区分内置与扩展。"""
    catalog, _caps = _catalog()
    info = _source_info(catalog, "read")
    assert info == {"source": "builtin", "path": "<builtin:read>",
                    "scope": "temporary", "origin": "top-level"}
    assert all(_source_info(catalog, n)["source"] == "builtin" for n in catalog.names)


def test_tool_can_declare_its_own_source_info(tmp_path, monkeypatch):
    """工具自带的 `source_info` 不被覆盖(它可能知道得比装载器更准)。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    project = _project(tmp_path)
    d = paths.project_home(project) / paths.EXTENSIONS_DIR_NAME / "custom"
    d.mkdir(parents=True)
    (d / EXTENSION_ENTRY_FILE).write_text("""
from qi_agent.extensions import Tool

def register(api):
    tool = Tool("self_declared", "描述", {"type": "object", "properties": {}}, None,
                source_info={"source": "我自己说的", "path": "x",
                             "scope": "user", "origin": "package"})
    api.registerTool(tool)
""", encoding="utf-8")

    catalog, caps = _catalog()
    discover_extensions(catalog, caps, project, bus=ExtensionBus(),
                        project_trusted=True)
    assert _source_info(catalog, "self_declared")["source"] == "我自己说的"


def test_extra_dirs_scope_labels(tmp_path, monkeypatch):
    """`extra_dirs` 两种形状:裸 `Path` 记 temporary,`(Path, scope)` 用给定标签。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    bare = tmp_path / "bare"
    _write_extension(bare, "b", "tool_b")
    tagged = tmp_path / "tagged"
    _write_extension(tagged, "t", "tool_t")

    catalog, caps = _catalog()
    names = discover_extensions(catalog, caps, tmp_path, bus=ExtensionBus(),
                                extra_dirs=[bare, (tagged, "user")],
                                project_trusted=False)
    assert names == ["b", "t"]
    assert _source_info(catalog, "tool_b")["scope"] == "temporary"
    assert _source_info(catalog, "tool_t")["scope"] == "user"


def test_tool_is_the_same_class_from_registry_and_extensions():
    """公开面搬家后的兼容底线:两条 import 路径必须是**同一个类**。

    否则 `isinstance` 判断会在“旧写法建的 Tool”与“新写法建的 Tool”之间失效 ——
    那是很难从现象反推的一类 bug。
    """
    from qi_agent import extensions as ext
    from qi_agent import registry as reg

    assert reg.Tool is ext.Tool
    assert reg.ToolError is ext.ToolError
