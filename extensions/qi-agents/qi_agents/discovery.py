"""角色发现(P-E4c 从 core 移出的那部分)。

core 现在只认识运行单元 `{name, prompt, tools}`;「角色」= 一个目录里的一份说明:

    agents/<名字>/agent.md      frontmatter(name / description / tools / model)+ 正文即 system prompt

三层本地来源(优先级从低到高):

    qi_agents/builtin/agents/  包内置(随 wheel 发布,零配置就能选)
    ~/.qi/agent/agents/        用户级(跨项目)
    <项目>/.qi/agents/         项目级(跟仓库走)→ 同名静默覆盖用户级/内置

`answer_scope` 决定看哪一层:默认只 `user` ——**项目级角色是仓库控制的提示词**,
默认不参与(对齐 pi subagent 的安全模型:要用项目角色得显式 `agentScope: "project"`,
而且宿主会先问一句信任)。内置层不属于项目,不受信任闸门影响;用户可以写同名角色覆盖它。

**第三类角色:远端 A2A 智能体。** 装了 qi-a2a 且它的配置里有**固定 tenant** 的条目时,
那些条目也会出现在角色表里(`source="a2a"`,`remote` 上挂着 qi-a2a 给的句柄)。
它们不是本地提示词,而是一次远端对话 —— 委派给它们的语义与本地子运行一致
(独立上下文、跑完就结束),所以放在同一张表里由模型按 `description` 挑。
合并时**本地优先**:同名时远端不会顶掉你自己写的角色。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

AgentScope = Literal["user", "project", "both"]
DEFAULT_SCOPE: AgentScope = "user"


@dataclass(frozen=True)
class SuggestionGroup:
    """一级「推荐问题」分组（`opening.suggestions` 的一项）。

    分组只负责**组织展示**，不改变点选语义：二级里的每一条仍然是一条完整的
    user 消息。`title` 为空表示一组**全域基线**建议（不占一级分类位，会追加到
    任意已选标题下）；有标题的组才是可点击展开的一级。
    """

    title: str = ""
    items: tuple[str, ...] = ()
    description: str = ""

    def as_dict(self) -> dict:
        return {"title": self.title, "description": self.description,
                "items": list(self.items)}


@dataclass(frozen=True)
class Opening:
    """角色的开场白与 UI 快捷提问(`agent.md` 的 `opening`)。

    `message` 是开场白正文；`suggestions` 是**选择角色时**就能展示的快捷提问，
    点选后才作为一条 user 消息发出去（不进开场白本身）。

    `suggestions` 永远保留一份**扁平清单**（兼容老宿主/老前端）；两级展示看
    `groups`：一级是分组标题，二级是组内问题。两种写法可以并存。
    """

    message: str = ""
    suggestions: tuple[str, ...] = ()
    groups: tuple[SuggestionGroup, ...] = ()

    def as_dict(self) -> dict:
        return {"message": self.message, "suggestions": list(self.suggestions),
                "groups": [group.as_dict() for group in self.groups]}


@dataclass(frozen=True)
class Role:
    """一个角色:名字 + 用途(供模型选)+ 提示词 + 可选的工具/模型限制。"""

    name: str
    description: str
    prompt: str
    tools: list[str] | None = None      # None = 继承父(不是"全部":那会提权)
    disallowed_tools: list[str] | None = None   # 从上面的结果里**减掉**的(支持 fnmatch 通配)
    model: str | None = None            # "provider/model";None = 继承父
    #: 能否**再把自己的任务往下派**(分层委派的总开关)。缺省 True —— 拆解给多个
    #: agent 是这个工具的常态用法;`delegate: false` 才是"到此为止"的显式声明。
    #: 它只是闸之一:深度上限与 spawn 预算(见 subagent.py)对所有角色一视同仁。
    delegate: bool = True
    source: str = "user"                # builtin | user | project | a2a
    path: Path | None = None
    #: `agent.md` 的 `opening`（可选）：开场白 + 选择期快捷提问。
    opening: Opening | None = None
    #: **远端角色**的句柄(`qi_a2a.remote.RemoteAgent`);本地角色是 None。
    #:
    #: 它是 opaque 的:`discovery` 只管"有这么个角色",怎么建连接、怎么发消息全在
    #: qi-a2a 那边 —— 这里多知道一点协议细节,就把两边的版本锁在一起了。
    #: `subagent._run_one` 只看"有没有这个句柄"来分流。
    remote: object | None = None

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "source": self.source,
                "tools": self.tools, "disallowed_tools": self.disallowed_tools,
                "model": self.model, "delegate": self.delegate,
                "opening": self.opening.as_dict() if self.opening else None,
                "remote": self.remote is not None,
                "path": str(self.path or "")}


def _parse_tools(value: object) -> list[str] | None:
    """frontmatter 的 `tools` 两种写法都认:`read, bash`(字符串)与 `[read, bash]`(列表)。

    别的形状(数字/映射)返回 None 而不是抛 —— 这一层在**发现期**跑,一个坏文件不该
    让同目录其它角色一起失效(与 core 的装载校验器不同:那是落盘前的严格校验)。
    """
    raw = value if isinstance(value, list) else (
        str(value).split(",") if isinstance(value, str) else [])
    tools = [str(t).strip() for t in raw if str(t).strip()]
    return tools or None


def _parse_bool(value: object, default: bool) -> bool:
    """frontmatter 的布尔字段(现在只有 `delegate`):认 YAML bool 与常见字符串写法。

    别的形状(数字/映射)回 default 而不是抛 —— 与 `_parse_tools` 同一条理由:
    发现期不该让一个坏文件把整层角色拖下水。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "on", "1"):
            return True
        if text in ("false", "no", "off", "0"):
            return False
    return default


def _parse_suggestions(raw: object) -> tuple[tuple[str, ...], tuple[SuggestionGroup, ...]]:
    """把 `suggestions` 解析成 `(扁平清单, 二级分组)`。

    兼容两种写法：

        suggestions: ["问题一", "问题二"]                    # 旧:平铺；也等于纯全域
        suggestions:
          - "全域通用问题"                                   # 新:无标题 = 全域基线
          - title: 联合岛屿作战                              # 新:一级样式
            description: 可跳过，直接提问即全域问答
            items: ["问题一", "问题二"]
          - title: 联合海上机动作战
            items: ["问题三"]

    **不要**把“全域”写成 `title`：全域是“全部一级标题都选中”的合集语义。
    顶层散落的字符串不会被丢掉：有显式分组时，它们收进一个**无标题**的基线
    分组，追加到任意已选标题下。
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return (), ()

    flat: list[str] = []
    groups: list[SuggestionGroup] = []
    loose: list[str] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                flat.append(text)
                loose.append(text)
            continue
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or item.get("label") or "").strip()
        description = str(item.get("description") or item.get("hint") or "").strip()
        values = item.get("items", item.get("suggestions"))
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            values = []
        items = tuple(str(value).strip() for value in values
                      if value is not None and str(value).strip())
        if not title and not items:
            continue
        groups.append(SuggestionGroup(title=title, items=items, description=description))
        flat.extend(items)

    # 旧写法是平铺时 `groups` 为空，让前端继续用单行胶囊；只有“真的分组了”
    # 或新旧混写时才生成一级。混写里的散落项收进无标题分组，绝不静默丢弃。
    if groups and loose:
        groups.append(SuggestionGroup(items=tuple(loose)))
    return tuple(flat), tuple(groups)


def _parse_opening(value: object) -> Opening | None:
    """解析 `opening`：`{message, suggestions}`（也容忍直接给一条字符串）。

    `suggestions` 既可以是字符串数组，也可以是 `[{title, items}]` 的两级结构。
    发现期不抛：写坏一个字段只让这一块为空，不影响角色本身被列出来。
    """
    if isinstance(value, str):
        message = value.strip()
        return Opening(message=message) if message else None
    if not isinstance(value, dict):
        return None
    message = str(value.get("message") or "").strip()
    suggestions, groups = _parse_suggestions(value.get("suggestions"))
    if not message and not suggestions:
        return None
    return Opening(message=message, suggestions=suggestions, groups=groups)


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
                delegate=_parse_bool(meta.get("delegate"), True),
                opening=_parse_opening(meta.get("opening")),
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


def _builtin_agents_dir() -> Path:
    """随 qi-agents 包发布的内置角色目录（`qi_agents/builtin/agents/`）。

    用普通 `Path` 而不是 `importlib.resources`：wheel 安装后包是解压到磁盘的，
    `discover()` 又要把路径交给 `_load_dir` / `AgentInfo.path`，这里保持文件系统语义。
    """
    return Path(__file__).resolve().parent / "builtin" / "agents"


def _global_agents_dir() -> Path:
    from qi_agent import paths

    return paths.global_home() / "agents"


def _project_agents_dir(cwd: Path | None) -> Path | None:
    """项目角色目录。靠 core 的 `project_home`(它以 `.git` 为锚),所以与 core 的口径一致。"""
    from qi_agent import paths

    home = paths.project_home(cwd)
    return home / "agents" if home.is_dir() else None


def discover(cwd: Path | None = None, scope: AgentScope = DEFAULT_SCOPE, *,
             trusted: bool = True, include_remote: bool = True,
             include_builtin: bool = True) -> dict[str, Role]:
    """按作用域汇总角色:`{名字: Role}`。**本地同名覆盖远端**,项目覆盖用户/内置。

    `include_remote` 把 qi-a2a 里**固定 tenant** 的条目也当成角色(见 `qi_a2a.remote`)。
    没装 qi-a2a / 一份条目都没有 → 空表,不影响本地角色。
    `include_builtin=False` 只用于“只想看用户/项目这两层”的诊断/测试：
    内置角色是随包发布的只读内容,不归任何一层用户目录管。

    合并次序(**后者赢**):远端 → 内置 → 用户级 → 项目级。本地优先是有意的:
    远端是别人家的,同名时不该把你自己写的 `scout` 顶掉;内置同理,用户/项目同名
    角色可以覆盖它,而不是相反。
    """
    if scope not in ("user", "project", "both"):
        scope = DEFAULT_SCOPE
    remote = discover_remote(cwd, scope, trusted=trusted) if include_remote else {}
    builtin = _load_dir(_builtin_agents_dir(), "builtin") if include_builtin else {}
    user = _load_dir(_global_agents_dir(), "user") if scope in ("user", "both") else {}
    project_dir = _project_agents_dir(cwd) if scope in ("project", "both") else None
    project = _load_dir(project_dir, "project") if project_dir else {}
    return {**remote, **builtin, **user, **project}   # 项目 > 用户 > 内置 > 远端


def discover_remote(cwd: Path | None = None, scope: AgentScope = DEFAULT_SCOPE, *,
                    trusted: bool = True) -> dict[str, Role]:
    """qi-a2a 的**远端角色**(固定 tenant 的条目)。

    没装 qi-a2a、或它没有可用条目 → `{}`。**降级不报错**:角色发现跑在每次
    `subagent` 调用上,一个可选的扩展不在不该把本地委派一起拦下来。

    层与 `agentScope` 的对齐在 `qi_a2a.remote` 里(全局层 = user,项目层 = project),
    项目层的信任门控也由它把关 —— 这里不另设一道。
    """
    try:
        from qi_a2a.remote import remote_agents
    except ImportError:                 # 没装 qi-a2a
        return {}
    found = remote_agents(cwd, trusted=trusted, scope=scope)
    return {
        agent.name: Role(name=agent.name, description=agent.description, prompt="",
                         source="a2a", remote=agent)
        for agent in found
    }


def project_roles(cwd: Path | None, names: list[str]) -> list[Role]:
    """这些名字里,哪些来自**项目**目录(需要先过信任确认)。

    `discover(cwd, "project")` 现在也会带出内置层(它是所有作用域的兜底),所以这里
    必须再按 `source` 过滤一次 —— 内置角色不是仓库控制的提示词。
    """
    found = discover(cwd, "project", include_remote=False)
    return [found[n] for n in names
            if n in found and found[n].source == "project"]


def format_roles(roles: dict[str, Role], max_items: int = 12) -> str:
    """给模型看的一行清单(`name (source): description`)。"""
    if not roles:
        return "(无角色)"
    listed = sorted(roles.values(), key=lambda r: r.name)[:max_items]
    text = "; ".join(f"{r.name} ({r.source}): {r.description}" for r in listed)
    rest = len(roles) - len(listed)
    return f"{text}(还有 {rest} 个未列出)" if rest > 0 else text
