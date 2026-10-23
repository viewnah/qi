"""qi-lab API 的请求/响应模型(`/api` v1 契约)。

契约版本(`CONTRACT_VERSION`)是 UI 与宿主的依赖面:前端在 `/api/meta` 里读到它,
形状不兼容时**显式报错**而不是白屏(见 design/web.md §3 的"契约语义版本化")。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: AG-UI 改造是**破坏性**的:事件形状、传输形态(单 POST)、续传机制全变了,
#: 所以升到 2。前端此刻仍声明 "1" → 会走它自己的显式报错分支(而不是白屏),
#: 这是**刻意**的过渡状态:报错比坏页面好。前端跟上后两侧一起改成 "2"。
CONTRACT_VERSION = "2"

APP_NAME = "qi-lab"


class Meta(BaseModel):
    """宿主自述:能力与默认值。前端据此降级,不靠猜。"""

    app: str = APP_NAME
    contract: str = CONTRACT_VERSION
    version: str = "0"
    default_cwd: str = ""
    auth_required: bool = False
    static_ready: bool = False
    capabilities: dict[str, bool] = Field(default_factory=dict)


class SessionCreate(BaseModel):
    """新建会话。工作目录由宿主固定,请求体里不再接受 `cwd`。"""

    title: str = ""


class SessionRename(BaseModel):
    title: str = Field(min_length=1)


class ModelOption(BaseModel):
    """模型菜单里的一条:`provider` + 模型 id。

    **不给**"是否当前"这种状态:当前是**会话级**的(同一 runtime 服务多个会话),
    `/api/config` 的 `default_model` 说的是"settings 里的默认",两者不是一件事。
    界面上"现在是哪个"应当来自它自己刚刚设的那个值(见 `ModelView`)。
    """

    provider: str
    id: str
    #: 上下文窗口(tokens,来自 models.json)。0 = 取不到 → 界面不画占用条。
    context_window: int = 0


class ModelCatalog(BaseModel):
    """`GET /api/models` —— 可选模型清单(与 TUI 的 `/model` **同一份口径**)。

    口径是**能用**的模型:解析得出凭证的 provider 才在里面(`selectable_models`)。
    所以不需要“这条有没有凭证”的字段 —— 清单里每一条都有。缺凭证的 provider 想去配
    看设置页的 provider 卡片(`/api/config` 那份带 `credential_ok`)。

    `currently` = 这次请求的会话**当前生效**的模型(可从会话里还原过,未必等于 settings
    默认)。`session` 为 None 时它取自 runtime 的当前状态。
    """

    models: list[ModelOption] = Field(default_factory=list)
    #: `provider/id`;空 = 没有可用模型(界面据此禁用菜单,而不是画一个空面板)。
    currently: str = ""
    #: 当前生效的思考级别。
    thinking_level: str = ""
    #: 思考级别的合法取值(界面照它画那一行,不自己写死一份)。
    thinking_levels: list[str] = Field(default_factory=list)


class ModelSelect(BaseModel):
    """`POST /api/model` —— 换模型 / 换思考级别。

    两个字段都是可选的、且**可以只给一个**:这让"只换级别"和"只换模型"各是一条请求,
    不必为了改一个值而把另一个也传上来(那会覆盖掉会话里刚还原的值)。
    `provider` + `model` 必须成对给(给一个会被 422 拒掉)。
    """

    provider: str | None = None
    model: str | None = None
    thinking_level: str | None = None
    #: 作用在哪个会话上(会话级:换模型要落进**那个**文件,并改那个会话的还原点)。
    #: 不给 = 只改 runtime 的当前状态(不落盘)。
    session: str | None = None


class SessionFork(BaseModel):
    """分叉。`at` = 从哪个 entry 分叉(默认 = 当前节点,即最后一个完整回合)。

    `at=None`(不给)与 `at=""`(从第一条消息之前)含义不同,所以用 `str | None`。
    """

    at: str | None = None


class DirEntry(BaseModel):
    """目录选择器里的一条(只有目录,没有文件 —— 见 `web/browse.py`)。"""

    name: str
    path: str


class DirectoryListing(BaseModel):
    """`GET /api/fs/dirs` 的响应。形状照 pi-web 的 `/api/cwd/browse`。

    `parent` 为 `None` 表示"已经在根上"(前端据此禁用"上级")。
    `roots` 只有 Windows 会给盘符,其余平台为空。
    """

    path: str
    parent: str | None = None
    home: str = ""
    roots: list[DirEntry] = Field(default_factory=list)
    entries: list[DirEntry] = Field(default_factory=list)


class CompactionResult(BaseModel):
    """手动压缩的结果。

    `compacted=False` 表示"没什么可压的"(`prepare_compaction` 回 `None`),不是失败 ——
    调用方据此提示用户,而不是当错误。
    """

    compacted: bool = False


class DeletedSessions(BaseModel):
    """批量删除的结果:**真正被删掉的会话 id**。

    只回 id 不回条数:前端要拿它判断"当前正开着的那个是否也在被删之列"。
    """

    ids: list[str] = Field(default_factory=list)


class WorkspaceRename(BaseModel):
    """工作区改名。`name` 空串 = 恢复成目录名(见 `workspaces.py`)。"""

    cwd: str = Field(min_length=1)
    name: str = ""


class WorkspaceNames(BaseModel):
    """工作区改过的**显示名**(目录 → 名字)。

    只有这一样 —— 项目列表本身是从会话 `cwd` 派生的,后端不维护名单,否则就会出现
    "名单说有三个工作区、会话文件只有一个"的两份真相。目录没有条目时用目录名。
    """

    names: dict[str, str] = Field(default_factory=dict)


class WorkspaceDeleted(BaseModel):
    """删除工作区的结果:**连同它的会话一起删掉了**。

    `ids` 是真正被删掉的会话 id(前端据此判断"当前正开着的那个是否也没了"),
    `names` 是删除后剩下的显示名表。目录本身不动 —— qi 不动用户的文件夹。
    """

    ids: list[str] = Field(default_factory=list)
    names: dict[str, str] = Field(default_factory=dict)


class SessionSummary(BaseModel):
    id: str
    title: str
    created_at: str
    #: 会话文件 mtime(最近活跃)。左栏的「15分钟」用它,不是 `created_at`。
    updated_at: str = ""
    cwd: str | None = None
    message_count: int = 0
    running: bool = False
    path: str = ""


class SessionList(BaseModel):
    sessions: list[SessionSummary]
    now_running: list[str] = Field(default_factory=list)


class UsageSummary(BaseModel):
    """一个会话的用量汇总(后端算:`session.usage_summary`)。

    放在会话明细上而不是让前端累加 —— 前端只拿得到分页窗口,长会话会静默少算。
    老会话(写入 usage 之前)除 `turns` 以外全是 0:没有就是没有,不补估值。
    """

    turns: int = 0
    steps: int = 0
    tools: int = 0
    tool_failures: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    #: 最后一轮的 prompt 大小 = 现在上下文里装着多少(不是各轮相加)
    context_tokens: int = 0


class SessionDetail(BaseModel):
    id: str
    title: str
    created_at: str
    cwd: str | None = None
    path: str = ""
    running: bool = False
    total_entries: int = 0
    #: 只回一个**窗口**(渐进恢复:老会话不能因为细节太多而卡住)
    entries: list[dict] = Field(default_factory=list)
    #: 窗口前面还有多少条(前端据此决定是否继续向更早翻页)
    skipped: int = 0
    #: 整条分支的用量汇总(与窗口无关:它统计的是落盘的全部)
    usage: UsageSummary = Field(default_factory=UsageSummary)
class ProviderInfo(BaseModel):
    """凭证状态**只回掩码与来源**,永不回明文(design/web.md §4)。"""

    name: str
    #: 人看的名字(预置表给的 label;自定义 provider 为空)。
    label: str = ""
    base_url: str | None = None
    api: str | None = None
    models: int = 0
    #: 模型 id 清单(设置页按 provider → model 两级展示)。
    model_ids: list[str] = Field(default_factory=list)
    credential_ok: bool = False
    credential_source: str = ""
    credential_masked: str = ""


class ProviderAdd(BaseModel):
    """`POST /api/providers`:新建一个**用户级** provider(写 models.json)。

    已存在的 provider 不从这里加 —— 那是“配凭证”(`POST /api/auth/{provider}`),
    两条路分开,免得“加一个”把已有声明默默改写。
    """

    name: str = ""
    base_url: str = ""
    api: str = ""
    api_key: str = ""
    #: 模型 id。上下文窗口与输出上限用 core 的默认值,
    #: 要精确就之后改 models.json(与 `qi init` 的“只加不删”同一条降级)。
    models: list[str] = Field(default_factory=list)


class ConfigView(BaseModel):
    providers: list[ProviderInfo]
    default_model: str | None = None
    #: 不带 provider 前缀的模型名 —— **给展示用**(输入卡右下的那一行放不下
    #: `provider/model`,而模型 id 自己可能还带斜杠:`deepseek/deepseek-v4.1-flash`)。
    #: 诊断面(遥测抽屉 / 检查项)继续用 `default_model` 的完整标签。
    default_model_name: str = ""
    default_model_source: str = ""
    router_model: str | None = None
    #: 默认模型的上下文窗口(tokens,来自 models.json 的 `contextWindow`)。
    #: 取不到就是 0 —— 前端据此决定画不画"上下文占用"那条进度(别除零)。
    default_model_context_window: int = 0
    settings_files: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    session_dir: str = ""
    #: 凭证文件的绝对路径。二次确认弹层要显示"改的是哪个文件",所以必须回给前端。
    auth_file: str = ""
    #: 新建 provider 写哪个 models.json(用户级)。弹层里要显示目标文件。
    models_write_file: str = ""
    checks: list[dict] = Field(default_factory=list)


class AuthWrite(BaseModel):
    """写入 provider 凭证。key 只在请求体里出现,不进日志。"""

    key: str = Field(min_length=1)


class SkillInfo(BaseModel):
    name: str
    description: str = ""
    source: str = ""
    path: str = ""


class SkillList(BaseModel):
    skills: list[SkillInfo]


#: 技能的两个**可写**层。`.agents/skills` 与 settings 的 `skills[]` 不在其中 ——
#: 见 `qi_lab.skills` 模块说明(“我建的我才删”)。
SKILL_SCOPES = ("global", "project")


class SkillWrite(BaseModel):
    """`POST /api/skills`:新建一个技能(`<根>/<名字>/SKILL.md`)。

    `description` 必给而 `name` 可空(name 空则回 400):顶层技能按 description 索引,
    没有它扫描器直接跳过 —— 写出来一个看不见的技能,比拒绝写更贵。
    """

    scope: Literal["global", "project"]
    name: str = ""
    description: str = ""
    body: str = ""


class SkillDelete(BaseModel):
    """`DELETE /api/skills`:按**显示名**删掉该层的一个技能(整目录)。"""

    scope: Literal["global", "project"]
    name: str = ""


class SkillImport(BaseModel):
    """`POST /api/skills/import`:把本机路径上的一个技能导进该层。"""

    scope: Literal["global", "project"]
    #: 服务器上的路径:技能目录 / 单份 .md / .zip(里面要能定位到**唯一**的 SKILL.md)
    source: str = ""
    #: 落点名(可选)。不给 → 用源里 frontmatter 的 name,再没有才用目录/文件名
    name: str = ""


class ExtensionList(BaseModel):
    """已装载的扩展名。

    `discover_extensions()` 只返回名字(它的职责是装载,不是描述),所以这里就只回名字——
    不编造版本/作者之类拿不到的信息。
    """

    extensions: list[str]


class FileEntry(BaseModel):
    """文件树的一行。路径一律**相对会话目录**(前端拿它当 key,也拿它回传)。"""

    name: str
    path: str
    kind: Literal["dir", "file"]
    size: int = 0
    mtime: float = 0
    #: 能否原字节预览(图片 / PDF / HTML)—— 前端据此选预览方式
    raw: bool = False


class FileListing(BaseModel):
    """一层目录(前端懒展开,一次只取一层)。"""

    #: 会话目录的绝对路径(根,也是边界)
    root: str
    #: 这一层的绝对路径
    path: str
    #: 上一层的相对路径;已在根上时为 None(前端据此禁用"上级")
    parent: str | None = None
    entries: list[FileEntry] = Field(default_factory=list)


class FileContent(BaseModel):
    """文本预览。

    `kind` 是判别位:`text` 才有 `text`;`binary` / `too_large` 是"能看见这个文件、
    但不预览内容"—— 前端据此显示一行说明而不是空白。
    """

    path: str
    kind: Literal["text", "binary"]
    size: int = 0
    #: 被上限截断(只回了前一段)
    truncated: bool = False
    lang: str = ""
    text: str = ""


# ── P-E5 ③ 切片 3c:角色与 MCP 走**扩展的真实数据**(用户选 A)─────

class AgentSuggestionGroup(BaseModel):
    """一级推荐问题分组：一级是标题，二级是组内问题。

    `title` 为空的分组表示“全域基线建议”（旧平铺写法或新旧混写），不占一级分类，
    会追加到任意已选标题下；有标题的组才是可点击展开的一级。
    `description` 是可选的分类说明（如“可跳过，直接提问即全域问答”）。
    """

    title: str = ""
    description: str = ""
    items: list[str] = Field(default_factory=list)


class AgentOpening(BaseModel):
    """角色的开场白 + 选择期快捷提问(`agent.md` 的 `opening`)。

    `suggestions` 必须在**选择 agent 时**就能拿到 —— 它不走 AG-UI：
    那时还没有 run，调用 `POST /api/ag-ui` 会真的开一轮。

    `suggestions` 是扁平清单（兼容旧前端）；两级展示读 `groups`。
    """

    message: str = ""
    suggestions: list[str] = Field(default_factory=list)
    groups: list[AgentSuggestionGroup] = Field(default_factory=list)


class AgentInfo(BaseModel):
    """一个角色 —— 数据来自 **qi-agents**(`discover()`)。

    不再有 v1 的 `display_name` / `keywords` / 私有技能计数:那些属于被移出 core 的 agent
    配置(P-E4c)。角色现在就是 `agent.md` 的一份 frontmatter + 正文。
    """

    name: str
    description: str = ""
    #: `user` | `project`(项目级同名覆盖用户级)
    source: str = ""
    #: 角色的 `tools:` 白名单;**空 = 继承父**(不是"全部")
    tools: list[str] = Field(default_factory=list)
    model: str = ""
    path: str = ""
    #: 角色目录里 `mcp.json` 声明了几个 server(角色私有那层)
    mcp: int = 0
    #: 开场白 + 选择期快捷提问;没有 `opening` 时为 `null`
    opening: AgentOpening | None = None


class AgentList(BaseModel):
    agents: list[AgentInfo] = Field(default_factory=list)
    #: qi-agents 没装 → True(界面据此说"装 qi-agents 才有角色"),而不是 500
    unavailable: bool = False


#: 可删的两层。词汇跟着 **qi-agents**(读模型的 `source` 就是它),不是技能的
#: `global` / `project` —— 界面显示什么词,请求就发什么词,不设翻译表。
AGENT_SCOPES = ("user", "project")


class AgentDelete(BaseModel):
    """`DELETE /api/agents`:按读模型的名字删掉该层的一个角色(整目录)。"""

    scope: Literal["user", "project"]
    name: str = ""


class McpServerInfo(BaseModel):
    """一个 MCP server 的**结构**,不含任何值(这条例矩从 v1 继承)。

    口径:名字 / 传输类型 / 目标 URL / `env` 与 `headers` 的**键名**。
    `env` 值、`headers` 值、以及 stdio 的 `command` / `args` **一律不出宿主** ——
    后三个字段是最容易直接写进明文密钥的地方(`npx -y x --token=sk-…`)。
    """

    name: str
    #: 展示用的传输名。声明里写了 Agent Plugins 的 `type` 就照它显示(`stdio` /
    #: `streamable-http` / `sse`);没写的老声明则按字段推断成 `stdio` / `http` /
    #: `socket`,都没有才是 `unknown`。
    transport: str = ""
    url: str = ""                  # http 的目标;stdio 的 command **不给**
    #: socket 传输的套接字路径。**它是结构不是密钥**(与 `url` 同一档)——
    #: 不给的话,socket 那种 server 在界面上永远改不动(读不到就只能整个重建)。
    socket: str = ""
    env_keys: list[str] = Field(default_factory=list)
    header_keys: list[str] = Field(default_factory=list)
    disabled: bool = False
    direct_tools: bool | list[str] = False
    unknown_fields: list[str] = Field(default_factory=list)
    source: str = ""               # global | project | role


class McpSource(BaseModel):
    """一处声明表。`exists=false` 也回 —— 前端据此区分"没这个文件"与"文件在但没写 server"。"""

    scope: str                     # global | project | role
    path: str
    exists: bool = False
    servers: list[McpServerInfo] = Field(default_factory=list)
    #: `scope == "role"` 时是哪个角色
    role: str = ""


class McpList(BaseModel):
    sources: list[McpSource] = Field(default_factory=list)
    unavailable: bool = False      # qi-mcp 没装


# ── MCP 声明的写操作(增删改)────────────────────────────
#
# 读那一套(`McpServerInfo`)**只给结构**;写的是另一套模型:值必须进得来(不然没法把
# `env` / `args` 写进 mcp.json),但**响应回的仍是 `McpList`** —— 一个值都不出宿主。
# 这条规矩读写两侧同一条(`test_mcp_endpoint_never_leaks_values` 钉读侧,
# `test_mcp_write_never_echoes_values` 钉写侧)。

#: 可写的传输**闭集**。qi-mcp 允许任意 `type` 原样透传(生态还在长),但**写**要收敛:
#: 往配置里写一个谁也连不上的 `type`,症状是“永远连不上”而不是一句报错,最难查。
#: 末尾那个 `http` 是老写法的推断名(与 `streamable-http` 同族)—— 读端会把它给你,
#: 不收下的话,一份老声明在界面上改一次就存不回去。
MCP_TRANSPORTS = ("stdio", "streamable-http", "sse", "socket", "http")


class McpServerEdit(BaseModel):
    """一个 server 的**可写**字段(POST 新建;PATCH 的部分语义见 `McpServerPatch`)。

    读端不回的那几个(command / args / env / headers 的值)在这里必须收得进来 ——
    否则“写”就没法写密钥。收进来的值只落盘,**不回显**。
    """

    name: str
    transport: str = ""            # `MCP_TRANSPORTS` 之一,写进 `type`
    url: str = ""                  # streamable-http / sse / http 的目标
    command: str = ""              # stdio 的命令(**只进不回**)
    args: list[str] = Field(default_factory=list)        # stdio 的参数(只进不回)
    cwd: str = ""
    socket: str = ""               # socket 传输的套接字路径
    env: dict[str, str] = Field(default_factory=dict)     # 只进不回
    headers: dict[str, str] = Field(default_factory=dict) # 只进不回
    disabled: bool = False
    direct_tools: bool | list[str] = False


class McpServerPatch(BaseModel):
    """PATCH 的部分语义:**只改 `model_fields_set` 里出现过的键**,没出现的一个不动。

    全部默认 `None`:“没送这个键” 与 “送了 null” 都读作不改。这一条正是
    “值看不见也能改”的实现 —— 读端不给值,前端就不送键,旧值原样留着。
    """

    name: str | None = None        # 给了就是**改名**(PATCH 顶层那个 name 是改前的名字)
    transport: str | None = None
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    cwd: str | None = None
    socket: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    disabled: bool | None = None
    direct_tools: bool | list[str] | None = None


class McpWrite(BaseModel):
    """`POST /api/mcp`:在某一层新增一个 server。"""

    scope: Literal["global", "project", "role"]
    #: `scope == "role"` 时是哪个角色(= 角色目录名)
    role: str = ""
    server: McpServerEdit


class McpPatch(BaseModel):
    """`PATCH /api/mcp`:改某一层里的一个 server(`name` 是**改之前**的名字)。"""

    scope: Literal["global", "project", "role"]
    role: str = ""
    name: str
    patch: McpServerPatch


class McpDelete(BaseModel):
    """`DELETE /api/mcp`:从某一层删掉一个 server。"""

    scope: Literal["global", "project", "role"]
    role: str = ""
    name: str


# ── A2A 条目(两层 a2a.json)────────────────────────────
#
# 与 MCP 同一套读写口径,但**只有两层**(`global` / `project`)—— a2a.json 没有角色私有
# 那层。另外 a2a.json 的合并规则是**同名深合并**(不是整体覆盖),所以读端除了逐层原样,
# 还回一份 `resolved` 生效视图:否则界面上看到的永远是"某一层写了什么",而不是
# "现在到底连的是哪"。

#: 可写的层。闭集,与 `qi_a2a.config.scope_files()` 一致。
A2A_SCOPES = ("global", "project")


class A2ASkill(BaseModel):
    """名片里的一条技能。只用于探测结果展示。"""

    id: str = ""
    name: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class A2AEntryInfo(BaseModel):
    """一个 A2A 条目的**结构视图**。

    与 MCP 同一条安全规矩:`headers` 只给**键名**,值一律不出宿主(那是 token 最常待的地方)。
    端点三个字段(卡片 / 基址 / RPC)照文件里写了什么就给什么 —— 它们是结构不是密钥,
    而且不给的话界面上就分不清这条走的是"卡片发现"还是"直连端点"。
    """

    name: str
    card_url: str = ""
    base_url: str = ""
    rpc_url: str = ""
    tenant: str = ""
    description: str = ""
    header_keys: list[str] = Field(default_factory=list)   # 值不给
    timeout_ms: int = 600_000
    streaming: bool = True
    discovery: str = "auto"
    direct_tools: bool | list[str] = False
    #: 直连工具名(`<prefix>__<条目>`),开了 `directTools` 才有意义
    tool_name: str = ""
    disabled: bool = False
    unknown_fields: list[str] = Field(default_factory=list)
    #: 这条声明来自哪一层(`global` / `project`);`resolved` 里是**生效值**的主导层
    source: str = ""
    #: `resolved` 视图里:有哪些层参与了这个条目(深合并的痕迹)
    layers: list[str] = Field(default_factory=list)


class A2ASource(BaseModel):
    """一处声明表。`exists=false` 也回 —— 前端据此区分"没这个文件"与"文件在但没写条目"。"""

    scope: str                     # global | project
    path: str
    exists: bool = False
    entries: list[A2AEntryInfo] = Field(default_factory=list)


class A2AList(BaseModel):
    sources: list[A2ASource] = Field(default_factory=list)
    #: 两层深合并后的**生效**视图(含被 `disabled` 关掉的,界面要能显示"关了")
    resolved: list[A2AEntryInfo] = Field(default_factory=list)
    unavailable: bool = False      # qi-a2a 没装


class A2AEntryEdit(BaseModel):
    """一个条目的**可写**字段(POST 新建)。

    字段名用 snake_case(与所有 `/api` 一致),落盘时转成 a2a.json 的 camelCase。
    空串 / 空表表示"不写这个键" —— 默认值由 qi-a2a 自己补,不在这里抄一遍。
    """

    name: str
    card_url: str = ""
    base_url: str = ""
    rpc_url: str = ""
    tenant: str = ""
    description: str = ""
    headers: dict[str, str] = Field(default_factory=dict)   # 只进不回
    timeout_ms: int | None = None
    streaming: bool | None = None
    discovery: str = ""
    direct_tools: bool | list[str] = False
    tool_prefix: str = ""
    disabled: bool = False


class A2AEntryPatch(BaseModel):
    """PATCH 的部分语义:**只改 `model_fields_set` 里出现过的键**。

    全部默认 `None`:“没送这个键”读作不改。**送了 `null` 也读作不改** —— 读端不给
    `headers` 的值,前端就不可能把整张表重发回来,把 `null` 当"删掉"会让人一保存就丢 token。
    要清掉某张表,送一个空对象 `{}`。
    """

    name: str | None = None        # 给了就是**改名**(顶层那个 name 是改前的名字)
    card_url: str | None = None
    base_url: str | None = None
    rpc_url: str | None = None
    tenant: str | None = None
    description: str | None = None
    headers: dict[str, str] | None = None
    timeout_ms: int | None = None
    streaming: bool | None = None
    discovery: str | None = None
    direct_tools: bool | list[str] | None = None
    tool_prefix: str | None = None
    disabled: bool | None = None


class A2AWrite(BaseModel):
    """`POST /api/a2a`:在某一层新增一个条目。"""

    scope: Literal["global", "project"]
    entry: A2AEntryEdit


class A2APatch(BaseModel):
    """`PATCH /api/a2a`:改某一层里的一个条目(`name` 是**改之前**的名字)。"""

    scope: Literal["global", "project"]
    name: str
    patch: A2AEntryPatch


class A2ADelete(BaseModel):
    """`DELETE /api/a2a`:从某一层删掉一个条目(同名的下层会露出来)。"""

    scope: Literal["global", "project"]
    name: str


class A2AProbeRequest(BaseModel):
    """`POST /api/a2a/probe`:去真实端点拉一次名片。

    **只探测已配置的条目**(不接受任意 URL)—— 这个端点的用途是"我配的对不对",
    不是"帮我访问一个地址";后者会把 web 宿主变成内网探测器。
    """

    server: str = ""               # 条目名;只配了一个时可省略
    agent: str = ""                # 条目下有多个智能体时,按 tenant 或名字选


class A2AProbeCard(BaseModel):
    """一张名片(探测结果)。只回展示需要的字段,不回原始 JSON —— 名片可能很大。"""

    name: str = ""
    description: str = ""
    tenant: str = ""
    rpc_url: str = ""
    skills: list[A2ASkill] = Field(default_factory=list)


class A2AProbeResult(BaseModel):
    ok: bool = False
    server: str = ""
    source: str = ""               # 生效条目来自哪一层
    discovery_url: str = ""        # 实际 GET 的地址;空 = 没走发现(合成卡)
    elapsed_ms: int = 0
    cards: list[A2AProbeCard] = Field(default_factory=list)
    error: str = ""


# ── 商店(qi-store)──────────────────────────────────────
#
# 字段一律 camelCase(与前端一致),值**只出展示需要的东西**:
# 上游的原始 JSON、令牌、headers 的值都不经这里出宿主。
#
# 第二个不变量:**qi-store 没装不是 500** —— `available=false` + `error`,界面据此说清楚
# (与 `/api/agents`、`/api/mcp` 的 `unavailable` 同一套口径)。


class StoreSource(BaseModel):
    name: str
    baseUrl: str = ""
    default: bool = False
    loggedIn: bool = False
    login: str = ""
    role: str = ""
    expiresAt: int | None = None
    expiresIn: int | None = None


class StoreStatus(BaseModel):
    available: bool = False
    sources: list[StoreSource] = Field(default_factory=list)
    defaultScope: str = "global"
    wanted: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    storeFile: str = ""
    error: str = ""


class StoreItemOut(BaseModel):
    id: str
    kind: str
    name: str
    description: str = ""
    version: str = ""
    updatedAt: str = ""
    requiresConfig: bool = False
    safety: dict = Field(default_factory=dict)
    badges: dict = Field(default_factory=dict)
    installed: bool = False
    installedVersion: str | None = None
    updateAvailable: bool = False


class StoreMissing(BaseModel):
    kind: str
    reason: str


class StoreList(BaseModel):
    available: bool = False
    store: str = ""
    items: list[StoreItemOut] = Field(default_factory=list)
    total: int = 0
    #: 哪一类没查成(未登录 / 网络)。界面要把它显示出来 —— 否则"商店是空的"是最坏的误解
    missing: list[StoreMissing] = Field(default_factory=list)
    cachedAt: int | None = None
    error: str = ""


class StoreInstalledItem(BaseModel):
    id: str
    kind: str
    name: str
    version: str = ""
    scope: str = ""
    target: str = ""
    needsConfig: list[str] = Field(default_factory=list)
    installedAt: str = ""
    upstreamUpdatedAt: str = ""
    #: **磁盘上还在不在**(账本只记"装过什么";从 A2A/MCP 那两节删掉、手改 json、
    #: `rm -rf` 目录都不经过商店)。false → 界面标"已不在配置里"并给一颗「清理账本」。
    present: bool = True
    #: `present=false` 时的人话原因(直接显示)
    missingReason: str = ""


class StoreInstalled(BaseModel):
    available: bool = False
    items: list[StoreInstalledItem] = Field(default_factory=list)
    error: str = ""


class StorePlanItem(BaseModel):
    id: str
    name: str
    note: str = ""
    targets: list[str] = Field(default_factory=list)
    needsConfig: list[str] = Field(default_factory=list)
    conflict: str | None = None
    unsafe: bool = False


class StorePlan(BaseModel):
    available: bool = False
    scope: str = "global"
    items: list[StorePlanItem] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
    needsConfig: list[str] = Field(default_factory=list)
    #: 有冲突且没 `force` → 界面必须先让用户选"覆盖"再重发
    blocked: bool = False
    error: str = ""


class StorePlanRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)      # id 或名称;空 = 400
    scope: Literal["global", "project"] | None = None
    force: bool = False


class StoreInstallRequest(StorePlanRequest):
    allowUnsafe: bool = False


class StoreInstallResult(BaseModel):
    available: bool = False
    scope: str = "global"
    installed: list[StoreInstalledItem] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
    needsConfig: list[str] = Field(default_factory=list)
    error: str = ""


class StoreUninstallRequest(BaseModel):
    id: str = ""
    scope: Literal["global", "project"] | None = None


class StoreUninstallResult(BaseModel):
    available: bool = False
    id: str = ""
    #: 条目类型 —— 宿主据此决定要不要重扫技能缓存
    kind: str = ""
    removed: list[str] = Field(default_factory=list)
    error: str = ""


class StoreCheckItem(BaseModel):
    id: str
    name: str = ""
    scope: str = ""
    version: str = ""
    upstreamVersion: str | None = None
    #: up-to-date | update | gone(上游没了) | missing(本地没了)
    status: str = ""
    #: `missing` 时的具体原因(哪个键/哪个文件不在了)
    detail: str = ""


class StoreCheck(BaseModel):
    available: bool = False
    items: list[StoreCheckItem] = Field(default_factory=list)
    wantedMissing: list[dict] = Field(default_factory=list)
    error: str = ""


class StoreLoginRequest(BaseModel):
    """`POST /api/store/login`:用**终端账号**换一个令牌。

    密码只在这一层用一次:`qi_store.service.login` 把它交给上游就丢,不落盘、不日志、
    不回显 —— 响应是 `StoreStatus`(只有登录态),**令牌一个字节都不出去**。

    写操作,所以和其它写端点一样要 `X-Qi-Confirm: yes`(它会往磁盘写凭证)。
    """

    login: str = Field(min_length=1)
    password: str = Field(min_length=1)
    #: 第一次登录时必须给(还没有源);之后可省略,沿用已配置的地址
    baseUrl: str = ""
    #: 多源时选一个;省略 = defaultStore
    store: str = ""


class StoreLogoutRequest(BaseModel):
    store: str = ""
    all: bool = False


# ── 知识库（qi-kb）────────────────────────────────────────


class KbProvider(BaseModel):
    name: str
    baseUrl: str = ""
    apiPrefix: str = "/api"
    instance: str = ""
    login: str = ""
    role: str = ""
    loggedIn: bool = False
    expiresAt: int | None = None
    expiresIn: int | None = None
    kbCount: int = 0
    scope: str = "global"


class KbStatus(BaseModel):
    available: bool = False
    providers: list[KbProvider] = Field(default_factory=list)
    knowledgeBases: list[dict] = Field(default_factory=list)
    defaults: dict = Field(default_factory=dict)
    defaultProvider: str = ""
    defaultKnowledgeBase: str = ""
    files: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class KbProviders(BaseModel):
    providers: list[KbProvider] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class KbProviderAddRequest(BaseModel):
    name: str = Field(min_length=1)
    baseUrl: str = Field(min_length=1)
    apiPrefix: str = "/api"
    login: str = ""
    role: str = ""
    instance: str = ""
    scope: Literal["global", "project"] = "global"


class KbProviderLoginRequest(BaseModel):
    name: str = Field(min_length=1)
    login: str = ""
    password: str = Field(min_length=1)
    baseUrl: str = ""
    apiPrefix: str = "/api"
    instance: str = ""
    scope: Literal["global", "project"] = "global"


class KbProviderRemoveRequest(BaseModel):
    name: str = Field(min_length=1)
    cascade: bool = False


class KbItem(BaseModel):
    id: str
    provider: str = ""
    name: str = ""
    description: str = ""
    embeddingModel: str = ""
    visibility: str = "private"
    docCount: int = 0
    chunkCount: int = 0
    updatedAt: str = ""
    icon: str = ""
    bound: bool = False
    boundProvider: str | None = None
    scope: str = ""


class KbList(BaseModel):
    available: bool = False
    items: list[KbItem] = Field(default_factory=list)
    total: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class KbDocs(BaseModel):
    available: bool = False
    provider: str = ""
    kbId: str = ""
    name: str = ""
    documents: list[dict] = Field(default_factory=list)
    total: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class KbSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    kbIds: list[str] = Field(default_factory=list)
    provider: str = ""
    topK: int | None = None
    strategy: Literal["dense", "sparse", "hybrid"] | None = None
    useRerank: bool | None = None
    rerankTopK: int | None = None


class KbSearchResult(BaseModel):
    available: bool = False
    provider: str = ""
    query: str = ""
    results: list[dict] = Field(default_factory=list)
    total: int = 0
    strategy: str = ""
    rerankMethod: str | None = None
    kbIds: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class KbAskRequest(BaseModel):
    question: str = Field(min_length=1)
    kbIds: list[str] = Field(default_factory=list)
    provider: str = ""
    topK: int | None = None
    strategy: Literal["dense", "sparse", "hybrid"] | None = None
    useRerank: bool | None = None
    rerankTopK: int | None = None


class KbAskResult(BaseModel):
    available: bool = False
    provider: str = ""
    question: str = ""
    answer: str = ""
    sources: list[dict] = Field(default_factory=list)
    kbIdsUsed: list[str] = Field(default_factory=list)
    evidenceSufficient: bool = False
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class KbPlanRequest(BaseModel):
    kbId: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    scope: Literal["global", "project"] = "global"
    rebind: bool = False


class KbPlan(BaseModel):
    available: bool = False
    action: str = ""
    kbId: str = ""
    provider: str = ""
    existingProvider: str | None = None
    targetProvider: str = ""
    blocked: bool = False
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class KbInstallRequest(KbPlanRequest):
    pass


class KbInstallResult(BaseModel):
    available: bool = False
    action: str = ""
    kbId: str = ""
    provider: str = ""
    scope: str = "global"
    name: str = ""
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class KbUninstallRequest(BaseModel):
    kbId: str = Field(min_length=1)
    provider: str = ""


class KbUninstallResult(BaseModel):
    available: bool = False
    kbId: str = ""
    provider: str = ""
    name: str = ""
    removed: bool = False
    error: str = ""


class KbDefaultsRequest(BaseModel):
    defaults: dict = Field(default_factory=dict)
    scope: Literal["global", "project"] = "global"
