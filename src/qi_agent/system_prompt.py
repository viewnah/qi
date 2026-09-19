"""system prompt 构建(对齐 pi 的 `dist/core/system-prompt.js`)。

pi 的默认 prompt 是**代码内字符串 + 运行时拼接**;qi v1 曾把基座写成包数据
`src/qi_agent/SYSTEM.md`(静态 Markdown,无占位符)。本模块改为对齐 pi:

1. **默认基座在代码里**(`DEFAULT_*` 三段静态文案 + 按实际工具集生成的
   「可用工具」「指南」两块动态文案);
2. `SYSTEM.md`(项目 > 用户)一旦存在就**整体替换**默认基座 —— pi 的
   customPrompt 语义,不是只换某一层;
3. 无论走哪条分支,后面统一追加:**角色层**(agent.md 正文)→ **项目上下文**
   (AGENTS.md 等)→ **技能清单**(XML)→ **数据源** → **当前工作目录**。

角色层始终追加,这是 qi 多 agent 语义不变的关键:换 agent = 换角色层。

副作用(与 pi 一致,已写进 docs/system-prompt.md):自定义 SYSTEM.md 会丢掉
默认基座里的「可用工具 / 指南」两块 —— 那两块只随默认基座出现。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:                                  # 只为类型标注,避免运行期耦合
    from .extensions import Tool
    from .loader import AgentUnit
    from .models import Skill

# ── 默认基座:静态文案 ───────────────────────────────────────

DEFAULT_IDENTITY = (
    "你是运行在 qi 框架中的 AI 助手。qi 是一个多 agent 编码框架:每次任务由一个专职 "
    "agent(`agents/<name>/agent.md`)执行,下面附加的角色说明才是你此刻的身份。"
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
    for tool in tools:
        for line in tool.prompt_guidelines:
            add(line)
    add("结论先行,简明扼要")
    add("涉及文件时把路径写清楚")
    add("使用与用户一致的语言(默认中文)")
    return out


def default_base_prompt(tools: Sequence[Tool] = ()) -> str:
    """默认基座(身份 + 可用工具 + 指南 + 环境 + 做法)。

    只在没有自定义 `SYSTEM.md` 时使用;`tools` 为该 agent **解析后**的工具集
    (`ToolCatalog.resolve` 的结果),清单即模型可调用的全集 —— 所以这里不写
    pi 那句“你可能还有别的工具”:qi 不会给出清单外的工具。
    """
    listed = "\n".join(f"- {t.name}: {t.prompt_line}" for t in tools) or "(无)"
    guidelines = "\n".join(f"- {g}" for g in build_guidelines([t.name for t in tools], tools))
    return "\n\n".join([
        DEFAULT_IDENTITY,
        f"可用工具:\n{listed}",
        f"指南:\n{guidelines}",
        DEFAULT_ENVIRONMENT,
        DEFAULT_METHOD,
    ])


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
                        skills: Sequence[Skill] = ()) -> str:
    """拼出 core 的 system prompt:**基座 → 项目上下文 → 技能 → 工作目录**。

    参数:
      - `base_prompt`:自定义基座(`SYSTEM.md` 正文)。空/None → 默认基座;
        非空 → **整体替换**默认基座(pi 的 customPrompt 语义)。
      - `tools`:本回合实际可用的工具集,决定「可用工具 / 指南 / 技能能否被读取」。
      - `cwd`:会话工作目录,写进 prompt 末尾;`context_files=None` 时也用它去找
        `AGENTS.md`(`context_files=[]` 表示显式不注入)。
      - `skills`:**顶层**技能(六层来源,见 loader)。

    **没有“角色层”与“数据源”** —— 两者都是 agent 的概念,归 qi-agents(E1.1/E15):
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
    return "\n\n".join(p for p in parts if p and p.strip())
