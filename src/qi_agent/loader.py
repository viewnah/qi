"""内容装载器(P2):agent 目录发现(两层)、agent.md/技能解析、校验、import。

对齐 agent-config.md §3/§6/§10:
- 位置: ~/.qi/agents + <项目>/.qi/agents;项目静默覆盖全局;同层重复报错
- 校验: name==目录名 / description 非空 / tools 存在 / include 存在 / 技能同名冲突
- import: 与装载共用同一校验器;明文凭证扫描
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from . import paths
from .models import AgentConfig, AgentUnit, DataSource, McpServerSpec, Skill

AGENT_FILE = "agent.md"
SKILL_FILE = "SKILL.md"
ASSETS_DIR = "assets"
SKILLS_DIR = "skills"
MCP_FILE = "mcp.json"
DATA_SOURCES_FILE = "data_sources.json"

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


def scan_agent_dirs(cwd: Path | None = None) -> dict[str, tuple[Path, str]]:
    """收集 {name: (dir, source)}:项目版覆盖用户版(静默);同层重复由文件系统保证不存在。"""
    found: dict[str, tuple[Path, str]] = {}
    for qi_home, source in (
        (paths.project_home(cwd), "project"),
        (paths.global_home(), "user"),
    ):
        agents_dir = qi_home / "agents"
        if not agents_dir.is_dir():
            continue
        for child in sorted(agents_dir.iterdir()):
            if not child.is_dir():
                continue
            if (child / AGENT_FILE).is_file():
                found[child.name] = (child, source)   # 高优先级在后写入,覆盖低优先级
    return found


def scan_skills(agent_dir: Path) -> list[Skill]:
    skills: dict[str, Skill] = {}
    skills_root = agent_dir / SKILLS_DIR
    if skills_root.is_dir():
        for skill_dir in sorted(skills_root.iterdir()):
            skill_file = skill_dir / SKILL_FILE
            if not skill_file.is_file():
                continue
            meta, body = split_frontmatter(skill_file.read_text(encoding="utf-8"))
            name = meta.get("name") or skill_dir.name
            desc = str(meta.get("description", "")).strip()
            if name in skills:
                raise LoadError(f"{skill_file}:技能 {name!r} 在 {agent_dir} 内重复")
            skills[name] = Skill(name=name, description=desc or "(无描述)", path=skill_file)
    return list(skills.values())


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


def load_private_mcp(agent_dir: Path) -> list[McpServerSpec]:
    f = agent_dir / MCP_FILE
    if not f.is_file():
        return []
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoadError(f"{f}: JSON 解析失败 {exc}") from exc
    servers = raw.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise LoadError(f"{f}: 缺少 mcpServers 对象")
    return [McpServerSpec(name=name, config=cfg) for name, cfg in servers.items()]


def scan_plaintext_secrets(agent_dir: Path) -> list[Path]:
    """导入/装载前明文凭证扫描(只扫结构文件,不读技能正文)。"""
    hits: list[Path] = []
    for name in (MCP_FILE, DATA_SOURCES_FILE):
        f = agent_dir / name
        if f.is_file() and _PLAINTEXT_KEY_RE.search(f.read_text(encoding="utf-8")):
            hits.append(f)
    return hits


def load_agent_dir(agent_dir: Path, source: str, catalog_names: set[str],
                   has_data_source_provider: bool = False) -> AgentUnit:
    """解析单个 agent 目录(装载与 import 共用同一校验器)。"""
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
        from .registry import provider_types
        for ds in data_sources:
            if ds.type not in provider_types("data_sources"):
                raise LoadError(f"{entry}: 数据源 {ds.id} 类型 {ds.type!r} 无插件支持")

    unit = AgentUnit(
        config=config,
        source=source,
        path=agent_dir,
        system_prompt=system_prompt,
        skills=scan_skills(agent_dir),
        data_sources=data_sources,
        mcp_private=load_private_mcp(agent_dir),
        tools=tools,
    )
    return unit


def load_all_agents(cwd: Path | None = None, catalog_names: set[str] | None = None,
                    has_data_source_provider: bool = False) -> dict[str, AgentUnit]:
    """装载全部 agent(项目版已覆盖用户版)。坏 agent 抛 LoadError(启动报错)。"""
    if catalog_names is None:
        catalog_names = set()
    units: dict[str, AgentUnit] = {}
    for name, (agent_dir, source) in scan_agent_dirs(cwd).items():
        units[name] = load_agent_dir(agent_dir, source, catalog_names, has_data_source_provider)
    return units
