"""CLI 入口(对齐 docs/cli.md;P1-P6)。

顶层:qi [options] [--] [消息...]  →  -p 无头执行(auto);否则提示/进 TUI(v2 在 cli 不可用时提示)
子命令:doctor / models list / auth / init / agents / sessions / version
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, prompt
from .auth import AuthStore, DEFAULT_API_KEY_ENV, resolve_key
from .config import (
    DEFAULT_API,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_TOKENS,
    SUPPORTED_APIS,
    ConfigError,
    load_config,
    load_models_file,
    require_default_model,
    resolve_model,
    resolve_router_model,
    save_models_file,
)
from .loader import LoadError, load_agent_dir, load_all_agents, scan_agent_dirs
from .paths import MODELS_FILE_NAME, global_home, project_home
from .registry import AgentRegistry, CapabilityRegistry, ToolCatalog, discover_plugins
from .session import SessionStore
from .tools import register_builtin_tools

console = Console(highlight=False)
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
    try:
        units = load_all_agents(catalog_names=catalog.names, ds_types=set())
    except LoadError as exc:
        console.print(f"[red]装载失败:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
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
        console.print(f"[yellow]未找到任何 {MODELS_FILE_NAME}[/yellow](env 指定 / 项目 .qi / 用户 ~/.qi)")

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
        default = require_default_model(cfg)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    rk = resolve_key(default.provider, default.api_key_ref, store)
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
    default_label = f"{cfg.defaultProvider}/{cfg.defaultModel}" if cfg.defaultProvider and cfg.defaultModel else None
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
    if default_label and default_label not in shown:
        spec = resolve_model(cfg, cfg.defaultProvider, cfg.defaultModel)  # type: ignore[arg-type]
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

    api_default = entry.get("api") if entry.get("api") in SUPPORTED_APIS else DEFAULT_API
    entry["api"] = SUPPORTED_APIS[prompt.select(
        "API 类型", list(SUPPORTED_APIS), default=list(SUPPORTED_APIS).index(api_default))]

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
               "contextWindow": int(ctx), "maxTokens": int(mx)}
        for i, m in enumerate(models):
            if isinstance(m, dict) and m.get("id") == mid:
                models[i] = {**m, **new}
                break
        else:
            models.append(new)
        console.print(f"[green]✓[/green] Model '{escape(name)}' ({escape(mid)}) added.")


def _activate_llm(data: dict, providers: dict) -> None:
    """QwenPaw 的 --- Activate LLM Model ---:选 provider → 选 model。"""
    console.print("\n[bold]--- Activate LLM Model ---[/bold]")
    eligible = [n for n in sorted(providers)
                if any(isinstance(m, dict) and m.get("id") for m in providers[n].get("models", []))]
    if not eligible:
        console.print("[red]没有可用模型,已取消。[/red]")
        raise typer.Exit(code=1)

    def _prov_label(n: str) -> str:
        mark = "✓" if _provider_configured(providers[n]) else "✗"
        return f"{n} [{mark}]"

    cur_prov = data.get("defaultProvider")
    pidx = eligible.index(cur_prov) if cur_prov in eligible else 0
    p = eligible[prompt.select("Select provider for LLM", [_prov_label(n) for n in eligible], default=pidx)]

    ids = [m["id"] for m in providers[p]["models"] if isinstance(m, dict) and m.get("id")]
    cur_model = data.get("defaultModel")
    midx = ids.index(cur_model) if cur_model in ids else 0
    labels = [f"{m.get('name') or m['id']}" for m in providers[p]["models"]
              if isinstance(m, dict) and m.get("id")]
    m = ids[prompt.select("Select LLM model", labels, default=midx)]

    data["defaultProvider"] = p
    data["defaultModel"] = m
    console.print(f"[green]✓[/green] LLM: {escape(p)} / {escape(m)}")


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

    console.print(f"Working dir: {target_dir}")
    console.print("\n[bold]=== LLM Provider Configuration ===[/bold]")
    console.print("[bold]--- Provider Configuration ---[/bold]")
    while True:
        provider = _select_existing_provider(providers, data.get("defaultProvider"))
        if provider is None:
            provider = prompt.text("Provider name", required=True)
            entry = providers.setdefault(provider, {})
        else:
            entry = providers[provider]
        _configure_provider(provider, entry)
        _add_models_interactive(provider, entry)
        if not prompt.confirm("Configure another provider?", default=False):
            break

    _activate_llm(data, providers)
    _write_models(target_dir, target_file, data)
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
        provider = data.get("defaultProvider")
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
        model = data.get("defaultModel") if data.get("defaultProvider") == provider else None
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
            merged["contextWindow"] = (int(context_window) if context_window is not None
                                       else m.get("contextWindow", DEFAULT_CONTEXT_WINDOW))
            merged["maxTokens"] = (int(max_tokens) if max_tokens is not None
                                   else m.get("maxTokens", DEFAULT_MAX_TOKENS))
            models[i] = merged
            break
    else:
        models.append({
            "id": model,
            "reasoning": bool(reasoning) if reasoning is not None else False,
            "contextWindow": int(context_window) if context_window is not None else DEFAULT_CONTEXT_WINDOW,
            "maxTokens": int(max_tokens) if max_tokens is not None else DEFAULT_MAX_TOKENS,
        })

    providers[provider] = entry
    data["defaultProvider"] = provider
    data["defaultModel"] = model
    _write_models(target_dir, target_file, data)
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


def main() -> None:
    app()
