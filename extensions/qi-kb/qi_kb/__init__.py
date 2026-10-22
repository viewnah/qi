"""qi-kb：hikqin 知识库的 provider/model 式扩展。

`register` 保持薄：只注册 CLI、工具、斜杠命令和 `before_agent_start`。
真正的读写在 `config.py` / `providers.py` / `service.py`。
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

from . import cli, format as fmt, service
from .errors import KbError
from .tools import build_tool

__all__ = ["register", "main"]


def _before_agent_start(payload: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
    cwd = getattr(ctx, "cwd", None) or getattr(ctx, "workdir", None)
    session_id = None
    view = getattr(ctx, "session_manager", None)
    if view is not None:
        session_id = getattr(view, "session_id", None)
    section = service.prompt_section(cwd, session_id=session_id)
    if not section:
        return None
    current = str(payload.get("system_prompt") or "")
    return {"system_prompt": (current.rstrip() + "\n\n" + section).strip()}


async def _command(args: str, ctx: Any) -> None:
    """`/kb …`：只做只读动作，写操作走 CLI / lab。"""
    cwd = getattr(ctx, "cwd", None) or getattr(ctx, "workdir", None)
    try:
        argv = shlex.split(str(args or ""))
    except ValueError as exc:
        ctx.ui.notify(f"参数解析失败：{exc}")
        return
    if not argv:
        data = await asyncio.to_thread(service.status, cwd)
        ctx.ui.notify(fmt.format_status(data))
        return
    head = argv[0].lower()
    if head in ("providers", "provider"):
        data = await asyncio.to_thread(service.providers_list, cwd)
        ctx.ui.notify(fmt.format_providers(data.get("providers") or []))
        return
    if head == "list":
        data = await asyncio.to_thread(service.available, cwd,
                                       bound_only="--bound" in argv)
        ctx.ui.notify(fmt.format_list(data.get("items") or [],
                                      bound_only="--bound" in argv,
                                      warnings=data.get("warnings") or []))
        return
    if head == "search":
        query = " ".join(argv[1:]).strip()
        if not query:
            ctx.ui.notify("用法：/kb search <关键词> [@provider]")
            return
        data = await asyncio.to_thread(service.search, cwd, query)
        ctx.ui.notify(fmt.format_search(data))
        return
    if head == "ask":
        question = " ".join(argv[1:]).strip()
        if not question:
            ctx.ui.notify("用法：/kb ask <问题> [@provider]")
            return
        data = await asyncio.to_thread(service.ask, cwd, question)
        ctx.ui.notify(fmt.format_ask(data))
        return
    if head == "docs":
        kb = " ".join(argv[1:]).strip()
        if not kb:
            ctx.ui.notify("用法：/kb docs <kbId|名称[@provider]>")
            return
        data = await asyncio.to_thread(service.documents, cwd, kb)
        lines = [f"文档（{data['provider']} / {data['name']}）"]
        for doc in data.get("documents") or []:
            lines.append(f"- {doc.get('filename')}  chunks={doc.get('chunk_count')}  "
                         f"{doc.get('status')}")
        ctx.ui.notify("\n".join(lines))
        return
    ctx.ui.notify(
        "用法：\n"
        "  /kb                     状态\n"
        "  /kb providers           账号\n"
        "  /kb list [--bound]      可用 / 已绑定\n"
        "  /kb search <关键词>     检索\n"
        "  /kb ask <问题>          问答\n"
        "  /kb docs <kb>           文档"
    )


def register(api: Any) -> None:
    api.register_cli_command(
        "kb",
        cli.main,
        description="hikqin 知识库：provider（账号）+ knowledgeBases（KB）",
    )
    api.register_tool(build_tool())
    api.register_command("kb", _command, description="知识库：状态 / 检索 / 问答 / 文档")
    api.on("before_agent_start", _before_agent_start)


def main(argv: list[str] | None = None) -> int:
    return cli.main(argv)
