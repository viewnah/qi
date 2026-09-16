"""协作式中断信号(对齐 pi 的 `AbortSignal`)。

**为什么不用 `asyncio.Task.cancel()`**:取消会把整条 async generator 撕掉 ——
`QiRuntime.stream()` 末尾那次「助手消息落盘」就不会执行,用户屏幕上已经看过的半截回答
在会话文件里消失(直播与回放不一致);工具子进程也只能靠 GC 兜(见 tools/shell.py 的
进程组回收)。

所以走**协作式**:信号在边界被检查(每轮开始、LLM 流式途中、每个工具调用前后),
runner 因此能走到正常收尾路径 —— 未执行的工具调用补上「已中断」结果(不让 `tool_calls`
悬空)、已有文本照常落盘、照常发 `agent_end`。pi 的做法完全一致:被中断的工具调用返回一条
`"Operation aborted"` 结果,而不是把会话撕掉。

两条入口都收敛到「回合有始有终」:
- TUI:`escape` → `AbortSignal.abort()`(再按一次才是强制 `cancel_all()`);
- Web:客户端 abort 请求 → ASGI 取消生成器 → `CancelledError`,这也是 `runtime.stream()`
  会就地收尾的一种情形(见 runtime.py)。
"""

from __future__ import annotations

import asyncio


class AbortSignal:
    """一次性中断信号:`abort()` 置位 / `aborted` 同步读 / `wait()` 异步等。"""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def abort(self) -> None:
        self._event.set()

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()
