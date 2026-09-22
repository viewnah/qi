"""CLI 入口(对齐 docs/cli.md;P1-P6)。

顶层:`qi [options] [--] [消息...]` —— 不带 `-p` 且 `--mode text` 时进 TUI(裸 `qi`
或 `qi "问题"`,后者把消息作为首条提交);否则无头执行后退出(`-p` / `--mode json`)。
子命令:doctor / models list / auth / init / agents / sessions / config / web / version
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from typer.core import TyperGroup

from . import __version__, paths, prompt
from .auth import AuthStore, DEFAULT_API_KEY_ENV, resolve_key
from .config import (
    DEFAULT_API,
    models_file_for_provider,
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
from .llm import THINKING_LEVELS, ThinkingLLMClient
from .extensions import ExtensionBus, CliCommandRegistry
from .loader import (
    LoadError,
    load_top_level_skills,
)
from .paths import MODELS_FILE_NAME, SETTINGS_FILE_NAME, global_home, project_home
from .registry import CapabilityRegistry, ToolCatalog, discover_extensions
from .packages import install_hints, package_report
from .session import SessionStore
from .settings import (
    SCOPES,
    TUI_MODES,
    QiSettings,
    SettingsError,
    extension_dirs,
    load_settings,
    load_settings_by_scope,
    load_settings_raw,
    parse_value,
    resolve_project_trust,
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
        # `--` 之后一律当字面消息。但 click 会在解析时把那段内容当普通位置参数混进
        # `ctx.args`,而且**把 `--` 标记本身丢掉** —— 那样就再也区分不出“转义后的
        # `--plan`”与“扩展旗标 `--plan`”。所以在这里(唯一还看得到标记的地方)先记下分割点。
        ctx.meta["qi_after_double_dash"] = (
            list(args[args.index("--") + 1:]) if "--" in args else [])
        super().parse_args(ctx, args)
        # TyperGroup.parse_args 已把首个位置参数挪进 _protected_args(当子命令)。
        if not ctx._protected_args:
            return ctx.args
        if ctx.params.get("print_mode") or self.get_command(ctx, ctx._protected_args[0]) is None:
            ctx.args = [*ctx._protected_args, *ctx.args]
            ctx._protected_args = []
        return ctx.args


def _core_option_names(ctx: typer.Context) -> set[str]:
    """顶层自己的长选项名(`--no-session` → `no-session`)。

    用处:识别"把 qi 自己的选项写到了消息后面"—— click 的 Group 遇到位置参数后就
    不再解析选项,那些 token 会落到 `ctx.args`,再被当成**扩展旗标**。报"没有对应的
    扩展旗标"是在指错方向,用户写的明明是 core 的旗标。
    """
    names: set[str] = set()
    for param in getattr(ctx.command, "params", []) or []:
        for opt in (*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ())):
            if str(opt).startswith("--"):
                names.add(str(opt)[2:])
    return names


def _classify_cli_args(tokens: list[str]) -> tuple[list[str], list[str], list[str]]:
    """把 click 留下的 token 分成「消息 / 扩展旗标 / 错误」(照抄 pi 的三条规则)。

    * `--name=value` / `--name` → 扩展旗标候选(装完再对账,**未注册的报错**)
    * `-x`(单横线)→ **直接报错**:短旗标不允许扩展占用(pi 同款:`-z` → Unknown option)
    * 其余 → 消息

    **一处刻意与 pi 不同**:pi 会把 `--flag` 后面的 token 当成它的值**吃掉**(布尔旗标
    也一样 —— 于是 `pi --plan "帮我看这个"` 里的那句 prompt 就没了)。qi **不消费后面的
    token**:字符串旗标写 `--name=value`(与 `--ext` 形式一致),`--name` 单独出现就按声明
    类型解释。代价是不支持 `--agent reviewer` 这种空格写法;收益是**永远不会吃掉用户的一句话**。
    """
    messages: list[str] = []
    flags: list[str] = []
    errors: list[str] = []
    for index, token in enumerate(tokens):
        if token == "--":
            # `--` 之后一律当字面消息(pi 同款)。真实路径里 click 已经把它剥掉了
            # (见 `QiGroup.parse_args` 存的 `qi_after_double_dash`),但这里也要处理:
            # 否则它会被当成“名字为空的旗标”混进 flags。
            messages.extend(tokens[index + 1:])
            break
        if token.startswith("--"):
            flags.append(token[2:])
        elif token.startswith("-") and token != "-":
            errors.append(f"未知选项: {token}")
        else:
            messages.append(token)
    return messages, flags, errors


app = typer.Typer(
    name="qi",
    cls=QiGroup,
    help="编码 agent 框架:单 agent core;MCP / 多 agent / web 走扩展(参数尽量对齐 pi)",
    no_args_is_help=False,  # 无参 → 进 TUI(见 callback);帮助用 qi -h
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True,
                      "help_option_names": ["-h", "--help"]},
)


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
    print_mode: bool = typer.Option(False, "--print", "-p", help="无头一次执行(单 agent)"),
    cont: bool = typer.Option(False, "--continue", "-c", help="续上次会话"),
    session_id: str | None = typer.Option(None, "--session", help="指定会话 id"),
    fork_id: str | None = typer.Option(None, "--fork", help="从已有会话分叉出新会话(path|id 前缀)"),
    name: str | None = typer.Option(None, "--name", "-n", help="会话显示名"),
    no_session: bool = typer.Option(False, "--no-session", help="不落盘(临时)"),
    export_file: str | None = typer.Option(None, "--export", help="导出会话 JSONL 到文件后退出"),
    mode: str = typer.Option("text", "--mode", help="输出: text|json(json 隐含无头,不进 TUI)"),
    verbose: bool = typer.Option(False, "--verbose", help="显示分派与工具调用进度(默认只输出答案,对齐 pi)"),
    skill: list[str] = typer.Option(None, "--skill", help="额外技能文件/目录(可重复;叠加)"),
    no_skills: bool = typer.Option(False, "--no-skills", "-ns", help="关闭技能自动发现(--skill 仍生效)"),
    extension: list[str] = typer.Option(None, "-e", "--extension",
                                       help="一次性试用一个扩展目录(可重复;仅本进程,scope=temporary)。"
                                            "与 `--ext` 不同:`--ext` 是给扩展声明过的**旗标**传值(name=value)"),
    ext: list[str] = typer.Option(None, "--ext",
                                  help="扩展旗标:name=value(可重复;由扩展 registerFlag 声明。"
                                       "已声明的旗标也可直接写 --name 或 --name=value)"),    approve: bool = typer.Option(False, "--approve", "-a", help="信任项目 .qi(加载项目级扩展)"),
    no_approve: bool = typer.Option(False, "--no-approve", "-na", help="不信任项目 .qi(显式拒绝)"),
    thinking: str | None = typer.Option(None, "--thinking", help="思考级别: " + "/".join(THINKING_LEVELS)),
    tools: str | None = typer.Option(None, "--tools",
                                     help="工具白名单(严格;逗号/空格分隔。对齐 pi 的 --tools,可写 -t)"),
    exclude_tools: str | None = typer.Option(None, "--exclude-tools",
                                             help="从最终工具集里排除这些工具(对齐 pi 的 -xt)"),
    no_tools: bool = typer.Option(False, "--no-tools", help="禁用全部工具(对齐 pi 的 -nt)"),
    no_builtin_tools: bool = typer.Option(False, "--no-builtin-tools",
                                          help="禁用内置工具、保留扩展工具(对齐 pi 的 -nbt)"),
    offline: bool = typer.Option(False, "--offline",
                                 help="对齐 pi 的旗标;qi 启动期没有任何网络操作,所以它无实际作用"),
    version: bool = typer.Option(False, "--version", "-v", is_eager=True, help="显示版本并退出"),
    # ── 模型(pi 同名)──
    provider: str | None = typer.Option(None, "--provider", help="provider 名(本次运行覆盖 models.json 的默认)"),
    model: str | None = typer.Option(None, "--model",
                                     help='模型:"provider/模型",可带 ":<思考级别>" 后缀'),
    api_key: str | None = typer.Option(None, "--api-key",
                                       help="本次运行的密钥(优先于 auth store / env / models.json;**不落盘**)"),
    models_cycle: str | None = typer.Option(None, "--models",
                                            help="Ctrl+P 轮换的模型清单(逗号分隔;仅本次运行,不回写 settings)"),
    list_models: bool = typer.Option(False, "--list-models",
                                     help="列出可用模型后退出(搜索词写成位置参数:`qi --list-models sonnet`)"),
    # ── 会话(pi 同名)──
    resume: bool = typer.Option(False, "--resume", "-r", help="浏览并选择一个历史会话(需要 TTY)"),
    exact_session_id: str | None = typer.Option(None, "--session-id",
                                                help="用精确的项目会话 id(不存在则创建)"),
    session_dir: str | None = typer.Option(None, "--session-dir",
                                           help="会话存储目录(覆盖 settings.sessionDir)"),
    # ── 系统提示词与资源开关(pi 同名)──
    system_prompt: str | None = typer.Option(None, "--system-prompt",
                                             help="整体替换基座(与 `.qi/SYSTEM.md` 同一语义)"),
    no_extensions: bool = typer.Option(False, "--no-extensions", "-ne",
                                       help="关掉扩展发现(`-e` 显式给的仍然生效)"),
    no_context_files: bool = typer.Option(False, "--no-context-files", "-nc",
                                          help="不注入 AGENTS.md / CLAUDE.md"),
    # ── 对齐 pi 但**功能未实现**的旗标:接受,然后明确报出来(不假装,也不掉进扩展旗标的报错)──
    prompt_template: list[str] = typer.Option(None, "--prompt-template", help="[未实现] prompt 模板"),
    no_prompt_templates: bool = typer.Option(False, "--no-prompt-templates", "-np",
                                             help="[未实现] 关掉 prompt 模板发现"),
    theme_file: list[str] = typer.Option(None, "--theme", help="[未实现] 自定义主题文件"),
    use_theme: str | None = typer.Option(None, "--use-theme", help="[未实现] 指定初始主题"),
    no_themes: bool = typer.Option(False, "--no-themes", help="[未实现] 关掉主题发现"),
    tui_mode: str | None = typer.Option(None, "--tui-mode",
                                        help="TUI 模式(regular|fullscreen;默认 fullscreen)"),
    append_prompt: list[str] = typer.Option(None, "--append-system-prompt",
                                            help="追加到 system prompt 末尾(可重复;值是文件路径时读文件内容)"),
) -> None:
    if version:
        console.print(f"qi {__version__}")
        raise typer.Exit()
    _reject_unimplemented_flags(
        prompt_template=prompt_template, no_prompt_templates=no_prompt_templates,
        theme_file=theme_file, use_theme=use_theme, no_themes=no_themes, mode=mode)
    # `--tui-mode` 的值域很窄:打错就当场报(否则会静默回落默认,用户以为切过去了)
    if tui_mode is not None and tui_mode.strip().lower() not in TUI_MODES:
        err_console.print(f"[red]未知 TUI 模式: {escape(tui_mode)}"
                          f"(可选 {'/'.join(TUI_MODES)}）[/red]")
        raise typer.Exit(code=2)
    tui_mode = tui_mode.strip().lower() if tui_mode else None
    # `--tools`(严格白名单)与另外两个整集选择互相矛盾 —— 报出来,不替用户选一个赢。
    if tools and (no_tools or no_builtin_tools):
        err_console.print("[red]--tools 与 --no-tools/--no-builtin-tools 冲突:"
                          "前者是严格白名单,后两者是整集选择[/red]")
        raise typer.Exit(code=2)
    if ctx.invoked_subcommand is not None:
        # 开了 `ignore_unknown_options` 后,子命令自己仍然严格解析,但**顶层**的未知选项
        # 会落到这里(实测:`qi --zzz agents list` 会让子命令不再被分派)。不能静默丢。
        _leftover_messages, _leftover_flags, leftover_errors = _classify_cli_args(
            list(ctx.args))
        if leftover_errors:
            for problem in leftover_errors:
                err_console.print(f"[red]{escape(problem)}[/red]")
            raise typer.Exit(code=2)
        return
    if export_file:
        _cmd_export(session_id or "", Path(export_file))
        raise typer.Exit()
    # `--` 之后一律当字面消息(click 已把标记吃掉,所以从 QiGroup 记下的分割点取回)
    after_dashdash = list(ctx.meta.get("qi_after_double_dash") or [])
    tokens = list(ctx.args)
    if after_dashdash and tokens[-len(after_dashdash):] == after_dashdash:
        tokens = tokens[:-len(after_dashdash)]
    messages, cli_flags, cli_errors = _classify_cli_args(tokens)
    # `--list-models [搜索词]`:pi 的可选值 click 表达不了(实测 `--list-models` 单独出现会报
    # "requires an argument"),所以搜索词走**位置参数** —— `qi --list-models sonnet` 与 pi 同形。
    if list_models:
        _cmd_list_models(" ".join(messages).strip() or None)
        raise typer.Exit()
    # 把 core 自己的选项写到消息后面 = 解析不到:报得具体一点(否则用户会去查那个
    # 根本没写的 `--ext`)。`--tools=read` 这种带值的也按名字判。
    core_options = _core_option_names(ctx)
    misplaced = [name for name in cli_flags
                 if name.split("=", 1)[0].replace("_", "-") in core_options]
    if misplaced:
        for name in misplaced:
            err_console.print(
                f"[red]选项 `--{name.split('=', 1)[0]}` 要写在**消息之前**:"
                f"qi 的顶层选项在遇到消息后不再解析(写成 `qi --{name.split('=', 1)[0]} … \"消息\"`)[/red]")
        raise typer.Exit(code=2)
    messages += after_dashdash
    if cli_errors:
        for problem in cli_errors:
            err_console.print(f"[red]{escape(problem)}[/red]")
        raise typer.Exit(code=2)
    prompt = " ".join(messages).strip()
    # 扩展旗标两个通道都接住:`--ext name=value` 与直接写的 `--name[=value]`。
    # 直接写的放后面 → 同名声时它胜(更具体的写法优先)。
    extension_flags = list(ext or []) + cli_flags
    # `--provider` / `--model` 合成一个 `provider/模型`;拼不出来就在**这里**报,
    # 不退化成"默默用默认模型"(那样用户以为旗标生效了)
    try:
        model_spec = _model_spec(provider, model)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    # `--models`:本次运行的 Ctrl+P 轮换清单(**不回写 settings**)
    from .runtime import split_tool_list

    scoped = split_tool_list(models_cycle) if models_cycle else None
    # `--system-prompt` / `--append-system-prompt`:与 SYSTEM.md 同义(替换 / 追加)。
    # 值是**可读文件**时读文件内容 —— pi 的 `--append-system-prompt` 就是这个口径
    # (help 里写的是 "Append text or file contents")。
    append_prompt = [_text_or_file(item) for item in (append_prompt or [])]
    system_prompt = _text_or_file(system_prompt) if system_prompt else None
    # docs/cli.md §1 / 对齐 pi:不带 -p 恒为交互(`-p` 才是无头),也不再需要 `qi tui`;
    # 给了消息就进 TUI 并把它作为首条消息发出(pi 的 `pi "问题"` 同款)。
    # `--mode json` 是脚本路径(输出事件流,不是 TUI),仍走无头。
    interactive = not print_mode and mode == "text"
    if resume and not interactive:
        err_console.print("[red]`-r/--resume` 要选会话,只能在 TUI 里用"
                          "(无头下请用 `--session <id>`)[/red]")
        raise typer.Exit(code=2)
    if interactive:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            # 非 TTY(管道/CI):退化为提示,而不是抛 traceback / 卡住。
            console.print("交互界面需要 TTY;无头用法: qi -p \"问题\" [--agent name]")
            raise typer.Exit(code=0)
        _launch_tui(prompt or None, session_id=session_id, cont=cont, fork_id=fork_id,
                    no_session=no_session, name=name,
                    approve_project=_trust_flag(approve, no_approve),
                    extension_flags=extension_flags,
                    extra_extension_paths=[Path(p) for p in (extension or [])],
                    tools=tools, exclude_tools=exclude_tools,
                    no_tools=no_tools, no_builtin_tools=no_builtin_tools,
                    append_system_prompt=append_prompt,
                    resume=resume, exact_session_id=exact_session_id,
                    base_prompt_override=system_prompt,
                    no_extensions=no_extensions, no_context_files=no_context_files,
                    session_dir_path=session_dir, model_override=model_spec,
                    api_key=api_key, scoped_models=scoped, tui_mode=tui_mode)
        return
    if not prompt:
        usage = (
            "qi -p \"问题\"" if print_mode
            else "qi --mode json \"问题\"(--mode json 为无头输出)"
        )
        console.print(f"[red]需要消息内容: {usage}[/red]")
        raise typer.Exit(code=2)
    if thinking is not None and thinking.strip().lower() not in THINKING_LEVELS:
        console.print(f"[red]未知思考级别: {thinking}(可选 {"/".join(THINKING_LEVELS)}）[/red]")
        raise typer.Exit(code=2)
    from .runtime import QiRuntime
    from .config import ConfigError as _CfgErr
    from .loader import LoadError as _LoadErr
    try:
        # 轮次:qi 自己**不设上限**(对齐 pi —— 核心循环与 print 模式都没有 maxTurns)。
        # `RunnerSettings.stop_after` 是留给嵌入方的钩子(qi 生产代码不用它,
        # 与 pi 定义了却从不实现 `shouldStopAfterTurn` 同形)。
        runtime = QiRuntime(skills_enabled=not no_skills,
                            thinking_level=thinking,
                            approve_project=_trust_flag(approve, no_approve),
                            extension_flags=extension_flags,
                            extra_skill_paths=[Path(p) for p in (skill or [])],
                            extra_extension_paths=[Path(p) for p in (extension or [])],
                            tools=tools, exclude_tools=exclude_tools,
                            no_tools=no_tools, no_builtin_tools=no_builtin_tools,
                            append_system_prompt=append_prompt,
                            base_prompt_override=system_prompt,
                            no_extensions=no_extensions,
                            no_context_files=no_context_files,
                            session_dir_path=session_dir,
                            model_override=model_spec,
                            api_key=api_key,
                            scoped_models=scoped)
    except _LoadErr as exc:
        console.print(f"[red]装载失败:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    except _CfgErr as exc:
        console.print(f"[red]配置错误:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    # `--ext` 报错 → **退出码 2**(与未知 CLI 选项同类:这是命令行打错了,不是运行时失败)。
    # 必须在这里拦:继续跑的话用户会以为旗标生效了,而实际值一个都没进去。
    if runtime.flag_errors:
        for problem in runtime.flag_errors:
            err_console.print(f"[red]{escape(problem)}[/red]")
        raise typer.Exit(code=2)
    store = runtime.sessions
    # 启动提示(未信任跳过项目级扩展、旧 plugins/ 目录残留…)走 stderr:
    # `-p` 的 stdout 是给脚本/管道用的,不能混入提示。
    #
    # 分两次打印:会话绑定**之后**还会产生提示(最典型的是"会话里记的模型没能恢复,
    # 已用默认模型继续" —— 见 sessions.md §6.1)。只在这里打一次的话,那条提示
    # 在无头模式下永远看不到,而它恰恰是"为什么不是我上次用的模型"的唯一解释。
    printed_notes = len(runtime.notes)
    for note in runtime.notes:
        err_console.print(f"[yellow]{escape(note)}[/yellow]")
    session = None
    if no_session:
        # 真·不落盘:以前走 `create()`,于是 `--no-session` 照样留一个文件
        session = store.ephemeral(name or "ephemeral", cwd=runtime.cwd)
    elif fork_id:
        # 对齐 pi `--fork <path|id>`:把源会话的当前分支复制成一个新会话再跑
        source = _open_session(store, fork_id)
        if source is None:
            console.print(f"[red]会话不存在: {fork_id}[/red]")
            raise typer.Exit(code=1)
        session = store.fork_at(source, source.current,
                                title=name or (f"{source.title} @fork" if source.title else "fork"))
        err_console.print(f"[dim]已从 {source.id} 分叉出新会话 {session.id}[/dim]")
    elif exact_session_id:
        # `--session-id <id>`:精确 id,**不存在则创建**(pi 同名旗标)
        session = store.get(exact_session_id) or store.create(
            name or "", cwd=runtime.cwd, session_id=exact_session_id)
    elif session_id:
        session = _open_session(store, session_id)
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
    # 绑定会话:设置类 entry 要写进这个文件,而且会话里记的模型/级别要在**这一轮之前**
    # 恢复好(否则第一轮就用错了模型 —— 见 sessions.md §6.1)
    # 防御式取用:与 TUI 的 `notify_session_tree` 同一条约定 —— 测试替身不必实现
    # 每个可选方法(此处是「可选」是因为替身也可以什么都不记)。
    bind = getattr(runtime, "bind_session", None)
    if callable(bind):
        bind(session)
    for note in runtime.notes[printed_notes:]:
        err_console.print(f"[yellow]{escape(note)}[/yellow]")

    async def _run() -> None:
        # 会话已绑定 → 通知扩展(pi 的 `session_start { reason }`);错误进 notes,不挡这一轮
        await runtime.start_session(session, reason=_session_reason(
            no_session=no_session, fork_id=fork_id, session_id=session_id, cont=cont))
        # 对齐 pi 的 `-p`:默认只输出答案;分派行/工具进度仅在 --verbose 时显示
        # (且走 stderr,不污染 stdout)。`--mode json` 本就输出全部事件,不受此开关影响。
        async for ev in runtime.stream(prompt, session):
            if mode == "json":
                # `indent=None` = 不美化:一个事件**一行**(JSONL)。
                # 这条流是给脚本/管道消费的(见 docs/json.md),多行美化会让消费端无法
                # 按行切分 —— “输出事件 JSON 行”与 pi 的 JSONL 都会不成立。
                console.print_json(data={"kind": ev.kind, "agent": ev.agent,
                                         "tool": ev.tool, "text": ev.text, "data": ev.data},
                                   indent=None)
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

    _run_headless(_run())
    # provider 拒了 reasoning_effort:已经自动降级重试,但要告知(否则用户以为级别生效了)
    client = getattr(runtime, "llm_exec", None)
    if isinstance(client, ThinkingLLMClient) and client.reasoning_dropped:
        err_console.print("[yellow]提示:该 provider 不接受 reasoning_effort,已按不思考运行[/yellow]")


def _run_headless(coro) -> None:
    """无头执行:装退出信号处理器,并把 Ctrl-C 收敛成 130。

    Ctrl-C 不经过信号处理器(asyncio 自己把主任务取消掉,子进程由 `run_shell` 的
    finally 回收),但 Python 默认会带上 traceback 并以 1 退出 —— 惯例是 130。
    抽成函数是为了可测(见 tests/test_interrupt.py)。
    """
    restore = _install_exit_signal_handlers()
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        raise typer.Exit(code=_exit_code_for(signal.SIGINT)) from None
    finally:
        restore()


# ── 无头运行的退出信号(对齐 pi 的 print 模式)──────────────────
#
# pi 在 print 模式下注册 SIGTERM /(非 Windows)SIGHUP:收到就 killTrackedDetachedChildren()
# 再以 143 / 129 退出。qi 少了轮次上限之后,这一层就是“无头被卡住”的主要兔底;
# 另外它补的是一条没有 Python 异常可依托的路径 —— 进程被 kill,清理代码不会自然跑到。
#
# SIGINT 不在这里:Python 默认转 KeyboardInterrupt → asyncio 取消主任务 →
# `run_shell` 的 finally 回收子进程组(见 tools/shell.py)。

_EXIT_CODE_BY_SIGNAL: dict[int, int] = {signal.SIGTERM: 143, signal.SIGHUP: 129}


def _exit_code_for(signum: int) -> int:
    """128 + 信号号(对齐 pi:143 = 128+15、129 = 128+1)。

    `signum` 已经是 int(信号处理器收到的也是 `signal.Signals`,IntEnum 子类),
    所以直接相加 —— 不需要 `int()` 包一层。
    """
    return _EXIT_CODE_BY_SIGNAL.get(signum, 128 + signum)


def _install_exit_signal_handlers() -> Callable[[], None]:
    """装 SIGTERM/SIGHUP 处理器:先回收在跑的子进程组,再按信号码退出。

    返回“恢复原处理器”的回调(CLI 跑完就走,但测试/嵌入方需要干净收尾)。
    """
    from .tools.shell import kill_live_children

    signals = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):        # Windows 没有 SIGHUP
        signals.append(signal.SIGHUP)
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: object) -> None:
        kill_live_children()             # 同步:信号处理器里不能 await
        raise SystemExit(_exit_code_for(signum))

    for sig in signals:
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):    # 非主线程/平台不支持 → 跳过于净
            pass

    def restore() -> None:
        for sig, old in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, old)

    return restore


def _cmd_export(session_id: str, out: Path) -> None:
    store = SessionStore()
    session = store.get(session_id) if session_id else store.latest()
    if session is None:
        console.print("[red]无可导出会话[/red]")
        raise typer.Exit(code=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 按**扩展名**选格式(pi 同口径:`.html` → 自包含 HTML,其余 → 原始 JSONL)
        from .export_html import html_wanted, write_session_html

        if html_wanted(out):
            write_session_html(session, out)
            console.print(f"[green]已导出会话(HTML,当前分支) → {out}[/green]")
            return
        shutil.copy(session.path, out)
    except OSError as exc:
        console.print(f"[red]导出失败: {escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]已导出 {session.path} → {out}[/green]")


def _refresh_models(names: str, *, all_providers: bool, local: bool,
                    timeout: float = 20.0) -> None:
    """从 provider 的 `/models` 接口拉模型列表,并进 `models.json`(**只加不删**)。

    为什么要这条:预置表是**离线种子**,而模型 id 会漂(如 DeepSeek 从
    `deepseek-v4-flash` 换成 `deepseek-flash`)—— 以厂商接口为准最省事。
    接口不返回 `contextWindow` / `maxTokens`,新加的条目先用 qi 的默认值,
    要精确就照厂商文档在 `models.json` 里补。
    """
    from .model_catalog import CatalogError, fetch_model_ids, merge_model_ids

    try:
        cfg, _files = load_config()
    except ConfigError as exc:
        console.print(f"[red]配置错误:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    store = AuthStore()

    wanted = [item.strip() for item in (names or "").split(",") if item.strip()]
    if all_providers:
        # “已配置”= models.json 里写过的 + 预置里**有凭证**的(与 `/model` 的口径一致)
        wanted += [name for name, prov in cfg.providers.items()
                   if name not in cfg.presetProviders or resolve_key(name, prov.apiKey, store).ok]
    wanted = list(dict.fromkeys(name.strip() for name in wanted if name.strip()))
    if not wanted:
        console.print("[red]要刷新哪个 provider?[/red] `qi init --refresh deepseek`"
                      " 或 `qi init --refresh-all`")
        raise typer.Exit(code=2)
    unknown = [name for name in wanted if name not in cfg.providers]
    if unknown:
        console.print(f"[red]未知 provider: {', '.join(unknown)}[/red]")
        console.print("[dim]看看有哪些:`qi doctor`(预置的见 `qi init --list-presets`)[/dim]")
        raise typer.Exit(code=2)

    # 写回**定义它的那个文件**(见 models_file_for_provider)—— 没定义的(预置兜底来的)
    # 才回落到用户级;`--local` 强制写项目级。
    if local:
        target_file = project_home() / MODELS_FILE_NAME
    else:
        target_file = models_file_for_provider(wanted[0]) or (global_home() / MODELS_FILE_NAME)
    target_dir = target_file.parent
    data = load_models_file(target_file)
    failures: list[str] = []

    for name in wanted:
        prov = cfg.providers[name]
        rk = resolve_key(name, prov.apiKey, store)
        if not rk.ok:
            console.print(f"[red]{name}: 拿不到 API key[/red] {rk.describe()} —— "
                          f"`qi auth login {name}` 或设约定环境变量")
            failures.append(name)
            continue
        try:
            ids = fetch_model_ids(prov.baseUrl or "", rk.key, timeout=timeout)
        except CatalogError as exc:
            console.print(f"[red]{name}: 拉取失败[/red] {escape(str(exc))}")
            failures.append(name)
            continue

        raw = (data.setdefault("providers", {})).get(name)
        if isinstance(raw, dict):
            entry = raw
        else:
            # 预置兜底来的 provider:物化时把预置**整段**写下来 —— 包括那些
            # **带核过 ctx/max 的种子模型**。只写接口返回的 id 会把这两个数丢掉
            # (接口不返回 ctx/max),而一旦写进 models.json,预置兜底就不再插手这个 provider。
            from .presets import apply_presets, get_preset

            if get_preset(name) is not None:
                apply_presets(data, [name])
            entry = data["providers"].setdefault(name, {})
            for key, value in (("baseUrl", prov.baseUrl), ("api", prov.api or DEFAULT_API),
                               ("apiKey", prov.apiKey)):
                if value and not entry.get(key):
                    entry[key] = value
        added, stale = merge_model_ids(entry, ids)
        console.print(f"[green]{name}[/green]: 接口返回 {len(ids)} 个模型;"
                      f"新增 {len(added)}"
                      + (":" + ", ".join(added) if added else ""))
        if stale:
            console.print(f"  [dim]本地有但接口没返回(保留,不替你删):"
                          f"{', '.join(stale)}[/dim]")

    if not failures:
        _strip_legacy_defaults(data)
        _write_models(target_dir, target_file, data)
    else:
        console.print("[yellow]有 provider 没刷新成功,这次不写文件。[/yellow]")
        raise typer.Exit(code=1)


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

    ext_lines, ext_warnings = _extension_report()
    if ext_lines:
        console.print("[green]扩展:[/green]")
        for line in ext_lines:
            console.print(f"  {line}")
    else:
        console.print("[dim]扩展:无(pip install qi-agents / qi-mcp / qi-web)[/dim]")
    for warning in ext_warnings:
        console.print(f"  [yellow]⚠ {escape(warning)}[/yellow]")

    # 包声明层比对(只读):声明了没装 / 装了没声明。装法输出**可复制的命令**,不自己调 pip。
    try:
        report = package_report(Path.cwd())
    except SettingsError as exc:
        # 上面已经报过一次;这里只说“包声明这一节为何缺席”
        console.print(f"  [dim]包声明:跳过({escape(str(exc))})[/dim]")
        report = None
    if report is not None:
        _print_package_report(report)

    store = AuthStore()
    if cfg.providers:
        table = Table(title="providers")
        table.add_column("provider"); table.add_column("baseUrl")
        table.add_column("api"); table.add_column("模型"); table.add_column("凭证")
        for name_, prov in cfg.providers.items():
            rk = resolve_key(name_, prov.apiKey, store)
            label = name_ + ("  [dim](预置)[/dim]" if name_ in cfg.presetProviders else "")
            table.add_row(label, prov.baseUrl or "(内置)", prov.api or DEFAULT_API,
                          str(len(prov.models)),
                          f"{'[green]OK[/green]' if rk.ok else '[red]缺密钥[/red]'}  {rk.describe()}")
        console.print(table)
        if cfg.presetProviders:
            console.print("[dim]带 (预置) 的来自 qi 的预置表(`qi init --list-presets`);"
                          "models.json 里写了同名 provider 就以你的为准[/dim]")

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

def _print_package_report(report) -> None:
    """`qi doctor` 与 `qi list` 共用的包声明输出。

    两个方向 + 认不出的那些都要印:只印一个方向会让另一个方向的偏差永远不被发现。
    """
    if report.missing:
        console.print("[green]包声明:[/green]")
        for decl in report.missing:
            console.print(f"  [yellow]✗[/yellow] 声明了但没装: {escape(decl.spec)}"
                          f" [dim]({decl.source})[/dim]")
            for hint in install_hints(decl.spec):
                console.print(f"      [dim]{escape(hint)}[/dim]")
    elif report.declared:
        tail = "与已装扩展一致" if report.consistent else ""
        console.print(f"[green]包声明:[/green] {len(report.declared)} 条{tail}")
    # 认不出的声明必须印 —— “声明了但看起来没声明”比报错难诊断得多
    for bad in report.unparsed:
        console.print(f"  [yellow]⚠ 无法解析的声明:[/yellow] {escape(bad)}")
        console.print("      [dim]用 `名字 @ URL` 写法才认得出名"
                      "(如 qi-mcp @ git+https://host/repo)[/dim]")
    if report.undeclared:
        names = "、".join(item.name for item in report.undeclared)
        console.print(f"  [dim]已装但未声明({len(report.undeclared)}): {escape(names)}"
                      " —— 写进 settings.packages 才能在 uv tool 重建后补回[/dim]")


@app.command("list")
def list_extensions() -> None:
    """列出已装的扩展,并与 settings.packages 的声明比对。"""
    try:
        report = package_report(Path.cwd())
    except SettingsError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    if not report.installed and not report.declared:
        console.print("[dim]没有已装扩展,settings.packages 里也没有声明。[/dim]")
        console.print("[dim]装法:uv tool install qi-agent --with qi-mcp[/dim]")
        return
    declared_names = {item.name for item in report.declared}
    table = Table(title="扩展")
    table.add_column("扩展"); table.add_column("通道"); table.add_column("版本")
    table.add_column("来源"); table.add_column("声明")
    for item in report.installed:
        table.add_row(item.name, item.channel, item.version or "—",
                      f"{item.origin} · {item.scope}",
                      "是" if item.name in declared_names else "否")
    for decl in report.missing:            # 声明了却加载不到 —— 单独一行,不混进已装列表
        table.add_row(decl.name, decl.channel, "—", f"{decl.spec} · {decl.source}", "未安装")
    console.print(table)
    _print_package_report(report)


def _cmd_list_models(search: str | None = None) -> None:
    """`--list-models [搜索词]`:列可用模型与默认模型。

    搜索词按**子串**过滤 `provider/模型`(pi 是模糊匹配;qi 先做最直白的那种)。
    """
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
    table.add_column("api"); table.add_column("ctx"); table.add_column("max")
    table.add_column("credential")
    shown: set[str] = set()
    for name_, prov in cfg.providers.items():
        rk = resolve_key(name_, prov.apiKey, store)
        # 预置兜底那批只列**能用的**(有凭证的)—— 没登录的 provider 列出来也没用,
        # 想知道有哪些可登录的看 `qi init --list-presets` 或直接 `qi auth login <名>`。
        # `models.json` 里显式写过的照列(那是用户自己的配置)。
        if name_ in cfg.presetProviders and not rk.ok and name_ != provider:
            continue
        for m in prov.models:
            label = f"{name_}/{m.id}"
            if search and search.strip().lower() not in label.lower():
                continue
            shown.add(label)
            provider_label = name_ + ("  [dim](预置)[/dim]"
                                      if name_ in cfg.presetProviders else "")
            table.add_row("*" if label == default_label else "", provider_label, m.id,
                          m.api or prov.api or DEFAULT_API, str(m.contextWindow),
                          str(m.maxTokens), rk.describe())
    if (default_label and default_label not in shown and provider and model
            # 搜索词对**每一行**都生效 —— 默认模型那行也不例外(它是循环外补的)
            and (not search or search.strip().lower() in default_label.lower())):
        spec = resolve_model(cfg, provider, model)
        rk = resolve_key(spec.provider, spec.api_key_ref, store)
        table.add_row("*", spec.provider, spec.model, spec.api, str(spec.context_window),
                      str(spec.max_tokens), rk.describe())
    console.print(table)


# ── 装 / 卸 / 更新(对齐 pi;判决 C 已撑销)──────────────────


def _run_pip(args: list[str]) -> int:
    """跑一次 pip,把命令**先打出来**。

    为什么先打:目标是哪个解释器只有用户知道 —— 打出来他才能看出"装错环境了",
    也能直接照拄。失败时补上 `uv tool install --with` 那条出路(只读解释器 / uv tool
    环境里 pip 会自己报错,而那时用户需要知道另一条路)。
    """
    import shlex
    import subprocess

    cmd = [sys.executable, "-m", "pip", *args]
    console.print("[dim]$ " + " ".join(shlex.quote(c) for c in cmd) + "[/dim]")
    try:
        code = subprocess.call(cmd)
    except OSError as exc:
        err_console.print(f"[red]起不了 pip:{escape(str(exc))}[/red]")
        return 1
    if code != 0:
        err_console.print(f"[red]pip 退出码 {code}[/red]")
        err_console.print("[dim]如果目标是只读解释器或 uv tool 环境,改用:"
                          "`uv tool install qi-agent --with <包>`[/dim]")
    return code


def _as_declaration(source: str) -> str:
    """把用户给的来源规整成一条**声明**:存在的目录 → `local:<绝对路径>`,其余原样。

    目录通道的存在意义就是"不装也能用" —— 所以本地目录**不调 pip**。
    """
    text = source.strip()
    if text.startswith(("pip:", "local:")):
        if text.startswith("local:"):
            return "local:" + str(Path(text[6:]).expanduser().resolve())
        return text
    maybe = Path(text).expanduser()
    if maybe.is_dir():
        return "local:" + str(maybe.resolve())
    return text


def _pip_requirement(spec: str) -> str:
    """声明 → pip 的 requirement(`pip:` 前缀去掉;裸 `名字` 也算 pip)。"""
    return spec[4:].strip() if spec.startswith("pip:") else spec.strip()


def _local_path(spec: str) -> Path:
    """声明 → 目录路径(`local:` 前缀去掉)。

    别拿 `_pip_requirement` 当通用去前缀用:它只认 `pip:`,于是 `local:/abs/x` 会被
    当成一个叫 `local:` 的目录(这个坑真踩过,测试抓出来的)。
    """
    return Path(spec[6:].strip() if spec.startswith("local:") else spec).expanduser()


def _declaration_name(spec: str) -> str:
    """声明的**归一名字**(用于去重 / 比对)。交给 `packages.parse_declaration` 判 ——
    它是唯一同时认识 `pip:` / `local:` / `名字 @ URL` / 裸路径的那处逻辑。"""
    from .packages import normalize_name, parse_declaration

    declaration = parse_declaration(spec, "cli")
    return declaration.name if declaration is not None else normalize_name(spec)


def _write_declared(scope: str, specs: list[str]) -> None:
    """把声明写回 `settings.packages`(整表替换;空列表也写 —— 那表示“都卸了”)。"""
    set_value(scope, "packages", specs)


def _declared_for(scope: str) -> list[str]:
    from .settings import load_settings_by_scope

    found = load_settings_by_scope(None).get(scope)
    return [str(item) for item in (getattr(found, "packages", None) or [])]


@app.command("install")
def install(source: str = typer.Argument(..., help="包名 / requirement / 本地目录"),
            local: bool = typer.Option(False, "--local", "-l",
                                       help="写进项目 `.qi/settings.json`(默认写全局)")) -> None:
    """装一个扩展:调 pip(本地目录只登记)+ 写进 `settings.packages`。

    与 `pi install` 同形。**先装后记**:pip 失败就不改声明 —— 否则 `qi doctor` 会
    报告一条“声明了但没装”,而那是我们刚刚制造的。
    """
    from .packages import parse_declaration

    scope = "project" if local else "user"
    declaration = parse_declaration(_as_declaration(source), "cli:install")
    if declaration is None:
        err_console.print(f"[red]认不出这个来源:{escape(source)}[/red]")
        err_console.print("[dim]裸 URL 要写成 `名字 @ URL`"
                          "(如 qi-mcp @ git+https://host/repo)[/dim]")
        raise typer.Exit(code=2)

    if declaration.channel == "pip":
        code = _run_pip(["install", _pip_requirement(declaration.spec)])
        if code != 0:
            raise typer.Exit(code=code)
    else:
        target = _local_path(declaration.spec)
        if not (target / "extension.py").is_file():
            err_console.print(f"[red]目录里没有 extension.py:{escape(str(target))}[/red]")
            err_console.print("[dim]一个目录扩展的入口固定叫 extension.py(E9)[/dim]")
            raise typer.Exit(code=2)
        console.print(f"[green]目录通道,不用装[/green] {escape(str(target))}")

    specs = _declared_for(scope)
    name = declaration.name
    specs = [s for s in specs if _declaration_name(s) != name]
    specs.append(declaration.spec)
    _write_declared(scope, specs)
    where = "项目" if local else "全局"
    console.print(f"[green]已写入{where} settings.packages:[/green] {escape(declaration.spec)}")
    console.print("[dim]`qi doctor` 会确认它真的能被宿主加载[/dim]")


@app.command("remove")
def remove(source: str = typer.Argument(..., help="包名 / 本地目录(与 install 同一个来源)"),
           local: bool = typer.Option(False, "--local", "-l", help="只从项目设置里移除")) -> None:
    """从 `settings.packages` 里移除一条声明(**不卸包** —— 与 pi 同义)。

    pi 的 `remove` 只动设置;真要卸包它不替你决定。qi 同口径:移除时把
    `pip uninstall` 命令打出来,跑不跑由你。
    """
    from .packages import normalize_name, parse_declaration

    scope = "project" if local else "user"
    declaration = parse_declaration(_as_declaration(source), "cli:remove")
    target = declaration.name if declaration is not None else normalize_name(Path(source).name)
    specs = _declared_for(scope)
    kept = [s for s in specs if _declaration_name(s) != target]
    if len(kept) == len(specs):
        err_console.print(f"[yellow]{scope} 的 settings.packages 里没有 {escape(source)}[/yellow]")
        raise typer.Exit(code=1)
    _write_declared(scope, kept)
    console.print(f"[green]已从{scope}声明里移除 {escape(source)}[/green]")
    if declaration is not None and declaration.channel == "pip":
        console.print("[dim]包本体还在环境里。真卸掉:"
                      f"{sys.executable} -m pip uninstall {escape(_pip_requirement(declaration.spec))}[/dim]")


@app.command("uninstall")
def uninstall(source: str = typer.Argument(..., help="同 remove"),
              local: bool = typer.Option(False, "--local", "-l")) -> None:
    """`remove` 的别名(pi 三个名字都有)。"""
    remove(source, local)


@app.command("update")
def update(target: str = typer.Argument(None, help="更新谁:self | pi | 包名/来源"),
           self_only: bool = typer.Option(False, "--self", help="只更新 qi 自己"),
           extensions: bool = typer.Option(False, "--extensions", help="只更新已声明的扩展"),
           models_only: bool = typer.Option(False, "--models", help="[无对应] 刷新模型目录"),
           everything: bool = typer.Option(False, "--all", help="qi 自己 + 扩展"),
           extension: str | None = typer.Option(None, "--extension", help="只更一个"),
           force: bool = typer.Option(False, "--force", help="即使已是最新也重装")) -> None:
    """更新 qi 自己 / 已声明的扩展。**没有目标时只更 qi 自己**(与 pi 同默认)。"""
    if models_only:
        # qi 没有"远端模型目录"这个概念:模型全在 models.json 里,你自己维护
        console.print("[yellow]qi 没有模型目录可以刷新[/yellow]:模型写在 `models.json`,"
                      "由你自己维护(pi 是从远端拉目录,qi 不是)。")
        return
    want_self = self_only or everything or (not extensions and not extension
                                            and target in (None, "self", "pi"))
    want_ext = extensions or everything or bool(extension) or (
        target is not None and target not in ("self", "pi"))
    if not want_self and not want_ext:
        console.print("[yellow]没说要更新什么[/yellow](用 `--self` / `--extensions` / `--all`)")
        raise typer.Exit(code=2)

    failed = 0
    if want_self:
        failed += _run_pip(["install", "--upgrade", *(
            ["--force-reinstall"] if force else []), "qi-agent"]) != 0
    if want_ext:
        pick = extension or (target if target not in (None, "self", "pi") else None)
        specs = _declared_for("user") + _declared_for("project")
        if pick:
            wanted = _declaration_name(_as_declaration(pick))
            specs = [s for s in specs if _declaration_name(s) == wanted]
            if not specs:
                err_console.print(f"[red]没有声明过 {escape(pick)}[/red]")
                raise typer.Exit(code=1)
        pip_specs = [s for s in specs if not s.startswith("local:")]
        if not pip_specs:
            console.print("[yellow]已声明的都是目录通道,没有要更新的包[/yellow]")
        for spec in pip_specs:
            failed += _run_pip(["install", "--upgrade", *(
                ["--force-reinstall"] if force else []), _pip_requirement(spec)]) != 0
    raise typer.Exit(code=1 if failed else 0)


# ── auth / init ──────────────────────────────────────

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
        raise _AuthError(f"未知模型 {name!r}(用 `qi --list-models` 看已配置的模型)")
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

def _open_resource_panel(cwd: Path) -> None:
    """TTY 下开资源启停面板(pi 的 `pi config` 位置);保存后把改动写成声明。

    面板只回“哪些保持启用”,写声明的活在 `packages.apply_resource_selection` ——
    所以取消时**没有任何副作用**(调用方根本不调那个函数)。
    """
    from .packages import apply_resource_selection, list_resources

    resources = list_resources(cwd)
    if not resources:
        console.print("[dim]没有可启停的资源(没装扩展、settings.packages 也是空的)。[/dim]")
        return
    try:
        from .tui import run_resource_panel
    except Exception as exc:      # noqa: BLE001 textual 依赖问题 → 退化成表,不留下一个死命令
        console.print(f"[yellow]面板不可用:{escape(str(exc))}[/yellow]")
        _print_resources(cwd)
        return
    chosen = run_resource_panel([
        (item.name, "扩展" if item.kind == "extension" else "包", item.scope, item.enabled)
        for item in resources])
    if chosen is None:
        console.print("[dim]已取消,没有改动。[/dim]")
        return
    notes = apply_resource_selection(resources, chosen, cwd)
    if not notes:
        console.print("[dim]没有改动。[/dim]")
        return
    for note in notes:
        console.print(f"[green]{escape(note)}[/green]")


def _print_resources(cwd: Path) -> None:
    """`qi config` 不带旗标:列出可启停的资源。

    “关”写在该作用域的声明里(目录扩展→`-<路径>` 否定项;pip 包→对象形态 `extensions: []`),
    所以**状态是被记住的** —— 这张表能区分“启用”与“主动关了”,而不是只能看到“存在”。
    """
    from .packages import list_resources

    resources = list_resources(cwd)
    if not resources:
        console.print("[dim]没有可启停的资源(没装扩展、settings.packages 也是空的)。[/dim]")
        return
    table = Table(title="资源")
    table.add_column("资源"); table.add_column("类型"); table.add_column("作用域")
    table.add_column("状态"); table.add_column("位置")
    for item in resources:
        table.add_row(item.name, "扩展" if item.kind == "extension" else "包", item.scope,
                      "[green]启用[/green]" if item.enabled else "[yellow]已关闭[/yellow]",
                      item.detail)
    console.print(table)
    console.print("[dim]关/开写进该作用域的声明(目录扩展 `-<路径>`;包对象形态"
                  "`extensions: []`),所以状态被记住。改:直接改那个 settings.json。[/dim]")


@app.command("config")
def config_cmd(
    local: bool = typer.Option(False, "--local", "-l",
                               help=f"操作项目 {'.qi'}/{SETTINGS_FILE_NAME}(默认全局 agent 目录)"),
    set_: list[str] = typer.Option(None, "--set", help="写键:--set skills='[\"~/x\"]'(可重复)"),
    get_: str | None = typer.Option(None, "--get", help="读一个键(点号路径,如 compaction.enabled)"),
    unset: list[str] = typer.Option(None, "--unset", help="删键(可重复)"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出合并后的设置"),
) -> None:
    """查看/编辑 settings.json:全局 `~/.qi/agent/` > 项目 `<git根>/.qi/`。"""
    scope = "project" if local else "user"
    cwd = Path.cwd()

    # `--get <键>`:读**该作用域自己**的那份(点号路径,与 --set 对称)
    if get_ is not None:
        from .settings import read_json

        path = settings_scope_path(scope, cwd)
        value: Any = read_json(path) if path.is_file() else {}
        for part in [p for p in get_.split(".") if p]:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                err_console.print(f"[yellow]{scope} settings 里没有 {escape(get_)}[/yellow]")
                raise typer.Exit(code=1)
        console.print(json.dumps(value, ensure_ascii=False, indent=2)
                      if isinstance(value, (dict, list)) else str(value))
        return

    if not (set_ or unset or json_out):
        # 不带旗标 = pi 的"资源面板"位置:TTY 下开面板,否则给一张表(脚本 / CI 也能用)。
        # 看得见“哪些资源被关了”本来就是面板的一半价值。
        if sys.stdin.isatty() and sys.stdout.isatty():
            _open_resource_panel(cwd)
            return
        _print_resources(cwd)
        console.print()      # 后面那段设置总览留着(不 return)
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
    for sub in ("agents", "extensions", "sessions"):
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
    preset: str | None = typer.Option(None, "--preset",
                                      help="用预置 provider(逗号分隔,如 deepseek,moonshot)"),
    list_presets: bool = typer.Option(False, "--list-presets", help="列出所有预置 provider 后退出"),
    refresh: str | None = typer.Option(None, "--refresh",
                                       help="从 provider 的 /models 接口拉模型列表写回(逗号分隔)"),
    refresh_all: bool = typer.Option(False, "--refresh-all",
                                     help="刷新所有已配置(有凭证)的 provider"),
) -> None:
    """引导默认模型:Provider Config → Add Models → Activate LLM,写 models.json + auth.json。

    `--preset` 是一条显式捷径:把预置的国产 provider(baseUrl/api/模型)直接写进
    `models.json`(已有的值不动),并把它的第一个模型设为默认。

    `--refresh` 走**另一条路**:预置表是离线种子,而模型 id 会漂(实例:DeepSeek 从
    `deepseek-v4-flash` 换成 `deepseek-flash`)—— 直接问厂商的 `/models` 接口最省事。
    """
    if list_presets:
        _print_presets()
        return
    if refresh or refresh_all:
        _refresh_models(refresh or "", all_providers=refresh_all, local=local)
        return
    if preset:
        _init_from_presets(preset, local=local, default_model=model)
        return
    if yes:
        _init_noninteractive(provider=provider, model=model, base_url=base_url, api=api,
                             api_key=api_key, api_key_env=api_key_env, reasoning=reasoning,
                             context_window=context_window, max_tokens=max_tokens, local=local)
    else:
        _init_interactive(local)


def _print_presets() -> None:
    """`qi init --list-presets`:把预置表打印成人能看的样子。"""
    from .presets import PRESETS

    for name in sorted(PRESETS):
        preset = PRESETS[name]
        console.print(f"[bold]{name}[/bold]  [dim]{preset.label}[/dim]")
        console.print(f"  baseUrl  {preset.base_url}")
        console.print(f"  apiKey   ${preset.api_key_env}(或 `qi auth login {preset.provider}`)")
        console.print("  模型     " + ", ".join(
            f"{m.id}(ctx {m.context_window:,} / max {m.max_output:,})"
            for m in preset.models))
        if preset.note:
            console.print(f"  [yellow]注意[/yellow]  {preset.note}")
    console.print("\n用法:[bold]qi init --preset <名字>[,<名字>][/bold]")
    console.print("[dim]预置是离线种子;要跟厂商接口对齐用 `qi init --refresh <名字>`"
                  "(只加不删)。[/dim]")


def _init_from_presets(preset: str, *, local: bool, default_model: str | None) -> None:
    """`qi init --preset …`:把预置写进 models.json(不覆盖已有值),再设默认模型。"""
    from .presets import apply_presets, get_preset

    names = [item.strip() for item in preset.split(",") if item.strip()]
    missing = [name for name in names if get_preset(name) is None]
    if missing:
        console.print(f"[red]未知预置: {', '.join(missing)}[/red]")
        console.print("[dim]可用预置:`qi init --list-presets`[/dim]")
        raise typer.Exit(code=2)
    # 解析一次(下面多处要用;`get_preset` 已保证这里都不是 None)
    chosen_list = [item for item in (get_preset(name) for name in names) if item is not None]

    target_dir = project_home() if local else global_home()
    target_file = target_dir / MODELS_FILE_NAME
    data = load_models_file(target_file)
    _data, changed = apply_presets(data, names)
    _strip_legacy_defaults(data)
    _write_models(target_dir, target_file, data)
    console.print(f"已写入 {len(changed)} 项:" + (", ".join(changed) if changed else "(都已存在,无改动)"))

    # 默认模型:显式 `--model` 优先,否则取**第一个预置**的第一个模型
    first = chosen_list[0] if chosen_list else None
    chosen_model = default_model or (first.default_model if first else None)
    if first is not None and chosen_model:
        _write_default_model(first.provider, chosen_model, local)

    providers = ", ".join(item.provider for item in chosen_list)
    console.print(f"[dim]下一步:给 {providers} 配 key —— 环境变量(见上表)或 "
                  f"`qi auth login <provider>`;然后 `qi doctor` 校验。[/dim]")


def _session_reason(*, no_session: bool, fork_id: str | None,
                    session_id: str | None, cont: bool) -> str:
    """`session_start` 的 reason(对齐 pi 的 startup / new / resume / fork)。

    `ephemeral` 是 qi 自己的:`--no-session` 不落盘,扩展据此可以跳过“往会话里存状态”。
    """
    if no_session:
        return "ephemeral"
    if fork_id:
        return "fork"
    if session_id or cont:
        return "resume"
    return "new"


def _trust_flag(approve: bool, no_approve: bool) -> bool | None:
    """`-a` / `-na` → 三态信任参数(None = 没表态,交给 settings 与默认策略)。

    两个都给是矛盾输入 → 报错退出,不猜(与 `-t`/`-xt` 的处理同形)。
    """
    if approve and no_approve:
        console.print("[red]-a 与 -na 不能同时给[/red]")
        raise typer.Exit(code=2)
    if approve:
        return True
    if no_approve:
        return False
    return None


def _launch_tui(initial_prompt: str | None = None, *, session_id: str | None = None,
                cont: bool = False, fork_id: str | None = None,
                no_session: bool = False, name: str | None = None,
                approve_project: bool | None = None,
                extension_flags: list[str] | None = None,
                extra_extension_paths: list[Path] | None = None,
                tools: str | None = None, exclude_tools: str | None = None,
                no_tools: bool = False, no_builtin_tools: bool = False,
                append_system_prompt: list[str] | None = None,
                resume: bool = False, exact_session_id: str | None = None,
                base_prompt_override: str | None = None,
                no_extensions: bool = False, no_context_files: bool = False,
                session_dir_path: str | None = None,
                model_override: str | None = None, api_key: str | None = None,
                scoped_models: list[str] | None = None,
                tui_mode: str | None = None) -> None:
    """启动 TUI(顶层 `qi` 的默认去向)。

    刻意不做成子命令:`pi` 也没有 `pi tui` —— 裸 `qi` 就是交互界面。
    `initial_prompt` 来自 `qi "问题"`:进界面后立刻提交这条消息;
    会话选择参数透传给 TUI(否则 `qi -c` 进界面后会失效)。
    """
    try:
        from .tui import run_tui
    except Exception as exc:  # textual 依赖问题
        console.print(f"[red]TUI 不可用: {escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    run_tui(initial_prompt, session_id=session_id, cont=cont, fork_id=fork_id,
            no_session=no_session, name=name, approve_project=approve_project,
            extension_flags=extension_flags,
            extra_extension_paths=extra_extension_paths,
            tools=tools, exclude_tools=exclude_tools,
            no_tools=no_tools, no_builtin_tools=no_builtin_tools,
            append_system_prompt=append_system_prompt,
            resume=resume, exact_session_id=exact_session_id,
            base_prompt_override=base_prompt_override,
            no_extensions=no_extensions, no_context_files=no_context_files,
            session_dir_path=session_dir_path, model_override=model_override,
            api_key=api_key, scoped_models=scoped_models, tui_mode=tui_mode)



def _core_subcommand_names() -> set[str]:
    """core 自己的子命令名(从 typer 的登记表拿;拿不到就退回空集,只是少了这层保护)。"""
    names: set[str] = set()
    for holder in ("registered_commands", "registered_groups"):
        for item in getattr(app, holder, []) or []:
            name = getattr(item, "name", None)
            if name:
                names.add(str(name))
    return names


def _discover_cli_commands(cwd: Path | None = None) -> CliCommandRegistry:
    """只装载扩展、只要它们的 CLI 子命令表(轻量发现:没有 runtime / 会话 / 模型)。

    **未信任的项目目录不扫**(§5.3):CLI 子命令会在 typer 之前执行扩展的代码,而项目目录是
    仓库控制的。

    **登记处必须给齐**(与 `_extension_report` 那份一样):装载器的策略是"扩展坏 → 启动报错,
    不静默"(registry.py 里那句 `扩展 X 装载失败`),所以少给一个登记处不是"那个扩展少注册
    一样东西",而是**整个发现过程中断**。曾因缺 `commands`/`flags` 使 qi-mcp(`api.registerCommand`)
    与 qi-agents(`api.registerFlag`)当场倒下,连带 qi-web 的 `qi web` 从未注册。
    这里只消费 `cli_commands`,其余两个只是让兄弟们能正常走完 `register()`。
    """
    registry = CliCommandRegistry()
    # 登记处从 `.extensions` 取(与 `_extension_report` 同源);局部导入避免 CLI 模块顶部就拖上宿主
    from .extensions import CommandRegistry, FlagRegistry

    try:
        discover_extensions(ToolCatalog(), CapabilityRegistry(), cwd,
                            bus=ExtensionBus(),
                            commands=CommandRegistry(), flags=FlagRegistry(),
                            cli_commands=registry,
                            project_trusted=False)
    except Exception as exc:      # noqa: BLE001 一个扩展装坏不该让整个 CLI 不可用
        err_console.print(f"[yellow]扩展发现失败(忽略): {escape(str(exc))}[/yellow]")
    return registry


#: 官方扩展提供的 CLI 子命令 → 提供它的包。
#: 与 `docs/extensions.md` §8.1 那张表同源:core 不内置它们,所以这份名单必须写在 core 里 ——
#: 否则"没装那个扩展的人"永远只能看到 `No such command`,而不知道要装什么。
_OFFICIAL_EXTENSION_COMMANDS = {"web": "qi-web"}


def _reject_unimplemented_flags(*, prompt_template, no_prompt_templates, theme_file,
                                use_theme, no_themes, mode) -> None:
    """接受 pi 有、qi **还没有对应功能**的旗标,但明确报出来(退出码 2)。

    为什么接受:pi 的命令行迁过来时,"未知选项"看不出问题在哪 —— 用户会以为是拼写错。
    为什么报错而不是静默忽略:这几条都**改行为**,忽略了就等于"我说的没生效"而没任何提示。
    每一行写清缺的是**什么功能**、以及现在能用什么替代。
    """
    missing: list[str] = []
    if prompt_template or no_prompt_templates:
        missing.append("prompt 模板 -- qi 没有模板发现 / 注入这套机制; "
                       "要固定前缀就写进 `.qi/SYSTEM.md` 或做成技能")
    if theme_file or use_theme or no_themes:
        missing.append("自定义主题文件 -- qi 只有内置 dark / light / auto; "
                       "选主题用 `QI_THEME=light` 或 `qi config --set theme=`")
    if str(mode or "").strip().lower() == "rpc":
        missing.append("`--mode rpc` -- qi 的输出模式只有 text | json; "
                       "stdio JSON-RPC 还没实现(见 docs/cli.md §9)")
    if not missing:
        return
    for item in missing:
        err_console.print(f"[red]还没实现:{escape(item)}[/red]")
    err_console.print("[dim]qi 接受这条旗标是为了让 pi 的命令行能迁过来,但不会假装它生效。[/dim]")
    raise typer.Exit(code=2)


def _model_spec(provider: str | None, model: str | None) -> str | None:
    """`--provider` / `--model` 合成一个 `provider/模型`(pi 允许两者分开写)。

    只给 `--provider` 时用它 models.json 里的**第一个**模型(pi 用该 provider 的默认模型)。
    拼不出来的情况**报错**,不猜。
    """
    if not provider and not model:
        return None
    if model and "/" in model:
        return model
    if not provider:
        raise ValueError('只给 `--model` 时要写成 "provider/模型"')
    if model:
        return f"{provider}/{model}"
    from .config import load_config

    cfg, _files = load_config()
    entry = cfg.providers.get(provider)
    if entry is None or not entry.models:
        raise ValueError(f"models.json 里没有 provider {provider!r}(或它没声明模型)")
    return f"{provider}/{entry.models[0].id}"


def _text_or_file(value: str) -> str:
    """旗标值:是**可读文件**就取文件内容,否则就当文本(pi 的 "text or file contents")。

    为什么这样判:pi 就这么做,而这两种输入在命令行上没法区分 —— 用户写
    `--append-system-prompt ./house-rules.md` 时期待的是文件内容。读不到就当字面文本。
    """
    if not value:
        return value
    maybe = Path(value).expanduser()
    try:
        if maybe.is_file():
            return maybe.read_text(encoding="utf-8")
    except OSError:
        return value
    return value


def _open_session(store: Any, ref: str) -> Any:
    """`--session <path|id>` / `--fork <path|id>`:先当**文件路径**,再当 id / 前缀。

    以前只走 `store.get()`,而它按 header id / 文件名 stem 前缀匹配 —— 传路径永远不命中,
    与帮助文字里写的 `path|id` 不符。
    """
    maybe = Path(ref).expanduser()
    if maybe.is_file():
        opened = store.open_file(maybe)
        if opened is not None:
            return opened
    return store.get(ref)


def _hint_missing_extension_command(argv: list[str]) -> None:
    """`qi <官方扩展子命令>` 但那个扩展没装 → 报装法(而不是 `No such command`)。

    代价故意压到零:只有 `argv[0]` 命中那份小名单时才去发现扩展命令表(那步要装载)。
    """
    if not argv or argv[0].startswith("-") or argv[0] in _core_subcommand_names():
        return
    package = _OFFICIAL_EXTENSION_COMMANDS.get(argv[0])
    if package is None or _discover_cli_commands().find(argv[0]) is not None:
        return
    err_console.print(f"[red]`qi {argv[0]}` 需要 **{package}** 扩展(这条子命令由它提供,"
                      f"core 不内置)。[/red]")
    for hint in install_hints(f"pip:{package}"):
        err_console.print(f"  [dim]{escape(hint)}[/dim]")
    err_console.print("[dim]装完用 `qi doctor` 确认宿主真的收到了它。[/dim]")
    raise typer.Exit(code=2)


def _dispatch_extension_command(argv: list[str]) -> bool:
    """`qi <扩展子命令> …` → 交给扩展。返回 True 表示已处理。

    **必须在 `app()` 之前**:typer 的子命令表是静态的,动态加会踩坑(E19)。
    **core 自己的名字优先** —— 扩展撞了 core 的子命令名要被看见,而不是悄悄接管。
    """
    if not argv or argv[0].startswith("-") or argv[0] in _core_subcommand_names():
        return False
    command = _discover_cli_commands().find(argv[0])
    if command is None:
        return False
    raise SystemExit(command.handler(argv[1:]) or 0)



def _extension_report(cwd: Path | None = None) -> tuple[list[str], list[str]]:
    """`qi doctor` 的扩展一节:`(每个扩展一行, 依赖警告)`。

    信息来自**注册面的反查**,不是扩展自报 —— 这样"扩展说它注册了 X"与"宿主真收到了 X"
    不会各说各话(doctor 的价值正在此)。所以这里宁可做一次轻量装载,也不读扩展自己的日志。
    """
    from .extensions import CliCommandRegistry, CommandRegistry, ExtensionBus, FlagRegistry
    from .registry import CapabilityRegistry, ToolCatalog, discover_extensions

    catalog, caps = ToolCatalog(), CapabilityRegistry()
    commands, flags, cli_commands = CommandRegistry(), FlagRegistry(), CliCommandRegistry()
    warnings: list[str] = []
    loaded = discover_extensions(catalog, caps, cwd or Path.cwd(), bus=ExtensionBus(),
                                 commands=commands, flags=flags, cli_commands=cli_commands,
                                 on_warning=warnings.append, project_trusted=False)

    tools: dict[str, int] = {}
    origins: dict[str, str] = {}
    for tool in catalog.all():
        info = tool.source_info or {}
        name = str(info.get("source") or "?")
        tools[name] = tools.get(name, 0) + 1
        if name not in origins:
            origin = str(info.get("origin") or "")
            scope = str(info.get("scope") or "")
            origins[name] = f"{origin or '?'}" + (f" · {scope}" if scope else "")

    owned: dict[str, list[str]] = {name: [] for name in loaded}
    for name in commands.names:
        command = commands.find(name)
        if command is not None:
            owned.setdefault(command.source, []).append(f"/{name}")
    for name in cli_commands.names:
        command = cli_commands.find(name)
        if command is not None:
            owned.setdefault(command.source, []).append(f"qi {name}")
    for name in loaded:                       # 配置种类的归属(反查)
        kinds = sorted(k for k in caps.kinds if name in caps.providers(k))
        if kinds:
            owned[name].append("配置种类 " + ", ".join(kinds))
        if tools.get(name):
            owned[name].append(f"工具 {tools[name]}")

    lines: list[str] = []
    for name in loaded:
        what = owned.get(name) or ["(装载了但没有注册任何东西)"]
        lines.append(f"{name}  [dim]{origins.get(name, '?')}[/dim]  " + " · ".join(what))
    if flags.names:
        # 旗标登记处没有归属面,所以单独列(不硬凑成"某扩展的旗标")
        lines.append("旗标: " + ", ".join(flags.names))
    return lines, warnings


#: pi 的多字符短旗标 → qi 的长旗标。click 的短选项是**按字符**解析的(它只在 `_short_opt`
#: 里查 `-n`、`-x` 这类单字符键),所以 `-nt` 会被拆成 `-n t` —— 会话名悄悄变成 `"t"`。
#: 多字符短选项因此必须在 typer 看到 argv **之前**展开。pi 的 CLI 是手写 argv 循环
#: (`cli/args.ts`),没有这个问题;qi 的这套展开就是那个循环的最小替代(见 E19)。
_SHORT_FLAG_ALIASES = {"-nt": "--no-tools", "-nbt": "--no-builtin-tools",
                       "-t": "--tools", "-xt": "--exclude-tools",
                       "-ne": "--no-extensions", "-nc": "--no-context-files",
                       "-np": "--no-prompt-templates"}
#: 会**吃掉下一个 token** 的选项:预处理时不能把它们的值当旗标改写
#:(`-n -nt` 里的 `-nt` 是会话名,不是旗标)。
_VALUE_FLAGS = {"--session", "--fork", "--name", "-n", "--export", "--mode", "--skill",
                "--ext", "-e", "--extension", "--thinking", "--tools", "--exclude-tools"}


def normalize_short_flags(argv: list[str]) -> list[str]:
    """把 pi 的短旗标展开成长旗标;`--` 之后一律不动(那是消息原文)。"""
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            out.extend(argv[i:])
            break
        if token in ("-t", "-xt"):
            out.append(_SHORT_FLAG_ALIASES[token])
            if i + 1 < len(argv):        # 缺值就交给 click 报错(不在这里猜)
                i += 1
                out.append(argv[i])
        elif token in _SHORT_FLAG_ALIASES:
            out.append(_SHORT_FLAG_ALIASES[token])
        elif token in _VALUE_FLAGS:
            out.append(token)
            if "=" not in token and i + 1 < len(argv):
                i += 1
                out.append(argv[i])
        else:
            out.append(token)
        i += 1
    return out


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
    # pi 的短旗标先展开再交给 typer(见 `normalize_short_flags`)
    sys.argv = [sys.argv[0], *normalize_short_flags(list(sys.argv[1:]))]
    # 「装了但没装那个扩展」的官方子命令:先给装法,别让 typer 报 No such command
    _hint_missing_extension_command(list(sys.argv[1:]))
    # 扩展子命令要在 typer 之前认出来(它的子命令表是静态的)
    if _dispatch_extension_command(list(sys.argv[1:])):
        return
    app()
