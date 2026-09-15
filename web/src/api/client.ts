/**
 * API 客户端:唯一允许与宿主说话的地方。
 *
 * 两条刻意的约束(为将来的桌面壳留路,见 docs/web.md):
 *   1. **不依赖 cookie 会话** —— 口令走 `Authorization` 头,换到别的 origin 的壳里也不失效。
 *   2. **不用 `EventSource`** —— 它不能带自定义头、也不能中途 abort(Stop 按钮靠后者)。
 *      代价是要自己切帧。AG-UI 的编码器只写 `data:` 行(`encodeSSE` =
 *      `` `data: ${JSON.stringify(e)}\n\n` ``),所以切帧比通用 SSE 还简单;
 *      但仍按 SSE 规范处理"多行 data 用 \n 连接",以免将来编码器变化就静默截断。
 *
 * **与改造前的关键差别**:一次运行是**单次 POST**,响应**就是**流。
 * 不再有"POST 拿 run_id → 再 GET /events"的两跳,也就没有"晚连上来的客户端"这件事。
 */
import { CONTRACT_VERSION } from "./types";
import type {
  AgentInfo,
  AgentList,
  AguiEvent,
  ConfigView,
  Meta,
  PluginList,
  RunAgentInput,
  SessionDetail,
  SessionList,
  SessionSummary,
  SkillList,
} from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

/** 契约不匹配是**显式**错误,不是白屏。 */
export class ContractError extends Error {
  constructor(
    public readonly hostContract: string,
    public readonly uiContract: string,
  ) {
    super(
      `宿主契约 v${hostContract} 与页面需要的 v${uiContract} 不一致,请更新其中一侧。`,
    );
    this.name = "ContractError";
  }
}

async function detailOf(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    return JSON.stringify(body.detail ?? body);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  if (res.status === 204) return undefined as T;
  if (!res.ok) throw new ApiError(res.status, await detailOf(res));
  return (await res.json()) as T;
}

export const api = {
  async meta(): Promise<Meta> {
    const meta = await request<Meta>("/api/meta");
    if (meta.contract !== CONTRACT_VERSION) {
      throw new ContractError(meta.contract, CONTRACT_VERSION);
    }
    return meta;
  },

  sessions: () => request<SessionList>("/api/sessions"),

  createSession: (title: string, cwd?: string) =>
    request<SessionSummary>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title, cwd: cwd ?? null }),
    }),

  session: (id: string) =>
    request<SessionDetail>(`/api/sessions/${encodeURIComponent(id)}`),

  renameSession: (id: string, title: string) =>
    request<SessionSummary>(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),

  deleteSession: (id: string) =>
    request<void>(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),

  agents: () => request<AgentList>("/api/agents"),

  agentList: async (): Promise<AgentInfo[]> => (await api.agents()).agents,

  config: () => request<ConfigView>("/api/config"),

  skills: () => request<SkillList>("/api/skills"),

  plugins: () => request<PluginList>("/api/plugins"),

  /** 写凭证必须带确认头:后端也会拦(428),这是双保险。 */
  setAuth: (provider: string, key: string) =>
    request<void>(`/api/auth/${encodeURIComponent(provider)}`, {
      method: "POST",
      headers: { "X-Qi-Confirm": "yes" },
      body: JSON.stringify({ key }),
    }),

  deleteAuth: (provider: string) =>
    request<void>(`/api/auth/${encodeURIComponent(provider)}`, {
      method: "DELETE",
      headers: { "X-Qi-Confirm": "yes" },
    }),
};

// ── 流式运行(AG-UI)─────────────────────────────────────

/**
 * 按 `\n\n` 切帧,再按 SSE 规范从每帧里取 `data:`(多行用 `\n` 连接)。
 *
 * 刻意**不解析** `event:` —— AG-UI 的编码器不写它,事件类型在 JSON 的 `type` 里。
 * 但如果将来编码器开始写(SSE 允许),这里也不会因此丢帧(只是忽略该字段)。
 *
 * @returns 已完成的 JSON 载荷 + 尚未完整、留给下次的尾巴
 */
export function parseFrames(buffer: string): { payloads: string[]; rest: string } {
  const payloads: string[] = [];
  let rest = buffer;
  for (;;) {
    const idx = rest.indexOf("\n\n");
    if (idx < 0) break;
    const raw = rest.slice(0, idx);
    rest = rest.slice(idx + 2);
    const dataLines: string[] = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith(":")) continue; // 注释/心跳:AG-UI 不用,但合法
      const colon = line.indexOf(":");
      if (colon < 0) continue;
      const field = line.slice(0, colon);
      if (field === "data") dataLines.push(line.slice(colon + 1).replace(/^ /, ""));
    }
    if (dataLines.length > 0) payloads.push(dataLines.join("\n"));
  }
  return { payloads, rest };
}

export interface RunHandlers {
  onEvent: (ev: AguiEvent) => void;
  onOpen?: () => void;
  /** 流正常结束(收到 RUN_FINISHED 或服务端关闭)。 */
  onClose?: () => void;
  onError?: (err: Error) => void;
}

/**
 * 跑一轮:单次 POST `RunAgentInput`,响应就是 AG-UI 事件流。
 *
 * 取消 = `abort()`。**不要**在服务端另开取消端点:单 POST 之下"客户端断开"
 * 就是唯一的取消语义,而且它天然覆盖"过一会儿才发现"的情况。
 *
 * @param threadId qi 的 session id(AG-UI 的 `threadId`)。qi 的 fork 会新建会话文件,
 *   所以每个分支天然是独立 thread,不需要额外映射。
 * @param text 本轮用户输入。后端自己从会话 JSONL 取上下文,所以只送这一条。
 * @returns abort 函数(交给 Stop 按钮)
 */
export function run(
  threadId: string,
  text: string,
  handlers: RunHandlers,
  opts: { agent?: string | null } = {},
): () => void {
  const controller = new AbortController();
  const body: RunAgentInput = {
    threadId,
    runId: crypto.randomUUID().replace(/-/g, "").slice(0, 12),
    messages: [{ role: "user", content: text }],
    forwardedProps: { agent: opts.agent ?? null },
  };

  void (async () => {
    try {
      const res = await fetch("/api/ag-ui", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) throw new ApiError(res.status, await detailOf(res));
      handlers.onOpen?.();
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        const { payloads, rest } = parseFrames(buffer);
        buffer = rest;
        for (const raw of payloads) {
          let ev: AguiEvent;
          try {
            ev = JSON.parse(raw) as AguiEvent;
          } catch {
            // 单帧坏掉不该毁掉整条流:丢掉它,继续读。
            continue;
          }
          handlers.onEvent(ev);
        }
      }
      handlers.onClose?.();
    } catch (err) {
      // abort 是**正常**路径(用户按了 Stop),不是错误。
      if (controller.signal.aborted) {
        handlers.onClose?.();
      } else {
        handlers.onError?.(err instanceof Error ? err : new Error(String(err)));
      }
    }
  })();

  return () => controller.abort();
}
