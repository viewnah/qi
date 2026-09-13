"""CLI 入口(对齐 docs/cli.md;P1-P6)。

顶层:qi [options] [--] [消息...]  →  -p 无头执行(auto);否则提示/进 TUI(v2 在 cli 不可用时提示)
子命令:doctor / models list / auth / init / agents / sessions / version
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from typer.core import TyperGroup

from . import __version__, paths, prompt
from .auth import AuthStore, DEFAULT_API_KEY_ENV, resolve_key
from .config import (
    DEFAULT_API,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_TOKENS,
    SUPPORTED_APIS,
    ConfigError,
    default_model_spec,
    legacy_default_keys,
    load_config,
    load_models_file,
    resolve_default_model,
    resolve_model,
    resolve_router_model,
    save_models_file,
)
from .loader import (
    LoadError,
    load_agent_dir,
    load_all_agents,
    load_top_level_skills,
    scan_agent_dirs,
)
from .models import AgentUnit
from .paths import MODELS_FILE_NAME, SETTINGS_FILE_NAME, global_home, project_home
from .registry import AgentRegistry, CapabilityRegistry, ToolCatalog, discover_plugins
from .session import SessionStore
from .settings import (
    SCOPES,
    QiSettings,
    SettingsError,
    load_settings,
    load_settings_by_scope,
    load_settings_raw,
    parse_value,
    save_settings,
    set_value,
    settings_scope_path,
    unset_value,
)
from .tools import register_builtin_tools

console = Console(highlight=False)
# 诊断/进度(分派、工具调用)走 stderr:`qi -p "..." > out.txt` 只会得到答案本身,
# 脚本不必过滤装饰行;终端里仍看得到。答案正文依旧走 stdout。
err_console = Console(stderr=True, highlight=False)


class QiGroup(TyperGroup):
    """顶层群组:位置参数何时算“消息”、何时算“子命令”。

    Click 的 Group 会把第一个位置参数无条件当子命令名解析,于是:
      - `qi -p "你好"` → No such command '你好'(docs/cli.md §1)
      - `qi -p "version"` → 消息恰好同名子命令时,跑去了 `qi version`
    规则:`-p/--print` 已给出 —— 位置参数全是消息;否则首个位置参数不是已注册
    子命令时,也当消息(交给顶层 callback 拼成 prompt)。
    """

    def parse_args(self, ctx, args: list[str]) -> list[str]:
        super().parse_args(ctx, args)
        # TyperGroup.parse_args 已把首个位置参数挪进 _protected_args(当子命令)。
        if not ctx._protected_args:
            return ctx.args
        if ctx.params.get("print_mode") or self.get_command(ctx, ctx._protected_args[0]) is None:
            ctx.args = [*ctx._protected_args, *ctx.args]
            ctx._protected_args = []
        return ctx.args


app = typer.Typer(
    name="qi",
    cls=QiGroup,
    help="多 agent 编码框架:专职角色 + auto 分派(参数尽量对齐 pi)",
    no_args_is_help=False,  # 无参 → 进 TUI(见 callback);帮助用 qi -h
    context_settings={"allow_extra_args": True, "help_option_names": ["-h", "--help"]},
)


def _env_catalog() -> tuple[ToolCatalog, list[str]]:
    """内置工具 + 已发现插件(catalog 名字,用于装载校验/import)。"""
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    caps = CapabilityRegistry()
    plugins = discover_plugins(catalog, caps)
    return catalog, plugins


def _load_registry(catalog: ToolCatalog, cwd: Path | None = None) -> AgentRegistry:
    """装载 agent(含顶层技能);`qi agents list/show` 与 TUI 共用。"""
    try:
        settings, _files = load_settings(cwd)
        top_skills = load_top_level_skills(cwd, settings, enabled=settings.skillsEnabled)
    except SettingsError:
        top_skills = None      # settings 坏了不阻止列出 agent;`qi config` / doctor 会报细节
    try:
        units = load_all_agents(cwd, catalog_names=catalog.names, ds_types=set(),
                                extra_skills=top_skills)
    except LoadError as exc:
        console.print(f"[red]装载失败:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    reg = AgentRegistry()
    reg.register_all(units)
    return reg


def _display_name(unit: AgentUnit) -> str:
    """UI 显示名;未设 display_name 时回落为 "-"(`name` 已在单独一列)。"""
    return (unit.config.display_name or "").strip() or "-"


def _replay_names(entries: list[dict]) -> dict[str, str]:
    """会话回放用映射:agent 名 → 记录时的展示名。

    展示名在写入 dispatch entry 时一并落盘(而非回放时查 registry),因为:
      - 历史会话应反映**当时**的展示名,不受之后改名/删 agent 影响;
      - 回放不必装载 agent/插件(无副作用)。
    旧会话没记录 display_name 时回落为 name。
    """
    names: dict[str, str] = {}
    for e in entries:
        if e.get("type") == "dispatch" and e.get("agent"):
            names[str(e["agent"])] = str(e.get("display_name") or e["agent"])
    return names


def _print_agents_table(reg: AgentRegistry, catalog: ToolCatalog | None = None) -> None:
    table = Table(title=f"agents({len(reg.names)})")
    table.add_column("name"); table.add_column("显示名"); table.add_column("来源")
    table.add_column("工具"); table.add_column("描述")
    for unit in reg.all():
        tools = ",".join(unit.tools) if unit.tools else "(全部)"
        desc = unit.config.description.splitlines()[0] if unit.config.description else ""
        table.add_row(unit.name, _display_name(unit), unit.source, tools, desc[:60])
    console.print(table)


def _tool_snippet(text: str, limit: int = 400) -> str:
    """工具结果摘要:保留换行结构(便于看 ls/grep 这类多行输出),超长时附提示。

    不把 `\n` 压成空格 —— 那样 ls 的“一行一条目”会糊成不可读的长串。
    """
    text = text.rstrip()
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    head = text[:limit].rstrip()
    return f"{head}\n  …(已截断,共 {len(lines)} 行 / {len(text)} 字)"


def _short_cwd(cwd: str | None, limit: int = 48) -> str:
    """会话 cwd 的终端友好形式。

    家目录缩为 `~`;仍过长则只保留末两段(`…/parent/name`)——否则长路径会把
    `sessions list` 的一行挤成三行。缺失时为“(未知)”(v0.1 旧会话)。
    """
    if not cwd:
        return "(未知)"
    home = str(Path.home())
    text = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    if len(text) <= limit:
        return text
    parts = Path(cwd).parts
    return "…/" + "/".join(parts[-2:]) if len(parts) >= 2 else text


# ── 顶层 callback:headless 运行 ─────────────────────────

@app.callback(invoke_without_command=True)
def root_callback(
    ctx: typer.Context,
    print_mode: bool = typer.Option(False, "--print", "-p", help="无头一次执行(auto 分派)"),
    agent: str | None = typer.Option(None, "--agent", help="指定 agent(manual)"),
    cont: bool = typer.Option(False, "--continue", "-c", help="续上次会话"),
    session_id: str | None = typer.Option(None, "--session", help="指定会话 id"),
    name: str | None = typer.Option(None, "--name", "-n", help="会话显示名"),
    no_session: bool = typer.Option(False, "--no-session", help="不落盘(临时)"),
    export_file: str | None = typer.Option(None, "--export", help="导出会话 JSONL 到文件后退出"),
    mode: str = typer.Option("text", "--mode", help="输出: text|json"),
    verbose: bool = typer.Option(False, "--verbose", help="显示分派与工具调用进度(默认只输出答案,对齐 pi)"),
    skill: list[str] = typer.Option(None, "--skill", help="额外技能文件/目录(可重复;叠加)"),
    no_skills: bool = typer.Option(False, "--no-skills", "-ns", help="关闭技能自动发现(--skill 仍生效)"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if export_file:
        _cmd_export(session_id or "", Path(export_file))
        raise typer.Exit()
    messages = list(ctx.args)
    if not messages and not print_mode:
        # 无参:进了。(docs/cli.md §1:无 -p = TUI);非 TTY(管道/CI)退化为提示。
        if sys.stdin.isatty() and sys.stdout.isatty():
            tui()
        else:
            console.print("交互界面需要 TTY;无头用法: qi -p \"问题\" [--agent name]")
        raise typer.Exit(code=0)
    prompt = " ".join(messages).strip() or ""
    if not prompt and print_mode:
        console.print("[red]需要消息内容: qi -p \"问题\"[/red]")
        raise typer.Exit(code=2)
    from .runtime import QiRuntime, RuntimeConfig
    from .config import ConfigError as _CfgErr
    from .loader import LoadError as _LoadErr
    try:
        runtime = QiRuntime(skills_enabled=not no_skills,
                            extra_skill_paths=[Path(p) for p in (skill or [])])
    except _LoadErr as exc:
        console.print(f"[red]装载失败:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    except _CfgErr as exc:
        console.print(f"[red]配置错误:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    store = runtime.sessions
    session = None
    if no_session:
        session = store.create(name or "ephemeral", cwd=runtime.cwd)
    elif session_id:
        session = store.get(session_id)
        if session is None:
            console.print(f"[red]会话不存在: {session_id}[/red]")
            raise typer.Exit(code=1)
    elif cont:
        session = store.latest()
        if session is None:
            console.print("[yellow]无历史会话,新建。[/yellow]")
            session = store.create(name or "", cwd=runtime.cwd)
    else:
        session = store.create(name or prompt[:30], cwd=runtime.cwd)
    assert session is not None

    async def _run() -> None:
        # 对齐 pi 的 `-p`:默认只输出答案;分派行/工具进度仅在 --verbose 时显示
        # (且走 stderr,不污染 stdout)。`--mode json` 本就输出全部事件,不受此开关影响。
        async for ev in runtime.stream(prompt, session, agent_override=agent):
            if mode == "json":
                console.print_json(data={"kind": ev.kind, "agent": ev.agent,
                                         "tool": ev.tool, "text": ev.text, "data": ev.data})
            elif ev.kind == "text" and ev.text:
                console.print(ev.text)
            elif ev.kind == "error":
                err_console.print(f"[red]{escape(ev.text)}[/red]")
            elif verbose and ev.kind == "dispatch":
                err_console.print(f"[cyan]→ {escape(ev.text)}[/cyan]")
            elif verbose and ev.kind == "tool_start":
                args = json.dumps(ev.data.get("args", {}), ensure_ascii=False)
                err_console.print(f"[dim]  ⚙ {ev.tool} {escape(args[:200])}[/dim]")
            elif verbose and ev.kind == "tool_end":
                data = ev.data or {}
                mark = "✓" if data.get("status") == "ok" else "✗"
                ms = data.get("duration_ms")
                cost = f" {ms}ms" if isinstance(ms, int) else ""
                err_console.print(f"[dim]  ↳ {mark}{cost} {escape(_tool_snippet(ev.text))}[/dim]")
            elif verbose and ev.kind == "opening":
                err_console.print(f"[bold]{escape(ev.text)}[/bold]")

    asyncio.run(_run())


def _cmd_export(session_id: str, out: Path) -> None:
    store = SessionStore()
    session = store.get(session_id) if session_id else store.latest()
    if session is None:
        console.print("[red]无可导出会话[/red]")
        raise typer.Exit(code=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy(session.path, out)
    except OSError as exc:
        console.print(f"[red]导出失败: {escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
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
        console.print(f"[yellow]未找到任何 {MODELS_FILE_NAME}[/yellow](env 指定 / 项目 .qi / 用户 ~/.qi/agent)")

    try:
        _settings, settings_files = load_settings()
    except SettingsError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        settings_files = []
    if settings_files:
        console.print(f"[green]已装载 {SETTINGS_FILE_NAME}:[/green]")
        for f in settings_files:
            console.print(f"  {f}")
    else:
        console.print(f"[dim]无 {SETTINGS_FILE_NAME}(可选;默认模型可写在里面)[/dim]")

    store = AuthStore()
    if cfg.providers:
        table = Table(title="providers")
        table.add_column("provider"); table.add_column("baseUrl")
        table.add_column("api"); table.add_column("模型"); table.add_column("凭证")
        for name_, prov in cfg.providers.items():
            rk = resolve_key(name_, prov.apiKey, store)
            table.add_row(name_, prov.baseUrl or "(内置)", prov.api or DEFAULT_API,
                          str(len(prov.models)),
                          f"{'[green]OK[/green]' if rk.ok else '[red]缺密钥[/red]'}  {rk.describe()}")
        console.print(table)

    try:
        default = resolve_default_model(cfg)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    rk = resolve_key(default.provider, default.api_key_ref, store)
    _provider, _model, model_source = default_model_spec()
    console.print(f"[dim]默认模型来源: {model_source}[/dim]")
    for path, legacy_provider, legacy_model in legacy_default_keys():
        err_console.print(
            f"[yellow]警告:[/yellow] {path} 里的 defaultProvider/defaultModel 已不再读取。\n"
            f"  迁移: qi config --set defaultProvider={legacy_provider or '<name>'} "
            f"--set defaultModel={legacy_model or '<id>'},然后删掉该文件里的这两行。"
        )
    console.print(f"[bold]默认模型:[/bold] {default.label}  "
                  f"api={default.api}  ctx={default.context_window}  max={default.max_tokens}  "
                  f"reasoning={default.reasoning}  "
                  f"{'[green]凭证 OK[/green]' if rk.ok else '[red]缺密钥[/red]'} {rk.describe()}")
    if not rk.ok:
        exit_code = 3
    router = resolve_router_model(cfg)
    if router.label != default.label:
        console.print(f"[bold]分派模型:[/bold] {router.label}")
    if exit_code:
        console.print(f"[yellow]提示:qi auth login <provider> 写入 auth store,或在 {MODELS_FILE_NAME} 的 provider 配 apiKey。[/yellow]")
    raise typer.Exit(code=exit_code)


# ── models ──────────────────────────────────────────────

models_app = typer.Typer(help="查看命名模型")
app.add_typer(models_app, name="models")


@models_app.command("list")
def models_list() -> None:
    """列出 models.json 中的 provider/模型与默认模型。"""
    try:
        cfg, _files = load_config()
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    store = AuthStore()
    provider, model, _source = default_model_spec()
    default_label = f"{provider}/{model}" if provider and model else None
    table = Table(title="模型")
    table.add_column("默认"); table.add_column("provider"); table.add_column("模型")
    table.add_column("api"); table.add_column("ctx"); table.add_column("credential")
    shown: set[str] = set()
    for name_, prov in cfg.providers.items():
        rk = resolve_key(name_, prov.apiKey, store)
        for m in prov.models:
            label = f"{name_}/{m.id}"
            shown.add(label)
            table.add_row("*" if label == default_label else "", name_, m.id,
                          m.api or prov.api or DEFAULT_API, str(m.contextWindow), rk.describe())
    if default_label and default_label not in shown and provider and model:
        spec = resolve_model(cfg, provider, model)
        rk = resolve_key(spec.provider, spec.api_key_ref, store)
        table.add_row("*", spec.provider, spec.model, spec.api, str(spec.context_window), rk.describe())
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
    console.print(f"显示名: {_display_name(unit)}")
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
        console.print(f"{s.id}  {s.title or '(无标题)'}  消息{s.message_count}  "
                      f"{_short_cwd(s.cwd)}  {s.created_at}")


@sessions_app.command("show")
def sessions_show(session_id: str = typer.Argument(...)) -> None:
    """查看会话内容(分派与说话人用记录时的展示名,与 agents list 一致)。"""
    store = SessionStore()
    s = store.get(session_id)
    if s is None:
        console.print(f"[red]会话不存在: {session_id}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[bold]{s.id}[/bold] {s.title}")
    console.print(f"[dim]cwd: {escape(_short_cwd(s.cwd, limit=64))}  文件: {escape(str(s.path))}[/dim]")
    names = _replay_names(s.entries)
    for e in s.entries:
        if e.get("type") == "message":
            role = e.get("role")
            body = escape(str(e.get('content', '')))[:200]
            if role == "user":
                # 用户消息不拄说话人名(agent_id 只是"将处理它的 agent",不是发言者)
                console.print(f"[dim]{role}[/dim] {body}")
            else:
                who = names.get(str(e.get("agent_id", "")), e.get("agent_id", ""))
                console.print(f"[dim]{role}[/dim] {who}: {body}")
        elif e.get("type") == "dispatch":
            who = e.get("display_name") or e.get("agent")
            console.print(f"[cyan]dispatch[/cyan] → {who} ({e.get('source')}, {e.get('confidence')}) {e.get('reasoning','')}")
        elif e.get("type") == "tool":
            mark = "✓" if e.get("status") == "ok" else "✗"
            ms = e.get("duration_ms")
            cost = f" {ms}ms" if isinstance(ms, int) else ""
            code = e.get("exit_code")
            tail = f" exit={code}" if isinstance(code, int) and code else ""
            args = json.dumps(e.get("args") or {}, ensure_ascii=False)
            console.print(f"[dim]tool[/dim] {mark} {e.get('tool')}{cost}{tail} "
                          f"[dim]{escape(args[:120])}[/dim]")
        elif e.get("type") == "custom" and e.get("custom_type") == "assistant_narration":
            # 工具调用**之前**的叙述:直播时走 text_delta,回放时必须同位置重现,
            # 否则 CLI 回放与实时看到的内容不一致(见 web.md §13 的顺序不变量)
            who = names.get(str(e.get("agent", "")), e.get("agent", ""))
            console.print(f"[dim]叙述[/dim] {who}: {escape(str(e.get('content', ''))[:200])}")


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

auth_app = typer.Typer(help="管理 ~/.qi/agent/auth.json 凭证")
app.add_typer(auth_app, name="auth")


class _AuthError(Exception):
    """对齐 pi 的 AuthCommandError:消息直接讲原因,退出码由调用处定。"""


def _auth_target(provider: str | None, model: str | None) -> tuple[str, str | None]:
    """解析 --provider/--model(至少给一个);只给 --model 时反查 provider。"""
    cfg, _files = load_config()
    if provider:
        return provider.strip().lower(), ((model or "").strip() or None)
    name = (model or "").strip()
    if not name:
        raise _AuthError("需给 --provider <provider> 或 --model <model>")
    matches = [p for p, prov in cfg.providers.items()
               if any(entry.id == name for entry in prov.models)]
    if len(matches) == 1:
        return matches[0], name
    if not matches:
        raise _AuthError(f"未知模型 {name!r}(用 `qi models list` 看已配置的模型)")
    raise _AuthError(
        f"模型 {name!r} 在多个 provider 中出现({', '.join(matches)});请加 --provider"
    )


def _auth_resolve(provider: str | None, model: str | None) -> tuple[str, str | None, str | None]:
    """解析出 (provider, model, key);解析顺序同 resolve_key。"""
    name, found_model = _auth_target(provider, model)
    cfg, _files = load_config()
    prov = cfg.providers.get(name)
    rk = resolve_key(name, prov.apiKey if prov else None, AuthStore())
    return name, found_model, (rk.key if rk.ok else None)


def _auth_fail(message: str, code: int) -> None:
    err_console.print(f"[red]Error: {escape(message)}[/red]")
    raise typer.Exit(code=code)


@auth_app.command("print-api-key")
def auth_print_api_key(
    provider: str | None = typer.Option(None, "--provider", help="provider 名"),
    model: str | None = typer.Option(None, "--model", help="用模型反查 provider"),
) -> None:
    """打印某 provider 的 API key 到 stdout(对齐 pi:`pi auth print-api-key`)。"""
    try:
        name, _mdl, key = _auth_resolve(provider, model)
    except (_AuthError, ConfigError) as exc:
        _auth_fail(str(exc), 1)
        return
    if not key:
        _auth_fail(f"provider {name!r} 无可用 API key(auth store / 环境变量 / models.json 均未提供)", 1)
        return
    sys.stdout.write(f"{key}\n")          # 裸 stdout:可管道,不加 rich 装饰


@auth_app.command("print-bearer-token")
def auth_print_bearer_token(
    provider: str | None = typer.Option(None, "--provider", help="provider 名"),
    model: str | None = typer.Option(None, "--model", help="用模型反查 provider"),
    min_expiry: str | None = typer.Option(
        None, "--min-expiry",
        help="有效期下限(如 30m/1h);qi 凭证无过期时间,只校验格式"),
) -> None:
    """打印可作 Bearer token 的凭证(对齐 pi:`pi auth print-bearer-token`)。

    qi 的 auth store 只存 api_key(无 OAuth),故这里的“bearer token”即 API key ——
    对配了 `authHeader: true` 的 provider 就是 `Authorization: Bearer <key>` 的值。
    """
    if min_expiry is not None and not re.fullmatch(r"\d+(ms|s|m|h)", min_expiry.strip()):
        _auth_fail("--min-expiry 需为时长,例如 30m 或 1h", 1)
        return
    try:
        name, _mdl, key = _auth_resolve(provider, model)
    except (_AuthError, ConfigError) as exc:
        _auth_fail(str(exc), 1)
        return
    if not key:
        _auth_fail(f"provider {name!r} 未配置可用的 bearer 凭证", 1)
        return
    sys.stdout.write(f"{key}\n")


@auth_app.command("check")
def auth_check(
    provider: str | None = typer.Option(None, "--provider", help="provider 名"),
    model: str | None = typer.Option(None, "--model", help="用模型反查 provider"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出结果"),
    credentials: bool = typer.Option(False, "--credentials", help="输出里带上凭证"),
    no_refresh: bool = typer.Option(False, "--no-refresh", help="不刷新凭证(qi 无 OAuth,接受即无操作)"),
) -> None:
    """检查某 provider 的凭证是否就绪。

    退出码对齐 pi:`ready`=0,`not_ready`=1,`invalid`=2。
    """
    del no_refresh          # 语义与 pi 一致地接受,但 qi 没有 OAuth 需要刷新
    requested = (provider or model or "").strip()
    result: dict[str, str]
    try:
        name, _mdl, key = _auth_resolve(provider, model)
        cfg, _files = load_config()
        known = name in cfg.providers or name in DEFAULT_API_KEY_ENV
        if not known:
            result = {"status": "not_ready", "provider": name,
                      "reason": "provider_not_found"}
        elif not key:
            result = {"status": "not_ready", "provider": name,
                      "reason": "credentials_not_configured"}
        else:
            result = {"status": "ready", "provider": name, "authType": "api_key"}
            if credentials:
                result["credentials"] = key
    except (_AuthError, ConfigError):
        result = {"status": "invalid", "provider": requested, "reason": "invalid_state"}

    if json_out:
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(result.get("credentials") or result["status"] + "\n")
    status = result["status"]
    raise typer.Exit(code=0 if status == "ready" else 1 if status == "not_ready" else 2)


@auth_app.command("login")
def auth_login(provider: str = typer.Argument(...)) -> None:
    """输入 API key 写入 auth store(provider 可自定义;输入可见)。"""
    provider = provider.strip().lower()
    key = prompt.text(f"{provider} API key", required=True)
    store = AuthStore()
    store.set_key(provider, key)
    console.print(f"[green]已保存 {provider} 到 {store.path}(0600): [/green]{escape(_mask(key))}")


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


# ── qi config:settings.json(对齐 pi 的键与分层) ───────

@app.command("config")
def config_cmd(
    local: bool = typer.Option(False, "--local", "-l",
                               help=f"操作项目 {'.qi'}/{SETTINGS_FILE_NAME}(默认全局 agent 目录)"),
    set_: list[str] = typer.Option(None, "--set", help="写键:--set skills='[\"~/x\"]'(可重复)"),
    unset: list[str] = typer.Option(None, "--unset", help="删键(可重复)"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出合并后的设置"),
) -> None:
    """查看/编辑 settings.json:全局 `~/.qi/agent/` > 项目 `<git根>/.qi/`。"""
    scope = "project" if local else "user"
    cwd = Path.cwd()

    if set_ or unset:
        for item in set_ or []:
            if "=" not in item:
                console.print(f"[red]--set 需要 K=V 形式:[/red] {escape(item)}")
                raise typer.Exit(code=2)
            key, _, raw_value = item.partition("=")
            try:
                path = set_value(scope, key.strip(), parse_value(raw_value), cwd)
            except SettingsError as exc:
                console.print(f"[red]写入失败:[/red] {escape(str(exc))}")
                raise typer.Exit(code=2) from exc
            console.print(f"[green]已写[/green] {path}: {key.strip()} = {escape(raw_value)}")
        for key in unset or []:
            try:
                path, removed = unset_value(scope, key.strip(), cwd)
            except SettingsError as exc:
                console.print(f"[red]删除失败:[/red] {escape(str(exc))}")
                raise typer.Exit(code=2) from exc
            tag = "[green]已删[/green]" if removed else "[yellow]无此键[/yellow]"
            console.print(f"{tag} {path}: {key.strip()}")
        return

    try:
        merged, files = load_settings_raw(cwd)
        settings, _ = load_settings(cwd)
    except SettingsError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc

    if json_out:
        console.print_json(data={"files": [str(p) for p in files], "settings": merged})
        return

    console.print(f"[bold]{SETTINGS_FILE_NAME}[/bold](高 → 低):")
    if files:
        for path in files:
            console.print(f"  {path}")
    else:
        console.print("  (无;均为默认值)")
    for name in SCOPES:
        console.print(f"  {name:8} → {settings_scope_path(name, cwd)}")

    if merged:
        table = Table(title="合并后设置")
        table.add_column("键")
        table.add_column("值")
        for key in sorted(merged):
            table.add_row(key, json.dumps(merged[key], ensure_ascii=False))
        console.print(table)

    provider, model, source = default_model_spec(cwd)
    console.print(f"[bold]默认模型:[/bold] {provider or '-'}/{model or '-'}  (来源:{source})")
    console.print(f"[bold]技能发现:[/bold] {'开' if settings.skillsEnabled else '关'}")

    try:
        skills = load_top_level_skills(cwd, settings, enabled=settings.skillsEnabled)
    except LoadError as exc:
        console.print(f"[red]技能装载失败:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    if skills:
        table = Table(title=f"顶层技能({len(skills)})")
        table.add_column("name")
        table.add_column("来源")
        table.add_column("description")
        for skill in skills:
            table.add_row(skill.name, skill.source or "-", skill.description[:60])
        console.print(table)
    else:
        console.print("[dim]顶层技能:无(把 SKILL.md 放进 ~/.qi/agent/skills/<name>/)[/dim]")


# ── qi init ─────────────────────────────────────────────

def _mask(key: str) -> str:
    """部分遮蔽:首尾各留少量字符,便于确认写入成功。"""
    if not key:
        return "(未设置)"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-2:]}"


def _provider_configured(entry: dict) -> bool:
    """已配置 = 有 baseUrl 且至少一个模型(QwenPaw 的 [✓]/[✗] 标记)。"""
    models = [m for m in entry.get("models", []) or []
              if isinstance(m, dict) and m.get("id")]
    return bool(entry.get("baseUrl")) and bool(models)


def _provider_labels(providers: dict) -> list[tuple[str, str]]:
    """返回 (label, provider_name) 列表,label 带 [✓]/[✗]。"""
    out: list[tuple[str, str]] = []
    for name in sorted(providers):
        entry = providers[name]
        mark = "✓" if _provider_configured(entry) else "✗"
        out.append((f"{name} [{mark}]", name))
    return out


def _select_existing_provider(providers: dict, default_provider: str | None) -> str | None:
    """选已有 provider;返回 None 表示要新建。"""
    pairs = _provider_labels(providers)
    labels = [p[0] for p in pairs] + ["＋ 新建 provider"]
    names = [p[1] for p in pairs]
    default = names.index(default_provider) if default_provider in names else len(names)
    idx = prompt.select("选择 provider", labels, default=default)
    return names[idx] if idx < len(names) else None


def _configure_provider(provider: str, entry: dict) -> None:
    """按 QwenPaw 流程配置单个 provider:baseUrl → api → API key。"""
    store = AuthStore()
    current_key = store.get(provider) or ""

    base_default = entry.get("baseUrl") or None
    base = prompt.text("Base URL (OpenAI-compatible endpoint)", default=base_default, required=True)
    if base:
        entry["baseUrl"] = base

    api_options: list[str] = list(SUPPORTED_APIS)
    api_current = entry.get("api") if entry.get("api") in SUPPORTED_APIS else DEFAULT_API
    api_index = next((i for i, a in enumerate(api_options) if a == api_current), 0)
    entry["api"] = SUPPORTED_APIS[prompt.select("API 类型", api_options, default=api_index)]

    # 凭证:可见输入;已有则回车保留(QwenPaw 的 [set] 语义)
    suffix = f" [{'set' if current_key else 'not set'}, 回车保留]" if current_key else ""
    key = prompt.text(f"{provider} API key", suffix=suffix, required=not current_key)
    if key:
        store.set_key(provider, key)
        current_key = key
    summary = f"[green]✓[/green] {provider} — API Key: {escape(_mask(current_key))}"
    if entry.get("baseUrl"):
        summary += f", Base URL: {escape(entry['baseUrl'])}"
    console.print(summary)


def _add_models_interactive(provider: str, entry: dict) -> None:
    """QwenPaw 风格的 Add a model? 循环;每个模型含 qi 参数(有默认值)。"""
    models: list = entry.setdefault("models", [])
    console.print(f"\n[bold]--- Add Models ---[/bold]")
    if models:
        console.print(f"Current models for {provider}:")
        for m in models:
            if isinstance(m, dict) and m.get("id"):
                console.print(f"  - {m.get('name') or m['id']} ({m['id']})")
    else:
        console.print(f"No models configured for {provider}.")

    while prompt.confirm("Add a model?", default=not models):
        mid = prompt.text("Model identifier", required=True)
        name = prompt.text("Model display name", default=mid).strip() or mid
        reasoning = prompt.confirm("Supports reasoning (扩展思考)?", default=False)
        ctx = prompt.integer("contextWindow", DEFAULT_CONTEXT_WINDOW)
        mx = prompt.integer("maxTokens", DEFAULT_MAX_TOKENS)
        new = {"id": mid, "name": name, "reasoning": reasoning,
               "contextWindow": ctx, "maxTokens": mx}
        for i, m in enumerate(models):
            if isinstance(m, dict) and m.get("id") == mid:
                models[i] = {**m, **new}
                break
        else:
            models.append(new)
        console.print(f"[green]✓[/green] Model '{escape(name)}' ({escape(mid)}) added.")


def _activate_llm(providers: dict, current: tuple[str | None, str | None]) -> tuple[str, str]:
    """QwenPaw 的 --- Activate LLM Model ---:选 provider → 选 model。

    返回 `(provider, model)` —— 默认模型写进 settings.json,不再进 models.json。
    """
    console.print("\n[bold]--- Activate LLM Model ---[/bold]")
    eligible = [n for n in sorted(providers)
                if any(isinstance(m, dict) and m.get("id") for m in providers[n].get("models", []))]
    if not eligible:
        console.print("[red]没有可用模型,已取消。[/red]")
        raise typer.Exit(code=1)

    def _prov_label(n: str) -> str:
        mark = "✓" if _provider_configured(providers[n]) else "✗"
        return f"{n} [{mark}]"

    cur_prov, cur_model = current
    pidx = eligible.index(cur_prov) if cur_prov in eligible else 0
    p = eligible[prompt.select("Select provider for LLM", [_prov_label(n) for n in eligible], default=pidx)]

    ids = [m["id"] for m in providers[p]["models"] if isinstance(m, dict) and m.get("id")]
    midx = ids.index(cur_model) if cur_model in ids and cur_prov == p else 0
    labels = [f"{m.get('name') or m['id']}" for m in providers[p]["models"]
              if isinstance(m, dict) and m.get("id")]
    m = ids[prompt.select("Select LLM model", labels, default=midx)]
    console.print(f"[green]✓[/green] LLM: {escape(p)} / {escape(m)}")
    return p, m


def _strip_legacy_defaults(data: dict) -> None:
    """models.json 里的 defaultProvider/defaultModel 已不生效 —— 顺手剔掉(init 本来就在重写该文件)。"""
    dropped = [k for k in ("defaultProvider", "defaultModel") if k in data]
    for key in dropped:
        data.pop(key, None)
    if dropped:
        console.print(f"[dim]已从 {MODELS_FILE_NAME} 移除旧键: {', '.join(dropped)}"
                      f"(现在只写在 {SETTINGS_FILE_NAME})[/dim]")


def _write_default_model(provider: str, model: str, local: bool) -> Path:
    """把默认模型写进 settings.json(全局或项目),返回文件路径。"""
    scope = "project" if local else "user"
    set_value(scope, "defaultProvider", provider)
    path = set_value(scope, "defaultModel", model)
    console.print(f"[green]✓[/green] 默认模型已写入 {path}")
    return path


def _current_default(local: bool) -> tuple[str | None, str | None]:
    """读当前默认模型(settings.json),供 init 交互预选。"""
    scope = "project" if local else "user"
    try:
        settings = load_settings_by_scope().get(scope)
    except SettingsError:
        return None, None
    if settings is None:
        return None, None
    return settings.defaultProvider, settings.defaultModel


def _write_models(target_dir: Path, target_file: Path, data: dict) -> None:
    for sub in ("agents", "plugins", "sessions"):
        (target_dir / sub).mkdir(parents=True, exist_ok=True)
    save_models_file(target_file, data)
    console.print(f"[green]✓[/green] Configuration saved to {target_file}")


def _init_interactive(local: bool) -> None:
    """QwenPaw 风格:Provider Configuration → Add Models → Activate LLM Model。"""
    target_dir = project_home() if local else global_home()
    target_file = target_dir / MODELS_FILE_NAME
    data = load_models_file(target_file)
    providers: dict = data.setdefault("providers", {})
    current = _current_default(local)

    console.print(f"Working dir: {target_dir}")
    console.print("\n[bold]=== LLM Provider Configuration ===[/bold]")
    console.print("[bold]--- Provider Configuration ---[/bold]")
    while True:
        provider = _select_existing_provider(providers, current[0])
        if provider is None:
            provider = prompt.text("Provider name", required=True)
            entry = providers.setdefault(provider, {})
        else:
            entry = providers[provider]
        _configure_provider(provider, entry)
        _add_models_interactive(provider, entry)
        if not prompt.confirm("Configure another provider?", default=False):
            break

    provider, model = _activate_llm(providers, current)
    _strip_legacy_defaults(data)
    _write_models(target_dir, target_file, data)
    _write_default_model(provider, model, local)
    console.print("\n[green]✓ Initialization complete![/green]")


def _init_noninteractive(*, provider: str | None, model: str | None, base_url: str | None,
                         api: str | None, api_key: str | None, api_key_env: str | None,
                         reasoning: bool | None, context_window: int | None,
                         max_tokens: int | None, local: bool) -> None:
    target_dir = project_home() if local else global_home()
    target_file = target_dir / MODELS_FILE_NAME
    data = load_models_file(target_file)
    providers: dict = data.setdefault("providers", {})

    if not provider:
        provider = _current_default(local)[0]
    if not provider:
        console.print("[red]-y 模式需要 --provider(或先用交互模式配置)。[/red]")
        raise typer.Exit(code=2)
    provider = provider.strip()

    entry: dict = dict(providers.get(provider) or {})
    if base_url:
        entry["baseUrl"] = base_url
    if api:
        if api not in SUPPORTED_APIS:
            console.print(f"[red]未知 api {api};支持: {', '.join(SUPPORTED_APIS)}[/red]")
            raise typer.Exit(code=2)
        entry["api"] = api
    entry.setdefault("api", DEFAULT_API)

    store = AuthStore()
    if api_key:
        store.set_key(provider, api_key)
    elif api_key_env:
        entry["apiKey"] = f"${api_key_env}"
    elif not entry.get("apiKey") and not store.get(provider):
        env_name = DEFAULT_API_KEY_ENV.get(provider)
        if env_name:
            entry["apiKey"] = f"${env_name}"

    models: list = entry.setdefault("models", [])
    if not model:
        cur_provider, cur_model = _current_default(local)
        model = cur_model if cur_provider == provider else None
        if not model and models:
            model = models[0].get("id")
    if not model:
        console.print("[red]-y 模式需要 --model。[/red]")
        raise typer.Exit(code=2)
    model = model.strip()

    new = {"id": model}
    for i, m in enumerate(models):
        if isinstance(m, dict) and m.get("id") == model:
            merged = {**m, **new}
            merged["reasoning"] = bool(reasoning) if reasoning is not None else m.get("reasoning", False)
            merged["contextWindow"] = (context_window if context_window is not None
                                       else m.get("contextWindow", DEFAULT_CONTEXT_WINDOW))
            merged["maxTokens"] = (max_tokens if max_tokens is not None
                                   else m.get("maxTokens", DEFAULT_MAX_TOKENS))
            models[i] = merged
            break
    else:
        models.append({
            "id": model,
            "reasoning": bool(reasoning) if reasoning is not None else False,
            "contextWindow": context_window if context_window is not None else DEFAULT_CONTEXT_WINDOW,
            "maxTokens": max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
        })

    providers[provider] = entry
    _strip_legacy_defaults(data)
    _write_models(target_dir, target_file, data)
    _write_default_model(provider, model, local)
    console.print(f"[bold]默认模型:[/bold] {provider}/{model}")
    console.print("[yellow]下一步:qi doctor 校验。[/yellow]")


@app.command("init")
def init(
    provider: str | None = typer.Option(None, "--provider", help="provider 名(已有或新建)"),
    model: str | None = typer.Option(None, "--model", help="模型 id(已有或新建)"),
    base_url: str | None = typer.Option(None, "--base-url", help="provider 的 API endpoint"),
    api: str | None = typer.Option(None, "--api", help="api 类型: " + "/".join(SUPPORTED_APIS)),
    api_key: str | None = typer.Option(None, "--api-key", help="API key(写入 auth.json)"),
    api_key_env: str | None = typer.Option(None, "--api-key-env", help="在 models.json 引用该环境变量"),
    reasoning: bool | None = typer.Option(None, "--reasoning/--no-reasoning", help="模型支持 reasoning"),
    context_window: int | None = typer.Option(None, "--context-window", help="上下文窗口 token"),
    max_tokens: int | None = typer.Option(None, "--max-tokens", help="最大输出 token"),
    local: bool = typer.Option(False, "--local", "-l", help="写入项目 .qi/models.json"),
    yes: bool = typer.Option(False, "--yes", "-y", help="非交互:需配合 --provider/--model"),
) -> None:
    """引导默认模型:Provider Config → Add Models → Activate LLM,写 models.json + auth.json。"""
    if yes:
        _init_noninteractive(provider=provider, model=model, base_url=base_url, api=api,
                             api_key=api_key, api_key_env=api_key_env, reasoning=reasoning,
                             context_window=context_window, max_tokens=max_tokens, local=local)
    else:
        _init_interactive(local)


@app.command("version")
def version() -> None:
    """显示版本。"""
    console.print(f"qi {__version__}")


@app.command("tui")
def tui() -> None:
    """启动文本交互界面(TUI)。"""
    try:
        from .tui import run_tui
    except Exception as exc:  # textual 依赖问题
        console.print(f"[red]TUI 不可用: {escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    run_tui()


def _port_free(host: str, port: int) -> bool:
    """端口能不能绑定(启动前预检)。

    为什么必须预检:uvicorn 的 bind 失败发生在 `console.print(URL)` **之后**,
    于是“端口被占用”会表现成“启动成功但页面是旧的/坏的”——真发生过。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


@app.command("web")
def web_cmd(
    port: int = typer.Option(30142, "--port", "-p", help="端口(默认 30142,与 pi-web 的 30141 错开)"),
    hostname: str = typer.Option("127.0.0.1", "--hostname", "-H", help="绑定地址(默认只回环)"),
    no_open: bool = typer.Option(False, "--no-open", help="不自动打开浏览器"),
    cwd: str | None = typer.Option(None, "--cwd", help="默认工作目录(默认当前目录)"),
    password: str | None = typer.Option(None, "--password", help="访问口令(也可用 QI_WEB_PASSWORD)"),
) -> None:
    """启动本地 Web UI(默认 http://127.0.0.1:30142)。"""
    import contextlib
    import os
    import threading
    import webbrowser

    from .web.security import is_loopback, require_safe_config

    try:
        import uvicorn

        from .web.app import create_app
    except ImportError as exc:  # web 是可选 extra
        console.print(f"[red]缺少 web 依赖:[/red] {escape(str(exc))}")
        console.print("  安装: [bold]pip install 'qi-agent[web]'[/bold]")
        raise typer.Exit(code=2) from exc

    pw = password or os.environ.get("QI_WEB_PASSWORD") or None
    # 不安全的组合直接拒启(跨回环 + 无口令),见 web/security.py
    require_safe_config(hostname, pw)

    workdir = Path(cwd).expanduser() if cwd else Path.cwd()
    if not workdir.is_dir():
        console.print(f"[red]工作目录不存在:[/red] {escape(str(workdir))}")
        raise typer.Exit(code=2)

    allowed = [h for h in os.environ.get("QI_WEB_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not _port_free(hostname, port):
        console.print(f"[red]端口已被占用:[/red] {hostname}:{port}")
        console.print(f"  换个端口: [bold]qi web -p {port + 1}[/bold]")
        raise typer.Exit(code=2)

    app_obj = create_app(cwd=workdir.resolve(), password=pw, allowed_hosts=allowed,
                         bind_host=hostname)

    shown = "127.0.0.1" if is_loopback(hostname) else hostname
    url = f"http://{shown}:{port}"
    console.print(f"[green]qi web[/green] → [bold]{url}[/bold]")
    console.print(f"[dim]默认工作目录: {escape(str(workdir))}[/dim]")
    if pw:
        console.print("[dim]已启用口令(Bearer 或 Basic,用户名任意)[/dim]")
    if not is_loopback(hostname):
        console.print("[yellow]警告:[/yellow] 已绑定到回环之外——它能执行高权限操作,请确认网络可信。")
    if not (Path(__file__).parent / "web" / "static").is_dir():
        console.print("[dim]未找到前端产物;先访问 API,或 cd web && npm install && npm run build[/dim]")

    if not no_open:
        def _open() -> None:
            # 尽力而为:服务起来前打开可能白页,失败也不影响启动
            with contextlib.suppress(Exception):
                webbrowser.open(url)

        threading.Timer(1.5, _open).start()

    uvicorn.run(app_obj, host=hostname, port=port, log_level="warning")


def main() -> None:
    """CLI 入口:先确保目录布局(旧扁平布局 → `~/.qi/agent/`),再交给 typer。"""
    try:
        moved = paths.ensure_layout()
    except OSError as exc:
        err_console.print(f"[yellow]目录初始化失败(继续): {escape(str(exc))}[/yellow]")
        moved = []
    if moved:
        console.print("[green]已迁移到 agent 目录(对齐 pi):[/green]")
        for src, dst in moved:
            console.print(f"  {src} → {dst}")
    app()
