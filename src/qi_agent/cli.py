"""CLI 入口(对齐 docs/cli.md;P1-P6)。

顶层:qi [options] [--] [消息...]  →  -p 无头执行(auto);否则提示/进 TUI(v2 在 cli 不可用时提示)
子命令:doctor / models list / auth / init / agents / sessions / version
"""

from __future__ import annotations

import asyncio
import getpass
import shutil
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .auth import AuthStore, resolve_key
from .config import CONFIG_FILE_NAME, SUPPORTED_PROVIDERS, ConfigError, load_config, require_default_model
from .loader import LoadError, load_agent_dir, load_all_agents, scan_agent_dirs
from .paths import global_home, project_home
from .registry import AgentRegistry, CapabilityRegistry, ToolCatalog, discover_plugins
from .session import SessionStore
from .tools import register_builtin_tools

console = Console()
app = typer.Typer(
    name="qi",
    help="多 agent 编码框架:专职角色 + auto 分派(参数尽量对齐 pi)",
    no_args_is_help=True,
    context_settings={"allow_extra_args": True, "help_option_names": ["-h", "--help"]},
)


def _env_catalog() -> tuple[ToolCatalog, list[str]]:
    """内置工具 + 已发现插件(catalog 名字,用于装载校验/import)。"""
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    caps = CapabilityRegistry()
    plugins = discover_plugins(catalog, caps)
    return catalog, plugins


def _load_registry(catalog: ToolCatalog) -> AgentRegistry:
    units = load_all_agents(catalog_names=catalog.names)
    reg = AgentRegistry()
    reg.register_all(units)
    return reg


def _print_agents_table(reg: AgentRegistry, catalog: ToolCatalog | None = None) -> None:
    table = Table(title=f"agents({len(reg.names)})")
    table.add_column("name"); table.add_column("来源"); table.add_column("工具")
    table.add_column("描述")
    for unit in reg.all():
        tools = ",".join(unit.tools) if unit.tools else "(全部)"
        desc = unit.config.description.splitlines()[0] if unit.config.description else ""
        table.add_row(unit.name, unit.source, tools, desc[:60])
    console.print(table)


# ── 顶层 callback:headless 运行 ─────────────────────────

@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    print_mode: bool = typer.Option(False, "--print", "-p", help="无头一次执行(auto 分派)"),
    agent: str | None = typer.Option(None, "--agent", help="指定 agent(manual)"),
    cont: bool = typer.Option(False, "--continue", "-c", help="续上次会话"),
    session_id: str | None = typer.Option(None, "--session", help="指定会话 id"),
    name: str | None = typer.Option(None, "--name", "-n", help="会话显示名"),
    no_session: bool = typer.Option(False, "--no-session", help="不落盘(临时)"),
    export_file: str | None = typer.Option(None, "--export", help="导出会话 JSONL 到文件后退出"),
    mode: str = typer.Option("text", "--mode", help="输出: text|json"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if export_file:
        _cmd_export(session_id or "", Path(export_file))
        raise typer.Exit()
    messages = [m for m in ctx.args if not m.startswith("@") or True]
    if not messages and not print_mode:
        console.print("交互界面(TUI)开发中;无头用法: qi -p \"问题\" [--agent name]")
        raise typer.Exit(code=0)
    prompt = " ".join(messages).strip() or ""
    if not prompt and print_mode:
        console.print("[red]需要消息内容: qi -p \"问题\"[/red]")
        raise typer.Exit(code=2)
    from .runtime import QiRuntime, RuntimeConfig
    from .config import ConfigError as _CfgErr
    from .loader import LoadError as _LoadErr
    try:
        runtime = QiRuntime()
    except _LoadErr as exc:
        console.print(f"[red]装载失败:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    except _CfgErr as exc:
        console.print(f"[red]配置错误:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    store = runtime.sessions
    session = None
    if no_session:
        session = store.create(name or "ephemeral")
    elif session_id:
        session = store.get(session_id)
        if session is None:
            console.print(f"[red]会话不存在: {session_id}[/red]")
            raise typer.Exit(code=1)
    elif cont:
        session = store.latest()
        if session is None:
            console.print("[yellow]无历史会话,新建。[/yellow]")
            session = store.create(name or "")
    else:
        session = store.create(name or prompt[:30])
    assert session is not None

    async def _run() -> None:
        async for ev in runtime.stream(prompt, session, agent_override=agent):
            if mode == "json":
                console.print_json(data={"kind": ev.kind, "agent": ev.agent,
                                         "tool": ev.tool, "text": ev.text, "data": ev.data})
            elif ev.kind == "dispatch":
                console.print(f"[cyan]→ {escape(ev.text)}[/cyan]")
            elif ev.kind == "tool_start":
                console.print(f"[dim]  ⚙ {ev.tool}({escape(str(ev.data.get('args', {})))[:120]})[/dim]")
            elif ev.kind == "tool_end":
                snippet = ev.text[:200].replace("\n", " ")
                console.print(f"[dim]  ↳ {escape(snippet)}[/dim]")
            elif ev.kind == "text" and ev.text:
                console.print(ev.text)
            elif ev.kind == "error":
                console.print(f"[red]{escape(ev.text)}[/red]")
            elif ev.kind == "opening":
                console.print(f"[bold]{escape(ev.text)}[/bold]")

    asyncio.run(_run())


def _cmd_export(session_id: str, out: Path) -> None:
    store = SessionStore()
    session = store.get(session_id) if session_id else store.latest()
    if session is None:
        console.print("[red]无可导出会话[/red]")
        raise typer.Exit(code=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(session.path, out)
    console.print(f"[green]已导出 {session.path} → {out}[/green]")


# ── doctor ──────────────────────────────────────────────

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
    table = Table(title="模型")
    table.add_column("用途"); table.add_column("模型"); table.add_column("凭证")
    for name_, spec in (("default", default), ("router", cfg.models.router)):
        if spec is None:
            continue
        rk = resolve_key(spec, store)
        ok = rk.ok
        table.add_row(f"[models.{name_}]", f"{spec.provider}/{spec.model}",
                      f"{'[green]OK[/green]' if ok else '[red]缺密钥[/red]'}  {rk.describe()}")
        if not ok:
            exit_code = 3
    console.print(table)
    if exit_code:
        console.print("[yellow]提示:qi auth login <provider> 写入 auth store,或 export 对应环境变量。[/yellow]")
    raise typer.Exit(code=exit_code)


# ── models ──────────────────────────────────────────────

models_app = typer.Typer(help="查看命名模型")
app.add_typer(models_app, name="models")


@models_app.command("list")
def models_list() -> None:
    """列出命名模型(models.default / models.router)与凭证状态。"""
    try:
        cfg, _files = load_config()
        default = require_default_model(cfg)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    store = AuthStore()
    table = Table(title="命名模型")
    table.add_column("名称"); table.add_column("provider"); table.add_column("model"); table.add_column("凭证")
    for name_, spec in (("default", default), ("router", cfg.models.router)):
        if spec is None:
            continue
        rk = resolve_key(spec, store)
        table.add_row(name_, spec.provider, spec.model, rk.describe())
    console.print(table)


# ── agents ──────────────────────────────────────────────

agents_app = typer.Typer(help="agent 查看/导入/导出")
app.add_typer(agents_app, name="agents")


@agents_app.command("list")
def agents_list() -> None:
    """列出已装载 agent(来源/工具/描述)。"""
    catalog, _plugins = _env_catalog()
    reg = _load_registry(catalog)
    _print_agents_table(reg, catalog)


@agents_app.command("show")
def agents_show(name: str = typer.Argument(...)) -> None:
    """单 agent 解析诊断。"""
    catalog, _plugins = _env_catalog()
    reg = _load_registry(catalog)
    unit = reg.get(name)
    if unit is None:
        console.print(f"[red]agent {name} 不存在[/red]")
        raise typer.Exit(code=1)
    console.print(f"[bold]{unit.name}[/bold](来源: {unit.source}) 目录: {unit.path}")
    console.print(f"描述: {unit.config.description}")
    console.print(f"工具: {', '.join(unit.tools) if unit.tools else '(全部)'}")
    console.print(f"技能: {', '.join(s.name for s in unit.skills) or '(无)'}")
    if unit.data_sources:
        console.print("数据源: " + ", ".join(f"{d.id}({d.type})" for d in unit.data_sources))
    if unit.mcp_private:
        console.print("私有 MCP: " + ", ".join(s.name for s in unit.mcp_private))
    if unit.config.opening:
        console.print(f"opening: {unit.config.opening.message[:60]}")


@agents_app.command("import")
def agents_import(source: str = typer.Argument(...),
                  local: bool = typer.Option(False, "--local", "-l", help="导入到项目 .qi"),
                  force: bool = typer.Option(False, "--force", help="覆盖同名"),
                  rename: str | None = typer.Option(None, "--rename", help="改目录名导入")) -> None:
    """导入 agent:拷贝 + 复用装载校验器 + 明文凭证扫描。"""
    src = Path(source).expanduser()
    if src.is_file() and src.name == "agent.md":
        src = src.parent
    if not src.is_dir():
        console.print(f"[red]源不是 agent 目录: {src}[/red]")
        raise typer.Exit(code=2)
    catalog, _plugins = _env_catalog()
    try:
        load_agent_dir(src, "source", catalog.names)   # 先校验(坏包不放行)
    except LoadError as exc:
        console.print(f"[red]导入校验失败:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    target_root = project_home() if local else global_home()
    dst_name = rename or src.name
    dst = target_root / "agents" / dst_name
    if dst.exists() and not force:
        console.print(f"[yellow]目标已存在 {dst};--force 覆盖或 --rename 改名。[/yellow]")
        raise typer.Exit(code=1)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=force)
    console.print(f"[green]已导入 {src} → {dst}[/green]")


@agents_app.command("export")
def agents_export(name: str = typer.Argument(...),
                  out: str = typer.Option(".", "-o", help="输出目录")) -> None:
    """导出 agent 目录(产物可直接 import)。"""
    catalog, _plugins = _env_catalog()
    reg = _load_registry(catalog)
    unit = reg.get(name)
    if unit is None:
        console.print(f"[red]agent {name} 不存在[/red]")
        raise typer.Exit(code=1)
    dst = Path(out).expanduser() / unit.name
    shutil.copytree(unit.path, dst, dirs_exist_ok=True)
    console.print(f"[green]已导出 → {dst}[/green]")


# ── sessions ────────────────────────────────────────────

sessions_app = typer.Typer(help="会话管理")
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("list")
def sessions_list() -> None:
    """列出会话(最新在前)。"""
    store = SessionStore()
    for s in store.list():
        console.print(f"{s.id}  {s.title or '(无标题)'}  消息{s.message_count}  {s.created_at}")


@sessions_app.command("show")
def sessions_show(session_id: str = typer.Argument(...)) -> None:
    """查看会话内容。"""
    store = SessionStore()
    s = store.get(session_id)
    if s is None:
        console.print(f"[red]会话不存在: {session_id}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[bold]{s.id}[/bold] {s.title}  {s.path}")
    for e in s.entries:
        if e.get("type") == "message":
            console.print(f"[dim]{e.get('role')}[/dim] {e.get('agent_id', '')}: {escape(str(e.get('content','')))[:200]}")
        elif e.get("type") == "dispatch":
            console.print(f"[cyan]dispatch[/cyan] → {e.get('agent')} ({e.get('source')}, {e.get('confidence')}) {e.get('reasoning','')}")


@sessions_app.command("rm")
def sessions_rm(session_id: str = typer.Argument(...)) -> None:
    """删除会话。"""
    store = SessionStore()
    if store.delete(session_id):
        console.print(f"[green]已删除 {session_id}[/green]")
    else:
        console.print(f"[yellow]会话不存在: {session_id}[/yellow]")
        raise typer.Exit(code=1)


# ── auth / init / version ───────────────────────────────

auth_app = typer.Typer(help="管理 ~/.qi/auth.json 凭证")
app.add_typer(auth_app, name="auth")


@auth_app.command("login")
def auth_login(provider: str = typer.Argument(...)) -> None:
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
    store = AuthStore()
    if store.remove(provider.strip().lower()):
        console.print(f"[green]已删除 {provider}[/green]")
    else:
        console.print(f"[yellow]{provider} 无凭证。[/yellow]")


@auth_app.command("list")
def auth_list() -> None:
    store = AuthStore()
    providers = store.providers()
    if not providers:
        console.print("(auth store 为空)")
        return
    for p in providers:
        console.print(p)


@app.command("init")
def init(
    global_: bool = typer.Option(False, "--global", "-g", help="写全局 ~/.qi(默认当前项目 .qi)"),
    force: bool = typer.Option(False, "--force", "-f", help="已存在时覆盖"),
    yes: bool = typer.Option(False, "--yes", "-y", help="全默认,不交互"),
) -> None:
    target = global_home() if global_ else project_home()
    config_path = target / CONFIG_FILE_NAME
    if config_path.exists() and not force:
        console.print(f"[yellow]已存在 {config_path};用 -f 覆盖。[/yellow]")
        raise typer.Exit(code=1)
    for sub in ("agents", "plugins", "sessions"):
        (target / sub).mkdir(parents=True, exist_ok=True)
    provider = "deepseek"
    if not yes:
        provider = typer.prompt(f"provider({', '.join(SUPPORTED_PROVIDERS)})", default="deepseek")
    provider = provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        console.print(f"[red]未知 provider {provider}[/red]")
        raise typer.Exit(code=2)
    model = "deepseek-chat" if provider in ("deepseek", "openai") else "qwen3:8b"
    if not yes:
        model = typer.prompt("model 名", default=model)
    lines = ["# qi_agent.toml(由 qi init 生成)", "[models.default]",
             f'provider = "{provider}"', f'model = "{model}"']
    if provider != "ollama":
        api_key_env = ""
        if not yes:
            api_key_env = typer.prompt("凭证方式:输入环境变量名(留空=存 auth store)", default="",
                                       show_default=False)
        if api_key_env:
            lines.append(f'api_key_env = "{api_key_env}"')
            console.print(f"[yellow]记得 export {api_key_env}=sk-…[/yellow]")
        else:
            key = getpass.getpass(f"输入 {provider} 的 API key(将写入 auth store):")
            if key:
                AuthStore().set_key(provider, key)
                console.print("[green]已写入 auth store(0600)[/green]")
    lines.append("")
    target.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[green]已生成 {config_path}[/green]")
    console.print("[yellow]下一步:qi doctor 校验。[/yellow]")


@app.command("version")
def version() -> None:
    """显示版本。"""
    console.print(f"qi {__version__}")


def main() -> None:
    app()
