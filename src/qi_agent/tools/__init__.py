"""内置 8 工具 + clarify(对齐 docs/tools.md):read/ls/find/grep/write/edit/bash/powershell。

安全:六个文件工具的路径限制在会话工作目录内。bash / powershell 与 pi 一致 —— **不做命令级过滤**
(不筛子命令、不拦重定向),限制只来自工具级收窄(tools / disallowed_tools)或容器/VM。
见 docs/tools.md §4 与 docs/bash-allowlist.md。
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..abort import AbortSignal
from ..extensions import ExtensionUi, Tool, ToolError, register_tool
from ..models import TOOL_ERROR, TOOL_OK, ToolOutcome
from .shell import (
    POWERSHELL_UTF8_PREFIX,
    ShellError,
    ShellResult,
    resolve_powershell_config,
    resolve_shell_config,
    run_shell,
)

MAX_RESULT = 200          # 截断行数
MAX_FILE_CHARS = 50_000   # read 默认截断字符

# clarify 回调签名:async (text) -> str | None
AskFn = Callable[[str], Awaitable[str | None]]


@dataclass
class ToolContext:
    agent_name: str
    workdir: Path                        # 会话工作目录(路径边界)
    data_sources: list = field(default_factory=list)
    ask: AskFn | None = None             # async (text)->str|None,供 clarify 用
    shell_path: str | None = None        # settings.shellPath:显式指定 bash(对齐 pi)
    abort: AbortSignal | None = None     # 本回合的中断信号(runner 按回合注入)
    #: 扩展工具要问人时的入口。**总是存在**(没前端就按 default 回答),与 handler 侧的
    #: `ctx.ui` 同一套规则 —— 否则“扩展工具不能问人”会变成一个说不清的例外。
    ui: ExtensionUi = field(default_factory=ExtensionUi)
    #: 本项目是否已被信任(`-a` / 交互确认 / `defaultProjectTrust`)。
    #: 工具**看得见**这件事,才能自己对“项目里带来的东西”设门 —— 否则每个要设门的
    #: 扩展都会退化成相信自己写对了(而它无从知道)。写 qi-agents 时发现并加上:
    #: 项目级角色是仓库控制的提示词,`subagent` 工具得先问一句。
    project_trusted: bool = True

    def guard(self, p: str | Path) -> Path:
        """路径必须落在 workdir 内(防越界,对齐 hikqin validate_path 思想)。"""
        raw = Path(p).expanduser()
        if raw.is_absolute():
            resolved = raw.resolve(strict=False)
        else:
            resolved = (self.workdir / raw).resolve(strict=False)
        root = self.workdir.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise ToolError(f"路径越界(仅允许会话目录内): {p}")
        return resolved


def _truncate(text: str, limit: int = MAX_RESULT) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit]) + f"\n…(截断,共 {len(lines)} 行,前 {limit} 行)"


def _num_arg(args: dict, key: str, cast, default):
    """取数值型工具参数:缺失/空 → default;非法 → ToolError(模型可读,不裸抛 ValueError)。"""
    raw = args.get(key)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise ToolError(f"{key} 需要{cast.__name__}类型,收到 {raw!r}") from None


# ── read ─────────────────────────────────────────────

async def _read(args: dict, ctx: ToolContext) -> str:
    path = ctx.guard(str(args["path"]))
    if path.is_dir():
        raise ToolError(f"{path} 是目录")
    if not path.is_file():
        raise ToolError(f"文件不存在: {path}")
    start = _num_arg(args, "start_line", int, 1)
    end = _num_arg(args, "end_line", int, 0)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    if end <= 0:
        end = total
    if start < 1 or end < start:
        raise ToolError("start_line ≥ 1 且 end_line ≥ start_line")
    seg = lines[start - 1 : end]
    shown = "\n".join(seg)
    if sum(len(l) for l in seg) > MAX_FILE_CHARS:
        shown = shown[:MAX_FILE_CHARS] + "\n…(内容过长已截断)"
    head = f"# {path} (行 {start}-{min(end,total)}/{total})"
    return _truncate(head + "\n" + shown, limit=MAX_RESULT)


# ── ls ───────────────────────────────────────────────

async def _ls(args: dict, ctx: ToolContext) -> str:
    path = ctx.guard(str(args.get("path", ".")))
    if not path.is_dir():
        raise ToolError(f"不是目录: {path}")
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        raise ToolError(f"无法列出目录: {exc}") from exc
    rows = []
    for n in names:
        full = path / n
        try:
            kind = "d" if full.is_dir() else ("x" if os.access(full, os.X_OK) else "f")
        except OSError:  # 权限不足/坏软链:仍列出条目,退化为普通文件
            kind = "f"
        rows.append(f"{kind} {n}")
    return _truncate(f"# {path} ({len(rows)} 项)\n" + "\n".join(rows))


# ── find(文件名/glob) ────────────────────────────────

async def _find(args: dict, ctx: ToolContext) -> str:
    root = ctx.guard(str(args.get("path", ".")))
    pattern = str(args.get("pattern", ""))
    if not pattern:
        raise ToolError("需要 pattern(名称或 glob,如 *.py)")
    hits: list[Path] = []
    for base, _dirs, files in os.walk(root):
        if ".git" in base.split(os.sep):
            continue
        for f in files:
            if fnmatch.fnmatch(f, pattern):
                hits.append(Path(base) / f)
    rel = [str(p.relative_to(ctx.workdir)) if ctx.workdir in p.parents else str(p) for p in hits]
    return _truncate(f"# 匹配 {pattern}: {len(rel)} 个\n" + "\n".join(sorted(rel)))


# ── grep(正则 + 行号 + 上下文) ───────────────────────

async def _grep(args: dict, ctx: ToolContext) -> str:
    root = ctx.guard(str(args.get("path", ".")))
    pattern = str(args.get("pattern", ""))
    if not pattern:
        raise ToolError("需要 pattern")
    regex = re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
    context = _num_arg(args, "context", int, 0)
    max_count = _num_arg(args, "max_count", int, 100)
    matches: list[str] = []
    files = [root] if root.is_file() else sorted(
        p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts
    )
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines):
            if regex.search(line):
                rel = str(f.relative_to(ctx.workdir)) if ctx.workdir in f.parents else str(f)
                matches.append(f"{rel}:{idx+1}: {line.strip()[:500]}")
                for off in range(1, context + 1):
                    if idx + off < len(lines):
                        matches.append(f"{rel}:{idx+off+1}: {lines[idx+off].strip()[:500]}")
                if len(matches) >= max_count:
                    return _truncate(f"# 命中 {pattern}(已达 {max_count} 条上限)\n" + "\n".join(matches))
    return _truncate(f"# 命中 {pattern}: {len(matches)}\n" + "\n".join(matches))


# ── write ────────────────────────────────────────────

async def _write(args: dict, ctx: ToolContext) -> str:
    path = ctx.guard(str(args["path"]))
    content = str(args.get("content", ""))
    if path.is_dir():
        raise ToolError(f"{path} 是目录")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    return f"{'已覆盖' if existed else '已新建'} {path} ({len(content)} 字符)"


# ── edit(diff 精确编辑:old_text→new_text 唯一匹配) ───

async def _edit(args: dict, ctx: ToolContext) -> str:
    path = ctx.guard(str(args["path"]))
    old_text = str(args.get("old_text", ""))
    new_text = str(args.get("new_text", ""))
    if not old_text:
        raise ToolError("edit 需要 old_text(精确匹配)")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        raise ToolError(f"old_text 未匹配到(检查空白/缩进): {old_text[:80]!r}")
    if count > 1:
        raise ToolError(f"old_text 匹配到 {count} 处,请扩大上下文使其唯一")
    path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"已编辑 {path}:替换 1 处,{len(old_text)}→{len(new_text)} 字符"


# ── 命令执行:bash / powershell(与 pi 对齐:真 shell + 无命令级过滤) ──


def _shell_outcome(result: ShellResult, timeout: float) -> ToolOutcome:
    """两个 shell 工具共用的结果收敛 —— 文本形态与旧版**逐字一致**。"""
    if result.aborted:
        return ToolOutcome(status=TOOL_ERROR, error="aborted",
                           result="操作已中断(用户中止本回合)")
    if result.timed_out:
        return ToolOutcome(status=TOOL_ERROR, error="timeout",
                           result=f"命令超时(>{timeout:.0f}s),已终止")
    rc = result.exit_code
    if rc == 0:
        return ToolOutcome(status=TOOL_OK, result=_truncate(result.text), exit_code=0)
    return ToolOutcome(status=TOOL_ERROR, result=f"exit={rc}\n{_truncate(result.text)}", exit_code=rc)


async def _bash(args: dict, ctx: ToolContext) -> ToolOutcome:
    """执行 bash 命令 —— 解析**真正的 bash**,不是系统默认 shell(对齐 pi)。

    `bash` 是唯一能上报退出码的工具:`result` 文本与旧版**逐字一致**(失败时保留
    `exit=N` 前缀),模型看到的内容不变;`status` / `exit_code` 是给 UI 工具卡片用的
    结构化字段。

    安全:与 pi 一致,内置 bash **不筛命令**。要收紧就在 agent 级摘工具
    (`disallowed_tools: [bash]`),要真边界就把进程放进容器/VM。
    """
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("bash 需要 command")
    timeout = _num_arg(args, "timeout", float, 120.0)
    try:
        config = resolve_shell_config(ctx.shell_path)
    except ShellError as exc:
        raise ToolError(str(exc)) from None
    result = await run_shell(config, command, cwd=ctx.workdir, timeout=timeout,
                             abort=ctx.abort)
    return _shell_outcome(result, timeout)


async def _powershell(args: dict, ctx: ToolContext) -> ToolOutcome:
    """Windows 原生通道:PowerShell(pi 的第 8 个工具)。

    非 Windows **不**静默降级成 bash —— 那会给出“命令确实跑过了”的假象;
    这里回一条能照做的错误(换 bash,或用 `pwsh -Command ...`)。
    """
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("powershell 需要 command")
    timeout = _num_arg(args, "timeout", float, 120.0)
    try:
        config = resolve_powershell_config()
    except ShellError as exc:
        raise ToolError(str(exc)) from None
    result = await run_shell(config, POWERSHELL_UTF8_PREFIX + command,
                             cwd=ctx.workdir, timeout=timeout, abort=ctx.abort)
    return _shell_outcome(result, timeout)


# ── clarify(通用小工具,B4:v1 内置) ──────────────────

async def _clarify(args: dict, ctx: ToolContext) -> str:
    question = str(args.get("question", "请澄清:")).strip()
    if ctx.ask is not None:
        answer = await ctx.ask(question)
        if answer:
            return f"用户回答: {answer}"
    return f"[需要用户澄清] {question}"


# ── 注册 ─────────────────────────────────────────────

def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def register_builtin_tools(catalog) -> None:
    """注册内置工具。

    P-E2a **dogfood**:走的是扩展同一个 `register_tool`(不是 `catalog.register`),
    来源照 pi 的写法记 `source="builtin"` / `path="<builtin:名>"` —— 于是
    `getAllTools()` 能用同一把尺子区分内置与扩展工具,而不需要额外维护一张名单。

    `description` 进 tool schema(模型决定调不调时的说明),`prompt_snippet` 进系统
    提示词「可用工具」那一行(更短);`prompt_guidelines` 只在该工具启用时追加。
    """
    def add(tool: Tool) -> None:
        register_tool(catalog, tool, source="builtin", path=f"<builtin:{tool.name}>",
                      scope="temporary", origin="top-level")

    add(Tool("read", "读取文件内容,支持行范围。路径相对会话目录。", _schema(
        {"path": {"type": "string", "description": "文件路径"},
         "start_line": {"type": "integer"},
         "end_line": {"type": "integer"}}, ["path"]), _read,
        prompt_snippet="读取文件(可给行范围)",
        prompt_guidelines=["用 read 读文件,不要用 bash 的 cat/sed/head/tail 代替(带行号且省 token)。"]))
    add(Tool("ls", "列出目录内容(前缀 d=目录/f=文件)。", _schema(
        {"path": {"type": "string"}}), _ls,
        prompt_snippet="列出目录内容"))
    add(Tool("find", "按文件名/glob 搜索(如 *.py)。", _schema(
        {"path": {"type": "string"}, "pattern": {"type": "string"}}, ["pattern"]), _find,
        prompt_snippet="按文件名/glob 找文件"))
    add(Tool("grep", "正则搜索文件内容,返回 文件:行号:行。", _schema(
        {"pattern": {"type": "string"}, "path": {"type": "string"}, "ignore_case": {"type": "boolean"},
         "context": {"type": "integer"}, "max_count": {"type": "integer"}}, ["pattern"]), _grep,
        prompt_snippet="正则搜文件内容"))
    add(Tool("write", "整写文件(新建/覆盖)。", _schema(
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]), _write,
        prompt_snippet="整写文件(新建/覆盖)",
        prompt_guidelines=["用 write 建新文件;改已有文件用 edit。"]))
    add(Tool("edit", "diff 精确编辑:old_text 必须唯一匹配后替换为 new_text。", _schema(
        {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
        ["path", "old_text", "new_text"]), _edit,
        prompt_snippet="精确替换(唯一匹配)",
        prompt_guidelines=["改已有文件优先用 edit(精确替换),不要用 write 整写覆盖。"]))
    add(Tool("bash", "执行 bash 命令(工作目录=会话目录);输出截断 + timeout。", _schema(
        {"command": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]), _bash,
        prompt_snippet="执行 bash 命令"))
    add(Tool("powershell", "执行 PowerShell 命令(仅 Windows;输出走 UTF-8)。", _schema(
        {"command": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]), _powershell,
        prompt_snippet="执行 PowerShell 命令(仅 Windows)"))
    add(Tool("clarify", "向用户提出澄清问题,等待回答。拿不准需求时使用。", _schema(
        {"question": {"type": "string"}}, ["question"]), _clarify,
        prompt_snippet="向用户澄清"))
