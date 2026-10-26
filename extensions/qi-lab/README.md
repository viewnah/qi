# qi-lab

qi 的 **Web UI** 扩展:HTTP 宿主(REST + SSE)+ AG-UI 桥 + 官方界面。

> ⚠️ **当前状态:qi-web 的副本 + 第一处功能性分化**(迁移基线,2026-10)。整包由
> `extensions/qi-web` 复制而来,只改了**身份**这几处:包名 `qi_lab` / 发行名 `qi-lab` /
> CLI 子命令 `qi lab` / entry point 名 `lab` / 环境变量 `QI_LAB_*` / **默认端口 `3456`**(4 位,
> 与 qi-web 的 30142 错开,可与 `qi web` 并排跑)。
>
> 已分化的内容:**MCP 声明的增删改**(见下节)。其余(字标「Qi Web」、`WebState`、契约号 2)
> 与 qi-web **逐字相同** —— 后续分化逐条改。

它原本住在 core(`src/qi_agent/web/`,1762 行);P-E5 ③ 按"**core 一点不留**"的决定搬出来 ——
core 不再拖 fastapi/uvicorn,`qi lab` 这个入口也由本扩展自己提供(经 core 的
`registerCliCommand` 面)。见 `design/extensions-design.md` 的 E25。

## 装

```bash
qi install qi-lab        # 或 -e ./extensions/qi-lab(开发)
```

装完 `qi lab` 才有这条子命令 —— 没装时它不是"报错",而是**不存在**(core 里已经没有它了)。

## 用

```bash
qi lab                        # 默认 127.0.0.1:3456
qi lab -p 4321 --no-open      # 换端口、不开浏览器
qi lab -H 0.0.0.0 --password 口令
```

参数由本扩展自己解析(argparse):core 只把**命令名之后的原始 argv** 交给 `serve()` ——
选项表是静态的,动态改 typer 的选项表会踩坑(E19 的实测结论)。

安全默认(见 `security.py`):默认只绑回环;绑 `0.0.0.0` 不再强制口令,
也不再用 `QI_LAB_ALLOWED_HOSTS` 卡局域网 IP(绑定通配符 = 放行任意 Host)。
要对外提供服务时,建议自行加 `--password` / `QI_LAB_PASSWORD`(口令不加密传输,
对外请走 HTTPS 反代或 VPN)。

## 已知未收口

前端源码(`ui/`)与构建产物(`qi_lab/static/`)都随包带上了(与 qi-web 同一套)。但字标还写着
「Qi Web」 —— 分化时记得连 `ui/index.html`、`ui/src/App.tsx`、`ui/src/components/Rail.tsx`
一起改,再 `cd ui && npm run build`(Python 侧是 editable 安装、改完即生效;前端改完**必须**
重新 build 才进 `static/`)。

## 会话导出 / 导入

导出在 qi-web 就有(`GET /api/sessions/{sid}/export`,原样发 JSONL);qi-lab 把导入也补上了。
界面上两条入口:

- 导出:左栏会话行「…」里的「导出」,或输入卡 `/export`;浏览器直接下载 `<标题>-<id>.jsonl`。
- 导入:输入卡 `/import`;选一个 `.jsonl` 文件。

导入是 `POST /api/sessions/import`,请求体是**文件全文** + 可选标题。三处**必须**改写
(其余原样保留):

| 改写 | 为什么 |
| --- | --- |
| `header.id` → 新 id | 导两次不会撞车;也不信文件里那个可能是路径成分的串 |
| `header.cwd` → 宿主固定工作目录 | 列表只列 `default_cwd` 下的会话,不改写就会“导进来了但左栏看不见” |
| 去掉 `header.parentSession` | 那是源机器上的文件路径,留着只会指到一个不存在的文件 |

entry 的 `id` / `parentId` **原样保留**;v1 线性文件补链。导入**只新增文件**,
不覆盖任何已有会话,所以与新建 / 分叉同一口径 —— **不要** `X-Qi-Confirm`。
坏文件 400 **且一个文件都不建**;上限 32 MiB / 200 000 条。
回归测试:`tests/test_lab_session_import.py`;契约在 `docs/api/sessions.md`。

## MCP 声明的增删改(相对 qi-web 的第一处分化)

qi-web 的设置页里 MCP 节是**只读**的(它只有 `GET /api/mcp`)。qi-lab 把写也补齐了:

| 方法 | 路径 | 干什么 |
| --- | --- | --- |
| `POST` | `/api/mcp` | 新建一个 server(`scope` = global / project / role,201) |
| `PATCH` | `/api/mcp` | 改一个 server:**只改送出去的键** |
| `DELETE` | `/api/mcp` | 删一层声明(同名的下层会露出来) |

三条共同的规矩:

- **要确认**:`X-Qi-Confirm: yes`(缺 → **428**),与写凭证同一道闸;并且都绑在 `guard` 上
  (开着口令就要口令);
- 返回**更新后的 `McpList`**(结构视图)—— 方便 `curl` 写完直接看到结果;
- 写的是 `qi_mcp.config.scope_files()` 给出的**那一层文件**,不碰 `load_layers()` 的合并规则:
  新建只拒绝**同层**里已有的名字(跨层同名是合法的,那正是覆盖关系);
- 原子落盘(临时文件 + `os.replace`),新建文件 0600;保留顶层其它键(如 `$schema`);
- **值只进不回**:env / headers 只给键名,stdio 的 `command` / `args` 连给都不给(见 `GET /api/mcp`)。
  所以界面里那几个框在编辑态是「**留空 = 不改**」—— 看不见的值不能因为保存一次就没了。

界面(设置 → MCP):每层一个「添加」,每个 server 有「编辑 / 删除」;确认弹层里先列出
**目标文件的绝对路径**再让你按确认,表单没填完时确认键是灰的,错误也报在弹层里
(顶上那条 notice 被遮罩挡着)。能力位 `mcp_write` 读自 `/api/meta`,老宿主没有 → 那一节自动
回到只读。

回归测试:`tests/test_lab_mcp_write_api.py`(宿主侧:201/400/404/428、只改送来的键、值不回显)、
`ui/src/components/McpPanel.test.ts`(表单侧:留空不改)、`ui/src/api/client.test.ts`
(两侧字符串对齐的变异验证)。

## 技能的增删改(第二处分化)

qi-web 的 `/api/skills` 只有**读**;qi-lab 把写也补齐了(设置页目前只接了「重新扫描」,新建/删除/导入还是接口先行):

| 方法 | 路径 | 干什么 |
| --- | --- | --- |
| `POST` | `/api/skills` | 新建 `<scope 层>/<name>/SKILL.md`(201) |
| `DELETE` | `/api/skills` | 按**显示名**删掉一层技能(整目录,200) |
| `POST` | `/api/skills/import` | 从本机路径导一个技能进来:目录 / 单份 `.md` / `.zip`(201) |

| `POST` | `/api/skills/reload` | 重扫技能目录(只刷缓存,不改文件) |

- **为什么有 `reload`**:`GET /api/skills` 与系统提示词读的都是 `runtime.top_skills`
  (按 cwd 缓存的快照),而缓存只在**写端点**里重扫。所以"没经过本宿主"的变更(手工拷目录、
  另一个进程/CLI 装的、改了 frontmatter)在页面上看不见,**刷新浏览器也没用**(那只是再发
  一次同一个 GET)。TUI 有 `/reload`,这是它的 web 对应物;设置面「技能」节有一颗
  「重新扫描」按钮(它不写文件,但仍带确认头 —— 保住"系列 POST 一律带确认头"这条规矩)。
  商店装/卸 **skill** 时会自动调它,不需要手点。

- **只可写两层**:`scope=global` → `~/.qi/agent/skills`、`scope=project` → `<cwd>/.qi/skills`;
  `.agents/skills` 与 settings 的 `skills[]` **不碰** —— 跨工具的家 + 用户指定的路径,
  边界一句话:**我建的我才删**(与 MCP 写端点同一套词汇);
- **三不变量**(每条都有测试钉着):没 `description` 就 400(扫描器看不见它 = 写了个寂寞);
  落点名与源的 `name` 不一致时**只改 name**(否则"目录叫 A、显示叫 B",按显示名删就找不到);
  **同层重名 409**(跨层同名是合法覆盖)。导入**不覆盖**已有技能;
- **要确认**:`X-Qi-Confirm: yes`(缺 → **428**),并绑在 `guard` 上;三个端点都回更新后的 `SkillList`;
- **写完立刻生效**:`QiRuntime` 按 cwd 缓存 `top_skills`,所以落盘后 `refresh_skills()` 重扫一次 —
  少了它就会出现"`GET /api/skills` 说它在、模型却用不上";不走 `reload()`,不惊动扩展、不重启活动会话;
- **zip 两条防御**:成员名带 `..`/绝对路径 → 400(`extractall` 会解到目标目录外面)、
  200 个成员 / 8 MiB 上限;软链接条目直接丢;
- 能力位 `skill_write` 读自 `/api/meta`,老宿主没有 → 界面把技能节当只读。

回归测试:`tests/test_lab_skill_api.py`(35 条);文档与路由的双向对齐由
`tests/test_lab_docs_api.py` 钉住,契约在 `docs/api/skills.md`。

## 角色的删除(第三处分化)

qi-web 只把角色**列**出来;qi-lab 多给一个删除端点,设置页的 Agent 一节也跟着有键:

| 方法 | 路径 | 干什么 |
| --- | --- | --- |
| `DELETE` | `/api/agents` | 按**显示名**删掉某层的一个角色(**整目录**,200) |

- **词汇跟 qi-agents,不跟技能**:`scope` 只取可写的 `user` / `project`(就是读模型的
  `source` 与界面角标上的词),传 `global` → **422**;`builtin` 是随 qi-agents 包发布的
  内置角色,读端点会列出来,但不归这个端点删(按两层删都是 404);
- **为什么只有删、没有建**:角色的正文是提示词、`tools` / `model` 是有语义的限制,
  写入格式与校验归 qi-agents;而"删掉一个放错的角色"是边界清楚的文件操作;
- **不自己拼路径**:`name` 是**查找键**,目标由 `discover()` 给出,与读端点同一条代码
  路径 —— 于是 `../` 只会 404;查到后落刀前再 `resolve()` 查一次包含性(符号链接 →
  400,理由里带两个路径);
- **整目录 + 只删该层**:角色目录里还有它自己的 `mcp.json`,只删 `agent.md` 会留下
  "看不见但还在生效"的孤儿声明;删 `user` 层时 `project` 同名的会**露出来**而不是被
  一起带走;
- **不用 refresh**:技能写完要 `refresh_skills()`(runtime 缓存了 `top_skills`),角色
  不进那个缓存 —— 每次派单现 `discover()`;
- **要确认** `X-Qi-Confirm: yes`(缺 → **428**);能力位 `agent_write` 没有 → 界面把
  Agent 节当只读(读端点一直在);没装 qi-agents → 读 `unavailable=true`、删 **503**。

回归测试:`tests/test_lab_agent_write_api.py`(17 条);契约在 `docs/api/agents.md`。

## A2A 条目(第四处分化)

qi-web 没有这块;qi-lab 把 [qi-a2a](../qi-a2a) 的两层 `a2a.json` 接上了
(读 + 增删改 + 名片探测):

| 方法 | 路径 | 干什么 |
| --- | --- | --- |
| `GET` | `/api/a2a` | 逐层原样 + **深合并后的生效视图** |
| `POST` | `/api/a2a` | 在某层新建一个条目(201,需确认头) |
| `PATCH` | `/api/a2a` | 改一个条目:**只改送出去的键** |
| `DELETE` | `/api/a2a` | 删一层里的条目(同名的下层会露出来) |
| `POST` | `/api/a2a/probe` | 去真实端点拉一次名片(只读,无需确认头) |

三条与 MCP 共用同一套规矩(确认头 / 只写读端指的那一层 / 值只进不回),
A2A 自己的三件:

- **只有两层**(`global` / `project`),没有角色私有那层;
- **合并是深合并**(数组替换、`null` 删键),所以读端多回一份 `resolved` ——
  否则界面上看到的永远是“某层写了什么”,而不是“现在到底连的是哪”;
- **校验看合并后的生效结果**:项目层只写 `{"tenant": …}` 是合法的(端点由全局层给),
  只验这一层会把它误判成“缺端点”;
- **`PATCH` 的 `null` = 不改**(与 MCP 一致):读端不给 `headers` 的值,把 `null` 当
  “删掉”会让人一保存就丢 token。要清掉整张表就送 `{}`;
- **探测只收已配置的条目名**,不收任意 URL —— 否则这个端点就成了内网探测器。
  失败也是 `200` + `ok=false`(那是**结果**,不是宿主异常)。

回归测试:`tests/test_lab_a2a_api.py`(35 条);契约在 `docs/api/a2a.md`。

**界面**:设置页 → **A2A**(与 MCP / 技能同一节的形式)。上面是深合并后的
**生效条目**,下面是逐层的原始声明;每张卡带一个「探测」,结果就地展开(探测是
只读的,弹层会挡着列表)。表单里 `headers` 是"留空 = 保持不变 / 填 `-` = 清空整张表"
——读端给不出值,所以只能这样表达。

## 商店(设置页 → 扩展 → 商店)

把 **hikqin** 当应用商店:`qi-store` 扩展负责取货 / 转换 / 落盘 / 记账,qi-lab 只做**代理与界面**
(进程内懒 import `qi_store.service`,与 `/api/mcp` 消费 `qi_mcp.config` 同一套)。

**按四类标签**摆,每个标签只放它那一类(能装的 + 已装的):**Agent / 技能 / MCP / A2A**。
Agent 标签暂时只说明"为什么不提供"(hikqin 的模板蓝图与 qi 的角色目录格式没对齐,
且蓝图里的 `model`/`api_key` 是剥离状态),不摆一个永远空着的列表。

| 口径 | 做法 |
| --- | --- |
| 浏览器不直连 hikqin | `/api/store/*` 全在宿主进程里转发(不依赖上游 CORS) |
| 令牌不进浏览器 | `POST /api/store/login` 收账号密码 → 交上游 → 只回 `StoreStatus`;磁盘上是用户级 `store.json`(0600) |
| 登录 / 退出也是写操作 | 与其它写端点同一条 `X-Qi-Confirm: yes` 闸 |
| 计划先行 | 点「安装」先 `POST /api/store/plan`,确认层里显示**写哪里 / 冲突 / 待补 env** |
| 少装扩展不是 500 | `qi-store` 不在 → `available=false` + `error`;`/api/meta` 的 `store_write` 是真去 import 一次的结论 |
| 卸载只删自己写的 | `mcp.json` / `a2a.json` 只删那一个键,文件空了才删文件 |

契约在 [docs/api/store.md](docs/api/store.md);回归测试 `tests/test_lab_store_api.py`。

## 知识库（设置页 → 知识库）

把 **hikqin 知识库**接成 qi-kb 的 provider/model：账号 = provider，库 = knowledgeBase。
qi-lab 只做**代理与界面**（进程内懒 import `qi_kb.service`，与商店同一套）。

| 方法 | 路径 | 干什么 |
| --- | --- | --- |
| GET | `/api/kb/status` | provider / 已绑定 / 默认参数 |
| GET/POST/DELETE | `/api/kb/providers` | 账号列表 / 新增 / 删除（写要确认头） |
| POST | `/api/kb/providers/login` | 登录（密码只过一次） |
| POST | `/api/kb/plan` | 只算不写：install / update / rebind |
| GET | `/api/kb/available` | 可用 KB + 已绑定 |
| POST | `/api/kb/install` / `uninstall` | 装 / 卸（写要确认头；冲突 409） |
| POST | `/api/kb/search` / `ask` | 检索 / 上游 RAG 问答 |
| POST | `/api/kb/ask/stream` | 流式 RAG 问答（SSE） |
| GET | `/api/kb/files/{provider}/{kb_id}/{file_id}` | 稳定的源文件下载/预览 |
| PATCH | `/api/kb/defaults` | 默认参数（写要确认头） |

三条口径：

- **账号不在 `store.json`**：安装 KB 时从 qi-store 的 source 复制到 qi-kb 自己的
  `kb.json` / `kb-auth.json`；之后 `qi store logout` 不影响已装知识库；
- **同一个 `(instance, kbId)` 只保留一份**：重复安装先 `plan`，`blocked=true` 时
  前端弹「保留 / 覆盖为当前 provider」；覆盖 = rebind，不复制文档；
- **令牌不回显**：响应只有登录态；写操作过 `X-Qi-Confirm`。

**测试控制台的「流式问答」**：边生成边显示 token，最后追加可点击的 sources（文件名 → 源文件）；可随时停止。

**回答正文下方的来源**：如果本轮调用了 `kb` 工具，回答正文下面会有一条分割线 +
「来源」chip（文件名 + 分数）；点击会在**右侧文件面板**打开预览（PDF / 图片 / HTML 等），
不跳浏览器新页。历史回放同样显示。正文里的
`[来源N]` 会按 sources 的 `index`（没有就按数组顺序）变成指向同一文件的链接；
模型自己写的 `来源：[1][2]` 行在有结构化 sources 时会被清掉，避免和下方 chip 重复。
普通 `[N]` 只有在 sources 里存在对应编号时才会转成链接。
来源来自 tool entry 的 `details.sources` / `details.results`，旧历史只有 `file_ref`
时也能还原链接。

**对话页的知识库 chip**：输入卡右下角可以只勾选本轮要查的已绑定 KB。它通过
`forwardedProps.kbIds` 传给宿主，宿主把选择记进当前会话的进程内 scope；清空 = 回到
`kb.json` 的全局/项目绑定。刷新页面或换会话不会落盘。

契约在 [docs/api/kb.md](docs/api/kb.md)；回归测试 `tests/test_lab_kb_api.py`。

### 对话页面的「智能体」chip 也能选远端角色

`/api/agents` 与 `subagent` 用的是**同一份角色表**,所以 A2A 条目会自动出现在那个
下拉里(`source` 显示成 `a2a`)。选中之后走的是**另一条执行路径**:

| | 本地角色 | 远端角色 |
| --- | --- | --- |
| 怎么生效 | `pin_role` 把角色正文拼进系统提示词 | **整轮转发**给远端(它没有本地正文) |
| 帧 | 本地 runner 的 delta / 工具事件 | 远端的 `artifactUpdate` **逐片转发**成 `TEXT_MESSAGE_CONTENT` |
| 落盘 | `runtime.stream` 自己写 | 自己写 user + assistant 两条 |

第二行是这次修的那个 bug 的根:`pin_role` 只会拼提示词,而远端角色的 `prompt` 是空串
——于是 chip 显示着 `skill-debug`、实际跑的还是基座模型,**一个会说谎的控件**。
判据在 `serve.remote_role_for()`,与 `/api/agents` 同一份数据,不会出现"界面能选、
这里认不出"。

**流式**:A2A 的 `artifactUpdate` 本来就是增量的,所以远端轮**边收边发**
(`TEXT_MESSAGE_CONTENT` 一片一帧),而不是等整轮跑完再贴上 —— 后者在长回答上就是
"界面空着十几秒,然后突然出现一大段"。桥接用一条 `asyncio.Queue`:`run_remote` 是
协程,`_remote_turn` 是 async generator,两者之间只能这样转。非流式远端(一个增量都
没有)回落到 `run.text` 整段发一次。

远端轮**不带** `qi.usage` 元数据(没有 token 计量),否则前端会显示"用量未记录" ——
那句提示是为"本地轮跑了但没写 usage"的旧会话准备的。

回归测试:`tests/test_lab_a2a_agent_pin.py`(8 条:分流判据、会话落盘、失败转 `RUN_ERROR`)。
流式那一段的用例在同一个文件里(增量一帧一片、非流式兜底、落盘用聚合全文)。

另有一条**零构建**的兜底页面 **`/a2a`**(原生 JS,不依赖 `npm run build`):
它住在 `qi_lab/pages/`,**不是** `qi_lab/static/` —— 后者是 Vite 的 outDir
(`emptyOutDir: true`),下一次构建会把不属于它的文件一起清掉。
前端产物缺失或还没构建时,这条路径仍然可用。口令与主界面一致:
用 `http://user:口令@host:port/a2a` 打开,或在页顶框里填(存 `sessionStorage`,不落盘)。

## 接口文档

**`docs/api/`** —— 72 条 `/api` 路由的完整契约(请求体、响应字段表、状态码、curl 例子、
写端的规矩与坑):

| 文件 | 内容 |
| --- | --- |
| [`docs/api/README.md`](docs/api/README.md) | 索引 + 通用约定(口令/Host/`X-Qi-Confirm` 确认头/错误形状/能力位) |
| [`docs/api/meta.md`](docs/api/meta.md) | `/api/meta`、`/api/health` |
| [`docs/api/sessions.md`](docs/api/sessions.md) | 会话:列表、明细、改名、删除、导出、导入、压缩、分叉 |
| [`docs/api/ag-ui.md`](docs/api/ag-ui.md) | 运行入口:POST → SSE 的帧格式与事件表、取消语义 |
| [`docs/api/mcp.md`](docs/api/mcp.md) | MCP 声明(全局 / 项目 / 角色私有)的读与增删改 |
| [`docs/api/a2a.md`](docs/api/a2a.md) | A2A 条目:两层深合并、确认头、`null` = 不改、名片探测 |
| [`docs/api/agents.md`](docs/api/agents.md) | 角色:读(只回元数据)与删除(两层、按显示名、整目录) |
| [`docs/api/config.md`](docs/api/config.md) | 配置/模型/扩展只读视图、凭证写入 |
| [`docs/api/skills.md`](docs/api/skills.md) | 技能:读(六层来源)与添加 / 删除 / 导入 |
| [`docs/api/store.md`](docs/api/store.md) | 商店:hikqin 目录 / 登录 / 安装 / 卸载 |
| [`docs/api/kb.md`](docs/api/kb.md) | 知识库:provider / KB / 检索 / 问答 / 默认参数 |
| [`docs/api/workspaces.md`](docs/api/workspaces.md) | 工作区显示名与目录选择器(**兼容保留;UI 固定 cwd 后不再调用**) |
| [`docs/api/files.md`](docs/api/files.md) | 文件三端点:边界、排序、限额、预览白名单 |

没有 `/docs`(Swagger 关着)—— 这份就是文档。**改端点的人顺手改文档**:
`tests/test_lab_docs_api.py` 拿 `create_app()` 注册的路由双向比对,
漏写一条、写一条不存在的,都会红。

## 与基座的接口(2026-09 重新接上 / 补全)

这几处此前**都在静默空转或压根不存在**(不报错、只是不生效),现在各有回归测试钉住:

1. **会话绑定**。`WebState.bind()` 每轮开跑前调 `runtime.bind_session()` +
   `start_session()`。没有它:换模型/级别没有落盘点、续会话不还原模型、
   `session_start` **从不派发** —— 而 qi-mcp 的直连工具正挂在那上面
   (`directTools` 在 web 端从未注册过)。见 `tests/test_lab_api.py`。
2. **「智能体 chip」**。前端送的 `forwardedProps.agent` 现在走 `serve.pin_role()` →
   `before_agent_start`,把那个角色的正文拼进本轮系统提示词。**不再**用
   `stream(agent_override=…)`:那是 P-E4c 之前的接口,core 收窄成单 agent 之后
   流水线里已无人读它。见 `tests/test_lab_extensions_api.py`。
3. **模型面**(`/api/models` + `/api/model`)。此前 web **只有 `/api/config` 的只读回声**:
   界面能显示模型、却换不了(TUI 有 `/model` / ctrl+l / ctrl+p)。现在补上清单与切换,
   清单口径与 TUI 的 `/model` **同一份**(core 的 `selectable_models()`,即
   **解析得出凭证的 provider 才列**:缺凭证的 provider 一条都不进菜单,要配凭证去设置页),
   切换走 `runtime.set_model()` / `set_thinking_level()`(事件与落盘都在里面)。
   输入卡右下那个只读的模型名因此变成了一个真 chip(`ModelMenu`)。

> 这三条都是"**一个 runtime 服务多条会话**"才会显形的形态 —— 而 web 正是这个形态
> (`WebState.runtime_for` 按 cwd 缓存)。补第 3 条时顺手在基座里挖出三处
> **静默串会话**的记账错误(切回会话不还原 / 级别跨会话泄漏 / 还原失败时留在上一条
> 会话的模型上),详见 `design/web.md` §18.29 与 `docs/session-format.md` §6.2。

## 测试(在扩展自己的目录里)

```bash
pytest extensions/qi-lab/tests      # 从仓库根:只跑这个扩展
pytest                              # 从仓库根:全量(根 tests/ + 各扩展自带的 tests/)
cd extensions/qi-lab && pytest tests   # 从扩展内也行(根 ini 会向上被找到)
```

用例全放在 **`extensions/qi-lab/tests/`** —— 根 `tests/` 是宿主自己的地盘,
扩展的用例不该搬去那里(扩展搬不走,根目录也被越堆越长)。有两件事一改就会忘:

- **`pyproject.toml` 的 `testpaths` 要同时登记** `extensions/qi-lab/tests`,
  否则 pytest 压根不收集它 —— 不报错,只是那一批测试静默不跑;
- **conftest 按目录生效**,根 `tests/conftest.py` 覆盖不到子目录,所以本目录自带一份
  同源精简版 `tests/conftest.py`(entry-point stub、`~/.qi` 隔离、`minimal_config`)。
  改根 conftest 的隔离逻辑时**两边一起改** —— 与 `extensions/qi-agents/tests/conftest.py`
  是同一个道理、同一种风险。

| 文件 | 测什么 |
| --- | --- |
| `tests/test_lab_agent_write_api.py` | 角色:读(含 `opening.groups` / `opening.suggestions`)与删除(确认闸、两个层、整目录、按显示名、符号链接 400、`agent_write` 能力位、没装 qi-agents → 503) |
| `tests/test_lab_api.py` | API 契约:会话 CRUD、AG-UI 线格式 + 落盘回放、口令与 Host 允许表、凭证二次确认闸 |
| `tests/test_lab_session_import.py` | 会话导入:新 id / cwd 改写、v1 补链、标题覆盖、坏文件 400 不落盘、大小上限 |
| `tests/test_lab_browse.py` | 目录浏览的纯逻辑(`~` 展开、软链、排序、只列目录) |
| `tests/test_lab_docs_api.py` | 文档 ↔ 路由**双向**对齐(`docs/api/` 少写一条、多写一条都红) |
| `tests/test_lab_extensions_api.py` | 扩展面:智能体 chip 走 `before_agent_start`、角色列表 |
| `tests/test_lab_files.py` | 文件三端点:边界、排序、限额、预览白名单 |
| `tests/test_lab_mcp_write_api.py` | MCP 声明的增删改:201/400/404/409/428、只改送来的键、值不回显 |
| `tests/test_lab_skill_api.py` | 技能的增删改 + 导入:确认闸、两个可写层、三不变量、重扫、zip 防御 |

## 端到端怎么验

```bash
python scripts/e2e_ui_lab.py     # 真 Chrome/CDP:模型 chip 的可点性、菜单层叠、切换落盘
```

需要本机装了 Google Chrome(headless 起临时实例,不动你的 profile)。
