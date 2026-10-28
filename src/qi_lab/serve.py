"""`qi-lab` —— Web UI 的独立 CLI 入口。

qi-lab 是独立应用,不再注册成 core 的 `qi lab` 子命令。参数由本文件自己解析
(argparse 的用法错误退出码正好是 2,与 qi 的约定一致)。

    qi-lab                        # 默认 127.0.0.1:3456
    qi-lab -p 4321 --no-open      # 换端口、不开浏览器
    qi-lab -H 0.0.0.0 --password 口令
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markup import escape

from .agents import discover
from .security import is_loopback

if TYPE_CHECKING:                       # 只给类型注解用:import 期不拖 qi_agent
    from .core import QiRuntime

console = Console()
err_console = Console(stderr=True)


def _browser_host(host: str) -> str:
    """浏览器该访问哪个 host。

    `0.0.0.0` / `::` 只是“监听所有网卡”，不是可访问地址；浏览器在**本机**跑，
    所以统一收回到 `127.0.0.1`。其它非回环地址保留原样（浏览器可能就在那台机器上）。
    """
    host = host.strip()
    if not host or host in {"0.0.0.0", "::", "[::]"} or is_loopback(host):
        return "127.0.0.1"
    return host


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
    parser = argparse.ArgumentParser(prog="qi-lab", description="启动本地 Web UI")
    parser.add_argument("--port", "-p", type=int, default=3456,
                        help="端口(默认 3456,4 位;与 pi-web 的 30141 错开)")
    parser.add_argument("--hostname", "--host", "-H", dest="hostname",
                        default="127.0.0.1",
                        help="绑定地址(默认只回环;绑 0.0.0.0 时建议同时给口令)")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--cwd", default=None, help="默认工作目录(默认当前目录)")
    parser.add_argument("--password", default=None, help="访问口令(可选,也可用 QI_LAB_PASSWORD)")
    parser.add_argument("--list-extensions", action="store_true",
                        help="列出内置扩展状态后退出")
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
        role = discover(cwd, "both").get(str(name))
    except ImportError:
        return ""
    return str(getattr(role, "prompt", "") or "")


def role_is_project(cwd: Path | None, name: str | None) -> bool:
    """这个角色是不是**项目级**的(仓库控制的提示词,要过信任)。"""
    if not name:
        return False
    try:
        role = discover(cwd, "both").get(str(name))
    except ImportError:
        return False
    return str(getattr(role, "source", "")) == "project"


def remote_role_for(cwd: Path | str | None, name: str | None) -> Any:
    """这个角色是不是**远端 A2A 角色**?是就返回它,否则 None。

    ## 为什么这一条单独判,而不是让 `pin_role` 顺手处理

    远端角色**没有本地提示词**(`role.prompt` 是空串)。`pin_role` 的做法是把正文拼进
    系统提示词 —— 对远端角色,那等于什么都没做。于是界面上选了 `skill-debug`、
    chip 也显示着它,而实际跑的还是基座模型:**一个会说谎的控件**,正是这个文件里
    `pin_role` 那段注释已经警告过的那种失败。

    所以"钉的是远端"这一支要**换一条执行路径**(整轮转发给远端),而不是换提示词。
    这也是它必须发生在 `runtime.stream` **之前**的原因。

    判据只看 `qi_agents.discovery` 给出的角色表 —— 与 `/api/agents`、`/agents` 命令
    完全同一份数据,不存在"界面能选、这里认不出"的可能。
    """
    if not name:
        return None
    # `cwd` 在这里可能是 str(`session.cwd` 就存成字符串):角色发现那一层要 `Path`。
    root = Path(cwd) if isinstance(cwd, str) else (cwd or Path.cwd())
    try:
        role = discover(root, "both").get(str(name))
    except ImportError:
        return None
    if role is None:
        return None
    remote = getattr(role, "remote", None)
    return remote if remote is not None else None


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

    钉住值挂在**该会话自己的 runtime** 上:同一个 cwd 下不同会话互不覆盖。
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

    runtime.bus.on("before_agent_start", on_start, source="qi-lab")


def serve(argv: list[str]) -> int:
    """起 HTTP 宿主。返回退出码(0 正常,2 用法/环境问题)。"""
    args = _parser().parse_args(argv)        # 坏参数 → argparse 自己退 2(与 qi 的约定一致)

    try:
        import uvicorn

        from .app import create_app
    except ImportError as exc:               # fastapi/uvicorn 是**本扩展**的依赖
        err_console.print(f"[red]缺少 web 依赖:[/red] {escape(str(exc))}")
        err_console.print("  安装: [bold]uv sync[/bold](或 [bold]pip install -e .[/bold])")
        return 2

    password = args.password or os.environ.get("QI_LAB_PASSWORD") or None

    workdir = Path(args.cwd).expanduser() if args.cwd else Path.cwd()
    if not workdir.is_dir():
        err_console.print(f"[red]工作目录不存在:[/red] {escape(str(workdir))}")
        return 2

    if args.list_extensions:
        from . import bundled

        for row in bundled.status(workdir):
            console.print(f"{row['name']:16} {row['state']:9} {row.get('version') or '-':8} "
                          f"{row['path']}", soft_wrap=True)
        return 0

    allowed = [h for h in os.environ.get("QI_LAB_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not _port_free(args.hostname, args.port):
        err_console.print(f"[red]端口已被占用:[/red] {args.hostname}:{args.port}")
        err_console.print(f"  换个端口: [bold]qi-lab -p {args.port + 1}[/bold]")
        return 2

    app_obj = create_app(cwd=workdir.resolve(), password=password, allowed_hosts=allowed,
                         bind_host=args.hostname)

    shown = _browser_host(args.hostname)
    url = f"http://{shown}:{args.port}"
    console.print(f"[green]qi-lab[/green] → [bold]{url}[/bold]")
    console.print(f"[dim]默认工作目录: {escape(str(workdir))}[/dim]")

    if not args.no_open:
        def _open() -> None:
            with contextlib.suppress(Exception):     # 打不开浏览器不该影响服务
                webbrowser.open(url)

        threading.Timer(0.8, _open).start()

    uvicorn.run(app_obj, host=args.hostname, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    """console script 入口:`qi-lab [参数…]`。"""
    return serve(list(sys.argv[1:] if argv is None else argv))
