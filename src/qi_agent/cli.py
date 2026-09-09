"""CLI 入口(P1:doctor / models / auth / init;其余命令面见 docs/cli.md)。

设计:参数尽量对齐 pi;配置即文件;交互在 TUI。
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .auth import AuthStore, resolve_key
from .config import CONFIG_FILE_NAME, SUPPORTED_PROVIDERS, ConfigError, load_config, require_default_model
from .paths import global_home, project_home

app = typer.Typer(
    name="qi",
    help="多 agent 编码框架:专职角色 + auto 分派(参数尽量对齐 pi)",
    no_args_is_help=True,
)
console = Console()

auth_app = typer.Typer(help="管理 ~/.qi/auth.json 凭证")
app.add_typer(auth_app, name="auth")


@app.command("doctor")
def doctor() -> None:
    """检查配置、模型与凭证(启动诊断)。"""
    exit_code = 0
    try:
        cfg, files = load_config()
    except ConfigError as exc:
        console.print(f"[red]配置错误:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    if files:
        console.print("[green]已装载配置:[/green]")
        for f in files:
            console.print(f"  {f}")
    else:
        console.print("[yellow]未找到任何 qi_agent.toml[/yellow](env 指定 / 项目 .qi / 用户 ~/.qi)")

    try:
        default = require_default_model(cfg)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc

    store = AuthStore()
    rows: list[tuple[str, str, str]] = []
    for name, spec in (("default", default), ("router", cfg.models.router)):
        if spec is None:
            continue
        rk = resolve_key(spec, store)
        status = "[green]OK[/green]" if rk.ok else "[red]缺密钥[/red]"
        rows.append((f"[models.{name}]", f"{spec.provider}/{spec.model}", f"{status}  {rk.describe()}"))
        exit_code = exit_code or (0 if rk.ok else 3)

    table = Table(title="模型")
    table.add_column("用途"); table.add_column("模型"); table.add_column("凭证")
    for row in rows:
        table.add_row(*row)
    console.print(table)

    if exit_code:
        console.print("[yellow]提示:qi auth login <provider> 写入 auth store,或 export 对应环境变量。[/yellow]")
    raise typer.Exit(code=exit_code)


models_app = typer.Typer(help="查看命名模型")
app.add_typer(models_app, name="models")


@models_app.command("list")
def models_list() -> None:
    """列出命名模型(models.default / models.router)与凭证状态。"""
    try:
        cfg, files = load_config()
        default = require_default_model(cfg)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    store = AuthStore()
    table = Table(title="命名模型")
    table.add_column("名称"); table.add_column("provider"); table.add_column("model")
    table.add_column("凭证")
    specs = [("default", default)]
    if cfg.models.router:
        specs.append(("router", cfg.models.router))
    for name, spec in specs:
        rk = resolve_key(spec, store)
        table.add_row(name, spec.provider, spec.model, rk.describe())
    console.print(table)
    if not files:
        console.print("[yellow](未装载任何配置文件,以上可能来自 env 指定文件)[/yellow]")


@app.command("init")
def init(
    global_: bool = typer.Option(False, "--global", "-g", help="写全局 ~/.qi(默认写当前项目 .qi)"),
    force: bool = typer.Option(False, "--force", "-f", help="已存在时覆盖"),
    yes: bool = typer.Option(False, "--yes", "-y", help="全默认,不交互"),
) -> None:
    """首次配置引导:目录 + [models.default/router] + 凭证。"""
    target = global_home() if global_ else project_home()
    config_path = target / CONFIG_FILE_NAME
    if config_path.exists() and not force:
        console.print(f"[yellow]已存在 {config_path};用 -f 覆盖。[/yellow]")
        raise typer.Exit(code=1)

    for sub in ("agents", "plugins", "sessions"):
        (target / sub).mkdir(parents=True, exist_ok=True)

    if not yes:
        console.print("配置执行模型 [models.default]")
    provider = "deepseek"
    if not yes:
        provider = typer.prompt(
            f"provider({', '.join(SUPPORTED_PROVIDERS)})", default="deepseek"
        ).strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            console.print(f"[red]未知 provider {provider}[/red]")
            raise typer.Exit(code=2)
    model = "deepseek-chat" if provider in ("deepseek", "openai") else ("qwen3:8b" if provider == "ollama" else "claude-sonnet-4-5")
    if not yes:
        model = typer.prompt("model 名", default=model)

    lines = [
        "# qi_agent.toml(由 qi init 生成)",
        "[models.default]",
        f'provider = "{provider}"',
        f'model = "{model}"',
    ]
    if provider != "ollama":
        if yes:
            api_key_env = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]
        else:
            api_key_env = typer.prompt(
                "凭证方式:输入环境变量名(留空=存 auth store)", default="", show_default=False
            )
        if api_key_env:
            lines.append(f'api_key_env = "{api_key_env}"')
            console.print(f"[yellow]记得 export {api_key_env}=sk-… [/yellow]")
        else:
            key = getpass.getpass(f"输入 {provider} 的 API key(将写入 ~/.qi/auth.json):")
            if key:
                AuthStore().set_key(provider, key)
                console.print("[green]已写入 auth store(0600)[/green]")
    lines.append("")
    target.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]已生成 {config_path}[/green]")
    console.print("[yellow]下一步:qi doctor 校验配置与凭证。[/yellow]")


@auth_app.command("login")
def auth_login(
    provider: str = typer.Argument(..., help="provider 名(如 deepseek / openai / anthropic)"),
) -> None:
    """交互输入 API key 写入 auth store(0600)。"""
    provider = provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        console.print(f"[red]未知 provider {provider};支持: {', '.join(SUPPORTED_PROVIDERS)}[/red]")
        raise typer.Exit(code=2)
    key = getpass.getpass(f"输入 {provider} API key:")
    if not key:
        console.print("[red]未输入,取消。[/red]")
        raise typer.Exit(code=1)
    store = AuthStore()
    store.set_key(provider, key)
    console.print(f"[green]已保存 {provider} 到 {store.path}(0600)[/green]")


@auth_app.command("logout")
def auth_logout(provider: str = typer.Argument(...)) -> None:
    """删除某 provider 的凭证。"""
    store = AuthStore()
    if store.remove(provider.strip().lower()):
        console.print(f"[green]已删除 {provider}[/green]")
    else:
        console.print(f"[yellow]{provider} 无凭证。[/yellow]")


@auth_app.command("list")
def auth_list() -> None:
    """列出已存 provider(不回显 key)。"""
    store = AuthStore()
    providers = store.providers()
    if not providers:
        console.print("(auth store 为空)")
        return
    for p in providers:
        console.print(p)


@app.command("version")
def version() -> None:
    """显示版本。"""
    console.print(f"qi {__version__}")


def main() -> None:
    app()
