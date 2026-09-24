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
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:                       # 只给类型注解用:import 期不拖 qi_agent
    from qi_agent.runtime import QiRuntime

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


def role_prompt_for(cwd: Path | None, name: str | None) -> str:
    """取某个角色的提示词(`name` 为空 → 空串)。没装 qi-agents / 没这个角色 → 空串。

    单独抽出来是因为它有两个调用方:HTTP 端的启动期提示(告诉用户"你钉的角色不存在")
    与 AG-UI 那一轮的 `before_agent_start`。两处必须用同一个判据,否则会出现
    "界面上说在用 X、实际没用" —— 那正是这个功能最容易说谎的地方。
    """
    if not name:
        return ""
    try:
        from qi_agents.discovery import discover
    except ImportError:
        return ""
    role = discover(cwd, "both").get(str(name))
    return str(getattr(role, "prompt", "") or "")


def role_is_project(cwd: Path | None, name: str | None) -> bool:
    """这个角色是不是**项目级**的(仓库控制的提示词,要过信任)。"""
    if not name:
        return False
    try:
        from qi_agents.discovery import discover
    except ImportError:
        return False
    role = discover(cwd, "both").get(str(name))
    return str(getattr(role, "source", "")) == "project"


def pin_role(runtime: QiRuntime, name: str | None) -> None:
    """把"这个客户端钉住的角色"告诉 runtime;首次调用时挂上 `before_agent_start`。

    ## 为什么必须有这一段(而不是把 `agent_override` 传给宿主)

    `runtime.stream(..., agent_override=…)` 是 **P-E4c 之前**的接口:那时 core 里有分派器,
    `agent_override` 决定"这一轮直派谁"。core 收窄成单 agent 之后,那个参数**仍在签名里、
    但流水线里已经没有任何地方读它** —— 传它不报错,只是什么都没发生(静默空转),
    于是 web 界面上的「智能体 chip」成了一个**会说谎**的控件。

    角色现在是 qi-agents 的 `before_agent_start`(E14),所以"钉住"要走同一条路:
    在这里挂一个 handler,把那个角色的正文拼进本轮的系统提示词。

    ## 粒度

    `WebState` 按 cwd 缓存 runtime(一个 runtime 服务一个 cwd),所以钉住值就挂在这个
    runtime 上 —— 同一个 cwd 下的多个浏览器客户端会互相覆盖(取最后一次请求)。
    这与该功能既有的"不落盘、刷新回 auto"口径一致(见 design/web.md §18.24 的四条);
    要做得更细得给每个客户端一条独立通道,那是另一个决定。
    """
    # **同一个 dict 原地改**,不要每次换一个新的:handler 闭包捕获的是这个对象,
    # 换新的等于"第一次钉住之后再也不变"(实测踩过:切回 auto 之后角色提示词还在)。
    pinned: dict[str, str] = getattr(runtime, "_qi_pinned_role", None) or {"name": ""}
    runtime._qi_pinned_role = pinned                       # type: ignore[attr-defined]
    pinned["name"] = (name or "").strip()
    if getattr(runtime, "_qi_role_hook", False):
        return
    bus = getattr(runtime, "bus", None)                    # 替身 runtime 可能没有总线
    if bus is None or not hasattr(bus, "on"):
        return                                             # 没有事件面 → 钉不住,但不该崩
    runtime._qi_role_hook = True                           # type: ignore[attr-defined]
    cwd = getattr(runtime, "cwd", None) or Path.cwd()

    def on_start(payload: dict, ctx: Any) -> Any:
        wanted = pinned["name"]
        if not wanted:
            return None
        # 项目级角色 = 仓库控制的提示词 → 未信任时不注入。qi-agents 里对
        # `--ext agent=<项目角色>` 是同一条闸门;web 的"钉住"是本前端自己的入口,所以在这里。
        if role_is_project(cwd, wanted) and not bool(getattr(ctx, "project_trusted", True)):
            return None
        text = role_prompt_for(cwd, wanted)
        if not text:
            return None
        return {"system_prompt": f"{payload['system_prompt']}\n\n{text}".rstrip()}

    runtime.bus.on("before_agent_start", on_start, source="qi-web")


def serve(argv: list[str]) -> int:
    """起 HTTP 宿主。返回退出码(0 正常,2 用法/环境问题)。"""
    args = _parser().parse_args(argv)        # 坏参数 → argparse 自己退 2(与 qi 的约定一致)

    from .security import is_loopback, require_safe_config

    try:
        import uvicorn

        from .app import create_app
    except ImportError as exc:               # fastapi/uvicorn 是**本扩展**的依赖
        err_console.print(f"[red]缺少 web 依赖:[/red] {escape(str(exc))}")
        err_console.print("  安装: [bold]qi install qi-web[/bold]")
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
