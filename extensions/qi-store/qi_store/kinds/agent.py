"""Agent:hikqin 的**模板蓝图** → qi 的**角色目录**。

两边的形状差得比别的 kind 多,但目标很明确:qi-agents 的"一个目录就是一个自包含包"

    agents/<名>/agent.md          frontmatter(name / description / tools / model)+ 正文=提示词
    agents/<名>/mcp.json          角色私有 MCP 声明(qi-agents 自己读,负责连的是 qi-mcp)
    agents/<名>/skills/<名>/SKILL.md

| hikqin 蓝图(`format_version: 1`) | qi 角色目录 | 处理 |
| --- | --- | --- |
| `agent.name` / `description` | frontmatter `name` / `description` | 名字长度与路径字符做校验 |
| `agent.system_prompt` | agent.md 正文 | 空 → 装不了(角色没有正文等于没有角色) |
| `agent.skills[].files`(base64 内联) | `<角色>/skills/<名>/…` | 校验有 `SKILL.md` + `description`(qi 的硬规则) |
| `agent.mcp_services[]` | `<角色>/mcp.json` | 复用 mcp kind 的转换;`tool_names` → `includeTools` |
| `agent.sub_agents[]` | **同级**角色目录 | qi 没有嵌套,一层层摊平 |
| `agent.model` | frontmatter `model:` | **只在本地 `models.json` 里真有这个 provider/model 时才写** |
| `opening_*` / `data_sources` / `knowledge_bases` / `config` | — | qi 没有对应物 → 丢弃并在计划里说出来 |

三处**必须显式处理**的坑:

1. **`tools:` 不写就没有 MCP**。qi-agents 的语义是"什么都不写 = 没有任何 MCP 访问,
   而且连都不连";而角色私有 server 只能靠 `mcp__<名>__*` 直连(`mcp` 代理看不见它们)。
   所以蓝图里有 MCP 时,我们**必须**写 `tools:`,而写了就等于"内置工具也要点名" ——
   于是按 `DEFAULT_TOOLS` 一起写上(这是**收窄**了角色权限,不是提权,方向是对的)。
   没有 MCP 时**不写** `tools:`(= 继承父,零改动)。
2. **`api_key` / `auth_config` 是剥离状态**。上游只给"需要配置"这个事实,所以装了之后
   要么本地已有同名 provider,要么用户自己补 —— 用 `needs_config` 说出来。
3. **子智能体是兄弟不是孩子**。蓝图是两层树,qi 的角色表是平铺的;同名的子角色跳过并说明
   (静默覆盖会让人"装了 A 却跑成 B")。
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

from ..actions import Planned, WriteBytes, WriteText
from ..catalog import StoreItem, content_sig
from ..client import StoreClient
from ..errors import NotFound, UsageError
from . import mcp as mcp_kind
from .base import PlanContext, check_name, read_frontmatter

KIND = "agent"
PAGE_SIZE = 50
MAX_PAGES = 20

#: 生成的角色在**有 MCP 时**写进 `tools:` 的内置工具集。
#:
#: 为什么是这一套:写 `tools:` 就意味着"点名的才有"(qi 的语义),所以一旦为了 MCP 不得不
#: 写它,就得把编码 agent 的常用内置工具一起列上 —— 否则装出来的角色"有 MCP 但没有 read"。
#: `powershell` 在非 Windows 上只是不可用,不会报错(内置工具是无条件注册的)。
DEFAULT_TOOLS = ("read", "ls", "find", "grep", "write", "edit", "bash", "powershell")

#: 蓝图里 qi 没有对应物的段落 —— 装之前就说清,别让人装完才发现少了东西
DROPPED_SECTIONS = {
    "opening_enabled": "开场白",
    "opening_message": "开场白",
    "opening_suggestions": "开场建议",
    "config": "DeepAgents 专有配置(迭代上限 / 记忆 / 摘要)",
    "interrupt_on": "人在环审批",
    "data_sources": "数据源",
    "knowledge_bases": "知识库",
}


def fetch(client: StoreClient) -> list[StoreItem]:
    items: list[StoreItem] = []
    page = 1
    while page <= MAX_PAGES:
        data = client.get("/agent-templates", {"page": page, "page_size": PAGE_SIZE})
        if not isinstance(data, dict):
            break
        batch = data.get("items") or []
        items.extend(_item(raw) for raw in batch if isinstance(raw, dict))
        total = int(data.get("total") or 0)
        if len(batch) < PAGE_SIZE or len(items) >= total:
            break
        page += 1
    return items


def _item(raw: dict) -> StoreItem:
    summary = raw.get("resource_summary") or {}
    name = str(raw.get("name") or "")
    return StoreItem(
        id=f"agent:{raw.get('id')}",
        kind=KIND,
        name=name,
        description=str(raw.get("description") or ""),
        version=content_sig(name, raw.get("updated_at"), summary),
        updated_at=str(raw.get("updated_at") or ""),
        # 蓝图里模型/密钥是剥离状态 → 这两类基本都要用户补
        requires_config=bool(summary.get("model") or summary.get("mcp")),
        badges={"category": raw.get("category"), "usage": raw.get("usage_count"),
                "agents": summary.get("sub_agent"), "skills": summary.get("skill"),
                "mcp": summary.get("mcp"), "model": summary.get("model")},
        meta={"template_id": raw.get("id"), "summary": summary},
    )


# ── 蓝图 ────────────────────────────────────────────────


def fetch_blueprint(item: StoreItem, ctx: PlanContext) -> dict:
    client = ctx.require_client()
    template_id = item.meta.get("template_id")
    if template_id is None:
        raise NotFound(f"{item.id} 缺少模板 id,装不了")
    data = client.get(f"/agent-templates/{template_id}")
    content = (data or {}).get("content") if isinstance(data, dict) else None
    if not isinstance(content, dict):
        raise UsageError(f"模板 {template_id} 没有 content(上游格式变了?)")
    version = content.get("format_version")
    if version != 1:
        # 不猜新格式:猜错的表现是"装出来的角色行为不对",比报错难查得多
        raise UsageError(f"不认识的模板格式:format_version={version}(本扩展只认 1)")
    agent = content.get("agent")
    if not isinstance(agent, dict):
        raise UsageError(f"模板 {template_id} 的 content.agent 不是对象")
    return agent


# ── 本地模型表(决定要不要写 `model:`)──────────────────────


def local_models(root: Path | None = None) -> set[str]:
    """本地 `models.json` 里**能被解析**的 `provider/model`。

    读不到 / 格式不对 → 空集(等于"一个都不写"),不是报错:装一个技能化的角色不该因为
    模型文件坏了而失败;调用方会在计划里说明"模型没写"。
    """
    file = (root or _agent_dir()) / "models.json"
    if not file.is_file():
        return {}
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return set()
    providers = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(providers, dict):
        return set()
    found: set[str] = set()
    for provider, cfg in providers.items():
        models = (cfg or {}).get("models") if isinstance(cfg, dict) else None
        for model in models or []:
            if isinstance(model, dict) and model.get("id"):
                found.add(f"{provider}/{model['id']}")
    return found


def _agent_dir() -> Path:
    from .. import paths

    return paths.global_agent_dir()


def _pick_model(blueprint: dict) -> tuple[str | None, str]:
    model = blueprint.get("model")
    if not isinstance(model, dict):
        return None, ""
    provider = str(model.get("provider") or "").strip()
    name = str(model.get("name") or "").strip()
    if not provider or not name:
        return None, ""
    spec = f"{provider}/{name}"
    if spec in local_models():
        return spec, ""
    # 上游的 `requires_config` 说的正是"api_key 被剥离了" —— 而"本地没有这个模型"是同一个
    # 事实的另一种说法。合成一句,免得两条都报。
    extra = "(上游标记:需要本地凭证)" if model.get("requires_config") else ""
    return None, f"本地 models.json 里没有 {spec}{extra}"


# ── 转换 ────────────────────────────────────────────────


def _frontmatter(fields: dict[str, str]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _role_actions(blueprint: dict, role_dir: Path, name: str, *, is_sub: bool,
                  taken: set[str], notes: list[str],
                  needs: list[str]) -> list:
    """一个角色 → 一批动作。`taken` 是本次已经用掉的目录名(防同名互盖)。"""
    prompt = str(blueprint.get("system_prompt") or "").strip()
    if not prompt:
        raise UsageError(f"角色 `{name}` 没有 system_prompt —— 装出来是个空角色")

    actions: list = []
    description = str(blueprint.get("description") or "").strip()
    if not description:
        description = "从 hikqin 导入的智能体"
        notes.append(f"{name}:上游没有 description,已用兜底描述(模型靠它挑角色)")

    # ── 角色私有 MCP ──
    servers: dict[str, dict] = {}
    for server in blueprint.get("mcp_services") or []:
        if not isinstance(server, dict) or not server.get("name"):
            continue
        entry, server_needs = mcp_kind.build_entry(server)
        tool_names = [str(t) for t in (server.get("tool_names") or []) if str(t).strip()]
        if tool_names:
            # hikqin 的 `tool_names` = 这个 agent 被允许用哪些工具 → qi-mcp 的 includeTools
            entry["includeTools"] = tool_names
        servers[str(server["name"])] = entry
        for env in server_needs:
            if env not in needs:
                needs.append(env)

    # ── 角色私有技能(蓝图里 base64 内联)──
    for skill in blueprint.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        skill_name = str(skill.get("name") or "").strip()
        files = skill.get("files") or {}
        if not skill_name or not isinstance(files, dict) or not files:
            notes.append(f"{name}:跳过一个没有文件的技能({skill_name or '无名'})")
            continue
        if not str(skill.get("description") or "").strip():
            notes.append(f"{name}:跳过技能 {skill_name}(上游没有 description,"
                         "qi 会看不见它)")
            continue
        if "SKILL.md" not in files:
            notes.append(f"{name}:跳过技能 {skill_name}(根上没有 SKILL.md)")
            continue
        skill_dir = role_dir / "skills" / check_name(skill_name, "技能")
        for rel, encoded in files.items():
            try:
                data = base64.b64decode(str(encoded), validate=True)
            except (binascii.Error, ValueError):
                notes.append(f"{name}:技能 {skill_name} 的 {rel} base64 坏了,跳过该文件")
                continue
            actions.append(WriteBytes(skill_dir / _safe_rel(rel), data))

    # ── 子智能体:平铺成同级角色 ──
    children = [child for child in (blueprint.get("sub_agents") or [])
                if isinstance(child, dict)]
    for child in children:
        if not child.get("name"):
            continue
        try:
            child_name = check_name(str(child["name"]), "子智能体")
        except UsageError as exc:
            notes.append(f"{name}:跳过子智能体 {child.get('name')!r}({exc.message})")
            continue
        if child_name in taken:
            # 静默覆盖的后果是"装了 A 跑成 B" —— 宁可少装一个并说出来
            notes.append(f"{name}:子智能体 {child_name} 与已有角色同名,已跳过")
            continue
        taken.add(child_name)
        actions.extend(_role_actions(child, role_dir.parent / child_name, child_name,
                                     is_sub=True, taken=taken, notes=notes, needs=needs))

    # ── agent.md ──
    declared = str(blueprint.get("name") or "").strip()
    if declared and declared != name:
        notes.append(f"{name}:蓝图里的名字是 `{declared}`,落盘用 `{name}`(frontmatter 跟着目录名)")

    fields: dict[str, str] = {"name": name, "description": description}
    model, model_note = _pick_model(blueprint)
    if model:
        fields["model"] = model
    elif model_note:
        notes.append(f"{name}:没写 model —— {model_note}")

    if servers:
        # 有 MCP 就必须写 tools(否则 qi-agents 一行 MCP 工作都不做);
        # 写了就是"点名的才有",所以内置工具也要一起列。
        tools = [*DEFAULT_TOOLS, *(f"mcp__{server}__*" for server in servers)]
        if children:
            tools.append("subagent")
        fields["tools"] = ", ".join(tools)
        notes.append(f"{name}:已写 tools(MCP 需要显式点名;内置工具按默认编码集列出)")
        actions.append(WriteText(role_dir / "mcp.json",
                                 json.dumps({"mcpServers": servers}, ensure_ascii=False,
                                            indent=2) + "\n"))
    elif children:
        notes.append(f"{name}:有子智能体但没有 MCP —— 没写 tools(继承父,含 subagent)")

    actions.append(WriteText(role_dir / "agent.md", _frontmatter(fields) + "\n" + prompt + "\n"))
    return actions


def _safe_rel(rel: str) -> Path:
    """技能包里的相对路径:不许绝对路径、不许 `..`(base64 是上游给的,但仍要防)。"""
    path = Path(str(rel).replace("\\", "/"))
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise UsageError(f"技能包里的路径越界:{rel}")
    return path


def _dropped(blueprint: dict) -> list[str]:
    """哪些段落没有对应物(只在真的非空时报,免得刷屏)。

    按**标签**归并:`opening_enabled` 与 `opening_message` 说的是同一个"开场白",
    不归并的话计划里会出现两次同样的词。
    """
    counts: dict[str, int] = {}
    for key, label in DROPPED_SECTIONS.items():
        value = blueprint.get(key)
        if isinstance(value, (list, dict)):
            if value:
                counts[label] = counts.get(label, 0) + len(value)
        elif isinstance(value, str):
            if value.strip():
                counts.setdefault(label, 0)
        elif value is True:
            counts.setdefault(label, 0)
    return [f"{label}×{count}" if count else label for label, count in counts.items()]


# ── 计划 ────────────────────────────────────────────────


def plan(item: StoreItem, ctx: PlanContext) -> Planned:
    blueprint = fetch_blueprint(item, ctx)
    name = check_name(ctx.name_override or str(blueprint.get("name") or item.name), "角色")
    role_dir = ctx.root / "agents" / name

    notes: list[str] = []
    needs: list[str] = []
    taken = {name}
    actions = _role_actions(blueprint, role_dir, name, is_sub=False,
                            taken=taken, notes=notes, needs=needs)

    dropped = _dropped(blueprint)
    if dropped:
        notes.append("已丢弃(qi 没有对应物):" + "、".join(dropped))

    sub_count = len(taken) - 1
    summary = f"角色目录 agent.md"
    if sub_count:
        summary += f" + {sub_count} 个同级子角色"
    if (role_dir / "mcp.json").exists() or any(
            isinstance(action, WriteText) and action.target.name == "mcp.json"
            for action in actions):
        summary += " + 私有 mcp.json"
    skill_dirs = {action.target.parent.name for action in actions
                  if isinstance(action, WriteBytes)}
    if skill_dirs:
        summary += f" + {len(skill_dirs)} 个私有技能"

    return Planned(item=item, target=role_dir, actions=actions,
                   needs_config=needs, note=" · ".join([summary, *notes]))
