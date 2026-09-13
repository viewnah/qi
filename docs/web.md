# Web 能力设计(宿主 + UI 插件)

> 状态:设计定稿(v2 实现)。相关文档:[plugins.md](plugins.md)(插件机制)、[tools.md](tools.md)(ToolCatalog)。参考:pi 的 RPC 模式与 pi-web(独立 Web 应用)的取舍。

## 1. 结论与形态

**宿主在框架,UI 是插件**:

| 层 | 内容 | 归属 |
| --- | --- | --- |
| HTTP 宿主 | uvicorn、基础 `/api`(会话/发消息)、SSE 事件流、静态资源托管、安全(回环/密码/信任) | **框架(v2)** |
| UI 前端 | 页面资源(HTML/JS/CSS:聊天 + 分派可视化) | **插件**(pip 包,随包分发) |

理由:web 是 `runtime.stream()` 的**第三个 consumer**(CLI / TUI / HTTP,同进程零协议),框架内置宿主即可免掉 pi-web 那套子进程 RPC 桥;而前端属于"内容/可选能力",插件化与"框架零内置内容"哲学一致,核心不养前端。

## 2. 方案取舍(为何不选外置 / 纯内置)

| | 外置应用(pi-web 模式) | 纯内置 UI | **宿主 + UI 插件(采用)** |
| --- | --- | --- | --- |
| 进程/协议 | 子进程 + JSONL-RPC | 同进程 | 同进程,零协议 |
| 核心养前端 | 不养 | 要养 | 不养(官方出参考 UI 插件) |
| UI 升级/换皮 | 独立升级 | 跟框架发布 | pip 升级/换插件 |
| 复杂度 | 两套部署 | 最小 | UI 与宿主 `/api` 需契约版本化 |

## 3. HTTP 宿主(v2)

```
qi web                          # 起本地服务;默认 127.0.0.1 + 端口配置 + 可选密码
▼
FastAPI/Starlette + uvicorn     # 复用 hikqin server.py 思路
├── /api/sessions · /api/agent/<id>     # REST:查会话 / 发消息
├── /api/agent/<id>/events(SSE)         # 事件流 = stream() 直连(同 CLI/TUI 的 consumer)
├── /api/health
└── 静态托管:已安装 UI 插件的页面资源
```

- 应用级配置(host/port/password_env)属应用配置主题(v2 待定;模型/凭证已在 `models.json` + `auth.json`)
- 安全默认:仅 `127.0.0.1`;远程需显式 hostname + 密码(参考 pi-web 的警告语义:暴露可执行高权限操作的 agent)
- **API 契约版本化**:`/api` 形状是 UI 插件的依赖,语义版本化(v1/v2),宿主保证兼容,防止契约漂移
- 不装 UI 插件:`qi web` 只提供 API/健康检查

## 4. UI 插件形态

```python
# qi-web-ui 插件的 register()
def register(registry):
    registry.add_static("/", package_dir / "ui")   # 前端资源挂到宿主
    # 基础 /api + SSE 复用宿主内置;需要自定义后端才 add_route
```

```toml
# 插件 pyproject.toml
[tool.setuptools.package-data]
qi_web_ui = ["ui/**/*"]
```

- 前端技术栈:**已推翻“零构建轻量”**——改为 React + Vite + TypeScript,构建产物作为静态资源分发(决策与理由见 §8)。原判断“避免构建链进 pip 包”仍成立:构建产物进 wheel,源码 `web/` 不进。
- 深度定制(如 DB 管理台代理):插件 `add_route` 扩展(见 plugins.md),需安全审计
- 官方维护一个参考 UI 插件,与框架 `/api` 契约同步演进

## 5. 与其他主题的关系

- **插件机制**:web UI 是消费型能力之一(`add_static/add_route` 与 plugins.md 的 register 扩展);安全/信任规则同 plugins.md §5
- **headless RPC**(JSONL over stdio,对齐 pi `--mode rpc`):内置宿主让 Web 不需要它;它留给**外部客户端**(IDE/独立工具),作为独立未来项,不在 v2 web 范围内
- 数据共享:前端读会话 JSONL / `.qi` 配置渲染历史与管理视图(与 agent 进程解耦),格式已定

## 6. 决策记录

| 决策 | 结论 |
| --- | --- |
| 形态 | 宿主在框架(v2)+ UI 插件化,同进程直连 stream(),不做外置 RPC 桥 |
| 宿主 | `qi web`:uvicorn + 基础 REST + SSE + 静态托管;默认回环 + 可选密码 |
| 契约 | `/api` 语义版本化,防 UI 插件契约漂移 |
| UI | ~~零构建轻量前端~~ → **React + Vite + TS**,构建产物进 wheel(源码不进);深度定制走 add_route。见 §8 |
| 视觉基线 | 参考 **DeepSeek Harness**:取其实测的 **Web 面灰阶骨架** + TUI 雾蓝家族的语义色。见 §8 |
| RPC | 不进 v2 web;留给外部客户端(IDE)的未来项 |

## 7. 待定(并入对应主题)

- `[web]` 配置字段与端口默认(应用配置主题)
- UI 插件首发范围(会话聊天 + 分派可视化 + agent/配置查看?)
- SSE vs WebSocket(事件量大时的取舍,v2 实现时定)
- 语义色策略:是否保留有彩色状态色(dsh web 实测为纯灰阶,无状态色;qi 必须补)。见 §12
- 桌面端:Electron(按 dsh 做法,私有 Host + 自定义协议,不开 loopback)vs Tauri。影响传输层设计,需在 P1 前定

## 8. 视觉基线(决策记录)

> 状态:已定稿(2026-09)。取代更早两条只存在于讨论中的基线(零构建 vanilla、Claude Code 暖橙);
> 后者从未落库,此处仅作追溯留痕。

### 8.1 决策

参考 **DeepSeek Harness**。但必须记录一个关键区分:**dsh 有两个面,视觉系统并不相同**,不能笼统说“抄 dsh”:

| 面 | 视觉系统 | 来源 | 本项目的用法 |
| --- | --- | --- | --- |
| dsh **Web UI** | **灰阶**(画布 `#F5F6F7` / 面 `#FFFFFF` / 墨 `#0F1115`) | 官方截图**实测**(见 §10) | **骨架与文字色取此** |
| dsh **TUI** | Gentle Mist Blue **雾蓝暖色**(`#F6F3ED` 暖白纸 + 雾蓝) | `dsh-TUI/src/theme.ts` | 仅取**语义色**(状态/身份/diff) |

理由:qi-web 是网页,其骨架应参照 dsh 的网页面;而 dsh 的网页面**没有状态色**(实测最大 chroma = 12,见 §10),
qi 又必须表达 success/error/warning、diff 增删、agent 身份与工具类别,故语义色另取 dsh TUI 的雾蓝家族并做 AA 校准。

### 8.2 采纳

| 项 | 采纳内容 |
| --- | --- |
| 骨架 | 浅灰画布 + 纯白浮面 + 近黑墨 + 近乎不可见的发丝线 |
| 命名法 | 颜色走**语义键**(`accent`/`text-inactive`/`diff-added-word`),禁止调色板命名(`blue500`);且**来源命名要改成语义命名**(dsh 正把 `claude`/`clawd_body` 改名,qi 直接用语义名,不重复这段历史) |
| 令牌纪律 | `tokens.css` 是唯一色值来源;组件出现 `#rrggbb` 即回归失败(§11 第 1 条,已可自动拦截) |
| 工具卡片 | **状态标题 + 内收正文**,折叠只按行数(不用 `⏺` 子弹行——那是 Claude Code 的形态) |
| 双视图 | 同一事件族驱动**对话视图**与**轨迹视图**两个 target,各自持有 display model、互不导入 |
| 重连 | 逻辑 seq 区间校验 + 每代 opening snapshot 原子替换 + `page()` 修 gap;不用 `Last-Event-ID` 续传 |
| 主题容错 | 坏主题跳过整文件、未知键跳过并告警、主题名不得越出主题目录、一个坏主题不阻塞启动 |

### 8.3 不采纳

| 项 | 理由 |
| --- | --- |
| `--dsw-*` 前缀 | 抄纪律不抄前缀;qi 用 `--qi-*` |
| dsh 的插件底座(Cordis)与 profile/组合包 | qi 的插件机制是 pip entry point + 本地目录双通道(docs/plugins.md),不同构;只借“能力可替换”的概念,不搬实现 |
| dsh TUI 的 `dark-ansi` 第三套色板 | 那是终端 16 色的能力降级,浏览器无此约束 |
| 品牌资产(字标、logo、成套主题、吉祥物色 `clawd_*`) | 色值可借鉴,品牌资产不复刻 |
| 窗口 chrome 的 `#C0C0C0` 竖条 | 那是截图里的宿主窗口标题栏,不是 web UI 的一部分 |

## 9. 对比度校准记录

### 9.1 阈值政策

| 类别 | 阈值 | 说明 |
| --- | --- | --- |
| 正文 / 次要文字 / 语义色 / 强调色 | **≥ 4.5:1** | AA 正文 |
| 装饰与身份标识(agent 八色、工具点) | **≥ 3:1** | WCAG 非文本对比度 |
| 发丝线 `--qi-hairline` | **不设阈值** | 纯分隔用途;承担可交互边界时必须改用 `--qi-border`(≥3:1) |

判定面**取最紧的那个**:同一前景色会同时出现在画布(`--qi-bg`)与卡片面(`--qi-surface`)上,
按两者中对比度更低者判定(工具行就在卡片面里)。

### 9.2 为什么必须校准

dsh TUI 的浅色板是**给终端用的前景色**——终端自带底色,色值只需彼此可区分;
搬到网页底色上实测后大面积不达 AA。实测(校准前,画布 `#F6F3ED`):

| 键 | dsh TUI 原值 | 实测 | 现取值(light) | 现比值 |
| --- | --- | --- | --- | --- |
| `text-inactive` | `#8991A0` | 2.86 ✗ | `#626773` | 4.52 |
| `text-subtle` | `#A6ADBA` | 2.04 ✗ | `#7E848E` | 3.01 |
| `success` | `#4E9675` | 3.19 ✗ | `#3A7158` | 4.55 |
| `error` | `#C65D6B` | 3.66 ✗ | `#A34B57` | 4.53 |
| `warning` | `#C08A3E` | 2.73 ✗ | `#866029` | 4.51 |
| `merged` | `#9B86B8` | 2.92 ✗ | `#706086` | 4.53 |
| `accent-shimmer` | `#5E88CC` | 3.23 ✗ | `#47679D` | 4.54 |
| 词级 diff(added) | `#A9D3B4` on `#DCEBDD` | **1.34** ✗ | `#566D5C` | 4.54 |
| agent 八色中 4 个 | yellow 2.81 / orange 2.87 / cyan 2.97 / pink 3.20 | ✗ | 分别 `#A77C2E`/`#B77343`/`#508D95`/`#BA6A92` | 3.00–3.02 |

**dark 侧只需改两个键**:`text-subtle` `#5E6673`→`#6D7684`、`promptBorder` `#55606F`→`#697789`
(原值在卡片面 `#292D36` 上只有 2.62 / 2.38)。其余 dark 原值在 `#22262E` 画布上本已达标(text 12.15、inactive 5.04、accent 5.80)。

### 9.3 实测的 dsh web 文字色直接通过

换用 §10 实测的 dsh web 值后无需校准,印证其网页面本身就是按可读性调的:
`#0F1115` on `#F5F6F7` = **17.46**;`#61666B` = **5.36**;`#81858C` = **3.42**(装饰档)。

### 9.4 方法与可复跑

校准在**线性光空间**内二分调明度(保持色相比),目标为“在画布与卡片面中更紧者上达标”。
阈值不靠感觉:全部结论由 `node web/scripts/check-design.mjs` 复核,当前 58 项全过、退出码 0。

## 10. 结构级并排比对(基于官方截图实测)

### 10.1 方法与其局限

参照物:`docs/user/guide/providers-models-page.zh.png`(1600×866)与
`providers-custom-form.zh.png`(1128×864)(dsh 仓库内官方 web UI 截图)。

**诚实声明**:执行本比对时所用模型**不具备视觉能力**,无法“看图说话”。
因此改用**测量**替代观察——纯标准库解码 PNG(zlib + unfilter),统计色频、
有彩色占比、行/列色段边界。可测量项如下;字号、间距、圆角、图标形态**无法**由像素统计可靠反推,
列为未测量项(§10.3),需要视觉复核。

### 10.2 实测结果

| 维度 | dsh web 实测 | qi-web 取值 | 差异说明 |
| --- | --- | --- | --- |
| 画布 | `#F5F6F7`(67.6% / 29.2%) | `--qi-bg` 同值 | — |
| 浮面 | `#FFFFFF`(28.0% / 65.7%) | `--qi-surface` 同值 | **关系与直觉相反**:白面浮在**浅灰**画布上 |
| 发丝线 | `#E5E5E5` | `--qi-hairline` 同值 | 刻意近乎不可见;qi 拆出 `--qi-border` 承担可交互边界 |
| 墨 / 次要 / 三级 | `#0F1115` / `#61666B` / `#81858C` | `--qi-text` / `-inactive` / `-subtle` 同值 | 近黑墨,非雾蓝的 `#343945` |
| 悬停 / 选中底 | `#EBEEF2` / `#E1E5EE` | `--qi-surface-alt` / `-selected` | — |
| **最大 chroma** | **12 / 255**(≈4.7% 饱和) | 骨架全灰阶 | **dsh web 无品牌色、无状态色** |
| 左栏 | 424px @1600 = **26.5%**,白面 | `--qi-shell-sidebar-width: 240px`(固定) | 实测是比例宽;qi 取固定值以适配常见窗口 |
| 顶部 | ~28px 窗口 chrome(`#C0C0C0` 系) | 不采纳(宿主 chrome,非 UI) | — |
| 底部 | 48px 白条 | `--qi-shell-statusbar-height: 36px` | qi 取更矮,内容更密 |
| 输入框门控 | 未选中 workspace 前**会话输入框不可用** | 待定:qi 会话绑定 `cwd` 后可同构 | 官方 guide 明载 |

### 10.3 未测量项(需视觉复核)

1. **dsh web 的 dark 主题**:官方截图只有浅色,故 qi 的 dark 画布 `#22262E` 是**qi 自选**(注释已标)。
2. 字号阶梯、行高、间距节奏、圆角半径、图标与状态点的具体形态。
3. 工具卡片的实际视觉权重(文档只给了“状态标题 + 内收正文”这条结构规则)。
4. 轨迹视图的节点形态(官方 Features 页文案只说“按来源查看”,未给结构细节)。

> 补测方式:把上述两张 PNG 交给**有视觉能力**的模型或人工,按 §10.2 表逐行核对并把差异回填本节。

## 11. 设计回归检查清单

进 CI 的判定项;标 **[自动]** 的已由 `node web/scripts/check-design.mjs` 强制(当前 58 项通过)。

| # | 判定项 | 方法 |
| --- | --- | --- |
| 1 | `web/src/` 下除 `theme/tokens.css` 外不出现任何 `#rrggbb` | **[自动]** 扫源码,越界即失败 |
| 2 | 所有前景色在**画布与卡片面中更紧者**上达标(正文/次要/语义 ≥4.5,装饰 ≥3) | **[自动]** 解析 `light-dark()` 实测 |
| 3 | 词级 diff 色在**各自行底**上 ≥4.5 | **[自动]** 专项规则 |
| 4 | 填充底上的反色字 ≥4.5 | **[自动]** `text-inverse` vs `accent` |
| 5 | 发丝线只做分隔,不得承担可交互边界(可交互边界必须用 `--qi-border`) | 评审:搜 `--qi-hairline` 的使用点 |
| 6 | 折叠态工具行**恰好一行**,高度 = `--qi-tool-row-height` | 视觉快照 |
| 7 | 卡片只用 `--qi-surface` + `--qi-hairline` 表达,**不用阴影** | 视觉快照 + 评审 |
| 8 | 语义色只用于状态与身份,不得作装饰 | 评审 |
| 9 | 状态不单靠颜色(成功/失败必须带 ✓/✗ 形符) | 视觉快照 |
| 10 | 外壳尺寸只用 `--qi-shell-*` 与 `--qi-content-max-width`,不得就地写字面值 | 评审(可扩展为自动) |

## 12. 已收口与仍待定(视觉相关)

### 已收口

- **语义色策略(2026-09)**:骨架灰阶 + 状态少量彩色。
  - 骨架、文字、分隔线全用 §10.2 **实测**的 dsh web 灰阶,不引入暖色底;
  - 彩色只出现在**状态与身份**处:success/error/warning/plan-mode、diff 增删、agent 八色、工具类别点;
  - **不选**全灰阶(丢颜色通道后,多 agent 并发时无法靠颜色区分 agent,高密度工具行扫读变慢);
  - **不选**整体雾蓝暖色(与 dsh web 实测配色不符,且已校准的 light 语义值需再搬一次)。
  - 权衡:与 dsh web 相似度 = **骨架 100%,彩色是 qi 补的**——因为 dsh web 没有 dispatcher 这一层,
    而 qi 必须把分派置信度与 agent 身份表达出来。
  - 落实位置:`web/src/theme/tokens.css`(本已符合该策略,决策未引起改动);由检查清单第 8 条持续强制。

### 仍待定(不阻塞 P0/P1,可在 P2 前定)

- dark 画布色:现为暖炭 `#22262E`(qi 自选,dsh web 只提供了浅色截图)。备选:改用更贴 dsh web 浅色面板的冷灰家族。
- 左栏宽度:现取**固定 240px**;dsh web 实测为**比例宽**(26.5%)。窄窗口下比例宽更稳,宽屏下固定值更克制。

## 13. P0 契约改动记录(2026-09)

> 这些不是“视觉”而是**前端与历史回放依赖的契约**。先于画皮做:事件 `data` 与会话 entry 一旦被前端依赖,
> 再改就要同时改后端、前端、回放三处。

| 改动 | 落实位置 | 契约形状 |
| --- | --- | --- |
| 工具结果结构化 | `models.ToolOutcome`、`runner._execute` → `tool_end.data` | `{"status":"ok\|error","duration_ms":int,"exit_code":int\|None,"error":str\|None}`;`text` **仍是模型可见原文**(与旧版逐字一致,所以模型行为不变) |
| usage 透出 | `runner.run` → `agent_end.data["usage"]`;`llm.usage_to_dict` | `{"turns":int,"llm_calls":int,"prompt_tokens":int,"completion_tokens":int,"total_tokens":int, 以及 provider 特有的整数字段}`。usage 已归一为**纯 dict**——litellm 返回的是 pydantic 对象,直接塞进事件会让 SSE / `--mode json` 的 `json.dumps` 失败 |
| 会话 cwd | `session.Session.cwd`、`create(cwd=…)`、`ensure_cwd()` | header `{type,id,title,created_at,**cwd**}`。v0.1 旧会话首次被使用时回填一次;已有值**绝不覆盖**(跳目录恢复旧会话不得改历史);header 不合法则不猜、不改文件 |
| 第五类 entry(`tool`) | `runtime._persist_tool` | `{type:"tool",agent,tool,args,status,duration_ms,exit_code,error,result}`。补齐 PLAN A5 已宣布但**从未实现**的第五类;`result` 落盘上限 `MAX_TOOL_ENTRY_CHARS=8000`(插件工具可能不自截断) |
| user 消息即时落盘 | `runtime.stream` | 旧实现把 user 写在回合**结束后**,运行中刷新/断线就看不到自己说了什么 |
| 逐字流式 | `llm.LLMDelta` / `StreamingLLMClient` / `stream_llm`;`runner` → `text_delta` | 契约:**0+ 个文本块 + 恰好一个 `finished` 块**(`tool_calls` / `usage` **只在** finished 上)。`LLMClient` 只要求 `chat()`;**流式是可选增强**——没 `astream` 就用 `chat` 合成单块(第三方实现与测试替身零改动) |
| 叙述落盘 | `runner` → `assistant_message`;`runtime._persist_narration` | 每轮 LLM 回复声明一次 `{"step":int,"tool_calls":[名字]}`。**宣布了工具调用的那条**是"过程"→ `custom/assistant_narration`;**不带工具调用的那条**才是最终回答 → `message` entry(所以不重复) |

**落盘顺序不变量**(新增,回放依赖它):

```text
header → dispatch → state → user → (assistant_narration → tool)* → assistant
```

叙述**立即**落盘而不缓冲到下一轮:若等到下一轮才写,它会被写在它触发的工具卡片**之后**,
回放顺序就变成"工具卡 → 叙述",与真实因果相反。把叙述做成 `custom` 而非 `message` 也是刻意的——
`_history()` 只读 `message`,所以**模型跳轮上下文完全不变**(零提示词回归风险)。

**流式的两处容忍**(均为**粘性**,不会每轮重试注定失败的请求):

1. `stream_options.include_usage` 被 provider 拒绝 → 去掉该参数重试一次。不加它,流式下拿不到 usage,
   成本/token 显示会长期为空。
2. 流式本身打不开(网关/代理不支持)→ 永久退回非流式,整段文本作为单块发出。
   **消费者形状不变**,只是不再逐字。

**已知限制(写明而不假装支持)**:若流**已吐出部分文本后**才失败,不重试、直接抛出——重试会导致文本重复。
Web 宿主应把它转成 error 帧(而不是断开连接);CLI 保持与旧版一致的行为(向上抛)。

**进程型工具的退出码**:`bash` 是唯一上报 `exit_code` 的工具(超时拿不到退出码,改用 `error="timeout"`)。
其余工具继续返回 `str` 即可,由 AgentRunner 统一计时并包成 `ok`;插件工具可返回 `ToolOutcome` 自行上报。

**附带修掉的既有缺陷**:`_bash` 超时路径只 `kill()` 不 `wait()` 回收子进程,transport 会拖到事件循环关闭后才 GC,
触发 “Event loop is closed” 的 unraisable 异常(测试会报资源警告)。

**不变量由测试锁定**:`tests/test_contract_p0.py`(16 项)与 `tests/test_streaming.py`(16 项)。
改这里的字段名或事件顺序必须同步改那两个文件——这正是把它们单独成文件而不是塞进参考实现的原因。

## 14. 首个可运行版本(2026-09)

> `qi web` 已经能**真跑**:真实模型逐字流式 → 工具调用 → 落盘 → 刷新回放。本节记怎么跑、契约长什么样、以及实测证据。

### 14.1 跑起来

```bash
uv pip install -e ".[web]"      # 只会多装 fastapi/uvicorn 等(web 是可选 extra)
cd web && npm install && npm run build && cd ..   # 前端构建产物 → src/qi_agent/web/static/
qi web                           # → http://127.0.0.1:30142
```

| 选项 / 环境变量 | 说明 | 默认 |
| --- | --- | --- |
| `-p, --port` | 端口(与 pi-web 的 30141 错开) | `30142` |
| `-H, --hostname` | 绑定地址 | `127.0.0.1` |
| `--no-open` | 不自动开浏览器 | 开 |
| `--cwd` | 默认工作目录 | 当前目录 |
| `--password` / `QI_WEB_PASSWORD` | 访问口令(Bearer 或 Basic) | 无 |
| `QI_WEB_ALLOWED_HOSTS` | 反代外部域名允许表(逗号分隔) | 空 |

**不安全的组合拒启**:`--hostname` 非回环且无口令 → 直接退出并给出修复步骤。
暴露出去的是一个能执行高权限操作的 agent,不是静态站点。

### 14.2 已实现的 `/api` v1

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/meta` | 契约号 + 能力开关 + 默认 cwd(前端据此降级) |
| GET | `/api/health` | 存活(**免鉴权**,给反代探活) |
| GET/POST | `/api/sessions` | 列表 / 新建(新建写 `cwd`) |
| GET/PATCH/DELETE | `/api/sessions/{id}` | 窗口明细(`limit`/`before`) / 改名 / 删除 |
| POST | `/api/sessions/{id}/turn` | 提交一轮 → 202 `{run_id}`;已有活跃 run → **409** |
| POST | `/api/sessions/{id}/cancel` | 取消当前 run(→ `run.finished: cancelled`) |
| GET | `/api/sessions/{id}/events` | **SSE**;`?run_id=` 可重放**已完成**的 run |
| GET | `/api/agents` / `/api/config` | agent 清单 / 模型与检查项(凭证**只回掩码**) |
| POST/DELETE | `/api/auth/{provider}` | 写/清凭证;必须带 `X-Qi-Confirm: yes`(**428** 否则) |

### 14.3 SSE 契约(前端依赖它)

```text
(1) : qi-web sse open            ← 开场注释帧
(2) event: snapshot              ← 每代的**权威起点**(不带 id,避免与 seq 撞号)
(3) event: <kind>  id: <seq>     ← 事件,按真实因果顺序
(4) event: run.finished          ← 结束,随后**关闭连接**
```

- 断线重连:带 `?from=<seq>` 补齐;`?run_id=` 可重放整轮(含已完成的)。
- 空闲会话:发 `snapshot` 后只发心跳注释帧保活。
- 无活跃 run 时**不会**推事件;客户端靠 `snapshot` 重建(落盘结果才是权威)。

前端用 **fetch + ReadableStream** 而不是 `EventSource`:后者不能带鉴权头、也没法中途 abort
(也避开了 cookie 会话,为将来的桌面壳留路)。

### 14.4 实测证据(真实模型,非 stub)

```text
id 0  dispatch         qi (router, 0.90) + reasoning            ← 真实 Router 决策
id 2  text_delta       "I"
id 3  text_delta       "'ll"
 ...                    …逐字直到 "I'll look at the top-level directory."
id 11 assistant_message {step:1, tool_calls:["ls"]}              ← 叙述(过程)
id 12 tool_start       ls {"path": "."}
id 13 tool_end         {status: ok, duration_ms: 0, exit_code: null}
id 68 text             …最终回答
id 69 agent_end        usage {turns:2, llm_calls:2, total_tokens:1687}
id 70 run.finished     {status: ok}
```

落盘顺序(同一轮):`session → dispatch → state → user → assistant_narration → tool → assistant`,
`qi sessions show` 回放与之一致——**直播与回放不会不一致**。

> 说明:流式下能拿到 usage,是因为请求带了 `stream_options.include_usage`;
> provider 不接受时自动去掉重试(见 §13)。

### 14.5 本期**不做**(已预留位置)

文件树与 diff 审阅 / Web 终端(pty) / bash 审批闸门与 clarify(HITL) / 多会话看板 /
queue-steer 语义 / headless RPC / UI 插件拆包。

### 14.6 打包注意

前端产物**不入版本控制**(`.gitignore` 已忽略 `src/qi_agent/web/static/`),
但**进 wheel**。因此打包前必须先 `cd web && npm run build`;
若缺产物,`qi web` 会给一个说明页而不是坏页面(`static_ready: false`)。

### 14.7 验证怎么跑(三层各管一段)

```bash
# 宿主 + 协议层(python)
.venv/bin/python -m pytest -q          # 180 项

# 前端
cd web
npm test                               # 37 项(vitest)
npm run typecheck                      # tsc --noEmit(含测试文件)
npm run check:design                   # 颜色:唯一色值来源 + 对比度
npm run build                          # 产出 → src/qi_agent/web/static/
```

| 层 | 工具 | 管什么 |
| --- | --- | --- |
| 颜色 | `scripts/check-design.mjs` | 除 `tokens.css` 外不得出现具体色值;58 项对比度 |
| 逻辑 | `web/src/**/*.test.ts`(vitest) | SSE 切帧、事件→视图归约、真实一轮的因果顺序 |
| 契约 | `web/src/api/contract.test.ts` | 版本号两侧一致、`/api/meta` 真实响应形状、**事件 kind 两侧对齐** |
| 后端 | `tests/test_*.py`(pytest) | 事件字段/落盘顺序不变量、API、并发 409、凭证闸门 |

契约守卫做过**变异验证**:在后端注入一个新的 `kind` → 测试变红并指名道姓
(`后端新增了未声明的 kind:…`),而不是默默丢掉这个事件。

测试只覆盖**纯函数**(切帧/归约/契约)——不引 jsdom 与组件快照:组件渲染的问题靠肉眼看更快,
而真正的风险集中在“事件怎么变成视图”这段逻辑上。构建产物里不会混入测试代码
(测试不被入口引用,Vite 不打包;产物体积已核对不变)。
