# 终端 UI 组件

扩展往界面里加东西的三种集成点。用哪个取决于你要的是"问一句话"还是"接管一块屏幕"。

| 集成点 | 用在哪 | 入口 |
| --- | --- | --- |
| **数据层** | 问用户、改状态、读外观 | `ctx.ui.confirm` / `select` / `input` / `editor` / `notify` / `set_status` … |
| **组件层**(TUI-only) | 往界面里塞自己的组件 | `ctx.ui.custom` / `set_widget` / `set_footer` / `set_header` / `set_editor_component` / `add_autocomplete_provider` / `on_terminal_input` |
| **渲染回调** | 换掉工具卡片 / 消息 / markdown 的渲染 | `register_entry_renderer` / `register_message_renderer` / `register_markdown_transformer` / `Tool.render_call` / `render_result` / `render_shell` |

`ctx.ui` **总是存在**:没有前端时按调用方给的 `default` 回答,`notify` 落进启动提示,永远不会挂住。

## 数据层

```python
async def confirm(message, *, title=None, default=False) -> bool
async def select(message, options, *, title=None, default=None) -> str | None
async def input(message, *, title=None, default=None, secret=False) -> str | None
async def editor(prefill="", *, title=None) -> str | None      # 多行
async def notify(message, *, level="info") -> None
# 状态与外观
async def set_status(key, text) / set_title(text)
await set_working_message(text) / set_working_visible(bool)
await set_working_indicator({"frames": [...], "intervalMs": int})
await set_hidden_thinking_label(label)
await get_tools_expanded() / set_tools_expanded(bool)
await get_all_themes() / get_theme(name) / set_theme(theme) / theme
await paste_to_editor / set_editor_text / get_editor_text
```

**两条硬规则**:

1. **没有后端时每个问答方法返回调用方给的 `default`** —— "交互"退化成"按事先声明好的策略走",
   而**永远不会挂住**。`confirm` 默认 **False**(拒绝是安全边);`select` / `input` / `editor` 默认 None(取消)。
   默认值由**调用方**给:只有它知道"这里拒绝安全还是继续安全"。
2. `notify` 没有后端时**落进 `notes`**,不丢弃。

后端自己抛异常也走 `default`(记一条 note):交互是辅助手段,不该成为新的失败点。
阻塞式问答还会发 `ui_prompt_start` / `ui_prompt_end`(`{reason, kind, title}`),扩展因此能知道"有人在等交互"。

## 组件层

- `set_widget(fn_or_lines, placement=…)` —— 挂进编辑器上方 / 下方的两个槽位。
- `custom(fn)` —— 放进模态:签名叫 `fn(ctx, done)` / `fn(ctx)` / `fn()`,返回组件;`done(value)` 结束并返回。
  非 TUI 前端下返回 `None`。
- `set_footer(factory)` / `set_header(factory)` —— 内置的只藏起来不卸载,撤回立即恢复。
- `set_editor_component(factory)` —— 换掉 `#editor`。**必须返回 `TextArea`(或子类)**,否则记 note 并拒绝:
  qi 的历史环 / kill-ring / 补全面板 / 光标定位都建立在这个接口上。写个 `TextArea` 子类就能做 vim 式模态编辑。
- `add_autocomplete_provider(factory)` —— `factory(text, cursor_offset) -> list[str | {value, label, description}]`,
  与命令参数补全**同一套元素形状**;**返回退订函数**;支持 async(晚一拍回填,文本动过就丢弃)。
- `on_terminal_input(handler)` —— 只支持 `{"consume": True}`(吃掉按键),不支持"换成另一个键":
  Textual 的 `events.Key` 不是为改写设计的,要改写就用 `set_editor_component` 接管编辑器。返回退订函数。

组件是 **textual widget**;`ctx.mode`(`tui` / `rpc` / `json` / `print`)是组件层的门控口
(`if ctx.mode == "tui":`)。**按形参个数适配**:声明三个形参(`x, theme, context`)会拿到
`(payload, ctx, None)`,不报错也不静默失效。

## 渲染回调

回调直接返回组件(或 markdown 字符串);**抛异常 / 返回 `None` → 退回内置渲染** + 在界面上说一句 ——
工具卡片与消息绝不会因为一个坏回调而消失。

| 钩子 | 签名 | 位置 |
| --- | --- | --- |
| `register_entry_renderer(customType, fn)` | `fn(entry, ctx) -> Widget \| None` | 回放遇到该 `custom_type` 时;未知 custom entry 会以 `[type] {json}` 原样显示,**绝不丢弃** |
| `register_message_renderer(customType, fn)` | `fn(entry, ctx) -> Widget \| None` | 同上,用于带 `custom_type` 的 **message** entry(即 `api.send_message` 以 dict 形状写的那些) |
| `register_markdown_transformer(fn)` | `fn(markdown, ctx) -> str` | user / assistant 的**最终文本**渲染前链式改写(流式增量不逐个改 —— 半截 markdown 改了更糟) |
| `Tool.render_call` | `fn(args, ctx) -> Widget \| None` | `tool_start` 时整个替换默认卡片 |
| `Tool.render_result` | `fn(result, ctx) -> Widget \| None` | `tool_end` 时替换结果组件 |
| `Tool.render_shell` | `"default"` / `"self"` | `"default"` 给组件套上工具卡片外壳(状态底色 + `(1,1)` 内边距);`"self"` = 自己画框 |

工具块可以是内置 `ToolBlock` 也可以是 `ExtensionToolBlock` —— 两者都实现 `set_state` / `set_output`,
所以展开 / 复制 / 会话回放不必分叉。

## 例:自定义组件 + 工具卡片

```python
from qi_agent.extensions import Tool
from textual.widgets import Static

def register(api):
    # 1) 往编辑器上方挂一行字
    api.on("session_start", lambda payload, ctx: ctx.ui.set_widget(lambda ctx: Static("my-ext 在看着"), placement="above"))

    # 2) 一个模态:问一句话,把结果当返回
    def ask_lot(ctx, done):
        async def pick(value=None):
            done(value)
        return ctx.ui.custom(lambda ctx: Static("…"))     # 真实组件按你的界面写

    # 3) 工具用自己的卡片渲染
    async def run(args, ctx):
        return "ok"

    api.register_tool(Tool(
        name="my_tool", description="示例", parameters={"type": "object", "properties": {}},
        execute=run,
        render_call=lambda args, ctx: Static(f"my_tool {args}"),
        render_result=lambda result, ctx: Static(str(result)),
    ))
```

配色请走主题的语义键(别写死 `#rrggbb`),否则换主题时你的组件不会跟着变 —— 见 [themes.md](themes.md)。

## 与其它页面的关系

- 扩展整体的写法(工具、事件、命令、旗标、依赖)见 [extensions.md](extensions.md)。
- 键位与编辑器行为见 [keybindings.md](keybindings.md)。
- 实现细节(Textual 的 priority / 焦点坑)见 [design/internals.md](../design/internals.md)。
