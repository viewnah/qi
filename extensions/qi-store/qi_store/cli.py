"""`qi store` 的命令行。

用 argparse(不用 typer):`qi` 的扩展子命令拿到的是**命令名之后的原始 argv**,选项表是静态的,
各扩展自己解析自己的参数(与 `qi-lab/serve.py` 同一约定 —— 那里的用法错误退出码正好是 2,
与我们的 `EXIT_USAGE` 一致)。

四条认证命令(P0) + 六条取货命令(P1)。取货命令共用一条流水线:

    catalog(取目录) → 解析 id/选择器 → planner(算计划) → 确认 → installer(落盘+记账)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from rich.console import Console
from rich.table import Table

from . import config, installer, ops, paths, planner, service
from .catalog import Catalog, StoreItem, load as load_catalog
from .client import StoreClient, token_claims, token_exp
from .config import Source, StoreConfig
from .errors import (
    EXIT_CANCELLED,
    EXIT_OK,
    EXIT_USAGE,
    Cancelled,
    Conflict,
    NotLoggedIn,
    NotFound,
    StoreError,
    UsageError,
)
from .kinds import ALL_KINDS, NOT_IMPLEMENTED, get_kind
from .kinds.base import PlanContext
from .ledger import Ledger

console = Console()
err_console = Console(stderr=True)


# ── 小工具 ────────────────────────────────────────────────


def _human_left(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _human_size(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.0f} MB"


def _prompt(text: str) -> str:
    """问一句。**非 tty 一律失败** —— 不许在管道里"等输入"等到 CI 超时。"""
    if not sys.stdin.isatty():
        raise UsageError(
            f"非交互终端,无法询问「{text}」",
            hint="用参数给全(如 `-u <用户>`)或 --password-stdin / --token / -y",
        )
    try:
        return input(text).strip()
    except EOFError as exc:                       # Ctrl-D
        raise Cancelled("已取消") from exc


def _confirm(text: str, *, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise UsageError(f"非交互终端,无法确认「{text}」", hint="确认无误请加 -y")
    return _prompt(f"{text} [y/N] ").lower() in ("y", "yes")


def _source_name(cfg: StoreConfig, explicit: str | None) -> str:
    if explicit:
        return explicit
    if cfg.default_store:
        return cfg.default_store
    if "hikqin" in cfg.sources:
        return "hikqin"
    return config.DEFAULT_STORE_NAME


def _open(source: Source, http_factory: Any, **kwargs: Any) -> StoreClient:
    """带上"令牌变了就写回"的回调。测试注入 `http_factory` 走同一条路。

    真实现在 `ops.open_client`:qi-lab 的面板走的也是它 —— 两处必须同一套行为。
    """
    return ops.open_client(source, http_factory, **kwargs)


def _scope_of(args: argparse.Namespace, cfg: StoreConfig) -> str:
    scope = "project" if getattr(args, "local", False) else (
        getattr(args, "scope", None) or cfg.install.get("scope") or "global")
    if scope not in ("global", "project"):
        raise UsageError(f"未知的作用域:`{scope}`", hint="可用:global / project")
    return scope


# ── 认证 ────────────────────────────────────────────────


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        line = sys.stdin.readline()
        if not line:
            raise UsageError("--password-stdin 没收到内容")
        return line.rstrip("\r\n")
    if not sys.stdin.isatty():
        raise UsageError(
            "非交互终端拿不到密码",
            hint="用 `--password-stdin < pw`(推荐)或 `--token`(CI)",
        )
    return getpass.getpass("密码:")


def cmd_login(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    name = _source_name(cfg, getattr(args, "store", None))
    existing = cfg.sources.get(name)
    url = (args.url or os.environ.get(paths.QI_STORE_URL)
           or (existing.base_url if existing else None))
    if not url:
        raise UsageError(
            "不知道商店地址",
            hint=f"给一个:`qi store login http://<host>:6091`(写进 {paths.user_config_file()})",
        )
    url = url.rstrip("/")
    if existing and existing.base_url != url and not args.yes:
        if not _confirm(f"检测到另一个实例({existing.base_url} → {url}),替换源?"):
            raise Cancelled("已取消(源未替换)")

    source = Source(name=name, base_url=url,
                    api_prefix=existing.api_prefix if existing else config.DEFAULT_API_PREFIX)
    client = _open(source, http_factory)
    try:
        if args.token:
            auth = client.use_token(args.token)
        else:
            login = (args.user or (existing.login_name if existing else None)
                     or _prompt("用户名: "))
            if not login:
                raise UsageError("用户名不能为空")
            auth = client.login(login, _read_password(args))
    finally:
        client.close()

    if not cfg.default_store:
        config.save_default(name)          # 第一个源自动成为默认,省掉一次编辑

    left = _human_left((auth.get("exp") or 0) - time.time()) if auth.get("exp") else "未知"
    console.print(
        f"[green]已登录[/green] {auth.get('login') or '?'} "
        f"({auth.get('role') or '?'}) @ [cyan]{url}[/cyan] · 剩 {left},7 天内自动续"
    )
    console.print(f"[dim]凭证写在 {paths.user_config_file()}(0600;该目录应仅本人可读)[/dim]")
    return EXIT_OK


def cmd_logout(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    name = _source_name(cfg, getattr(args, "store", None))
    targets = sorted(cfg.sources) if args.all else [name]
    removed = []
    for target in targets:
        if cfg.sources.get(target) and cfg.sources[target].auth:
            config.save_auth(target, None)
            removed.append(target)
    if args.all:
        shutil.rmtree(paths.cache_dir(), ignore_errors=True)
    if removed:
        console.print(f"[green]已删除本地凭证[/green]:{', '.join(removed)}")
    else:
        console.print("[dim]本来就没登录(幂等)[/dim]")
    console.print(
        "[yellow]注意[/yellow]:上游没有吊销接口 —— 本地删了不等于令牌立即失效,"
        "要立刻失效请改密码。"
    )
    return EXIT_OK


def cmd_whoami(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    if not cfg.sources:
        # "一个源都没配" 对 whoami 来说就是"未登录"(3),不是用法错误 —— 下一步相同
        raise NotLoggedIn("未登录:没有配置任何商店源",
                          hint="`qi store login http://<host>:6091`")
    source = cfg.source(None)
    token = os.environ.get(paths.QI_STORE_TOKEN) or source.access_token
    if not token:
        raise NotLoggedIn(f"未登录:{source.name}", hint="`qi store login`")
    claims = token_claims(token)
    exp = token_exp(token)
    left = _human_left(exp - time.time()) if exp else "未知"
    origin = "env QI_STORE_TOKEN" if os.environ.get(paths.QI_STORE_TOKEN) else "store.json"
    console.print(
        f"{claims.get('username') or source.login_name or '?'} "
        f"({claims.get('role') or (source.auth or {}).get('role') or '?'}) "
        f"@ [cyan]{source.name}[/cyan] {source.base_url} · 剩 {left} · 来源 {origin}"
    )
    if exp and exp <= time.time():
        raise NotLoggedIn("凭证已过期", hint="`qi store login`")
    if args.remote:
        client = _open(source, http_factory, token_override=token)
        try:
            user = client.me()
        finally:
            client.close()
        console.print(f"[dim]远端确认:{user.get('username')} ({user.get('role')})[/dim]")
    return EXIT_OK


def cmd_sources(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    if not cfg.sources:
        console.print("[dim]没有配置源。`qi store login <url>`[/dim]")
        return EXIT_OK
    for name in sorted(cfg.sources, key=lambda n: -cfg.sources[n].priority):
        source = cfg.sources[name]
        marks = []
        if name == cfg.default_store:
            marks.append("default")
        if source.disabled:
            marks.append("disabled")
        if source.auth and source.access_token:
            marks.append(f"已登录 {source.login_name or '?'}")
        suffix = f" [dim]({', '.join(marks)})[/dim]" if marks else ""
        console.print(f"  {name:<12} {source.base_url}{suffix}")
    return EXIT_OK


# ── 取货公用 ────────────────────────────────────────────


def _load_catalog(args: argparse.Namespace, source: Source, http_factory: Any) -> Catalog:
    kinds = [args.kind] if getattr(args, "kind", None) else None
    client = _open(source, http_factory)
    try:
        return load_catalog(client, store_name=source.name, kinds=kinds,
                            offline=getattr(args, "offline", False))
    finally:
        client.close()


def _plan_ctx(args: argparse.Namespace, source: Source, http_factory: Any,
              scope: str, cwd: Path | None) -> PlanContext:
    client = _open(source, http_factory) if not getattr(args, "offline", False) else None
    return PlanContext(
        root=planner.scope_root(scope, cwd), scope=scope, store_name=source.name,
        base_url=source.base_url, client=client,
        name_override=getattr(args, "name", None),
    )


def _split_store_suffix(ids: list[str]) -> tuple[list[str], str | None]:
    """`kb:<id>@alice` → (`kb:<id>`, `alice`)。同一批只允许一个源。"""
    out: list[str] = []
    stores: set[str] = set()
    for key in ids:
        text = str(key)
        if "@" in text:
            left, _, right = text.rpartition("@")
            if right:
                stores.add(right)
                text = left
        out.append(text)
    if len(stores) > 1:
        raise UsageError("同一批条目不能来自多个源", hint="分开装，或用 `--store` 逐个执行")
    return out, next(iter(stores), None)


def _kind_positional(ids: list[str], kind: str | None) -> tuple[list[str], str | None]:
    """`qi store install skill` —— **单位置参且是 kind 名**时它指的是 kind,不是 id。

    argparse 会把 `skill` 当成 `ids[0]`,而 `find("skill")` 会模糊命中 `skill-creator`,
    于是"我想选一个技能"变成"装了某个我没想到的技能"。所以这一层必须显式认。
    """
    if len(ids) == 1 and ids[0] in ALL_KINDS + ("agent",) and kind is None:
        return [], ids[0]
    return ids, kind


def _resolve_ids(catalog: Catalog, raw_ids: list[str], *, kind: str | None,
                 ledger: Ledger) -> list[StoreItem]:
    """把命令行给的东西解析成条目。给了 id 就**一个都不许糊**:有歧义就报错退出 4。"""
    if raw_ids:
        return [catalog.find(key) for key in raw_ids]
    pool = catalog.filter(kind=kind)
    if not pool:
        raise NotFound("目录里没有可安装的条目" if not kind else f"目录里没有 {kind}",
                       hint="`qi store search` 看看有什么" if not kind else None)
    return _choose(pool, ledger=ledger)


def _choose(pool: list[StoreItem], *, ledger: Ledger | None = None,
            title: str = "选择要处理的条目") -> list[StoreItem]:
    """编号选择器。**输入的是列表序号,不是主键** —— 所以两列都显示(README §3.4)。"""
    if not sys.stdin.isatty():
        raise UsageError(
            "非交互终端不能进选择器",
            hint=f"直接给 id,例如 `{pool[0].id}`",
        )
    shown = pool[:30]
    picked: list[int] = []
    attempts = 0
    while True:
        _print_items(shown, ledger=ledger, title=title)
        if len(pool) > len(shown):
            console.print(f"[dim]共 {len(pool)} 条,这里显示前 {len(shown)} 条 —— "
                          f"用 /关键词 缩小[/dim]")
        hint = "输入编号（1,3 或 2-4；/关键词 过滤；回车取消）: "
        raw = _prompt(hint)
        if not raw:
            raise Cancelled("已取消")
        if raw.startswith("/"):
            needle = raw[1:].strip().lower()
            filtered = [item for item in pool
                        if needle in item.name.lower() or needle in item.description.lower()]
            if not filtered:
                console.print("[yellow]没有匹配的条目[/yellow]")
                continue
            shown = filtered[:30]
            picked = []
            attempts = 0
            continue
        try:
            indexes = _parse_indexes(raw, len(shown))
        except ValueError as exc:
            attempts += 1
            console.print(f"[yellow]{exc}[/yellow](还有 {3 - attempts} 次)")
            if attempts >= 3:
                raise Cancelled("输入不合法,已取消") from exc
            continue
        picked = indexes
        break
    return [shown[index - 1] for index in picked]


def _parse_indexes(raw: str, limit: int) -> list[int]:
    picked: list[int] = []
    for part in raw.replace("，", ",").split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            head, _, tail = token.partition("-")
            start, end = int(head), int(tail)
            if start > end:
                start, end = end, start
            numbers = list(range(start, end + 1))
        else:
            numbers = [int(token)]
        for number in numbers:
            if number < 1 or number > limit:
                raise ValueError(f"编号 {number} 超出范围(1-{limit})")
            if number not in picked:
                picked.append(number)
    if not picked:
        raise ValueError("没有有效编号")
    return picked


def _print_items(items: list[StoreItem], *, ledger: Ledger | None = None,
                 title: str = "条目") -> None:
    table = Table(title=title, title_justify="left", header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("名称", no_wrap=True)
    table.add_column("说明", overflow="ellipsis", max_width=44, no_wrap=True)
    table.add_column("大小/规格", justify="right", style="dim")
    table.add_column("安全", style="dim")
    table.add_column("状态", style="dim")
    for index, item in enumerate(items, start=1):
        state = ""
        if ledger is not None:
            record = ledger.find(item.id)
            if record is not None:
                state = "已装" if record.version == item.version else f"已装 {record.version}"
        table.add_row(
            str(index), item.id, item.name, item.description.replace("\n", " "),
            _human_size(item.badges.get("size")),
            item.safety_label(),
            state,
        )
    console.print(table)


# ── search / info ───────────────────────────────────────


def cmd_search(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    source = cfg.source(getattr(args, "store", None))
    catalog = _load_catalog(args, source, http_factory)
    items = catalog.filter(kind=args.kind, keyword=args.keyword)
    if args.limit:
        items = items[: args.limit]
    if args.json:
        console.print_json(json.dumps([item.as_dict() for item in items], ensure_ascii=False))
        return EXIT_OK
    if not items:
        console.print("[yellow]没找到[/yellow]" + (f":`{args.keyword}`" if args.keyword else ""))
    else:
        _print_items(items, ledger=Ledger.load(), title=f"{source.name} · {len(items)} 条")
    for kind, reason in catalog.missing:
        # 一个 kind 没查成必须说出来 —— 否则"商店看起来是空的"是最坏的误解
        err_console.print(f"[yellow]⚠ {kind} 没查成[/yellow]:{reason}")
    if not (source.access_token or os.environ.get(paths.QI_STORE_TOKEN)):
        err_console.print("[dim]未登录:只列了 a2a。要 skill/mcp 请 `qi store login`[/dim]")
    return EXIT_OK


def cmd_info(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    source = cfg.source(getattr(args, "store", None))
    catalog = _load_catalog(args, source, http_factory)
    item = catalog.find(args.id)
    ledger = Ledger.load()
    record = ledger.find(item.id)

    console.print(f"[bold]{item.name}[/bold]  [cyan]{item.id}[/cyan]")
    if item.description:
        console.print(item.description)
    console.print(
        f"kind={item.kind} · 版本={item.version} · 更新={item.updated_at or '-'} · "
        f"需补配置={'是' if item.requires_config else '否'} · "
        f"已装={('是 ' + record.version) if record else '否'}"
    )
    if item.safety:
        console.print(f"安全:{item.safety_label()}")
    for key, value in (item.badges or {}).items():
        console.print(f"  [dim]{key}[/dim]={value}")
    if item.kind == "skill" and item.meta.get("upstream_id") is not None:
        _show_skill_files(source, http_factory, item)
    if record is not None:
        console.print(f"[dim]已装到:{record.target or '(配置键)'}[/dim]")
        if record.needs_config:
            console.print("[yellow]待补环境变量[/yellow]:" + ", ".join(record.needs_config))
    return EXIT_OK


def _show_skill_files(source: Source, http_factory: Any, item: StoreItem) -> None:
    client = _open(source, http_factory)
    try:
        data = client.get(f"/skills/{item.meta['upstream_id']}/files")
    except StoreError:
        return
    finally:
        client.close()
    files = data.get("files") if isinstance(data, dict) else None
    if isinstance(files, list) and files:
        console.print("[dim]文件:[/dim]" + "、".join(
            f"{entry.get('name')}{'/' if entry.get('is_directory') else ''}"
            for entry in files if isinstance(entry, dict)))


# ── install ────────────────────────────────────────────


def cmd_install(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    args.ids, id_store = _split_store_suffix(args.ids)
    if id_store:
        if getattr(args, "store", None) and args.store != id_store:
            raise UsageError(f"`@{id_store}` 与 `--store {args.store}` 不一致")
        args.store = id_store
    source = cfg.source(getattr(args, "store", None))
    scope = _scope_of(args, cfg)
    args.ids, args.kind = _kind_positional(args.ids, args.kind)
    if args.offline:
        raise UsageError("离线(offline)不能 install",
                         hint="缓存里只有目录元数据,没有要写盘的内容")
    if args.name and len(args.ids) > 1:
        raise UsageError("--name 只能和单个条目一起用")
    catalog = _load_catalog(args, source, http_factory)
    ledger = Ledger.load()
    items = _resolve_ids(catalog, args.ids, kind=args.kind, ledger=ledger)

    if items and all(item.kind == "kb" for item in items):
        return _install_kb(args, scope, getattr(args, "store", None))

    if scope == "project":
        # 项目级安装会让**仓库控制的目录**多出东西,所以每次都要人点头
        # (我们不读 trust.json —— 那份语义归宿主,扩展不该复制一遍判定)
        target = planner.scope_root("project", getattr(args, "cwd", None))
        _confirm(f"装进项目目录 {target}?", assume_yes=args.yes)

    ctx = _plan_ctx(args, source, http_factory, scope, getattr(args, "cwd", None))
    try:
        plan = planner.build(items, ctx, ledger=ledger)
        if not plan.items:
            for item, reason in plan.skipped:
                console.print(f"[dim]{item.id} 跳过:{reason}[/dim]")
            console.print("[dim]没有要装的东西[/dim]")
            return EXIT_OK
        unsafe = [planned for planned in plan.items if planned.item.is_unsafe()]
        if unsafe and not args.allow_unsafe:
            names = ", ".join(planned.item.id for planned in unsafe)
            if not _confirm(f"{names} 被上游标记为高危,仍要安装?"):
                raise Cancelled("已取消")
        if not args.json:
            # --json 的输出必须是**纯 JSON**:表格与后续的"已安装"行都会污染它
            _print_plan(plan, source, scope)
        if plan.conflicts and not args.force:
            raise Conflict(
                "目标已存在(不是本账本装的):"
                + ";".join(p.conflict or "" for p in plan.conflicts),
                hint="确认要覆盖请加 --force(会先备份)",
            )
        if args.dry_run:
            if args.json:
                console.print_json(json.dumps({"dryRun": True, "plan": _plan_json(plan)},
                                              ensure_ascii=False))
            else:
                console.print("[dim]--dry-run:没有写任何东西[/dim]")
            return EXIT_OK
        _confirm("继续?", assume_yes=args.yes)
        records = installer.apply(plan, ledger=ledger, store=source.name,
                                  scope=scope, force=args.force)
    finally:
        if ctx.client is not None:
            ctx.client.close()

    if args.json:
        console.print_json(json.dumps({
            "plan": _plan_json(plan),
            "installed": [record.as_dict() for record in records],
        }, ensure_ascii=False))
        return EXIT_OK

    for record in records:
        console.print(f"[green]已安装[/green] {record.itemId} → {record.target}")
    needs = plan.needs_config
    if needs:
        console.print("[yellow]还要补环境变量[/yellow](qi-mcp 在连接时读,缺了会明确报错):")
        for name in needs:
            console.print(f"  export {name}=…")
    return EXIT_OK


def _install_kb(args: argparse.Namespace, scope: str, store: str | None = None) -> int:
    """KB 的安装走 qi-kb 的配置写入器，不复用 store 的 PatchJson/账本。"""
    plan = service.plan(getattr(args, "cwd", None), ids=args.ids, scope=scope,
                        force=args.force, store=store)
    if args.json and args.dry_run:
        console.print_json(json.dumps({"dryRun": True, "plan": plan}, ensure_ascii=False))
        return EXIT_OK
    if not args.json:
        for item in plan["items"]:
            mark = " [yellow](已存在[/yellow]" if item.get("conflict") else ""
            console.print(f"[cyan]{item['id']}[/cyan] {item['name']} → "
                          f"{', '.join(item.get('targets') or [])}{mark}")
            if item.get("conflict"):
                err_console.print(f"[yellow]! {item['conflict']}[/yellow]"
                                  f"，确认覆盖请加 --force")
    if plan.get("blocked") and not args.force:
        if args.yes or not sys.stdin.isatty():
            raise Conflict("KB 已由其它 provider 安装", hint="覆盖请加 --force")
        if not _confirm("覆盖为当前 provider？"):
            console.print("[dim]保留原样[/dim]")
            return EXIT_OK
        args.force = True
    if args.dry_run:
        if args.json:
            console.print_json(json.dumps({"dryRun": True, "plan": plan}, ensure_ascii=False))
        else:
            console.print("[dim]--dry-run:没有写任何东西[/dim]")
        return EXIT_OK
    _confirm("继续?", assume_yes=args.yes)
    data = service.install(getattr(args, "cwd", None), ids=args.ids, scope=scope,
                           force=args.force, store=store)
    if args.json:
        console.print_json(json.dumps(data, ensure_ascii=False))
        return EXIT_OK
    for item in data.get("installed") or []:
        console.print(f"[green]已安装[/green] {item['id']}（{item.get('action') or 'install'}）")
    for item in data.get("skipped") or []:
        console.print(f"[dim]{item['id']} 跳过：{item['reason']}[/dim]")
    return EXIT_OK


def _plan_json(plan: planner.Plan) -> list[dict[str, Any]]:
    """计划 → 机器格式。`note` 里装着"为什么这么转 / 丢弃了什么 / 为什么没写 model"。"""
    return [{
        "id": planned.item.id, "name": planned.item.name,
        "version": planned.item.version, "target": str(planned.target),
        "note": planned.note,
        "targets": [str(action.target) for action in planned.actions],
        "needsConfig": planned.needs_config, "conflict": planned.conflict,
    } for planned in plan.items]


def _print_plan(plan: planner.Plan, source: Source, scope: str) -> None:
    console.print(f"\n[bold]{source.name}[/bold] · {source.base_url} · scope={scope}\n")
    for planned in plan.items:
        item = planned.item
        flag = " [red](冲突)[/red]" if planned.conflict else ""
        needs = " · 需补 " + ",".join(planned.needs_config) if planned.needs_config else ""
        console.print(f"  [cyan]{item.id:<14}[/cyan] {item.name}{flag}")
        console.print(f"      [dim]{planned.note}{needs}[/dim]")
        console.print(f"      → {_targets(planned)}")
    for item, reason in plan.skipped:
        console.print(f"  [dim]{item.id} 跳过:{reason}[/dim]")
    if plan.conflicts:
        for planned in plan.conflicts:
            console.print(f"  [red]冲突[/red]:{planned.conflict}")
    console.print(
        f"\n共 {len(plan.items)} 条 · 需要补配置 {len(plan.needs_config)} 项"
    )


def _targets(planned: planner.Planned) -> str:
    shown = [str(action.target) for action in planned.actions]
    return "、".join(dict.fromkeys(shown))


# ── download ───────────────────────────────────────────


def cmd_download(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    source = cfg.source(None)
    args.ids, args.kind = _kind_positional(args.ids, args.kind)
    catalog = _load_catalog(args, source, http_factory)
    items = _resolve_ids(catalog, args.ids, kind=args.kind, ledger=Ledger.load())
    out_dir = Path(args.output or ".").expanduser()
    ctx = _plan_ctx(args, source, http_factory, _scope_of(args, cfg),
                    getattr(args, "cwd", None))
    if ctx.client is None:
        raise UsageError("--offline 不能 download(缓存里没有内容)")
    written: list[Path] = []
    try:
        for item in items:
            module = get_kind(item.kind)
            if hasattr(module, "download"):
                written.extend(module.download(item, ctx, out_dir, raw=args.raw))
                continue
            # mcp / a2a:产物就是一段 JSON。**不碰配置目录** —— 放哪由人决定
            # (它可能是某个角色私有的 mcp.json,商店不该假设)
            planned = module.plan(item, ctx)
            fragments = [action for action in planned.actions
                         if hasattr(action, "section")]
            if not fragments:
                # agent 这类是**多文件目录**,"只取一件"对它没意义 —— 说清楚,
                # 而不是静默写出 0 个文件还报成功。
                raise UsageError(
                    f"{item.kind} 不支持 download(它是多文件的目录)",
                    hint=f"用 `qi store install {item.id}` 装,或 `qi store info {item.id}` 看内容",
                )
            for action in fragments:
                payload = {action.section: {action.key: action.value}}
                path = out_dir / f"{item.kind}-{action.key}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
                written.append(path)
    finally:
        if ctx.client is not None:
            ctx.client.close()
    for path in written:
        console.print(f"[green]已保存[/green] {path}")
    if written:
        console.print("[dim]只取件:没有改任何配置,也没有记账[/dim]")
    return EXIT_OK


# ── list / uninstall ───────────────────────────────────


def cmd_list(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    ledger = Ledger.load()
    records = [record for record in ledger.items if not args.kind or record.kind == args.kind]
    kb_rows: list[dict[str, Any]] = []
    if not args.kind or args.kind == "kb":
        try:
            kb_rows = [row for row in service.installed(getattr(args, "cwd", None))["items"]
                       if row.get("kind") == "kb"]
        except Exception:  # noqa: BLE001 读 KB 配置失败不该让商店列表 500
            kb_rows = []
    if args.json:
        console.print_json(json.dumps(
            [record.as_dict() for record in records] + kb_rows, ensure_ascii=False))
        return EXIT_OK
    if not records and not kb_rows:
        console.print("[dim]还没有装任何东西。`qi store search` 看看有什么[/dim]")
        return EXIT_OK
    table = Table(title=f"已安装 {len(records) + len(kb_rows)} 条", title_justify="left")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("名称", no_wrap=True)
    table.add_column("作用域")
    table.add_column("版本", style="dim")
    table.add_column("待补配置", style="yellow")
    table.add_column("落点", overflow="ellipsis", max_width=40, style="dim")
    for record in records:
        table.add_row(record.itemId, record.name, record.scope, record.version,
                      ", ".join(record.needs_config) or "-", record.target or "(配置键)")
    for row in kb_rows:
        table.add_row(row["id"], row["name"], row.get("scope", ""), "-", "-",
                      row.get("target", ""))
    console.print(table)
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    if args.ids and any(str(key).startswith("kb:") for key in args.ids):
        if not all(str(key).startswith("kb:") for key in args.ids):
            raise UsageError("KB 与其它 kind 请分开卸载")
        args.ids, _ = _split_store_suffix(args.ids)
        _confirm(f"卸载 {len(args.ids)} 个知识库？(只删本地绑定)", assume_yes=args.yes)
        for key in args.ids:
            data = service.uninstall(getattr(args, "cwd", None), id=key)
            console.print(f"[green]已卸载[/green] {data['id']}（{len(data['removed'])} 处改动）")
        return EXIT_OK
    ledger = Ledger.load()
    scope = getattr(args, "scope", None)
    if args.ids:
        records = []
        for key in args.ids:
            record = ledger.find(key, scope) or ledger.by_name(key, scope)
            if record is None:
                raise NotFound(f"账本里没有 `{key}`", hint="`qi store list` 看已装的")
            records.append(record)
    else:
        pool = [record for record in ledger.items if not scope or record.scope == scope]
        if not pool:
            console.print("[dim]账本里没有可卸载的条目[/dim]")
            return EXIT_OK
        records = _choose_records(pool, ledger)
    for record in records:
        console.print(f"[dim]{record.itemId} {record.name} → {record.target or '(配置键)'}[/dim]")
    _confirm(f"卸载 {len(records)} 条?", assume_yes=args.yes)
    for record in records:
        removed = installer.uninstall(record, ledger=ledger)
        console.print(f"[green]已卸载[/green] {record.itemId}({len(removed)} 处改动)")
    return EXIT_OK


# ── check / upgrade / sync ─────────────────────────────


def cmd_check(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    source = cfg.source(None)
    ledger = Ledger.load()
    catalog = _load_catalog(args, source, http_factory)
    rows = ops.diff(ledger, catalog)

    declared = ops.wanted_specs(cfg)
    missing: list[tuple[str, str]] = []
    for spec in declared:
        try:
            item = catalog.find(spec)
        except StoreError as exc:
            missing.append((spec, str(exc)))
            continue
        if ledger.find(item.id) is None:
            missing.append((spec, "声明了但没装"))

    if args.json:
        console.print_json(json.dumps({
            "installed": [{"itemId": r.itemId, "name": r.name, "scope": r.scope,
                           "version": r.version,
                           "upstream": (item.version if item else None),
                           "status": status}
                          for r, item, status in rows],
            "wanted_missing": [{"spec": spec, "reason": reason}
                               for spec, reason in missing],
        }, ensure_ascii=False))
        return EXIT_OK

    if not rows and not missing:
        console.print("[dim]没有装任何东西,也没有声明[/dim]")
        return EXIT_OK
    if rows:
        table = Table(title="已装条目与上游对比", title_justify="left")
        table.add_column("id", style="cyan", no_wrap=True)
        table.add_column("名称", no_wrap=True)
        table.add_column("作用域")
        table.add_column("已装版本", style="dim")
        table.add_column("上游", style="dim")
        table.add_column("状态")
        for record, item, status in rows:
            style = "yellow" if item is not None and item.version != record.version else (
                "red" if item is None else "dim")
            table.add_row(record.itemId, record.name, record.scope, record.version,
                          item.version if item else "-", f"[{style}]{status}[/{style}]")
        console.print(table)
    if missing:
        console.print("[yellow]声明了但没装[/yellow](" + str(len(missing)) + "):")
        for spec, reason in missing:
            console.print(f"  {spec} — {reason}")
        console.print("[dim]用 `qi store sync` 按声明补装[/dim]")
    return EXIT_OK


def cmd_upgrade(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    source = cfg.source(None)
    ledger = Ledger.load()
    if args.offline:
        raise UsageError("离线(offline)不能 upgrade", hint="缓存里只有目录元数据")
    catalog = _load_catalog(args, source, http_factory)
    rows = ops.diff(ledger, catalog)

    if args.ids:
        wanted_ids = {key for key in args.ids}
        chosen = [(r, item) for r, item, _status in rows
                  if r.itemId in wanted_ids or r.name in wanted_ids]
    else:
        chosen = [(r, item) for r, item, status in rows
                  if item is not None and item.version != r.version]

    if not chosen:
        console.print("[dim]没有可升级的(全部与上游一致)[/dim]")
        return EXIT_OK

    console.print(f"[bold]将升级 {len(chosen)} 条[/bold]")
    for record, item in chosen:
        console.print(f"  [cyan]{record.itemId}[/cyan] {record.name}: "
                      f"{record.version} → {(item.version if item else '?')}")
    if args.dry_run:
        console.print("[dim]--dry-run:没有写任何东西[/dim]")
        return EXIT_OK
    _confirm("继续?", assume_yes=args.yes)

    for record, item in chosen:
        if item is None:
            console.print(f"[yellow]跳过 {record.itemId}:上游已不可见[/yellow]")
            continue
        # 一条一条来:同一批里可能跨 scope,而 scope 决定落点根
        ctx = PlanContext(root=planner.scope_root(record.scope, getattr(args, "cwd", None)),
                          scope=record.scope,
                          store_name=source.name, base_url=source.base_url,
                          client=_open(source, http_factory),
                          name_override=record.name)     # 保持原来的落盘名/键名
        try:
            plan = planner.build([item], ctx, ledger=ledger)
            # 升级是"本账本换掉自己的东西",所以 force(先备份旧版) 是对的
            installer.apply(plan, ledger=ledger, store=source.name,
                            scope=record.scope, force=True)
            console.print(f"[green]已升级[/green] {record.itemId} → {item.version}")
        finally:
            if ctx.client is not None:
                ctx.client.close()
    return EXIT_OK


def cmd_sync(args: argparse.Namespace, *, cfg: StoreConfig, http_factory: Any) -> int:
    """按 `wanted[]` 对账安装。**装缺的,不替你卸多的** —— 声明是"应该有",
    不等于"多出来的删掉"(照 qi 自己的 `qi sync` 口径)。"""
    source = cfg.source(None)
    specs = ops.wanted_specs(cfg)
    if not specs:
        console.print(
            "[dim]没有声明。在 " + str(paths.project_config_file(None)) +
            " 里写 wanted: [\"skill:ppt-generator\"] [/dim]"
        )
        return EXIT_OK
    if args.offline:
        raise UsageError("离线(offline)不能 sync", hint="要对账安装就得取货")

    ledger = Ledger.load()
    catalog = _load_catalog(args, source, http_factory)
    scope = _scope_of(args, cfg)
    to_install: list[StoreItem] = []
    problems: list[tuple[str, str]] = []
    for spec in specs:
        try:
            item = catalog.find(spec)
        except StoreError as exc:
            problems.append((spec, str(exc)))
            continue
        record = ledger.find(item.id)
        if record is None:
            to_install.append(item)
        elif record.version != item.version:
            console.print(f"[yellow]{item.id}[/yellow] 声明里有更新:"
                          f"{record.version} → {item.version}(用 `qi store upgrade`)")

    for spec, reason in problems:
        err_console.print(f"[yellow]⚠ {spec} 解析不了[/yellow]:{reason}")

    if not to_install:
        console.print("[dim]声明里的条目都已装齐[/dim]")
        return EXIT_OK

    ctx = _plan_ctx(args, source, http_factory, scope, getattr(args, "cwd", None))
    try:
        plan = planner.build(to_install, ctx, ledger=ledger)
        _print_plan(plan, source, scope)
        if plan.conflicts and not args.force:
            raise Conflict("目标已存在(不是本账本装的):" +
                           ";".join(p.conflict or "" for p in plan.conflicts),
                           hint="确认要覆盖请加 --force(会先备份)")
        if args.dry_run:
            console.print("[dim]--dry-run:没有写任何东西[/dim]")
            return EXIT_OK
        _confirm("按声明安装?", assume_yes=args.yes)
        installer.apply(plan, ledger=ledger, store=source.name, scope=scope,
                        force=args.force)
    finally:
        if ctx.client is not None:
            ctx.client.close()
    for item in to_install:
        console.print(f"[green]已安装[/green] {item.id}")
    if plan.needs_config:
        console.print("[yellow]还要补环境变量[/yellow]:" +
                      ", ".join(plan.needs_config))
    return EXIT_OK


def _choose_records(records: list, ledger: Ledger) -> list:
    """已装条目的选择器:同样"序号 ≠ 主键",所以两列都显示。"""
    if not sys.stdin.isatty():
        raise UsageError("非交互终端不能进选择器", hint=f"直接给 id,例如 `{records[0].itemId}`")
    table = Table(title="已安装", header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("id", style="cyan")
    table.add_column("名称")
    table.add_column("作用域")
    table.add_column("版本", style="dim")
    for index, record in enumerate(records, start=1):
        table.add_row(str(index), record.itemId, record.name, record.scope, record.version)
    console.print(table)
    raw = _prompt("输入编号（1,3 或 2-4；回车取消）: ")
    if not raw:
        raise Cancelled("已取消")
    try:
        return [records[index - 1] for index in _parse_indexes(raw, len(records))]
    except ValueError as exc:
        raise UsageError(f"输入不合法:{exc}") from exc


# ── 解析与入口 ────────────────────────────────────────────


def _add_common(item: argparse.ArgumentParser, kind_help: str) -> None:
    item.add_argument("-k", "--kind", choices=ALL_KINDS + ("agent",), help=kind_help)
    item.add_argument("--offline", action="store_true", help="读本地缓存,不联网")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qi store",
        description="从 hikqin 应用商店安装 MCP server 配置 / Skill 包 / A2A 名片",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<子命令>")

    def add(name: str, handler: Callable[..., int], help: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help, description=help)
        item.set_defaults(func=handler)
        return item

    # ── 认证 ──
    login = add("login", cmd_login, "登录并保存凭证(账号写在 store.json 的 auth 段)")
    login.add_argument("url", nargs="?", help="商店后端地址,如 http://10.1.43.6:6091")
    login.add_argument("-u", "--user", help="用户名(不给则询问,或沿用上次的)")
    login.add_argument("--password-stdin", action="store_true",
                       help="从 stdin 读一行密码(CI;避免 shell 历史)")
    login.add_argument("--token", help="直接给 access token(CI;不会被刷新)")
    login.add_argument("--store", help="源名(默认 defaultStore 或 hikqin)")
    login.add_argument("-y", "--yes", action="store_true", help="不再确认(换源等)")

    logout = add("logout", cmd_logout, "删除本地凭证(上游无吊销接口)")
    logout.add_argument("--all", action="store_true", help="所有源 + 清缓存")
    logout.add_argument("--store", help="源名")

    whoami = add("whoami", cmd_whoami, "显示当前身份与凭证剩余时间")
    whoami.add_argument("--remote", action="store_true", help="再问一次上游(/auth/me)确认")

    add("sources", cmd_sources, "列出配置的商店源")

    # ── 取货 ──
    search = add("search", cmd_search, "搜索条目(带序号与 id)")
    search.add_argument("keyword", nargs="?", help="匹配名称或描述")
    search.add_argument("--limit", type=int, default=0, help="最多显示多少条")
    search.add_argument("--json", action="store_true", help="输出 JSON")
    search.add_argument("--offline", action="store_true", help="读本地缓存")
    search.add_argument("-k", "--kind", choices=ALL_KINDS, help="只看某一种")
    search.add_argument("--store", help="源名(默认 defaultStore)")

    info = add("info", cmd_info, "看一个条目的详情(含文件树与已装状态)")
    info.add_argument("id")
    info.add_argument("--offline", action="store_true", help="读本地缓存")
    info.add_argument("-k", "--kind", choices=ALL_KINDS, help=argparse.SUPPRESS)
    info.add_argument("--store", help="源名(默认 defaultStore)")

    install = add("install", cmd_install, "安装(省略 id 则进序号选择器)")
    install.add_argument("ids", nargs="*", help="条目 id 或名称,如 ppt-generator / skill:4")
    _add_common(install, "只在这一种里选/装")
    install.add_argument("--scope", choices=("global", "project"), help="装到哪里")
    install.add_argument("-l", "--local", action="store_true", help="等于 --scope project")
    install.add_argument("--name", help="换个落盘名(MCP/A2A 改条目 key,技能不支持)")
    install.add_argument("--force", action="store_true", help="覆盖并备份已有目标")
    install.add_argument("--allow-unsafe", action="store_true", help="允许装高危标记的条目")
    install.add_argument("--dry-run", action="store_true", help="只打计划,不动手")
    install.add_argument("-y", "--yes", action="store_true", help="不再确认")
    install.add_argument("--json", action="store_true", help="计划以 JSON 输出")
    install.add_argument("--store", help="源名(默认 defaultStore)")

    download = add("download", cmd_download, "只取件(不落配置、不记账)")
    download.add_argument("ids", nargs="*", help="条目 id 或名称")
    download.add_argument("-o", "--output", help="输出目录(默认当前目录)")
    download.add_argument("--raw", action="store_true", help="skill 保留上游 zip")
    _add_common(download, "只取这一种")
    download.add_argument("-l", "--local", action="store_true", help=argparse.SUPPRESS)

    listing = add("list", cmd_list, "列出已装的条目(读账本,离线可用)")
    listing.add_argument("-k", "--kind", choices=ALL_KINDS, help="只看某一种")
    listing.add_argument("--json", action="store_true", help="输出 JSON")

    uninstall = add("uninstall", cmd_uninstall, "卸载(省略 id 则进选择器)")
    uninstall.add_argument("ids", nargs="*", help="条目 id 或名称")
    uninstall.add_argument("--scope", choices=("global", "project"), help="只在这层卸载")
    uninstall.add_argument("-y", "--yes", action="store_true", help="不再确认")

    check = add("check", cmd_check, "比对账本与上游(可升级 / 下架 / 声明未装)")
    check.add_argument("--json", action="store_true", help="输出 JSON")
    _add_common(check, "只看某一种")

    upgrade = add("upgrade", cmd_upgrade, "升级(省略 id 则升级所有可升级的)")
    upgrade.add_argument("ids", nargs="*", help="条目 id 或名称")
    upgrade.add_argument("--dry-run", action="store_true", help="只打计划,不动手")
    upgrade.add_argument("-y", "--yes", action="store_true", help="不再确认")
    upgrade.add_argument("--offline", action="store_true", help=argparse.SUPPRESS)
    upgrade.add_argument("-k", "--kind", choices=ALL_KINDS, help=argparse.SUPPRESS)

    sync = add("sync", cmd_sync, "按 store.json 的 wanted[] 补装缺的条目")
    sync.add_argument("--scope", choices=("global", "project"), help="装到哪里")
    sync.add_argument("-l", "--local", action="store_true", help="等于 --scope project")
    sync.add_argument("--force", action="store_true", help="覆盖并备份已有目标")
    sync.add_argument("--dry-run", action="store_true", help="只打计划,不动手")
    sync.add_argument("-y", "--yes", action="store_true", help="不再确认")
    _add_common(sync, "只看某一种")

    for name, reason in NOT_IMPLEMENTED.items():
        item = add(name, cmd_pending, f"{name}(未实现)")
        item.add_argument("rest", nargs="*", help=argparse.SUPPRESS)
        item.set_defaults(reason=reason)
    return parser


def cmd_pending(args: argparse.Namespace, **_: Any) -> int:
    raise UsageError(getattr(args, "reason", "这个 kind 还没实现"))


def run(argv: list[str] | None = None, *, cwd: Path | None = None,
        http_factory: Any = None) -> int:
    """返回退出码。**所有可预期失败都变成 `StoreError`**,这里统一翻译 ——
    命令层不做 if/else 映射(退出码只有一个来源)。"""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:            # argparse 自己的用法错误(2) / --help(0)
        return int(exc.code or 0)
    # **项目级落点靠它**:早先各命令自己传 `None` → `Path.cwd()`,于是 `run(cwd=…)`
    # 只在测试里生效、真实调用却写到进程工作目录。一个挂在这里,所有命令都有。
    args.cwd = cwd
    if getattr(args, "func", None) is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        cfg = config.load(cwd)
        for warning in cfg.warnings:
            err_console.print(f"[yellow]警告[/yellow]:{warning}")
        return args.func(args, cfg=cfg, http_factory=http_factory)
    except StoreError as exc:
        for index, line in enumerate(exc.lines()):
            err_console.print(f"[red]错误[/red]:{line}" if index == 0 else f"[dim]{line}[/dim]")
        return exc.exit_code
    except KeyboardInterrupt:
        err_console.print("[yellow]已取消[/yellow]")
        return EXIT_CANCELLED
    except httpx.HTTPError as exc:                     # 兜底:别把栈丢给用户
        err_console.print(f"[red]错误[/red]:{exc}")
        return EXIT_USAGE


def main(argv: list[str]) -> int:
    """`qi store …` 的入口(宿主只把命令名之后的 argv 递进来)。"""
    return run(argv)
