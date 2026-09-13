/**
 * `/api` v1 契约的类型镜像。
 *
 * 单一真相在 python 侧 `src/qi_agent/web/schemas.py`;这里是**手写镜像**。
 * 之所以手写:`CONTRACT_VERSION` 不匹配时前端要能**显式报错**而不是白屏,
 * 而自动生成(openapi → types)留到拆 UI 插件包时再做(见 docs/web.md §13)。
 * 两侧字段一旦改名,`tests/test_web_api.py` 与这里的类型会同时报错,不会静默漂移。
 */

export const CONTRACT_VERSION = "1";

export interface Meta {
 app: string;
 contract: string;
 version: string;
 default_cwd: string;
 auth_required: boolean;
 static_ready: boolean;
 capabilities: Record<string, boolean>;
}

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

/** 会话 JSONL 的一条 entry(五类 + header)。字段按 type 取舍,故多数可选。 */
export interface Entry {
 type: string;
 ts?: string;
 // header(entries[0])
 id?: string;
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
 // dispatch
 agent?: string | null;
 display_name?: string | null;
 confidence?: number;
 source?: string;
 reasoning?: string;
 // state / custom
 key?: string;
 value?: unknown;
 custom_type?: string;
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

/** 顶层技能(六层来源 + settings 追加)。 */
export interface SkillInfo {
 name: string;
 description: string;
 source: string;
 path: string;
}

export interface SkillList {
 skills: SkillInfo[];
}

/** 已装载的插件名:`discover_plugins()` 只返回名字,所以这里也只有名字。 */
export interface PluginList {
 plugins: string[];
}

export interface ConfigView {
 providers: ProviderInfo[];
 default_model: string | null;
 default_model_source: string;
 router_model: string | null;
 settings_files: string[];
 config_files: string[];
 session_dir: string;
 /** 凭证文件绝对路径:确认弹层要显示它,所以后端一并返回。 */
 auth_file: string;
 checks: { name: string; ok: boolean; detail: string }[];
}

/** 事件 data 里我们用到的形状(见 models.py 的字段约定)。 */
export interface ToolEndData {
 status: "ok" | "error";
 duration_ms: number;
 exit_code: number | null;
 error: string | null;
}

export interface DispatchData {
 confidence: number;
 source: string;
 agent: string | null;
 display_name: string;
 reasoning: string;
}

export interface UsageData {
 turns?: number;
 llm_calls?: number;
 prompt_tokens?: number;
 completion_tokens?: number;
 total_tokens?: number;
 [key: string]: number | undefined;
}

export interface AgentEvent {
 seq: number;
 kind: string;
 agent: string | null;
 tool: string | null;
 text: string;
 data: Record<string, unknown>;
}

export interface RunFinishedData {
 status: "ok" | "error" | "cancelled";
 error: string | null;
 run_id: string;
}
