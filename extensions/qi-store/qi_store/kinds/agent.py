"""Agent:hikqin 的**智能体详情** → qi 的**角色目录**。

上游没有"给我一个 qi 能用的包"这种接口:离线包(`/api/agents/{id}/download`)是 Agent Plugins
那份规范(技能按目录、MCP 走规范子集),和 qi-agents 的角色目录**不是一回事**;模板蓝图又是
另一类东西(管理员维护的蓝本)。

所以这里**自己封装**:先取智能体详情,再按需把它的每个资源取成原始形状,拼成:

    agents/<名>/agent.md          frontmatter(name/description/tools/model)+ 正文=提示词
    agents/<名>/mcp.json          角色私有 MCP 声明(qi-agents 读,qi-mcp 连)
    agents/<名>/skills/<名>/…     角色私有技能(从技能 zip 里取)

数据来源(每个都只读):

| 要什么 | 从哪取 | 备注 |
| --- | --- | --- |
| 商店目录 | `GET /api/agents` | 你账号能看到的**全部**智能体(草稿与已发布都列) |
| 提示词 / 模型 / 绑定 | `GET /api/agents/{id}` | 模型的 `api_key` 根本不在这里 |
| MCP 细节 | `GET /api/mcp/servers/{mcp_service_id}` | 详情里只有 id 与 `tool_names`,`config` 恒为 `{}` |
| 技能文件 | `GET /api/skills/{skill_id}/download` | 详情里只有 `skill_id`/名字;zip 与 skill kind 同一套校验 |
| 子智能体 | `GET /api/agents/{child_agent_id}` | 详情里只有 id/名字 → 递归取它自己的详情 |

四件**必须显式处理**的事:

1. **`tools:` 不写就没有 MCP**(qi-agents 的语义),而角色私有 server 只能 `mcp__<名>__*` 直连。
   有 MCP 时必须写 `tools:`,而写了就是"点名的才有" → 内置编码工具一起列上;没有 MCP 就
   **不写**(继承父,零改动)。
2. **`tool_names` 是 None 还是列表**含义不同:`None` = 全部工具(→ 不写 `includeTools`),
   列表 = 白名单(→ 写 `includeTools`)。
3. **模型只在本地 `models.json` 里真有这个 `provider/model_name` 时才写**。上游的密钥不在这里,
   我们也不该碰。
4. **子智能体是兄弟不是孩子**:详情里的 `sub_agents[]` 只有 id/名字,所以要各自再取一次详情;
   同名就跳过(静默互盖的症状是"装了 A 跑成 B"),并按 id 防环。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..actions import Planned, WriteBytes, WriteText
from ..catalog import StoreItem, content_sig
from ..client import StoreClient
from ..errors import NotFound, UsageError
from . import mcp as mcp_kind
from . import skill as skill_kind
from .base import PlanContext, check_name

KIND = "agent"
PAGE_SIZE = 50
MAX_PAGES = 20
#: 递归取子智能体详情的深度上限(上游自己也只允许两层,这里防环 + 防跑飞)
MAX_DEPTH = 5

#: 详情里 qi 没有对应物的段落 —— 装之前就说清,别让人装完才发现少了东西
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
        data = client.get("/agents", {"page": page, "page_size": PAGE_SIZE})
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
    """列表页的一条 → 目录条目。**商店的 Agent 就是 `GET /api/agents`**。

    草稿与已发布都列(`publish_status` 进 badges):商店列的是"你账号能看到的智能体",
    是否已发布是运营信息,不是可见性。
    """
    name = str(raw.get("name") or "")
    badge = {
        "publish": raw.get("publish_status"),
        "status": raw.get("status"),
        "model": raw.get("model_name"),
        "skills": raw.get("skills_count"),
        "mcp": raw.get("mcp_services_count"),
        "agents": raw.get("subagents_count") or raw.get("sub_agents_count"),
    }
    return StoreItem(
        id=f"agent:{raw.get('id')}",
        kind=KIND,
        name=name,
        description=str(raw.get("description") or ""),
        # **结构指纹**(不含内容):目录页拿不到内容,为算版本把每个 agent 都取一遍详情太重。
        # 代价写在 README 的已知限制里 —— agent 的"可升级"只在结构调整时出现。
        version=content_sig(name, raw.get("agent_type"), raw.get("model_name"),
                            raw.get("created_at"), badge["skills"], badge["mcp"],
                            badge["agents"]),
        updated_at=str(raw.get("created_at") or ""),
        requires_config=bool(raw.get("model_name") or raw.get("mcp_services_count")),
        badges=badge,
        meta={"agent_id": raw.get("id")},
    )


def fetch_detail(agent_id: str, ctx: PlanContext) -> dict:
    """取一个智能体的详情(含 system_prompt / model / mcp_services / skill_bindings / sub_agents)。"""
    client = ctx.require_client()
    detail = client.get(f"/agents/{agent_id}")
    if not isinstance(detail, dict):
        raise UsageError(f"智能体 {agent_id} 的详情不是对象(上游格式变了?)")
    if not str(detail.get("system_prompt") or "").strip():
        raise UsageError(f"智能体 {detail.get('name') or agent_id} 没有 system_prompt "
                         "—— 装出来是个空角色")
    return detail


# ── 本地模型表(决定要不要写 `model:`)──────────────────────


# ── 转换 ────────────────────────────────────────────────


def _frontmatter(fields: dict[str, str]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _unescape_newlines(prompt: str) -> tuple[str, bool]:
    """把**字面**的反斜杠-n 还原成换行 —— 只在"整段没有真换行"时动手。

    现场:上游有些智能体的 `system_prompt` 录入时多转义了一层,导出来是一串两个字符的
    ``\\n``,模型看到的就是一整行带反斜杠的文本。判据收紧成"**一个真换行都没有**":
    只有这样才确定是双转义;真有换行、又夹着字面写法(比如在讲转义字符)的不动。
    """
    literal = "\\" + "n"
    if literal in prompt and "\n" not in prompt:
        return prompt.replace(literal, "\n"), True
    return prompt, False


def _safe_rel(rel: str) -> Path:
    """技能包里的相对路径:不许绝对路径、不许 `..`。"""
    path = Path(str(rel).replace("\\", "/"))
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise UsageError(f"技能包里的路径越界:{rel}")
    return path


def _mcp_entry(binding: dict, ctx: PlanContext, *, needs: list[str],
               notes: list[str]) -> tuple[str, dict] | None:
    """一条 `mcp_services[]` 绑定 → `(server 名, qi-mcp 的条目)`。

    详情里只有 `mcp_service_id` / `tool_names`(`config` 恒为 `{}`),所以细节要回 server 端点取。
    """
    server_id = binding.get("mcp_service_id")
    name = str(binding.get("mcp_service_name") or "").strip()
    if server_id is None:
        notes.append(f"跳过一个没有 id 的 MCP 绑定({name or '无名'})")
        return None
    try:
        server = ctx.require_client().get(f"/mcp/servers/{server_id}")
    except Exception as exc:  # noqa: BLE001
        # 一个 server 取不到不该让整个角色装不了:记一笔,不写这个条目
        notes.append(f"跳过 MCP `{name or server_id}`:详情取不到({exc})")
        return None
    if not isinstance(server, dict):
        notes.append(f"跳过 MCP `{name or server_id}`:详情不是对象")
        return None
    server = dict(server, name=name or server.get("name") or f"mcp-{server_id}")
    entry, server_needs = mcp_kind.build_entry(server)
    tool_names = binding.get("tool_names")
    if isinstance(tool_names, list) and tool_names:
        # hikqin:None = 全部工具;非空列表 = 白名单 → qi-mcp 的 includeTools
        entry["includeTools"] = [str(t) for t in tool_names if str(t).strip()]
    for env in server_needs:
        if env not in needs:
            needs.append(env)
    return str(server["name"]), entry


def _skill_actions(binding: dict, role_dir: Path, ctx: PlanContext, *,
                   notes: list[str]) -> list:
    """一条 `skill_bindings[]` → 把技能 zip 解成 `<角色>/skills/<名>/`。"""
    skill_id = binding.get("skill_id")
    label = str(binding.get("name") or "").strip() or f"skill-{skill_id}"
    if skill_id is None:
        notes.append(f"跳过一个没有 id 的技能绑定({label})")
        return []
    try:
        data = ctx.require_client().get_bytes(f"/skills/{skill_id}/download")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"跳过技能 `{label}`:下载失败({exc})")
        return []
    try:
        front, payload = skill_kind.zip_payload(data, name=label)
    except UsageError as exc:
        notes.append(f"跳过技能 `{label}`:{exc.message}")
        return []

    declared = str(front.get("name") or "").strip()
    skill_name = check_name(declared or label, "技能")
    skill_dir = role_dir / "skills" / skill_name
    return [WriteBytes(skill_dir / _safe_rel(rel), content) for rel, content in payload.items()]


def _role_actions(detail: dict, role_dir: Path, name: str, *, ctx: PlanContext,
                  depth: int, seen: set[str], taken: set[str],
                  notes: list[str], needs: list[str]) -> list:
    prompt = str(detail.get("system_prompt") or "").strip()
    if not prompt:
        raise UsageError(f"角色 `{name}` 没有 system_prompt —— 装出来是个空角色")

    actions: list = []
    description = str(detail.get("description") or "").strip()
    if not description:
        description = "从 hikqin 导入的智能体"
        notes.append(f"{name}:上游没有 description,已用兜底描述(模型靠它挑角色)")

    # ── 私有 MCP(详情里只有 id/工具白名单,细节回 server 端点取)──
    servers: dict[str, dict] = {}
    for binding in detail.get("mcp_services") or []:
        if not isinstance(binding, dict):
            continue
        built = _mcp_entry(binding, ctx, needs=needs, notes=notes)
        if built is not None:
            servers[built[0]] = built[1]

    # ── 私有技能(详情里只有 id/名字,文件从技能 zip 取)──
    for binding in detail.get("skill_bindings") or []:
        if not isinstance(binding, dict):
            continue
        actions.extend(_skill_actions(binding, role_dir, ctx, notes=notes))

    # ── 子智能体:详情里只有 id/名字 → 各自再取详情,平铺成同级角色 ──
    children: list[dict] = []
    for link in detail.get("sub_agents") or detail.get("subagents") or []:
        if not isinstance(link, dict):
            continue
        child_id = link.get("child_agent_id") or link.get("id")
        if not child_id:
            continue
        if str(child_id) in seen:
            notes.append(f"{name}:跳过子智能体 {link.get('name') or child_id}(成环)")
            continue
        if depth >= MAX_DEPTH:
            notes.append(f"{name}:跳过子智能体 {link.get('name') or child_id}(超过 {MAX_DEPTH} 层)")
            continue
        try:
            child = fetch_detail(str(child_id), ctx)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{name}:跳过子智能体 {link.get('name') or child_id}(取不到详情:{exc})")
            continue
        children.append(child)

    for child in children:
        child_name = str(child.get("name") or "").strip()
        try:
            child_name = check_name(child_name, "子智能体")
        except UsageError as exc:
            notes.append(f"{name}:跳过子智能体({exc.message})")
            continue
        if child_name in taken:
            # 静默覆盖的后果是"装了 A 跑成 B" —— 宁可少装一个并说出来
            notes.append(f"{name}:子智能体 {child_name} 与已有角色同名,已跳过")
            continue
        taken.add(child_name)
        seen.add(str(child.get("id") or child_name))
        actions.extend(_role_actions(child, role_dir.parent / child_name, child_name,
                                     ctx=ctx, depth=depth + 1, seen=seen, taken=taken,
                                     notes=notes, needs=needs))

    # ── agent.md ──
    prompt, fixed = _unescape_newlines(prompt)
    if fixed:
        notes.append(f"{name}:上游的 system_prompt 里是字面反斜杠-n,已还原成换行")

    # **只写 name + description**（与 hikqin 导出的包同一口径）:
    # - 不写 model:角色跟随会话/父角色的模型
    # - 不写 tools:省略 = 继承父;角色私有 MCP 由 qi-agents 自动接上
    fields: dict[str, str] = {"name": name, "description": description}
    if servers:
        actions.append(WriteText(role_dir / "mcp.json",
                                 json.dumps({"mcpServers": servers}, ensure_ascii=False,
                                            indent=2) + "\n"))

    actions.append(WriteText(role_dir / "agent.md", _frontmatter(fields) + "\n" + prompt + "\n"))
    return actions


def _dropped(detail: dict) -> list[str]:
    """哪些段落没有对应物(只在真的非空时报,按标签归并免得刷屏)。"""
    counts: dict[str, int] = {}
    for key, label in DROPPED_SECTIONS.items():
        value = detail.get(key)
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
    agent_id = item.meta.get("agent_id")
    if not agent_id:
        raise NotFound(f"{item.id} 缺少上游 id,装不了")
    detail = fetch_detail(str(agent_id), ctx)
    name = check_name(ctx.name_override or str(detail.get("name") or item.name), "角色")
    role_dir = ctx.root / "agents" / name

    notes: list[str] = []
    needs: list[str] = []
    taken = {name}
    seen = {str(agent_id)}
    actions = _role_actions(detail, role_dir, name, ctx=ctx, depth=0, seen=seen,
                            taken=taken, notes=notes, needs=needs)

    dropped = _dropped(detail)
    if dropped:
        notes.append("已丢弃(qi 没有对应物):" + "、".join(dropped))

    sub_count = len(taken) - 1
    summary = "角色目录 agent.md"
    if sub_count:
        summary += f" + {sub_count} 个同级子角色"
    if any(isinstance(action, WriteText) and action.target.name == "mcp.json"
           for action in actions):
        summary += " + 私有 mcp.json"
    skills = {action.target.parent.name for action in actions
              if isinstance(action, WriteBytes)}
    if skills:
        summary += f" + {len(skills)} 个私有技能"

    return Planned(item=item, target=role_dir, actions=actions,
                   needs_config=needs, note=" · ".join([summary, *notes]))
