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
   cwd: string | null;
   message_count: number;
   running: boolean;
   path: string;
}

export interface SessionList {
   sessions: SessionSummary[];
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
}

export interface AgentInfo {
   name: string;
   display_name: string;
   description: string;
   source: string;
   tools: string[];
   keywords: string[];
   skills: number;
   data_sources: number;
   mcp_private: number;
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
   default_model: string | null;
   default_model_source: string;
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

/** 已装载的插件名。`discover_plugins()` 只返回名字,所以这里也只回名字。 */
export interface PluginList {
   plugins: string[];
}

export interface AgentList {
   agents: AgentInfo[];
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
