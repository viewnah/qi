"""TUI(P7,textual 基础版):消息流 + 输入 + 内部命令。

命令:/help /quit /new /agents /mode <auto|manual> /agent <name> /tools /skills
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog

from .cli import _load_registry, _print_agents_table
from .config import ConfigError
from .loader import LoadError
from .registry import ToolCatalog
from .runtime import QiRuntime, RuntimeConfig
from .session import SessionStore
from .tools import register_builtin_tools

HELP_TEXT = """\
可用命令:
  /help            本帮助
  /quit            退出
  /new             新会话
  /resume          选历史会话恢复
  /sessions        列出会话(用 /resume <id> 恢复)
  /resume <id>     恢复指定会话
  /agents          列出 agent
  /mode auto|manual  切换分派模式
  /agent <name>    manual 模式锁定执行 agent
  /tools           当前/全部 agent 工具清单(简版)
  @name 开头       直接点名 agent
其余输入按 auto/manual 规则执行。
"""


class QiTui(App):
    TITLE = "qi"
    SUB_TITLE = "多 agent · auto 分派"

    BINDINGS = [("ctrl+c", "quit", "退出"), ("ctrl+l", "clear_log", "清屏")]

    def __init__(self, runtime: QiRuntime | None = None):
        super().__init__()
        self._rt = runtime
        self._session = None
        self._agent: str | None = None      # manual 锁定
        self._auto = True
        self._shown_name = "?"              # 当前 agent 的展示名(display_name 优先)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RichLog(id="log", markup=True, highlight=True, wrap=True)
        yield Input(placeholder="输入问题…(@agent 点名; /help 看命令)", id="input")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#log", RichLog)
        try:
            if self._rt is None:
                self._rt = QiRuntime()
            store = SessionStore()
            self._session = store.create("tui") or store.latest()
            if self._session is None:
                self._session = store.create("tui")
            log.write(f"[dim]qi {self._rt.registry.names or '(无 agent;请把样例拷进 ~/.qi/agents 或项目 .qi/agents)'}[/dim]")
            log.write(HELP_TEXT)
        except (LoadError, ConfigError) as exc:
            log.write(f"[red]启动失败: {exc}[/red]")
            self._rt = None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#input", Input).value = ""
        if not text:
            return
        log = self.query_one("#log", RichLog)
        log.write(f"[bold cyan]你:[/bold cyan] {text}")
        if text.startswith("/"):
            self._command(text, log)
            return
        if self._rt is None:
            log.write("[red]运行时不可用。[/red]")
            return
        if self._session is None:
            self._session = SessionStore().create("tui")
        override = None if self._auto else self._agent
        self.run_worker(self._run(text, override), exclusive=False)

    async def _run(self, text: str, override: str | None) -> None:
        log = self.query_one("#log", RichLog)
        assert self._rt is not None and self._session is not None
        async for ev in self._rt.stream(text, self._session, agent_override=override):
            if ev.kind == "dispatch":
                # 事件 text 已含展示名与来源,如 "qi (router, 0.90)"
                self._shown_name = str(ev.data.get("display_name") or ev.agent or "?")
                log.write(f"[cyan]→ {ev.text} {ev.data.get('reasoning', '')}[/cyan]")
            elif ev.kind == "tool_start":
                log.write(f"[dim]⚙ {ev.tool}[/dim]")
            elif ev.kind == "tool_end":
                log.write(f"[dim]↳ {ev.text[:160]}[/dim]")
            elif ev.kind == "text" and ev.text:
                log.write(f"[bold green]{self._shown_name}:[/bold green] {ev.text}")
            elif ev.kind == "opening":
                log.write(f"[bold]{ev.text}[/bold]")
            elif ev.kind == "error":
                log.write(f"[red]{ev.text}[/red]")

    def _command(self, text: str, log: RichLog) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        assert self._rt is not None
        if cmd == "/help":
            log.write(HELP_TEXT)
        elif cmd == "/quit":
            self.exit()
        elif cmd == "/clear":
            log.clear()
        elif cmd == "/new":
            self._session = SessionStore().create("tui")
            self._agent = None
            self._auto = True
            log.write("[dim]已开新会话(auto)[/dim]")
        elif cmd == "/resume":
            if arg:
                s = SessionStore().get(arg)
                if s:
                    self._session = s
                    log.write(f"[dim]已恢复 {s.id}[/dim]")
                else:
                    log.write(f"[red]会话不存在 {arg}[/red]")
            else:
                for s in SessionStore().list()[:10]:
                    log.write(f"  {s.id}  {s.title}  {s.created_at}")
        elif cmd == "/agents":
            catalog = ToolCatalog()
            register_builtin_tools(catalog)
            reg = _load_registry(catalog)
            for u in reg.all():
                shown = (u.config.display_name or "").strip() or u.name
                log.write(f"[bold]{shown}[/bold]({u.name} · {u.source}) tools={','.join(u.tools) or '全部'}")
                log.write(f"   {u.config.description.splitlines()[0]}")
        elif cmd == "/mode":
            mode = arg.strip().lower()
            if mode in ("auto", "manual"):
                self._auto = mode == "auto"
                log.write(f"[dim]模式: {mode}[/dim]")
            else:
                log.write("用法: /mode auto|manual")
        elif cmd == "/agent":
            if arg and self._rt.registry.get(arg):
                self._agent = arg
                self._auto = False
                log.write(f"[dim]锁定 agent: {arg}(manual)[/dim]")
            else:
                log.write(f"[red]未知 agent: {arg};可用 /agents 查看[/red]")
        elif cmd == "/tools":
            log.write(f"[dim]内置工具: read ls find grep write edit bash clarify[/dim]")
        else:
            log.write(f"[yellow]未知命令 {cmd};/help 查看[/yellow]")

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    # ctrl+c(见 BINDINGS)映射到 "quit",由 Textual 内置的
    # `App.action_quit`(async,内部即 self.exit())处理 —— 不重复实现。


def run_tui() -> None:
    QiTui().run()
