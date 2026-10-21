"""Agent:hikqin 的**离线包** → qi 的**角色目录**。

上游 `GET /api/agents/{id}/download` 给的是一个 zip(Agent Plugins 1.0.0):

    agent-<名>/plugin.json                规范清单;hikqin 的私有配置在
                                          extensions["com.hikqin.agent"](format_version: 2)
    agent-<名>/mcp.json                   规范子集(type/url/headers,不含密钥)
    agent-<名>/skills/<名>/SKILL.md …     技能**是真文件**(不是 base64)
    agent-<名>/knowledge_bases/<名>/…     知识库源文件

qi 要的是 qi-agents 的"一个目录一个自包含包":

    agents/<名>/agent.md          frontmatter(name/description/tools/model)+ 正文=提示词
    agents/<名>/mcp.json          角色私有 MCP 声明(qi-agents 读,qi-mcp 连)
    agents/<名>/skills/<名>/…     角色私有技能

| 包里 | 落到 | 处理 |
| --- | --- | --- |
| `system_prompt` | agent.md 正文 | **空 → 直接失败**(角色没有正文等于没有角色) |
| `skills[]`(`package_path` 指目录) | `<角色>/skills/<名>/` | 复制真文件;校验根上有 `SKILL.md` + description 非空(qi 的硬规则) |
| `mcp_services[]`(私有,比 mcp.json 全) | `<角色>/mcp.json` | 复用 mcp kind 的转换;`tool_names` → `includeTools`;stdio 也认(规范 mcp.json 表达不了) |
| `sub_agents[]`(同一份清单里递归) | **同级**角色目录 | qi 没有嵌套;同名就跳过并说明 |
| `model` | frontmatter `model:` | 只在本地 `models.json` 里有这个 `provider/name` 时才写。**`api_key` 是 AES 密文,一个字节都不碰** |
| `opening_*` / `data_sources` / `knowledge_bases` / `config` / `interrupt_on` | — | qi 没有对应物 → 丢弃,并在计划里列出 |

三处**必须显式处理**的坑:

1. **`tools:` 不写就没有 MCP**(qi-agents 的语义),而角色私有 server 只能用 `mcp__<名>__*` 直连。
   所以有 MCP 时必须写 `tools:`,而写了就等于"点名的才有" → 内置编码工具一起列上。
   没有 MCP 就**不写**(继承父,零改动)。
2. **`api_key` 是密文**。上游把它加密导出了,但 qi 没有那把密钥 —— 所以只取
   `provider`/`name` 去本地 `models.json` 里找,找不到就不写 `model:`。
3. **子智能体是兄弟不是孩子**。包里的 `sub_agents[]` 与主清单同构、技能也都摊在同一个
   `skills/` 下(靠 `package_path` 区分),所以按角色逐个复制自己那几份。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from ..actions import Planned, WriteBytes, WriteText
from ..catalog import StoreItem, content_sig
from ..client import StoreClient
from ..errors import NotFound, UsageError
from . import mcp as mcp_kind
from .base import PlanContext, check_name

KIND = "agent"
PAGE_SIZE = 50
MAX_PAGES = 20

#: 包里的私有命名空间(反向域名,Agent Plugins §8)
EXTENSION_KEY = "com.hikqin.agent"
#: 认得的包格式(上游 `PACKAGE_FORMAT_VERSION`)
PACKAGE_FORMAT = 2

#: 生成的角色在**有 MCP 时**写进 `tools:` 的内置工具集。
#:
#: 为什么是这一套:写 `tools:` 就意味着"点名的才有"(qi 的语义),所以一旦为了 MCP 不得不
#: 写它,就得把编码 agent 的常用内置工具一起列上 —— 否则装出来的角色"有 MCP 但没有 read"。
#: `powershell` 在非 Windows 上只是不可用,不会报错(内置工具无条件注册)。
DEFAULT_TOOLS = ("read", "ls", "find", "grep", "write", "edit", "bash", "powershell")

#: 包里 qi 没有对应物的段落 —— 装之前就说清,别让人装完才发现少了东西
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

    不是 `/api/agent-templates`:那是管理员维护的蓝图(数量少、用途是"复制出一个智能体"),
    而"远端有 6 个 agent"说的是这里。草稿与已发布都在(`publish_status` 进 badges):
    商店列的是**你账号能看到的智能体**,是否已发布是运营信息,不是可见性。
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
        # **结构指纹**(不含内容):目录页拿不到内容,且下载 6 个包只为算版本太重。
        # 代价写在 README 的已知限制里 —— agent 的"可升级"只在结构调整时出现。
        version=content_sig(name, raw.get("agent_type"), raw.get("model_name"),
                            raw.get("created_at"), badge["skills"], badge["mcp"],
                            badge["agents"]),
        updated_at=str(raw.get("created_at") or ""),
        requires_config=bool(raw.get("model_name") or raw.get("mcp_services_count")),
        badges=badge,
        meta={"agent_id": raw.get("id")},
    )


# ── 取包 ────────────────────────────────────────────────


def fetch_package(item: StoreItem, ctx: PlanContext) -> tuple[dict, dict[str, bytes]]:
    """下载离线包,返回 `(私有清单, {相对插件根的路径: 内容})`。

    插件根 = `plugin.json` 所在的那一层(包里通常是 `agent-<名>/`)。
    """
    client = ctx.require_client()
    agent_id = item.meta.get("agent_id")
    if not agent_id:
        raise NotFound(f"{item.id} 缺少上游 id,装不了")
    data = client.get_bytes(f"/agents/{agent_id}/download")

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UsageError(f"下载到的不是合法 zip:{exc}") from exc

    manifest_path = next(
        (name for name in archive.namelist()
         if name.count("/") == 1 and name.endswith("/plugin.json")),
        None,
    )
    if manifest_path is None:
        raise UsageError("包里没有 plugin.json(Agent Plugins 规范要求它在插件根)",
                         hint="上游的离线包格式可能变了")
    root = manifest_path.rsplit("/", 1)[0] + "/"

    files: dict[str, bytes] = {}
    for name in archive.namelist():
        if name.endswith("/") or not name.startswith(root):
            continue
        relative = name[len(root):]
        path = Path(relative)
        # 路径必须留在插件根内(上游已过滤过,这里再挡一次)
        if path.is_absolute() or any(part in ("..", "") for part in path.parts):
            raise UsageError(f"包里的路径越界:{relative}")
        files[relative] = archive.read(name)

    try:
        plugin = json.loads(files["plugin.json"].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError(f"plugin.json 读不了:{exc}") from exc

    extensions = plugin.get("extensions") if isinstance(plugin, dict) else None
    manifest = (extensions or {}).get(EXTENSION_KEY) if isinstance(extensions, dict) else None
    if not isinstance(manifest, dict):
        raise UsageError(f"plugin.json 里没有 {EXTENSION_KEY}(不是 hikqin 的包?)")
    version = manifest.get("format_version")
    if version != PACKAGE_FORMAT:
        raise UsageError(f"不认识的离线包格式:format_version={version}"
                         f"(本扩展只认 {PACKAGE_FORMAT})")
    return manifest, files


# ── 本地模型表(决定要不要写 `model:`)──────────────────────


def local_models(root: Path | None = None) -> set[str]:
    """本地 `models.json` 里**能被解析**的 `provider/model`。

    读不到 / 格式不对 → 空集(等于"一个都不写"),不是报错:装一个角色不该因为模型文件坏了
    而失败;调用方会在计划里说明"模型没写"。
    """
    from .. import paths

    file = (root or paths.global_agent_dir()) / "models.json"
    if not file.is_file():
        return set()
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


def _pick_model(manifest: dict) -> tuple[str | None, str]:
    model = manifest.get("model")
    if not isinstance(model, dict):
        return None, ""
    provider = str(model.get("provider") or "").strip()
    name = str(model.get("name") or "").strip()
    if not provider or not name:
        return None, ""
    spec = f"{provider}/{name}"
    if spec in local_models():
        return spec, ""
    # `api_key` 是**密文**,不碰;这里只说"本地没有,你自己配一个同名的"
    return None, f"本地 models.json 里没有 {spec}(上游的 api_key 是加密导出,qi 用不了)"


# ── 转换 ────────────────────────────────────────────────


def _frontmatter(fields: dict[str, str]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _skill_files(skill: dict, files: dict[str, bytes]) -> dict[str, bytes]:
    """一个技能在包里的文件:按 `package_path` 前缀取出来(子智能体的技能也在同一个根下)。"""
    prefix = str(skill.get("package_path") or "").strip().strip("/")
    if not prefix:
        return {}
    head = prefix + "/"
    return {name[len(head):]: data for name, data in files.items()
            if name.startswith(head) and not name.endswith("/")}


def _role_actions(manifest: dict, role_dir: Path, name: str, *, files: dict[str, bytes],
                  taken: set[str], notes: list[str], needs: list[str]) -> list:
    prompt = str(manifest.get("system_prompt") or "").strip()
    if not prompt:
        raise UsageError(f"角色 `{name}` 没有 system_prompt —— 装出来是个空角色")

    actions: list = []
    description = str(manifest.get("description") or "").strip()
    if not description:
        description = "从 hikqin 导入的智能体"
        notes.append(f"{name}:上游没有 description,已用兜底描述(模型靠它挑角色)")

    # ── 角色私有 MCP(用私有清单,不用规范 mcp.json:前者含 tool_names,且认 stdio)──
    servers: dict[str, dict] = {}
    for server in manifest.get("mcp_services") or []:
        if not isinstance(server, dict) or not server.get("name"):
            continue
        entry, server_needs = mcp_kind.build_entry(server)
        tool_names = [str(t) for t in (server.get("tool_names") or []) if str(t).strip()]
        if tool_names:
            entry["includeTools"] = tool_names
        servers[str(server["name"])] = entry
        for env in server_needs:
            if env not in needs:
                needs.append(env)

    # ── 角色私有技能(包里是**真文件**)──
    for skill in manifest.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        skill_name = str(skill.get("name") or "").strip()
        payload = _skill_files(skill, files)
        if not skill_name or not payload:
            notes.append(f"{name}:跳过一个没有文件的技能({skill_name or '无名'})")
            continue
        if "SKILL.md" not in payload:
            notes.append(f"{name}:跳过技能 {skill_name}(根上没有 SKILL.md)")
            continue
        if not str(skill.get("description") or "").strip():
            notes.append(f"{name}:跳过技能 {skill_name}(上游没有 description,"
                         "qi 会看不见它)")
            continue
        skill_dir = role_dir / "skills" / check_name(skill_name, "技能")
        for rel, data in payload.items():
            actions.append(WriteBytes(skill_dir / _safe_rel(rel), data))

    # ── 子智能体:平铺成同级角色(技能也在同一个文件池里)──
    children = [child for child in (manifest.get("sub_agents") or [])
                if isinstance(child, dict) and not child.get("truncated")]
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
                                     files=files, taken=taken, notes=notes, needs=needs))

    # ── agent.md ──
    prompt, fixed = _unescape_newlines(prompt)
    if fixed:
        notes.append(f"{name}:上游的 system_prompt 里是字面反斜杠-n,已还原成换行")

    fields: dict[str, str] = {"name": name, "description": description}
    model, model_note = _pick_model(manifest)
    if model:
        fields["model"] = model
    elif model_note:
        notes.append(f"{name}:没写 model —— {model_note}")

    if servers:
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


def _dropped(manifest: dict) -> list[str]:
    """哪些段落没有对应物(只在真的非空时报,按标签归并免得刷屏)。"""
    counts: dict[str, int] = {}
    for key, label in DROPPED_SECTIONS.items():
        value = manifest.get(key)
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
    manifest, files = fetch_package(item, ctx)
    name = check_name(ctx.name_override or str(manifest.get("name") or item.name), "角色")
    role_dir = ctx.root / "agents" / name

    notes: list[str] = []
    needs: list[str] = []
    taken = {name}
    actions = _role_actions(manifest, role_dir, name, files=files, taken=taken,
                            notes=notes, needs=needs)

    dropped = _dropped(manifest)
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
