/**
 * API 客户端:唯一允许与宿主说话的地方。
 *
 * 两条刻意的约束(为将来的桌面壳留路,见 design/web.md):
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
// 会话/工作区的写操作也在这里:分叉、改名、以及工作区的两个「人工决定」。
import type {
  AgentInfo,
  AgentList,
  AguiEvent,
  /** 手动压缩的结果(`POST /api/sessions/{id}/compact`)。 */
  CompactionResult,
  /** 批量删除的结果(`DELETE /api/sessions?scope=ungrouped`)。 */
  DeletedSessions,
  /** 目录选择器(`GET /api/fs/dirs`)。 */
  DirectoryListing,
  ConfigView,
  /** 已装载的扩展名(`GET /api/extensions`)。 */
  ExtensionList,
  FileContent,
  FileListing,
  McpList,
  Meta,
  ModelCatalog,
  RunAgentInput,
  SessionDetail,
  SessionList,
  SessionSummary,
  SkillList,
  WorkspaceDeleted,
  WorkspaceNames,
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
    request<void>(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  /**
   * 清除「未分组」桶:**没有 cwd 的旧会话**会被一起删掉(不可恢复)。
   *
   * 作用域是闭集(`scope=ungrouped`),拼错会被后端 422 拒掉 —— 不能悄悄变成"删全部"。
   */
  clearUngrouped: () =>
    request<DeletedSessions>("/api/sessions?scope=ungrouped", {
      method: "DELETE",
    }),

  /**
   * 导出会话 JSONL(`GET /api/sessions/{id}/export`)。
   *
   * 用 fetch + Blob,而不是 `window.location` 或 `<a href>`:前者能带上应用自己的
   * 鉴权头、也能把 404/401 读成一句错误 —— 直接导航到 URL 只会得到浏览器自己的错误页。
   * 文件名**由后端给**(`Content-Disposition` 里的 `<标题>-<id>.jsonl`),前端不拼名字 ——
   * 标题可以出现任意字符,拼名字的规则只应该有一处。
   */
  exportSession: async (id: string): Promise<{ blob: Blob; name: string }> => {
    const res = await fetch(`/api/sessions/${encodeURIComponent(id)}/export`);
    if (!res.ok) throw new ApiError(res.status, await detailOf(res));
    const disposition = res.headers.get("Content-Disposition") ?? "";
    // 后端按 RFC 5987 双写:优先 `filename*=UTF-8''…`(标题可以是中文),
    // 回落到 ASCII 的 `filename="…"`。
    const utf8 = /filename\*=UTF-8''([^;]+)/.exec(disposition)?.[1];
    const name = utf8 === undefined
      ? /filename="([^"]+)"/.exec(disposition)?.[1] ?? `${id}.jsonl`
      : decodeURIComponent(utf8);
    return { blob: await res.blob(), name };
  },

  /** 手动压缩上下文(= TUI 的 `/compact`)。运行中会被 409 拒掉。 */
  compactSession: (id: string) =>
    request<CompactionResult>(
      `/api/sessions/${encodeURIComponent(id)}/compact`,
      { method: "POST", body: JSON.stringify({}) },
    ),

  /** 分叉:把当前分支复制成**新会话**(后端 201 回新会话的摘要)。 */
  /**
   * 分叉:`at` = 从哪个 entry 分叉(省略 = 当前节点,即最后一个完整回合)。
   * 省略与 `at=""` 含义不同(后者 = 从第一条消息之前),所以只在给了才进 body。
   */
  forkSession: (id: string, at?: string) =>
    request<SessionSummary>(
      `/api/sessions/${encodeURIComponent(id)}/fork`,
      { method: "POST", body: JSON.stringify(at === undefined ? {} : { at }) },
    ),

  /**
   * 列服务器上的子目录(`path` 省略 = 主目录)。
   *
   * **只读、只列目录**:权限面比已有的 bash 工具小得多,所以沿用同一道鉴权。
   * 形状照 pi-web 的 `/api/cwd/browse`(见 `api/types.ts` 的 `DirectoryListing`)。
   */
  dirs: (path = "") =>
    request<DirectoryListing>(
      `/api/fs/dirs?path=${encodeURIComponent(path)}`,
    ),

  /** 工作区改过的显示名(目录 → 名字)。`name` 空串 = 取消改名(恢复成目录名)。 */
  workspaces: () => request<WorkspaceNames>("/api/workspaces"),

  renameWorkspace: (cwd: string, name: string) =>
    request<WorkspaceNames>("/api/workspaces", {
      method: "PATCH",
      body: JSON.stringify({ cwd, name }),
    }),

  /** **删除工作区连同它的会话**(不可恢复;目录本身不删)。 */
  deleteWorkspace: (cwd: string) =>
    request<WorkspaceDeleted>(
      `/api/workspaces?cwd=${encodeURIComponent(cwd)}`,
      { method: "DELETE" },
    ),

  agents: () => request<AgentList>("/api/agents"),

  agentList: async (): Promise<AgentInfo[]> => (await api.agents()).agents,

  config: () => request<ConfigView>("/api/config"),

  /**
   * 可选模型清单 + 当前值(`GET /api/models`)。
   *
   * `session` 给了就按**那条会话**的口径答(后端会先绑定它)—— "当前是哪个模型"是
   * 会话级的状态,而不是宿主进程的全局状态。
   */
  models: (session: string | null = null) =>
    request<ModelCatalog>(
      `/api/models${session === null ? "" : `?session=${encodeURIComponent(session)}`}`,
    ),

  /**
   * 换模型 / 换思考级别(`POST /api/model`)。两个字段都可选、可只给一个 ——
   * 只给一个时另一个保持会话里的值(实现是 PATCH 语义,不是 PUT)。
   *
   * 返回更新后的清单:界面在同一次往返里把 chip 的字与菜单里的 ✓ 一起改掉。
   */
  setModel: (
    body: { provider?: string; model?: string; thinking_level?: string; session?: string | null },
  ) =>
    request<ModelCatalog>("/api/model", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  skills: () => request<SkillList>("/api/skills"),

  extensions: () => request<ExtensionList>("/api/extensions"),

  /** MCP 声明(全局 / 项目 / agent 私有)。老宿主回 404 → 调用方降级,不当作致命错误。 */
  mcp: () => request<McpList>("/api/mcp"),

  /** 列会话目录内**一层**目录(右侧文件面板;边界与文件工具同一条)。 */
  files: (session: string | null, path = "") =>
    request<FileListing>(
      `/api/files?path=${encodeURIComponent(path)}${
        session === null ? "" : `&session=${encodeURIComponent(session)}`
      }`,
    ),

  /** 文本预览。二进制不是错误:回 `kind: "binary"`。 */
  fileContent: (session: string | null, path: string) =>
    request<FileContent>(
      `/api/files/content?path=${encodeURIComponent(path)}${
        session === null ? "" : `&session=${encodeURIComponent(session)}`
      }`,
    ),

  /** 原字节预览的 URL:图片 / PDF / HTML 直接喂给 `img` / `iframe`,不走 fetch。 */
  fileRawUrl: (session: string | null, path: string) =>
    `/api/files/raw?path=${encodeURIComponent(path)}${
      session === null ? "" : `&session=${encodeURIComponent(session)}`
    }`,

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
export function parseFrames(buffer: string): {
  payloads: string[];
  rest: string;
} {
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
      if (field === "data")
        dataLines.push(line.slice(colon + 1).replace(/^ /, ""));
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
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok || !res.body)
        throw new ApiError(res.status, await detailOf(res));
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
