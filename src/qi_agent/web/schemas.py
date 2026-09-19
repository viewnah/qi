"""qi-web API 的请求/响应模型(`/api` v1 契约)。

契约版本(`CONTRACT_VERSION`)是 UI 与宿主的依赖面:前端在 `/api/meta` 里读到它,
形状不兼容时**显式报错**而不是白屏(见 docs/web.md §3 的"契约语义版本化")。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: AG-UI 改造是**破坏性**的:事件形状、传输形态(单 POST)、续传机制全变了,
#: 所以升到 2。前端此刻仍声明 "1" → 会走它自己的显式报错分支(而不是白屏),
#: 这是**刻意**的过渡状态:报错比坏页面好。前端跟上后两侧一起改成 "2"。
CONTRACT_VERSION = "2"

APP_NAME = "qi-web"


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
    title: str = ""
    cwd: str | None = None


class SessionRename(BaseModel):
    title: str = Field(min_length=1)


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


class AgentInfo(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    source: str = ""
    tools: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    skills: int = 0
    data_sources: int = 0
    mcp_private: int = 0


class AgentList(BaseModel):
    agents: list[AgentInfo]
    #: 当前会话粘性选中的 agent(未选为 None)
    active_agent: str | None = None
    mode: str = "auto"


class ProviderInfo(BaseModel):
    """凭证状态**只回掩码与来源**,永不回明文(docs/web.md §4)。"""

    name: str
    base_url: str | None = None
    api: str | None = None
    models: int = 0
    credential_ok: bool = False
    credential_source: str = ""
    credential_masked: str = ""


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


class McpServerInfo(BaseModel):
    """一个 MCP server 的**结构**,不含任何值(docs/web.md §18.18)。

    口径:名字 / 传输类型 / 目标 URL / `env` 与 `headers` 的**键名**。
    `env` 值、`headers` 值、以及 stdio 的 `command` / `args` **一律不出宿主**——
    后三个字段是最容易直接写进明文密钥的地方(`npx -y x --token=sk-…`),
    而 mcp.json 的 env 值不像 data_sources 的 dsn 那样被强制 `{env:XXX}`。
    与"凭证只回掩码"同一条规矩。
    """

    name: str
    #: `streamable-http` / `stdio` / …(mcp.json 里的 `type` 原值;没写就是空)
    transport: str = ""
    #: http 类回 URL;stdio 回 `"stdio"`(命令与参数不回显,见类注释)
    target: str = ""
    #: `env` / `headers` 的键名(值永不出宿主)
    env_keys: list[str] = Field(default_factory=list)
    header_keys: list[str] = Field(default_factory=list)
    #: 声明绑定它的 agent 名(全局/项目层才有意义 —— 那是"门控"的结果)
    bound_by: list[str] = Field(default_factory=list)


class McpSource(BaseModel):
    """一处 mcp.json(全局 / 项目 / 某个 agent 的私有)。

    `exists=False` 也回:`servers` 为空配上看路径,前端才能区分"这里没有文件"
    与"文件在但没声明 server"。
    """

    scope: Literal["global", "project", "agent"]
    #: scope=="agent" 时是 agent 名
    owner: str = ""
    path: str = ""
    exists: bool = False
    servers: list[McpServerInfo] = Field(default_factory=list)


class McpList(BaseModel):
    """设置页 MCP 节的全部数据。

    v1 只有**解析与门控**,没有 MCP client(见 docs/PLAN.md 的未决清单),
    所以这里没有任何"已连接 / 工具发现"状态可回 —— 回的就是磁盘上的声明。
    """

    sources: list[McpSource]
