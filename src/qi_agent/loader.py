"""内容装载器(P2):agent 目录发现(两层)、agent.md/技能解析、校验、import。

对齐 design/agent-config-design.md §3/§6/§10:
- 位置: ~/.qi/agents + <cwd>/.qi/agents + 包内置 qi_agent/builtin/agents
- 优先级: 项目 > 用户 > 内置(静默覆盖);同层重复报错
- 校验: name==目录名 / description 非空 / tools 存在 / include 存在 / 技能同名冲突
- import: 与装载共用同一校验器;明文凭证扫描
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from . import paths
from .models import Skill
from .settings import (
    QiSettings,
    load_settings_by_scope,
    settings_exclude_paths,
    settings_include_paths,
)

AGENT_FILE = "agent.md"
SKILL_FILE = "SKILL.md"
ASSETS_DIR = "assets"
SKILLS_DIR = "skills"
# Agent Skills 标准的跨工具目录(~/.agents/skills、.agents/skills);
# 不对应单一工具,见 design/agent-config-design.md。
CROSS_TOOL_DIR = ".agents"
SYSTEM_FILE_NAME = "SYSTEM.md"  # 基座提示词覆盖文件

_PLAINTEXT_KEY_RE = re.compile(
    r'("?(?:api[_-]?key|password|secret|access_token|auth_config)"?\s*[:=]\s*["\'])([^"\']{6,})'
    r'|(["\']Bearer\s+)([A-Za-z0-9._~+/=-]{12,})',
    re.IGNORECASE,
)


class LoadError(Exception):
    pass


def split_frontmatter(text: str) -> tuple[dict, str]:
    """解析 '---' 包裹的 YAML frontmatter + 正文。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    body = "\n".join(lines[end + 1 :]).strip()
    return (meta if isinstance(meta, dict) else {}), body
def _skill_from_file(skill_file: Path, *, fallback_name: str, source: str) -> Skill | None:
    """从 SKILL.md(或根级 *.md)读出一个技能;frontmatter 无 description 时返回 None。"""
    try:
        meta, _body = split_frontmatter(skill_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    desc = str(meta.get("description") or "").strip()
    if not desc:
        return None
    name = str(meta.get("name") or fallback_name)
    return Skill(name=name, description=desc, path=skill_file, source=source)


def scan_skills(agent_dir: Path, source: str = "") -> list[Skill]:
    """agent 自带技能:`<agent_dir>/skills/<name>/SKILL.md`(单层,同名报错)。"""
    skills: dict[str, Skill] = {}
    label = source or f"agent:{agent_dir.name}"
    skills_root = agent_dir / SKILLS_DIR
    if skills_root.is_dir():
        for skill_dir in sorted(skills_root.iterdir()):
            skill_file = skill_dir / SKILL_FILE
            if not skill_file.is_file():
                continue
            meta, _body = split_frontmatter(skill_file.read_text(encoding="utf-8"))
            name = meta.get("name") or skill_dir.name
            desc = str(meta.get("description", "")).strip()
            if name in skills:
                raise LoadError(f"{skill_file}:技能 {name!r} 在 {agent_dir} 内重复")
            skills[name] = Skill(name=name, description=desc or "(无描述)",
                                 path=skill_file, source=label)
    return list(skills.values())


def scan_skill_root(root: Path, *, label: str, out: list[Skill],
                    allow_root_md: bool = True) -> None:
    """递归扫描一个技能根(对齐 pi):

      - 含 SKILL.md 的目录即技能,**不再向内递归**
      - 无 SKILL.md 的子目录继续向内找(支持分组目录)
      - `allow_root_md` 时,根下带 description 的 `*.md` 也算独立技能
    """
    if root.is_file():
        if root.suffix == ".md":
            skill = _skill_from_file(root, fallback_name=root.stem, source=label)
            if skill:
                out.append(skill)
        return
    if not root.is_dir():
        return

    def walk(directory: Path, *, is_root: bool) -> None:
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir():
                nested = child / SKILL_FILE
                if nested.is_file():
                    skill = _skill_from_file(nested, fallback_name=child.name, source=label)
                    if skill:
                        out.append(skill)
                    continue          # 已是技能,不再向内递归
                walk(child, is_root=False)
            elif is_root and allow_root_md and child.suffix == ".md":
                skill = _skill_from_file(child, fallback_name=child.stem, source=label)
                if skill:
                    out.append(skill)

    walk(root, is_root=True)


def _skill_layer_entries(cwd: Path,
                         scopes: dict[str, QiSettings],
                         ) -> list[tuple[Path, str, bool]]:
    """构造技能层列表(低 → 高);`scopes` 必须是各作用域**各自**的设置。"""
    entries: list[tuple[Path, str, bool]] = [
        (Path.home() / CROSS_TOOL_DIR / SKILLS_DIR, "agents-global", False),
        (paths.global_home() / SKILLS_DIR, "qi-global", True),
    ]
    entries += [(p, "settings-global", True)
                for p in settings_include_paths(scopes.get("user"), "user", "skills", cwd)]
    for ancestor in reversed(paths.project_context_ancestors(cwd)):   # 远 → 近
        entries.append((ancestor / CROSS_TOOL_DIR / SKILLS_DIR, "agents-project", False))
    entries.append((paths.project_home(cwd) / SKILLS_DIR, "qi-project", True))
    entries += [(p, "settings-project", True)
                for p in settings_include_paths(scopes.get("project"), "project", "skills", cwd)]
    return entries


def top_level_skill_dirs(cwd: Path | None = None,
                         settings: QiSettings | None = None,
                         ) -> list[tuple[Path, str, bool]]:
    """顶层技能根的完整优先级顺序(低 → 高),返回 (目录, 层标签, 允许根级 md)。

    对齐 pi 的发现规则,并叠上 qi 的私有层:

      1. `~/.agents/skills`            跨工具共享(全局,永远视为已信任)
      2. `~/.qi/agent/skills`          qi 全局私有
      3. user settings 的 `skills[]`    追加路径(相对 `~/.qi/agent`)
      4. `.agents/skills`(cwd→git 根,远→近)  跨工具共享(项目,需信任)
      5. `<cwd>/.qi/skills`          qi 项目私有
      6. project settings 的 `skills[]` 追加路径(相对 `<cwd>/.qi`)

    同一标签内的同名技能视为冲突(报错);跨标签同名则高优先级静默覆盖。
    `settings` 参数只为兼容旧调用保留 —— 两个作用域的 `skills[]` 一律从各自的
    原始文件读取,因为相对路径必须按各自所在目录解析。
    """
    del settings           # 保留参数以兼容调用方;实际按作用域各读各的
    root = Path(cwd) if cwd else Path.cwd()
    return _skill_layer_entries(root, load_settings_by_scope(root))


def load_top_level_skills(cwd: Path | None = None,
                          settings: QiSettings | None = None,
                          extra_paths: list[Path] | None = None,
                          enabled: bool = True) -> list[Skill]:
    """装载顶层技能(低 → 高覆盖);`extra_paths`(CLI `--skill`)最高优先。

    `enabled=False` 时跳过目录发现,但仍装载 extra_paths —— 对齐 pi 的
    `--no-skills`:`--skill <path>` 在禁用发现时依然生效。

    来源取值见 `top_level_skill_dirs`;返回值按优先级从低到高,可见项在后。

    `settings` 只为兼容旧调用保留(实际按作用域各读各的)。
    """
    merged: dict[str, Skill] = {}
    labels: dict[str, str] = {}          # 技能名 → 已占用的层标签
    root = Path(cwd) if cwd else Path.cwd()
    scopes = load_settings_by_scope(root)
    # 排除项(`!pat` / `-pat`)作用于**整个发现集**,不只是数组里纳入的根
    excludes: list[Path] = []
    for scope in ("user", "project"):
        for path in settings_exclude_paths(scopes.get(scope), scope, "skills", root):
            if path not in excludes:
                excludes.append(path)

    def excluded(skill: Skill) -> bool:
        return any(skill.path == ex or ex in skill.path.parents for ex in excludes)

    def add(skill: Skill) -> None:
        if excluded(skill):
            return
        owner = labels.get(skill.name)
        if owner is not None and owner == skill.source:
            raise LoadError(
                f"技能名 {skill.name!r} 在 {skill.source} 层重复(冲突文件:{skill.path})"
            )
        if owner is not None:
            merged.pop(skill.name, None)      # 高优先级覆盖低优先级
        labels[skill.name] = skill.source
        merged[skill.name] = skill

    if enabled:
        for layer_root, label, allow_root_md in _skill_layer_entries(root, scopes):
            found: list[Skill] = []
            scan_skill_root(layer_root, label=label, out=found, allow_root_md=allow_root_md)
            for skill in found:
                add(skill)

    for raw in (extra_paths or []):
        found = []
        scan_skill_root(Path(raw).expanduser(), label="cli", out=found, allow_root_md=True)
        for skill in found:
            add(skill)

    return list(merged.values())
def system_file_candidates(cwd: Path | None = None) -> list[tuple[Path, str]]:
    """可能的 SYSTEM.md 路径,按优先级从高到低返回 (文件, 来源)。"""
    return [
        (paths.project_home(cwd) / SYSTEM_FILE_NAME, "project"),
        (paths.global_home() / SYSTEM_FILE_NAME, "user"),
    ]


def resolve_base_prompt(cwd: Path | None = None) -> tuple[str, str]:
    """解析**自定义**基座,返回 `(文本, 来源说明)`;来源取值 `project|user|builtin`。

    文本为空串表示没有自定义基座 —— 调用方改用代码内默认
    (`system_prompt.default_base_prompt()`),所以基座层永不为空。
    空文件/读取失败视为未配置,继续往下一层找(不会静默降级成空提示词)。
    """
    for path, source in system_file_candidates(cwd):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return text, f"{source}:{path}"
    return "", "builtin"


# ── 项目上下文(AGENTS.md / CLAUDE.md,对齐 pi 的 project_context)────
#
# 每级目录按候选顺序取**第一个**命中的文件(顺序照搬 pi);全局(agent 目录)在最前,
# 项目祖先链由远到近追加 —— 近者可覆盖远者的同义约定。
# **祖先链一路走到文件系统根**(对齐 pi 的 `loadProjectContextFiles`;与 `.agents/skills`
# 止于 git 根的那条不同),空文件视为未配置。

CONTEXT_FILE_NAMES = ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")


def load_context_file_from_dir(directory: Path) -> tuple[Path, str] | None:
    """目录内按候选顺序取第一个 AGENTS/CLAUDE 文件;空文件/读失败视为没有。"""
    for name in CONTEXT_FILE_NAMES:
        path = directory / name
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return path, text
    return None


def load_project_context(cwd: Path | None = None) -> list[tuple[Path, str]]:
    """项目上下文文件 `(路径, 正文)`:全局 → 远祖先 → 近祖先(含 cwd);按路径去重。"""
    out: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def take(directory: Path) -> None:
        found = load_context_file_from_dir(directory)
        if found is None:
            return
        key = found[0].resolve(strict=False)
        if key in seen:
            return
        seen.add(key)
        out.append(found)

    take(paths.global_home())
    for ancestor in reversed(paths.context_file_ancestors(cwd)):   # 远 → 近
        take(ancestor)
    return out
