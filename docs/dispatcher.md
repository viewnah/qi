# Dispatcher 与 auto 模式(已被取代)

> ⚠️ **v3 起本文整体作废**:`auto` 分派取消,多 agent 改为 **agent-as-tool**(一个 `subagent` 工具 + 起子 agent,见 [extensions.md §7](extensions.md))。核心决策 B7'(每轮重新路由)随之作废 —— pi 的事件面里根本没有「每轮换角色」的 hook,而 agent-as-tool 也不需要它。
> 保留理由:信号分层(L1 规则 / L3 Router / L4 兜底)、`keywords` 词边界匹配、置信度阈值这些**没作废**,会搬进 qi-agents 扩展(若要给 `subagent` 工具加「自动选角色」的便利模式)。
>
> 状态:设计定稿(v1 实现)。已收口决策:B5 tool-call 约束、B6 confidence_min=0.6、**B7' 每轮重新路由(不做会话亲和)**、B8 L2 可插拔默认关。
> 相关:[agent-config.md](agent-config.md)(description/keywords 定义)、[model-config.md](model-config.md)(`[models.router]`)、[runtime 决策表(README)](../README.md)。

## 1. 术语

- **auto**:每轮输入由 Dispatcher 决定执行 agent(默认)。
- **manual**:`--agent <name>` 固定(整会话);TUI `/mode manual` + `/agent <name>`。

## 2. 信号分层(description 是主信号,keywords 只是便宜层)

| 层 | 用什么信号 | 机制 | 定位 |
|---|---|---|---|
| L1 规则 | keywords(精确命中) | 字符串/正则 | 便宜直派,零成本可离线 |
| L2 embedding | description 向量 | 输入 vs 各 description 相似度,取 top-k | **可插拔模块,默认关**(B8);离线/省成本场景开 |
| L3 Router-LLM | **description(主)+ keywords(附)** | 读每个候选的"适用/不适用"段落语义判断 | 真正的语义路由 |
| L4 兜底 | — | confidence 低于阈值 → general;不可行 → 澄清 | 兜底 |

> **路由准确率的上限由 description 质量决定**(必须写"适用 + 明确不适用");keywords 弥补不了差描述,同义改写只能靠 L3/L2 语义层。

## 3. 分派管线(每轮用户输入)

```
输入 + 会话(active_agent、摘要)
  ├─ ① @点名 "@name …" registry 命中 → 直派(source=mention)
  ├─ ② L1 规则层(可关):keywords 命中
  │     唯一命中 → 直派(source=rules)
  │     多命中/无 → 继续
  ├─ ③ L3 Router-LLM(routerProvider/routerModel)
  │     tool-call 强约束输出 {agent, confidence, reasoning}(B5)
  │     解析失败 / agent 不在 registry → 重试 1 次
  └─ ④ L4 兜底:confidence < 0.6(B6,可配 [runtime])
         → general;general 不在 registry 且不可行 → 返回澄清,不执行
```

**每轮都重新路由**(B7'):规则层不认 `active_agent`,确定性直派只有 @点名 与 keywords
唯一命中两条;其余输入一律交 L3。`active_agent` 只作为 Router 的上下文提示——Router 据此
判断"这是新任务(改派)还是当前任务的延续(追问、'继续'、纠正)"。因此**没有任何输入会被
会话状态钉死在某个 agent 上**,代价是每轮多一次 router 模型调用(延迟 + 费用)。

> 早期版本曾是 sticky 关键词启发(B7):命中 active_agent 就零 LLM 沿用。它有两个问题:
> ① 兜底判给 `general` 后,后续输入零命中 → 永久沿用,Router 再无介入机会;
> ② 不设 keywords、纯靠语义路由的 agent 拿不到分派。路由一致性改由 Router 自身承担。

## 4. 优先级

`--agent /manual(整会话固定)` > `@点名` > L1 规则(keywords 唯一命中)> L3 Router > L4 兜底。

> 没有"会话亲和"档:同一会话内每轮都从头走这条链。

## 5. Router 契约

```
system: 你是分派器。按候选 agent 的 description 选择最合适的一个,只输出一次工具调用。
        每轮独立判断,不因同会话而默认沿用:
        新任务(与当前 agent 职责不符)→ 改派;当前任务的延续(追问/继续/纠正)→ 仍选它。
候选(description 即判断依据):…
user: [会话摘要 ≤3 轮] 用户:… 当前agent:…(若最新输入是该任务的延续,应继续选它)
```

- 候选清单 = registry 全部 agent 的 `name + description + keywords`
- 输出 agent 名不存在 → 重试 1 次 → L4

## 6. 数据与审计

```jsonc
// 会话 JSONL 每轮追加(type="dispatch"):
{"type":"dispatch","agent":"code-analyst","confidence":0.93,
 "reasoning":"…","source":"router|rules|mention|manual","ts":"…"}

// 会话级状态(自定义 entry,resume 恢复):
{"type":"state","active_agent":"code-analyst"}
```

TUI dispatch 卡片 = 该 entry 的渲染(谁/置信度/理由/来源,分派透明)。

## 7. 与执行衔接

决定后:active_agent 变更或新会话 → 注入 opening(message 进历史,建议 UI 层);AgentRunner 组装 system_prompt(正文+include)+ 工具(allowlist)+ skills 描述 + 数据源上下文;`stream()` 出事件;结束写会话 + active_agent 状态。

## 8. 测试(回归集,PLAN P5/P9)

```yaml
# tests/router_cases.yaml
- input: "帮我分析这个仓库的 bug"
  expect: code-analyst
- input: "写一份项目周报"
  expect: writer
- input: "继续"                    # 带 active 上下文重新路由(追问 → 沿用)
  active: writer
  expect: writer
- input: "换成帮我分析这个 bug"     # 新任务 → 改派
  active: writer
  expect: code-analyst
```

分派日志可导出做准确率评估(description 质量的持续观测)。
