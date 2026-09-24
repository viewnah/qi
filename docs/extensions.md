# 扩展

扩展是**一个 Python 模块 + 一个 `register(api)` 入口**。装进 qi 之后,它可以给模型加工具、订阅事件、加命令,以及自己画界面。

能做什么:

| 想做 | 用什么 | 见 |
| --- | --- | --- |
| 给模型加工具 | `api.register_tool(...)` | §7 |
| 拦或改工具调用 | `api.on("tool_call" / "tool_result" / "tool_execution_*")` | §4 |
| 改系统提示词 / 注入消息 | `before_agent_start`、`api.send_message` / `send_user_message` | §4、§5 |
| 加斜杠命令 / 快捷键 / CLI 旗标 / 子命令 | `register_command` / `register_shortcut` / `register_flag` / `register_cli_command` | §5 |
| 问用户(确认 / 选择 / 输入 / 编辑器) | `ctx.ui.*` | §8 |
| 自定义界面 | `ctx.ui.custom` / `set_widget` / `set_footer`,以及工具与消息的渲染回调 | §8 |
| 存自己的状态 | `api.append_entry` / `set_label` / `set_session_name` | §5 |
| 起一个受管的子运行 | `api.run_agent(spec, task)` | §5 |
| 声明消费型配置(按作用域) | `provides_config` / `register_resolver` / `resolve_tools` | §9 |

可跑的样例:[`examples/extensions/hello/`](../examples/extensions/hello/extension.py) —— 一份 `extension.py` 把主要的面各走一遍,复制它改名字就是你的扩展。`extensions/` 下的三个自带扩展是更完整的参考实现。

> 相关文档:[packages.md](packages.md)(安装与声明)、[how-qi-works.md](how-qi-works.md)(内置工具)、[settings.md](settings.md)(`extensions` / `packages` 字段)。

## 1. 快速开始

```python
# ~/.qi/agent/extensions/my-ext/extension.py
from qi_agent.extensions import Tool

def register(api):
    # 1) 订阅事件:会话开始说一句(没有界面时自动落到启动提示)
    api.on("session_start", lambda payload, ctx: ctx.ui.notify("my-ext 已装载"))

    # 2) 拦住危险命令
    def check_tool(payload, ctx):
        if payload["tool_name"] == "bash" and "rm -rf" in str(payload["input"].get("command", "")):
            return {"block": True, "reason": "被 my-ext 拦下"}
    api.on("tool_call", check_tool)

    # 3) 注册一个模型能调用的工具
    async def greet(args, ctx):
        return f"你好,{args['name']}!(工作目录:{ctx.workdir})"
    api.register_tool(Tool(
        name="greet",
        description="跟某人打个招呼。",
        parameters={"type": "object",
                    "properties": {"name": {"type": "string", "description": "称呼"}},
                    "required": ["name"]},
        execute=greet,
    ))

    # 4) 加一条斜杠命令
    api.register_command("hello", lambda args, ctx: ctx.ui.notify(f"hello {args}"),
                         description="示例命令")
```

试跑与确认:

```bash
qi -e ./my-ext          # 只本次运行(scope=temporary);TUI 与无头都生效
qi list                 # 看装了哪些扩展,以及与 settings.packages 的声明比对
qi doctor               # 诊断:装载失败 / 依赖契约 / 声明不一致
```

`register` 可以是 async —— 返回 awaitable 时宿主会等它跑完再继续启动。

**别在 `register` 里起后台资源**(进程、socket、watcher、定时器):有些调用根本不进会话,那些资源会没人收。要在 `session_start` 里起,并在 `session_shutdown` 里幂等收尾。

## 2. 扩展放在哪、怎么加载

一个扩展 = 一个目录 + 固定入口 `extension.py`(文件名固定,里面必须有 `register(api)`)。

| 通道 | 位置 |
| --- | --- |
| 用户级 | `~/.qi/agent/extensions/<名>/extension.py` |
| 项目级 | `<项目>/.qi/extensions/<名>/extension.py`(未信任不加载,见下) |
| 附加路径 | `settings.json` 的 `extensions[]`:每项可以是扩展目录的**父目录**,也可以直接指向**单个扩展目录**;支持 `-<路径>` 排除 |
| 单次试用 | `qi -e <目录>` / `--extension`(只本进程,`scope=temporary`) |
| pip 包 | entry point 组 `qi.extensions`,**且必须在 `settings.packages` 里声明**(没声明的装了也不加载 —— 对齐 pi) |

装载顺序:**项目级 → 用户级 → `settings.extensions[]` → entry point**,各组内按名排序。顺序确定,但**不是稳定 API** —— 需要保证先后关系的扩展应当写成一个扩展。

**装载失败是整体中断**:任何一个扩展在 `register` 里抛异常,这一轮装载就停下(不会"坏一个、其余照跑")。所以 `register` 要薄,重活留给事件。

**项目级要过信任门控**:项目目录与项目级 `settings.extensions[]` 只有在项目被信任后才装载 —— 扩展是仓库控制的任意代码。

- `qi -a` 信任本项目(本次运行),`qi -na` 明确不信任;两者同给报错退出码 2。
- 没表态时看**用户级** `settings.defaultProjectTrust`(`ask` / `always` / `never`);`ask` 在没有 UI(如 `-p`)时**保守判不信任**,并在 stderr 提示用 `-a`。
- 用户级扩展(以及 `-e` 给的)不受项目信任影响;`project_trust` 事件让它们参与这次投票 —— 项目级扩展自己不参与(否则仓库能给自己背书)。
- 跳过项目级资源时会有启动提示(`qi doctor` 也能看到原因)。

**关掉发现**:`--no-extensions`(=`-ne`)关掉 entry point + 两个内置目录 + `settings.extensions[]`;`-e` 显式给的仍然生效。

安装、声明(`settings.packages`)与两条装法的失效面见 [packages.md](packages.md)。

## 3. 可用的 import(公开面)

扩展能 import 的公开面只有 `qi_agent.extensions`:

`ExtensionBus` · `ExtensionApi` · `ExtensionContext` · `EmitResult` · `ExecResult` · `ExtensionUi` · `SessionView` · `ModelView` · `ModelRegistryView` · `RendererRegistry` · `Tool` · `ToolError` · `ToolExecutor` · `ToolOutcome` · `register_tool` · `tool_info` · `CommandRegistry` · `FlagRegistry`

- `Tool` 就住在这一层 —— 要写工具就必然要这个类型;`registry` 仍**转发导入**它,所以旧写法不会断。
- 调用方的接口是 `register(api)` 拿到的那个 `ExtensionApi`;上面这些类型只在"要写工具 / 要造类型"时才需要 import。
- 其它 `qi_agent.*` 模块是内部实现,不承诺兼容。

## 4. 事件(`api.on`)

`api.on(事件名, handler)`:handler 收 `(payload, ctx)`,可以同步或 async。返回值按事件分三类 —— **通知型**(忽略)、**可改**(补丁)、**裁决**(停链)。

**事件共 36 个。**

| 事件 | 时机 | 契约 |
| --- | --- | --- |
| `project_trust` | 首次会话绑定时(信任判定之前) | 返回 `{trusted: "yes"\|"no"\|"undecided"}` —— **首个 yes/no 拥有决定权**;`remember: true` 写进 `trust.json`;`undecided` 继续走 `trust.json` → `defaultProjectTrust`。**只有用户级/CLI 扩展参与**,给了 `-a`/`-na` 时不发 |
| `session_start` | 会话建立 / 载入 / 重载 | 通知。`{reason, session, cwd}`;`reason` ∈ `startup`/`reload`/`new`/`resume`/`fork`。**幂等**(同一会话至多一次) |
| `resources_discover` | `session_start` 之后 | 返回 `{skillPaths, promptPaths, themePaths}`。**qi 真的加载**:技能进 `top_skills`(给技能根或直接给 `<name>/` 都认),另两类路径进 `runtime.extra_resource_paths` |
| `input` | 收到用户输入(自动压缩之前) | `continue` / `transform`(改 **`text`**)/ `handled`(**首胜**,链停,`reply` = 给用户的答复) |
| `before_agent_start` | 用户消息**已落盘**后、agent loop 前 | `{prompt, system_prompt, agent}`;可链式改 `system_prompt`;`message` = 注入一条**持久**消息(字符串或 `{content}`) |
| `agent_start` / `agent_end` | 回合起止 | 通知。`{agent}` / `{agent, text, turns, aborted}` |
| `turn_start` / `turn_end` | 每轮(一次 LLM 响应 + 工具) | **通知**。`{turn_index, timestamp}` / `{turn_index, text, tool_calls}` |
| `context` | 每次 LLM 调用前 | `{messages}`:可换列表 / 裁剪。**要改就返回新列表**;就地改消息对象会污染跨轮上下文 |
| `message_start` / `message_update` / `message_end` | 消息生命周期(user / assistant / tool 都会发) | `message_end` 可改 `{message}` 的 **`content`**;`role` 与工具调用不可改(改了会让模型看到的与跑过的不一致) |
| `tool_call` | 工具执行前 | `{tool_name, tool_call_id, input}`;可改 `input`(真生效、**不重校**);`{block, reason?}` 拦住;handler 抛异常 **fail-safe 拦住** |
| `tool_execution_start` / `tool_execution_update` / `tool_execution_end` | 工具执行的生命周期 | **通知**。`{tool_call_id, tool_name, args}` / `+ partial_result`(工具调 `ctx.on_update(partial)` 触发)/ `{result, is_error, details}` |
| `tool_result` | 工具执行后 | `{tool_name, tool_call_id, input, result, details, status, exit_code}`;patch 语义:返回 `{result?, details?, status?}` |
| `before_provider_request` | 请求发出前 | `{payload}` —— **可整个替换**(返回 `{payload: <新 dict>}`) |
| `before_provider_headers` | 请求头组装后 | `{headers}` —— handler **原地改**(值为 `None` 则删掉该头) |
| `after_provider_response` | 拿到响应、还没消费流 | 通知。`{status, headers}`(取不到元数据时 `0` / `{}`) |
| `model_select` / `thinking_level_select` | 模型 / 思考级别变化 | 通知。`{model, previous, source}` / `{level, previous_level, source}`。**只从 runtime 发**(TUI 与扩展共用那一处) |
| `session_info_changed` | 会话改名 | 通知。`{name, source}`;`source` ∈ `user` / `auto` / `extension` |
| `session_before_switch` / `session_before_fork` / `session_before_tree` | 切会话 / fork / 树跳转**之前** | `{cancel: true}` 拦下。扩展调 `ctx.new_session()/fork()/navigate_tree()/switch_session()` **以及** TUI 的 `/new` `/resume <id>` `/fork` `/tree` 都走这道闸门 |
| `session_tree` | 树跳转之后 | 通知。`{newLeafId}` |
| `session_before_compact` / `session_compact` / `session_compact_failed` | 压缩 | `session_before_compact` 可 `{cancel}` 拦下、或 `{summary}` 自带摘要(自带时**不调模型**);成功后 `session_compact`;失败先发 `session_compact_failed`(`{error}`)再抛 |
| `session_shutdown` | 退出 / 换会话之前 | 通知。`{reason}` ∈ `quit`/`new`/`resume`。TUI 退出时发;`emit_session_shutdown()` 也可手动调 |
| `agent_settled` | 回合真的结束(无排队消息) | 通知。**保守近似**:provider 级重试在客户端内部发生,那时本方法还没跑完 |
| `ui_prompt_start` / `ui_prompt_end` | 阻塞式 UI 问答开始 / 结束 | 通知。`{reason: "ui_prompt", kind, title}`;`kind` ∈ `confirm`/`select`/`input`/`editor`/`custom` |
| `user_bash` | 用户 `!` / `!!` 命令 | `{command, excludeFromContext, cwd}`;返回 `{operations: {command}}` 换命令,或 `{result}` 直接给结果(不执行)。**TUI 专属**(无头模式没有 `!`) |

被拦下的工具调用以 `status=error` + `error="denied"` 的结果还给模型(不是抛异常):
模型看到"被拦了 + 原因"才能换个思路,而抛异常会把整个回合打成失败。

> **顺序**:`tool_execution_start`(带**未被闸门改过**的原始参数)→ `tool_call`(闸门在这里改)→ 真正执行。
> 两者不矛盾是因为**持久化来源是内部 `AgentEvent(kind="tool_start")`**(在闸门**之后**发),
> 不是这条扩展事件 —— 所以"回放里看到的参数不是跑过的"那个顾虑不成立。
>
> **payload 的键名用 snake_case**(`tool_name` / `system_prompt` / `turn_index`),方法名用
> snake_case、驼峰写法同样可用 —— 见 §5 开头的命名规则。写扩展时以本节为准。
>
> **兼容别名(两套都收)**:总线在派发前给每个 payload 补上 `type`(= 事件名,判别字段),
> 以及**同名同义**键的驼峰别名(`tool_name` → `toolName`、`tool_call_id` → `toolCallId`、
> `system_prompt` → `systemPrompt`、`turn_index` → `turnIndex` …)。qi 自己的键**一个不少**,
> 所以两套写法(`event.type` / `event.toolName` 或 snake_case)同时成立。
> payload **结构**不假造:如 `turn_end` 给的是 `text`/`tool_calls`。

### 4.1 事件链语义(硬规则)

不把这几条定死,每个扩展作者都会发明一套。规则如下(每条都有测试钉住):

| 规则 | 内容 | 实现 |
| --- | --- | --- |
| **顺序** | 按**扩展装载顺序**(项目目录 → 全局目录 → `settings.extensions[]` → entry point,各自按名排序)。写明“顺序确定,但不是稳定 API” | `ExtensionBus._handlers` 追加序;派发**串行**(不是 gather) |
| **快照遍历** | handler 里再 `on()` **不影响本次派发**(否则“注册一个自己”会让派发越跑越多) | 派发前取 `list(...)` |
| **链式 + patch** | 同类 handler 依次执行,后者看到前者**改过之后**的值;返回 `dict` 即浅合入 payload;payload 是**同一个 dict**,原地改也对后续可见 | `base.update(returned)` + 共享同一个 `base` |
| **裁决有两种形状** ⭐ | ① **键真值**:`tool_call` 的 `{block: True}`(`{block: False}` 不算,不能被假值短路)② **值在集合里**:`input` 的 `{action: "handled"}`。**必须按值判** —— `transform` 走的是同一个 `action` 键,但它是**链式**的(多级改写要接着往下走) | `emit_until(stop_keys=…, stop_values=…)` |
| **不重新校验** | `tool_call` 改完参数**不再过一遍 schema/权限** —— 这是**刻意的**(写进文档,否则会有人以为改了会被拦) | `tool_call` 闸门与执行之间没有第二道校验 |
| **不认识的返回值** | 忽略,不报错(扩展可能比宿主新 —— “扩展发了东西但没人看见”比多一个无害返回值难诊断得多) | 非 dict 返回只记录进 `returns` |
| **失败隔离** | 单个 handler 抛异常 → **记进 `EmitResult.errors` 并继续链,不中断会话** | `except Exception` + `continue` |
| **fail-safe(拦截器专用)** | 安全类事件用 `on_error_result` 兜底:**闸门自己崩了就拦住**。放行等于“装了闸门反而更不安全” | `on_error_result` 命中即裁决停链 |
| **异常要看得见** | `errors` 不能只存在对象里:`QiRuntime.start_session` 把它转成 `notes`(界面上的一行字)— 否则扩展坏掉的表现是“啥都没发生” | `notes` 通道 |
| **通知型事件** | `turn_start` / `model_select` 等的返回值被忽略 —— 写进文档,避免“我返回了为什么不生效” | 用 `emit()`(不给 `stop_*`);runtime 侧走 `_emit_notice`(后台任务,同步入口也能发) |

### 4.2 事件粒度

- 每个用户 prompt 一次 `before_agent_start`(可换 system prompt、可注入消息)。
- 每轮 `turn_start` 只是通知。
- 每次 LLM 调用前 `context` 可改 messages。

需要更细的粒度(比如"每轮换一套提示词 / 工具集")就用 `api.run_agent(spec, task)` 起一个**子运行**,而不是指望 hook —— 子运行有自己的提示词、工具集与模型。

## 5. 方法(`api.*`)

**命名:snake_case 是正式名,驼峰是别名**(两套都指向同一个函数对象,
`api.registerTool is api.register_tool`)。**参数形状两边都收**:options 对象写法
(`registerCommand(name, {handler, description, getArgumentCompletions})`、
`registerFlag(name, {type, default})`、`registerTool({name, label, ..., execute})`)
与关键字写法都认。

| 方法 | 作用 |
| --- | --- |
| `register_tool(tool)` / `registerTool(def)` | 注册工具(含**动态注册**:load 之后、事件里、命令里都能调,下一轮即可调)。收 `Tool`,也收 dict 形状(见 §7) |
| `add_tool(tool)` | v1 旧名,`register_tool` 的别名 |
| `get_all_tools()` / `get_active_tools()` / `set_active_tools(names)` | 读全部工具元数据(含 `source_info` + `promptGuidelines`/`sourceInfo` 键)/ 读启用集 / 改运行时工具集。**未知名字被过滤但会记进 `notes`** |
| `exec(cmd, args, opts)` | 起子进程(**不经 shell**,`args` 原样进 argv;`signal` / `timeout` 任一命中即 kill)。结果带 `command/args/code/stdout/stderr/killed/duration_ms` |
| `register_command(name, handler, opts)` | 斜杠命令;handler 收 `(args, ctx)`。**重名不覆盖**:变成 `name:1` / `name:2`。`getArgumentCompletions` **会收下并出现在 `get_commands()` 里**,但 TUI 补全尚未消费它 |
| `register_shortcut(key, handler, opts)` | 快捷键(key 用 textual 写法)。坏键名只提示,不让启动失败 |
| `get_commands()` | 命令清单。形状:`{name, description, source, sourceInfo}` + qi 的 `has_argument_completions` |
| `register_flag(name, opts)` / `get_flag(name)` | CLI 旗标。**值走 `--ext name=value`**。装载后对账:未注册名 / 字符串旗标缺值 → 退出码 2 |
| `send_message(msg, opts)` | 注入消息(**进 LLM 上下文**)。收字符串或 dict 形状 `{customType, content, display, details}`;`deliver_as` 三档(`steer`/`follow_up`/`next_turn`,驼峰别名 `followUp`/`nextTurn` 也认);`trigger_turn=True` = 空闲时也开一轮(前端领 `take_turn_request()`) |
| `send_user_message(content, opts)` | 同上,落盘标成"用户说的"(`injected_by`)。`trigger_turn` 缺省 **True** |
| `append_entry(customType, data)` | 落盘扩展自定义 entry(**不进 LLM 上下文**)。写口**只此一个**,章由宿主盖 |
| `set_session_name(name)` / `get_session_name()` / `set_label(entryId, label)` | 会话名与 entry label(`set_label` 落成 `{type: "label", targetId, label}`) |
| `set_model(model)` / `get_thinking_level()` / `set_thinking_level(level)` | 模型与思考级别。`set_model` 收 `"provider/model"` / `ModelView` / `{provider, id}`,返回 bool。**唯一入口是 runtime**(UI 与扩展共用,事件只发一次) |
| `register_provider(name, cfg)` / `unregister_provider(name)` | 动态注册/注销 provider。**只改内存,不写 `models.json`**;覆盖同名会记 note |
| `register_message_renderer` / `register_entry_renderer` / `register_markdown_transformer` | 渲染回调登记(`RendererRegistry`)。契约见 §8 |
| `events.on(channel, handler)` / `events.emit(channel, data)` | **扩展之间**的消息频道。`on` **返回退订函数**。与宿主事件是两套 API、同一总线对象 |
| `run_agent(spec, task, opts)` | 受管的**子运行**(不碰当前会话)。`spec = {system_prompt(必填), tools?(缺省**继承父**), model?, name?}` |
| `provides_config(kind, types)` | 消费型配置声明(见 §10) |
| `register_resolver(kind, fn)` / `resolve_tools(kind, scope=…)` | 配置种类 → 工具的一般机制(见 §10)。没人提供 → `[]` |
| `register_cli_command(name, handler)` | `qi <name> …` 子命令(选项表静态,各扩展自己解析 argv) |

## 6. 上下文(`ctx`)

`ctx` 的 snake_case 是正式名,驼峰写法是别名(`ctx.hasUI` / `ctx.isIdle()` /
`ctx.getSystemPrompt()` / `ctx.newSession()` …),两套指向同一份实现。

| 成员 | 说明 |
| --- | --- |
| `ctx.ui` | **总是存在**(见 §8) |
| `ctx.mode` | `tui` / `rpc` / `json` / `print`。组件层 UI 用 `ctx.mode == "tui"` 门控 |
| `ctx.cwd` | 工作目录 |
| `ctx.model` | `ModelView` —— **`str` 子类**:`ctx.model == "provider/model"` 与 `ctx.model.id` / `.contextWindow` **同时**成立 |
| `ctx.thinkingLevel` / `ctx.thinking_level` | 当前思考级别 |
| `ctx.hasUI` / `ctx.has_ui` | 是否有人在看(TUI/web=真,`-p`=假) |
| `ctx.isProjectTrusted()` | 信任状态 |
| `ctx.signal` | **协作式中断信号** —— 扩展做异步时必须传它(Esc 才能取消 `fetch`/子进程) |
| `ctx.abort()` | 中断当前回合(`ctx.signal` 是**信号对象**,这里的是**方法** —— 两者名字接近,别取错;`ctx` 在两个位置上形状不同,见 §6.1) |
| `ctx.session_manager` | 当前会话的**只读**视图。读:`entries`/`custom_entries`/`get_entry`/`get_leaf_id`/`get_leaf_entry`/`get_branch`/`build_context_entries`/`get_header`/`get_tree`/`get_label`/`get_cwd`/`get_session_dir`/`get_session_file`/`get_session_name`;写只走 `api.append_entry`(以及 `api.set_label` / `api.set_session_name`) |
| `ctx.model_registry` | `ModelRegistryView`:`get_all` / `get_available` / `find` / `has_configured_auth` / `get_provider_display_name` / `register_provider` / `unregister_provider` |
| `ctx.scoped_models` / `scopedModels` | `--models` / `enabledModels` 圈定的模型(空 = 不限制) |
| `ctx.abort()` / `isIdle()` / `hasPendingMessages()` / `shutdown()` | 运行控制。`shutdown()` 需要前端注入退出实现,否则记一条 note |
| `ctx.compact(opts)` / `getContextUsage()` | 压缩(**触发即返回**,不等完成)/ 上下文占用 `{tokens, contextWindow, percent}` |
| `ctx.getSystemPrompt()` / `getSystemPromptOptions()` | 读当前提示词(**优先给本回合真正建好的那一份**,含 `before_agent_start` 的改写) |
| `ctx.waitForIdle()` | 等当前回合跑完 |
| `ctx.newSession()` / `fork(entryId)` / `navigateTree(targetId)` / `switchSession(path)` / `reload()` | 会话操作。前四个都过**可取消**的 `session_before_*` 闸门,返回 `{cancelled}`。`reload()` 重扫技能/提示词并重发 `resources_discover` + `session_start(reason="reload")`,**不重新 import 扩展模块**(Python 不保证安全重载) |

### 6.1 `ctx` 有两个形状

同一个 `ctx`,在两条路径上不是同一个类 —— 而它们**字段名不同**,只认其中一个的话,
另一个形状上会静默拿到 `None`(不报错、不失效地"看起来在跑")。

| 路径 | 类型 | 中断信号 | 工作目录 | 问人 | 信任 |
| --- | --- | --- | --- | --- | --- |
| **handler**(`api.on(...)` / 命令) | `ExtensionContext` | `ctx.signal` | `ctx.cwd` | `ctx.ui.confirm`(async) | `ctx.project_trusted` |
| **工具**(`Tool.execute(args, ctx)` 的第 2 个参数) | `ToolContext` | `ctx.abort` | `ctx.workdir` | `ctx.ui.confirm`(async) | `ctx.project_trusted` |

三条由此而来的写法规矩(都是踩过的):

1. **取信号要两边都认**:`ctx.abort` 在 handler 侧是**方法**(`ctx.abort()`),`ctx.signal`
   在工具侧不存在。判据落在形状上(可调用的一律不认),不要只 `getattr` 一个名字 ——
   两次写错的版本都不抛错,症状只是"Esc 杀不掉子 agent"。
2. **取目录同理**:`ctx.cwd` / `ctx.workdir` 各认一半。
3. **项目级 prompt / 角色正文这类"仓库控制的内容"必须先过 `project_trusted`** ——
   两条路径都要。扩展工具想设这道门就得读 `ctx.project_trusted`。

参考实现:`extensions/qi-agents/qi_agents/subagent.py` 的 `_abort_signal()` / `_cwd_of()`。

## 7. 写一个工具

工具 = 一个 `Tool`(或等价 dict),注册后进入模型的工具清单(下一轮生效)。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 工具名(模型看到的名字;同一 catalog 内唯一) |
| `description` | 是 | 进 tool schema 的说明(模型"要不要调它"时看的就是这段,可以长一些) |
| `parameters` | 是 | 参数的 JSON Schema |
| `execute` | 是 | `async def execute(args, ctx) -> str \| ToolOutcome`。也收 `(tool_call_id, params, signal, on_update, ctx)` 那种五参形状(按形参个数适配) |
| `prompt_snippet` | 否 | 系统提示词「可用工具」那一行的摘要(省略则回落到 `description`) |
| `prompt_guidelines` | 否 | 该工具**被启用时**追加的指南 bullet;**必须自带工具名**(指南是平铺追加的) |
| `label` | 否 | 界面显示名(回落 `name`) |
| `prepare_arguments` | 否 | 拿到**原始**参数后、执行前的整理钩子;返回的 dict 才是执行用的那一份(也进 `tool_call` 事件的 `input`) |
| `render_call` / `render_result` / `render_shell` | 否 | TUI 自己画工具卡片(见 §8) |
| `execution_mode` | 否 | `"parallel"` = 可与同批其它 parallel 工具并发(见 §7.1) |
| `constrained_sampling` | 否 | **收下但忽略**(注册时记一条 note);逐工具受限采样要 provider 配合,liteLLM 没有可移植的对应物 |

```python
from qi_agent.extensions import Tool, ToolError, ToolOutcome

async def read_issue(args, ctx):
    if not args.get("id"):
        raise ToolError("缺少 id")            # → 以 error 结果返回给模型,不中断会话
    ...
    return ToolOutcome(                        # 结构化结果(可选)
        result="…模型可见的文本…",
        details={"issue": {...}},              # 给前端渲染用;**不进** LLM 上下文
        exit_code=0,
    )

api.register_tool(Tool(
    name="read_issue",
    description="按 id 读一个 issue。",
    parameters={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    execute=read_issue,
    prompt_snippet="读 issue",
    prompt_guidelines=["需要 issue 内容时用 read_issue,不要猜。"],
))
```

- **错误**:抛 `ToolError`(或返回 `ToolOutcome(status="error", …)`)→ 变成模型可读的 error 结果,回合继续。
- **进度**:`ctx.on_update(partial)` 推 `tool_execution_update` 事件(TUI 会更新卡片)。
- **`ctx`**:`workdir`(会话目录;`ctx.guard(path)` 能校验"不越界")、`abort`、`ui`、`project_trusted`、`tool_call_id`、`session_env`。
- **动态注册**:装载后、事件里、命令里都能 `register_tool`,**下一轮**就能调。

### 7.1 并发:`executionMode`

工具的 `executionMode` 声明 `"sequential" | "parallel"`(默认顺序),语义如下:

| 规则 | 内容 |
| --- | --- |
| 默认 | 不声明 = 顺序 |
| 成批 | **相邻**的 parallel 工具并成一批并发跑(`asyncio.gather`) |
| 打断 | 一个顺序工具把批次**切断** —— 顺序工具永远不会与并行工具重叠(否则“我声明了顺序”就没意义) |
| 并发安全 | **由声明方负责**:写 `"parallel"` 等于说“我的实现是并发安全的”。qi 不做文件变更队列 |
| 顺序不变 | 事件与上下文顺序仍然确定:先把整批的 `tool_execution_start` 发完,再按**声明序**发 `tool_execution_end` / `tool_result` / 消息 |
| 没有 session 级默认 | 只认**逐工具**声明 |

内部 `AgentEvent(kind="tool_start"/"tool_end")` **总是带 `tool_call_id`** —— 并发时前端工具卡片与落盘靠它把 start/end 配对;想自己配对的事件订阅者也照这个键来。

## 8. 终端 UI 与渲染

扩展能问用户、改状态、往界面里塞组件,以及换掉工具卡片 / 消息 / markdown 的渲染 —— 这些都走
`ctx.ui` 与渲染回调,**契约、方法清单、无后端行为与例子在 [终端 UI 组件](tui.md)**。

一句话:**`ctx.ui` 总是存在**,没有前端时按调用方给的 `default` 回答(`notify` 落进启动提示),
所以"交互"永远不会把回合挂住。

## 9. 配置种类与作用域

扩展之间共享同一份**目录语义**时,不必各自实现一遍目录遍历:
**定义 scope 的是目录的主家,读配置的是配置种类提供者。**

```text
定义 scope 的扩展:  扫 <scope 目录> → 得到一个 scope(名字 + 目录 + 信任状态)
配置种类提供者:     按 scope 自取 —— "给我一个 scope,我读我要的那个文件"
```

- **目录遍历只一处**(定义 scope 的那个扩展),其余扩展只实现"读自己那个文件";
- 声明方式:`api.provides_config(kind, types)`;没人提供该种类时**整个种类不装载**(静默跳过);
- core 侧的一般机制:`api.register_resolver(kind, fn)` / `api.resolve_tools(kind, scope=…)`,
  没人提供 → `[]`;
- 边界:**只在 scope 目录内**。全局(`~/.qi/agent/`)与项目(`<项目>/.qi/`)那两层的声明表沿用
  按作用域分层的旧规则(它们不是任何 scope 私有的)。

## 10. 依赖契约

**安装目标:统一装进 qi 自己的解释器环境。** 目标是 qi 所在的那个 venv,**不是**用户 cwd 的项目 venv(否则就是那个经典失败:"我在项目里 pip install 了,qi 就是 import 不到")。装法就是 `qi install <来源>` —— 它固定往 `sys.executable` 对应的环境装(uv tool 环境里自动改用 uv 的安装器)。

- **公开 import 白名单**:只能 import §3 列的那些名字。别的 `qi_agent.*` 模块算内部实现。
- **扩展不得把 `qi-coding-agent` 写进 `dependencies`**(否则 pip 会在解析时把 qi 自己降级 —— 宿主被自己的扩展踢掉)。
  装载时会读 pip 通道的 `requires()`(目录通道读 PEP 723 的 `# /// script` 声明),命中就**报告**(带版本满足判定与修复动作)。**只报告不拒绝** —— 声明本身不危险,危险的是被 pip 解成一棵冲突的树;拒载会让本来能跑的扩展直接不可用。判定按 PEP 503 归一(`Qi.Coding.Agent` / `qi_coding_agent` 都算),但 `qi-coding-agent-extra` 不算。
  另一条会单独报出来:**旧名 `qi-agent`** —— 那个名字在 PyPI 上属于别的项目,写进依赖不是"多写一条",
  而是会真的装一个不相干的包(还可能与 qi 的 import 包在 `site-packages` 里撞车)。
- **接受的代价**:所有扩展 + 宿主共用一棵解析树 → **版本冲突无处躲**。三层缓解:① 装载时比对已装版本,**不一致就报告**(不静默);② 冲突就拆成独立进程(自己的环境);③ 重依赖优先走外部进程/服务。
- **uv tool / pipx 的坑**:uv 文档原话 —— tool 环境 "may be upgraded via `uv tool upgrade`, or **re-created entirely** via subsequent `uv tool install`",所以 pip 装进去的扩展**会被重建抹掉**。规矩:声明永远在 `settings.packages`,重建后用 **`qi sync`** 一条命令补回来(它按声明对账,自动走 uv 的安装器);也可以用 `qi doctor` / `qi list` **发现**它丢了(并给出装法)。或者这类用户直接用 `uv tool install qi-coding-agent --with <包>`(写进 uv 的托管依赖,升级不丢 —— 这条更省事,推荐)。
- **宿主环境只读时**(系统 Python / Homebrew 管理的解释器):pip 会自己报错;出路是 `uv tool install qi-coding-agent --with <ext>`,或换一个可写的安装方式。qi 不检测这件事 —— 它不调 pip,所以也不该假装知道装不装得进。
- **`qi install <来源> [-l]`** 帮你走一步:调安装器(uv tool 环境里**自动改用 uv 的安装器**,目标仍是 qi 自己的解释器)+ 写进 `settings.packages`(带 `-l` 写项目)。它每次都先把要跑的命令打出来。`qi remove` / `uninstall` 从指定作用域删声明,若没有别的作用域还声明它,就连包一起卸(对齐 pi)。详见 [packages.md](packages.md) 与 [cli.md](cli.md)。

## 11. 细节与已知限制

已经全齐的面:事件 36 项、`api.*` 方法、`ctx` 成员、**`ctx.ui` 的全部方法**
(含组件层的 `set_widget` / `custom` / `set_footer` / `set_header` /
`set_editor_component` / `add_autocomplete_provider` / `on_terminal_input`)、
**渲染回调与工具渲染钩子(TUI 已消费)**、命令参数补全、`/resume` 选择器过闸门。

| 项 | 契约 |
| --- | --- |
| `ctx.ui.set_editor_component` 的**边界** | 要求组件是 **`TextArea`(或子类)**;不是则记 note 并拒绝。qi 的编辑器是承重的(历史环 / kill-ring / 补全 / 光标定位),把边界画在这里,15 处调用点就不必各自做兼容 —— 写一个 `TextArea` 子类仍然能做 vim 式模态编辑(拦截按键、自己改文本)。契约见 §8 |
| `ctx.ui.add_autocomplete_provider` 的**形状** | `factory(text, cursor_offset) -> list[str \| {value, label, description}]`;**返回退订函数**;与命令参数补全**同一套元素形状**,两种补全只学一次 |
| `ctx.ui.on_terminal_input` 的 `data` 改写 | 只支持 `{"consume": True}`(吃掉按键);不支持"换成另一个键" —— 需要改写按键的场景用 `set_editor_component` 接管编辑器(Textual 的 `events.Key` 不是为改写设计的) |
| `register_command` 的 `get_argument_completions` / `add_autocomplete_provider` 返回 **async** | **支持**(只是晚一拍):结果回来时若文本与光标未动就回填,动了就丢掉(补全主路径是同步的,async 走 worker + 一次重刷) |
| `constrainedSampling` | **收下但忽略**(注册时记一条 note):逐工具受限采样要 provider 配合(JSON schema / grammar),litellm 没有可移植的对应物。**收而不报才是最坏的**:扩展会以为参数被约束住了 |
| `user_bash` | 只在 TUI 发(无头模式没有 `!` 命令这回事) |
| `agent_settled` | 保守近似:qi 没有回合级重试/重压机制;provider 级重试在客户端内部,那时 `stream()` 还没返回 |
| 事件 payload 的**结构** | 键名双双可用(snake_case + 驼峰别名,见 §4),但**结构不假造**:如 `turn_end` 给 `{turn_index, text, tool_calls}` —— payload 面向本仓自己的前端词汇 |
| `deliver_as` 取值 | `follow_up` / `next_turn`(驼峰别名也收) |
| 设置类 entry 的键名 | `model_id`(本仓 entry 一律 snake_case:`custom_type` / `agent_id` / `duration_ms`) |
| `register_flag` 的取值通道 | 只能 `--ext name=value`(typer 的选项表静态,放宽未知长旗标会把用户的笔误变成一句 prompt) |
| 入口文件名 | 固定 `extension.py` |
| `add_route` / `add_static` | 未实现(HTTP 宿主的挂载点,不属现行扩展面) |

**另外几件已落地**:`renderShell`(见 §9)、`executionMode`(见 §7.1)、payload 的 `type` 字段与
驼峰键别名、async 参数/补全提供者、**模型/级别的落盘 + 环境变量 + 续会话还原**(见
[session-format.md §6.1](session-format.md) —— 换模型是界面状态,不进上下文,
所以 agent 只能靠 `QI_*` 环境变量自查)。
