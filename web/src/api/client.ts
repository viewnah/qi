/**
 * API 客户端:唯一允许与 `/api` 说话的地方。
 *
 * 两条刻意的约束(为将来的桌面端/Tauri 留路,见 docs/web.md §8.2):
 *   1. **不依赖 cookie 会话** —— 口令走 `Authorization` 头,浏览器会为同源请求
 *      自动复用 Basic 凭证;换到 `dsh-app://` 之类的壳里也不会因为 origin 不同而失效。
 *   2. **SSE 用 fetch + ReadableStream,不用 EventSource** —— 后者无法带自定义头,
 *      也没法在中途 abort。代价是要自己切帧(见 `parseFrames`)。
 */
import { CONTRACT_VERSION } from "./types";
import type {
  AgentEvent,
  AgentInfo,
  ConfigView,
  Meta,
  PluginList,
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

  session: (id: string, opts: { limit?: number; before?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit !== undefined) q.set("limit", String(opts.limit));
    if (opts.before !== undefined) q.set("before", String(opts.before));
    const suffix = q.toString() ? `?${q}` : "";
    return request<SessionDetail>(
      `/api/sessions/${encodeURIComponent(id)}${suffix}`,
    );
  },

  renameSession: (id: string, title: string) =>
    request<SessionSummary>(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),

  deleteSession: (id: string) =>
    request<void>(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  turn: (id: string, text: string, agent?: string | null) =>
    request<{ run_id: string; session_id: string; from_seq: number }>(
      `/api/sessions/${encodeURIComponent(id)}/turn`,
      { method: "POST", body: JSON.stringify({ text, agent: agent ?? null }) },
    ),

  cancel: (id: string) =>
    request<{ cancelled: boolean }>(
      `/api/sessions/${encodeURIComponent(id)}/cancel`,
      {
        method: "POST",
      },
    ),

  agents: () => request<{ agents: AgentInfo[] }>("/api/agents"),

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

// ── SSE ───────────────────────────────────────────────────

export interface StreamHandlers {
  onOpen?: () => void;
  onEvent?: (ev: AgentEvent) => void;
  onSnapshot?: (snapshot: SessionDetail) => void;
  onFinished?: (status: string) => void;
  onError?: (err: Error) => void;
}

interface Frame {
  id?: number;
  event: string;
  data: string;
}

/** 切 SSE 帧。按 `\n\n` 分帧,帧内按行解析(`event:` / `data:` / `id:` / 注释)。 */
export function parseFrames(buffer: string): { frames: Frame[]; rest: string } {
  const frames: Frame[] = [];
  let rest = buffer;
  for (;;) {
    const idx = rest.indexOf("\n\n");
    if (idx < 0) break;
    const raw = rest.slice(0, idx);
    rest = rest.slice(idx + 2);
    let event = "message";
    let id: number | undefined;
    const dataLines: string[] = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith(":")) continue; // 心跳/注释
      const colon = line.indexOf(":");
      if (colon < 0) continue;
      const field = line.slice(0, colon);
      const value = line.slice(colon + 1).trimStart();
      if (field === "event") event = value;
      else if (field === "id") id = Number(value);
      else if (field === "data") dataLines.push(value);
    }
    if (dataLines.length > 0)
      frames.push({ id, event, data: dataLines.join("\n") });
  }
  return { frames, rest };
}

/**
 * 订阅某会话的事件流。返回一个 abort 函数。
 *
 * 服务端约定:每代以 `snapshot` 开头,run 结束后发 `run.finished` 并关闭连接。
 * 因此调用方在 `onFinished` 里重新拉一次会话明细即可拿到落盘结果(含叙述与工具卡)。
 */
export function stream(
  sessionId: string,
  handlers: StreamHandlers,
  opts: { runId?: string; from?: number } = {},
): () => void {
  const controller = new AbortController();
  const q = new URLSearchParams();
  if (opts.runId) q.set("run_id", opts.runId);
  if (opts.from) q.set("from", String(opts.from));
  const suffix = q.toString() ? `?${q}` : "";

  void (async () => {
    try {
      const res = await fetch(
        `/api/sessions/${encodeURIComponent(sessionId)}/events${suffix}`,
        { signal: controller.signal, headers: { Accept: "text/event-stream" } },
      );
      if (!res.ok || !res.body) {
        throw new ApiError(res.status, await detailOf(res));
      }
      handlers.onOpen?.();
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        const { frames, rest } = parseFrames(buffer);
        buffer = rest;
        for (const frame of frames) {
          if (frame.event === "snapshot") {
            handlers.onSnapshot?.(JSON.parse(frame.data) as SessionDetail);
            continue;
          }
          if (frame.event === "run.finished") {
            const payload = JSON.parse(frame.data) as { status: string };
            handlers.onFinished?.(payload.status);
            continue;
          }
          const ev = JSON.parse(frame.data) as AgentEvent;
          handlers.onEvent?.(ev);
        }
      }
    } catch (err) {
      if (!controller.signal.aborted) {
        handlers.onError?.(err instanceof Error ? err : new Error(String(err)));
      }
    }
  })();

  return () => controller.abort();
}
