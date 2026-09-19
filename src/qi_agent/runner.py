"""AgentRunner(P4):单 agent tool-loop,消费 ToolCatalog + LLMClient。

system_prompt 的拼装在 `system_prompt.py`(对齐 pi 的 core/system-prompt.js):
默认基座 / 自定义 SYSTEM.md → 角色层(正文 + include)+ 项目上下文 + 技能清单
+ 数据源 + 工作目录。这里只负责在跑之前把**解析后的工具集**和 cwd 递给它。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field, replace

from .abort import AbortSignal
from .extensions import Tool, ToolError
from .extensions import ExtensionBus, ExtensionContext
from .llm import ChatMessage, LLMClient, LLMDelta, ToolCallOut, stream_llm
from .models import TOOL_ERROR, TOOL_OK, AgentEvent, AgentUnit, ToolOutcome
from .registry import ToolCatalog
from .system_prompt import build_system_prompt


@dataclass
class RunnerSettings:
    """一轮的旋钮。

    `stop_after` 对齐 pi 的 `shouldStopAfterTurn`(**谓词**,不是数字):runner 自己没有
    “最多几轮”的概念,只在每轮结束时问一次嵌入方“现在停吗”。没给就一直跑 —— pi 的核心
    循环也是裸 `while (true)`,退出靠模型不再调工具 / 中断 / 这个谓词。

    **这里没有请求超时**:pi 的超时在 provider/SDK 那一层(`retry.provider.timeoutMs`
    + 客户端重试,见 llm.py),不是套在回合外面的 `asyncio.timeout`。qi 曾经套过一层,
    后果是一次慢请求会把整个回合打成“执行超时”错误。
    """

    #: 每轮结束后调用,收到已完成的轮数;True = 优雅停(stop_after_turns(n) 是常用形状)
    stop_after: Callable[[int], bool] | None = None


def stop_after_turns(limit: int) -> Callable[[int], bool]:
    """跑满 `limit` 轮就优雅停(pi 那个谓词钩子的常用形状)。

    headless(`-p` / `--mode json`)用它做“防跑飞”;交互式不传,靠 `escape` 中断。
    """
    return lambda turns: turns >= limit


def _accumulate_usage(total: dict, usage: dict | None) -> None:
    """把一次 LLM 调用的 usage 累加进总计。

    按需累加所有**整数**字段:provider 差异大(有的给 `cached_tokens`,有的给
    `cache_read_input_tokens`),不设白名单。非整数值忽略(bool 也是 int 的子类,单独排除)。

    只有**可累加**的量走这里。`context_tokens`(最后一次调用的 prompt 大小)不走:
    几轮的 prompt 相加没有含义,它由调用方单独算出。
    """
    total["llm_calls"] = total.get("llm_calls", 0) + 1
    for key, value in (usage or {}).items():
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


async def _iter_until_abort(iterator: AsyncGenerator[LLMDelta, None],
                           abort: AbortSignal | None) -> AsyncGenerator[LLMDelta, None]:
    """把 LLM 流包一层:`abort` 置位就**立即**收尾,不等下一个 token。

    只检查“两次 delta 之间”是不够的:流卡住时下一片可能迟到几十秒,escape 会看起来没反应。
    所以每片都与同一个 watcher 任务赛跑。
    """
    if abort is None:
        async for item in iterator:
            yield item
        return
    it = iterator                                # 异步生成器本身就是迭代器,无需 __aiter__
    watcher = asyncio.ensure_future(abort.wait())
    try:
        while not watcher.done():
            nxt = asyncio.ensure_future(it.__anext__())
            done, _pending = await asyncio.wait({nxt, watcher},
                                                return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:                     # 中断优先于“再等一片”
                nxt.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await nxt
                return
            try:
                yield nxt.result()
            except StopAsyncIteration:
                return
    finally:
        watcher.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await watcher
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await it.aclose()


def _interrupted_outcome() -> ToolOutcome:
    """未执行就被中断的工具调用,补一条结果 —— 不让 `tool_calls` 悬空(pi 同款)。"""
    return ToolOutcome(status=TOOL_ERROR, error="aborted",
                       result="操作已中断(用户中止本回合)")


def _blocked_outcome(reason: str) -> ToolOutcome:
    """被扩展拦下的工具调用。

    以 **error 结果**还给模型(不抛异常):模型看到“被拦了 + 原因”才能换个思路,
    而抛异常会把这个回合打成失败。`error="denied"` 是 models.py 里已经预留的机器可读值。
    """
    return ToolOutcome(status=TOOL_ERROR, error="denied",
                       result=f"Error: 工具调用被扩展拦截:{reason or '未说明原因'}")


def _apply_result_patch(outcome: ToolOutcome, patch: dict) -> None:
    """把 `tool_result` 的 patch 应用到结果上(省略字段保持当前值 —— patch 语义)。

    `patch` 是链结束后的合并 payload,所以“没提到的字段”拿到的就是原值。
    """
    if patch.get("result") is not None:
        outcome.result = str(patch["result"])
    if "details" in patch:
        outcome.details = patch["details"]
    status = patch.get("status")
    if status in (TOOL_OK, TOOL_ERROR):
        outcome.status = status


class AgentRunner:
    def __init__(self, unit: AgentUnit, catalog: ToolCatalog, llm: LLMClient,
                 settings: RunnerSettings | None = None, tool_ctx=None,
                 base_prompt: str | None = None,
                 tool_names: list[str] | None = None,
                 system_prompt: str | None = None,
                 bus: ExtensionBus | None = None,
                 extension_ctx: Callable[[AbortSignal | None], ExtensionContext] | None = None,
                 report: Callable[[str], None] | None = None):
        """`tool_names` 非空时**覆盖** `unit.tools`(扩展的 `setActiveTools` 走这里)。

        为什么要这个参数而不是直接改 unit:`unit` 是装载期的快照,而工具集可以在
        运行时被扩展改(`/plan` 那种只读档)—— 混淆两者会让“改了没生效”变成谜。

        `system_prompt` 非空时直接用给定的(宿主经 `before_agent_start` 链后的结果)。

        三个扩展相关的参数:`bus`(事件总线)、`extension_ctx`(按回合的 ctx 工厂 ——
        `signal` 每回合不同所以不能缓存)、`report`(把 handler 异常报到宿主的 notes)。
        **给了 bus 就必须给 extension_ctx**,否则 handler 拿到的 ctx 是 None ——
        那种“静默少传一个”会在扩展里变成看不懂的 AttributeError。
        """
        if bus is not None and extension_ctx is None:
            raise ValueError("给了 bus 就必须给 extension_ctx(否则 handler 拿不到 ctx)")
        self.unit = unit
        self.catalog = catalog
        self.llm = llm
        self.settings = settings or RunnerSettings()
        self.tool_ctx = tool_ctx
        self.base_prompt = base_prompt
        self.tool_names = tool_names
        self.system_prompt = system_prompt
        self.bus = bus
        self.extension_ctx = extension_ctx
        self.report = report

    async def _emit(self, event: str, payload: dict, abort: AbortSignal | None, *,
                    stop_keys: tuple[str, ...] = (),
                    stop_values: dict[str, tuple[str, ...]] | None = None,
                    on_error_result: dict | None = None):
        """派发一个扩展事件。**零订阅时直接返回 None** —— 不建 payload、不建结果对象,
        所以“零扩展等于零开销”是写在各派发点上的一行判断,而不是一句承诺。"""
        if self.bus is None or not self.bus.has(event):
            return None
        if self.extension_ctx is None:
            # `__init__` 已经保证“有 bus 就有 extension_ctx”;这里是给类型检查器的显式收窄。
            return None
        ctx = self.extension_ctx(abort)
        if stop_keys or stop_values:
            return await self.bus.emit_until(event, payload, ctx=ctx, stop_keys=stop_keys,
                                             stop_values=stop_values,
                                             on_error_result=on_error_result)
        return await self.bus.emit(event, payload, ctx=ctx)

    def _report(self, event: str, result) -> None:
        """把 handler 的异常报到宿主(`EmitResult.errors` 不能只存在对象里)。"""
        if result is None or self.report is None:
            return
        for source, exc in result.errors:
            self.report(f"扩展 {source} 的 {event} 处理失败: {exc}")

    def _tools(self) -> list[Tool]:
        names = self.unit.tools if self.tool_names is None else self.tool_names
        return self.catalog.resolve(names)

    async def run(self, user_input: str, history: list[ChatMessage] | None = None,
                  abort: AbortSignal | None = None):
        """执行一轮用户输入,产出事件。

        `history` 为会话上下文(system 已在其中则跳过)。
        `abort` 为协作式中断信号(见 abort.py):置位后本轮**干净收尾**——未执行的工具
        调用补上「已中断」结果、已有文本照常产出、照常发 `agent_end`(data 里带 `aborted`)。
        **runner 没有轮次上限**:要不要停由 `RunnerSettings.stop_after` 谓词决定(对齐 pi)。
        """
        tools = self._tools()
        msgs: list[ChatMessage] = []
        if history is None or not any(m.role == "system" for m in history):
            # 工具集与 cwd 都要在跑之前定下来:清单进 prompt,且决定技能能否被读取。
            # `system_prompt` 非空表示**宿主已经建好了**(扩展的 `before_agent_start`
            # 可能改过它)—— 那就不要在这里重建,否则扩展的改动会被静默覆盖。
            prompt = self.system_prompt if self.system_prompt is not None else build_system_prompt(
                self.unit, self.base_prompt, tools=tools,
                cwd=self.tool_ctx.workdir if self.tool_ctx else None)
            msgs.append(ChatMessage(role="system", content=prompt))
        if history:
            msgs.extend(history)
        msgs.append(ChatMessage(role="user", content=user_input))

        yield AgentEvent(kind="agent_start", agent=self.unit.name)
        await self._emit("agent_start", {"agent": self.unit.name}, abort)
        last_text = ""
        usage_total: dict = {}
        #: 最后一次 LLM 调用看到的 prompt 大小 = **当前上下文占用**。
        #: 与 `usage_total` 里的 `prompt_tokens`(各步相加)不是一回事 —— 几轮的
        #: prompt 相加得到的数字没有含义,所以单列一个键。
        context_tokens: int = 0
        turns_used = 0
        turn = 0
        aborted = False
        schemas = [t.to_llm_schema() for t in tools] or None
        try:
            while True:                      # 无轮次上限:退出靠模型停 / 中断 / stop_after(对齐 pi)
                turn += 1
                await self._emit("turn_start",
                                 {"turn_index": turn, "timestamp": time.time()}, abort)
                acc_text = ""
                acc_thinking = ""
                tool_calls: list[ToolCallOut] = []
                usage: dict = {}
                # `context`:每次 LLM 调用前可换消息表(裁剪 / 注入)。返回里的 `messages`
                # 就是这一轮要发出去的那一份 —— 要改就返回**新列表**,别就地改消息对象
                # (它们是会话历史的来源,就地改会污染跨轮上下文)。
                patched = await self._emit("context", {"messages": msgs}, abort)
                if patched is not None:
                    self._report("context", patched)
                    replacement = patched.payload.get("messages")
                    if isinstance(replacement, list):
                        msgs = replacement
                # 请求级超时归 provider/SDK(见 llm.py 的 retry.provider),不在这里套 asyncio.timeout
                async for delta in _iter_until_abort(
                        stream_llm(self.llm, msgs, tools=schemas), abort):
                    if delta.reasoning:
                        # 思考内容:与回答分开流式(pi 的 thinking block)
                        acc_thinking += delta.reasoning
                        yield AgentEvent(kind="thinking_delta", agent=self.unit.name,
                                         text=delta.reasoning)
                    if delta.text:
                        acc_text += delta.text
                        # 逐字流式:Web 端靠它打字。CLI/TUI 只读回合末尾的 text 事件,
                        # 所以它们的输出不变——这是有意为之的向后兼容。
                        yield AgentEvent(kind="text_delta", agent=self.unit.name,
                                         text=delta.text)
                    if delta.finished:
                        tool_calls = delta.tool_calls
                        usage = delta.usage
                turns_used = turn
                # 流式途中被中断:半截的 tool_calls 参数不可信,一律丢(pi 对截断消息同处理)
                if abort is not None and abort.aborted:
                    aborted = True
                    tool_calls = []
                _accumulate_usage(usage_total, usage)
                prompt_side = (usage or {}).get("prompt_tokens")
                if isinstance(prompt_side, int) and not isinstance(prompt_side, bool):
                    context_tokens = prompt_side
                msgs.append(ChatMessage(role="assistant", content=acc_text,
                                        tool_calls=tool_calls))
                last_text = acc_text
                # 每轮 LLM 回复单独声明一次:让 runtime 能把"工具之间的叙述"落盘,
                # 否则直播看得见、刷新后丢失(直播与回放不一致)。
                yield AgentEvent(kind="assistant_message", agent=self.unit.name,
                                 text=acc_text,
                                 data={"step": turns_used,
                                       "thinking": acc_thinking,
                                       "tool_calls": [c.name for c in tool_calls]})
                if not tool_calls:
                    pass                  # 空列表 → 下面的 for 自然跳过;`turn_end` 仍要发
                for call in tool_calls:
                    # `tool_call`:可改 `input`(改动真生效、不重校)、可 `block`。
                    # 出错时 **fail-safe 拦住** —— “装了闸门反而放行”是最坏的结果。
                    verdict = await self._emit(
                        "tool_call",
                        {"tool_name": call.name, "tool_call_id": call.id, "input": call.args},
                        abort, stop_keys=("block",),
                        on_error_result={"block": True, "reason": "扩展闸门异常"})
                    if verdict is not None:
                        self._report("tool_call", verdict)
                        patched_args = verdict.payload.get("input")
                        if isinstance(patched_args, dict):
                            call.args = patched_args
                    # 顺序:**先**过闸门再发 `tool_start` —— 事件里的 args 必须是真正要执行的
                    # 那一份(前端与落盘都读它)。pi 是反的(tool_execution_start 在前),
                    # 但 qi 的事件流同时是持久化来源,args 对不上会变成“回放里看到的参数
                    # 不是跑过的”—— 那种不一致比事件顺序上的形式对齐更重要。
                    yield AgentEvent(kind="tool_start", agent=self.unit.name,
                                     tool=call.name, data={"args": call.args})
                    if abort is not None and abort.aborted:
                        # 剩下的调用一律不跑:补结果而不是默默消失,否则两边记录不一致
                        aborted = True
                        outcome = _interrupted_outcome()
                    elif verdict is not None and verdict.result is not None:
                        outcome = _blocked_outcome(str(verdict.result.get("reason") or ""))
                    else:
                        outcome = await self._execute(tools, call, abort)
                    # `tool_result`:patch 语义(可改模型看到的结果 / 结构化 details / status)
                    patched = await self._emit(
                        "tool_result",
                        {"tool_name": call.name, "tool_call_id": call.id, "input": call.args,
                         "result": outcome.result, "details": outcome.details,
                         "status": outcome.status, "exit_code": outcome.exit_code},
                        abort)
                    if patched is not None:
                        self._report("tool_result", patched)
                        _apply_result_patch(outcome, patched.payload)
                    # 结构化结果:前端工具卡片靠 status/duration_ms/exit_code 渲染,
                    # 不再解析 text 前缀;text 仍是模型可见的原文(与旧版一致)。
                    yield AgentEvent(kind="tool_end", agent=self.unit.name, tool=call.name,
                                     text=outcome.result,
                                     data={"status": outcome.status,
                                           "duration_ms": outcome.duration_ms,
                                           "exit_code": outcome.exit_code,
                                           "error": outcome.error,
                                           # 插件/工具给客户端看的自由结构。
                                           # 这一行以前不存在 —— 于是插件就算自己造了
                                           # details 也**到这一行就被扔掉**(见 docs/web.md §16)。
                                           "details": outcome.details})
                    msgs.append(ChatMessage(role="tool", content=outcome.result,
                                            tool_call_id=call.id))
                await self._emit("turn_end",
                                 {"turn_index": turn, "text": acc_text,
                                  "tool_calls": [c.name for c in tool_calls]}, abort)
                if not tool_calls or aborted:
                    break
                # pi 的 shouldStopAfterTurn:每轮结束问一次嵌入方,而不是比较一个数字。
                # 注意必须显式 break —— 旧实现靠 `for turn in range(limit)` 自然耗尽,
                # 换成 `while True` 之后只发事件不退出会跑飞(有测试钉住这一条)。
                if self.settings.stop_after is not None and self.settings.stop_after(turns_used):
                    yield AgentEvent(kind="error", agent=self.unit.name,
                                     text=f"达到轮次上限 {turns_used},已停止")
                    break
        except asyncio.CancelledError:
            raise                                    # 硬取消照旧上抛(收尾由 runtime.stream 做)
        if last_text:
            yield AgentEvent(kind="text", agent=self.unit.name, text=last_text)
        await self._emit("agent_end", {"agent": self.unit.name, "text": last_text,
                                        "turns": turns_used, "aborted": aborted}, abort)
        end_data: dict = {"messages": [m.to_dict() for m in msgs[1:]],
                          "usage": {"turns": turns_used, "context_tokens": context_tokens,
                                    **usage_total}}
        if aborted:
            end_data["aborted"] = True
        yield AgentEvent(kind="agent_end", agent=self.unit.name, text=last_text,
                         data=end_data)

    async def _execute(self, tools: list[Tool], call: ToolCallOut,
                       abort: AbortSignal | None = None) -> ToolOutcome:
        """执行一次工具调用,返回**结构化**结果。

        计时在统一入口做,所以所有工具(含插件工具)都自动带上 duration_ms,不必各自上报。
        工具返回 `str`(旧约定)视为 `ok`;返回 `ToolOutcome` 则采用其 status/exit_code。

        `abort` 是**每回合**的信号,而 `tool_ctx` 跨回合复用 —— 所以用副本带上它,
        不会把一个回合的中断状态串到下一个回合(工具层靠它杀进程:见 tools/shell.py)。
        """
        started = time.perf_counter_ns()

        def elapsed_ms() -> int:
            return (time.perf_counter_ns() - started) // 1_000_000

        tool = self.catalog.get(call.name)
        if tool is None:
            return ToolOutcome(status=TOOL_ERROR, error="unknown_tool",
                               result=f"Error: 未知工具 {call.name}", duration_ms=elapsed_ms())
        ctx = self.tool_ctx
        if abort is not None and ctx is not None:
            ctx = replace(ctx, abort=abort)
        try:
            raw = await tool.execute(call.args, ctx)
        except ToolError as exc:
            return ToolOutcome(status=TOOL_ERROR, error="tool_error",
                               result=f"Error: {exc}", duration_ms=elapsed_ms())
        except Exception as exc:  # noqa: BLE001 工具异常 → 可读结果
            return ToolOutcome(status=TOOL_ERROR, error="exception",
                               result=f"Error: 工具执行异常 {type(exc).__name__}: {exc}",
                               duration_ms=elapsed_ms())
        if isinstance(raw, ToolOutcome):
            if not raw.duration_ms:      # 工具未自行上报 → 统一计时兜底
                raw.duration_ms = elapsed_ms()
            return raw
        return ToolOutcome(status=TOOL_OK, result=str(raw), duration_ms=elapsed_ms())
