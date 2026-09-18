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
| GET | `/api/mcp` | MCP 声明(全局 / 项目 / agent 私有)。**只回结构不回值**:`env` / `headers` 回键名,stdio 的 `command` / `args` 不回(§18.18) |
| POST/DELETE | `/api/auth/{provider}` | 写/清凭证;必须带 `X-Qi-Confirm: yes`(**428** 否则) |

三处**已移除**(契约 v1 → v2 的破坏性改动,见 §15):`POST /sessions/{id}/turn`、
`POST /sessions/{id}/cancel`、`GET /sessions/{id}/events`。取消改由**客户端断开**表达
(单 POST 之下"流就是这次响应"),所以不需要端点。

取消的**收尾语义**(后端):客户端断开 → ASGI 取消生成器 → `CancelledError`。
`runtime.stream()` 会先就地把**已流式输出但还没落盘**的助手文本写成 message entry,
再把 `CancelledError` 抛给 ASGI —— 否则直播里用户已经看过的半截回答在刷新后就消失了
(直播与回放不一致)。这条路径无法“先收尾再取消”(服务端只观测到取消,不是信号),
所以 Web 不走 TUI 那条协作式 `AbortSignal`。

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

> ⚠️ **本节的"版式"部分已被 §17 取代**(2026-09):外壳(两栏 → 原三栏 → 回去的两栏 + 抽屉)、
> 空态(hero)、左栏(二级目录)都换回 dsh 的版式。**本节仍然有效的是** AG-UI 事件契约、
> 单 POST 取消语义、判别联合、`details["ui"]` 词汇表、以及 §15.4 的**转录行语言**
> (`final`/`narration`/`opening` 三种 tone、过程可折叠结论不可折叠)。

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

## 17. 版式回归 dsh + 左栏二级目录(2026-09)

> 状态:已实现。参照物是 **dsh 的网页面**(官方截图 `data/dsh.png` 与
> `data/deepseek-harness` 里的源码),不是转述。这次**推翻 §15 的一部分**:§15 把
> 三栏 + 遥测常驻 + 空态"广告牌 vs 工作台"那一套换掉了 dsh 的版式,本节把外壳换回去,
> 只保留 qi 自己的转录行语言。

### 17.1 决定

| 项 | §15 之前/之中 | 本节 |
| --- | --- | --- |
| 外壳 | 三栏(会话 / 工作台 / 遥测常驻) | **两栏 + 遥测抽屉**(默认关闭,打开时中栏让出 300) |
| 左栏 | 平铺会话列表(按"工作目录末段"标注) | **二级目录:项目 → 会话**(可折叠) |
| 空态 | 居中标题 + 三条 facts 胶囊 | **dsh 的 hero**:品牌标 + 26/32 标题 + 角标,下面 12px 是项目 chip,再下面 12px 是输入卡 |
| 输入卡 | 半径 18、无芯片行 | **dsh InputBar**:半径 22、上内衬 8、按钮行 `2px 8px 6px`、发送键 34px 圆 + `button-info-fill` |
| 转录 | 行语言(tone + 左缘细线) | **不变**(见 §15.4;理由见 17.4) |
| 活动条 | 常驻(空闲时写"就绪") | **只在运行时存在** |

### 17.2 「项目」= 会话的 `cwd`,前端派生

没有新后端概念、没有新契约字段:

- 会话 header 早就带 `cwd`(`session.Session.cwd`),`/api/sessions` 早就回给前端;
- `SessionStore.list()` 早就按**文件 mtime 倒序**返回;
- `POST /api/sessions` 早就支持指定 `cwd`。

于是"项目"是 `groupByProject(sessions, current)` 的派生结果(`web/src/state/projects.ts`),
两条规则写死在那里并有单测:组的顺序 = 组内最近活跃的那条在列表里的位置;`cwd` 为空的旧会话
进「未分组」(dsh 也有这一桶)。

**为什么不引入 dsh 那种持久化 workspace**(名单 + 重命名 + 排序):那是它的一份额外状态,
它连"删除工作区"都要额外解释会话会落到「未分组」。qi 取**目录即项目**:用户不必先"添加工作区",
也不会出现"会话记录在、工作区名单没了"的两份真相。代价是**不能给项目起别名、不能手工排序**——
按 mtime 排就是"最近动过的项目在最上面",这恰好是多数时候想要的顺序。

新增一个契约字段(加法,`SessionSummary.updated_at`):左栏行尾的「15分钟」必须显示
**最近活跃**,而列表顺序本来就是 mtime;用 `created_at` 会出现"排在最前却写着 3天前"。
后端由 `path.stat().st_mtime` 给出(本地时间,与 `session._now()` 同格式),文件被外部删掉时
回落 `created_at`。

### 17.3 实测证据:与 `data/dsh.png` 逐项对齐

方法同 §10.1:纯标准库解码 PNG(zlib + unfilter),量行段与矩形,**不靠肉眼**。
两侧截图都是 **1253×879 CSS @2x = 2506×1758**(与 dsh.png 同尺寸同倍率):

```bash
cd web && npm run build
QI_AGENT_HOME=/tmp/qi-shot/agent .venv/bin/qi web --no-open -p 30199 --cwd <项目目录>
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
  --force-device-scale-factor=2 --window-size=1253,879 --screenshot=/tmp/shot.png <url>
```

| 项 | dsh.png(2x 像素) | qi(2x 像素) | 结论 |
| --- | --- | --- | --- |
| 左栏右边界 | x=559(279.5 CSS) | x=559 | 一致 |
| 品牌行 | y 54–91 | y 55–91 | 一致 |
| 新会话按钮 | y 148–223,x 28–531 | y 148–223,x 28–530 | 一致(高 38、宽 252) |
| 区块标题 | y 266–293 | y 266–292 | 一致 |
| 一级行(项目) | y 345–370,标题 x=86 | y 345–370,标题 x=86 | 一致 |
| 二级行(会话) | y 414–440,标题 x=82 | y 414–440,标题 x=80 | 一致(差 1 CSS px 是字形侧边距) |
| 行尾时间 | x→518 | x→517 | 一致 |
| 第二/三条会话 | y 485–507 / 553–575 | y 484–506 / 552–574 | 一致 |
| 选中行矩形 | x 24–535,y 396–459 | x 24–533,y 396–459 | 一致(半径 8、距栏边 12) |
| hero 整块 | headline y 645–697 / 项目行 767–794 / 输入卡 824–1053 | 653–709 / 752–777 / 816–1045 | 整块中心两侧都是 **y≈424.5 CSS**,内部各行差 ≤4px |
| 输入卡 | x 810–2235(712.5×114.5 CSS) | x 821–2245(712×114) | 一致(整体右移 5px,见下) |

一处**已知且可解释**的差:**整块内容右移 ~5px**。dsh 的会话列里给滚动条留了槽
(`scrollbar-gutter`),中栏内容因此左偏 5px;qi 的中栏是普通 flex,内容严格居中。要消掉它
得复刻 dsh 那个"把滚动条推进列边"的负外边距组合 —— 收益是 5px,不做。

逐行差 ≤4px 的那几处来自**内容本身**:qi 的标题里有拉丁字母 `q`(带下伸部,墨迹更高),
dsh 的项目行是"工作区 chip + 模式 chip"两颗,qi 只有前者。

### 17.4 三条**有意**的偏离(写清代价)

1. **转录仍用行语言,没换 dsh 的"按轮"渲染**。dsh 的转录是轮粒度的(`TurnProcessNodeView` /
   `TurnTailNodeView` / `TurnNavigator`),换过去要连事件归约一起改,而且会丢掉 §15.4 那层
   "过程 vs 结论"的区分(气泡/轮卡片把两者渲染成同类东西)。**这次只对齐外壳与空态**。
2. **不做窄栏(56px)折叠形态**,所以 dsh logoRow 右侧那个 panel 开关**不放**——宁可少一个
   控件,也不放一个点了没反应的图标。窄屏(≤1024px,即 dsh 的 `SIDEBAR_AUTO_COLLAPSE`)
   只把栏宽收到 264px。
3. ~~**眉条常驻**~~ **已改**(见 17.7):空态**没有眉条**,与 dsh 一致;眉条只在有内容
   时出现,它的左右内衬对齐内容列(`max(16px, (100% - 内容列)/2)`)。
   代价写在 17.7 里:第一次发言之前没有开遥测的入口。(设置浮层不再影响这一条 ——
   它已经改成盖在应用之上的一层,见 18.17。)
4. **一级行只有"在此项目新建会话"**;二级行的重命名/删除/分叉(dsh 有 `…` 菜单)**暂缺** ——
   API(`PATCH`/`DELETE /api/sessions/{id}`)已在,缺的是 UI。

### 17.5 顺手修掉的两个既有缺陷

都不是"顺带美化",是这次对齐**必需**的:

| 缺陷 | 症状 | 修法 |
| --- | --- | --- |
| 没有根高度/去默认外边距 | `.shell { height: 100% }` 在 `height: auto` 的父级上不解析 → 外壳按内容高度收缩,左栏底部浮在页面中间;`body` 默认 8px 外边距把整个外壳从左上推开 8px(dsh.png 的侧栏左缘就在 x=0) | `html, body, #root { height: 100% }` + `body { margin: 0 }` |
| 没有滚动条皮肤 | 滚动区画 UA 原生滚动条:两套主题下都不跟色,而且**宽度不是 dsh 的 8px** → `scrollbar-gutter: stable` 白吃 15px,每一行右缘左缩 7px(实测:行尾时间 dsh 落在 x=259 CSS,qi 只有 252) | 搬 dsh `ui-theme/src/styles/scrollbar.css` 的宽度与配色(缺"指针不在这一列就不画"那条交互) |

### 17.6 验证怎么跑

```bash
cd web && npm test          # 46 项:含 projects.test.ts 的 15 项(分组/标签/相对时间分档)
npm run typecheck           # 含测试文件
npm run check:design        # 色值纪律 + dsh 色值保真(这次新增的令牌都过了)
npm run check:tokens        # 90 条 alias/specific 颜色 + **13 条字体阶梯**逐条对拍 dsh 源文件
npm run build
.venv/bin/python -m pytest -q   # 390 项(含 /api/sessions 的 updated_at)
```

视觉回归靠 17.3 那套"截图 + 量行段"复核,**不进 CI**(要 Chrome 与固定窗口尺寸);
判定表本身的数字会随 dsh 版本变化,改版式时必须重跑。

### 17.7 去掉空态的眉条 + 字体阶梯逐条对拍(2026-09,第二轮)

两条来自使用反馈的收口。都已落到代码与门禁里,不是口径调整。

#### (1) 空态没有眉条

dsh.png 的空态顶上**什么都没有**:一屏白底,居中 hero + 输入卡。qi 之前留了一条 44px 的
眉条(标题 / cwd / 遥测开关),现在**整条在空态不渲染**(有内容的会话仍然有;设置浮层不再
影响这一条 —— 见 18.17)。

它原来那两样东西本来也不该在:标题位写的是占位文案「新会话」(会话行已经叫「未命名」,
两处叫法不一),而「遥测」是 harness 的内部视角,不该出现在开场白的第一屏。

**代价(必须记账)**:有内容之前**没有开遥测的入口** —— 它的开关就长在眉条上。要把它挪到
左栏底部(与「设置」并列)只需加一行;现在保持"先给一屏干净的开场",因为遥测是
"回看这一轮为什么这么走"的东西,它的消费者天然在对话发生之后。

顺带把 hero 的垂直位置对齐了:`.hero` 补上 dsh 的
`--dsh-composer-hero-padding-bottom`(32px)—— 那个令牌早就在 `tokens.css` 里,只是一直没被用。
dsh.png 的 hero 整块中心是 y≈423.75 CSS(比视口中心高 16px = 32/2),现在两侧一致。

#### (2) 字体:阶梯本身是逐字节的,而且现在有门禁

`tokens.css` 的 `--dsw-font-*` 那段声称"照搬 dsh",实际上**漂了 6 处**,而颜色门禁一条都拦不住
(它只管 `alias`/`specific` 颜色):

| 键 | dsh | qi 之前 | 性质 |
| --- | --- | --- | --- |
| `--dsw-font-xl-24` | `600 24px/32px` | `500 24px/32px` | 字重抄错 |
| `--dsw-font-xxxs-11` | `11px/14px` | `400 11px/16px` | 行高抄错 |
| `--dsw-font-m-18` | `500 16px/28px` | —(缺失) | 整条漏抄 |
| `--dsw-font-xs-strong-13` | `500 13px/20px` | —(缺失) | 整条漏抄 |
| `--dsw-font-xxs-strong-12` | `500 12px/18px` | —(缺失) | 整条漏抄 |
| `--dsw-font-xxxs-strong-11` | `500 11px/14px` | —(缺失) | 整条漏抄 |

修法是两条一起:**值改对 + 补全 13 条阶梯**,然后把它交给门禁。`check-dsh-tokens.mjs` 现在
多跑一段"字体阶梯"对拍(`gradient-shadow-text.css` 里 13 条阶梯令牌逐条比,两个方向都比较:
qi 缺键算失败、值不同算失败)。归一化只做两件事:斜杠两侧空格、**省略的字重当 400**
(`font: 14px/22px …` 在 CSS 里等于 `font: 400 14px/22px …`,dsh 省写、qi 写全不算错,
但 500 与 600 的差别照样抓得住)。

另外它还会检查**qi 自己声明**的 dsh 字体键(目前 1 条:`--dsw-font-markdown-code-block-font-size`,
代码块证据块用)也一致 —— 否则可以"搬一半":键名搬了、值写自己的。

**为什么门禁比"再抄一遍"重要**:这次漂移是**静默**的,颜色门禁全绿、页面也看不出坏;
下一次有人新加一个字号档位时,同样的错会再犯一次。

#### (3) 转录里那几个"不在 dsh 阶梯上"的字号

修掉漂移之后,顺手把转录里几个自造档位收回 dsh 的台阶上(之前 10/11/11.5/12px 混着用):

| 处 | 之前 | 现在 | dsh 依据 |
| --- | --- | --- | --- |
| 行眉(`.row__eyebrow`) | `10px/16px` | `font: var(--dsw-font-xxxs-11)`(11/14)+ 等宽 | dsh 的 11px 档(Tag / Menu / 代码块) |
| 正文行(`.row__text`) | `14px/22px` | `var(--dsh-content-font-size)` / `calc(24px + delta)` | dsh markdown base(14/24) |
| 工具行(`.tool-line`) | `10px` | `var(--dsh-content-font-size-secondary)`(13)+ 行高 24 | dsh `ToolRow.module.css` |
| 工具名 / 参数 / 耗时 | `12px` / `11px` / `11px` | 13 / `calc(secondary − 2px)` = 11 / 11 | 同上(等宽的参数比同行低两档) |
| 证据块(`.evidence`) | `11.5px` | `var(--dsw-font-markdown-code-block-font-size)`(11) | dsh 代码块档 |

`--dsh-content-font-size` / `--dsh-content-font-size-secondary` 这两个变量也照搬了 dsh 的
**派生关系**(`gradient-shadow-text.css` 54–57 行):改一处正文字号,低一档的 secondary 跟着走,
不会出现"正文改了、工具行没改"的分叉。qi 现在没有"正文字号偏好"那个设置,但派生先摆好。

结果:qi 现在用到的字号是 `{10, 11, 12, 13, 14, 18, 26}`,全部落在 dsh 的档位集合
`{6, 10, 11, 12, 12.5, 13, 14, 15, 16, 17, 18, 20, 24, 26}` 里;唯一那个 10px 是
`.note__tag`(分隔标签,与 dsh 轨迹时间轴同一档)。

#### (4) 关掉 pi-lens 的自动格式化(`.pi-lens.json`)

**这条不是版式决定,是仓库卫生。** 本轮改动期间,pi-lens 的自动格式化把几处**与本次无关**的
文件重排了:`web/src/api/types.ts`(3 空格缩进 → 2,402 行 diff)、`tokens.css`(把多行
`light-dark()` 压成单行 —— 而那个文件的注释明说"多行是为了与 dsh 源文件逐行对读")、
`HeroMark.tsx`(2 → 5 空格)、`Rail.tsx`(→ 6 空格)、`app.css`(1 → 2 空格)。

这些都已被恢复;但每次编辑都会再发生一次,于是每一轮都在制造与主题无关的 diff。
`format: { enabled: false }` 只关**自动格式化**,lint / 类型 / LSP 诊断照旧。

代价写清楚:以后 `pnpm`/`npm` 生态的格式化要**手动**跑(仓库本来也没有 prettier/biome 配置,
风格一直是手工维护的:app.css 用 1 空格缩进、tokens.css 用 2 空格、types.ts 用 3 空格)。
若将来想恢复自动格式化,正确做法是加一份把这三套风格都表达出来的配置,而不是让工具去猜。

#### (5) 字标:左上角与 hero 都直接写「Qi Web」

第二轮反馈的第二件事。两处改动都**做减法**,不是换皮:

| 位置 | 之前 | 现在 |
| --- | --- | --- |
| 左栏品牌行 | `HeroMark`(qi 的环形标,带 hover 摆尾动画)+ 字标 `qi` | 只有文字 **`Qi Web`**(18/24 600,与 dsh `.brandName` 同一档) |
| hero 标题 | `HeroMark`(34px,带摆尾)+「说一句话,让 qi 去分派」+「多 agent」角标 | 只有文字 **`Qi Web`**(26/32 500,与 dsh headline 同一档) |

具体删掉的东西:`components/HeroMark.tsx` **整个文件**(没有任何使用点后它就是死代码;
三段 SMIL 形变路径仍在 git 历史里)、`.rail__brand` / `.hero__hitbox` / `.hero__titlegroup` /
`.hero__badge` 四条 CSS、以及 hero 标题的 flex 换行布局(只剩一行文字,`text-align: center` 就够)。

**保留的图标**(都是功能性控件,不是装饰;若也要去掉请明说):

| 位置 | 图标 | 作用 |
| --- | --- | --- |
| 新会话按钮 | 对话加号 | 按钮自身的语义 |
| 工作区标题右侧 | 三角箭头 | 全部折叠 / 展开 |
| 一级行(项目) | 文件夹(悬停换成箭头) | **二级目录的层级语言** —— dsh 就是靠它区分层级(见 `Rail.tsx` 文件头) |
| 一级行右侧 | 加号 | 在此项目新建会话 |
| 底部一行 | 齿轮 | 设置(主题开关在 2026-09 按反馈移除,见 §18.10) |
| 项目 chip | 文件夹 + chevron | 可换项目时的可交互提示 |
| 输入卡 | 加号(禁用)/ 发送箭头 | 附件(未支持)/ 发送 |

实测(与 dsh.png 同尺寸同倍率):品牌行 y 57–86(文字 `Qi Web`,x 33–164)、hero 标题
y 661–703 居中于中栏(x 中心 1532.5 = 中栏中心 766.5 CSS ✓);hero 整块中心 y≈426.75 CSS
(dsh 是 424.5)。

#### (6) 少搬了一份 dsh 的 shell 底座(`packages/client/web/src/base.css`)

反馈是"工作区那行字是不是比 dsh 小"。**量下来的结论:字号一模一样,差的是字体栈与抗锯齿**——
而这来自一个从没被搬过来的文件。

qi 的全局段落里原来只有 `html, body, #root { height: 100% }` + `body { margin: 0 }`,
而 dsh 的 shell 底座(`ui-theme` 随包分发的 `packages/client/web/src/base.css`)还有五条,
**每一条都影响文字的长相**:

| 缺失的规则 | 后果 |
| --- | --- |
| `body { font-family: var(--dsw-font-family, …) }` | `--dsw-font-family` 在 `tokens.css` 里定义得再准也没人用 → **body 吃浏览器默认字体**(zh 内容:Chrome 落 PingFang、Safari 落宋体)。换浏览器就换一套字,字号对不上是必然的 |
| `-webkit-font-smoothing: antialiased` + `-moz-osx-font-smoothing: grayscale` | 默认是**次像素**抗锯齿:macOS 上 CJK 笔画更重。同一枚「工作区」实测墨像素 **845 → 719**、深像素 **535 → 365**(dsh 是 719 / 365) |
| `text-autospace: normal` | 中日韩与拉丁之间不插空隙(不支持的引擎忽略) |
| `color` / `background` 默认值 | 默认墨色与底色 |
| `button, input, select, textarea { font-family: inherit }` | UA 样式表把 button 钉在 Arial、textarea 钉在 monospace 上,不显式继承就与周围不是同一套字体 |

**实测(同尺寸同倍率截图,纯标准库量墨迹)**:

| 探针 | dsh.png | qi 搬底座前 | qi 搬底座后 |
| --- | --- | --- | --- |
| 「工作区」墨盒 | x 33–114 y 267–292 | x 33–114 y 266–292 | **x 33–114 y 267–292** |
| 「工作区」墨 / 深像素 | 719 / 365 | 845 / 535 | **719 / 365** |
| 「HELLO」墨 / 深像素 | 669 / 542 | — | **669 / 542** |

搬完之后两边的墨迹**逐像素相同**(不是"接近")。这一条也解释了为什么前面几轮对齐
几何时没被发现:版面尺寸全都对得上,差的只是笔画粗细与字体栈 —— 只有把两边的字并排量
墨像素才看得出来。

**教训(值得写进检查表)**:对齐一个设计系统时,"令牌搬全了"不等于"用上了"。
`--dsw-font-family` 定义了却没人消费,门禁(色值/字体阶梯)也管不到它 ——
因为门禁比的是**声明**,不是**渲染**。这类"声明了但没应用"的洞,只能靠实测渲染结果来抓。

## 18. 左栏的行动作:会话(重命名 / 分叉 / 删除)与工作区(重命名 / 删除)(2026-09)

> 状态:已实现。参照物仍是 dsh——它的会话行菜单是"重命名 / 分叉 / 归档",工作区行菜单是
> "重命名 / 删除"。qi 把归档换成**删除**(qi 没有归档状态),其余同构。

### 18.1 交互

| 层 | 行尾 | 菜单项 | 是否需要二次确认 |
| --- | --- | --- | --- |
| 二级行(会话) | hover / 键盘聚焦时,行尾的**相对时间换成「…」**(dsh 也是换,不是并排挤) | 重命名 · 分叉 · 删除 | 重命名、删除要;**分叉不要** |
| 一级行(工作区) | hover / 聚焦时,「…」与「＋」一起出现 | 重命名工作区 · 删除工作区 | 都要 |
| 一级行(未分组) | 同上(只有「…」,没有「＋」) | **清除会话** | 要 |

- **菜单几何逐条对拍 dsh `ui-primitives/src/Menu.module.css`**:面板内衬 4px、圆角 20、
  `--dsw-specific-menu` 底 + `--dsw-elevation-prominent`;条目 min-height 40、
  内衬 `8px 10px`、圆角 10、字号 14/22;危险项(删除)走错误色。
- **面板是 portal 出去的**(挂 `document.body` + `position: fixed`,位置由触发按钮的
  `getBoundingClientRect()` 算出,放不下向**上翻**)。不这么做就会被侧栏的
  `overflow-y: auto` / `.rail { overflow: hidden }` 裁掉一半(dsh 的 `Menu` 也带 `portal` 参数)。
- **未分组桶只有一件事可做:清除会话**。它没有目录,所以不能改名、也没有"删除工作区"
  可言;而它的会话(早先版本写下的、header 里没有 cwd 的旧会话)不属于任何目录,
  "删除工作区"根本带不走它们 —— 不给一个批量出口的话,用户只能一条条点。

### 18.2 「分叉」沿用 CLI/TUI 的语义

`POST /api/sessions/{sid}/fork` 直接调早就存在的 `SessionStore.fork_at()`
(pi 的 fork:把当前分支复制成**新会话文件**,不是同一个文件里开叉),标题按仓库既有约定
加 `@fork`、`cwd` 继承、entry 的 id/parentId 原样带过去。**运行中拒绝**(409):文件正在被
追加,拷到的可能是半截回合。分叉完前端直接打开新会话 —— 点"分叉"要的就是接着新分支往下说。

### 18.3 工作区的重命名/删除:一份偏好 + 一个**不可恢复**的删除

§17.2 定过"目录即项目"(项目列表是会话 `cwd` 的派生,后端不维护名单)。只有一件事派生不出来,
所以补一个只有这一样东西的文件(`src/qi_agent/workspaces.py` → `~/.qi/agent/workspaces.json`):

```json
{ "version": 1, "names": { "/Users/me/src/dsh-client": "dsh" } }
```

| 动作 | 端点 | 语义 |
| --- | --- | --- |
| 重命名工作区 | `PATCH /api/workspaces {cwd, name}` | **只改显示名,目录一个字节不动**。`name` 空串 = 取消改名(恢复成目录名) |
| 删除工作区 | `DELETE /api/workspaces?cwd=…` | **连同它名下的会话一起删**(unlink 会话文件,不可恢复)。**目录本身不删** |
| 清除未分组 | `DELETE /api/sessions?scope=ungrouped` | 批量删**没有 cwd 的旧会话**。作用域是**闭集**(`Literal["ungrouped"]`):拼错或不给都 422 —— 不能悄悄变成"删全部" |

**删除为什么是"连会话一起删"**(这条与 dsh 相反,是使用反馈定的):dsh 的删除工作区保留会话、
把它们丢进「未分组」,于是用户必须再一个个删会话 —— "删工作区"这个动作本来想省掉的正是这件事。
qi 没有"归档"这个中间状态,所以删除就是删除:一次把工作区与它的会话都清掉,并在确认弹层里
把**条数**与"不可恢复"写在明面上。

这一个改动**减掉了一个概念**:早先照 dsh 做时,必须再存一个 `ungrouped` 集合压住"删过的目录
又冒出来",还要配套一个 `regroup()`(在旧目录新建会话即恢复分组)。现在会话被删光,那个目录
名下不再有会话 → 组自然不存在,于是 `ungrouped` 与 `regroup()` 一起删掉了。
**留着的死概念比没有概念更坏。**

三条实现取舍(都没有变):

1. **键就是目录,不另造 id**。项目就是目录;多一层 id 只会多一处"id 与目录对不上"的不一致。
   键必须与 `SessionStore.create()` 同源规范化(`expanduser().resolve()`)——macOS 上
   `/tmp` 是 `/private/tmp` 的软链,不 resolve 就是同一个目录两个键。**删除时也按规范化后的
   cwd 圈定会话**(`normalize(session.cwd) == normalize(cwd)`),老会话的 header 未必规范化过,
   用软链路径删同一个工作区也有测试。
2. **原子写**(临时文件 + `os.replace`),`indent=2` 便于人直接看/直接改。
3. **坏文件一律当空**:它是 UI 偏好,不是数据源;为它抛异常会让 `qi web` 整个起不来,
   代价只是"分组回到目录名"。有测试覆盖(`{ 坏 JSON` / 数组 / 错类型 / 文件不存在)。

删除还有两条护栏,三处删除(单条 / 删除工作区 / 清除未分组)**共用同一套**:

- **任一会话在运行中就整体拒绝(409)** —— 删掉正在写的文件会毁掉那一轮;
- **前端把"当前正开着的会话也被删掉"翻译成回到空态**。收尾抽成了一个函数
  (`forgetSessions(ids)`),因为漏一处就会留下一个指向已删文件的 `sessionId` ——
  下一次发言拿它去 append,报一个谁也看不懂的错。

### 18.4 顺手修掉的一个真 bug:弹层被输入卡盖住

给行尾菜单写坐标点击的验证脚本时抓到:**确认弹层的"保存"按钮点不动**。
`document.elementFromPoint()` 在按钮中心返回的是 `DIV.composer__trailing` ——
输入卡的按钮行盖在弹层上。

根因是**页面内已经有好几层的 z-index**(`.hero__projectrow` 10 / `.tele` 10 /
`.projectmenu` 20 / `.rowmenu` 100,前两个是为"项目菜单要压在输入卡上"加的),
而 `.overlay`(弹层遮罩)`position: fixed` + `z-index: auto` —— 于是它在最下面。
修法:`.overlay { z-index: 200 }`(弹层永远最上层)。

**这个 bug 影响的不是新功能**:设置页里写凭证的二次确认弹层同样点不动,长期存在,
只在有内容/空态的某些布局下才显形。是靠"用坐标点按钮"的验证抓到的 —— 用 DOM
`click()` 绕过去会一直是绿的。

### 18.5 `/api/workspaces` 是**可选**能力,缺了要降级

这一条是实测踩出来的:一个**比端点更早启动**的 `qi web` 进程(同一个 editable 安装)
配上新前端时,`GET /api/workspaces` 回 404 —— 而首屏把这次失败当成致命错误,整页变成
"Not Found (HTTP 404)" 错误屏。同一个代码库里"宿主旧、页面新"是常态(重启就好),
但**显示名与分组不该有这种权力**。

修法:首屏那一次读取单独 try/catch,失败退回 `EMPTY_OVERRIDES`(分组回到目录名)。
判据很简单 —— 它**不是数据源**:丢了它,用户看到的还是全部会话,只是工作区叫目录名、
被删过的工作区又冒出来。

实测(旧宿主 30142 + 新构建):一级行与 52 条会话正常渲染、错误横幅为空、页面错误为空,
唯一的 4xx 是 `/api/workspaces` 与 `/favicon.ico`。

> 记账:行菜单里的**写操作**(重命名/分叉/删除)仍然会因为老宿主缺端点而报错,这是对的 ——
> 那些动作真的做不了,报出来比静静失败强。分叉是本次新增的端点,最容易撞上这一条。

### 18.6 测试

```bash
# 后端(新增 11 项)
.venv/bin/python -m pytest tests/test_workspaces.py -q    # 6 项:规范化 / 坏文件降级 / 原子写
.venv/bin/python -m pytest tests/test_web_api.py -q       # +5 项:分叉(含 409/400/404)、工作区四个端点

# 前端(新增 5 项)
cd web && npx vitest run src/state/projects.test.ts       # 20 项:含"两个人工决定"的合并规则
```

端到端那一次是用真实 Chrome(CDP)跑完的:会话行 hover 换「…」→ 菜单三项 → 重命名 200 →
分叉(7 条,新会话自动打开、标题带 `@fork`)→ 删除(7 → 6)→ 工作区改名 → 工作区删除
(一级行变成 `["未分组", "qi"]`,**会话总数不变** = 6)。

### 18.7 未分组桶的「清除会话」(2026-09,使用反馈)

「未分组」= **没有 cwd 的旧会话**(qi 早先版本的会话 header 里没有 cwd,无从判断它在哪个
项目里)。它是唯一一个**不能靠"删除工作区"清掉**的桶:那些会话不属于任何目录,删除工作区
带不走它们,于是只能一条条点删除。给它一个批量出口是必要的,不是补装饰。

实测(真实 Chrome + 磁盘核对):

```text
起始  : 一级行 ["Hermit","qi","未分组"]  会话 6 条(当前选中:未分组里的那条旧会话)
菜单  : ["清除会话"]
弹层  : 标题「清除未分组的会话」| 行 ["1 条","老的旧会话(没有 cwd)"] | 确认键「清除」(危险色)
警告  : 这些会话会被一起删除,不可恢复。它们没有工作目录(…没有 cwd),不属于任何工作区
        —— 所以只能在这里清掉。
清除后: 一级行 ["Hermit","qi"]  会话 6 → 5  当前选中 = 空 → 回到空态 hero ✓
磁盘  : fff666.jsonl(未分组那条)真被 unlink;其余 5 个文件原样 ✓   页面错误 0 ✓
```

后端 `DELETE /api/sessions?scope=ungrouped` 的三个细节:`scope` 用 `Literal["ungrouped"]`
做**闭集**(拼错/不传 → 422,绝不能悄悄退化成"删全部");按 `cwd is None` 圈定;任一条在运行
就整体 409。三条都有测试。

### 18.8 区块标题的搜索与「添加工作区」+ 两个真 bug(2026-09,使用反馈)

#### (1) 搜索

区块标题按 dsh `sectionHeader` 的三段式:`[工作区][搜索][动作组]`。点搜索 → **标签与动作
一起收起**(标签 `max-width → 0`、动作组隐藏,带 180ms 过渡),输入框占满 —— dsh 也是这个
行为,不是另开一行。输入**过滤两层目录**,规则在 `filterGroups()`(纯函数,5 项单测):

1. **工作区名命中** → 该工作区下的会话**全部**保留(搜 "hermit" 想看的是那个项目);
2. 否则只留**标题**命中的会话,空掉的工作区整行消失;
3. 搜索期间**忽略折叠状态**(命中的会话被折叠着等于没搜到);无命中显示「没有匹配「x」的会话」;
4. `Esc` 关闭并清空。

**刻意不做**:不搜会话内容。那要读每个会话文件(或加后端索引接口),而左栏要回答的是
"我那条会话跑哪去了"——标题足够,而且不必等 IO。这一条写在 `filterGroups` 的注释里,
免得下一个人以为漏了。

#### (2) 添加工作区

qi 的工作区就是目录,所以「添加工作区」= **在某个目录下新建一条会话**:弹层里只有一个
目录输入(预填当前工作目录,支持 `~`),成功后就地打开那条新会话,新工作区随之出现在列表
第一行(按最近活跃排)。

**失败留在弹层里**:目录打错是常见事(实测消息:`工作目录不存在: /tmp/…/nope(HTTP 400)`),
所以这个动作**先成功再关弹层**,错误用 `.dialog__error` 显示在输入框下方 ——
不能关掉弹层、也不能把整页打成错误屏。

#### (3) 修掉的 bug A:鼠标移开后工作区不恢复文件夹图标

**症状**:hover 工作区行时文件夹换成展开箭头(设计如此),但鼠标移开后箭头**不还原**。

**根因**:规则写的是 `:focus-within`。行是 `tabIndex=0` 的 `role=treeitem`,**鼠标点一下
就一直持有焦点** → `:focus-within` 永远为真 → 箭头永远显示。改成 `:focus-visible`:
鼠标点击不触发它,Tab 聚焦会触发 —— 两种输入方式都照顾到,不留"焦点态"的假象。
行尾动作(`…` / `＋`)与行尾时间的换出同理,一起改了。

实测(Chrome/CDP,量 `display`):悬停中 `文件夹 none / 箭头 flex` → **移开后 `文件夹 flex /
箭头 none`** → 点击后移开同样 `flex / none`。

#### (4) 修掉的 bug B:`cwdDraft` 的过期闭包(填什么路径都没用)

「添加工作区」第一次跑通时发现:**在输入框里填不存在的目录,会话却建到了预填目录里**,
而且弹层"成功"关闭。根因是 `confirmDialog` 的 `useCallback` 依赖数组漏了 `cwdDraft` ——
闭包捕获的是**弹层刚打开时**的值,输入框里的改动进不了那次调用。

这条不是笔误而是**没有 lint 兜底**:仓库里没有 ESLint 跑 `react-hooks/exhaustive-deps`
(App.tsx 里那两处 `eslint-disable-next-line` 就是证据:以前靠人记得写)。修法是把依赖列全
(`cwdDraft` / `openSession` / `forgetSessions` 三个都补上),并在那里写了注释说明后果。

#### (5) 实测(全新夹具,真实 Chrome)

```text
① 图标回退
   悬停中  : 文件夹 none  箭头 flex
   移开后  : 文件夹 flex  箭头 none        ← 反馈里的 bug,已修
   点击后移开: 文件夹 flex  箭头 none
② 搜索
   展开态  : 标签 max-width 0px | 动作组 none | 输入框高 30px
   hermit (工作区名命中): 组 ["Hermit"] 会话行 3
   二级     (标题命中)  : 组 ["qi"]     会话行 1
   zzz     (无命中)     : 组 []         提示「没有匹配「zzz」的会话」
   Esc 后  : 输入框关、6 行会话全回来
③ 添加工作区
   预填    : /private/tmp/qi-shot/Hermit
   错路径  : 弹层留住,行内错误「工作目录不存在: …(HTTP 400)」,整页无错误屏
   对路径  : 弹层关闭,一级行 ["NewProj","Hermit","qi","未分组"]
页面错误 = 0
```

### 18.9 区块标题按 dsh **源码**校正(2026-09,使用反馈)

上一版我是"看着 dsh 的样子"做的,于是多了、也少了东西。反馈里那句「没有折叠按钮」点破了
关键:**dsh 的 `sectionHeader` 里根本没有"全部折叠"**。回去逐行读
`ui-workspace/src/client/rows/WorkspaceBrowser.tsx` 的 `sectionHeader`,三处偏差:

| 项 | dsh 源码 | 我上一版 | 现在 |
| --- | --- | --- | --- |
| 动作组 | `[ViewOptionsMenu, addWorkspace]` —— **没有折叠** | 折叠全部 + 添加 | **只有添加**(qi 没有分组方式可挑,所以连"视图选项"都不需要) |
| 添加的图形 | `IconProjectAddOutline16`(项目 + 加号) | `IconPlusOutline16`(普通加号) | `IconProjectAddOutline16` |
| 搜索引擎 | 收起:`IconSearchOutline16 size={14}`;展开:`size={11}` + 24px 的 × 清空钮 | 固定 16px,没有 × | 14 / 11 + × |

顺带把 dsh 的两条交互细节也补上:

- **输入框常驻**,靠 `opacity` + `pointer-events` 收起(dsh 就是这么写的)—— 条件渲染会在
  展开的两帧之间把宽度过渡打断;
- **关闭只有两条路:Esc 与 ×**(dsh 的放大镜不负责关闭,它只负责展开);展开时自动聚焦输入框。

同时把整段标题抽成了 `SidebarSection`(它自己的展开状态、输入框 ref、聚焦副作用都收在里面),
`Rail` 只留"查询串"一个受控值去过滤列表 —— 否则 `Rail` 就是"二级目录 + 两组行内菜单 +
时间刷新 + 搜索状态"四件事堆在一个文件里。

实测(Chrome/CDP,量 boundingRect 与 computed style):

```text
收起态: 搜索槽 28×28 | 搜索钮 28×28 | 添加钮 28×28 | 头部高 36
        折叠钮在吗 false | 动作组按钮数 1 | 放大镜 14×14 | 清空钮 false
        label max-width 45% | 搜索槽 max-width 28px
展开态: 容器 30px 高、圆角 10px、0.5px 描边 l4 | 放大镜 11×11
        输入框 opacity 1 / pointer-events auto | 清空钮 24×24 | 焦点 = rail__searchinput
        label max-width 0 | 动作组 display none
行为  : 搜「二级」→ 组 ["qi"] 1 行 | 点 × → 值空、槽回 28px、6 行会话全回来
页面错误 = 0
```

**教训**:前几轮我量的是"行高 / 栏宽 / 图标落点"这类**几何**,而这次错的是**控件集合**
(多了一个按钮)与**图形选择**(普通加号 vs 项目加号)。前者能靠像素实测发现,后者只能靠
读源码 —— 量像素量不出"少了一个功能",也量不出"这个图形画的是什么"。所以对齐一个产品时,
**源码清单与像素实测要各管一半**。

### 18.10 移除左栏底部的主题开关(2026-09,使用反馈)

左栏底部原来两行:**设置** 与 **主题 · auto/浅色/深色**(点一下三态循环)。按反馈把主题那行
去掉,底部只剩「设置」。

随之成为死代码、一并删掉的东西:`theme.ts` 的 `THEME_ORDER` / `themeLabel()` / `cycleTheme()`
与 `LABELS` 表、`App` 里的 `theme` state 与 `cycleTheme` 接线、`Rail` 的两个 props、
`icons.tsx` 里只服务于那一行的 `IconLightOutline16` / `IconDarkOutline16`(图标数 17 → 15)。

**主题本身没消失,只是没有 UI 入口了**:

- 开机应用一次 `applyTheme(readTheme())`(见 `theme.ts`),值来自 `localStorage['qi.theme']`;
- 没存过就是 **`auto`** —— 页面跟随系统外观(实测 `data-theme = auto`);
- 以前在 UI 里选过浅色/深色的,那个值还在 localStorage 里,继续生效;
- 想强制固定:清掉 `qi.theme` 这个键(回到跟随系统)或改系统外观。

如果以后想换个地方放它,**设置页是最自然的位置**(那里已经有模型/agent/技能/插件几节)——
但那不是"移除",所以这次没擅自搬。

实测(Chrome/CDP):底部行 `["设置"]`、行数 1、整页再无「主题」字样、`data-theme=auto`、
底部仍贴在 y=835(与原来最后一行同位置)、页面错误 0。

### 18.11 底部「设置」行按 dsh 的 `.trigger` 校正(2026-09,使用反馈)

反馈:「设置按钮高度和 dsh 不一致,而且字体颜色没有 dsh 黑」。两条都成立 —— 我那一行是照
**列表行**(项目行 34 / 会话行 32)的规矩编的,而 dsh 的底部设置行根本不是列表行:
它是 `sidebar.settings` 槽的占位者 `ui-settings-general/SettingsRoot.module.css` 里的
`.triggerRow` + `.trigger`。

| 项 | dsh `.trigger` | 我之前 | 现在 |
| --- | --- | --- | --- |
| 行高 | **42px** | 34px | 42px |
| 圆角 | **12px** | 8px | 12px |
| 内衬 | `0 10px 0 8px` | `0 8px` | 同 dsh |
| gap | 8 | 6 | 8 |
| 字色 | **`label-primary`**(`#0F1115`,近黑) | `label-secondary`(`#61666B`,灰) | `label-primary` |
| 行高(文字的) | 22px | 20px | 22px |
| 行宽 / 外边距 | `calc(100% + 4px)` + `margin: 4px -2px` | 无 | 同 dsh(悬停底色因此贴进栏的左右内衬) |
| 图标 | `IconSettingsOutline16` @16,**直接放进按钮** | 16,但套了 `.rail__slot` | 直接放进按钮 |
| `.rail__foot` 的 padding-top | 无(行自带 `margin: 4px`) | `4px` | 去掉 |

**「没有 dsh 黑」其实是两个原因叠在一起**:

1. 文字用的是 `label-secondary`(灰)而不是 `label-primary`(近黑);
2. **图标又被套在 `.rail__slot` 里** —— 那个槽是列表行的规矩,它自己的颜色是
   `label-tertiary`(`#81858C`),把按钮的 `label-primary` 盖掉了。所以即便改对文字色,
   齿轮仍然是灰的。dsh 的 `TriggerContent` 是 `<>{(wide ? <IconSettingsOutline16 …/> : …)}</>`
   —— **直接把图标放进按钮**,靠继承拿颜色。

**教训一句话**:复用"列表行的槽"会把它的语义(灰色次要图标)一起带过来。槽不是布局工具,
它是列表行的约定;别的场景要的是**继承按钮自己的颜色**。

实测(同尺寸同倍率截图,纯标准库量墨迹;dsh.png 为参照物):

```text
              墨盒                     墨像素   深像素(<90)
dsh 设置行    y 1680-1711 x 37-138     17583    10229
qi  设置行    y 1680-1711 x 37-138     18070    10393      ← 墨盒逐像素相同,墨量差 ~1.6%(抗锯齿取整)
dsh 齿轮区    y 1680-1711 x 37-66       6072     4260
qi  齿轮区    y 1680-1711 x 37-66       6450     4381
```

computed style 也逐项等于 dsh:`height 42px`、`border-radius 12px`、
`padding 0px 10px 0px 8px`、`color rgb(15, 17, 21)`、`font 14px/22px`、图标 `16×16`。

### 18.12 「添加工作区」改成目录选择器(服务器路径,形状照 pi-web)(2026-09,使用反馈)

反馈:添工作区不该手打路径,要有选择目录的弹窗(参考 pi-web);而且初始页面输入框上方的
**项目 chip 菜单里也要有**这个入口(与 dsh 一致)。

**dsh 与 qi-web 的关键差别**:dsh 是 Electron 桌面应用,「添加工作区」弹的是**操作系统**的
文件选择器;qi-web 是**服务端**,前端碰不到用户的磁盘。所以这条路只能走服务器路径浏览 ——
参照物是同样服务端形态的 **pi-web**。

#### 后端:`GET /api/fs/dirs?path=…`

形状直接照 pi-web 的 `GET /api/cwd/browse`(实测自
`@agegr/pi-web/.next/server/app/api/cwd/browse/route.js`):

```json
{ "path": "/Users/me/src", "parent": "/Users/me", "home": "/Users/me",
  "roots": [], "entries": [{ "name": "dsh", "path": "/Users/me/src/dsh" }] }
```

| 取舍 | 为什么 |
| --- | --- |
| **只列目录,不列文件** | 选择器的用途是"在哪建会话",文件名只是噪音;少回一类信息就少一类泄露面(pi-web 的文件列表在另一个端点,qi 不需要) |
| `~` / `~/x` 展开 + `realpath()` | 否则同一个目录会被点出两种写法,而 qi 的"项目"按 `cwd` **字符串**分组 —— 两种写法就是两个项目(macOS 上 `/tmp` vs `/private/tmp` 必然发生) |
| 符号链接指向目录**算**目录 | 开发者常把源码放在软链后面;断链跳过,不让整次列举失败 |
| 错误分开回 | 不存在 **404**、不是目录 **400**、没权限 **403**(dsh/pi-web 也是这么分的;403 是 qi 加的,比 500 诚实) |
| `parent` 在根上为 `null` | 前端据此禁用「转到上级目录」 |
| `roots` 只在 Windows 有盘符 | 照抄 pi-web 的 `drives` |

**安全**:它**只读、只列目录**,不读文件内容、不写、不删,权限面比已有的 `bash` 工具
(能跑任意命令)小得多 —— 所以沿用同一道 `guard`(回环 + 可选口令),没有另造信任模型。
有一条专门测"它必须过鉴权"的用例:不能因为"只读"就免鉴权。

#### 前端:`DirPicker`(弹层壳仍是 `ConfirmDialog`)

交互词汇照 pi-web 的中文串:**目录路径 + 转到** / **转到上级目录** / **主目录** /
**正在加载目录…** / **没有子目录** / **选择此文件夹**。三条细节:

- 点一行就**进入**该目录;**确认键选的是"当前所在目录"**,不是列表高亮项;
- 路径打错时**保留当前列表**并就地报错(路径栏旁边),不把浏览状态清空、不关弹层;
- 失败也可能发生在"选完目录之后"(比如目录刚被删):所以 `onPick` 的契约是
  `Promise<string | null>` —— `null` = 成功并关闭,字符串 = 失败并显示在弹层里。

#### 项目 chip 菜单

菜单末尾加**分隔线 + 「添加工作区…」**(dsh 的 `menu.addWorkspace` 同位置),点它打开同一个
选择器。注意 chip 的既有语义没变:**会话一旦建好就变成静态回声**(`disabled`),所以这个入口
出现在"还没有会话"的首屏 —— 正是这次要的地方;左栏标题栏的 ＋ 则在任何时候都能用。

#### 实测(真实 Chrome,全新夹具)

```text
① chip 菜单 = ["Hermit/private/tmp/qi-shot/Hermit", "qi/private/tmp/qi-shot/work/qi", "添加工作区…"]
   从 chip → 选择器:标题「选择目录」/ 确认键「选择此文件夹」/ 初始目录 = 当前项目
   Hermit 是空目录 → 提示「没有子目录」
② 目录路径 + 转到 /tmp/qi-shot → 子目录 ["agent","Hermit","NewProj","work"]
③ 点行进 NewProj → 上级键可用;转到上级目录 → /private/tmp/qi-shot;主目录 → /Users/hanwei
④ 选择此文件夹 → 建会话在 /private/tmp/qi-shot/NewProj 并打开;一级行 ["NewProj","Hermit","qi","未分组"]
⑤ 错路径 → 弹层留住,行内错误「目录不存在」,列表仍在
页面错误 = 0
```

测试:后端 `tests/test_browse.py`(7 项:只列目录/排序/软链/点目录/`~` 展开/父目录/盘符)+
`tests/test_web_api.py` 的 4 项(响应形状、默认主目录、404/400、**必须过鉴权**)。

### 18.13 模型标签:输入卡只显示模型名(2026-09,使用反馈)

反馈:输入卡右下的模型标签带着 provider 前缀(`commandcode/deepseek/deepseek-v4.1-flash`),
太长了显示不下。实测:完整标签在 13px 下要 ~240px,而 `.composer__model` 的上限是 220px ——
必然被省略号截断。

**修法是把"展示名"与"完整标签"分开,而不是让前端去切字符串**:`provider/model` 是后端拼的
(`ResolvedModel.label`),而模型 id 自己**也可能带斜杠**(`deepseek/deepseek-v4.1-flash`),
前端按第一个 `/` 切会把模型 id 切坏。所以 `ConfigView` 多回一个字段:

| 字段 | 值 | 用在哪 |
| --- | --- | --- |
| `default_model`(原有) | `provider/model` 完整标签 | 诊断面:遥测抽屉的「模型」、`/api/config` 的检查项 |
| `default_model_name`(新增) | **裸模型名**(可能仍含斜杠) | 展示面:输入卡右下那一行 |

输入卡的标签是**只读回声**,完整标签移到 `title`(悬停可见)—— 同 id 不同 provider 时靠它分辨。
前端取值的写法是 `default_model_name || default_model`,所以**老宿主(没这个字段)不会显示空白**,
只是回到"带前缀、可能被截断"的旧行为。

实测:`输入卡 = "deepseek/deepseek-v4.1-flash"`(191px,放得下)、
`tooltip = "commandcode/deepseek/deepseek-v4.1-flash"`、遥测抽屉仍是完整标签。
后端测试:`test_config_exposes_bare_model_name_for_display`。

### 18.14 输入卡工具行:加号 = 调用指令,回形针 = 附件(2026-09,使用反馈)

**这一节先记一次我搞错的经过,因为教训比结论值钱。**

第一版我读的是仓库里那份 dsh 源码(`ui-conversation/InputBar.tsx` + `apply.ts`):
那里**只有一颗可见的 `+`**,label 是「添加文件或调用指令」、`aria-haspopup="listbox"`,
旁边是 `hidden` 的 `<input type="file">`,而"添加文件"是**菜单里的一行**,图标是回形针。
于是我做成"一个加号打开菜单,附件是菜单里的一行"。

用户纠正:**加号直接就是调用指令,回形针在它右边**。回去量 `data/dsh.png` —— 用户是对的:

```text
dsh.png 输入卡按钮行(y 993-1021 设备像素,2x)左起墨迹分组:
  x 843-866   CSS 中心 427.2  宽 12   ← ①
  x 924-945   CSS 中心 467.2  宽 11   ← ②(间距 40 = 28px 圆钮 + 12 gap)
  x 1004-1029 CSS 中心 508.2  宽 13   ← ③
  x 1040-1193 文字「工作区内修改」(它的权限 chip —— qi 没有这个功能)
```

**三颗 28px 圆钮**,不是一颗。也就是说**截图的版本比仓库副本新**:加号已经只管指令,
自附件分到了右边的回形针。两边冲突时**以截图为准** —— 用户看的是它。

（我第一版还有一个错:把"我在仓库副本里读到的事实"当成了"dsh 长什么样"。仓库副本只说明
*某一个版本*的实现;**参照物是用户给的那张图**。.)

#### 现在的实现

| 位置 | 内容 | 依据 |
| --- | --- | --- |
| 工具行左 ① | **加号**,label/title = **「调用指令」**,打开命令菜单 | 截图;label 沿用 dsh 的词 |
| 工具行左 ② | **回形针**,附件。qi 没有上传端点 → **置灰 + title「附件:qi 暂不支持」** | 截图 ② 即回形针;qi 能力有限,如实置灰而不是藏起来 |
| 命令菜单 | **新会话** · **分叉当前会话**(无会话时置灰)· **设置** | 三条都映射到已实现的调用(`App.tsx` 的 `runCommand`) |
| 工具行左 ③ | —(dsh 是权限 chip「工作区内修改」,qi 没有权限体系) | 不做假 chip |

几何实测:两颗圆钮 `28×28`、`border-radius 999px`、底色 `--dsw-specific-selector`,
**中心间距 40**(dsh 实测 40.0);整体中心 432.5 vs dsh 427.2 —— 差的 5px 是中栏那处
已知的滚动条留白(§17.3)。

`RowMenu` 仍然是两处菜单的共用实现(行尾「…」与输入卡「+」),只是触发件类名/图形/
对齐方式/条目 `disabled|note` 可配。

### 18.15 输入卡的指令菜单:点「+」或输入「/」都弹出(2026-09,使用反馈)

反馈:点加号要**直接弹出指令列表**;在输入框输入 `/` 也要弹;实现与样式照
`data/deepseek-harness/`。

读的是 dsh 的三个文件:`ui-input-trigger/MenuView.tsx` + `MenuView.module.css`(菜单本体,
combobox 形态)、`ui-commands/presentation.ts`(分节与行的字段)、`ui-commands/locales.ts`(文案)。

#### 从 dsh 抄来的三条硬规格

| 项 | dsh | 说明 |
| --- | --- | --- |
| 位置 | `position: absolute; bottom: calc(100% + 4px); left: 0; right: 0` | **它属于输入卡那张卡片**:贴着上沿、与卡片同宽(实测 712 = 卡宽),不是挂在按钮下面的浮层 |
| 外观 | `padding: 4px; border-radius: 20px; background: --dsw-specific-menu; box-shadow: elevation-prominent` | 与行尾「…」菜单同一套菜单皮 |
| 行 | `[图标16] 标题 /别名 …………… 描述(右对齐)`,min-height 40、`8px 10px`、圆角 10、14/22 | 描述右对齐是 dsh 注释里的原话:"让标题读成一列" |
| 分节 | 节标题 12/18 500、三级字色、`padding: 6px 10px 2px`,与上一条不同才出现 | dsh 只有两节:「添加」与「指令」 |
| 交互 | **combobox**:焦点始终在输入框,菜单只画高亮行(`aria-activedescendant`);行在 `onMouseDown` 上取(`preventDefault` 保焦点) | ↑↓ 移动、Enter 执行、Esc 关掉 |

#### qi 的指令表(只列真能做的)

| 节 | 标题 | 别名 | 说明 | 状态 |
| --- | --- | --- | --- | --- |
| 添加 | 附件 | `/file` | 把文件加进这一轮 | **置灰**(qi 无上传端点) |
| 指令 | 新会话 | `/new` | 新建一条会话 | ✓ |
| 指令 | 分叉 | `/fork` | 从当前分支复制出新会话 | ✓(无会话时置灰) |
| 指令 | 压缩 | `/compact` | 把旧消息摘要掉,腾出上下文 | ✓(无内容时置灰) |
| 指令 | 设置 | `/settings` | 打开设置页 | ✓ |

`/compact` 是这一轮**新加的能力**(不只是菜单项):后端补了
`POST /api/sessions/{sid}/compact`(复用早就存在的 `runtime.compact_session()`,运行中 409);
前端执行后重取会话明细,于是压缩追加的那条 `compaction` entry 会出现在转录里 ——
否则用户点了"压缩"看不到任何变化。三条命令都对应已实现的调用,**没有占位**。

#### 触发方式

- **点 `+`**:弹全部(高亮第一行);再点收起。
- **输入 `/`**:只在**整段就是一个 `/token`** 时弹(`/^\/(\S*)$/`)—— 普通句子里出现的 `/`
  不弹。继续打字即过滤(标题/别名/描述三处,大小写不敏感);Esc 关掉但**保留输入内容**。
- **无匹配**:显示「没有匹配「x」的指令」;此时 Enter **发送**这段文本(斜杠串被当成普通消息,
  与 dsh 同处理)。

#### 实测踩到的两个真 bug(都不是设计问题)

1. **点 `+` 后 ↑↓/Enter 全无效**:焦点在按钮上,键盘事件根本没进到菜单。dsh 那颗加号上有
   `onMouseDown={keepFocus}` —— 照做。
2. **改完仍然无效**:`preventDefault` 只保证"不抢走焦点",而**首屏本来就没人持有焦点**(
   `document.activeElement` 是 `body`)。所以打开菜单时必须**主动** `input.focus()`。
   (实测症状:ArrowDown 后高亮仍停在第一行,Enter 什么也没发生。)

#### 实测(真实 Chrome/CDP)

```text
① 点 + → 菜单 712×276,与卡同宽;离卡片上沿 4px;圆角 20px
   节 = ["添加","指令"];5 行(附件/新会话/分叉/压缩/设置),含别名与右对齐描述
   附件行 disabled,描述尾缀「暂不支持」;压缩在空会话上尾缀「还没有可压缩的内容」
② ↑↓ 移动高亮 → Enter 执行「新会话」→ 会话数 6 → 7,菜单关闭
③ 输入 "/" → 菜单弹出(5 行);"/for" → 只剩「分叉」;Esc → 关闭且保留 "/for"
   "/zzz" → 「没有匹配「zzz」的指令」
④ 输入 "/compact" → 只剩「压缩」;Enter → 输入框清空,请求 200 /api/sessions/ddd444/compact
页面错误 = 0
```

### 18.16 指令菜单的三个真 bug + 指令表扩容(2026-09,使用反馈)

反馈三条:指令太少、菜单"透明且和底层文字重合"、`/settings` 没效果。三条都成立,而且
前两条**同一个根因**。

#### (1) 菜单被"项目 chip 行"盖住 —— 这既是"透明重合",也是"点不动"

`elementFromPoint` 直接给出了答案:

```text
「设置」行的中心点上,最上层元素 = DIV.hero__projectrow     ← 不是那一行按钮
```

早先为了让**项目 chip 的下拉**能压在输入卡上,我给 `.hero__projectrow` 加了 `z-index: 10`、
`.dock` 加了 `z-index: 1`。于是指令菜单(它当时留在卡片里)被困在 `.dock` 的层叠上下文里,
被 z-index 10 的 chip 行盖住:视觉上就是"菜单透明、chip 的文字透出来",鼠标点下去落在 chip 行上
—— **`/settings` 因此完全没反应**(键盘路径走文本域的 Enter 是好的,所以此前只测键盘时没发现)。

修法与 dsh 一致(它的菜单统一挂在顶层 `overlayLayer`):**菜单 portal 到 `body`**,
用输入卡的 `getBoundingClientRect()` 算 `left/width/bottom`(仍是"与卡片同宽、贴卡片上沿 4px"),
窗口 resize 时重量。这样它不再受任何祖先 z-index 影响。

顺带把 ARIA 做对:`aria-activedescendant` 挂在**输入框**上(combobox 形态,**焦点在输入框**),
listbox 只留 `role="listbox"` + `id` —— 挂在不可聚焦的 listbox 上既不合规范,也点不亮高亮。

#### (2) 键盘移动高亮时没把行滚进视野

12 条指令 + `max-height: 400px` 必然要滚动,而**高亮是虚拟的**(焦点始终在输入框,
浏览器不会替你滚)—— 不滚的话 ↑↓ 能把高亮移到看不见的行上。dsh 的 `MenuView` 是显式
`scrollIntoView({ block: "nearest" })`,照做。

> 这个 bug 是**测试脚本先撞上的**:它按 boundingBox 去点第 11 行(滚动区外),点到了别处 ——
> 于是"点了没反应"。用户遇到的是同一回事(用鼠标点下半部分的指令)。

#### (3) 动作失败不该把整页打成错误屏

`/copy` 在无头环境里剪贴板被拒(`NotAllowedError`),而我当时 `setFatal` 了 —— **整个工作台
被替换成错误屏,连输入框都没了**。这是处理不对:剪贴板失败、导出失败、压缩被 409 拒,
都是"这一步没成",不是"应用坏了"。三处一律改成**追加一行 note** 把原因写出来,界面照常可用。

#### (4) 指令表:5 条 → 12 条,而且每条都真能用

qi 的 TUI 有 28 条命令。web 参考它补齐了**能真做**的,做不了的一条都不放
(终端专属的 `/quit` `/clear` `/hotkeys`;要新能力的 `/tree` `/thinking` `/mode` `/agent`
`/reload` `/import`):

| 节 | 指令 | 别名 | 实现 |
| --- | --- | --- | --- |
| 添加 | 附件 | `/file` | 置灰(qi 无上传端点) |
| 指令 | 新会话 | `/new` | 已有 |
| 指令 | 分叉 | `/fork` | 已有 |
| 指令 | 压缩 | `/compact` | 本轮新增端点 |
| 指令 | 重命名 | `/name` | 复用会话行的改名弹层 |
| 指令 | 导出 | `/export` | 本轮新增端点(下载 JSONL) |
| 指令 | 复制回答 | `/copy` | `navigator.clipboard` |
| 指令 | 会话信息 | `/session` | 本地 note行 |
| 指令 | 最近会话 | `/sessions` | 本地 note 行 |
| 指令 | Agent | `/agents` | 设置页 + 指定分区 |
| 指令 | 帮助 | `/help` | 本地 note 行(目录的纯文本视图) |
| 指令 | 设置 | `/settings` | 设置页 |

指令目录搬到了 `web/src/commands.tsx`(**单一真相**,菜单渲染与 `/help` 文本共用);
本地反馈用 `state/turn.ts` 新增的 `withNote()` —— 复用既有的 `note` 行,不新增行类型、不进 LLM 上下文。

#### (5) 顺带修掉的导出文件名 bug

`Content-Disposition` 是 latin-1:**标题里有中文就会抛 `UnicodeEncodeError`**(端点 500)。
按 RFC 5987 双写(`filename="qi-<id>.jsonl"` + `filename*=UTF-8''<百分号编码>`),
前端优先读后者。有一条专门用中文标题测的用例。

#### 实测(真实 Chrome/CDP)

```text
菜单 = 12 行 / 两节 ["添加","指令"] / 别名 [/file /new /fork /compact /name /export /copy /session /sessions /agents /help /settings]
/help     → note「可用指令 |【添加】/file 附件 — …【指令】/new 新会话 — …」
/session  → note「会话信息 | 会话 ddd444 标题 整页外壳对齐 dsh 目录 /private/tmp/qi-shot/Hermit 条数 6」
/sessions → note「最近会话 | 新会话 · aaa111 …」
/copy     → note「复制失败 | NotAllowedError…」        ← 无头环境剪贴板被拒:界面照常可用(修好前是整页错误屏)
/export   → note「已导出会话 | 整页外壳对齐 dsh-ddd444.jsonl」  ← RFC 5987 文件名解出来了
鼠标点「设置」→ 设置页 = true                          ← z-index bug 修好前这里是 false
页面错误 = 0
```

### 18.17 设置页重做成 dsh 的面板(2026-09,使用反馈)

反馈:「qi web 设置页面参考 deepseek-harness 重新优化一下」。这次不是"再对齐几个像素"——
设置页是**唯一一处没有照 dsh 结构做**的面,它自己的注释里就写着「保留原有两栏结构
(它已经够用,这次不动)」。这次动了,并顺手挖出三个真缺陷。

参照物:`packages/client/ui-settings-general/src/client/SettingsRoot.module.css`(figma 501:29947)
的**面板壳** + `ui-settings-models/src/client/ModelsSection.module.css` 的**内容语汇**。

#### (1) 形状:页内两栏 → dsh 的浮层面板

下面的数字都是 **Chrome/CDP 实测的 computed 值**,不是照设计稿编的:

| 项 | dsh | qi 现在 | 实测(1440×900) |
| --- | --- | --- | --- |
| 面板 | 800 × `min(800px, 100vh - 48px)`,r32,`bg-layer-2`,`elevation-prominent` | 同 | `800×800 @ (320,50)`,radius 32px,`#fff` / `#2c2c2e` |
| 遮罩 | `bg-mask-1` + `--dsw-mask-blur`,点它关闭 | 同 | `rgba(0,0,0,0.24)` + `blur(2px)` |
| 导轨 | 188,内衬 `22 12 0`,gap 18,无自有底色 | 同 | `188 @ x=320`,pad `22px 12px 0px` |
| 导轨标题 | 16/500 lh24,内衬 `0 12` | 同 | `500 16px/24px @ x=332` |
| 导航格 | 164×40,r12,内衬 `9 16 9 12`,gap 4,选中 `sidebar-nav-item-active` | 同 | 6 格全 `164×40` r12 |
| 内容头 | 54 高(dsh 那里有 `.actions` 槽) | 同,只为关闭键 | `612×54 @ y=50` |
| 关闭键 | 28×28,r28,14px 字形 | 同 | `28×28 @ (1078,70)` |
| options | 内衬 `0 24 24`,自己滚,节宽上限 720 | 同 | `612×746`,pad `0px 24px 24px` |

内容语汇也换成 dsh 的:节标题 16/500、引言 14/22 `label-tertiary`、卡片行是
**0.5px `border-l4` 描边 + r16**(不是实心底 —— qi 的面板底色已经是最浅一档,再铺一层实心
就看不出卡片边界)、行内动作是 28 高 r14 胶囊、状态用 **8px 圆点 + `aria-label`**
(不是 `✓/✗` 文本:颜色只是加强,读屏与色盲用户拿到的是同一句话)。

节表里只有 qi **真有**的那几节(模型 / Agent / 技能 / 插件 / 会话与路径 / 诊断),取值来自
`/api/config`、`/api/agents`、`/api/skills`、`/api/plugins` —— 不把 dsh 有而 qi 没有的节
(沙箱 / 审批 / 遥测开关)搬过来当假控件。导航图标按节选语义(与 dsh 的 `navIcon()` 同规矩),
`图标` 逐字节取自 dsh `ui-primitives`(新搬 5 个:Data / Personalization / Skill / Gauge / Close)。

#### (2) 三个真缺陷

**(2.1) 设置页占的是工作台的位置。** dsh 的设置是 `position: fixed` 的浮层,底下那屏原样不动;
qi 原来把它当"另一个页面"塞进 `work__body`,于是打开设置 = **当前会话与空态 hero 都被替换掉**,
关掉才回来。现在挂在 `shell` 这一层(左栏也归它盖),`Esc` / 右上角 × / 点遮罩三条关闭路径。

**(2.2) 每一行「标签:值」都是裸 DOM。** 这类行的类名是 `.row__label` / `.row__value`,
而 `app.css` 里**只有 `.rowline__*`**(`grep` 计数 0,从来没人用过)—— 于是设置页里那些行一直是
"标签和值挤在一起、没对齐、没字号差"。这次统一成 `.kv`:标签 `label-tertiary` 12/18、值
`label-primary` 等宽右对齐;多行绝对路径 `pre-line` + `break-all`,否则会把面板撑出横向滚动。

**(2.3) 两处文案写错了资源路径。** 技能空态原文是"放到 `~/.qi/agent/skills/` 或项目 `.qi/skills/`",
插件引言是"`~/.qi/agent/plugins/<名>/plugin.py`"。按代码逐条核对
(`paths.py` 的 `top_level_skill_dirs`、`registry.py` 的 `_iter_plugin_loaders`)后改成:
**技能** = `~/.qi/agent/skills/`、项目 `.qi/skills/`、跨工具 `.agents/skills/`;**插件** = 全局
`~/.qi/agent/plugins/<名>/plugin.py` 或项目 `<项目>/.qi/plugins/<名>/`(需信任)。

#### (3) 验证时撞出来的两个真 bug(都已修)

**(3.1) 一个 `Esc` 关掉了两层。** 凭证写入的二次确认开在面板**之上**。我最初把 `Esc` 写成
"document 上一监听,读 `editing` 状态决定关哪层" —— 实测结果是**弹层和面板同时关掉**。

根因不在状态判断,在**React 的离散事件同步冲刷**:`ConfirmDialog` 自己的 `onKeyDown` 在 React 里,
它在事件到达 `document` 之前就已经把 `setEditing(null)` 冲刷完、弹层卸载、我那个 effect 重建 ——
等事件冒泡到 `document` 时,跑的是**重建后**的监听器闭包(读到"没有弹层"),于是顺手把面板也关了。
修法:**捕获阶段**监听(`addEventListener(..., true)`),它在 React 之前跑,读到的才是按键按下
那一刻的层数。实测 `Esc#1 → {dialog:false, panel:true}`,`Esc#2 → panel:false`。

**(3.2) 焦点没还给触发器。** dsh 的面板关掉时把焦点还给左栏那个「设置」行(`triggerButton`)。
我最初用 `document.activeElement` 记"打开前焦点在哪" ——**macOS 上鼠标点按钮不聚焦**,记下来的是
`<body>`,于是关掉后焦点落回 body(键盘用户得重新 Tab 穿过整个左栏)。改成 `App` 持一个
`settingsTriggerRef` 传给 `Rail` 的按钮、再传给 `Settings` 的 `returnFocusTo`。

> 顺带给那行补了 `aria-haspopup="dialog"` / `aria-expanded` —— dsh 的 `.trigger` 本来就有,qi 漏了。

#### (4) 实测(真实 Chrome/CDP,`elementFromPoint` + computed style)

```text
面板      = 800×800 @ (320,50)  radius 32px  bg #fff / #2c2c2e(深色)
遮罩      = 1440×900  rgba(0,0,0,0.24)  backdrop-filter blur(2px)
导轨      = 188 宽 / pad 22px 12px 0px;标题 500 16px/24px @ x=332
6 格导航  = 模型 / Agent / 技能 / 插件 / 会话与路径 / 诊断,全 164×40 r12(选中=模型)
options   = 612×746  pad 0px 24px 24px;节标题 500 16px/24px
卡片行    = 564 宽,0.5px border-l4;凭证圆点 8px,aria-label「commandcode 已配置凭证」
行内动作  = 28 高 r14 胶囊(「设置凭证」「清除」)
逐节切换  = 模型 1 卡 / Agent 5 卡 / 技能 1 表(临时放了个探针技能,验完即删)/
            插件 空态 / 会话与路径 2 行 / 诊断 3 行
溢出      = 六节都无横向溢出(Agent 节纵向滚动 —— 这正是面板固定高度的理由)
二次确认  = elementFromPoint(弹层中心) 命中 .dialog,焦点在 .dialog__input(没被面板压住)
Esc#1     = {dialog:false, panel:true}                       ← 只关弹层
Esc#2     = panel:false,activeElement=.rail__footrow,aria-expanded=false   ← 关面板 + 焦点归还
页面错误  = 0
```

#### (5) 明确没做的

- **没加 dsh 有、qi 没有的节**(沙箱/审批/遥测开关/个人化):qi 没有这些能力,放上去就是假控件。
- **没搬 dsh 的"文档级动作"槽**(`.actions`):qi 的设置里没有这类动作,头部只有关闭键。
- `--dsw-mask-blur` 进了 `tokens.css`,但它和 `--dsw-elevation-*`、`--ds-ease-*` 一样
  **不在颜色门禁的对拍范围**(`check-dsh-tokens.mjs` 只管 alias/specific 与字体阶梯)。
  值是从 dsh `gradient-shadow-text.css` 逐字抄的 `blur(2px)` —— 但没有门禁替你盯着它漂移。
- **没做焦点陷阱**(focus trap)。面板是 `aria-modal="true"`,但 `Tab` 仍能从面板末尾
  走到后面那屏(与 `ConfirmDialog` 现状一致 —— 全应用都没有陷阱)。要做应该统一做。
- **像素级视觉复核没做**:本次的证据是 DOM 几何 + computed style + 0 页面错误(截图见
  验证时的 `/tmp/qi-set-*.png`)。人眼复核仍应补一次。

#### (6) 验证怎么跑

```bash
cd web && npm run typecheck && npm test     # 56 项
npm run check:design                        # 色值纪律 + dsh 色值保真
npm run check:tokens                        # 90 条颜色 + 13 条字体阶梯逐条对拍
npm run build
# 浮层/几何要真实浏览器(不进 CI):
.venv/bin/qi web -p 30199 --no-open --cwd .  # 另开 chrome --headless=new --remote-debugging-port=9333
```

### 18.18 设置页加一节 MCP(2026-09,使用反馈)

反馈:「设置的技能栏下面再添加一个 MCP 吧」。加了一节,但真正的工作量不在 UI ——
**qi 当时根本没有"全局/项目 MCP"这一层**,只有 agent 目录里的私有 `mcp.json`。

#### (1) 先把缺的那一层补上

原来 MCP 只有两个来源:agent 私有 `mcp.json`,和文档里写着的 `qi_agent.toml [mcp.servers]`
—— 后者**从来没有被实现**(`grep -rn "toml" src/qi_agent` 只命中 pyproject)。

按这次的要求落成两个 JSON 文件,三处**共用同一个解析器**(`loader.read_mcp_file`):

| 来源 | 位置 | 可见性 |
| --- | --- | --- |
| 全局 | `~/.qi/agent/mcp.json` | 按 agent 声明绑定 |
| 项目 | `<git根>/.qi/mcp.json` | 按 agent 声明绑定(同名覆盖全局) |
| 私有 | `<agent>/mcp.json` | **仅该 agent**,自动绑定 |

`mcp_servers`(frontmatter)从"解析了但没人用"变成真的绑定规则。两条硬规则:**同名项目覆盖全局**;
**未知名报错**(不静默跳过 —— 声明了却指不到东西,就是"这个 agent 以为自己有 github、其实没有",
比启动失败难查得多)。例外:`import` 校验传 `mcp_table=None`,那时"要装到哪"未知,只校验形状。

> 顺带把 `docs/agent-config.md §8` 与 `README` 里那句 `[mcp.servers]` 改成了真实位置 ——
> 文档之前描述的是一个不存在的实现。

#### (2) 设置页的 MCP 节

位置按反馈放在**技能下面**(导航 = 模型 / Agent / 技能 / **MCP** / 插件 / 会话与路径 / 诊断)。
内容按三处来源分组,每组一个文件头(层标签 + 绝对路径);每个 server 一行卡片:
名字 + 传输类型 + **绑定关系** + 目标 + env / headers 键名。

三处来源里**没有**的东西写清楚:没有"已连接 / 健康"状态 —— v1 没有 MCP client(PLAN.md),
那种字段只能是编的。

两处 UI 判断值得记:

- **「无人绑定」是真结论,不是加载失败**。全局表里写着一个 server、而没有任何 agent 声明它,
  是正常状态(文件写在那儿等着谁来声明);这个标签直接来自"哪个 agent 的 `mcp_servers` 里有它"。
- **同名覆盖要在界面上标出来**。全局那张 `github` 卡挂一个「被项目同名覆盖」标签 ——
  否则两行都写着「绑定:analyst」,读者会以为两个都生效。

#### (3) 脱敏:只回结构,不回值

口径(按反馈选的最严那档):`env` / `headers` **只回键名**;stdio 的 `command` / `args`
**一律不回**(只回 `stdio` 这个事实)。理由:mcp.json 的 env 值**不像** data_sources 的 dsn
那样被强制 `{env:XXX}`,手写明文完全可能;而 `args` 是最容易写成 `npx -y xx-mcp --token=sk-…`
的地方。与"凭证只回掩码"同一条规矩。

实测(真实宿主,故意塞了明文进去;7 个探针全部 0 命中):

```text
GET /api/mcp 含 "sk-GLOBAL-LEAK-CHECK" / "postgres://" / "pg-mcp" / "mcp-fs"
             / "@playwright/mcp" / "npx"  = 全部 false
渲染后的页面上同样 0 命中
```

#### (4) 实测(真实 Chrome/CDP)

```text
导航格 = ["模型","Agent","技能","MCP","插件","会话与路径","诊断"]   ← MCP 紧跟技能;164×40 r12
节标题 = MCP(5)                     ← 三处来源加起来 5 个 server
● 全局  /tmp/…/home/mcp.json
    github      streamable-http [被项目同名覆盖] 绑定:analyst  目标=https://api.githubcopilot.com/mcp/
    playwright  stdio           [无人绑定]       目标=stdio; env 键名=PW_TOKEN; headers 键名=Authorization
● 项目  /tmp/…/proj/.qi/mcp.json
    github      streamable-http 绑定:analyst      目标=https://project.example/mcp/
    postgres    stdio           绑定:analyst      目标=stdio; env 键名=PG_PASSWORD
● agent 私有 · analyst  /tmp/…/agents/analyst/mcp.json
    local-fs    stdio           自动绑定          目标=stdio
options 横向溢出 = false,纵向滚动 = true;页面错误 = 0
```

一条**顺带量出来的事实**:卡片行写的是 dsh `.rowCard` 同款的 `border: 0.5px`,但这台 Chrome 把
任何 <1px 的边框宽度都归整成 1px(0.25 / 0.3333 / 0.5 实测 computed 全部 `1px`)。声明与 dsh
逐字相同,所以两边渲染一致;但"发丝线"这个说法在 Chrome 下不成立,别按它推断视觉。

#### (5) 没做的

- **MCP 实连**(stdio / http 真连上、`tools/list` 拿工具)仍未做,PLAN.md 的待办照旧。
- **没有把声明表合并后再显示**:页面上全局与项目**各自**列一遍,而不是只显示生效的那份 ——
  这是刻意的:设置页要回答"我写了什么",覆盖关系用标签标出,而不是替用户合并掉。
- **设置页不能编辑 mcp.json**(只有凭证明文那一处可写,且过二次确认)。加编辑器是另一件事:
  它要处理明文凭证扫描、写盘竞态与信任边界。

#### (6) 验证怎么跑

```bash
.venv/bin/python -m pytest -q tests/test_mcp_plugin.py tests/test_web_api.py   # 11 + 38
.venv/bin/python -m pytest -q                                                 # 427
cd web && npm run typecheck && npm test && npm run check:design && npm run build
```
