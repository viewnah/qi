"""运行时目录与路径约定(对齐 pi 的两级布局)。

pi 的全局侧比项目侧深一层,配对关系是 `~/.pi/agent/` ↔ `<项目>/.pi/`:

    全局(agent 目录)  `~/.qi/agent`      ← 对齐 pi 的 `~/.pi/agent` / PI_CODING_AGENT_DIR
    名字空间根        `~/.qi`            ← 只作外壳,内容都在 agent/ 下
    项目              `<git根>/.qi`       ← 与 `~/.qi/agent` 配对,保持扁平

`QI_AGENT_HOME` 指向的是 **agent 目录本身**(语义同 `PI_CODING_AGENT_DIR`),
`QI_CONFIG_DIR` 覆盖名字空间根,`QI_AGENT_CONFIG` 直接指定 models.json 文件。

模型配置文件名 `models.json`,格式对齐 pi;凭证明文只在 `auth.json`。
旧版扁平布局(`~/.qi/models.json` 等)在启动时自动迁移到 `~/.qi/agent/`。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

GLOBAL_DIR_NAME = ".qi"
AGENT_DIR_NAME = "agent"
MODELS_FILE_NAME = "models.json"
AUTH_FILE_NAME = "auth.json"
SETTINGS_FILE_NAME = "settings.json"
SYSTEM_FILE_NAME = "SYSTEM.md"
#: 角色定义文件名。core **不读**它(qi-agents 读);这里只用来判「有没有角色目录」,
#: 好在没装 qi-agents 时说一句(见 `QiRuntime._note_missing_agent_support`)。
AGENT_FILE_NAME = "agent.md"
SESSIONS_DIR_NAME = "sessions"
AGENTS_DIR_NAME = "agents"
PLUGINS_DIR_NAME = "plugins"        # ⚠️ v0.1 旧名,仅用于迁移;新代码用 EXTENSIONS_DIR_NAME
EXTENSIONS_DIR_NAME = "extensions"  # 扩展目录(P-E1 改名:plugins → extensions)
SKILLS_DIR_NAME = "skills"
CROSS_TOOL_DIR_NAME = ".agents"   # Agent Skills 标准的跨工具目录(~/.agents, .agents)
DOCS_DIR_NAME = "docs"           # 手册目录(打包后住 qi_agent/docs/,源码树里在仓库根)
DOCS_INDEX_FILE_NAME = "docs.json"  # 手册索引:导航 + 重定向(见 docs/docs.json)

# 环境变量
QI_AGENT_HOME = "QI_AGENT_HOME"      # 覆盖全局 agent 目录(对齐 PI_CODING_AGENT_DIR)
QI_CONFIG_DIR = "QI_CONFIG_DIR"      # 覆盖名字空间根(默认 ~/.qi)
QI_AGENT_CONFIG = "QI_AGENT_CONFIG"  # 指向 models.json 具体文件(最高优先级)

# v0.1 的扁平布局条目:启动时搬进 agent/(目标已存在则不动)
LEGACY_GLOBAL_ENTRIES = (
    MODELS_FILE_NAME,
    AUTH_FILE_NAME,
    SETTINGS_FILE_NAME,
    SYSTEM_FILE_NAME,
    AGENTS_DIR_NAME,
    PLUGINS_DIR_NAME,      # 旧名,落地时按 LEGACY_ENTRY_RENAMES 改成 extensions/
    SKILLS_DIR_NAME,
    SESSIONS_DIR_NAME,
)

#: 旧条目名 → 现名(改名过的)。搬运时按**现名**落点,否则搬过去也没人读。
LEGACY_ENTRY_RENAMES = {PLUGINS_DIR_NAME: EXTENSIONS_DIR_NAME}


def package_dir() -> Path:
    """`qi_agent` 包目录。打包后的手册就住在它下面(`qi_agent/docs/`)。"""
    return Path(__file__).resolve().parent


def docs_dir() -> Path | None:
    """手册目录:优先 wheel 里那份(`qi_agent/docs/`),退回源码树的 `<repo>/docs/`。

    两处都找不到就返回 `None` —— 调用方据此**整块不注入**(而不是往提示词里写一个
    不存在的路径)。`--no-tools` 之类把 read/bash 拿掉的场景也由调用方另判。
    """
    packaged = package_dir() / DOCS_DIR_NAME
    if packaged.is_dir():
        return packaged
    repo = package_dir().parents[1] / DOCS_DIR_NAME      # src/qi_agent → 仓库根
    return repo if repo.is_dir() else None


def config_root() -> Path:
    """名字空间根(`~/.qi`;仅用于推导默认路径与迁移)。"""
    override = os.environ.get(QI_CONFIG_DIR)
    return Path(override).expanduser() if override else Path.home() / GLOBAL_DIR_NAME


def global_home() -> Path:
    """全局 agent 目录(`~/.qi/agent`,可用 QI_AGENT_HOME 覆盖)。

    这是全部用户级状态的挂载点:settings.json / models.json / auth.json /
    sessions / skills / agents / extensions。对齐 pi 的 `getAgentDir()`。
    """
    override = os.environ.get(QI_AGENT_HOME)
    return Path(override).expanduser() if override else config_root() / AGENT_DIR_NAME


def find_project_root(start: Path | None = None) -> Path:
    """向上找含 .git 的目录作为项目根;找不到则用 cwd。"""
    cur = (start or Path.cwd()).resolve()
    if cur.is_file():
        cur = cur.parent
    for p in (cur, *cur.parents):
        if (p / ".git").exists():
            return p
    return cur


def project_home(cwd: Path | None = None) -> Path:
    """项目 qi 目录(<git根>/.qi,扁平;与 user 侧 agent/ 配对)。"""
    return find_project_root(cwd) / GLOBAL_DIR_NAME


def project_context_ancestors(cwd: Path | None = None) -> list[Path]:
    """cwd → git 根(无 git 则到文件系统根)的祖先链,**由近到远**。

    用于 `.agents/skills` 这类允许逐级继承的目录(对齐 pi 的祖先探测)。
    """
    cur = (cwd or Path.cwd()).resolve()
    if cur.is_file():
        cur = cur.parent
    root = find_project_root(cur)
    chain = [cur, *cur.parents]
    return [p for p in chain if p >= root]


def models_file_candidates(cwd: Path | None = None) -> list[Path]:
    """按优先级从高到低返回可能的 models.json 路径(含 env 指定文件)。"""
    candidates: list[Path] = []
    env_file = os.environ.get(QI_AGENT_CONFIG)
    if env_file:
        candidates.append(Path(env_file).expanduser())
    candidates.append(project_home(cwd) / MODELS_FILE_NAME)
    candidates.append(global_home() / MODELS_FILE_NAME)
    return candidates


def settings_file_candidates(cwd: Path | None = None) -> list[tuple[Path, str]]:
    """按优先级从高到低返回 (settings.json, 来源)。

    来源取值 `project` / `user`;路径类字段相对**各自文件所在目录**解析。
    """
    return [
        (project_home(cwd) / SETTINGS_FILE_NAME, "project"),
        (global_home() / SETTINGS_FILE_NAME, "user"),
    ]


def cross_tool_skills_dirs(cwd: Path | None = None) -> list[tuple[Path, str]]:
    """Agent Skills 标准的 `.agents/skills` 目录,低 → 高优先级。

    全局 `~/.agents/skills`(永远视为已信任)与项目祖先链 `.agents/skills`
    (由远到近,近者覆盖远者)。不对应任何单一工具,见 design/agent-config-design.md。
    """
    home = Path.home() / CROSS_TOOL_DIR_NAME / SKILLS_DIR_NAME
    project = [
        p / CROSS_TOOL_DIR_NAME / SKILLS_DIR_NAME
        for p in reversed(project_context_ancestors(cwd))
    ]
    return [(home, "agents-global"), *((p, "agents-project") for p in project)]


def migrate_legacy_layout() -> list[tuple[Path, Path]]:
    """把 v0.1 扁平布局(`~/.qi/<条目>`)搬进 `~/.qi/agent/`。

    仅在目标不存在时搬(绝不覆盖);显式设了 QI_AGENT_HOME 时不动 —— 那是测试/CI
    自己指定的目录,不该被猜测。返回实际搬迁的 (旧, 新) 列表。
    """
    if os.environ.get(QI_AGENT_HOME):
        return []
    root = config_root()
    if not root.is_dir():
        return []
    target = global_home()
    moved: list[tuple[Path, Path]] = []
    for name in LEGACY_GLOBAL_ENTRIES:
        src, dst = root / name, target / LEGACY_ENTRY_RENAMES.get(name, name)
        if not src.exists() or dst.exists():
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError:
            continue
        if name == PLUGINS_DIR_NAME:
            _migrate_extension_entries(dst)
        moved.append((src, dst))
    return moved


#: 扩展目录内的旧入口名(registry.EXTENSION_ENTRY_FILE 的旧值)。
#: 这里写字面量而不是从 registry 导入:registry 反过来依赖 paths,不能成环。
_LEGACY_EXTENSION_ENTRY = "plugin.py"


def _migrate_extension_entries(root: Path) -> None:
    """把已搬进 `extensions/` 的入口 `plugin.py` 改成 `extension.py`。

    不改名等于**搬了个没人读的目录** —— 目录名对了、入口名还是旧的,扩展不会加载。
    仍然只在目标不存在时改,不覆盖任何东西。
    """
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        old, new = child / _LEGACY_EXTENSION_ENTRY, child / "extension.py"
        if old.is_file() and not new.exists():
            try:
                old.rename(new)
            except OSError:
                continue


def project_extensions_dir(cwd: Path | None = None) -> Path:
    """项目级扩展目录(`<项目>/.qi/extensions`)。"""
    return project_home(cwd) / EXTENSIONS_DIR_NAME


def migrate_extensions_dir() -> list[tuple[Path, Path]]:
    """`<agent 目录>/plugins/`(v1 名)→ `extensions/`。

    **只动用户自己的 agent 目录**。项目 `.qi/` 属于仓库内容,擅自搬迁会改 git 状态,
    所以那边只提示(见 `legacy_project_extensions_dir`)。仍然遵守"目标存在则不动"。
    """
    target = global_home()
    src, dst = target / PLUGINS_DIR_NAME, target / EXTENSIONS_DIR_NAME
    if not src.is_dir() or dst.exists():
        return []
    try:
        shutil.move(str(src), str(dst))
    except OSError:
        return []
    _migrate_extension_entries(dst)
    return [(src, dst)]


def legacy_project_extensions_dir(cwd: Path | None = None) -> Path | None:
    """项目里残留的旧 `.qi/plugins/`(存在则返回路径)—— 专供启动提示,不自动搬。"""
    legacy = project_home(cwd) / PLUGINS_DIR_NAME
    return legacy if legacy.is_dir() else None


def ensure_layout() -> list[tuple[Path, Path]]:
    """启动时调用:建好目录 + 完成旧布局迁移,返回搬迁记录。"""
    global_home().mkdir(parents=True, exist_ok=True)
    return migrate_legacy_layout() + migrate_extensions_dir()
