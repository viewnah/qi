# Web 能力设计(宿主 + UI 插件)

> 状态:设计定稿(v2 实现)。相关文档:[plugins.md](plugins.md)(插件机制)、[tools.md](tools.md)(ToolCatalog)。参照物:pi 的 RPC 模式、pi-web(独立 Web 应用)、hikqin(宿主形态)。
>
> **参照物事实核对(2026-09,对已装包实测,不是转述)**——这一节存在的原因是本文早先
> 把 pi-web 写成了「子进程 + JSONL-RPC」,实测不对:
>
> | 断言 | 实测 | 证据 |
> | --- | --- | --- |
> | pi-web 与 pi agent 走子进程 RPC | ❌ **同进程,内嵌 SDK** | `@agegr/pi-web@0.8.11` 的 `.next/server` 里有 **40 处** `import("@earendil-works/pi-coding-agent")`;全 `.next` 里 **0 处** `--mode rpc` |
> | pi-web 靠 spawn 拉起 agent | ❌ | `bin/pi-web.js` spawn 的是 **`next start`**(它自己的服务);构建产物里的 `spawn(` 命中是 Bluebird 的 `Promise.spawn()`(废弃 API),`rpc` 命中是 protobuf 的**语法高亮规则** |
> | pi-web 无应用级口令 | ❌ **有** | `bin/pi-web.js` 读 `PI_WEB_PASSWORD`;`.next/server/middleware.js` 用 `sha256` + **`timingSafeEqual`** 做 Basic Auth,并带 hostname 允许表 |
>
> `pi --mode rpc`(JSONL over stdio)是 pi **确实提供**的能力,但它的定位是给*跨语言 / 跨进程*的消费者
> (pi 文档原话:"useful for embedding the agent in other applications, IDEs, or custom UIs"),**pi-web 并没有用它**。
> qi 把它列为未来项(见 §5),判断不变,只是理由要基于这条事实。

## 1. 结论与形态

**宿主在框架,UI 是插件**:

| 层 | 内容 | 归属 |
| --- | --- | --- |
| HTTP 宿主 | uvicorn、基础 `/api`(会话/发消息)、SSE 事件流、静态资源托管、安全(回环/密码/信任) | **框架(v2)** |
| UI 前端 | 页面资源(HTML/JS/CSS:聊天 + 分派可视化) | **插件**(pip 包,随包分发) |

理由:web 是 `runtime.stream()` 的**第三个 consumer**(CLI / TUI / HTTP,同进程零协议)。
qi 能把宿主留在框架里,根因是 **agent core 就是 qi 自己的包** —— core 与宿主同一个进程、同一个
发布单元,连依赖都不用加。pi-web 做不到这一点是因为它是**另一个产品**:一个独立的 Next.js 应用,
把 pi 的 agent core 当 npm 依赖内嵌进来(见文档头的事实核对),于是它的后端天然只能服务自己那一个前端。
而前端属于"内容/可选能力",插件化与"框架零内置内容"哲学一致,核心不养前端。

## 2. 方案取舍(为何不选外置 / 纯内置)

三种"外置"要分开,不能笼统说"外置应用":

| | **独立产品内嵌 SDK**(pi-web 实际形态) | **跨语言外置**(pi `--mode rpc`) | 纯内置 UI | **宿主 + UI 插件(采用)** |
| --- | --- | --- | --- | --- |
| 进程/协议 | 同进程(应用自己 import agent core) | **子进程 + JSONL-RPC** | 同进程 | 同进程,零协议 |
| 适用前提 | 宿主与 core 同语言、且愿意把 core 打进自己的包 | 宿主不是 Node / 需要进程隔离与沙箱 / 不为版本负责 | 同语言 | 同语言 |
| 核心养前端 | 不养 | 不养 | **要养** | 不养(官方出参考 UI 插件) |
| UI 升级/换皮 | 跟宿主应用发布 | 跟宿主应用发布 | 跟框架发布 | pip 升级 / 换插件 |
| 复杂度 | 一个应用两套构建(Next.js 前后端同包) | 两套部署 + 进程生命周期管理 | 最小 | UI 与宿主 `/api` 需契约版本化 |
| qi 的取舍 | 不选:core 是自己的库,没必要为 UI 另起一个产品 | 不选:留给外部客户端(§5),v2 不在范围 | 不选:核心不养前端 | **采用** |

为什么 qi 不需要 pi-web 那一层:**pi-web 的成本来自"它是一个独立产品"** —— 前端与 agent core 分属两个 npm 包、两套构建流水线,所以它必须自己实现一整套 HTTP 路由(实测 **45 条 `/api/*`**)来当二者之间的桥。
qi 的 core 与宿主同一个包,这层桥用 `runtime.stream()` 一个异步生成器就替代了。

## 3. HTTP 宿主(v2)

```text
qi web                          # 起本地服务;默认 127.0.0.1 + 端口配置 + 可选密码
▼
FastAPI/Starlette + uvicorn     # 复用 hikqin server.py 思路
├── /api/sessions · /api/agent/<id>     # REST:查会话 / 发消息
├── /api/agent/<id>/events(SSE)         # 事件流 = stream() 直连(同 CLI/TUI 的 consumer)
├── /api/health
└── 静态托管:已安装 UI 插件的页面资源
```

> ⚠️ **上面是 v2 的设计草图,路由命名没有落地。** 实际实现是**围绕 session 而不是 agent** 的:
> `POST /api/sessions/{sid}/turn` + `GET /api/sessions/{sid}/events`,**不存在** `/api/agent/<id>`。
> 原因是分派器会把一轮路由到不同 agent(还可能中途换),把 run 挂在 agent 上会站不住。
> **真实路由清单见 §14.2**(以那份为准)。

- 应用级配置(host/port/password_env)属应用配置主题(v2 待定;模型/凭证已在 `models.json` + `auth.json`)
- 安全默认:仅 `127.0.0.1`;远程需显式 hostname + 密码(参考 pi-web 的警告语义:暴露可执行高权限操作的 agent)
  - 实测 pi-web 的同位做法:`PI_WEB_PASSWORD` + `.next/server/middleware.js` 里的 Basic Auth(`sha256` + `timingSafeEqual` 定长比较),非回环时警告"Basic Auth over HTTP,请用 HTTPS 或可信 VPN"。qi 的做法一致(`Authorization: Bearer`/`Basic` + `secrets.compare_digest` + 拒启不安全组合)
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
| 命名法 | 颜色走**语义键**(`label-primary`/`interactive-bg-hover`/`state-success-primary`),禁止调色板命名出现在语义层;且**来源命名要改成语义命名**(dsh 正把 `claude`/`clawd_body` 改名,qi 直接用语义名,不重复这段历史)。**注意分层**:`--dsw-static-*`(原始色板,如 `deepseek-400` / `neutral-bluish-100`)是**底层纯值**,按调色板命名是刻意的;语义纪律只管 `--dsw-alias-*` / `--dsw-specific-*` 两层 |
| 令牌纪律 | `tokens.css` 是唯一色值来源;组件出现 `#rrggbb` 即回归失败(§11 第 1 条,已可自动拦截) |
| 工具卡片 | **状态标题 + 内收正文**,折叠只按行数(不用 `⏺` 子弹行——那是 Claude Code 的形态) |
| 双视图 | 同一事件族驱动**对话视图**与**轨迹视图**两个 target,各自持有 display model、互不导入 |
| 重连 | ~~逻辑 seq 区间校验 + 每代 snapshot 原子替换 + `page()` 修 gap~~ **已随 AG-UI 改造作废**(§15):单 POST 之下"流就是这次响应",重连语义变成"重发一个 `RunAgentInput`" |
| 主题容错 | 坏主题跳过整文件、未知键跳过并告警、主题名不得越出主题目录、一个坏主题不阻塞启动 |

### 8.3 不采纳

| 项 | 理由 |
| --- | --- |
| ~~`--dsw-*` 前缀~~ **【已推翻·2026-09】** | 原写“抄纪律不抄前缀;qi 用 `--qi-*`”。**已改为逐字照搬 `--dsw-*`**(见 §8.4):手抄一套改名的令牌会静默漂移,而且无法与 dsh 源文件逐条对拍 |
| dsh 的插件底座(Cordis)与 profile/组合包 | qi 的插件机制是 pip entry point + 本地目录双通道(docs/plugins.md),不同构;只借“能力可替换”的概念,不搬实现 |
| dsh TUI 的 `dark-ansi` 第三套色板 | 那是终端 16 色的能力降级,浏览器无此约束 |
| 品牌资产(字标、logo、成套主题、吉祥物色 `clawd_*`) | 色值可借鉴,品牌资产不复刻 |
| 窗口 chrome 的 `#C0C0C0` 竖条 | 那是截图里的宿主窗口标题栏,不是 web UI 的一部分 |

### 8.4 令牌层:从「改名抄一套」改为「逐字照搬 + 对拍门禁」(2026-09,取代 §9)

**决定**:色值层**逐条等于** dsh,包括 dsh 自己的取值(**不再做 qi 自订的 AA 校准**)。参照物不是截图,是已装包里的
`@deepseek-ai/dsh-client-ui-theme/src/styles/design-platform.css` + `ui-theme/src/styles/base.css`。

分层与 dsh 一致:

| 层 | 例子 | 性质 |
| --- | --- | --- |
| ① `--dsw-static-*` | `--dsw-static-deepseek-500: #4176e6` | 原始色板,主题无关,纯值 |
| ② `--dsw-alias-*` | `--dsw-alias-label-primary`、`--dsw-alias-button-info-fill` | 语义别名,浅/深各自指向不同静态色 |
| ③ `--dsw-specific-*` | `--dsw-specific-input-major`、`--dsw-specific-menu` | 场景令牌 |
| ④ `--dsh-*` | `--dsh-composer-card-max-width`、`--dsh-sidebar-width` | 应用级布局常量 |

与 dsh 的**唯一机制差异**(刻意):dsh 用 `body[data-ds-dark-theme]` 打标记,qi 用 `color-scheme` + `light-dark()`,
于是三态(auto/light/dark)零重复,而且 `scripts/check-design.mjs` 能把两套值解析出来做保真校验。

elevation 三档必须**逐元素声明**(dsh 写在 `body, body *` 上,不是只写在 `:root`):`var()` 在声明元素上
就替换完了,继承下去的字符串里已经烘焙好当时的描边色,组件再重绑 `--dsw-elevation-stroke-color` 不生效。
qi 早先只写在 `:root`,导致输入卡片的描边静默吃到 `border-l4`(dsh 要的是 `border-l2`)-已修。

**代价与它换来的东西**:不再有“为可读性自调”的自由,但换来一条**可验证**的等价关系 —— 由两个门禁强制:

| 门禁 | 管什么 |
| --- | --- |
| `scripts/check-design.mjs` | dsh 色值保真(逐条比)+ 色值纪律(除 `tokens.css` 外不得出现 `#rrggbb`)+ 对比度**如实报出** |
| `scripts/check-dsh-tokens.mjs` | **全部 90 条** `--dsw-alias-*` / `--dsw-specific-*` × 2 主题,逐条对拍 dsh 源文件(参照物缺失时跳过并明说,不静默判过) |

第二个门禁存在的原因:第一个只硬编码了 22 个键,而 `design-platform.css` 有 90 条 —— 没被列到的可以整条抄错而门禁全绿。
事实上就是这样漏掉了 6 条真错(其中 `--dsw-alias-button-info-fill` **明暗两值整个抄反**、
`--dsw-alias-button-info-hover` 被写成与 fill 相同 → 浅色下发送按钮没有 hover 态)。

## 9. 对比度校准记录

> ⚠️ **【已被取代 · 2026-09】本节整体作废,保留作为决策留痕。**
>
> 本节记录的是一套 **qi 自订的 AA 校准**(把 dsh TUI 的色值调暗到在网页底上达 4.5:1)。
> 后来推翻,改为**逐条照搬 dsh 的真实设计系统、包括它自己的取值**(理由与门禁见 §8.4)。
> 因此:
>
> - **§9.1 的阈值政策不再是项目要求。** 现在的对比度检查是**如实报出而不作失败**,
>   dsh 自己的取值(如 `label-caption` 浅色 2.13)就低于 AA,qi 照搬。
> - **§9.2 的“现取值”一列已不在 `tokens.css` 里**(那些 `#626773` / `#3A7158` / `#A34B57` 等均已不存在)。
>   当时测的 `dsh-TUI/src/theme.ts` 也不再是参照物 —— 参照物换成了 `design-platform.css`。
> - 本节的**实测数字本身仍然有效**(“dsh TUI 的浅色板是给终端用的前景色,搬到网页底上大面积不达 AA”
>   这个观察没错),作废的是**据此自调色值**这个决定。
>
> 一句话:**从“抄结构、自调色”改成“色值也照搬,可读性由 dsh 负责,qi 只负责不漂移”。**

### 9.1 阈值政策(历史,已不作数)

| 类别 | 阈值 | 说明 |
| --- | --- | --- |
| 正文 / 次要文字 / 语义色 / 强调色 | **≥ 4.5:1** | AA 正文 |
| 装饰与身份标识(agent 八色、工具点) | **≥ 3:1** | WCAG 非文本对比度 |
| 发丝线 `--qi-hairline` | **不设阈值** | 纯分隔用途;承担可交互边界时必须改用 `--qi-border`(≥3:1) |

> 上面这两个 `--qi-*` 名字也一并作废了 —— 现在是 `--dsw-alias-border-l1/l2/l3`:
> `l1` 近乎不可见(发丝线,纯分隔)、`l2` 是输入卡片的描边、`l3` 是外壳分隔线。

判定面**取最紧的那个**:同一前景色会同时出现在画布(`--qi-bg`)与卡片面(`--qi-surface`)上,
按两者中对比度更低者判定(工具行就在卡片面里)。

> 现名:`--dsw-alias-bg-base`(画布)/ `--dsw-alias-bg-layer-1`(浮面)/ `--dsw-alias-label-primary`(墨)。

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
| 画布 | `#F5F6F7`(67.6% / 29.2%) | `--dsw-alias-bg-module-platform` 同值 | — |
| 浮面 | `#FFFFFF`(28.0% / 65.7%) | `--dsw-alias-bg-layer-1` 同值 | **关系与直觉相反**:白面浮在**浅灰**画布上 |
| 发丝线 | `#E5E5E5` | 拆成三层:`--dsw-alias-border-l1`(发丝)/ `-l2`(输入卡片描边)/ `-l3`(外壳分隔) | dsh 自己也是这个三层法;颜色取自 `design-platform.css`,不是从截图吸的 |
| 墨 / 次要 / 三级 | `#0F1115` / `#61666B` / `#81858C` | `--dsw-alias-label-primary` / `-secondary` / `-tertiary` 同值 | 近黑墨 |
| 悬停 / 选中底 | `#EBEEF2` / `#E1E5EE` | `--dsw-alias-interactive-bg-hover` / `--dsw-specific-sidebar-nav-item-active` | — |
| **最大 chroma** | **12 / 255**(≈4.7% 饱和) | 骨架全灰阶 | **dsh web 无品牌色、无状态色** |
| 左栏 | 424px @1600 = **26.5%**,白面 | `--dsh-sidebar-width: 280px`(**固定**) | 实测是比例宽;qi 取固定值以适配常见窗口 |
| 顶部 | ~28px 窗口 chrome(`#C0C0C0` 系) | 不采纳(宿主 chrome,非 UI) | — |
| 底部 | 48px 白条 | 状态条 `min-height: 32px` | qi 取更矮,内容更密 |
| 输入框门控 | 未选中 workspace 前**会话输入框不可用** | 未同构:qi 会话在 `create` 时就绑定了 `cwd`,没有"未选工作区"态 | 官方 guide 明载 dsh 有这一态 |

> **本节的定位已变**:它当初是"从官方截图**吸色**再自调"。现在色值的唯一来源是
> **已装包里的 `design-platform.css`**(见 §8.4),本表只作为"dsh 网页面确实无状态色"这个结构性结论的留痕;
> 逐条对拍交给 `check-dsh-tokens.mjs`。

### 10.3 未测量项(需视觉复核)

1. **dsh web 的 dark 主题**:官方截图只有浅色。**qi 的 dark 现在是照搬 dsh 的 `--dsw-alias-bg-base` 深色值
   (==`--dsw-static-neutral-bluish-950`),不再自选**(早先那个自选的 `#22262E` 已不存在)。
2. 字号阶梯、行高、间距节奏、圆角半径、图标与状态点的具体形态。
3. 工具卡片的实际视觉权重(文档只给了“状态标题 + 内收正文”这条结构规则)。
4. 轨迹视图的节点形态(官方 Features 页文案只说“按来源查看”,未给结构细节)。

> 补测方式:把上述两张 PNG 交给**有视觉能力**的模型或人工,按 §10.2 表逐行核对并把差异回填本节。

## 11. 设计回归检查清单

进 CI 的判定项。标 **[自动]** 的是**真会失败**的项;标 **[报告]** 的只输出数字、不作失败判定。

两个自动门禁(都可单跑,已接进 `npm run check:design`):

| 脚本 | 管什么 |
| --- | --- |
| `node web/scripts/check-design.mjs` | [A] dsh 色值逐条保真 [B] 色值纪律 [C] 对比度(**[报告]**,不作失败) |
| `node web/scripts/check-dsh-tokens.mjs` | **全部 90 条** alias/specific × 2 主题逐条对拍 dsh 源文件 |

| # | 判定项 | 方法 |
| --- | --- | --- |
| 1 | `web/src/` 下除 `theme/tokens.css` 外不出现任何 `#rrggbb` | **[自动]** 扫源码,越界即失败(`check-design` [B]) |
| 2 | 每条 `--dsw-alias-*` / `--dsw-specific-*` 在两个主题下都与 dsh 源文件一致 | **[自动]** `check-dsh-tokens.mjs`(90 条 × 2) |
| 3 | 骨架色值不得被改掉(22 个关键键的硬编码基准) | **[自动]** `check-design` [A] |
| 4 | 对比度:所有前景色在**画布与卡片面中更紧者**上的实测值 | **[报告]** 解析 `light-dark()` 实测。**不再要求 ≥4.5** —— dsh 自己的取值(如 `label-caption` 浅色 2.13)就低于 AA,qi 照搬(§8.4) |
| 5 | 发丝线只做分隔,不得承担可交互边界(可交互边界用 `--dsw-alias-border-l3`) | 评审:搜 `--dsw-alias-border-l1` 的使用点 |
| 6 | 折叠态工具行**恰好一行**(`.tool__head { min-height: 28px }`) | 视觉快照 |
| 7 | 卡片用 `--dsw-alias-bg-layer-*` + `--dsw-alias-border-l*` 表达 | 视觉快照 + 评审 |
| 8 | 语义色只用于状态与身份,不得作装饰 | 评审 |
| 9 | 状态不单靠颜色(成功/失败必须带 ✓/✗ 形符) | 视觉快照 |
| 10 | 外壳尺寸只用 `--dsh-*` 布局常量,不得就地写字面值 | 评审(可扩展为自动) |

## 12. 已收口与仍待定(视觉相关)

### 已收口

- **语义色策略(2026-09)**:骨架灰阶 + 状态少量彩色。
  - 骨架、文字、分隔线全用 dsh **源文件**里的灰阶(`--dsw-alias-bg-*` / `label-*` / `border-l*`),不引入暖色底;
  - 彩色只出现在**状态与身份**处:success/error/warning、diff 增删、agent 八色、工具类别点;
  - **不选**全灰阶(丢颜色通道后,多 agent 并发时无法靠颜色区分 agent,高密度工具行扫读变慢);
  - **不选**整体雾蓝暖色(与 dsh web 配色不符)。
  - 权衡:与 dsh 相似度 = **骨架 100%(现在连色值都逐条对拍,见 §8.4),彩色是 qi 补的**——因为 dsh web 没有 dispatcher 这一层,
    而 qi 必须把分派置信度与 agent 身份表达出来。
  - 落实位置:`web/src/theme/tokens.css`;由检查清单第 2/8 条持续强制。

### 仍待定(不阻塞 P0/P1,可在 P2 前定)

- **(已定)dark 画布**:~~现为暖炭 `#22262E`(qi 自选)~~ → 改为照搬 dsh 的 `--dsw-alias-bg-base` 深色值(`--dsw-static-neutral-bluish-950`)。
- **(已定)左栏宽度**:~~现取固定 240px~~ → 现为 `--dsh-sidebar-width: 280px`(仍是固定值,dsh 用 dsh-client-ui-layout 的常量;dsh web 截图里那个 26.5% 是它自己的比例宽实现)。
- **qi 自有语义键与 dsh 命名不一**:`--dsw-alias-state-success` / `state-warning` / `state-error` / 三个 `-bg`
  是 qi 起的名字,dsh 那边叫 `state-success-primary` / `state-warn-primary` / `state-error-primary`,
  而且**取值并不一一相等**(qi 这套浅/深各配一对,dsh 有些键两主题同值)、`-bg` 两个在 dsh 里没有对应物。
  它们只被消息流/工具行/轨迹用到,**不在首页路径**;统一命名需要同时改 `app.css` 的十几处使用点。
  `check-dsh-tokens.mjs` 会把这 7 个键列为“qi 自有令牌”如实报出,不作为失败。

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

### 14.2 已实现的 `/api`(契约 v2)

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/ag-ui` | **AG-UI 协议端点**:单次 POST `RunAgentInput` → `text/event-stream`。已有活跃 run → **409** |
| GET | `/api/meta` | 契约号 + 能力开关 + 默认 cwd(前端据此降级) |
| GET | `/api/health` | 存活(**免鉴权**,给反代探活) |
| GET/POST | `/api/sessions` | 列表 / 新建(新建写 `cwd`) |
| GET/PATCH/DELETE | `/api/sessions/{id}` | 窗口明细(`limit`/`before`) / 改名 / 删除 |
| GET | `/api/agents` / `/api/config` | agent 清单 / 模型与检查项(凭证**只回掩码**) |
| GET | `/api/skills` / `/api/plugins` | 顶层技能清单 / 已装载插件名 |
| POST/DELETE | `/api/auth/{provider}` | 写/清凭证;必须带 `X-Qi-Confirm: yes`(**428** 否则) |

三处**已移除**(契约 v1 → v2 的破坏性改动,见 §15):`POST /sessions/{id}/turn`、
`POST /sessions/{id}/cancel`、`GET /sessions/{id}/events`。取消改由**客户端断开**表达
(单 POST 之下"流就是这次响应"),所以不需要端点。

> 路由**围绕 session**、不围绕 agent(对比 §3 的草图):分派器可能中途换 agent,
> 把 run 挂在 agent 上站不住。**只有 `/api/health` 免鉴权**。

### 14.3 AG-UI 契约(前端依赖它)

参照物是**官方编码器**而不是文档:`@ag-ui/encoder@0.0.59`(发布自
`github.com/ag-ui-protocol/ag-ui`)的

```js
encodeSSE(event) { return `data: ${JSON.stringify(event)}\n\n`; }
getContentType() { return "text/event-stream"; }
```

于是三条硬约束:

```text
data: {"type":"RUN_STARTED","threadId":"…","runId":"…"}
data: {"type":"MESSAGES_SNAPSHOT","messages":[…]}
data: {"type":"STATE_SNAPSHOT","snapshot":{…}}
data: {"type":"CUSTOM","name":"qi.history","value":{"entries":[…]}}
data: {"type":"TEXT_MESSAGE_START","messageId":"msg_1","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg_1","delta":"先"}
data: {"type":"TEXT_MESSAGE_END","messageId":"msg_1"}
data: {"type":"RUN_FINISHED","outcome":{"type":"success"}}
```

1. **只有 `data:` 行,没有 `event:`、没有 `id:`。** 类型在 JSON 的 `type` 里,所以消费方
   不能用 `addEventListener("…")`,只能 `onmessage` + 分支 `type`。
2. **`RunAgentInput.messages` 带完整历史** —— AG-UI 天然是"客户端拥有状态"的模型,
   与 qi 的「session = 本地 JSONL 树」一致。因此**重连语义 = 重发一个 `RunAgentInput`**,
   而不是 `id:`/`Last-Event-ID` 续传。
3. **qi 独有概念走 `Custom{name, value}`**(AG-UI 的协议级扩展点),不污染标准事件。

映射表(实现在 `src/qi_agent/web/agui.py`):

| qi | AG-UI |
| --- | --- |
| `dispatch` / `opening` / `compaction_*` / `branch` | `CUSTOM{name:"qi.*"}` |
| `thinking_delta` | `REASONING_*`(`THINKING_*` 在 AG-UI 已弃用) |
| `text_delta` | `TEXT_MESSAGE_START → CONTENT* → END` |
| `tool_start` | `TOOL_CALL_START` + **一条** `TOOL_CALL_ARGS` + `TOOL_CALL_END`(qi 此刻已有完整参数) |
| `tool_end` | `TOOL_CALL_RESULT`(结构化结果挂 `metadata["qi.tool"]`) |
| `agent_end` | `RUN_FINISHED{outcome}`(usage 挂 `metadata["qi.usage"]`) |
| `error` | `RUN_ERROR` |

两点是 qi 自己补的:

- **`qi.history`**:`MESSAGES_SNAPSHOT` 只装"会进对话的消息",装不下
  `dispatch`/`tool`/`custom(assistant_narration)`。不给这一份,刷新之后轨迹就只剩对话
  —— 而那正是 qi 一直要避免的"直播与回放不一致"。走 `CUSTOM`,严格客户端会忽略它。
- **`RUN_STARTED` 之后紧跟快照**:AG-UI 不强制快照,但 qi 的 UI 靠它重建整屏。

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
.venv/bin/python -m pytest -q          # 322 项

# 前端
cd web
npm test                               # 28 项(vitest)
npm run typecheck                      # tsc --noEmit(含测试文件)
npm run check:design                   # 两个门禁:check-design + check-dsh-tokens
npm run check:tokens                   # 只跑 dsh 令牌保真(需要 data/deepseek-harness 或 DSH_REPO)
npm run build                          # 产出 → src/qi_agent/web/static/
```

| 层 | 工具 | 管什么 |
| --- | --- | --- |
| 色值纪律 | `scripts/check-design.mjs` | 除 `tokens.css` 外不得出现具体色值;dsh 关键色值保真;对比度**如实报出** |
| 令牌保真 | `scripts/check-dsh-tokens.mjs` | **全部 90 条** alias/specific × 2 主题逐条对拍 `design-platform.css`(§8.4) |
| 逻辑 | `web/src/**/*.test.ts`(vitest) | SSE 切帧、事件→视图归约、真实一轮的因果顺序 |
| 契约 | `web/src/api/contract.test.ts` | 版本号两侧一致、`/api/meta` 真实响应形状、**事件 kind 两侧对齐** |
| 后端 | `tests/test_*.py`(pytest) | 事件字段/落盘顺序不变量、API、并发 409、凭证闸门 |

契约守卫做过**变异验证**:在后端注入一个新的 `kind` → 测试变红并指名道姓
(`后端新增了未声明的 kind:…`),而不是默默丢掉这个事件。
它真的报过一次:TUI 线的三批提交加了 `thinking_delta` / `compaction_start` / `compaction_end` / `branch`
四个后端已在发、前端并未声明的 kind。当前这四个登记在 `IGNORED_BY_DESIGN` 里(带跟进方向),
**但前端仍未渲染它们** —— 这是真实的功能缺口,不是已完成。

测试只覆盖**纯函数**(切帧/归约/契约)——不引 jsdom 与组件快照:组件渲染的问题靠肉眼看更快,
而真正的风险集中在“事件怎么变成视图”这段逻辑上。构建产物里不会混入测试代码
(测试不被入口引用,Vite 不打包;产物体积已核对不变)。

## 15. AG-UI 改造 + 前端重做(2026-09,破坏性)

### 15.1 决定

把 `/api` 的事件契约**换成 AG-UI**,并且**推翻**之前那套照 dsh `HeroShell`/`InputBar`
逐条复刻的版式,改由自己设计。三条一起变的:

| 层 | 改前 | 改后 |
| --- | --- | --- |
| 传输 | 两跳:`POST /turn` → 202,再 `GET /events` | **单次 POST `RunAgentInput`,响应就是流** |
| 帧 | `id:` + `event:` + `data:`(命名事件) | **只有 `data:`**,类型在 JSON 的 `type` 里 |
| 续传 | `?from=<seq>` + 每代 `snapshot` 原子替换 | **没有续传**:重连 = 重发一个 `RunAgentInput` |
| 版式 | 复刻 dsh 的 hero + 输入卡 | 三栏(会话 / 工作台 / **遥测**)+ **行语言** |

### 15.2 为什么接受"没有续传"

这是本次最需要记账的取舍。单 POST 之下"流就是这次响应",于是:

- `POST /turn` 那两跳存在的唯一理由(事件可能在 POST 返回前就发出)消失了;
- `Run` 类、追加式事件日志、`seq`、`?run_id=` 重放、`POST /cancel` **一起消失**,
  `web/state.py` 从 208 行缩到 ~110 行,只剩 cwd→runtime 缓存 + 会话级忙闲闸门;
- 代价:掉线时**那次 run 的在途输出会丢**。已落盘的消息还在,重发即可继续。

AG-UI 的答案本来就是"重发 `RunAgentInput`"(它把 `messages` 放在请求体里),
所以这不是缺失,而是模型不同 —— **会话归客户端,不是归服务端**。

### 15.3 为什么保留配色令牌层

版式全部重做,但 `--dsw-*` 那 90 条**留着**:它们刚做过逐条对拍 dsh 源文件、
对比度由 dsh 验过。从零自造一套色板只会更差、且无法验证。所以
**色板当底座、版式自己设计**,两个门禁(`check-design` / `check-dsh-tokens`)继续通过。

### 15.4 版式的三条设计判断

qi 是**多 agent 分派 harness**,不是聊天软件。转录的本质是**因果轨迹**
(决策 → 思考 → 动作 → 结论),不是对称交谈。由此:

1. **行,不是气泡。** 气泡把"过程"和"结论"渲染成同类东西,结论被过程淹没。每行一种
   tone,左缘细线承担身份色,机器味儿的一律等宽 —— 读者能扫着跳过过程只读结论。
   `final`/`narration`/`opening` 三种 tone 是 qi 自己的区分,视觉权重不同。
2. **三栏,不是两栏。** 新增**遥测栏**:分派链与理由、上下文占用、动作计数、环境。
   聊天界面普遍不告诉你"它为什么这么做"和"还剩多少上下文" —— 对 harness 来说这是一等公民。
3. **活动条贴着输入框,不在页面底边。** 想知道的是"**此刻**在干什么",那信息属于手边;
   底边状态条离视线最远,却承载最需要即时可见的东西。

其余:过程可折叠、**结论不可折叠**;空态是"工作台"不是广告牌;工具行折叠态**恰好一行**;
状态不单靠颜色(带 ✓/✗)。

### 15.5 前端为什么用 discriminated union

AG-UI 把类型放在 payload 的 `type` 里,于是 `switch (ev.type)` 能被**穷尽检查** ——
漏一个分支就编译不过。旧方案是"读 SSE 的 `event:` 名 + 手写 kind 表",做不到这一点:
后端加一个 kind,前端只会**静默丢掉**(§14.7 记的那次 4 个 kind 就是这样漏的)。
`contract.test.ts` 继续做变异验证:在 `agui.py` 里加一个没声明的类型必须变红。

### 15.6 明确不做

- **不宣称"兼容 AG-UI"**:协议仍是 0.0.59、无协议级版本、无日期化冻结规范、
  CopilotKit 主导无基金会归属。借的是**形状**,不是身份。
- **不用 AG-UI 的服务端状态模型**:`previous_response_id` / `conv_...` 会让 fork 静默串历史、
  丢掉"读改压缩历史"、切 provider 就断。qi 一律 `store: false` + 每轮自己发完整历史。
- **不引客户端插件模块图**(dsh 路线):要加载器 + 版本钉 + 信任模型,而且插件仍绑死
  宿主内部组件 API。扩展性留给"数据层 `details` + 声明式 UI 词汇表" —— 尚未做,记在 §7 待定。

## 16. 插件怎么渲染:数据契约(2026-09 实现)

### 16.1 问题:`details` 曾经到不了前端

插件想显示自己的东西(todo 清单、查询结果、进度……)时,`result` 是**给模型看的散文**,
UI 不该去解析它;而 `status`/`duration_ms`/`exit_code` 只够画一个状态点。

`ToolOutcome` 本来就没有自由结构字段,而且 `runner` 里 `tool_end` 的 `data` 是**硬编码的 4 个键**:

```python
data={"status": …, "duration_ms": …, "exit_code": …, "error": …}
# 插件就算自己造了 details,也**到这一行就被扔掉** —— 而且毫无报错
```

修法是把 `details` 打通(加法,不动任何事件 kind / 端点 / schema 版本):

| 层 | 改动 |
| --- | --- |
| `models.ToolOutcome` | 加 `details: dict \| None = None` |
| `runner` → `tool_end.data` | 加 `"details": outcome.details`(就是上面那一行) |
| `runtime._persist_tool` | 落盘 `details`,并封顶 `MAX_TOOL_DETAILS_CHARS`(超限换成 `{"_truncated": True, "_full_chars": n}` —— **切 JSON 会得到非法 JSON,比截断更糟**) |
| `web/agui.py` | **不用改**:`tool_end.data` 整包进 `TOOL_CALL_RESULT.metadata["qi.tool"]` |
| 前端 | `ToolRowData.details` + 渲染器(§16.2) |

### 16.2 词汇表 `details["ui"]`(v1)

插件输出数据,宿主实现渲染。**`ui` 是可选字段**;不写它就退回"折叠显示原始 JSON"。

```json
{"ui_version": 1, "ui": [
  {"type": "list", "items": [
    {"label": "读需求", "state": "done"},
    {"label": "写实现", "state": "active", "note": "第 3 个文件"},
    {"label": "补测试", "state": "pending"}
  ]},
  {"type": "kv", "rows": [["模型", "deepseek-chat"], ["耗时", "1.2s"]]},
  {"type": "progress", "value": 2, "max": 5, "label": "已完成"},
  {"type": "code", "lang": "python", "text": "print(1)"},
  {"type": "note", "text": "只读查询,未做任何写操作"}
]}
```

| `type` | 字段 | 渲染 |
| --- | --- | --- |
| `list` | `items[].{label, state?: "done"\|"active"\|"pending", note?}` | 勾选列表(形符 ✓/▸/○,**不单靠颜色**) |
| `kv` | `rows: [键, 值][]` | 键值对(等宽值,右对齐) |
| `progress` | `{value, max, label?}` | 进度条 + `2 / 5` |
| `code` | `{text, lang?}` | 等宽证据块 |
| `note` | `{text}` | 一行说明 |

**两条硬规则**(由实现保证,也由测试锁定):

1. **不认识的 `type` 必须退回原始 JSON,不能丢弃。** 插件可能比宿主新;丢掉会让
   "插件发了东西但没人看见"变成不可诊断的问题。
2. **有 `details` 但没 `ui` → 折叠显示整个 details 的 JSON。** 所以插件即使完全
   不碰词汇表,也**永远有东西可看**。

### 16.3 为什么是数据契约,不是代码契约

三种成熟做法摆在一起:

| | 扩展点性质 | 谁渲染 | qi 是否跟 |
| --- | --- | --- | --- |
| **dsh** | **代码**:插件 UI 打进客户端模块图(真 React 组件 + slot/projection) | 宿主认识该插件 | ❌ 要引入客户端模块加载器 + 版本钉 + 信任模型,而且插件仍绑死宿主内部组件 API |
| **pi → pi-web** | **声明式方法集**:`ctx.ui.*` 经 `extension_ui_request` 桥接(9 个 method,双向) | 宿主实现固定方法集 | 部分借鉴(**已由 pi 验证可行**) |
| **AG-UI** | **事件**:`Custom{name,value}` / `ActivitySnapshot{activityType,content}` / `metadata` | 宿主按协议解释 | ✅ 借**形状** |
| **qi** | **数据**:`details["ui"]` 词汇表 | 宿主实现固定词汇表 | 本条 |

核心区别:**扩展点放在数据层,插件就永远不用改 API**。宿主加新节点类型是宿主的新特性,
所有已存在的插件立刻可用;插件用旧节点类型在新宿主上照常工作。

### 16.4 能力协商

`/api/meta` 的 `capabilities` 里:

| 键 | 含义 |
| --- | --- |
| `ag_ui` | 事件形状是 AG-UI |
| `ui_v1` | 认 `details["ui"]` 的 v1 词汇表 |

两个都缺也可以干活 —— 插件最保守的写法是只发 `details` 不带 `ui`,任何客户端都能折叠成
JSON 显示。**降级路径永远存在,所以插件不必探测宿主版本也能安全输出。**

### 16.5 只有读,没有交互

本词汇表是**只读渲染**。要让用户在面板上点一下回传(比如 todo 打勾),需要一条**上行通道**
(web → 宿主 → 插件),qi 现在**没有**。AG-UI 用 `interrupt` 做、pi 用 `extension_ui_input` 做,
都是各自协议的一部分 —— qi 要做的话是新契约,不在本次范围内。

### 16.6 插件侧怎么写

```python
from qi_agent.models import ToolOutcome

async def run(args, ctx):
    return ToolOutcome(
        result="已更新 3 项任务",          # 模型可见文本:照旧,不要塞结构化数据
        details={                          # 客户端可见:自由结构
            "ui_version": 1,
            "ui": [{"type": "list", "items": [
                {"label": t["text"], "state": t["state"]} for t in todos
            ]}],
        },
    )
```

`details` 不进 LLM 上下文(`result` 才进),所以放多大的结构都不会污染提示词 ——
但仍受 `MAX_TOOL_DETAILS_CHARS` 约束,免得会话文件无界增长。
