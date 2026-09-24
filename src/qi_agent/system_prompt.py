"""system prompt 构建(对齐 pi 的 `dist/core/system-prompt.js`)。

pi 的默认 prompt 是**代码内字符串 + 运行时拼接**;qi v1 曾把基座写成包数据
`src/qi_agent/SYSTEM.md`(静态 Markdown,无占位符)。本模块改为对齐 pi:

1. **默认基座在代码里**(`DEFAULT_*` 三段静态文案 + 按实际工具集生成的
   「可用工具」「指南」两块动态文案);
2. `SYSTEM.md`(项目 > 用户)一旦存在就**整体替换**默认基座 —— pi 的
   customPrompt 语义,不是只换某一层;
3. 无论走哪条分支,后面统一追加:**项目上下文**(AGENTS.md 等)→ **技能清单**(XML)
   → **当前工作目录**。

P-E4c 起 core **不再追加角色层与数据源** —— 两者都是“角色”的概念,归读角色的扩展
(E1.1/E15):角色说明由它通过 `before_agent_start` 拼进来(那个钩子的返回值是链式的),
数据源实例住在角色目录里。默认基座的身份描述也因此不再宣称“多 agent”。

副作用(与 pi 一致,已写进 docs/configuration.md):自定义 SYSTEM.md 会丢掉
默认基座里的「可用工具 / 指南」两块 —— 那两块只随默认基座出现。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:                                  # 只为类型标注,避免运行期耦合
    from .extensions import Tool
    from .models import Skill

# ── 默认基座:静态文案 ───────────────────────────────────────

DEFAULT_IDENTITY = (
    "你是运行在 qi 框架中的 AI 助手。qi 是一个编码 agent 框架:默认单 agent 干活;"
    "装了提供角色能力的扩展后可以按某个角色运行,也能把任务委派给别的角色。"
)

DEFAULT_ENVIRONMENT = """环境:
- 你有一个**会话工作目录**(见文末),所有文件工具的相对路径都基于它;只在该目录内读写。
- **需要事实时才用工具**:涉及本仓库/文件/命令的问题,用工具确认而不是凭记忆猜。
  寒暄、闲聊、概念性问答不需要动工具 —— 不要为了“显得在干活”而调工具。
- 需求不明确时用 `clarify` 提问;不要猜一个需求就动手。"""

DEFAULT_METHOD = """做法:
1. **先看再做**:改文件前先读;改代码前先定位调用点与影响面。
2. **小步验证**:改动后尽量用工具(读回、跑测试)确认结果,再报告结论。
3. **不编造**:工具没返回的信息就说没有;不确定就说不确定。失败与报错如实说,并给出下一步建议。
4. **务实**:能一步到位的不要拆成五步;不要为了完整而完整。"""


def build_guidelines(tool_names: Sequence[str], tools: Sequence[Tool] = ()) -> list[str]:
    """按**实际可用工具**生成指南(pi 的条件化 guidelines)。

    pi 只在「有 bash/powershell 但没有 grep/find/ls」时提示用 shell 做文件操作,
    并按两者是否同时存在分三种措辞。qi 的默认工具集自带 ls/find/grep,所以这条通常
    不出现,但收窄到 `["read","bash","edit","write"]` 时就会出现 —— 与 pi 同形。

    `tools` 非空时额外追加**工具自带的指南**(pi 的 `promptGuidelines`):它们随工具
    启用而出现,所以写工具的人不必把“用这个工具而不是那个”塞进 description。
    自带指南**必须点名工具**(平铺追加时“这个”指谁看不出来)。
    """
    has = set(tool_names)
    out: list[str] = []

    def add(text: str) -> None:
        if text not in out:
            out.append(text)

    if ({"bash", "powershell"} & has) and not ({"grep", "find", "ls"} & has):
        if "bash" in has and "powershell" in has:
            add("用 bash 或 PowerShell 做文件操作:列目录、搜索、找文件")
        elif "powershell" in has:
            add("用 PowerShell 做文件操作:列目录、搜索、找文件")
        else:
            add("用 bash 做文件操作:列目录、搜索、找文件")
    if {"bash", "powershell"} & has:
        # 装扩展的入口只有一个:`qi install` —— 它固定装进 qi 自己的解释器环境。
        # 裸 pip / uv 会落到系统 Python 或项目 .venv,那是装的"另一个环境"。
        add("装 qi 扩展用 `qi install <来源>`(如 `qi install qi-mcp`),"
            "它会装进 qi 自己的环境;不要用裸 `pip install` / `uv add`,"
            "那会装到别的环境。装完要重启 qi 才会加载。")
    for tool in tools:
        for line in tool.prompt_guidelines:
            add(line)
    add("结论先行,简明扼要")
    add("涉及文件时把路径写清楚")
    add("使用与用户一致的语言(默认中文)")
    return out


def docs_navigation() -> list[tuple[str, list[tuple[str, str]]]]:
    """读 `docs/docs.json` 的 `navigation`:`[(分组标题, [(手册标题, 路径)])]`。

    读不到(打包漏了 `docs.json`、开发树里没有 docs/、文件坏了)→ 空列表。
    调用方据此**整块不注入**,不报错也不注入半个块。
    """
    from .paths import DOCS_DIR_NAME, DOCS_INDEX_FILE_NAME, docs_dir   # 延迟导入,避免环

    root = docs_dir()
    if root is None:
        return []
    try:
        data = json.loads((root / DOCS_INDEX_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[tuple[str, list[tuple[str, str]]]] = []

    def collect(items: object) -> list[tuple[str, str]]:
        """把（可能嵌套的）分组的条目展平成一串 (标题, 路径)。

        `docs.json` 的分组照 pi 的写法可以有子分组("指南 → 运行 qi / 定制 qi / 构建于 qi"),
        而提示词里只需要一份平铺的“标题: 文件”清单 —— 递归取到底即可。
        """
        flat: list[tuple[str, str]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if path:
                flat.append((str(item.get("title") or ""), path))
            else:
                flat.extend(collect(item.get("items")))
        return flat

    for group in data.get("navigation") or []:
        if not isinstance(group, dict):
            continue
        items = [(title, name) for title, name in collect(group.get("items")) if title and name]
        if items:
            out.append((str(group.get("title") or DOCS_DIR_NAME), items))
    return out


def render_docs_index(tool_names: Sequence[str]) -> str:
    """把手册索引渲染成提示词里的一节(pi 的 `Additional docs:` 的 qi 版)。

    两个与 pi 的差别:
      - pi 把主题→文件写**死在源码里**;qi 从 `docs.json` 的 `navigation` 生成 ——
        手册增删时不会漂。
      - 与 `render_skills` 同规矩:没有能读文件的工具(`read` / `bash`)就**整块不注入**
        (给了路径也读不到)。

    路径给**绝对**的(`docs_dir()` 在 wheel 里与源码树里不一样,不能写死 `docs/`),
    正文不塞进上下文 —— 模型自己用 read 打开。
    """
    if not ({"read", "bash"} & set(tool_names)):
        return ""
    from .paths import docs_dir

    root = docs_dir()
    navigation = docs_navigation()
    if root is None or not navigation:
        return ""
    lines = ["文档(qi 自带的手册;要查用法、或要改 qi 自身,先读对应文件,不要凭记忆猜):",
             f"- 根目录: {root}"]
    for title, items in navigation:
        listed = "、".join(f"{name}" for _name_title, name in items)
        lines.append(f"- {title}: {listed}")
    lines.append("读的时候把根目录与文件名拼成绝对路径;文件之间互相引用时按相对链接跟着走。")
    return "\n".join(lines)


def default_base_prompt(tools: Sequence[Tool] = ()) -> str:
    """默认基座(身份 + 可用工具 + 指南 + 环境 + 做法)。

    只在没有自定义 `SYSTEM.md` 时使用;`tools` 为**本回合解析后**的工具集
    (`ToolCatalog.resolve` 的结果),清单即模型可调用的全集 —— 所以这里不写
    pi 那句“你可能还有别的工具”:qi 不会给出清单外的工具。
    """
    listed = "\n".join(f"- {t.name}: {t.prompt_line}" for t in tools) or "(无)"
    guidelines = "\n".join(f"- {g}" for g in build_guidelines([t.name for t in tools], tools))
    parts = [
        DEFAULT_IDENTITY,
        f"可用工具:\n{listed}",
        f"指南:\n{guidelines}",
        DEFAULT_ENVIRONMENT,
        DEFAULT_METHOD,
    ]
    # 手册索引只随**默认基座**出现(自定义 `SYSTEM.md` 是“整体替换”,作者自己决定要不要提)
    if (index := render_docs_index([t.name for t in tools])):
        parts.append(index)
    return "\n\n".join(parts)


# ── 技能清单(pi 的 <available_skills> 形态,渐进披露)──────────

_SKILL_READ_TOOLS = ("read", "bash")   # 能读 SKILL.md 全文的工具,优先 read


def _xml(value: object) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def render_skills(skills: Sequence[Skill], tool_names: Sequence[str]) -> str:
    """技能清单:只给名字/描述/路径,正文用时再读(渐进披露)。

    与 pi 一致:没有能读文件的工具(`read` / `bash`)就**整块不注入** —— 注入了也读不到,
    只会诱导模型调用不存在的工具。
    """
    if not skills:
        return ""
    reader = next((t for t in _SKILL_READ_TOOLS if t in set(tool_names)), None)
    if reader is None:
        return ""
    how = "用 read 读取它的 SKILL.md 全文" if reader == "read" else "用 bash 读取它的 SKILL.md 全文"
    lines = [
        f"以下技能为特定任务提供专门的执行说明;任务与其描述匹配时,{how}执行。",
        "技能文件里出现相对路径时,以 SKILL.md 所在目录(技能目录)为基准解析,"
        "并在工具命令里使用绝对路径。",
        "",
        "<available_skills>",
    ]
    for s in skills:
        lines += [
            "  <skill>",
            f"    <name>{_xml(s.name)}</name>",
            f"    <description>{_xml(s.description)}</description>",
            f"    <location>{_xml(s.path)}</location>",
            "  </skill>",
        ]
    lines.append("</available_skills>")
    return "\n".join(lines)


# ── 项目上下文(pi 的 <project_context>)──────────────────────

def render_project_context(files: Sequence[tuple[Path, str]]) -> str:
    """把 AGENTS.md 等**内容**渲染成 `<project_context>`(路径放在属性上)。"""
    if not files:
        return ""
    lines = ["<project_context>", "", "项目专属说明与约定:", ""]
    for path, content in files:
        lines.append(f'<project_instructions path="{path}">\n{content.strip()}\n'
                     "</project_instructions>")
        lines.append("")
    lines.append("</project_context>")
    return "\n".join(lines)


# ── 组装 ────────────────────────────────────────────────────

def build_system_prompt(base_prompt: str | None = None, *,
                        cwd: Path | None = None,
                        tools: Sequence[Tool] = (),
                        context_files: Sequence[tuple[Path, str]] | None = None,
                        skills: Sequence[Skill] = (),
                        append: str | None = None) -> str:
    """拼出 core 的 system prompt:**基座 → 项目上下文 → 技能 → 工作目录**。

    参数:
      - `base_prompt`:自定义基座(`SYSTEM.md` 正文)。空/None → 默认基座;
        非空 → **整体替换**默认基座(pi 的 customPrompt 语义)。
      - `tools`:本回合实际可用的工具集,决定「可用工具 / 指南 / 技能能否被读取」。
      - `cwd`:会话工作目录,写进 prompt 末尾;`context_files=None` 时也用它去找
        `AGENTS.md`(`context_files=[]` 表示显式不注入)。
      - `skills`:**顶层**技能(六层来源,见 loader)。

    **没有“角色层”与“数据源”** —— 两者都是 agent 的概念,归读角色的扩展(E1.1/E15):
    角色说明由它通过 `before_agent_start` 拼进来(那个钩子的返回值是链式的),数据源
    实例也在 agent 目录里。core 只负责“基座 + 项目上下文 + 技能 + cwd”。
    """
    tool_names = [t.name for t in tools]
    base = (base_prompt or "").strip() or default_base_prompt(tools)
    parts: list[str] = [base]

    if context_files is None:
        from .loader import load_project_context   # 延迟导入:避免 loader ↔ 本模块静态环

        context_files = load_project_context(cwd)
    ctx = render_project_context(context_files)
    if ctx:
        parts.append(ctx)

    rendered_skills = render_skills(skills, tool_names)
    if rendered_skills:
        parts.append(rendered_skills)

    parts.append(f"当前工作目录: {Path(cwd) if cwd else Path.cwd()}")
    # `--append-system-prompt`:用户显式追加的一段,**放最后**(与 pi 同义:追加到
    # system prompt 末尾,而不是插进某个中间层)。空/空白不占位。
    if append and append.strip():
        parts.append(append.strip())
    return "\n\n".join(p for p in parts if p and p.strip())
