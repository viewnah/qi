# 扩展机制(对齐 pi 的 extension 系统)

> 状态:**设计定稿,待实现**(v3)。取代 [plugins.md](plugins.md)(v1 的"插件"改名为"扩展",并从"工具 + 消费型配置"扩到 pi 的整套 hook 面)。
> 相关文档:[PLAN.md](PLAN.md)(阶段)、[tools.md](tools.md)(ToolCatalog)、[agent-config.md](agent-config.md)(agent 归属变化)、[dispatcher.md](dispatcher.md)(将被 qi-agents 取代)、[web.md](web.md)(web 归属变化)。
> 参照物:pi v0.85.1 的 `docs/extensions.md` / `docs/packages.md` / `docs/usage.md`、`examples/extensions/subagent/`。

## 1. 定位与分界

**core 只留 pi 也有的东西;qi 自己的增值功能全部拆成扩展。**

这不是折中,是 pi 已经写下的立场(`docs/usage.md`):

> It intentionally does not include built-in **MCP, sub-agents**, permission popups, plan mode, to-dos, or background bash. You can build or install those workflows as extensions or packages, or use external tools such as containers and tmux.

qi 要拆的三样(MCP / 多 agent / web)**正好落在这份清单上**。

| | core(pi 也有) | 扩展(pi 故意不做) |
| --- | --- | --- |
| 内容 | 会话 JSONL + 分支树 · 事件流 · LLMClient + model registry · AgentRunner(tool-loop) · 内置工具(read/ls/find/grep/write/edit/bash/powershell) · 提示词基座 · 技能 / prompt 模板 / 主题资源 · **扩展宿主** · 压缩 · 标题 · 认证 · CLI + TUI · 信任 | **qi-mcp**(MCP 客户 + 工具注册) · **qi-agents**(角色 + 委派) · **qi-web**(HTTP 宿主 + UI) · (将来)todo / plan-mode / 权限闸门 / git checkpoint |
| 依赖 | `pydantic` `typer` `rich` `textual` `PyYAML` `litellm` | qi-mcp 带 `mcp`;qi-web 带 `fastapi`/`uvicorn` |

**core 依赖因此瘦身**:`mcp` 从 `dependencies` 移出(现在是硬依赖),`fastapi`/`uvicorn` 从 optional-extra `web` 移进 qi-web 扩展包。

### 1.1 core 不再认识 "agent"

拆完之后 core 的运行单元是 **`{system_prompt, tools, model}`**,不是 `AgentUnit`:

- `AgentRunner(unit, ...)` → `AgentRunner(prompt_spec, ...)`(`runner.py:110`)
- `AgentRegistry` / `load_all_agents` / `.qi/agents/` 目录语义 → 搬进 **qi-agents**
- `loader.py` 拆两半:**通用 markdown + frontmatter 解析留 core**(技能要用),`agents/` 目录语义跟扩展走
- 会话 JSONL 里的 `agent_id` / `dispatch` / `opening_shown` entry → 变成**扩展自定义 entry**(`appendEntry` 覆盖,见 §3.2)
- `dispatch` / `opening` 事件 → 扩展自定义事件(AG-UI 走 `Custom`)

**所以:`qi` 裸启动 = 单 agent**(基座提示词 + 内置工具),没有角色层、没有分派。这正是 pi 的形态。

## 2. 与 pi 的对应关系

| 概念 | pi | qi |
| --- | --- | --- |
| 代码单元 | extension(`*.ts`,default export 工厂) | **extension**(`*.py`,`register(api)` / `default(api)`) |
| 分发单元 | package(npm/git/本地,`pi install`) | **packages 声明层**(`settings.packages` + `qi install/remove/list/update`,pip 生态;见 [plugins.md §2](plugins.md)) |
| 全局目录 | `~/.pi/agent/extensions/*.ts` 或 `*/index.ts` | `~/.qi/agent/extensions/<name>/extension.py` |
| 项目目录 | `.pi/extensions/`(信任后才加载) | `.qi/extensions/`(信任后才加载) |
| 附加路径 | `settings.extensions`(路径列表) | 同名,同一个语义(**激活 `settings.py:98` 那个死字段**) |
| 单次试用 | `pi -e <path>` | `qi -e <path>`(替换 cli.md 草案里的 `--plugin`) |
| 入口文件名 | 无固定名(`*.ts` / `index.ts`) | 固定 `extension.py`(**决策**:qi 保持"1 目录 = 1 扩展") |
| 依赖 | `dependencies` + `peerDependencies`(宿主提供) | 同构,但 Python 没有 peer:明文规定**扩展不得把 `qi-agent` 写进 `dependencies`**(见 §5.5) |

### 2.1 qi 的增量(pi 没有的)

| 增量 | 用途 | 为什么 pi 没有 |
| --- | --- | --- |
| `provides_config(kind, types)` | 声明消费的 **agent 配置种类**,无提供者则整个配置种类不装载(静默跳过) | pi 的扩展没有"消费型配置"这个概念;`data_sources.json` 是 qi 独有的 |
| `add_route(path, handler)` / `add_static(path, dir)` | 扩展往 qi-web 宿主挂页面/路由 | pi 不做 HTTP 宿主(`--mode rpc` 外置),web.md 已定"宿主在框架 + UI 插件化" |
| ~~`registerProfile(name, spec)`~~ | ~~注册具名 `{system_prompt, tools, model}`~~ → **已否决(E14)** | qi-agents 用 `registerFlag` + `before_agent_start` 自己实现 `qi --agent <name>`,core 零新增概念 |

**边界原则**:增量只做"宿主必须先知道"的事(配置门控、HTTP 挂载)。凡是扩展自己能用工具 + 事件做到的,不往上加 —— 每加一条,扩展面就离 pi 远一分。

## 3. 扩展 API 面

### 3.1 事件(`api.on`)

分四批落地(§9)。"通知" = 返回值被忽略;"可改" = 有明确返回契约。

> **P-E1d 已落地**:总线本体(`ExtensionBus`)、`api.on()`、`ctx` 最小集,以及**第一个真实事件** `session_start`(含 `reason`)。其余事件的**派发点**在 P-E2/P-E3 接上 —— 总线与链语义已就绪且有测试,接一个新事件 = 在对应代码位置加一次 `emit`/`emit_until`。

| 事件 | 时机 | 契约 | 批次 |
| --- | --- | --- | --- |
| `project_trust` | 决定是否信任项目前 | `{trusted}` | P-E1 |
| `session_start` / `session_shutdown` | 会话建立 / 拆除 | 通知 | P-E1 |
| `resources_discover` | `session_start` 后 | `{skillPaths, promptPaths, themePaths}` | P-E1 |
| `input` | 收到用户输入(自动压缩之前) | `continue` / `transform`(改 **`text`** = 改写后的用户输入)/ `handled`(**首胜**,链停,`reply` = 给用户的答复) | ✅ P-E2c-1 |
| `before_agent_start` | 用户消息后、agent loop 前 | `{system_prompt?}`(**链式**,已是建好的全文);`message` 注入延到 P-E3(与 `sendMessage` 同一套语义) | ✅ P-E2c-1 |
| `agent_start` / `agent_end` | 回合起止 | 通知。payload:`{agent}` / `{agent, text, turns, aborted}` | ✅ P-E2c-2 |
| `agent_settled` | 无重试与压缩残留 | **不做**:qi 没有回合级重试/重压机制,这个时机不存在(是“不做”不是“待做”) | ❌ |
| `turn_start` / `turn_end` | 每轮(一次 LLM 响应 + 工具) | **通知**(pi 也是通知 —— 这是“不做每轮换角色”的直接后果)。payload:`{turn_index, timestamp}` / `{turn_index, text, tool_calls}` | ✅ P-E2c-2 |
| `context` | 每次 LLM 调用前 | `{messages}`:可换列表 / 裁剪。**要改就返回新列表** —— 就地把消息对象改掉会污染跨轮上下文 | ✅ P-E2c-2 |
| `message_start` / `message_update` / `message_end` | 消息生命周期 | `message_end` 可 `{message}`(role 必须不变) | P-E3 |
| `tool_call` | 工具执行前 | payload `{tool_name, tool_call_id, input}`;可改 `input`(改动真生效、**不重校**);`{block, reason?}` 拦住;handler 抛异常时 **fail-safe 拦住** | ✅ P-E2c-2 |
| `tool_result` | 工具执行后 | payload `{tool_name, tool_call_id, input, result, details, status, exit_code}`;patch 语义:返回 `{result?, details?, status?}` | ✅ P-E2c-2 |
| `user_bash` | 用户 `!` 命令 | `{operations}` / `{result}` | P-E3 |
| `model_select` / `thinking_level_select` | 模型 / 思考级别变化 | 通知 | P-E3 |
| `session_before_switch` / `session_before_fork` | `/new` / `/resume` / `/fork` | `{cancel}` | P-E3 |
| `session_before_compact` / `session_compact` / `session_compact_failed` | 压缩 | `{cancel}` / `{compaction}` | P-E3 |
| `session_before_tree` / `session_tree` | `/tree` 跳转 | `{cancel}` / `{summary}` | P-E3 |
| `session_info_changed` | 会话改名 | 通知 | P-E3 |

qi 曾需要的"每轮可换角色"hook **不加** —— 见 §7(多 agent 走 agent-as-tool,不需要它)。

> **payload 的键名用 snake_case**(`tool_name` / `system_prompt` / `turn_index`),方法名照 pi 保留
> camelCase —— 见 §9 末尾的命名规则。写扩展时以本节为准。
>
> **一处刻意与 pi 不同**:pi 的顺序是 `tool_execution_start` → `tool_call`,所以它的 tool-start
> 看到的是**未被闸门改过**的参数。qi 反过来(**先过闸门再发 `tool_start`**):qi 的事件流同时是
> **持久化来源**(`_persist_tool` 落 `args`),两者不一致会让回放出现“看到的参数不是跑过的” ——
> 形式对齐让位于这一条。
>
> 被拦下的调用以 `status=error` + `error="denied"` 的结果还给模型(不是抛异常):
> 模型看到“被拦了 + 原因”才能换个思路,而抛异常会把整个回合打成失败。

### 3.2 方法(`api.*`)

| 方法 | 作用 | 批次 |
| --- | --- | --- |
| `registerTool(def)` | 注册工具(含**动态注册**:load 之后、事件里、命令里都能调,立即生效;动态那半在 P-E2b) | ✅ P-E2a |
| `add_tool(tool)` | v1 旧名,`registerTool` 的别名(走同一条盖章路径) | ✅ P-E2a |
| `provides_config(kind, types)` | 消费型配置声明 | 已有 |
| `setActiveTools(names)` / `getActiveTools()` / `getAllTools()` | 运行时改工具集 / 读启用集 / 读全部工具元数据(含 `source_info`)。**未知名字被过滤但会记进 `notes`**;覆盖**跳角色生效**且对后续回合有效 | ✅ P-E2b |
| `exec(cmd, args, opts)` | 起子进程(**不经 shell**,`args` 原样进 argv;带 `signal` / `timeout`,任一命中即 kill) | ✅ P-E2b |
| `registerCommand(name, handler, opts)` | 斜杠命令;handler 收 `(args, ctx)`。**重名不覆盖**:都留着并变成 `name:1` / `name:2`(一个都不丢)。`getArgumentCompletions` 未做 | ✅ P-E3b |
| `registerShortcut(key, handler, opts)` | 快捷键(key 用 textual 写法)。坏键名只提示,不让启动失败 | ✅ P-E3b |
| `getCommands()` | 当前可输入的命令清单(含 `source`);`/help` 用它把扩展命令列出来 | ✅ P-E3b |
| `registerFlag(name, opts)` / `getFlag(name)` | CLI 旗标。**两种写法**:直接 `--name` / `--name=value`,或 `--ext name=value`(可重复;两者同显时**直接写的胜**)。规则照 pi:长旗标宽容、**短旗标报错**、`--` 之后全字面;装载后对账:**未注册的名字**或**字符串旗标缺值** → 退出码 2。**一处刻意差异**:qi 不消费 `--flag` 后面的 token(pi 会吃掉,于是 `pi --plan "问题"` 里那句 prompt 就没了) | ✅ P-E3b-2 |
| `sendMessage(msg, opts)` | 注入自定义消息(**进 LLM 上下文**),`deliverAs: steer\|followUp\|nextTurn` | P-E3 |
| `sendUserMessage(content, opts)` | 注入用户消息(始终触发一轮) | P-E3 |
| `appendEntry(customType, data)` | 落盘扩展自定义 entry(**不进 LLM 上下文**) | P-E3 |
| `registerMessageRenderer` / `registerEntryRenderer` / `registerMarkdownTransformer` | TUI 渲染 | P-E3 |
| `sessionManager` / `getSessionName()` / `setSessionName()` / `setLabel()` | 会话读写 | P-E3 |
| `setModel(model)` / `getThinkingLevel()` / `setThinkingLevel(lv)` | 模型与思考级别 | P-E3 |
| `events.on/emit` | **扩展间**总线(不是宿主事件) | P-E4 |
| `registerProvider(name, cfg)` | 动态注册 provider(代理 / 自定义端点 / 团队模型配置) | P-E4 |
| `add_route` / `add_static` | qi 增量:HTTP 挂载 | P-E5 |

### 3.3 上下文(`ctx`)

| 成员 | 说明 |
| --- | --- |
| `ctx.ui` | **宿主无关的交互面**(见 §5.1):`confirm` / `select` / `input` / `notify`。**总是存在**(没后端就按调用方的 `default` 回答)✅ P-E3a |
| `ctx.sessionManager` | 读 entries、分支、标签 |
| `ctx.model` / `ctx.thinkingLevel` | 当前模型与思考级别 |
| `ctx.cwd` | 工作目录 |
| `ctx.hasUI` | 是否有人在看(TUI/web=真,`-p`=假) |
| `ctx.isProjectTrusted()` | 信任状态 |
| `ctx.signal` | **协作式中断信号** —— 扩展做异步时必须传它(Esc 才能取消 `fetch`/子进程) |
| `ctx.abort()` / `ctx.isIdle()` / `ctx.hasPendingMessages()` | 运行控制 |
| `ctx.compact()` / `ctx.getContextUsage()` | 压缩与上下文占用 |
| `ctx.reload()` | 重载扩展与资源 |
| `ctx.newSession()` / `ctx.fork()` / `ctx.navigateTree()` | 会话操作 |
| `ctx.getSystemPrompt()` / `ctx.getSystemPromptOptions()` | 读当前提示词 |

## 4. 中间件链语义(硬规则)

不把这几条定死,每个扩展作者都会发明一套。规则全部来自 pi 的实测行为:

| 规则 | 内容 | 实现(P-E1d) | 测试 |
| --- | --- | --- | --- |
| **顺序** | 按**扩展装载顺序**(项目目录 → 全局目录 → `settings.extensions[]` → entry point,各自按名排序)。写明“顺序确定,但不是稳定 API” | `ExtensionBus._handlers` 追加序;派发**串行**(不是 gather) | `test_handlers_run_in_registration_order`、`test_side_effects_keep_registration_order_even_when_async` |
| **快照遍历** | handler 里再 `on()` **不影响本次派发**(否则“注册一个自己”会让派发越跑越多) | `list(self._handlers.get(...))` | `test_handler_registered_during_dispatch_does_not_run_now` |
| **链式 + patch** | 同类 handler 依次执行,后者看到前者**改过之后**的值;返回 `dict` 即浅合入 payload;payload 是**同一个 dict**,原地改也对后续可见(对齐 pi 的 `event.input` 可原地改) | `base.update(returned)` + 共享同一个 `base` | `test_dict_returns_are_patched_and_chained`、`test_in_place_mutation_is_visible_to_later_handlers` |
| **裁决有两种形状** ⭐ | ① **键真值**:`tool_call` 的 `{block: True}`(`{block: False}` 不算,不能被假值短路)② **值在集合里**:`input` 的 `{action: "handled"}`。**必须按值判** —— `transform` 走的是同一个 `action` 键,但它是**链式**的(多级改写要接着往下走) | `emit_until(stop_keys=…, stop_values=…)` | `test_first_verdict_wins_and_stops_the_chain`、`test_falsy_verdict_does_not_stop`、`test_transform_chain_survives_because_transform_is_not_a_verdict` |
| **不重新校验** | `tool_call` 改完参数**不再过一遍 schema/权限** —— 这是 pi 的行为,也是 qi 的行为(写进文档,否则会有人以为改了会被拦) | —— | 待 P-E2 随 `tool_call` 一起 |
| **不认识的返回值** | 忽略,不报错(扩展可能比宿主新 —— “插件发了东西但没人看见”比多一个无害返回值难诊断得多) | 非 dict 返回只记录进 `returns` | `test_unknown_return_values_are_ignored_not_errors` |
| **失败隔离** | 单个 handler 抛异常 → **记进 `EmitResult.errors` 并继续链,不中断会话** | `except Exception` + `continue` | `test_one_handler_crashing_does_not_stop_the_chain`、`test_async_handler_crash_is_also_isolated` |
| **fail-safe(拦截器专用)** | 安全类事件用 `on_error_result` 兜底:**闸门自己崩了就拦住**。放行等于“装了闸门反而更不安全” | `on_error_result` 命中即裁决停链 | `test_fail_safe_result_on_error_stops_with_the_safe_verdict` |
| **异常要看得见** | `errors` 不能只存在对象里:`QiRuntime.start_session` 把它转成 `notes`(界面上的一行字)— 否则扩展坏掉的表现是“啥都没发生” | `notes` 通道 | `test_broken_handler_becomes_a_note_not_a_crash` |
| **通知型事件** | `turn_start` / `model_select` 等的返回值被忽略 —— 写进文档,避免“我返回了为什么不生效” | 用 `emit()`(不给 `stop_*`) | 待 P-E2 |

## 5. 前置件(不做这些,hook 系统是空壳)

### 5.1 `ctx.ui`(P-E3a 已落地:后端对象,不是往返事件)

qi 以前**完全没有上行通道**(web.md §16 记着这件事)。

**实现方式与原计划的差异**:原设计是“core 发 `ui_request` / 前端回 `ui_response`”的
往返事件。实际做成的是**后端对象**:`ctx.ui` 包一个鸭子类型的 backend,前端实现它的四个
方法。因为 TUI 与 runtime **在同进程同一个事件循环**里,“弹模态 + 等结果”就是一个
future —— 往返事件在这里只是多一层序列化。

```python
# 前端实现(TUI 在 QiRuntime(ui_frontend=…) 时递进来)
async def confirm(message, *, title=None, default=False) -> bool
async def select(message, options, *, title=None, default=None) -> str | None
async def input(message, *, title=None, default=None, secret=False) -> str | None
def notify(message, *, level="info") -> None
```

**两条硬规则**(§4 同级的契约,有测试钉住):

1. **没有后端时每个方法返回调用方给的 `default`**,于是“交互”退化成“按事先声明好的
   策略走”,而**永远不会挂住**。`confirm` 的 `default` 默认 **False**(拒绝是安全边);
   `select`/`input` 默认 None(取消)。默认值由**调用方**给 —— 只有它知道“这里是拒绝
   安全还是继续安全”。
2. `notify` 没有后端时**落进 `notes`**,不丢弃(否则“扩展说了一句话”就凭空消失)。

后端自己抛异常也走 `default`(记一条 note):交互是辅助手段,不该成为新的失败点。

**与 pi 的差异**:pi 的 method 集有 9 个(`select`/`confirm`/`input`/`editor`/`notify`/
`setStatus`/`setWidget`/`setFooter`/`custom`)。qi 先做前 4 个里能问的 3 个 + `notify` ——
`editor`(多行)与那几个“常驻 UI 元素”等真有需求再加。

**已知尾巴**:

- **web 不能直接用这套**:每个浏览器连接是一个**不同的前端**,所以 web 需要“按回合解析
  前端”而不是构造时固定一个(AG-UI 的 `Custom` 事件往返归 P-E5)。
- TUI 侧只做了 `confirm`/`select`/`input`/`notify`;`has_ui` 与 `ui.has_frontend` 是
  两个不同的问法(“有人在看” vs “能弹交互”),TUI 两者都置真。
- **顺带修掉一个旧缺陷**:`clarify` 以前**从没问过人** —— `QiRuntime._ask` 无条件返回
  None。现在有前端时走 `ui.input`,无头时行为不变(不吃 stdin、不挂住)。
- **扩展工具也能问人**:`ToolContext` 也带上了 `ui`(与 handler 侧同一套规则)—— 否则
  “扩展工具不能问人”会变成一个说不清的例外。

### 5.2 中间件链语义 → §4

### 5.3 信任门控(P-E1)

项目级扩展 = **仓库控制的任意代码**,必须在信任之后才加载。鸡生蛋:`project_trust` 事件本身由"用户级 + `-e` 扩展"参与,项目扩展不参与 —— **机制必须在 core**。

- `settings.defaultProjectTrust`(`ask`/`always`/`never`)已存在(`settings.py:78`),把它接上
- `-a` / `-na`(cli.md 已登记,未实现)落地为"本次信任 / 不信任"
- 未信任时:项目 `.qi/extensions/`、`.qi/agents/`、`.agents/skills` 一律不加载(现在 `.qi/extensions/` 是**无条件扫**的,`registry.py:_iter_plugin_loaders` 是真口子)
- **无 UI 时的默认 = 不信任**(已定,E16):`ask` + headless `-p` 没有人可问 → 跳过项目级资源,并在 stderr 打一条「未信任项目,项目级资源未加载(用 `-a` 信任)」。CI 必须显式传 `-a`。理由:扩展是**仓库控制的任意代码**,fail-safe 只能是"不执行"

**P-E1 已落地的部分**(2026-09):

| 项 | 状态 |
| --- | --- |
| `-a` / `-na`(三态:`None` = 没表态);两个同给 → 报错退出码 2 | ✅ |
| `settings.defaultProjectTrust` 接入(`resolve_project_trust`);未知取值按 `ask` | ✅ |
| **项目 `.qi/extensions/` 的门控**(未信任 → 不进扫描循环,而不是扫了再丢) | ✅ |
| 项目级 `settings.extensions[]` 同样受门控(它随仓库走) | ✅ |
| 提示走 `QiRuntime.notes`(runtime 不做 IO;TUI 逐条显示、CLI 走 stderr) | ✅ |
| `ask` 的**交互式询问** | ❌ 未做 —— 现在一律保守判不信任 + 提示 `-a`(询问要等 `ctx.ui` 通道,P-E3) |
| `.qi/agents/` 与 `.agents/skills` 的门控 | ❌ 未做 —— 它们现在仍无条件加载。留给各自迁移的阶段(P-E4/P-E5),因为 `P-E1` 只动扩展这一处,避免“headless 里项目 agent 突然消失”这种中途回归 |
| `qi web` 的信任入口 | ❌ 未做 —— `qi web` 没有 `-a`,它的 runtime 也不把 `notes` 显示到界面上。当前只能用 `settings.defaultProjectTrust=always`;随 qi-web 扩展(P-E5)一起收口 |

> `notes` 是**启动提示的统一出口**:runtime 只攒字符串(不做 IO/不打印),前端自己决定
> 怎么展示。测试里的 `FakeRuntime` 也需要带 `notes: list[str]`(已同步)。

**P-E1d 新增的 `QiRuntime` 对外面**(扩展宿主需要的那几个):

| 成员 | 作用 |
| --- | --- |
| `bus: ExtensionBus` | 事件总线;`is_empty` 为真时宿主可跳过整条派发路径 |
| `extension_ctx(signal=None) -> ExtensionContext` | 构造 handler 用的 `ctx`(每回合信号不同,所以**不缓存**) |
| `start_session(session, reason)` (`async`) | 派发 `session_start`;**同一 runtime 对同一会话至多一次** |
| `notes: list[str]` | 启动提示通道(扩展也能往里面加) |
| `project_trusted` / `trust_reason` | 信任判定结果与原因 |

### 5.4 hook 粒度结论

**采用 pi 的粒度,不超出**:

- 每个用户 prompt 一次 `before_agent_start`(可换 systemPrompt / 注入消息)
- 每轮 `turn_start` 只是通知
- 每次 LLM 调用前 `context` 可改 messages

理由:§7 选定的多 agent 形态是 agent-as-tool,**不需要**"每轮换角色"。

### 5.5 依赖契约(P-E2/P-E6)

**安装目标(已定,E13):统一装进 qi 自己的解释器环境。** `qi install` 用 `[sys.executable, "-m", "pip", "install", ...]` —— 目标是 qi 所在的那个 venv,**不是**用户 cwd 的项目 venv(否则就是那个经典失败:"我在项目里 pip install 了,qi 就是 import 不到")。

- **公开 import 白名单**(§5.5 —— pi 的 `## Available Imports` 对应物):扩展能 import 的只有 `qi_agent.extensions`:`ExtensionBus` / `ExtensionApi` / `ExtensionContext` / `EmitResult` / `Tool` / `ToolError` / `ToolExecutor` / `ToolOutcome` / `register_tool`。`Tool` 在 P-E2a **搬到了这里**(原来住在 `registry.py` —— 扩展要写工具就必然要这个类型,让它住在“注册表”里等于把内部结构当公开面);`registry` 仍**转发导入**这两个名字,所以旧写法不会断。
- **禁止钉宿主版本**:pi 用 `peerDependencies` + `"*"`;Python 没有 peer,所以明文规定**扩展不得把 `qi-agent` 写进 `dependencies`**(否则 pip 会在解析时把 qi 自己降级 —— 宿主被自己的扩展踢掉)。
  **P-E2d 已落地**:pip 通道装载时读 `importlib.metadata` 的 `requires`,命中就报(带版本满足判定与可执行动作)。**只报告不拒绝** —— 声明本身不危险,危险的是被 pip 解成一棵冲突的树;拒载会让本来能跑的扩展直接不可用。判定时按 PEP 503 归一(`Qi.Agent` / `qi_agent` 都算),但 `qi-agent-extra` 不算。目录通道没有 dist 元数据 → 这条只对 pip 通道生效(PEP 723 声明解析是 P-E6)。
- **目录通道的声明**:PEP 723 inline metadata(`# /// script` + `dependencies`)。**声明仍然要做**(`qi install` 据此装、`qi doctor` 据此查),但**目标是同一个环境**,不再用 `--target` 私有目录(E13)。
- **接受的代价**:所有扩展 + 宿主共用一棵解析树 → **版本冲突无处躲**。三层缓解:① 装载时读 `requires()` 比对已装版本,**不一致就报告**(不静默);② 冲突就拆成 MCP server(独立进程 + 自己的环境);③ 重依赖优先走 MCP。这条也是 §7.2 "进程内" 选择的同一个代价面。
- **uv tool / pipx 的坑**:uv 文档原话 —— tool 环境 "may be upgraded via `uv tool upgrade`, or **re-created entirely** via subsequent `uv tool install`",所以 pip 装进去的扩展**会被重建抹掉**。规矩:声明永远在 `settings.packages`,靠 `qi install --sync` 补回;或者这类用户改用 `uv tool install qi-agent --with qi-mcp`(把扩展写进 uv 的托管依赖,升级不丢)。
- **宿主环境只读时**(系统 Python / Homebrew 管理的解释器):明确报错并给两条出路 —— `uv tool install qi-agent --with <ext>`,或换一个可写的安装方式。不搞静默回退。

## 6. 官方扩展三件套

| 扩展 | 职责 | 带的依赖 | 需要 core 的面 |
| --- | --- | --- | --- |
| **qi-mcp** | `.qi/mcp.json` 各层声明表的解析 + MCP client + 把 server 工具注册进 catalog;`provides_config("mcp_servers")` | `mcp` | `registerTool`(动态)、`appendEntry`、`ctx.signal`、信任门控 |
| **qi-agents** | 角色发现(`.qi/agents/*.md`)+ 委派工具(§7);取代 [dispatcher.md](dispatcher.md) | 无(用 `exec` 或进程内 runner) | `registerTool`、`ctx.model`、`ctx.thinkingLevel`、`ctx.cwd`、`ctx.hasUI`、`ctx.isProjectTrusted()`、`ctx.ui.confirm`、`onUpdate`、`details`、renderer、`appendEntry` |
| **qi-web** | HTTP 宿主 + AG-UI 桥 + 官方 UI 资源;`add_route` / `add_static` | `fastapi`、`uvicorn` | `add_route`/`add_static`、事件流、`ctx.ui` 的 web 侧实现 |

**发布形态**(已定):独立官方 pip 包。core 不内置、不默认装 —— `qi web` 在没装 qi-web 时**不存在这条子命令**,并给出安装提示。

## 7. qi-agents 的设计(基于 pi 官方 subagent 的实测)

### 7.1 形态

**一个工具 `subagent`,三种模式**(照抄 pi 的已验证形状):

| 模式 | 参数 | 行为 |
| --- | --- | --- |
| single | `agent` + `task` | 起一个子 agent 跑任务 |
| parallel | `tasks: [{agent, task}]` | 并发(上限 8 个任务 / 4 并发) |
| chain | `chain: [{agent, task}]` | 顺序执行,`{previous}` 占位符传上一棒输出 |

**角色发现**(照抄,数据结构 qi 已有):`.md` + frontmatter(`name` / `description` / `tools` / `model`,正文 = system prompt);`~/.qi/agent/agents/`(user)+ `.qi/agents/`(project);project 覆盖同名;`agentScope: user|project|both`;**project 角色必须先过 `ctx.ui.confirm`**(它们是仓库控制的提示词 —— pi 的 README 明确写了这条安全模型)。

**工作流预设走 prompt 模板**(`scout → planner → worker` 那种),不写死进扩展。

### 7.2 子 agent 怎么跑:**进程内**(已定,E12)

**实测(2026-09,本仓 venv,热缓存 3 次)**:

| 测量 | 结果 |
| --- | --- |
| `import litellm` | **6.79 / 6.80 / 6.83 s** |
| `import qi_agent` / `qi_agent.cli` | 0.02s / 0.15s → litellm 是**懒加载**的(`llm.py:352,384`) |
| Node 起一次 | **0.04 s** |

含义:**每个进程首调 LLM 都要付 ~6.8s**。

- 进程内:6.8s 只付一次(主进程本来就在用 litellm,边际成本 ≈ 0)
- 子进程:6.8s × N —— 8 个并行子 agent ≈ **54 秒纯 import**,外加 8 份 litellm 内存

**pi 选子进程是因为 Node 起一次 0.04s;这个选择不能平移到 Python。** 而且 pi 自己就有进程内先例:
`docs/sdk.md` 的快速开始就是 `createAgentSession({sessionManager: SessionManager.inMemory()})` + `session.subscribe(...)`,
并且 sdk.md 把 "**Build custom tools that spawn sub-agents**" 列为用例之一。

**所以 core 新增一个挂点**:`ctx.runAgent(spec, task, signal, onUpdate)` —— 即 `QiRuntime.stream()` + 一个**内存会话**,复用 `AgentRunner`。

| | 进程内(采用) | 子进程(pi 的做法,qi 暂不做) |
| --- | --- | --- |
| 隔离 | **上下文**隔离(独立 history / 工具集 / 模型)—— subagent 的本意 | 上下文 + 崩溃 + 依赖 + 环境 |
| 代价 | 无崩溃隔离;依赖与宿主同树 | 6.8s × N;要解析子进程 JSON 事件;**还需补 `--model` / `--append-system-prompt`** |
| core 面 | 新增 `ctx.runAgent` | 零点新 API,但要两个 CLI 开关 |
| abort | 协作式 signal 直接透传(现成的) | 要自己转发 + kill 子进程 |
| 递归防护 | 扩展里一个 depth 计数 | 环境变量(pi 生态的做法) |

**子进程模式列为未来可选**:等真需要"崩溃/依赖/环境隔离"或"跑另一个 qi 版本"时再加(那时才补那两个 CLI 开关)。

### 7.3 必须从 core 拿到的通用能力(全不是"多 agent 专属")

`ctx.runAgent(spec, task)`(**E12 新增的挂点**;内部 = `QiRuntime.stream()` + 内存会话)· `ctx.model` / `ctx.thinkingLevel` / `ctx.cwd` / `ctx.hasUI` / `ctx.isProjectTrusted()` / `ctx.ui.confirm` · 工具的 `onUpdate` 流式进度 · `details` 结构化通道 · `renderCall`/`renderResult` · `ctx.signal`(中断传播)· `appendEntry`(落盘子 agent 调用记录)· `registerFlag`(提供角色选择旗标,语法见 E19)。

**这意味着 P-E1..P-E4 全是通用能力,qi-agents 只是消费者** —— 顺序上先做通用的,三件套最后迁。

### 7.4 agent 私有配置的归属:通用「作用域」(已定,E18)

`.qi/agents/<name>/` 里除了 `agent.md`,还住着 `mcp.json` / `data_sources.json` / `skills/`。v3 里这三样分属不同扩展,需要一个接口 —— 否则目录遍历逻辑要写三遍。

**结论:qi-agents 定义"agent 目录 = 一个 scope",配置种类提供者按 scope 自取。**

```text
qi-agents:  扫 .qi/agents/<name>/ → 得到一个 scope(名字 + 目录 + 信任状态)
qi-mcp:     把 <scope>/mcp.json 当"作用域级声明"解析
db 插件:    把 <scope>/data_sources.json 当"作用域级声明"解析
core 技能发现:同理(私有技能自动绑定)
```

- **目录遍历只一处**(qi-agents),其余扩展只实现"给我一个 scope,我读我要的那个文件"
- 保留 qi 相对 pi 唯一多出来的东西:**自包含 agent 目录**(可整体 import/export)
- 代价:接口层比 pi 多一个 `scope` 参数 —— 记为可接受
- 边界:**只在 agent 目录内**。全局(`~/.qi/agent/`)与项目(`<项目>/.qi/`)那两层的声明表沿用 v1 的作用域(它们不是 agent 私有)

## 8. 兼容与迁移(**一刀切**)

已定:不保留"官方扩展自动装载"。`qi web`、`.qi/agents/`、`mcp.json` 都要求**显式装扩展**。

### 8.1 具体后果清单

| 现在能用 | 拆完之后 |
| --- | --- |
| `qi web` | 没装 qi-web → **没有这条子命令**,提示 `qi install pip:qi-web` |
| `.qi/agents/`、`~/.qi/agent/agents/` | 没装 qi-agents → 目录被忽略(要报**一条启动提示**,不是静默) |
| `~/.qi/agent/mcp.json`、项目 `.qi/mcp.json`、agent 私有 `mcp.json` | 没装 qi-mcp → 不装载 + 启动提示 |
| `--agent <name>`、auto 分派、`@点名` | 全归 qi-agents;core 不再有 `--agent` —— 由 qi-agents 用 `registerFlag` + core 的 `--ext` 提供(**语法见 E19:`qi --ext agent=<name>`**) |
| `data_sources.json` | 归提供该种类的扩展(现在是 db 插件) |
| 会话里的 `agent_id` / `dispatch` / `opening_shown` | 变成扩展自定义 entry(旧会话**仍要能回放** —— 前端按 custom entry 容忍渲染) |

### 8.2 迁移动作

1. **`qi doctor` 新增一节**:检出"旧配置存在但没有对应扩展",输出可复制的安装命令(`qi install pip:qi-mcp pip:qi-agents pip:qi-web`)
2. **启动提示一次**(不是每次刷屏):`检测到 .qi/agents/ 但未安装 qi-agents;这些角色不会被加载。安装:qi install pip:qi-agents`
3. **`qi agents` / `qi web` 子命令在缺失扩展时报错并附安装命令**(而不是 `No such command`)
4. **旧会话回放兼容**:未知 custom entry 不丢弃(与 `details["ui"]` 同一条原则 —— "不认识的类型退回原始显示,绝不丢弃"),加测试钉住

## 9. 阶段与步骤

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **P-E1a/b/c 宿主骨架** ✅ | 改名对齐(plugin→extension:`qi.plugins`→`qi.extensions`、`.qi/plugins`→`.qi/extensions`、`plugin.py`→`extension.py`、`PluginApi`→`ExtensionApi`、`discover_plugins`→`discover_extensions`)+ 激活 `settings.extensions`(父目录或单个扩展目录都行)+ **信任门控**(`-a`/`-na` + `defaultProjectTrust` + headless fail-safe)+ legacy 目录迁移(连入口名一起改) | `tests/test_extensions.py` 19 项:未信任项目的 `.qi/extensions/` 不加载;用户级/项目级 settings 的分层正确;迁移后**能装载**(不只目录到位);entry point 组确为 `qi.extensions` |
| **P-E1d 事件面骨架** ✅ | 总线 `ExtensionBus`(`emit` / `emit_until` / 快照遍历 / patch 链 / 失败隔离 / fail-safe)+ `api.on()` + `ctx` 最小集(cwd/model/thinkingLevel/signal/hasUI/isProjectTrusted/notes)+ 首个真实事件 `session_start`(幂等 + `reason`) | `tests/test_extension_bus.py` 19 项:链语义逐条钉住;真实 `QiRuntime` 端到端收到 `session_start`;未信任项目的扩展**连 register 都不执行**;handler 崩了变成 `notes` 而不是崩会话 |
| **P-E2a 工具注册面** ✅ | `Tool` 搬到公开面(带 `prompt_snippet` / `prompt_guidelines` / `source_info`)+ `registerTool`(正式名)/ `add_tool`(别名)+ `register_tool` 统一盖章 + **内置工具改走 `registerTool`**(dogfood,来源记 `builtin`) | `tests/test_extensions.py` 5 项 + `test_system_prompt.py` 2 项:来源四个键逐条钉住;两条 import 路径是**同一个类**;snippet 回落规则;snippet 不进 tool schema;工具自带指南只随**实际启用**的工具出现 |
| **P-E2b 运行时工具集 + `exec`** ✅ | 动态注册(装载后、事件里都能注册且**下一轮就能调**)+ `getAllTools`(元数据 + `source_info`)/ `getActiveTools` / `setActiveTools`(覆盖优先于 agent 的 `tools`,跳角色生效)+ `exec`(不经 shell) | `tests/test_extension_tools.py` 13 项:端到端证明覆盖**穿过 runner**(下一轮 tool schema 里真的只剩 `read`);无宿主时 `getActiveTools` 降级而 `setActiveTools` **报错**;`exec` 的超时/中断都真 kill、带空格参数不被拆 |
| **P-E2c-1 输入面** ✅ | `input`(`transform` / `handled`)+ `before_agent_start`(链式 `system_prompt`)+ `bus.has()` 逐事件护栏 | `tests/test_extension_events.py` 12 项:`handled` **真的不跑 agent**(LLM 零调用);`transform` 影响模型看到的 + 落盘的;链式顺序;handler 崩了不拖垮这一轮;零扩展时行为与从前一致 |
| **P-E2c-2 轮次与工具事件** ✅ | runner 侧派发点全部接上:`agent_start`/`agent_end`、`turn_start`/`turn_end`、`context`、`tool_call`、`tool_result`(`agent_settled` 不做,见 §3.1) | `tests/test_extension_runner_events.py` 10 项:`turn` 事件的**次数与 index** 钉住;`context` 换掉的列表就是真发出去的那份;`block` = 工具**真的没执行**(带一个“去掉 gate 就真跑”的对照组);handler 崩了 fail-safe 拦住;`tool_result` 的 patch 真的改到模型看到的 |
| **P-E2d 依赖契约** ✅ | 装载时检查 pip 通道扩展的 `requires()`:把宿主写进 `dependencies` 就报告(含版本满足判定与修复动作);`packaging` 可选,拿不到就只报原始 spec 不猜 | `tests/test_extension_deps.py` 9 项:归一化(`Qi.Agent` 算、`qi-agent-extra` 不算)· 版本不满足时说清“不满足”· **报告了但仍然装载** · 一路到 `runtime.notes` 看得见 |
| **P-E3a `ctx.ui`** ✅ | `ExtensionUi`(后端可选 + 无后端时按 `default` 回答 + `notify` 落 notes)+ TUI 后端(复用 `PickerScreen` 弹选择/确认,新增 `PromptScreen` 做输入)+ `ToolContext.ui` + 修好 `clarify` 从没问过人 | `tests/test_extension_ui.py` 20 项:无头时 `confirm` 必然 False 且**闸门真的拦住**;有人点“是”就放行;扩展自己抛错/前端抛错都走 default;`clarify` 回归 |
| **P-E3b 命令与快捷键 + 旗标** ✅ | `registerCommand`(handler 收 `(args, ctx)`,重名加序号不覆盖)+ `registerShortcut`(textual 动态 `bind`)+ `getCommands` + **`registerFlag` / `getFlag`**(值走 core 静态 `--ext name=value`,未知名字退 2);TUI 侧:扩展命令**先于内置**认领(保留 `/quit` `/help` `/hotkeys`)、`/help` 列出扩展命令 | `tests/test_extension_commands.py` 13 项 + `test_extension_flags.py` 15 项:重名变 `:1`/`:2` 且一个不丢 · 没登记处就**报错** · 真 textual 下 `/cmd` 真跑到 handler · **扩展不能顶掉 `/quit`** · `--ext` 的布尔/字符串/打错三种形态 · 打错**不退 0** |
| **P-E3c 会话面** | `appendEntry`(落自定义 entry,不进 LLM 上下文)+ `sessionManager`(读 entries / 分支 / 标签)+ `sendMessage` / `sendUserMessage`(含 `deliverAs`)+ `before_agent_start` 的 `message` 注入 | 扩展能落盘自定义 entry 并在 TUI 回放里看到 |
| **P-E3d 模型、思考级别与会话事件** | `setModel` / `get-setThinkingLevel` + `model_select` / `thinking_level_select` / `session_before_*` / `session_compact*` / `session_tree` / `session_info_changed` + renderer 三件套 | 各事件的派发点接上真实代码路径 |
| **P-E4 core 收窄** | runner 入参从 `AgentUnit` 改 `{system_prompt, tools, model}`;**新增 `ctx.runAgent(spec, task)`(E12)**;`AgentRegistry`/`load_all_agents`/dispatcher 从 core 移出;**仓库自己的 4 个项目 agent 先搬进 `examples/`(E15)**;`events` 总线;`registerProvider` | `qi` 裸启动单 agent 跑通;core 不再 import `dispatcher`/`loader.load_all_agents`;core `dependencies` 去掉 `mcp`;`ctx.runAgent` 能在测试里跑完一个子任务 |
| **P-E5 三件套迁移** | qi-mcp → qi-agents → qi-web(按依赖从少到多);**qi-agents 落地后把仓库 agents 搬回 `.qi/agents/`(E15)**;`ctx.ui` 的 web 侧 + `add_route`/`add_static` | 三个包各自可装可卸;不装时启动提示与 `qi doctor` 正确;`qi web` 由扩展提供;`qi --agent <name>` 由 qi-agents 提供 |
| **P-E6 收尾** | 参考扩展样例(至少一个 `examples/extensions/` 下的完整例子)+ 目录通道的 PEP 723 声明解析 + 冲突报告 + 文档重写(agent-config.md / web.md / dispatcher.md 归档)+ 迁移提示打磨 | 新用户按 extensions.md 能写出并装上第一个扩展;声明与实装不一致时 `qi doctor` 报得出来 |

**依赖关系**:P-E1 ✅ → **P-E2 ✅** → **P-E3a ✅** → **P-E3b ✅** → P-E3c/d → P-E4 顺序做;P-E4 可与 P-E3 并行;P-E5 依赖 P-E2 + P-E3 + P-E4;P-E6 最后。

> **P-E2 整段完成**:工具面(注册 / 元数据 / 来源 / 运行时集合 / `exec`)+ 输入面(`input`、`before_agent_start`)+ 轮次与工具事件(`turn_*`、`context`、`tool_call`、`tool_result`)+ 依赖契约。
> 剩下的 P-E6 尾巴:PEP 723 目录通道声明解析、冲突报告深度、`qi doctor` 汇总。

**命名规则**(避免以后再纠结):**方法名照 pi 保留 camelCase**(`registerTool` / `getAllTools` / `setActiveTools` …) —— 那是扩展作者要背的那部分;qi **自己的数据结构用 snake_case**(`prompt_snippet` / `source_info` 的键)。v1 的旧名(`add_tool`)作为别名留着。

## 10. 决策记录

| # | 决策 | 结论 |
| --- | --- | --- |
| E1 | 命名 | 改叫 **extension**(对齐 pi);"package" 单独留给**安装/记录层**(`settings.packages`),不混用 |
| E2 | 分界 | core = pi 也有的;MCP / 多 agent / web 全拆扩展(pi 的 `usage.md` 立场) |
| E3 | 多 agent 形态 | **agent-as-tool**(pi 的 subagent 模型):一个工具 + 起子 agent,**取消 auto 自动分派** |
| E4 | hook 粒度 | 采用 pi 的粒度(per-prompt `before_agent_start` + 通知型 `turn_start`),**不超出 pi** |
| E5 | qi 定位 | 「单 agent 框架 + 可选多 agent 扩展」(README 第一行随之改) |
| E6 | 发布形态 | MCP / web **独立官方 pip 包**;core 不内置、不默认装 |
| E7 | 兼容 | **一刀切**:必须显式装扩展;`qi doctor` + 启动提示 + 子命令报错三条兜底 |
| E8 | `ctx.ui` 排期 | **先 TUI,再 web** |
| E9 | 目录粒度 | 保持"1 目录 = 1 扩展"固定入口 `extension.py`(不支持 pi 的单文件 `*.ts` 形态) |
| E10 | 链语义 | 见 §4(顺序 = 装载顺序;patch 链;`handled` 首胜;失败隔离不中断会话) |
| E11 | 依赖 | 公开 import 白名单 + **禁把 `qi-agent` 写进 dependencies** + 目录通道用 PEP 723 作声明 |
| E12 | 子 agent 执行 | **进程内**:新增 `ctx.runAgent(spec, task)`(复用 `AgentRunner` + 内存会话)。理由:Python 每进程首调 LLM 付 ~6.8s litellm import,子进程 = 6.8s×N(8 并行 ≈ 54s)vs Node 起一次 0.04s —— pi 的子进程模型不能平移;pi 自己的 SDK 也是进程内。子进程模式列为未来可选 |
| E13 | 依赖安装目标 | **统一装进 qi 自己的解释器环境**(`sys.executable -m pip`);PEP 723 只作声明。接受"共用一棵解析树"的代价:冲突靠报告 + 走 MCP;uv tool 用户走 `uv tool install --with` |
| E14 | `registerProfile` | **不做**。qi-agents 用 `registerFlag` + `before_agent_start` 自己提供角色选择,core 零新增概念(少一个增量就离 pi 近一步)。**具体语法见 E19** |
| E15 | 断点策略 | **P-E4 直接断**(core 不再认 agent),仓库自己的 4 个项目 agent 先搬 `examples/`,qi-agents 落地后搬回 `.qi/agents/`;**不留"先新增后移除"的中间态**(代价:P-E4→P-E5 期间仓库自己处于裸 core) |
| E16 | headless 信任默认 | `defaultProjectTrust=ask` + 无 UI → **默认不信任**:跳过项目级扩展/agents/技能 + stderr 提示 `-a`;CI 必须显式信任(§5.3) |
| E17 | agent 配置对齐 | **先不对齐**:不补 `model`、不改 `tools` 省略语义、不降级 `keywords`。与 CC / pi 的三方对照与理由见 §12;per-agent 模型若需要,走**工具参数**而非 `agent.md` 字段。**已知代价见 §11.7** |
| E18 | 私有配置归属 | qi-agents 定义 **agent 目录 = 一个 scope**;配置种类提供者(qi-mcp / db 插件)按 scope 自取 —— 目录遍历只一处(§7.4) |
| E19 | 扩展旗标 | **照抄 pi 的宽容解析 + 静态 `--ext` 两条并存**。启用 click 的 `ignore_unknown_options`,自己按 pi 的三条规则分类 click 剩下来的 token(长旗标宽容 / 短旗标报错 / `--` 之后全字面),装载后由 `FlagRegistry` 对账(未注册名、缺值都报错退 2)。**实测依据**(不动态改 typer 的选项表):`typer.main.get_command()` 每次重建(对返回值 append 白做);往 `TyperGroup` 里塞裸 `click.Option` 会崩(`'Context' object has no attribute '_param_default_explicit'`);纯 click 能 append 但 `expose_value=False` 时值不回 `ctx.params`、`True` 时又把参数当 kwarg 传给回调而破签名。**pi 为什么能这么做**:它的 CLI 是**手写 argv 循环**(`cli/args.ts`,447 行),未知长旗标直接收进 `unknownFlags` map,所以循环根本不存在。**对 E14 的影响**:qi-agents 的角色选择语法两种都行 —— `qi --agent=reviewer` 与 `qi --ext agent=reviewer` |

## 11. 未定清单

1. 装载顺序是否承诺稳定(现在是"确定但非 API";pi 也不承诺)
2. `ctx.ui` 的无 UI 默认值表(哪些默认拒绝、哪些默认继续)
3. 旧会话里 `agent_id` / `dispatch` entry 的**渲染归属**:core 前端容忍渲染 vs qi-agents 注册 `registerEntryRenderer`
4. `qi -e <path>` 的临时扩展是否进包依赖解析
5. **冲突报告的深度**:只报告"同一包被要求两个版本",还是做完整的解析预演(读全部 `requires()` + 已装版本 + 约束求解)
6. **litellm 的 ~6.8s import**(与本设计无关的独立性能问题):它拖累每一次 `qi -p` 的首次响应。查瘦身开关(`LITELLM_MODE=PRODUCTION` 一类)或换 provider 直连 —— 它会同时放大 §7.2 选了进程内的那个理由
7. **`agent.md` 的 `tools` 省略语义在 agent-as-tool 下有提权风险**(E17 决定先不动):父会话被 `-t` 收窄时,子 agent 按"省略 = catalog 全部"会拿到**全工具**。qi-agents 落地时二选一:① 在扩展里显式解析(用父的 active tools 作基,推荐,不动 `agent.md` 语义);② 改 `agent.md` 的省略语义为"继承父"
8. ~~`registerFlag` 怎么做~~ → **已决(E19)**,已实现:宽容长旗标 + `--ext` 两条并存。仍待定的两条尾巴:① **与 core 选项同名的扩展旗标收不到**(click 先吃掉,如 `--agent`;现在无诊断,只写在文档里);② 短旗标是否要开放给扩展(现照 pi 一律拒绝)。

## 12. agent 配置对齐(Claude Code / pi / qi 三方对照)

> 结论先行:**qi 不照抄任何一家的字段集**(决策 E17「先不对齐」)。本节是**为什么**,以及将来做 `qi agents import claude:` adapter([agent-config.md §10](agent-config.md))时哪些字段可映射、哪些必须降级。

### 12.1 三方对照

| CC 字段 | pi subagent | qi `agent.md` | 差异 |
| --- | --- | --- | --- |
| `name` ✅必填 | ✅ | ✅ 必填 | CC **文件名不必一致**;qi **必须等于目录名** |
| `description` ✅必填 | ✅ | ✅ 必填 | 三家都是路由信号 —— 最对齐的一格 |
| `tools` | ✅ | ✅ 三态 | CC 省略 = 继承子 agent 可用全部;pi 省略 = **继承父**;qi 省略/`*` = **catalog 全部**(见 §11.7) |
| `disallowedTools` | ❌ | `disallowed_tools` | CC 先应用 denylist 再把 tools 在剩余池里解析,两边都列则移除;支持 `mcp__server__*` 模式 |
| `model` | ✅ | ❌ | qi 把模型放在 `models.json`(v3 保持,E17) |
| `skills` | ❌ | 私有 `skills/` 目录 | CC = 按名**预加载全文**;qi = **渐进披露**(只进 description)且**无共享技能库** —— 机制不同 |
| `mcpServers` | ❌ | `mcp_servers` | 语义**接近**(这个 agent 能用哪些 server);CC 可内联定义,qi 只按名绑定声明表(默认无、显式声明) |
| `hooks` | ❌ | ❌ | CC 有 per-agent 生命周期 hook;qi v3 的扩展事件是**全局**的 |
| `permissionMode` · `maxTurns` · `effort` · `memory` · `background` · `isolation` · `omitClaudeMd` · `experimental` | ❌ | ❌ | CC 的执行策略字段 —— qi **一个都不抄**,理由见 12.2 |
| `color` | ❌ | `display_name` | 都是表现层(一个是颜色,一个是显示名) |
| `initialPrompt` | ❌ | `opening.message` | CC = 自动提交的首个 **user** 轮;qi = 助手侧**开场白**(进历史) |
| —— | ❌ | `keywords` | qi 独有(Dispatcher L1 规则层;v3 后暂不消费,保留不废弃 —— E17) |
| —— | ❌ | `include` | qi 独有:`assets/` 下 md 按序拼进 system prompt |
| —— | ❌ | `opening.suggestions` | qi 独有:UI 快捷提问,不进历史 |
| —— | ❌ | 私有 `mcp.json` / `data_sources.json` / `assets/` | qi 独有:**目录级自包含**(见 §7.4 的 scope 接口) |

**两家的重心不同**:CC 的可选字段大半是**执行策略**(能碰什么、跑多久、花多少、在哪隔离);qi 的大半是**内容与打包**(角色说明 + 私有资源)。

**结构性差异**:

| | Claude Code | pi subagent | qi |
| --- | --- | --- | --- |
| 形态 | 单文件 `.md` | 单文件 `.md` | **目录** `agents/<name>/` |
| 来源层 | 5 层(managed > `--agents` CLI > 项目 > 用户 > 插件),**递归扫描** | 2 层(用户 > 项目),`agentScope` 可限 | 3 处(builtin `general` → 用户 → 项目),不递归 |
| 嵌套 | **允许**,默认 3 层(`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`) | 环境变量防递归(第三方有 `maxSubagentDepth`) | 单层(§11 未定 B4) |

### 12.2 为什么 qi 不抄 CC 的执行策略字段

| CC 字段 | 与 qi 哪条既定决策冲突 |
| --- | --- |
| `permissionMode` | A6:bash **无命令级过滤**是故意的(限制靠 `tools` 收窄或容器/VM) |
| `maxTurns` | A12:轮次是**谓词** `stop_after`,且 **qi 自己不传**(同 pi 定义了却不实现) |
| `effort` | 思考级别是**全局** `thinkingLevel`(settings.json),不是 per-agent |
| `memory` / `background` / `isolation` / `omitClaudeMd` | qi 没有这些机制;真要做得先有对应能力,不是加个字段 |

抄进来就是两套政策并存 —— 和 `auto` / `-t` 那次一样,**半对齐比不对齐更容易踩空**。

**per-agent 模型若确实需要**:走**工具参数**(如 `subagent(model="haiku")`)而不是 `agent.md` 字段。这也是 pi 生态的做法 —— 第三方 pi-subagents 把 `session: "fork"` / `async` / `depth` 全放工具参数与自己的 config,不往 agent `.md` 加字段。

### 12.3 将来做导入 adapter 时的降级表

| 处理 | 字段 |
| --- | --- |
| **可直接映射** | `name` · `description` · `disallowedTools` → `disallowed_tools` · `mcpServers` → `mcp_servers` · `color` → `display_name` |
| **需转换** | `model`(qi 无此字段 → 提示丢弃或转成工具参数)· `tools`(CC 是 `Read/Grep/Glob/Bash` 大写名,qi 是小写 `read/grep/find/bash` → 需名字映射表;且两边工具集不重合:CC 有 `WebFetch/WebSearch/Task`,qi 有 `ls/find/powershell`) |
| **需实例化(不能拷字段)** | `skills`:CC 的"预加载全局技能全文" ≠ qi 的"私有渐进披露" → 要变成技能目录 |
| **导入时丢弃并提示** | `permissionMode` · `maxTurns` · `effort` · `memory` · `background` · `isolation` · `hooks` · `omitClaudeMd` · `experimental` |
| **反向导出会丢** | `keywords` · `include` · `opening.suggestions` |

导入 adapter 的价值因此**不是格式转换,而是一份诚实的降级报告**:告诉用户哪些能力在 qi 里没有对应物。
