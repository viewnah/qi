# 主题

qi 的 TUI 只有**两份内置调色板**:`dark` 与 `light`,就是 `src/qi_agent/themes/` 下的两个 JSON
(`dark.json` / `light.json`)—— 键名与语义固定成一套,要调配色直接改这两个文件。

> **qi 不支持自定义主题文件**:只有 `dark` / `light` 两个内置值,未知主题名会**报错**,
> 不会去找用户目录里的 JSON。见 §5。

**事实源:`src/qi_agent/theme.py` + `src/qi_agent/themes/{dark,light}.json`。**

## 1. 选哪一份

优先级(高 → 低):

| 顺序 | 来源 | 取值 |
| --- | --- | --- |
| 1 | 环境变量 `QI_THEME` | `dark` / `light` / `auto` |
| 2 | `settings.json` 的 `theme` | 同上 |
| 3 | **探测终端背景色**(OSC 11) | 亮背景 → `light`,暗背景 → `dark` |
| 4 | 兜底 | `dark` |

`auto`(以及未设置)走第 3 步。探测是**查询终端**(OSC 11,超时 150ms),不是猜:

- 终端支持 OSC 11 且回了 `#rrggbb` → 按亮度(`> 0.5` 视为亮)选 `light` / `dark`;
- **不支持 / 超时 / 回复解析不了 → 落 `dark`**,不报错、不卡住。

探测到的那个背景色会一并带在调色板上(`Palette.terminal_bg`):Textual 的 inline 区域必须有不透明
底色,把它设成终端背景色才能在视觉上"不填色"(qi 不画底色)。

## 2. 调色板文件长什么样

```jsonc
{
  "$schema": "…/theme-schema.json",
  "name": "dark",
  "vars":   { "cyan": "#00d7ff", "blue": "#5f87ff", … },      // 命名色(dark 16 项 / light 15 项)
  "colors": { "accent": "accent", "border": "blue", … },       // 语义键 → 变量名 或 #hex(55 项)
  "export": { "pageBg": "#18181e", "cardBg": "#1e1e24", … }
}
```

解析规则(`load_palette`):

- `colors` 的值**不以 `#` 开头**时,去 `vars` 里查同名变量;查不到就原样当颜色用。
- **全部转小写**。调色板 JSON 里大小写是混用的(`#2D2838` / `#343841`),而 Textual 的
  `Color.hex` 会**原样保留**传入字符串的大小写 —— 所以归一必须在 `load_palette` 里做。
- `export.pageBg` 缺省为 `#18181e`。
- 取值键(语义色)拼错时**直接报错**(`Palette.hex()`),不会静默变成白色 —— 拼错颜色名最怕
  的就是"看着像没生效"。

## 3. 同一份调色板喂给三个渲染层

`theme.py` 从一份 `Palette` 导出三个适配对象:

| 导出 | 给谁用 |
| --- | --- |
| `rich_theme(palette)` | Rich(非 TUI 场景的输出) |
| `syntax_theme(palette)` | 代码高亮 |
| `textual_theme(palette)` | Textual(TUI 本体) |

所以**改主题 = 改那 55 个语义键**,三个渲染层同时跟着变;不需要分别调。

## 4. 主题之外:`theme.py` 里还住着 footer 格式化

同一个文件里还有几个纯格式化函数(`format_tokens` / `shorten_home` / `format_cwd_line` /
`git_branch`),它们负责状态行里 `~/proj (main)` 这种显示。
它们与调色板无关,只是碰巧在同一个模块。

## 5. 不能做的事

| 想法 | 现状 |
| --- | --- |
| 自定义主题名 | **不支持**。`theme` 只认 `dark` / `light` / `auto`(`auto` 与未设等价);其他值会走"未知 → 退回 dark" |
| 放一份自己的 JSON 到 `~/.qi/agent/themes/` | **不支持**,没有用户主题目录 |
| 运行中切换主题 | 交互式前端可切(`ctrl+t` 之外见 [usage.md](usage.md));`QI_THEME` / `settings.theme` 在启动时决定初始值 |

要加自定义主题,需要的是:主题目录的发现规则 + `ThemeError` 之外的加载路径。现在没有,所以本文不描述
一个不存在的机制。
