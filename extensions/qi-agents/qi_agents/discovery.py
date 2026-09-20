"""角色发现(P-E4c 从 core 移出的那部分)。

core 现在只认识运行单元 `{name, prompt, tools}`;「角色」= 一个目录里的一份说明:

    agents/<名字>/agent.md      frontmatter(name / description / tools / model)+ 正文即 system prompt

两层来源(与 v1 的规则一致,只是现在住在扩展里):

    ~/.qi/agent/agents/   用户级(跨项目)
    <项目>/.qi/agents/    项目级(跟仓库走)→ 同名静默覆盖用户级

`answer_scope` 决定看哪一层:默认只 `user` ——**项目级角色是仓库控制的提示词**,
默认不参与(对齐 pi subagent 的安全模型:要用项目角色得显式 `agentScope: "project"`,
而且宿主会先问一句信任)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

AgentScope = Literal["user", "project", "both"]
DEFAULT_SCOPE: AgentScope = "user"


@dataclass(frozen=True)
class Role:
    """一个角色:名字 + 用途(供模型选)+ 提示词 + 可选的工具/模型限制。"""

    name: str
    description: str
    prompt: str
    tools: list[str] | None = None      # None = 继承父(不是"全部":那会提权)
    disallowed_tools: list[str] | None = None   # 从上面的结果里**减掉**的(支持 fnmatch 通配)
    model: str | None = None            # "provider/model";None = 继承父
    source: str = "user"                # user | project
    path: Path | None = None

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "source": self.source,
                "tools": self.tools, "disallowed_tools": self.disallowed_tools,
                "model": self.model, "path": str(self.path or "")}


def _parse_tools(value: object) -> list[str] | None:
    """frontmatter 的 `tools` 两种写法都认:`read, bash`(字符串)与 `[read, bash]`(列表)。

    别的形状(数字/映射)返回 None 而不是抛 —— 这一层在**发现期**跑,一个坏文件不该
    让同目录其它角色一起失效(与 core 的装载校验器不同:那是落盘前的严格校验)。
    """
    raw = value if isinstance(value, list) else (
        str(value).split(",") if isinstance(value, str) else [])
    tools = [str(t).strip() for t in raw if str(t).strip()]
    return tools or None


def _read_role(md: Path, source: str) -> Role | None:
    """解析一份 agent.md;不合格(缺 name/description)返回 None。"""
    from qi_agent.loader import split_frontmatter   # 公开面:通用 frontmatter 解析留 core

    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = split_frontmatter(text)
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        return None
    model = meta.get("model")
    return Role(name=name,
                description=" ".join(description.split()),
                prompt=body.strip(),
                tools=_parse_tools(meta.get("tools")),
                disallowed_tools=_parse_tools(meta.get("disallowed_tools")),
                model=str(model).strip() if isinstance(model, str) and model.strip() else None,
                source=source, path=md)


def _load_dir(root: Path, source: str) -> dict[str, Role]:
    out: dict[str, Role] = {}
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        md = child / "agent.md"
        if child.is_dir() and md.is_file():
            role = _read_role(md, source)
            if role is not None:
                out[role.name] = role
    return out


def _global_agents_dir() -> Path:
    from qi_agent import paths

    return paths.global_home() / "agents"


def _project_agents_dir(cwd: Path | None) -> Path | None:
    """项目角色目录。靠 core 的 `project_home`(它以 `.git` 为锚),所以与 core 的口径一致。"""
    from qi_agent import paths

    home = paths.project_home(cwd)
    return home / "agents" if home.is_dir() else None


def discover(cwd: Path | None = None, scope: AgentScope = DEFAULT_SCOPE) -> dict[str, Role]:
    """按作用域汇总角色:`{名字: Role}`。项目级同名覆盖用户级。"""
    if scope not in ("user", "project", "both"):
        scope = DEFAULT_SCOPE
    user = _load_dir(_global_agents_dir(), "user") if scope in ("user", "both") else {}
    project_dir = _project_agents_dir(cwd) if scope in ("project", "both") else None
    project = _load_dir(project_dir, "project") if project_dir else {}
    return {**user, **project}          # 项目覆盖用户


def project_roles(cwd: Path | None, names: list[str]) -> list[Role]:
    """这些名字里,哪些来自**项目**目录(需要先过信任确认)。"""
    found = discover(cwd, "project")
    return [found[n] for n in names if n in found]


def format_roles(roles: dict[str, Role], max_items: int = 12) -> str:
    """给模型看的一行清单(`name (source): description`)。"""
    if not roles:
        return "(无角色)"
    listed = sorted(roles.values(), key=lambda r: r.name)[:max_items]
    text = "; ".join(f"{r.name} ({r.source}): {r.description}" for r in listed)
    rest = len(roles) - len(listed)
    return f"{text}(还有 {rest} 个未列出)" if rest > 0 else text
