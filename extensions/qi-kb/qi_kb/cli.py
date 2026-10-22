"""`qi kb` 的命令行（argparse；选项表静态，自己解析 argv）。"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any, Callable

from rich.console import Console

from . import format as fmt, service
from .errors import (
    EXIT_CANCELLED,
    EXIT_OK,
    EXIT_USAGE,
    Cancelled,
    Conflict,
    KbError,
    UsageError,
)

console = Console()
err_console = Console(stderr=True)


def _json(data: Any) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False, default=str))


def _confirm(text: str, *, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise UsageError(f"非交互终端，无法确认「{text}」", hint="确认无误请加 -y")
    try:
        return input(f"{text} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError as exc:
        raise Cancelled("已取消") from exc


def _password(args: argparse.Namespace) -> str:
    if getattr(args, "password_stdin", False):
        line = sys.stdin.readline()
        if not line:
            raise UsageError("--password-stdin 没有读到内容")
        return line.rstrip("\n")
    if not sys.stdin.isatty():
        raise UsageError("非交互终端，无法输入密码", hint="用 --password-stdin")
    return getpass.getpass("密码: ")


def _scope(args: argparse.Namespace) -> str:
    scope = getattr(args, "scope", None) or "global"
    if scope not in ("global", "project"):
        raise UsageError(f"未知的作用域：`{scope}`", hint="可用：global / project")
    return scope


# ── 命令 ──────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
    data = service.status()
    _json(data) if args.json else console.print(fmt.format_status(data))
    return EXIT_OK


def cmd_provider_list(args: argparse.Namespace) -> int:
    data = service.providers_list()
    if args.json:
        _json(data)
    else:
        console.print(fmt.format_providers(data.get("providers") or []))
        for warning in data.get("warnings") or []:
            err_console.print(f"[yellow]! {warning}[/yellow]")
    return EXIT_OK


def cmd_provider_add(args: argparse.Namespace) -> int:
    data = service.provider_add(
        None, args.name, base_url=args.base_url, api_prefix=args.api_prefix,
        login=args.user, role=args.role, instance=args.instance, scope=_scope(args),
    )
    _json(data) if args.json else console.print(
        f"已添加 provider [bold]{data['name']}[/bold] @ {data['baseUrl']}")
    return EXIT_OK


def cmd_provider_login(args: argparse.Namespace) -> int:
    password = _password(args)
    data = service.provider_login(
        None, args.name, args.user or args.name, password,
        base_url=args.base_url, api_prefix=args.api_prefix,
        scope=_scope(args), instance=args.instance,
    )
    if args.json:
        _json(data)
    else:
        console.print(f"已登录 [bold]{data['name']}[/bold]（{data.get('role') or '?'}）")
    return EXIT_OK


def cmd_provider_remove(args: argparse.Namespace) -> int:
    if not _confirm(f"删除 provider {args.name}？", assume_yes=args.yes):
        raise Cancelled("已取消")
    data = service.provider_remove(None, args.name, cascade=args.cascade)
    _json(data) if args.json else console.print(
        f"已删除 provider [bold]{data['provider']}[/bold]"
        + (f"（连同 {len(data['knowledgeBases'])} 个 KB）" if data.get("knowledgeBases") else ""))
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    data = service.available(provider=args.provider, q=args.q,
                             bound_only=args.bound)
    if args.json:
        _json(data)
    else:
        console.print(fmt.format_list(data.get("items") or [], bound_only=args.bound,
                                      warnings=data.get("warnings") or []))
    return EXIT_OK


def _install_once(args: argparse.Namespace, *, rebind: bool) -> dict[str, Any]:
    return service.install(None, args.kb, args.provider,
                           scope=_scope(args), rebind=rebind)


def cmd_install(args: argparse.Namespace) -> int:
    try:
        data = _install_once(args, rebind=bool(args.rebind))
    except Conflict as exc:
        if args.keep:
            if args.json:
                _json({"action": "keep", "kb": args.kb,
                       "existingProvider": exc.existing_provider})
            else:
                console.print(f"保留原样：已由 {exc.existing_provider} 安装")
            return EXIT_OK
        if args.rebind:
            raise
        if args.yes or not sys.stdin.isatty():
            raise
        if not _confirm(
            f"该 KB 已由 {exc.existing_provider} 安装，覆盖为 {exc.target_provider}？"
            "（只改归属 provider，不复制文档）"
        ):
            console.print(f"保留原样：已由 {exc.existing_provider} 安装")
            return EXIT_OK
        data = _install_once(args, rebind=True)
    _json(data) if args.json else console.print(
        f"已安装 [bold]{data['kb']['name']}[/bold] "
        f"({data['action']}) → {data['provider']}/{data['kbId']}")
    return EXIT_OK


def cmd_rebind(args: argparse.Namespace) -> int:
    data = service.rebind(None, args.kb, args.provider, scope=_scope(args))
    _json(data) if args.json else console.print(
        f"已把 {data['name']} 从 {data['from']} 迁移到 {data['to']}")
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not _confirm(f"卸载知识库 {args.kb}？（只删本地绑定，不动上游）", assume_yes=args.yes):
        raise Cancelled("已取消")
    data = service.uninstall(None, args.kb)
    _json(data) if args.json else console.print(
        f"已卸载 [bold]{data['name']}[/bold]（{data['provider']}/{data['kbId']}）")
    return EXIT_OK


def _search_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "kb_ids": [args.kb] if args.kb else None,
        "provider": args.provider,
        "top_k": args.top_k,
        "strategy": args.strategy,
        "use_rerank": args.use_rerank,
        "rerank_top_k": args.rerank_top_k,
    }


def cmd_search(args: argparse.Namespace) -> int:
    data = service.search(None, args.query, **_search_kwargs(args))
    _json(data) if args.json else console.print(fmt.format_search(data))
    return EXIT_OK


def cmd_ask(args: argparse.Namespace) -> int:
    if args.stream and args.json:
        raise UsageError("--stream 与 --json 不能同时用")
    if args.stream:
        sources: list[dict[str, Any]] = []
        for event in service.ask_stream(None, args.question, **_search_kwargs(args)):
            kind = str(event.get("type") or "")
            if kind == "token":
                sys.stdout.write(str(event.get("content") or ""))
                sys.stdout.flush()
            elif kind == "sources":
                sources = [s for s in (event.get("content") or []) if isinstance(s, dict)]
            elif kind == "done":
                pass
        sys.stdout.write("\n")
        if sources:
            console.print("来源：")
            for index, source in enumerate(sources, start=1):
                meta = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
                doc = source.get("doc_name") or meta.get("filename") or "-"
                console.print(f"[{index}] {source.get('provider') or '-'} / "
                              f"{source.get('kb_name') or '-'} / {doc}")
        return EXIT_OK
    data = service.ask(None, args.question, **_search_kwargs(args))
    _json(data) if args.json else console.print(fmt.format_ask(data))
    return EXIT_OK


def cmd_docs(args: argparse.Namespace) -> int:
    data = service.documents(None, args.kb, provider=args.provider)
    if args.json:
        _json(data)
    else:
        console.print(f"文档（{data['provider']} / {data['name']} / {data['kbId']}）")
        for doc in data.get("documents") or []:
            console.print(f"- {doc.get('filename')}  chunks={doc.get('chunk_count')}  "
                          f"{doc.get('status')}")
    return EXIT_OK


# ── parser ────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qi kb",
        description="qi 的知识库扩展：provider（hikqin 账号）+ knowledgeBases（KB）",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<子命令>")

    def add(name: str, handler: Callable[..., int], help: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help, description=help)
        item.set_defaults(func=handler)
        return item

    status = add("status", cmd_status, "配置 / provider / 已绑定 KB 状态")
    status.add_argument("--json", action="store_true")

    provider = sub.add_parser("provider", help="provider（账号）管理")
    psub = provider.add_subparsers(dest="pcmd", metavar="<子命令>")

    def padd(name: str, handler: Callable[..., int], help: str) -> argparse.ArgumentParser:
        item = psub.add_parser(name, help=help, description=help)
        item.set_defaults(func=handler)
        return item

    plist = padd("list", cmd_provider_list, "列出 provider")
    plist.add_argument("--json", action="store_true")

    padd_cmd = padd("add", cmd_provider_add, "添加 provider（不登录）")
    padd_cmd.add_argument("name")
    padd_cmd.add_argument("--base-url", required=True)
    padd_cmd.add_argument("--api-prefix", default="/api")
    padd_cmd.add_argument("-u", "--user")
    padd_cmd.add_argument("--role")
    padd_cmd.add_argument("--instance")
    padd_cmd.add_argument("--scope", choices=("global", "project"))
    padd_cmd.add_argument("--json", action="store_true")

    plogin = padd("login", cmd_provider_login, "登录 provider（写 kb-auth.json）")
    plogin.add_argument("name")
    plogin.add_argument("-u", "--user")
    plogin.add_argument("--password-stdin", action="store_true")
    plogin.add_argument("--base-url")
    plogin.add_argument("--api-prefix", default="/api")
    plogin.add_argument("--instance")
    plogin.add_argument("--scope", choices=("global", "project"))
    plogin.add_argument("--json", action="store_true")

    premove = padd("remove", cmd_provider_remove, "删除 provider（连同 auth）")
    premove.add_argument("name")
    premove.add_argument("--cascade", action="store_true")
    premove.add_argument("-y", "--yes", action="store_true")
    premove.add_argument("--json", action="store_true")

    listing = add("list", cmd_list, "列出可用 / 已绑定知识库")
    listing.add_argument("--provider")
    listing.add_argument("--bound", action="store_true")
    listing.add_argument("-q", "--q")
    listing.add_argument("--json", action="store_true")

    install = add("install", cmd_install, "安装知识库（按 instance+kbId 去重）")
    install.add_argument("kb")
    install.add_argument("--provider", required=True)
    install.add_argument("--scope", choices=("global", "project"))
    install.add_argument("--rebind", action="store_true", help="已存在时覆盖（迁移归属）")
    install.add_argument("--keep", action="store_true", help="已存在时保留原样")
    install.add_argument("-y", "--yes", action="store_true")
    install.add_argument("--json", action="store_true")

    rebind = add("rebind", cmd_rebind, "把 KB 迁移到另一个 provider")
    rebind.add_argument("kb")
    rebind.add_argument("--provider", required=True)
    rebind.add_argument("--scope", choices=("global", "project"))
    rebind.add_argument("--json", action="store_true")

    uninstall = add("uninstall", cmd_uninstall, "卸载知识库（只删本地绑定）")
    uninstall.add_argument("kb")
    uninstall.add_argument("-y", "--yes", action="store_true")
    uninstall.add_argument("--json", action="store_true")

    search = add("search", cmd_search, "检索知识库")
    search.add_argument("query")
    search.add_argument("--kb")
    search.add_argument("--provider")
    search.add_argument("--top-k", type=int)
    search.add_argument("--strategy", choices=("dense", "sparse", "hybrid"))
    search.add_argument("--use-rerank", dest="use_rerank", action="store_true", default=None)
    search.add_argument("--no-rerank", dest="use_rerank", action="store_false")
    search.add_argument("--rerank-top-k", type=int)
    search.add_argument("--json", action="store_true")

    ask = add("ask", cmd_ask, "知识库问答（上游 RAG LLM）")
    ask.add_argument("question")
    ask.add_argument("--kb")
    ask.add_argument("--provider")
    ask.add_argument("--top-k", type=int)
    ask.add_argument("--strategy", choices=("dense", "sparse", "hybrid"))
    ask.add_argument("--use-rerank", dest="use_rerank", action="store_true", default=None)
    ask.add_argument("--no-rerank", dest="use_rerank", action="store_false")
    ask.add_argument("--rerank-top-k", type=int)
    ask.add_argument("--stream", action="store_true", help="边生成边打印（SSE）")
    ask.add_argument("--json", action="store_true")

    docs = add("docs", cmd_docs, "列出知识库文档")
    docs.add_argument("kb")
    docs.add_argument("--provider")
    docs.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return int(args.func(args) or EXIT_OK)
    except KbError as exc:
        for line in exc.lines():
            err_console.print(f"[red]{line}[/red]")
        return exc.exit_code
    except KeyboardInterrupt:
        return EXIT_CANCELLED
