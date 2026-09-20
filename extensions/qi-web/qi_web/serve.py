"""`qi web` —— qi-web 自己的 CLI 入口(P-E5 ③:core 一点不留)。

core 只提供"扩展注册 CLI 子命令"这个面(`registerCliCommand`),约定是:handler 收**命令名
之后的原始 argv**,返回退出码。**故意不接 typer/click 的解析结果** —— 选项表是静态的,各扩展
自己解析自己的参数(E19 的实测结论)。这里用 argparse:它的用法错误退出码正好是 2,与 qi 的
约定一致。

    qi web                        # 默认 127.0.0.1:30142
    qi web -p 3456 --no-open      # 换端口、不开浏览器
    qi web -H 0.0.0.0 --password 口令

(这个文件从 `qi_agent/cli.py` 的 `web_cmd` 搬来 —— 那一份已随"core 一点不留"删掉。)
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import threading
import webbrowser
from pathlib import Path

from rich.console import Console
from rich.markup import escape

console = Console()
err_console = Console(stderr=True)


def _port_free(host: str, port: int) -> bool:
    """端口能不能绑上。绑定失败 = 被占(顺手当"能不能用"的探针)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qi web", description="启动本地 Web UI")
    parser.add_argument("--port", "-p", type=int, default=30142,
                        help="端口(默认 30142,与 pi-web 的 30141 错开)")
    parser.add_argument("--hostname", "-H", default="127.0.0.1",
                        help="绑定地址(默认只回环)")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--cwd", default=None, help="默认工作目录(默认当前目录)")
    parser.add_argument("--password", default=None, help="访问口令(也可用 QI_WEB_PASSWORD)")
    return parser


def serve(argv: list[str]) -> int:
    """起 HTTP 宿主。返回退出码(0 正常,2 用法/环境问题)。"""
    args = _parser().parse_args(argv)        # 坏参数 → argparse 自己退 2(与 qi 的约定一致)

    from .security import is_loopback, require_safe_config

    try:
        import uvicorn

        from .app import create_app
    except ImportError as exc:               # fastapi/uvicorn 是**本扩展**的依赖
        err_console.print(f"[red]缺少 web 依赖:[/red] {escape(str(exc))}")
        err_console.print("  安装: [bold]pip install qi-web[/bold]")
        return 2

    password = args.password or os.environ.get("QI_WEB_PASSWORD") or None
    # 不安全的组合直接拒启(跨回环 + 无口令),见 security.py
    require_safe_config(args.hostname, password)

    workdir = Path(args.cwd).expanduser() if args.cwd else Path.cwd()
    if not workdir.is_dir():
        err_console.print(f"[red]工作目录不存在:[/red] {escape(str(workdir))}")
        return 2

    allowed = [h for h in os.environ.get("QI_WEB_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not _port_free(args.hostname, args.port):
        err_console.print(f"[red]端口已被占用:[/red] {args.hostname}:{args.port}")
        err_console.print(f"  换个端口: [bold]qi web -p {args.port + 1}[/bold]")
        return 2

    app_obj = create_app(cwd=workdir.resolve(), password=password, allowed_hosts=allowed,
                         bind_host=args.hostname)

    shown = "127.0.0.1" if is_loopback(args.hostname) else args.hostname
    url = f"http://{shown}:{args.port}"
    console.print(f"[green]qi web[/green] → [bold]{url}[/bold]")
    console.print(f"[dim]默认工作目录: {escape(str(workdir))}[/dim]")

    if not args.no_open:
        def _open() -> None:
            with contextlib.suppress(Exception):     # 打不开浏览器不该影响服务
                webbrowser.open(url)

        threading.Timer(0.8, _open).start()

    uvicorn.run(app_obj, host=args.hostname, port=args.port, log_level="info")
    return 0


def register(api) -> None:
    """把自己接到宿主的 CLI 上:`qi web [参数…]`。"""
    api.registerCliCommand("web", serve, description="启动本地 Web UI")
