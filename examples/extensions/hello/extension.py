"""最小扩展示例:一份 `extension.py`,把主要的面各用几行走一遍。

**这就是"照着写"的起点** —— 复制这个目录、改名字、删掉你不要的部分即可。完整的 API 与
设计取舍见 `design/extensions-design.md`;这里只演示形状,不解释为什么。

放哪儿(两处之一):

    <项目>/.qi/extensions/hello/extension.py     项目级(跟仓库走;未信任时不装载)
    ~/.qi/agent/extensions/hello/extension.py    用户级(跨项目)

**入口文件名固定是 `extension.py`**(一个目录=一个扩展),里面必须有 `register(api)`。
装了之后 `qi` 下次启动就会装载它 —— 不需要注册到任何清单里。
"""

from __future__ import annotations

from typing import Any

from qi_agent.extensions import Tool

# 这份示例覆盖的面:
#   1. 注册一个工具(`registerTool`)—— 模型能调用它
#   2. 注册一个斜杠命令(`registerCommand`)—— TUI 里 `/hello`
#   3. 注册一个 CLI 子命令(`registerCliCommand`)—— 终端里 `qi hello-cli`
#   4. 声明一个旗标(`registerFlag`)—— `qi --ext greet=张三 "…"`
#   5. 订阅一个事件(`on("session_start")`)—— 会话开始时说一句话
# 没覆盖的(按需要再看文档):系统提示词注入(`before_agent_start`)、消息注入
# (`sendMessage`)、进程内子运行(`runAgent`)、能力交接(`registerResolver`)。


def register(api: Any) -> None:
    """装载入口。宿主只认这个名字。"""

    # ── 1. 工具 ────────────────────────────────────────────
    # 执行器是 **async**,签名 `(args, ctx) -> str | ToolOutcome`;
    # `args` 是模型给的 JSON(已经过 schema 校验),`ctx` 见下面四处用到的东西。
    async def greet(args: dict, ctx: Any) -> str:
        who = str(args.get("who") or "").strip() or (api.getFlag("greet") or "世界")
        return f"你好,{who}!(工作目录:{ctx.workdir})"

    api.registerTool(Tool(
        name="greet",
        description="跟某人打个招呼。示例用,没有副作用。",
        parameters={                      # 标准 JSON Schema
            "type": "object",
            "properties": {"who": {"type": "string", "description": "称呼;留空用旗标给的默认"}},
        },
        execute=greet,
        # prompt_snippet 进系统提示词的「可用工具」那一行(省略则回落到 description)
        prompt_snippet="打招呼(示例工具)",
    ))

    # ── 2. 斜杠命令(界面里输入)────────────────────────────
    def on_hello(args: str, ctx: Any) -> None:
        """`args` 是命令名之后的原文(不解析);`ctx.ui.notify` 无界面时自动落 notes。"""
        ctx.ui.notify(f"hello 扩展:收到 {args!r};当前会话目录 {ctx.cwd}")

    api.registerCommand("hello", on_hello, description="示例命令:回显你输入的东西")

    # ── 3. CLI 子命令(终端里输入)──────────────────────────
    # handler 收**命令名之后的原始 argv**,返回退出码。扩展自己解析自己的参数 ——
    # core 不把 typer/click 的解析结果交给你(见 design/extensions-design.md 的 E19)。
    def on_cli(argv: list[str]) -> int:
        import sys

        print(f"hello 扩展:argv={argv!r}", file=sys.stdout)
        return 0

    api.registerCliCommand("hello-cli", on_cli, description="示例子命令:回显参数")

    # ── 4. 旗标 ────────────────────────────────────────────
    # 用户在命令行给值:`qi --ext greet=张三 "打个招呼"`。
    api.registerFlag("greet", type="string", default="",
                     description="greet 工具的默认称呼(也演示 getFlag)")

    # ── 5. 事件 ────────────────────────────────────────────
    # 同一个事件可以有多个订阅者,按注册顺序跑;返回值在链式事件里会被后一个看到
    # (如 `before_agent_start` 改系统提示词)。这里只是说一句话。
    def on_session_start(payload: dict, ctx: Any) -> None:
        ctx.ui.notify(f"hello 扩展已装载(session_start: {payload.get('reason')})——"
                      f"试试 /hello,或 `qi hello-cli 参数`")

    api.on("session_start", on_session_start)
