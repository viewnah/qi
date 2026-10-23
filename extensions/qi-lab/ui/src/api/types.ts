/**
 * 契约类型。单一真相在 python 侧(`schemas.py` + `agui.py`),这里**手写镜像**。
 *
 * 为什么手写:契约号不匹配时前端要能**显式报错**而不是白屏;自动生成(openapi → types)
 * 留到拆 UI 插件包时再做。两侧字段一旦改名,`contract.test.ts` 会立刻报错。
 *
 * AG-UI 的形状是**判别联合**:类型在 payload 的 `type` 里,而不是 SSE 的 `event:` 字段。
 * 所以这里用 TS 的 discriminated union —— 好处是 `switch (ev.type)` 能被编译器穷尽检查,
 * 漏一个分支就编译不过。这比"读 event: 名 + 手写映射表"强得多。
 */

export const CONTRACT_VERSION = "2";

// ── 宿主自述 ──────────────────────────────────────────────

export interface Meta {
   app: string;
   contract: string;
   version: string;
   default_cwd: string;
   auth_required: boolean;
   static_ready: boolean;
   capabilities: Record<string, boolean>;
}

// ── 会话(REST,不是 AG-UI 的一部分)──────────────────────

export interface SessionSummary {
   id: string;
   title: string;
   created_at: string;
   /** 会话文件 mtime(最近活跃)。左栏行尾的「15分钟」读它,不是 `created_at`。 */
   updated_at?: string;
   cwd: string | null;
   message_count: number;
   running: boolean;
   path: string;
}

export interface SessionList {
   sessions: SessionSummary[];
}

/**
 * 工作区改过的**显示名**(目录 → 名字)。
 *
 * 只有这一样:项目列表本身是会话 `cwd` 的派生,后端不维护名单 —— 否则会出现
 * "名单说有三个工作区、会话文件只有一个"的两份真相。见 `src/qi_agent/workspaces.py`。
 */
export interface WorkspaceNames {
   names: Record<string, string>;
}

/** 手动压缩的结果:`POST /api/sessions/{id}/compact`。`compacted=false` = 没什么可压。 */
export interface CompactionResult {
   compacted: boolean;
}

/** 批量删除的结果:`DELETE /api/sessions?scope=ungrouped`(「未分组」桶的清除)。 */
export interface DeletedSessions {
   /** 真正被删掉的会话 id(前端据此判断"当前开着的那个是否也没了")。 */
   ids: string[];
}

/** `DELETE /api/workspaces` 的结果:**连同会话一起删掉了**(不可恢复)。 */
export interface WorkspaceDeleted {
   /** 真正被删掉的会话 id(前端据此判断"当前开着的那个是否也没了")。 */
   ids: string[];
   /** 删除后剩下的显示名表。 */
   names: Record<string, string>;
}

/**
 * 目录选择器的一条(`GET /api/fs/dirs`)。**只有目录** —— 文件名在选择器里只是噪音,
 * 而且少回一类信息就少一类泄露面(见 `src/qi_agent/web/browse.py`)。
 */
export interface DirEntry {
   name: string;
   path: string;
}

/** `GET /api/fs/dirs` 的响应。形状照 pi-web 的 `/api/cwd/browse`。 */
export interface DirectoryListing {
   /** 归一后的真实路径(`~` 已展开、软链已解析)。 */
   path: string;
   /** 上一级;已经在根上时为 `null`(据此禁用"上级")。 */
   parent: string | null;
   /** 主目录(「主目录」快捷键的落点)。 */
   home: string;
   /** Windows 的盘符列表,其它平台为空。 */
   roots: DirEntry[];
   entries: DirEntry[];
}

/** 会话 JSONL 的一条 entry(五类 + header)。字段按 `type` 取舍,故多数可选。 */
export interface Entry {
   type: string;
   ts?: string;
   id?: string;
   // header
   title?: string;
   created_at?: string;
   cwd?: string;
   // message
   role?: "system" | "user" | "assistant" | "tool";
   content?: string;
   agent_id?: string;
   // tool(结构化字段来自 AgentRunner 的 ToolOutcome)
   tool?: string;
   args?: Record<string, unknown>;
   status?: "ok" | "error";
   duration_ms?: number;
   exit_code?: number | null;
   error?: string | null;
   result?: string;
   /** 插件给客户端看的自由结构(见 QiToolMeta)。回放时也要带上。 */
   details?: Record<string, unknown> | null;
   /** 这一次调用的用量。老会话(写入 usage 之前)没有这个字段。 */
   usage?: Record<string, unknown>;
   // dispatch
   agent?: string | null;
   display_name?: string | null;
   confidence?: number;
   reasoning?: string;
   source?: string;
   // custom
   custom_type?: string;
   // compaction / branch_summary
   summary?: string;
}

/**
 * 一个会话的用量汇总(后端算:`session.usage_summary`)。
 *
 * 后端算而不是前端累加:前端只拿得到**分页窗口**,长会话自己求和会静默少算。
 * 老会话(写入 usage 之前)除 `turns` 以外全是 0 —— 没有就是没有,不补估值。
 */
export interface UsageSummary {
   turns: number;
   steps: number;
   tools: number;
   tool_failures: number;
   llm_calls: number;
   prompt_tokens: number;
   completion_tokens: number;
   total_tokens: number;
   /** 最后一轮的 prompt 大小 = 现在上下文里装着多少(不是各轮相加) */
   context_tokens: number;
}

export interface SessionDetail {
   id: string;
   title: string;
   created_at: string;
   cwd: string | null;
   path: string;
   running: boolean;
   total_entries: number;
   entries: Entry[];
   skipped: number;
   /** 整条分支的用量汇总(与窗口无关) */
   usage: UsageSummary;
}

/** `agent.md` 的 `opening`:开场白 + 选择期快捷提问。 */
export interface AgentSuggestionGroup {
   /** 一级标题（如“联合岛屿作战”）；空串 = 全域基线，不占一级分类 */
   title: string;
   /** 一级说明（可选，如“可跳过，直接提问即全域问答”） */
   description?: string;
   /** 二级问题：点选后作为一条 user 消息提交 */
   items: string[];
}

export interface AgentOpening {
   message: string;
   /** 扁平清单：兼容旧前端；新前端优先读 groups */
   suggestions: string[];
   /** 两级推荐：一级分组 + 二级问题 */
   groups?: AgentSuggestionGroup[] | null;
}

/** 一个角色 —— 数据来自 **qi-agents**(`GET /api/agents`)。
 *
 * P-E4c 之后 core 不再认识"角色",所以这里没有 v1 的 `display_name` / `keywords` /
 * 技能与数据源计数:角色就是 `agent.md` 的一份 frontmatter + 正文。
 */
export interface AgentInfo {
   name: string;
   description: string;
   /** `user` | `project`(项目级同名覆盖用户级) */
   source: string;
   /** `tools:` 白名单;**空 = 继承父**(不是"全部") */
   tools: string[];
   /** `provider/model`;空 = 继承 */
   model: string;
   /** 角色目录(诊断用) */
   path: string;
   /** 角色私有 mcp.json 里声明了几个 server */
   mcp: number;
   /** 开场白 + 快捷提问;老宿主 / 没写 `opening` → null */
   opening?: AgentOpening | null;
}

export interface ProviderInfo {
   name: string;
   /** 人看的名字(预置表给的 label;自定义 provider 为空)。旧宿主可能不给。 */
   label?: string;
   base_url: string | null;
   api: string | null;
   models: number;
   /** 模型 id 清单(设置页按 provider → model 两级展示)。旧宿主可能不给。 */
   model_ids?: string[];
   credential_ok: boolean;
   credential_source: string;
   credential_masked: string;
}

/** `POST /api/providers`:新建一个用户级 provider(写 models.json)。 */
export interface ProviderAddBody {
   name: string;
   base_url: string;
   api: string;
   api_key: string;
   models: string[];
}

export interface ConfigView {
   providers: ProviderInfo[];
   /** 完整标签 `provider/model`(诊断面用:遥测抽屉、检查项)。 */
   default_model: string | null;
   /** 不带 provider 前缀的模型名(展示用:输入卡右下)。旧宿主可能不给,回落 `default_model`。 */
   default_model_name?: string;
   default_model_source: string;
   /** 默认模型的上下文窗口(tokens)。0 = 取不到 → 不画占用条(不是"窗口是 0") */
   default_model_context_window: number;
   router_model: string | null;
   settings_files: string[];
   config_files: string[];
   session_dir: string;
   auth_file: string;
   /** 新建 provider 写哪个 models.json(用户级)。旧宿主可能不给。 */
   models_write_file?: string;
   checks: { name: string; ok: boolean; detail: string }[];
}

export interface SkillInfo {
   name: string;
   description: string;
   source: string;
   path: string;
}

/**
 * `TOOL_CALL_RESULT.metadata["qi.tool"]` —— qi 的工具结构化结果。
 *
 * `details` 是**插件唯一的 UI 下行通道**(design/web.md §16):开放字典,
 * 宿主不为任何具体插件改接口。
 */
export interface QiToolMeta {
   status?: string;
   duration_ms?: number;
   exit_code?: number | null;
   error?: string | null;
   details?: Record<string, unknown> | null;
}

/**
 * `details["ui"]` 的词汇表(v1)。
 *
 * 这是**声明式**的:插件输出数据,宿主实现渲染。好处是插件永不改 API,
 * 而宿主加新组件类型时所有已存在的插件立刻可用。
 *
 * 遇到不认识的 `type`,渲染方必须**退回原始 JSON**而不是丢弃 —— 否则
 * "插件发了东西但没人看见"会变成不可诊断的问题。
 */
export type UiNode =
   | {
        type: "list";
        items: {
           label: string;
           /** v1.1 新增词:委派失败。不认它的老渲染端会当 `pending` 画成 ○,
            * 所以 `note` 里始终带着错误原文 —— 与「不认识的 type 退回 JSON」
            * 同一条降级哲学:读不到形状,至少读得到字。 */
           state?: "done" | "active" | "pending" | "error";
           note?: string;
           /** v1.1 新增:显示用缩进层级(1 起)。缺省时渲染端回退 label 前导空格。 */
           depth?: number;
        }[];
     }
   | { type: "kv"; rows: [string, string][] }
   | { type: "progress"; value: number; max: number; label?: string }
   | { type: "code"; lang?: string; text: string }
   | { type: "note"; text: string };

/** `details["ui"]` 的载荷形状。`ui_version` 用于将来做兼容判断。 */
export interface UiPayload {
   ui_version?: number;
   ui: UiNode[];
}

export interface SkillList {
   skills: SkillInfo[];
}

/** 已装载的扩展名。`discover_extensions()` 只返回名字,所以这里也只回名字。 */
export interface ExtensionList {
   extensions: string[];
}

/** 文件树里的一行(`GET /api/files`)。路径一律相对会话目录。 */
export interface FileEntry {
   name: string;
   path: string;
   kind: "dir" | "file";
   size: number;
   mtime: number;
   /** 能原字节预览(图片 / PDF / HTML) */
   raw: boolean;
}

/** 一层目录(前端懒展开,一次只取一层)。 */
export interface FileListing {
   /** 会话目录的绝对路径(根,也是边界) */
   root: string;
   /** 这一层的绝对路径 */
   path: string;
   /** 上一层的相对路径;已在根上时为 null */
   parent: string | null;
   entries: FileEntry[];
}

/** 文本预览。`kind` 是判别位:`binary` 是"能看见这个文件、但不预览内容"。 */
export interface FileContent {
   path: string;
   kind: "text" | "binary";
   size: number;
   truncated: boolean;
   lang: string;
   text: string;
}

/**
 * 一个 MCP server 的**结构**(不含值,见 design/web.md §18.18)。
 *
 * 宿主只投影出名字 / 传输类型 / 目标 / `env` 与 `headers` 的**键名**:
 * `env` 值、`headers` 值、stdio 的 `command` / `args` 都不出宿主 —— mcp.json 的
 * env 值不像 data_sources 的 dsn 那样被强制 `{env:XXX}`,手写明文是可能的。
 */
export interface McpServerInfo {
   name: string;
   /** `stdio` | `http` | `socket` | `unknown` */
   transport: string;
   /** http 的目标;**stdio 的 command/args 不出宿主**(最容易写进明文密钥的地方) */
   url: string;
   /** socket 传输的套接字路径 —— 与 `url` 同档(结构,不是密钥):读得回来才改得动 */
   socket: string;
   env_keys: string[];
   header_keys: string[];
   /** 声明里关了(`disabled: true`)—— 仍在界面上可见,只是不连 */
   disabled: boolean;
   /** `directTools`:`true` / 名字数组 / `false`(默认走代理) */
   direct_tools: boolean | string[];
   /** qi-mcp 不认识的字段名(生态字段还在长,只提示不判死) */
   unknown_fields: string[];
   source: string;
}

/** 一处 mcp.json(全局 / 项目 / 某个角色私有)。`exists=false` 也回,前端据此区分空态。 */
export interface McpSource {
   scope: "global" | "project" | "role";
   /** scope=="role" 时是角色名 */
   role: string;
   path: string;
   exists: boolean;
   servers: McpServerInfo[];
}

/** `GET /api/mcp`。`unavailable` = 没装 qi-mcp(界面说清楚,而不是当"没有声明")。 */
export interface McpList {
   sources: McpSource[];
   unavailable: boolean;
}

// ── MCP 声明的写操作(增删改 `/api/mcp`)──────────────

/**
 * 可写的传输**闭集**(与宿主 `schemas.MCP_TRANSPORTS` 逐字同一份)。
 *
 * 末尾的 `http` 是老写法的推断名:读端会把一份没写 `type` 的声明报成 `http`,
 * 不收下它,一份老声明在界面上改一次就存不回去(宿主会 400)。
 */
export const MCP_TRANSPORTS = [
   "stdio",
   "streamable-http",
   "sse",
   "socket",
   "http",
] as const;

export type McpScope = McpSource["scope"];

/**
 * `POST /api/mcp` 的 server 体。
 *
 * **值只进不回**:`command` / `args` / `env` / `headers` 的值必须写得进文件,但读接口
 * 一个都不回 —— 所以编辑时这几个框是“留空 = 不改”(见 `McpPanel` 的表单提示)。
 */
export interface McpServerInput {
   name: string;
   /** `MCP_TRANSPORTS` 之一(写进声明里的 `type`) */
   transport: string;
   url: string;
   command: string;
   args: string[];
   cwd: string;
   socket: string;
   env: Record<string, string>;
   headers: Record<string, string>;
   disabled: boolean;
   direct_tools: boolean | string[];
}

export interface McpWriteBody {
   scope: McpScope;
   /** `scope === "role"` 时是哪个角色 */
   role?: string;
   server: McpServerInput;
}

/** PATCH:**只有送出去的键会被改**(宿主按 `model_fields_set` 判)。 */
export interface McpPatchBody {
   scope: McpScope;
   role?: string;
   /** 改**之前**的名字;要改名写进 `patch.name` */
   name: string;
   patch: Partial<McpServerInput>;
}

export interface McpDeleteBody {
   scope: McpScope;
   role?: string;
   name: string;
}

/** `GET /api/agents`。`unavailable` = 没装 qi-agents。 */
export interface AgentList {
   agents: AgentInfo[];
   unavailable: boolean;
}

/**
 * `DELETE /api/agents` 的请求体。`scope` 的词汇是 **qi-agents 的**(`user` / `project`),
 * 与 `AgentInfo.source` 同一套 —— 界面显示什么词,请求就发什么词;技能那套 `global`
 * 在这里 `422`。`name` 是 **frontmatter 的显示名**,不是目录名。
 */
export interface AgentDeleteBody {
   scope: "user" | "project";
   name: string;
}

// ── A2A 条目(两层 a2a.json)─────────────────────────────
//
// 与 MCP 同一套读写口径,但**只有两层**(`global` / `project`),而且合并是
// **同名深合并** —— 所以读端除逐层原样,还回一份 `resolved`(现在到底连的是哪)。

/** 可写的层(闭集,与宿主 `schemas.A2A_SCOPES` 逐字同一份)。 */
export const A2A_SCOPES = ["global", "project"] as const;
export type A2AScope = (typeof A2A_SCOPES)[number];

export interface A2ASkill {
   id: string;
   name: string;
   description: string;
   tags: string[];
}

/**
 * 一个 A2A 条目的**结构视图**。`headers` 只给键名 —— 值不出宿主(那是 token
 * 最常待的地方),所以编辑表单里那一栏是"留空 = 不改"。
 */
export interface A2AEntryInfo {
   name: string;
   card_url: string;
   base_url: string;
   rpc_url: string;
   tenant: string;
   description: string;
   header_keys: string[];
   timeout_ms: number;
   streaming: boolean;
   discovery: string;
   direct_tools: boolean | string[];
   /** 直连工具名(`a2a__<条目>`);没开 `directTools` 时是空串 */
   tool_name: string;
   disabled: boolean;
   unknown_fields: string[];
   /** 这条声明来自哪一层;`resolved` 里是**主导层** */
   source: string;
   /** 参与的层(深合并的痕迹);只有 `resolved` 里有 */
   layers: string[];
}

/** 一处 a2a.json。`exists=false` 也回 —— 前端据此区分"没这个文件"与"文件在但没写条目"。 */
export interface A2ASource {
   scope: A2AScope;
   path: string;
   exists: boolean;
   entries: A2AEntryInfo[];
}

/** `GET /api/a2a`。`unavailable` = 没装 qi-a2a。 */
export interface A2AList {
   sources: A2ASource[];
   /** 两层深合并后的生效视图(含被 `disabled` 关掉的) */
   resolved: A2AEntryInfo[];
   unavailable: boolean;
}

/**
 * `POST /api/a2a` 的条目体。字段名用 snake_case(与所有 `/api` 一致),
 * 落盘时由宿主转成 a2a.json 的 camelCase。
 */
export interface A2AEntryInput {
   name: string;
   card_url?: string;
   base_url?: string;
   rpc_url?: string;
   tenant?: string;
   description?: string;
   /** 只进不回:读端给不出值,所以编辑时"留空 = 保持"、填 `{}` = 清空整张表 */
   headers?: Record<string, string>;
   timeout_ms?: number | null;
   streaming?: boolean | null;
   discovery?: string;
   direct_tools?: boolean | string[];
   tool_prefix?: string;
   disabled?: boolean;
}

export interface A2AWriteBody {
   scope: A2AScope;
   entry: A2AEntryInput;
}

/** PATCH:**只有送出去的键会被改**;送 `null` 也读作"不改"。 */
export interface A2APatchBody {
   scope: A2AScope;
   /** 改**之前**的名字;要改名写进 `patch.name` */
   name: string;
   patch: Partial<A2AEntryInput>;
}

export interface A2ADeleteBody {
   scope: A2AScope;
   name: string;
}

/** `POST /api/a2a/probe`:去真实端点拉一次名片(只收已配置的条目名)。 */
export interface A2AProbeRequest {
   server?: string;
   agent?: string;
}

export interface A2AProbeCard {
   name: string;
   description: string;
   tenant: string;
   rpc_url: string;
   skills: A2ASkill[];
}

/** 探测结果。**失败也是 200**:`ok=false` + `error`(那是结果,不是宿主异常)。 */
export interface A2AProbeResult {
   ok: boolean;
   server: string;
   source: string;
   discovery_url: string;
   elapsed_ms: number;
   cards: A2AProbeCard[];
   error: string;
}

// ── AG-UI:输入 ───────────────────────────────────────────

export interface AguiMessage {
   id?: string;
   role:
      | "user"
      | "assistant"
      | "system"
      | "tool"
      | "developer"
      | "reasoning"
      | "activity";
   content: string;
   name?: string;
}

export interface RunAgentInput {
   threadId: string;
   runId: string;
   messages: AguiMessage[];
   state?: unknown;
   tools?: unknown[];
   /** AG-UI 自带的逃生舱。qi 用它传 `agent`(点名一个 agent,不走分派)。 */
   forwardedProps?: Record<string, unknown>;
}

// ── AG-UI:事件 ───────────────────────────────────────────

/** 所有事件共有的底。`metadata` 是 AG-UI 的 open-by-key 扩展槽。 */
interface Base {
   type: string;
   timestamp?: number;
   metadata?: Record<string, unknown>;
   subagentRunId?: string;
}

interface MsgBase extends Base {
   messageId: string;
}

export type AguiEvent =
   // 生命周期
   | (Base & {
        type: "RUN_STARTED";
        threadId: string;
        runId: string;
        parentRunId?: string;
     })
   | (Base & {
        type: "RUN_FINISHED";
        outcome?: { type: string; interrupts?: unknown[] };
        result?: string;
     })
   | (Base & { type: "RUN_ERROR"; message: string; code?: string })
   | (Base & { type: "STEP_STARTED"; stepName: string })
   | (Base & { type: "STEP_FINISHED"; stepName: string })
   // 文本
   | (MsgBase & { type: "TEXT_MESSAGE_START"; role: string })
   | (MsgBase & { type: "TEXT_MESSAGE_CONTENT"; delta: string })
   | (MsgBase & { type: "TEXT_MESSAGE_END" })
   // 思考(AG-UI 的 THINKING_* 已弃用,现行是 REASONING_*)
   | (MsgBase & { type: "REASONING_START" })
   | (MsgBase & { type: "REASONING_MESSAGE_START"; role: string })
   | (MsgBase & { type: "REASONING_MESSAGE_CONTENT"; delta: string })
   | (MsgBase & { type: "REASONING_MESSAGE_END" })
   | (MsgBase & { type: "REASONING_END" })
   // 工具
   | (Base & {
        type: "TOOL_CALL_START";
        toolCallId: string;
        toolCallName: string;
        parentMessageId?: string;
     })
   | (Base & { type: "TOOL_CALL_ARGS"; toolCallId: string; delta: string })
   | (Base & { type: "TOOL_CALL_END"; toolCallId: string })
   | (Base & {
        type: "TOOL_CALL_RESULT";
        messageId: string;
        toolCallId: string;
        content: string;
        role?: string;
     })
   // 状态
   | (Base & { type: "STATE_SNAPSHOT"; snapshot: unknown })
   | (Base & { type: "STATE_DELTA"; delta: unknown[] })
   | (Base & { type: "MESSAGES_SNAPSHOT"; messages: AguiMessage[] })
   // 活动(AG-UI 的通用"结构化活动"通道 —— todo / plan 这类走这里)
   | (Base & {
        type: "ACTIVITY_SNAPSHOT";
        messageId: string;
        activityType: string;
        /** qi 的直播帧带:同一子 run 的稳定标识(同一 run 的多帧相等)。 */
        subagentRunId?: string;
        content: unknown;
        replace?: boolean;
     })
   | (Base & {
        type: "ACTIVITY_DELTA";
        messageId: string;
        activityType: string;
        patch: unknown[];
     })
   // 多智能体(0.0.59 的 SUBAGENT_* 事件族):生 / 死走原生事件,内容走 ACTIVITY_*
   | (Base & {
        type: "SUBAGENT_STARTED";
        subagentRunId: string;
        name: string;
        description?: string;
        parentSubagentRunId?: string;
        parentToolCallId?: string;
        parentMessageId?: string;
        /** qi 额外带的归卡 / 展示字段(`toolCallId` / `depth` / `parent`)。 */
        metadata?: Record<string, unknown>;
     })
   | (Base & {
        type: "SUBAGENT_FINISHED";
        subagentRunId: string;
        result?: unknown;
        outcome?: unknown;
        metadata?: Record<string, unknown>;
     })
   | (Base & {
        type: "SUBAGENT_ERROR";
        subagentRunId: string;
        message: string;
        code?: string;
        metadata?: Record<string, unknown>;
     })
   // 扩展点
   | (Base & { type: "CUSTOM"; name: string; value: unknown })
   | (Base & { type: "RAW"; event: unknown; source?: string });

/** `CUSTOM` 里 qi 自己的名字(AG-UI 不认识它们,严格客户端会忽略)。 */
export const QI_CUSTOM = {
   dispatch: "qi.dispatch",
   opening: "qi.opening",
   narration: "qi.narration",
   compaction: "qi.compaction",
   branch: "qi.branch",
   history: "qi.history",
   /** 一次 LLM 调用失败、qi 正在重跑它(pi 的 agent 级重试):前端据此**撤回半截输出**。 */
   retry: "qi.retry",
   /** 工具结构化结果挂在 TOOL_CALL_RESULT.metadata 下的键。 */
   toolMeta: "qi.tool",
   usage: "qi.usage",
} as const;

// ── 模型面(`GET /api/models` / `POST /api/model`)──────────────
//
// "当前是哪个模型"是**会话级**的:同一个宿主进程服务同一 cwd 下的多条会话,每条都可能
// 记着不同的模型(续会话会按会话里的 entry 还原)。所以清单请求带 `session`,而
// `ConfigView.default_model` 说的是"settings 里的默认" —— 两件事,别混。

/** 模型菜单里的一条。 */
export interface ModelOption {
   provider: string;
   id: string;
   /** 上下文窗口(tokens)。0 = 取不到 → 不画占用条。 */
   context_window: number;
}

export interface ModelCatalog {
   models: ModelOption[];
   /** 当前生效的 `provider/id`;空 = 没有可用模型。 */
   currently: string;
   thinking_level: string;
   /** 合法级别(照后端给的那份画,不在前端写死)。 */
   thinking_levels: string[];
}

// ── 商店(`/api/store/*`)───────────────────────────────────────
//
// 形状照 `qi_lab/schemas.py` 的 `Store*`(那些模型又是照 `qi_store.service` 的 dict 写的)。
// `available=false` 是**正常返回**:qi-store 没装 / 配置坏了 —— 与 `/api/agents` 的
// `unavailable` 同一套口径,不要当错误处理。

export interface StoreSource {
   name: string;
   baseUrl: string;
   default: boolean;
   loggedIn: boolean;
   login: string;
   role: string;
   /** 令牌到期的 unix 秒;null = 没有令牌(未登录) */
   expiresAt: number | null;
   /** 还剩多少秒;null = 没有令牌 */
   expiresIn: number | null;
}

export interface StoreStatus {
   available: boolean;
   sources: StoreSource[];
   defaultScope: "global" | "project";
   /** `store.json` 的 `wanted[]`(项目声明"应该有"的条目) */
   wanted: string[];
   warnings: string[];
   storeFile: string;
   error: string;
}

export type StoreKind = "skill" | "mcp" | "a2a" | "kb";

export interface StoreItemOut {
   id: string;
   kind: StoreKind;
   name: string;
   description: string;
   /** 内容指纹(`sig:` 前缀)—— 上游没有 semver */
   version: string;
   updatedAt: string;
   requiresConfig: boolean;
   /** skill 的安全扫描:`scan_status` / `reviewed` */
   safety: Record<string, unknown>;
   badges: Record<string, unknown>;
   installed: boolean;
   installedVersion: string | null;
   updateAvailable: boolean;
}

export interface StoreMissing {
   kind: string;
   /** 为什么这一类没查成(未登录 / 网络)。**必须显示**,否则会误以为商店是空的 */
   reason: string;
}

export interface StoreList {
   available: boolean;
   store: string;
   items: StoreItemOut[];
   total: number;
   missing: StoreMissing[];
   cachedAt: number | null;
   error: string;
}

export interface StoreInstalledItem {
   id: string;
   kind: string;
   name: string;
   version: string;
   scope: string;
   /** 落点:技能目录 或 `mcp.json` / `a2a.json` */
   target: string;
   /** 还需要用户自己 export 的环境变量名 */
   needsConfig: string[];
   installedAt: string;
   upstreamUpdatedAt: string;
   /** 磁盘上还在不在(账本只记"装过什么";别处删掉时这里是 false) */
   present: boolean;
   /** present=false 时的人话原因 */
   missingReason: string;
}

export interface StoreInstalled {
   available: boolean;
   items: StoreInstalledItem[];
   error: string;
}

export interface StorePlanItem {
   id: string;
   name: string;
   note: string;
   targets: string[];
   needsConfig: string[];
   conflict: string | null;
   unsafe: boolean;
}

export interface StorePlan {
   available: boolean;
   scope: string;
   items: StorePlanItem[];
   skipped: { id: string; reason: string }[];
   needsConfig: string[];
   /** 有冲突且没 force → 界面必须先让用户选"覆盖"再重发 */
   blocked: boolean;
   error: string;
}

export interface StorePlanBody {
   ids: string[];
   scope?: "global" | "project" | null;
   force?: boolean;
}

export interface StoreInstallBody extends StorePlanBody {
   allowUnsafe?: boolean;
}

export interface StoreInstallResult {
   available: boolean;
   scope: string;
   installed: StoreInstalledItem[];
   skipped: { id: string; reason: string }[];
   needsConfig: string[];
   error: string;
}

export interface StoreUninstallResult {
   available: boolean;
   id: string;
   removed: string[];
   error: string;
}

export interface StoreCheckItem {
   id: string;
   name: string;
   scope: string;
   version: string;
   upstreamVersion: string | null;
   /** up-to-date | update | gone(上游没了) | missing(本地没了) */
   status: string;
   /** missing 时的具体原因 */
   detail?: string;
}

export interface StoreCheck {
   available: boolean;
   items: StoreCheckItem[];
   wantedMissing: { spec: string; reason: string }[];
   error: string;
}

export interface StoreLoginBody {
   login: string;
   password: string;
   /** 第一次登录必须给(还没有源);之后可省,沿用已配置地址 */
   baseUrl?: string;
   /** 多源时选一个;省略 = defaultStore */
   store?: string;
}

export interface StoreLogoutBody {
   store?: string;
   all?: boolean;
}

// ── 知识库（qi-kb）────────────────────────────────────────
// 形状照 `qi_lab/schemas.py` 的 `Kb*`。

export interface KbProvider {
   name: string;
   baseUrl: string;
   apiPrefix: string;
   instance: string;
   login: string;
   role: string;
   loggedIn: boolean;
   expiresAt: number | null;
   expiresIn: number | null;
   kbCount: number;
   scope: string;
}

export interface KbStatus {
   available: boolean;
   providers: KbProvider[];
   knowledgeBases: Record<string, unknown>[];
   defaults: Record<string, unknown>;
   defaultProvider: string;
   defaultKnowledgeBase: string;
   files: string[];
   warnings: string[];
   error: string;
}

export interface KbItem {
   id: string;
   provider: string;
   name: string;
   description: string;
   embeddingModel: string;
   visibility: string;
   docCount: number;
   chunkCount: number;
   updatedAt: string;
   icon: string;
   bound: boolean;
   boundProvider: string | null;
   scope?: string;
}

export interface KbList {
   available: boolean;
   items: KbItem[];
   total: number;
   warnings: string[];
   error: string;
}

export interface KbSearchResult {
   available: boolean;
   provider: string;
   query: string;
   results: Record<string, unknown>[];
   total: number;
   strategy: string;
   rerankMethod: string | null;
   kbIds: string[];
   warnings: string[];
   error: string;
}

export interface KbAskResult {
   available: boolean;
   provider: string;
   question: string;
   answer: string;
   sources: Record<string, unknown>[];
   kbIdsUsed: string[];
   evidenceSufficient: boolean;
   warnings: string[];
   error: string;
}

export interface KbPlan {
   available: boolean;
   action: string;
   kbId: string;
   provider: string;
   existingProvider: string | null;
   targetProvider: string;
   blocked: boolean;
   warnings: string[];
   error: string;
}

export interface KbInstallResult {
   available: boolean;
   action: string;
   kbId: string;
   provider: string;
   scope: string;
   name: string;
   warnings: string[];
   error: string;
}

export interface KbStreamEvent {
   type: string;
   content?: unknown;
   message?: string;
   provider?: string;
   kbIds?: string[];
   [key: string]: unknown;
}
