"""qi-web API 的请求/响应模型(`/api` v1 契约)。

契约版本(`CONTRACT_VERSION`)是 UI 与宿主的依赖面:前端在 `/api/meta` 里读到它,
形状不兼容时**显式报错**而不是白屏(见 docs/web.md §3 的"契约语义版本化")。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

CONTRACT_VERSION = "1"

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


class TurnRequest(BaseModel):
    text: str = Field(min_length=1)
    agent: str | None = None


class TurnAccepted(BaseModel):
    """一轮已受理。`from_seq` 是客户端应当开始读事件的位置(0 = 从头)。"""

    run_id: str
    session_id: str
    from_seq: int = 0


class SessionSummary(BaseModel):
    id: str
    title: str
    created_at: str
    cwd: str | None = None
    message_count: int = 0
    running: bool = False
    path: str = ""


class SessionList(BaseModel):
    sessions: list[SessionSummary]
    now_running: list[str] = Field(default_factory=list)


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
    default_model_source: str = ""
    router_model: str | None = None
    settings_files: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    session_dir: str = ""
    #: 凭证文件的绝对路径。二次确认弹层要显示"改的是哪个文件",所以必须回给前端。
    auth_file: str = ""
    checks: list[dict] = Field(default_factory=list)


class AuthWrite(BaseModel):
    """写入 provider 凭证。key 只在请求体里出现,不进日志。"""

    key: str = Field(min_length=1)


class Conflict(BaseModel):
    """409:同一会话已有活跃 run(前端应提示"正在运行",而不是静默失败)。"""

    detail: str
    run_id: str | None = None


class SkillInfo(BaseModel):
    name: str
    description: str = ""
    source: str = ""
    path: str = ""


class SkillList(BaseModel):
    skills: list[SkillInfo]


class PluginList(BaseModel):
    """已装载的插件名。

    `discover_plugins()` 只返回名字(它的职责是装载,不是描述),所以这里就只回名字——
    不编造版本/作者之类拿不到的信息。
    """

    plugins: list[str]
