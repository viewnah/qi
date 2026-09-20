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
}

export interface ProviderInfo {
   name: string;
   base_url: string | null;
   api: string | null;
   models: number;
   credential_ok: boolean;
   credential_source: string;
   credential_masked: string;
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
 * `details` 是**插件唯一的 UI 下行通道**(docs/web.md §16):开放字典,
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
           state?: "done" | "active" | "pending";
           note?: string;
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
 * 一个 MCP server 的**结构**(不含值,见 docs/web.md §18.18)。
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

/** `GET /api/agents`。`unavailable` = 没装 qi-agents。 */
export interface AgentList {
   agents: AgentInfo[];
   unavailable: boolean;
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
        content: unknown;
        replace?: boolean;
     })
   | (Base & {
        type: "ACTIVITY_DELTA";
        messageId: string;
        activityType: string;
        patch: unknown[];
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
   /** 工具结构化结果挂在 TOOL_CALL_RESULT.metadata 下的键。 */
   toolMeta: "qi.tool",
   usage: "qi.usage",
} as const;
