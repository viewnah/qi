"""`subagent` 工具的呈现层 —— 文本格式契约 + TUI 自绘回调。

两面共用**一个**格式契约(本模块是唯一事实源):

* `subagent._render` 生成给**模型 / 导出 / 回放**看的纯文本:横幅分段
  (`── 名字 (深度N, 状态) ──`),每段 = 一个 agent 的完整过程(活动 / 思考 / 输出);
* 本模块的 `result_tree` 把同一段文本解析回来,在 TUI 卡片里上色、按段缩进重画。

**为什么不画树地图**:委派关系在运行时间线里已经可见(coordinator 的
`· subagent …` 派单行 + 新的 `── 名字 ──` 标题块),横幅又自带深度与状态 ——
地图是与它们重复的第二份信息。结构概览由**收起态**(各段横幅竖排)承担。

**为什么以文本为唯一事实源**:qi 给 `render_result` 的入参只有 result 字符串
(tui.py `_tool_result_widget(name, ev.text)`),拿不到执行期的内存状态 —— 于是把
格式做成可解析的,回放旧会话、导出、headless 与现场看到的是同一份;解析不出来
(用法提示、预算拒绝这类无横幅的自由文本)就**退回内置渲染**,绝不丢正文。

TUI 钩子(只在 TUI 里被调用;headless 不涉及):

* `render_call`    —— 调用开始时返回**平铺容器**(`_RunStack`):头部是调用摘要,
  之后每个 agent 启动时往里 `add_agent` 追加**自己的一张卡**(嵌套子层追加到同一
  个架子 —— 全树拍平)。卡标题 = 名字 ← 由谁指派 · 深度;正文区固定高内滚,
  每次刷新 `scroll_end` **自动跟到最后一行**,新卡挂载时 `scroll_visible` 把外层
  对话流也带到底部。**必须与 render_result 一起挂**(见 tui.py `_tool_block` 的配对)。
* `render_result`  —— 返回 `None`:各卡已在 run 结束时各自 `set_done` 定格,
  嵌套树卡退役;前端会把内置 `tool_body` 转发给容器,容器把它当 **ctrl+o 的全局
  开合**,不覆盖平铺内容。

返回 `None` 都表示"我不画" → 前端退回内置渲染(textual 缺席 / 构造失败时的降级
路径)。
"""

from __future__ import annotations

import re
from typing import Any

from rich.text import Text

# core 自带 textual,这里不再做「缺席时降级」的分支:把变量声明成 Optional 会让
# 每个组件类都变成不可继承的 `None | type`。真缺 textual 时 core 自己就起不来。
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

#: 展开态每段正文最多画的行数(完整文本在 result 里,模型与导出不截这个)。
BODY_LINES = 30

#: `render_call` 创建的**平铺容器队列**(每 agent 一卡的架子)。与 runner 的工具
#: 生命周期严格同序 —— `tool_start` 全部先发(同一批)、执行再按调用序启动,所以
#: `run_subagent` 在工具入口出队即与本次调用一一配对;headless/无前端不建组件 →
#: 队列空 → 出队得 None(该 run 的事件走时间线兜底)。
PENDING_CARDS: list[Any] = []

#: 分段横幅 —— `_render` 生成、本模块解析,格式变更只发生在 `banner()` 一处。
BANNER_RE = re.compile(
    r"^──\s*(?P<name>.+?)\s*\(深度(?P<depth>\d+),\s*(?P<state>成功|失败)\)\s*──$")


def banner(agent: str, depth: int, ok: bool) -> str:
    """一条分段横幅:归属(谁)+ 定位(深度)+ 结果(状态)。

    归属信息**不靠空格缩进承载** —— 纯文本场景(拷贝、导出、非 markdown 渲染)
    会吃掉或模糊缩进,横幅自己就说清这一段是谁的输出。
    """
    return f"── {agent} (深度{depth}, {'成功' if ok else '失败'}) ──"


def _tasks_of(args: dict) -> list[dict]:
    """三种模式归一出任务行(与执行侧 `_tasks_of` 同形,但这里是纯展示、不值得耦合)。"""
    if isinstance(args.get("tasks"), list):
        return [t for t in args["tasks"] if isinstance(t, dict)]
    if isinstance(args.get("chain"), list):
        return [t for t in args["chain"] if isinstance(t, dict)]
    if args.get("agent") and args.get("task"):
        return [{"agent": args["agent"], "task": args["task"]}]
    return []


def call_summary(args: dict) -> Text:
    """`render_call` 的正文:调用开始(pending)时"这一趟要派什么"。

    此时还没有结果 —— 不画深度(深度执行时才知道),只把模式与目标角色讲清楚,
    让用户在卡片还活着的时候就知道在等谁。
    """
    out = Text()
    rows = _tasks_of(args)
    if not rows:
        out.append("委派(subagent)", style="bold cyan")
        return out
    names = [str(r.get("agent") or "?") for r in rows]
    if isinstance(args.get("tasks"), list):
        out.append(f"并行委派 {len(rows)} 个任务", style="bold cyan")
        out.append(f"\n→ {', '.join(names)}", style="dim")
    elif isinstance(args.get("chain"), list):
        out.append(f"链式委派 {len(rows)} 棒", style="bold cyan")
        out.append(f"\n→ {' → '.join(names)}", style="dim")
    else:
        task = str(rows[0].get("task") or "").replace("\n", " ")
        if len(task) > 120:
            task = task[:120] + "…"
        out.append(f"委派 → {names[0]}", style="bold cyan")
        out.append(f"\n{task}", style="dim")
    return out


# -- 段解析 ------------------------------------------------------------------

def _parse(result: str) -> list[tuple[re.Match[str], list[str]]]:
    """按横幅切段;无横幅返回 [](调用方按自由文本处理)。"""
    segs: list[tuple[re.Match[str], list[str]]] = []
    for line in str(result or "").split("\n"):
        m = BANNER_RE.match(line)
        if m:
            segs.append((m, []))
        elif segs:
            segs[-1][1].append(line)
    return segs


def _segment_text(m: re.Match[str], body: list[str]) -> Text:
    """一段 = 横幅(上色)+ 正文行(`·` 活动 dim、`✎` 思考 dim 斜体、其余正文)。"""
    out = Text()
    ok = m["state"] == "成功"
    out.append(banner(m["name"], int(m["depth"]), ok),
               style="bold cyan" if ok else "bold red")
    out.append("\n")
    body = list(body)
    # 段间 join 出来的空行会落进上一段 body 的尾部 —— 去掉再画,不留"只有缩进"的行。
    while body and not body[-1].strip():
        body.pop()
    for line in body[:BODY_LINES]:
        style = ("dim" if line.startswith("· ")
                 else "dim italic" if line.startswith("✎")
                 else None)
        out.append(f"  {line}\n", style=style)
    rest = len(body) - BODY_LINES
    if rest > 0:
        out.append(f"  …(+{rest} 行,完整内容在 result 文本里)", style="dim")
    return out


def result_tree(result: str) -> Text:
    """展开态全文(也用于测试与 headless 检查):各段横幅 + 缩进正文。

    没有横幅的自由文本(用法提示 / 预算拒绝)不切段,原样透传。
    """
    segs = _parse(result)
    if not segs:
        out = Text()
        for i, line in enumerate(str(result or "").split("\n")):
            if i:
                out.append("\n")
            out.append(line, style="dim" if line.startswith("委派树") else None)
        return out
    out = Text()
    for i, (m, body) in enumerate(segs):
        if i:
            out.append("\n")
        out.append_text(_segment_text(m, body))
    return out


def _overview(segs: list[tuple[re.Match[str], list[str]]]) -> Text:
    """收起态:各段横幅竖排 —— 结构概览(谁、几层、成没成),不带正文。"""
    out = Text()
    for i, (m, _body) in enumerate(segs):
        if i:
            out.append("\n")
        ok = m["state"] == "成功"
        out.append(banner(m["name"], int(m["depth"]), ok),
                   style="bold cyan" if ok else "bold red")
    return out


# -- 卡片组件:每 agent 一卡,平铺 ------------------------------------------------

class _AgentCard(Vertical):
    """单个 agent 的卡 —— 标题 = `▸ 名字 ← 由谁指派 · 深度N`,正文 = 它自己的
    活动/思考流式。

    * **实时跟底**(`_live=True`,没人碰过):运行中正文区固定 12 行内滚,每次
      刷新 `scroll_end(animate=False)` —— 进度永远停在最后一行,不用手动滚;
    * **首次点击 / `set_done` / 全局开合 → 退出实时**(`_live=False` = 浏览态):
      高度改 `auto` **完全撑开、无内部滚动条**,点开即整段看全(修“展开内容
      太少”);浏览态不再强制拉到底(你已接管,自己滚);
    * **运行中**默认展开(流式就是给人看的),点击收起省渲染;**完成**后
      `set_done` 定格并默认收起正文(平铺多卡要短),点击展开看该 agent 全文
      (活动 / 各轮思考 / 输出)。
    """

    def __init__(self, name: str, parent: str, depth: int) -> None:
        super().__init__()
        # 注意别叫 `_name`/`_parent` —— Widget/MessagePump 的内部槽位
        # (`_parent` 是 weakref property,赋 str 会炸)。
        self._agent, self._origin, self._lvl = str(name), str(parent), int(depth)
        self._expanded = True
        self._live = True  # 实时跟底态:首次点击/完成/全局开合后转浏览态
        self._acts: list[str] = []
        self._stream = ""
        self._mode = "think"
        self._done = False
        self._ok = True
        self._output = ""
        self._thoughts: list[str] = []
        self._head = Static(self._head_text())
        self._body = Static(self._body_text())
        self._scroll = VerticalScroll(self._body)
        # 关键:Textual 的 `Vertical` 默认 `height:1fr` + `overflow:hidden hidden`
        # —— 会把卡片裁到“父分配的 1fr 高度”,内部 _scroll 的 12 行/auto 全文
        # 都被裁掉(实测只剩 ~6 行、展开看不全、跟底错乱)。core 的 `.msg{auto}`
        # 只盖到最外层,这里必须自己显式 auto → 卡高 = 内容,不裁。
        self.styles.height = "auto"
        self.styles.border = ("round", "grey")
        self.styles.padding = (0, 1)
        self.styles.margin = (0, 0, 1, 0)
        self._scroll.styles.height = 12

    def compose(self):
        # 同步 generator 钩子(widget.compose,而非 async 的 _compose —— 覆写
        # 后者会被 `await self._compose()` 打成 generator-await TypeError)。
        yield self._head
        yield self._scroll

    # -- 绘制 --

    def _head_text(self) -> Text:
        out = Text()
        if self._done and self._ok:
            out.append(f"▸ {self._agent}", style="bold green")
        elif self._done:
            out.append(f"▸ {self._agent}", style="bold red")
        else:
            out.append(f"▸ {self._agent}", style="bold cyan")
        out.append(f" ← {self._origin}", style="dim")
        out.append(f" · 深度{self._lvl}", style="dim")
        if self._done:
            out.append(" ✓" if self._ok else " ✗", style="green" if self._ok else "red")
        else:
            out.append(" · 运行中", style="cyan")
        return out

    def _summary_lines(self) -> list[str]:
        """完成态收起时的摘要:输出前 3 行(无输出用最后一轮思考顶)。"""
        for src in (self._output.splitlines(), list(reversed(self._thoughts))):
            lines = [ln for ln in src if ln and ln.strip()]
            if lines:
                return lines[:3]
        return ["(无文本输出)"]

    def _body_text(self) -> Text:
        out = Text()
        if not self._expanded:
            # 收起态 = 摘要行(输出前 3 行 / 最后一轮思考)+ 展开提示
            for ln in self._summary_lines():
                out.append(f"  {ln}\n", style="dim")
            out.append("  …(点击展开)", style="dim")
            return out
        if not self._done:
            for act in self._acts:
                out.append(f"  · {act}\n", style="dim")
            if self._stream:
                head = "  ✎ 思考…" if self._mode == "think" else "  ▍ 输出…"
                out.append(f"{head}\n", style="dim italic")
                # 画当前轮流式(上限防尾大不掉) —— 正文可超高,由内滚区 +
                # scroll_end 自动跟底接管
                for ln in self._stream.splitlines()[-80:]:
                    out.append(f"  {ln}\n")
            elif not self._acts:
                out.append("  (等待首个事件…)", style="dim")
            return out
        # 完成态(展开 = 该 agent 的全文:活动 → 各轮思考 → 输出)
        if self._acts:
            for act in self._acts:
                out.append(f"  · {act}\n", style="dim")
        for i, t in enumerate(self._thoughts):
            out.append(f"  ✎ 第{i + 1}轮\n", style="dim italic")
            for ln in str(t).splitlines() or [""]:
                out.append(f"    {ln}\n", style="italic")
        if self._output:
            out.append("  ▍ 输出\n", style="bold dim")
            for ln in self._output.splitlines():
                out.append(f"  {ln}\n")
        elif not self._acts and not self._thoughts:
            out.append("  (无文本输出)", style="dim")
        return out

    def _paint(self) -> None:
        # head / body **各自独立** try:headless 下 `update` 的 visualize 会因无
        # active_app 抛 NoActiveAppError,但 content 赋值在其之前 —— 分开 try
        # 才能保证两边都落地(headless 单测读的就是它),渲染态由 app 内的下次
        # update 重建。
        for widget, factory in ((self._head, self._head_text),
                                (self._body, self._body_text)):
            try:
                widget.update(factory())
            except Exception:  # pragma: no cover - 无 app 时 update 会拒
                pass
        try:
            # `_live`(实时跟底)→ 固定 12 行内滚 + 拉到底;浏览/完成态 → auto,
            # 整段撑开看全、无内部滚动条(修“展开内容太少 / 看不清”)
            self._scroll.styles.height = 12 if self._live else "auto"
        except Exception:  # pragma: no cover - 样式赋值不应带倒刷新
            pass
        if self._live and self._expanded:
            try:
                self._scroll.scroll_end(animate=False)  # 自动定位到最后一行
            except Exception:  # pragma: no cover - 未挂载时无效
                pass

    # -- 前端事件 --

    def on_click(self, event: Any = None) -> None:
        # 首次点击即退出实时跟底 → 浏览态(高度 auto,完全撑开看全)
        self._live = False
        self._expanded = not self._expanded
        self._paint()

    def set_stream(self, text: str, mode: str = "think") -> None:
        """节流后的流式刷新(完成后忽略 —— 已定格)。"""
        if self._done:
            return
        self._stream, self._mode = str(text), mode
        self._paint()

    def add_activity(self, line: str) -> None:
        if self._done:
            return
        self._acts.append(str(line))
        self._paint()

    def set_done(self, ok: bool, output: str, thoughts: list[str]) -> None:
        """run 收束定格:标题转 ✓/✗,正文收起为摘要(点击展开全文)。"""
        self._done, self._ok = True, bool(ok)
        self._output = str(output or "")
        self._thoughts = [str(t) for t in (thoughts or []) if t]
        self._live, self._expanded = False, False
        self._paint()

    def set_expanded(self, expanded: bool) -> None:
        # 全局开合(ctrl+o)也算接管浏览 → 退出实时跟底,高度走 auto
        self._live = False
        self._expanded = bool(expanded)
        self._paint()


class _RunStack(Vertical):
    """平铺架子:`render_call` 的返回值 —— 头部是调用摘要,每个 agent 启动时
    `add_agent` 追加一张 `_AgentCard`(嵌套子层追加到**同一个**架子 = 全树拍平)。"""

    def __init__(self, summary: Text) -> None:
        super().__init__()
        self._summary = Static(summary)
        self._cards: list[Any] = []
        self._pending: list[Any] = []
        self._all_expanded = True
        # `Vertical` 默认 `1fr`+`overflow:hidden` 会裁剪内部各卡 → 同样显式 auto,
        # 让架子随内容(头部 + 各 agent 卡)自由撑高,不裁任何一张
        self.styles.height = "auto"
        # 2C 精简:架子不再自画 border/padding(把被框吃掉的行列还给内容),
        # 分组感交给标题的下边距;外层壳已由 ExtensionToolBlock 提供
        self._summary.styles.margin = (0, 0, 1, 0)

    def compose(self):
        yield self._summary

    def add_agent(self, name: str, parent: str, depth: int) -> Any:
        """给一个 run 建卡并挂上(未挂载时先存 pending,on_mount 补挂)。"""
        card = _agent_card(name, parent, depth)
        if card is None:
            return None
        self._cards.append(card)
        if self.is_mounted:
            try:
                self.mount(card)
            except Exception:  # pragma: no cover - 挂失败也不能丢事件通道
                return card
            try:
                # 新卡出现 → 把外层对话流也带到可见处(用户不用手动滚找进度)
                card.scroll_visible(animate=False)
            except Exception:  # pragma: no cover
                pass
        else:
            self._pending.append(card)
        return card

    def on_mount(self) -> None:
        pending, self._pending = self._pending, []
        for card in pending:
            try:
                self.mount(card)
            except Exception:  # pragma: no cover
                pass

    def set_output(self, body: Any) -> None:
        """ctrl+o / 前端转发的内置 tool_body —— **不覆盖**平铺内容,改为全局开合
        (与旧树卡的 ctrl+o 语义一致)。"""
        self._all_expanded = not self._all_expanded
        for card in self._cards:
            setter = getattr(card, "set_expanded", None)
            if callable(setter):
                setter(self._all_expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._all_expanded = bool(expanded)
        for card in self._cards:
            setter = getattr(card, "set_expanded", None)
            if callable(setter):
                setter(self._all_expanded)


def _run_stack(args: Any) -> Any:
    """建平铺容器并入 `PENDING_CARDS` 队列(`run_subagent` 工具入口出队配对)。
    入参兼容两种:`render_call` 传的原始 args(dict),或已画好的调用摘要(Text)。"""
    if Vertical is None:  # pragma: no cover - 正常安装不存在
        return None
    try:
        summary = args if isinstance(args, Text) else call_summary(
            args if isinstance(args, dict) else {})
        stack = _RunStack(summary)
        PENDING_CARDS.append(stack)
        return stack
    except Exception:  # pragma: no cover - 构造失败不该带倒渲染链
        return None


def _agent_card(name: str, parent: str, depth: int) -> Any:
    """单 agent 卡(见 `_AgentCard`);构造失败返回 None(该 run 走时间线兜底)。"""
    if Vertical is None:  # pragma: no cover - 正常安装不存在
        return None
    try:
        return _AgentCard(name, parent, depth)
    except Exception:  # pragma: no cover
        return None


def render_call(args: Any, ctx: Any = None) -> Any:
    """`Tool.render_call`:返回**平铺容器**并入队(头部 = 调用摘要) —— 每个
    agent 启动时往里追加自己的一张卡,嵌套子层追加到同一架子(run_subagent
    工具入口出队配对)。"""
    return _run_stack(args if isinstance(args, dict) else {})


def render_result(result: Any, ctx: Any = None) -> Any:
    """`Tool.render_result`:返回 `None` —— 各卡已 `set_done` 定格,树卡退役;
    前端拿到 None 会把内置 tool_body 转发给容器,容器把它当 ctrl+o 全局开合。"""
    return None
