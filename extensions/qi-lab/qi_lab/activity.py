"""委派直播的内容提取:`tool_execution_update` 的 payload → AG-UI 活动内容。

链路(design/web-lab.md 二期):

    qi-agents._web() → ToolContext.on_update(payload)
      → runner 转成 `tool_execution_update` 总线事件(外层带 tool_call_id)
      → 本模块过滤成 content → `WebState` 广播到活跃流的队列
      → `agui.AguiTranslator.feed_activity` 翻译成 `ACTIVITY_SNAPSHOT` 帧

为什么是**过滤**而不是**构造**:同一事件上还跑着别的扩展的进度回调(工具的
`on_update` 是 pi 通用的 onUpdate),所以进门先认 `qi` 命名空间与 `kind`,不认识的
一律返回 `None` —— 与「不认识的 type 退回 JSON」同一条契约哲学。

回放红线:直播内容**不新增任何持久化事实**。每一行都能在结束后于工具 `result`
里逐字找到(`· 工具 …` / `✎ 第N轮: …` / 输出正文),所以 `qi.history` 回放走
一期的横幅契约解析,直播只是把同一批文字提前显示出来。
"""

from __future__ import annotations

from typing import Any

#: 活动类型(AG-UI `ACTIVITY_SNAPSHOT.activityType`)—— 前端按它认领委派直播。
ACTIVITY_TYPE = "qi.subagent"
#: 允许的阶段。前后端各执一份词汇表,改动必须两处同步(有测试盯着)。
PHASES = ("start", "stream", "act", "think", "done")

#: 队列消息种类:`WebState` 的直播队列里混着两种东西 —— 直播内容与主流帧。
KIND_ACTIVITY = "activity"
KIND_FRAME = "frame"
KIND_END = "end"          # 哨兵:主流 pump 收工(正常或错误帧之后)


def activity_content(payload: Any) -> dict | None:
    """bus 的 `tool_execution_update` payload → 直播 content;无关的进度返回 `None`。

    payload 外层是 runner 拼的(`tool_call_id` / `tool_name` / `partial_result`),
    qi-agents 的信封在 `partial_result["qi"]`。`tool_call_id` 抬进 content:前端靠它
    把整棵委派树归到**同一张**工具卡上(嵌套子运行共用发起方的那次调用)。
    """
    if not isinstance(payload, dict):
        return None
    inner = payload.get("partial_result")
    if not isinstance(inner, dict):
        return None
    inner = inner.get("qi")
    if not isinstance(inner, dict) or inner.get("kind") != "subagent":
        return None
    if inner.get("phase") not in PHASES:
        return None
    run = inner.get("run")
    if not isinstance(run, str) or not run:
        return None
    content = dict(inner)
    call_id = payload.get("tool_call_id")
    if isinstance(call_id, str) and call_id:
        content["toolCallId"] = call_id
    return content
