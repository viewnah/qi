"""内置 7 工具 + clarify(对齐 docs/tools.md):read/ls/find/grep/write/edit/bash。

安全:路径限制在会话工作目录内;bash 默认只读 allowlist。
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..models import TOOL_ERROR, TOOL_OK, ToolOutcome
from ..registry import Tool, ToolError

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


# ── bash(默认只读 allowlist) ────────────────────────

READ_ONLY_FIRST = {
    "cat", "head", "tail", "less", "more", "grep", "rg", "find", "ls", "pwd",
    "tree", "wc", "sort", "uniq", "echo", "printf", "date", "uname", "whoami",
    "which", "env", "du", "df", "file", "stat", "diff", "python3", "python",
}
GIT_READ_ONLY = {"status", "log", "diff", "branch", "show", "ls-files", "remote", "rev-parse", "stash", "submodule"}

WRITE_HINT = "默认只读白名单;写文件请用 write/edit 工具,或配置 [runtime] bash 放开"


def _bash_allowed(command: str) -> tuple[bool, str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False, "命令无法解析"
    if not tokens:
        return False, "空命令"
    first = tokens[0]
    if first == "git":
        sub = tokens[1] if len(tokens) > 1 else "status"
        return sub in GIT_READ_ONLY, f"git {sub} 不在只读列表;{WRITE_HINT}"
    if first in READ_ONLY_FIRST:
        return True, ""
    if first == "python3":
        # 只允许 -c 读脚本内无 shell 的场合?过宽,禁止裸 python(防任意写)
        return False, "禁止裸 python3;如确需,请配置 [runtime] bash 放开"
    return False, f"{first} 不在只读白名单;{WRITE_HINT}"


async def _bash(args: dict, ctx: ToolContext) -> ToolOutcome:
    """bash 是唯一能上报退出码的工具。

    `result` 文本与旧版**逐字一致**(失败时保留 `exit=N` 前缀),模型看到的内容不变;
    `status` / `exit_code` 是给 UI 工具卡片用的结构化字段。
    """
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("bash 需要 command")
    allowed, reason = _bash_allowed(command)
    if not allowed:
        raise ToolError(f"命令被安全策略拒绝: {reason}")
    timeout = _num_arg(args, "timeout", float, 120.0)
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=ctx.workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        # 必须回收:只 kill 不 wait 的话,transport 会拖到事件循环关闭后才被 GC,
        # 触发 “Event loop is closed” 的 unraisable 异常(测试里会报资源警告)。
        with contextlib.suppress(Exception):
            await proc.wait()
        # 超时拿不到退出码:用 error 字段给出机器可读原因
        return ToolOutcome(status=TOOL_ERROR, error="timeout",
                           result=f"命令超时(>{timeout:.0f}s),已终止")
    text = out.decode("utf-8", errors="replace")
    rc = proc.returncode
    if rc == 0:
        return ToolOutcome(status=TOOL_OK, result=_truncate(text), exit_code=0)
    return ToolOutcome(status=TOOL_ERROR, result=f"exit={rc}\n{_truncate(text)}", exit_code=rc)


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
    catalog.register(Tool("read", "读取文件内容,支持行范围。路径相对会话目录。", _schema(
        {"path": {"type": "string", "description": "文件路径"},
         "start_line": {"type": "integer"},
         "end_line": {"type": "integer"}}, ["path"]), _read))
    catalog.register(Tool("ls", "列出目录内容(前缀 d=目录/f=文件)。", _schema(
        {"path": {"type": "string"}}), _ls))
    catalog.register(Tool("find", "按文件名/glob 搜索(如 *.py)。", _schema(
        {"path": {"type": "string"}, "pattern": {"type": "string"}}, ["pattern"]), _find))
    catalog.register(Tool("grep", "正则搜索文件内容,返回 文件:行号:行。", _schema(
        {"pattern": {"type": "string"}, "path": {"type": "string"}, "ignore_case": {"type": "boolean"},
         "context": {"type": "integer"}, "max_count": {"type": "integer"}}, ["pattern"]), _grep))
    catalog.register(Tool("write", "整写文件(新建/覆盖)。", _schema(
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]), _write))
    catalog.register(Tool("edit", "diff 精确编辑:old_text 必须唯一匹配后替换为 new_text。", _schema(
        {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
        ["path", "old_text", "new_text"]), _edit))
    catalog.register(Tool("bash", "执行只读命令(安全白名单);写操作请用 write/edit。", _schema(
        {"command": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]), _bash))
    catalog.register(Tool("clarify", "向用户提出澄清问题,等待回答。拿不准需求时使用。", _schema(
        {"question": {"type": "string"}}, ["question"]), _clarify))
