"""qi-a2a:用 A2A 协议调用远端智能体(extension,入口 `register(api)`)。

四块:

1. **两层配置** `~/.qi/agent/a2a.json` → `<cwd>/.qi/a2a.json`(同名深合并,见 `config.py`);
2. **代理工具** `a2a`(list / card / send / resume / get / cancel,见 `proxy.py`);
3. **直连工具** `a2a__<条目>`(opt-in,见 `direct.py`);
4. **`/a2a` 面板**:只读诊断(来源层 / 未识别字段 / 环境变量键名),不联网。

一个条目通常就是**一个智能体**(一张 Agent Card);`baseUrl` 不带 `tenant` 时它是网关。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qi_agent.extensions import ExtensionApi

from . import config
from .direct import register_direct
from .proxy import build_tool

__all__ = ["register"]


def _cwd(ctx: Any) -> Path:
    value = getattr(ctx, "workdir", None) or getattr(ctx, "cwd", None)
    return Path(value) if value else Path.cwd()


def _note(ctx: Any, text: str) -> None:
    ui = getattr(ctx, "ui", None)
    if ui is not None and callable(getattr(ui, "notify", None)):
        ui.notify(text)
        return
    notes = getattr(ctx, "notes", None)
    if isinstance(notes, list):
        notes.append(text)


def _panel(args: str, ctx: Any) -> None:
    """`/a2a`:配置面板。**纯读 JSON**,不联网 —— 一个挂掉的网关不该让面板卡住。"""
    cwd = _cwd(ctx)
    trusted = bool(getattr(ctx, "project_trusted", True))
    try:
        entries = config.resolve(cwd, trusted=trusted)
    except config.ConfigError as exc:
        _note(ctx, f"a2a 配置读取失败:{exc}")
        return

    wanted = (args or "").strip()
    selected = {name: entry for name, entry in entries.items()
                if not wanted or name == wanted}
    if wanted and not selected:
        _note(ctx, f"没有条目 {wanted!r};有:{', '.join(sorted(entries)) or '(无)'}")
        return

    enabled = [entry for entry in selected.values() if not entry.disabled]
    lines = [f"A2A 条目({len(enabled)} 个启用 / {len(selected)} 个):"]
    if not selected:
        lines.append("  (无)")
    for name in sorted(selected):
        entry = selected[name]
        scope = "项目" if entry.scope == "project" else "全局"
        flag = "  [已关闭]" if entry.disabled else ""
        lines.append(f"- {name}  [{scope}]{flag}  {entry.summary()}")
        if entry.description:
            lines.append(f"    {entry.description}")
        if entry.wants_direct():
            lines.append(f"    直连工具:{config.entry_tool_name(entry)}")
        headers = entry.headers
        if headers:
            lines.append(f"    请求头(只显示键名):{', '.join(sorted(headers))}")
        unknown = config.unknown_fields(entry)
        if unknown:
            lines.append(f"    ⚠ 未识别字段:{', '.join(unknown)}(不会生效)")

    lines.append("")
    for scope, path in config.scope_files(cwd):
        label = "全局" if scope == "global" else "项目"
        exists = "有" if path.is_file() else "无"
        lines.append(f"{label}({exists}):{path}")
    if not trusted:
        lines.append("项目未受信任:项目层 a2a.json 已跳过(用 `-a` 信任本次运行)")
    lines.append('调用:a2a(action="list"),或 a2a(action="send", server="…", message="…")')
    _note(ctx, "\n".join(lines))


def register(api: ExtensionApi) -> None:
    # 装载阶段只登记**静态面**:不联网、不读项目配置(此时没有 cwd)。
    api.register_tool(build_tool())
    api.register_command("a2a", _panel, description="列出 A2A 条目、来源与配置")

    def on_session_start(payload: dict, ctx: Any) -> None:
        try:
            entries = config.active(_cwd(ctx),
                                    trusted=bool(getattr(ctx, "project_trusted", True)))
        except config.ConfigError as exc:
            _note(ctx, f"a2a 配置读取失败:{exc}")
            return
        added = register_direct(api, entries, ctx)
        if added:
            _note(ctx, f"a2a 已注册直连工具:{', '.join(added)}")

    api.on("session_start", on_session_start)
