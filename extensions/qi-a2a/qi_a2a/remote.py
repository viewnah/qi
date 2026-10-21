"""把 A2A 条目当成**远端角色**导出,给 qi-agents 的 `subagent` 用。

## 为什么是"角色"而不是"更多工具"

qi-a2a 已经有一个 `a2a` 代理工具,为什么还要这条路:两者解决的是**不同的事**。

| | `a2a` 工具 | 远端角色(本模块) |
| --- | --- | --- |
| 谁发起 | 主会话自己 | `subagent` 委派 |
| 上下文 | 与主会话**共享**一次对话 | **独立**一次对话(委派完就结束) |
| 多轮 | 必须自己传 `contextId` | 一次委派的输入输出就是全部 |
| 适合 | "跟我一起聊这个" | "把这块活交给它去做" |

## 哪些条目能当角色

**不是网关的都能** —— 也就是 `cardUrl` 项、或写了 `tenant` 的项。它们都指向
"一张卡"(`cardUrl` 的响应本身就可能带 tenant);只有 `baseUrl` 而不给 `tenant`
的条目是网关,后面挂着 N 个智能体。

网关为什么不行:枚举它的智能体要**联网拉名片**,而角色发现跑在每次 `subagent` 调用
与 `/agents` 上 —— 那里不能联网。网关继续由 `a2a` 工具带 `agent` 参数调用。

`cardUrl` 为真的情况下 tenant 可能为空(要取到名片才知道),所以角色名用**条目名**,
不用 tenant —— 前者是用户自己起的名,也是 `subagent` 里要写的那个名字。

## 名字与描述

* 角色名 = 条目名(本地同名角色**优先**,远端只填空位);
* 描述 = 条目里的 `description`,**它就是模型选这个角色的唯一依据** —— 空着的话
  只能拿到一句通用话,选得准不准全看它。

## 作用域对齐

`a2a.json` 的两层正好对上 `agentScope`:

| `agentScope` | 看哪些远端条目 |
| --- | --- |
| `user`(默认) | 全局层 |
| `project` | 项目层(且项目受信任) |
| `both` | 两层 |

项目层由 qi-a2a 自己的信任门控把关(`active(trusted=…)`),这里不另设一道 ——
否则同一条声明在两个地方各有一套"算不算数"的规则。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .card import CardError, cards_from_payload, discovery_url, select_card
from .client import A2AClient, RpcError, SendResult, state_name, text_of
from .config import A2AEntry, ConfigError, active

#: `agentScope` → 允许的层。与 qi-agents 的词汇逐字一致(`user` / `project` / `both`)。
_SCOPES: dict[str, tuple[str, ...]] = {
    "user": ("global",),
    "project": ("project",),
    "both": ("global", "project"),
}


@dataclass(frozen=True)
class RemoteAgent:
    """一个**可被委派**的远端智能体(固定 tenant 的 A2A 条目)。

    它只带"怎么找到它",不缓存名片 —— Hikqin 的发布状态是请求级现查的,
    缓存下来的那张卡下一秒就可能是过期的。
    """

    name: str
    description: str
    entry: A2AEntry
    tenant: str
    #: 该条目来自哪一层(`global` / `project`),角色表用 `source` 显示它
    scope: str = "global"

    def as_role_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "source": "a2a", "scope": self.scope, "remote": True}


@dataclass
class RemoteRun:
    """一次远端委派的结果。形状对齐 qi-agents 的本地子运行结果。"""

    ok: bool = False
    text: str = ""
    error: str = ""
    state: str = ""
    task_id: str = ""
    context_id: str = ""
    #: `input-required` 时的审批载荷(转给调用方渲染)
    interrupt: dict[str, Any] | None = None
    #: 过程行(`· …`),进委派卡与 result 的活动区
    steps: list[str] = field(default_factory=list)
    elapsed_ms: int = 0


def remote_agents(cwd: Path | None = None, *, trusted: bool = True,
                  scope: str = "both") -> list[RemoteAgent]:
    """当前配置里**能当角色用**的条目(固定 tenant 的那些)。

    不联网、不展开 `${ENV}` 之外的东西;坏配置回空表(角色发现不该因为一份写坏的
    `a2a.json` 就把整个 `subagent` 工具打挂 —— 那条路上已经有 `a2a` 工具在报错了)。
    """
    layers = _SCOPES.get(scope, _SCOPES["both"])
    try:
        entries = active(cwd, trusted=trusted)
    except ConfigError:
        return []
    out: list[RemoteAgent] = []
    for name in sorted(entries):
        entry = entries[name]
        if entry.scope not in layers:
            continue
        # 网关(只有 baseUrl、没有 tenant)不是单一智能体 —— 见模块文档。
        if not entry.card_url and not entry.tenant:
            continue
        out.append(RemoteAgent(
            name=entry.name,
            description=entry.description or f"远端 A2A 智能体 {entry.name}",
            entry=entry,
            tenant=entry.tenant,
            scope=entry.scope,
        ))
    return out


async def run_remote(agent: RemoteAgent, task: str, *,
                     on_progress: Callable[[str], None] | None = None,
                     on_text: Callable[..., None] | None = None,
                     on_event: Callable[[Any], None] | None = None,
                     signal: Any = None) -> RemoteRun:
    """把 `task` 交给远端智能体,**一次独立对话**(不传 `contextId`)。

    为什么不复用 `contextId`:委派 = 独立上下文(与本地子运行同一条语义)。
    复用会把上一次委派的内容带进这一次,而调用方(模型)根本不知道那件事发生过。

    `on_text` 是**正文增量**(A2A 的 `artifactUpdate`),签名 `(delta, *, replace)`:

    * `replace=True` 表示"之前收到的作废,从这段重新开始" —— A2A 的 `append` 缺省为
      false,Hikqin 实测首片正是这样发的。界面要据此把已显示的内容清掉,否则重放的
      那一句会和旧内容粘在一起。
    * 只有流式(`message/stream`)才有增量;非流式(`message/send`)一次给全,
      此时 `on_text` 不会被调用 —— 调用方要拿 `RemoteRun.text` 兜底。

    失败**不抛**:远端连不上是一个结果,不是宿主异常 —— 与本地子运行同一条规矩。
    """
    import time

    started = time.monotonic()
    run = RemoteRun()
    say = on_progress or (lambda _line: None)

    async def _go() -> None:
        async with A2AClient(agent.entry) as client:
            url = discovery_url(agent.entry)
            if url:
                say("· 取名片")
                payload = await client.get_json(url)
                cards = cards_from_payload(payload, agent.entry)
            else:
                from .card import card_from_entry
                cards = [card_from_entry(agent.entry)]
            card = select_card(cards, agent.tenant)
            run.steps.append(f"· {agent.name} ← {card.name}({card.tenant})")
            say(f"· 发送({len(task)} 字)")

            def _event(event: Any) -> None:
                kind = event.WhichOneof("payload")
                # 正文增量:界面上要"边收边显示",而不是等整轮跑完再一次性贴上。
                # 这里只做**转发**,聚合仍由 client 的 `_Aggregate` 负责 ——
                # 两边各算一遍必然会在某处不一致(比如 append 语义)。
                if kind == "artifact_update" and on_text is not None:
                    update = event.artifact_update
                    piece = text_of(update.artifact.parts)
                    if piece:
                        on_text(piece, replace=not bool(update.append))
                # 进度:状态变化记一行(`working` 不必记,它是起手式),其余原样转出去
                if kind == "status_update":
                    state = event.status_update.status.state
                    name = state_name(state)
                    if name and name != "working" and name not in run.steps:
                        run.steps.append(f"· 状态 {name}")
                        say(f"· 状态 {name}")
                if on_event is not None:
                    on_event(event)

            result: SendResult = await client.send(
                card, task, on_event=_event, signal=signal)
            run.state = result.state
            run.task_id = result.task_id
            run.context_id = result.context_id
            run.interrupt = result.interrupt
            run.text = result.text

    try:
        await _go()
    except (RpcError, CardError, ConfigError) as exc:
        run.ok = False
        run.error = str(exc)
    except Exception as exc:            # noqa: BLE001 远端/网络的意外一律转成结果
        run.ok = False
        run.error = f"{type(exc).__name__}: {exc}"
    else:
        run.ok = run.state != "failed"
        if not run.ok:
            run.error = run.text or f"远端返回 {run.state}"
    finally:
        run.elapsed_ms = int((time.monotonic() - started) * 1000)
        run.steps.append(f"· 用时 {run.elapsed_ms}ms")

    if run.interrupt:
        # `input-required` = 远端在等人拍板。委派本身**算成功**(它把话带回来了),
        # 但正文要说清"没执行",并把回传方式写出来 —— 主会话手里有 qi-a2a 的 `a2a`
        # 工具,`resume` 走那一条。
        run.ok = True
        run.text = _interrupt_text(agent, run)
        run.steps.append("· input-required(等待审批)")
    return run


def _interrupt_text(agent: RemoteAgent, run: RemoteRun) -> str:
    payload = run.interrupt or {}
    lines = [f"远端智能体 {agent.name} 需要人工审批,**这次委派没有执行**:", ""]
    for index, item in enumerate(payload.get("action_requests") or [], start=1):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("action") or "(未命名)"
        ident = item.get("id") or ""
        args = item.get("args")
        args_text = json.dumps(args, ensure_ascii=False) if args is not None else "{}"
        lines.append(f"  {index}. {name}" + (f" id={ident}" if ident else "")
                     + f" args={args_text}")
    allowed: set[str] = set()
    for review in payload.get("review_configs") or []:
        if isinstance(review, dict):
            allowed.update(str(d) for d in (review.get("allowed_decisions") or []))
    lines += ["",
              f"允许的 decisions:{', '.join(sorted(allowed)) or 'approve, edit, reject'}",
              "回传方式(主会话的 a2a 工具):"
              f"a2a(action=\"resume\", server=\"{agent.name}\", "
              f"task_id=\"{run.task_id}\", decisions=[{{\"type\": \"approve\"}}])"]
    return "\n".join(lines)


__all__ = ["RemoteAgent", "RemoteRun", "remote_agents", "run_remote"]
