"""内容装载器(P2):agent 目录发现(两层)、agent.md/技能解析、校验、import。

对齐 agent-config.md §3/§6/§10:
- 位置: ~/.qi/agents + <项目>/.qi/agents + 包内置 qi_agent/builtin/agents
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
# McpScope(Literal["global","project"])与 agent 私有那层一起构成 MCP 的"来源层",
# 定义在 models.py 里 —— 它是数据形状,不是装载细节。
from .models import AgentConfig, AgentUnit, DataSource, McpScope, McpServerSpec, Skill
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
# 不对应单一工具,见 docs/agent-config.md。
CROSS_TOOL_DIR = ".agents"
MCP_FILE = "mcp.json"
DATA_SOURCES_FILE = "data_sources.json"
AGENTS_DIR = "agents"
BUILTIN_DIR = "builtin"        # 包内置内容目录(qi_agent/builtin)
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


def builtin_agents_dir() -> Path | None:
    """包内置 agent 目录(`qi_agent/builtin/agents`,随 wheel 发布)。

    作为最低优先级来源,保证「零配置也能执行」:用户/项目同名 agent 会覆盖它。
    包数据缺失(裁剪安装/打包故障)时返回 None,不影响其余来源。
    """
    import importlib.resources as resources

    try:
        path = Path(str(resources.files("qi_agent").joinpath(BUILTIN_DIR, AGENTS_DIR)))
    except (ModuleNotFoundError, OSError, TypeError):
        return None
    return path if path.is_dir() else None


def scan_agent_dirs(cwd: Path | None = None) -> dict[str, tuple[Path, str]]:
    """收集 {name: (dir, source)}。

    优先级(低 → 高,后者覆盖前者):内置 builtin → 用户 `~/.qi/agents/` →
    项目 `<git根>/.qi/agents/`。低优先级先写入,高优先级覆盖,所以项目版胜出。
    """
    found: dict[str, tuple[Path, str]] = {}
    roots: list[tuple[Path | None, str]] = [
        (builtin_agents_dir(), "builtin"),
        (paths.global_home() / AGENTS_DIR, "user"),
        (paths.project_home(cwd) / AGENTS_DIR, "project"),
    ]
    for agents_dir, source in roots:
        if agents_dir is None or not agents_dir.is_dir():
            continue
        for child in sorted(agents_dir.iterdir()):
            if not child.is_dir():
                continue
            if (child / AGENT_FILE).is_file():
                found[child.name] = (child, source)   # 高优先级在后写入,覆盖低优先级
    return found


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
      5. `<git根>/.qi/skills`          qi 项目私有
      6. project settings 的 `skills[]` 追加路径(相对 `<git根>/.qi`)

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


def load_data_sources(agent_dir: Path) -> list[DataSource]:
    """data_sources.json 内容解析;是否装载由插件门控(plugins.md,在 registry 层决定)。"""
    f = agent_dir / DATA_SOURCES_FILE
    if not f.is_file():
        return []
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoadError(f"{f}: JSON 解析失败 {exc}") from exc
    out: list[DataSource] = []
    for item in raw.get("dataSources", []):
        ds_id = str(item.get("id", ""))
        if not ds_id:
            raise LoadError(f"{f}: dataSource 缺少 id")
        dsn = str(item.get("dsn", ""))
        if "{env:" not in dsn and dsn:
            raise LoadError(f"{f}: dsn 只允许 {{{{env:XXX}}}} 引用(禁止明文): {dsn}")
        out.append(DataSource(id=ds_id, type=str(item.get("type", "")), dsn=dsn,
                              description=str(item.get("description", ""))))
    return out


def read_mcp_file(f: Path) -> list[McpServerSpec]:
    """解析一份 mcp.json(`{"mcpServers": {…}}`)。**文件必须存在**(调用方先探)。

    agent 私有 / 全局 / 项目三处共用这一个解析器 —— 格式只有一份真相。
    """
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoadError(f"{f}: JSON 解析失败 {exc}") from exc
    servers = raw.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise LoadError(f"{f}: 缺少 mcpServers 对象")
    return [McpServerSpec(name=name, config=cfg) for name, cfg in servers.items()]


def load_private_mcp(agent_dir: Path) -> list[McpServerSpec]:
    """agent 私有 mcp.json:写在自己目录里 → **自动绑定**,只本 agent 可见。"""
    f = agent_dir / MCP_FILE
    return read_mcp_file(f) if f.is_file() else []


def mcp_scope_files(cwd: Path | None = None) -> list[tuple[McpScope, Path]]:
    """全局/项目两处 mcp.json,**低 → 高**(项目同名覆盖全局)。

    - 全局:`~/.qi/agent/mcp.json`(用户级基建)
    - 项目:`<git根>/.qi/mcp.json`(跟项目走,可提交共享)

    两处都只是**声明表**:哪个 agent 真能看到,由 agent.md 的 `mcp_servers` 决定
    (凭证敏感 → 默认无、必须显式声明,见 agent-config.md §8.2)。
    """
    return [("global", paths.global_home() / MCP_FILE),
            ("project", paths.project_home(cwd) / MCP_FILE)]


def load_mcp_scopes(cwd: Path | None = None) -> list[tuple[McpScope, Path, list[McpServerSpec]]]:
    """读全部作用域的声明表。

    **不存在的文件也回**(servers 为空,路径照样给):设置页要能区分"这里没有文件"
    与"文件在但没写 server",否则它只能靠猜。
    """
    return [(scope, f, read_mcp_file(f) if f.is_file() else [])
            for scope, f in mcp_scope_files(cwd)]


def mcp_name_index(cwd: Path | None = None) -> dict[str, McpServerSpec]:
    """全局+项目合成一张按名查的表 —— agent.md 的 `mcp_servers` 拿它解析。"""
    table: dict[str, McpServerSpec] = {}
    for _scope, _f, servers in load_mcp_scopes(cwd):
        for spec in servers:
            table[spec.name] = spec            # 后写覆盖:项目 > 全局
    return table


def resolve_declared_mcp(declared: list[str], table: dict[str, McpServerSpec],
                         origin: Path) -> list[McpServerSpec]:
    """把 `mcp_servers` 的名字解析成 server 定义。

    **未知名报错**,不静默跳过:声明了却指不到东西,就是"这个 agent 以为自己有
    github、其实没有"——那比装载失败难查得多(对齐 tools 的未知名报错)。
    """
    missing = [name for name in declared if name not in table]
    if missing:
        available = ", ".join(sorted(table)) or "(无)"
        raise LoadError(
            f"{origin}: mcp_servers 里的 {missing} 在全局/项目 mcp.json 里都没有"
            f"(可用的:{available})"
        )
    return [table[name] for name in declared]


def scan_plaintext_secrets(agent_dir: Path) -> list[Path]:
    """导入/装载前明文凭证扫描(只扫结构文件,不读技能正文)。"""
    hits: list[Path] = []
    for name in (MCP_FILE, DATA_SOURCES_FILE):
        f = agent_dir / name
        if f.is_file() and _PLAINTEXT_KEY_RE.search(f.read_text(encoding="utf-8")):
            hits.append(f)
    return hits


def load_agent_dir(agent_dir: Path, source: str, catalog_names: set[str],
                   has_data_source_provider: bool = False,
                   ds_types: set[str] | None = None,
                   extra_skills: list[Skill] | None = None,
                   mcp_table: dict[str, McpServerSpec] | None = None) -> AgentUnit:
    """解析单个 agent 目录(装载与 import 共用同一校验器)。

    `extra_skills` 是顶层技能(低优先级):agent 自带技能同名时胜出 —— 越具体越优先。
    `mcp_table` 是全局+项目合并后的 server 表(`mcp_name_index` 给)。传 `None`
    表示调用方**没有作用域上下文**(import 时校验的是源目录,不是"装到哪"):
    那时只校验声明形状,不解析名字。
    """
    entry = agent_dir / AGENT_FILE
    if not entry.is_file():
        raise LoadError(f"{agent_dir}: 缺少 {AGENT_FILE}")

    meta, body = split_frontmatter(entry.read_text(encoding="utf-8"))
    try:
        config = AgentConfig.model_validate(meta)
    except Exception as exc:  # pydantic.ValidationError
        raise LoadError(f"{entry}: frontmatter 校验失败: {exc}") from exc

    if config.name != agent_dir.name:
        raise LoadError(f"{entry}: name {config.name!r} 与目录名 {agent_dir.name!r} 不一致")
    if not config.description.strip():
        raise LoadError(f"{entry}: description 必填(路由信号)")

    # tools 解析与校验
    try:
        tools = config.resolves_tools(catalog_names)
    except ValueError as exc:
        raise LoadError(f"{entry}: {exc}") from exc

    # include:assets 按序拼入 system prompt
    parts = [body]
    for rel in config.include:
        inc = agent_dir / rel
        if not inc.is_file() or agent_dir.resolve() not in inc.resolve().parents:
            raise LoadError(f"{entry}: include {rel!r} 不存在或越界")
        parts.append(inc.read_text(encoding="utf-8"))
    system_prompt = "\n\n".join(p for p in parts if p.strip())

    secrets = scan_plaintext_secrets(agent_dir)
    if secrets:
        raise LoadError(f"{agent_dir}: 检测到疑似明文凭证({[str(s) for s in secrets]});只允许 {{{{env:XXX}}}} 引用")

    data_sources = load_data_sources(agent_dir) if has_data_source_provider else []
    if data_sources:
        supported = ds_types or set()
        for ds in data_sources:
            if ds.type not in supported:
                raise LoadError(f"{entry}: 数据源 {ds.id} 类型 {ds.type!r} 无插件支持")

    # 声明的全局 MCP:按名解析成定义(没有表就不解析 —— 见 docstring)。
    mcp_declared = (resolve_declared_mcp(config.mcp_servers, mcp_table, entry)
                    if mcp_table is not None else [])

    unit = AgentUnit(
        config=config,
        source=source,
        path=agent_dir,
        system_prompt=system_prompt,
        skills=_merge_skills(extra_skills, scan_skills(agent_dir)),
        data_sources=data_sources,
        mcp_private=load_private_mcp(agent_dir),
        mcp_declared=mcp_declared,
        tools=tools,
    )
    return unit


def _merge_skills(extra: list[Skill] | None, own: list[Skill]) -> list[Skill]:
    """合并技能:顶层技能(低)打底,agent 自带(高)覆盖同名。"""
    merged: dict[str, Skill] = {s.name: s for s in (extra or [])}
    for skill in own:
        merged[skill.name] = skill
    return list(merged.values())


def load_all_agents(cwd: Path | None = None, catalog_names: set[str] | None = None,
                    has_data_source_provider: bool = False,
                    ds_types: set[str] | None = None,
                    extra_skills: list[Skill] | None = None) -> dict[str, AgentUnit]:
    """装载全部 agent(项目版已覆盖用户版)。坏 agent 抛 LoadError(启动报错)。"""
    if catalog_names is None:
        catalog_names = set()
    mcp_table = mcp_name_index(cwd)
    units: dict[str, AgentUnit] = {}
    for name, (agent_dir, source) in scan_agent_dirs(cwd).items():
        units[name] = load_agent_dir(agent_dir, source, catalog_names,
                                     has_data_source_provider, ds_types,
                                     extra_skills=extra_skills,
                                     mcp_table=mcp_table)
    return units


# ── 基座系统提示词(SYSTEM.md 覆盖) ─────────────────
#
# 层级(高 → 低):
#     1. <项目>/.qi/SYSTEM.md      # 跟项目走,可提交共享
#     2. ~/.qi/agent/SYSTEM.md     # 全局
#     3. (无覆盖)                  # 用代码内默认基座:qi_agent/system_prompt.py
#
# 语义(对齐 pi 的 SYSTEM.md / customPrompt):SYSTEM.md **整体替换**默认基座。
# qi 仍保留自己的一层区分:**角色层**(agent.md 正文 + 技能清单 + 数据源)、
# 项目上下文、工作目录始终追加 —— 多 agent 语义不变:换 agent = 换角色层。
# 副作用见 docs/system-prompt.md(自定义基座会丢掉默认的「可用工具 / 指南」)。


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
# 与 pi 的两点差异:祖先链止于 **git 根**(qi 的 project_context_ancestors 约定,
# 与 .agents/skills 的继承范围一致;pi 一路走到文件系统根),空文件视为未配置。

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
    for ancestor in reversed(paths.project_context_ancestors(cwd)):   # 远 → 近
        take(ancestor)
    return out
