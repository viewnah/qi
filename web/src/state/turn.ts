/**
 * AG-UI 事件 → **行**。
 *
 * 为什么是"行"不是消息气泡:qi 是多 agent harness,转录的本质是**因果轨迹**
 * (决策 → 思考 → 动作 → 结论),不是对称交谈。气泡会把"过程"和"结论"渲染成同类,
 * 结论被过程淹没。所以每行带明确 tone,读者可以扫着跳过过程、只读结论。
 *
 * 为什么用 `switch (ev.type)`:AG-UI 是判别联合,switch 能被编译器**穷尽检查**,
 * 漏一个分支就编译不过。旧版"读 SSE 的 event: 名 + 手写 kind 表"做不到这一点。
 */
import type { AguiEvent, Entry } from "../api/types";
import { QI_CUSTOM } from "../api/types";

// ── 行模型 ──────────────────────────────────────────────

export interface YouRow {
  kind: "you";
  key: string;
  text: string;
}

/** 分派决策。qi 独有(走 CUSTOM),也是这个 harness 相对普通 chat 的差异点。 */
export interface RouteRow {
  kind: "route";
  key: string;
  agent: string | null;
  label: string;
  confidence: number;
  source: string;
  reasoning: string;
}

export interface ThinkRow {
  kind: "think";
  key: string;
  text: string;
  live: boolean;
}

/**
 * `tone` 是 qi 的关键区分:
 *   final     —— 本轮的**结论**(没宣布工具调用的那条)
 *   narration —— 调工具**之前**的过程陈述
 *   opening   —— agent 开场白(不进 LLM 上下文)
 * 三者视觉权重不同,因为读者对它们的关心程度不同。
 */
export interface SayRow {
  kind: "say";
  key: string;
  agent: string | null;
  text: string;
  live: boolean;
  tone: "final" | "narration" | "opening";
}

export interface ToolRowData {
  kind: "tool";
  key: string;
  callId: string;
  tool: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "error";
  durationMs: number | null;
  exitCode: number | null;
  error: string | null;
  result: string;
}

export interface NoteRow {
  kind: "note";
  key: string;
  label: string;
  detail: string;
}

export interface ErrorRow {
  kind: "error";
  key: string;
  text: string;
}

export type Row = YouRow | RouteRow | ThinkRow | SayRow | ToolRowData | NoteRow | ErrorRow;

export interface TurnState {
  rows: Row[];
  /** usage 原样保存(键名由 provider 决定,不做白名单) */
  usage: Record<string, unknown>;
  /** STATE_SNAPSHOT 里的 cwd / title */
  host: { cwd?: string; title?: string };
  phase: "idle" | "running" | "ok" | "error";
  /**
   * AG-UI 的身份 → 行 key。
   *
   * 必须跨 run 唯一:`messageId` 只在一条流里唯一(每条流都从 `msg_1` 开始),
   * 而同一个会话可以跑很多轮、行是累积的。所以行 key 由前端自增生成,
   * 这个表只负责把 AG-UI 的 id 映射过去。
   */
  ids: Record<string, string>;
  /**
   * 本轮提交的输入。存在理由:`qi.history` 是在 run **开始前**抓的,
   * 里面没有这一轮的 user 消息(那是 run 开始后才落盘的),重新水合后要补回去。
   */
  pendingUser: string | null;
}

export const emptyTurn: TurnState = {
  rows: [],
  usage: {},
  host: {},
  phase: "idle",
  ids: {},
  pendingUser: null,
};

let counter = 0;
const nextKey = (prefix: string) => `${prefix}-${++counter}`;

const asStr = (v: unknown, fallback = ""): string =>
  typeof v === "string" ? v : fallback;
const asNum = (v: unknown, fallback = 0): number =>
  typeof v === "number" && Number.isFinite(v) ? v : fallback;
const asRecord = (v: unknown): Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};

// ── 归约辅助 ────────────────────────────────────────────

function patchByKey(rows: Row[], key: string, fn: (row: Row) => Row): Row[] {
  return rows.map((row) => (row.key === key ? fn(row) : row));
}

/** 找最后一个某种行。返回类型按 kind 收窄,调用方不必再断言。 */
function findLast<K extends Row["kind"]>(
  rows: Row[],
  kind: K,
): Extract<Row, { kind: K }> | undefined {
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const row = rows[i];
    if (row && row.kind === kind) return row as Extract<Row, { kind: K }>;
  }
  return undefined;
}

/** 流结束后收掉所有 live 标记,避免光标残留。 */
function settle(rows: Row[]): Row[] {
  return rows.map((row) =>
    row.kind === "say" || row.kind === "think" ? { ...row, live: false } : row,
  );
}

// ── 历史回放:qi 的 entries → 行 ─────────────────────────

/**
 * qi 的会话 entry → 行。实时流开头的 `qi.history` 与 REST 会话明细**共用这一条路径**,
 * 所以直播与回放必然一致 —— 这是 qi 一直守着的不变量。
 */
export function fromEntries(entries: Entry[]): Row[] {
  const rows: Row[] = [];
  for (const e of entries) {
    switch (e.type) {
      case "message": {
        const text = e.content ?? "";
        if (!text) break;
        if (e.role === "user") {
          rows.push({ kind: "you", key: nextKey("you"), text });
        } else if (e.role === "assistant") {
          rows.push({
            kind: "say",
            key: nextKey("say"),
            agent: e.agent_id ?? null,
            text,
            live: false,
            tone: "final",
          });
        }
        break;
      }
      case "custom": {
        // 叙述不是 message entry(刻意如此:它不进 LLM 上下文),
        // 但它是"过程",回放时必须出现,否则直播有、刷新没了。
        if (e.custom_type === "assistant_narration" && e.content) {
          rows.push({
            kind: "say",
            key: nextKey("say"),
            agent: e.agent_id ?? null,
            text: e.content,
            live: false,
            tone: "narration",
          });
        }
        break;
      }
      case "dispatch":
        rows.push({
          kind: "route",
          key: nextKey("route"),
          agent: e.agent ?? null,
          label: e.display_name || e.agent || "?",
          confidence: asNum(e.confidence),
          source: e.source ?? "",
          reasoning: e.reasoning ?? "",
        });
        break;
      case "tool":
        rows.push({
          kind: "tool",
          key: nextKey("tool"),
          callId: "",
          tool: e.tool ?? "tool",
          args: asRecord(e.args),
          status: e.status === "error" ? "error" : "ok",
          durationMs: typeof e.duration_ms === "number" ? e.duration_ms : null,
          exitCode: typeof e.exit_code === "number" ? e.exit_code : null,
          error: e.error ?? null,
          result: e.result ?? "",
        });
        break;
      case "compaction":
        rows.push({
          kind: "note",
          key: nextKey("note"),
          label: "上下文已压缩",
          detail: asStr(e.summary).slice(0, 400),
        });
        break;
      case "branch_summary":
        rows.push({
          kind: "note",
          key: nextKey("note"),
          label: "分支摘要",
          detail: asStr(e.content).slice(0, 400),
        });
        break;
      default:
        // session header / state / opening_shown:没有可展示内容
        break;
    }
  }
  return rows;
}

/**
 * 用水合历史替换整屏,并把本轮待发的输入补回去。
 *
 * 补回去这一步是必需的:`qi.history` 抓的是 run 开始**之前**的 entries,
 * 而用户消息是 run 开始后才落盘的。不补,水合之后本轮输入就消失了。
 */
export function hydrate(prev: TurnState, entries: Entry[]): TurnState {
  const rows = fromEntries(entries);
  const pending = prev.pendingUser;
  if (pending && !(findLast(rows, "you")?.text === pending)) {
    rows.push({ kind: "you", key: nextKey("you"), text: pending });
  }
  return { ...prev, rows, ids: {} };
}

// ── 用户输入(乐观显示)──────────────────────────────────

/**
 * 先把用户消息放上去。真实竞态:服务端要等 run 开始才落盘,所以中间有一段
 * "界面上有、磁盘上还没有"的窗口。宁可先显示(输入框清空、有反馈),
 * 也不要在网络往返期间让用户对着空屏发呆。
 */
export function withUserMessage(prev: TurnState, text: string): TurnState {
  return {
    ...prev,
    pendingUser: text,
    phase: "running",
    rows: [...prev.rows, { kind: "you", key: nextKey("you"), text }],
  };
}

// ── AG-UI 事件归约 ──────────────────────────────────────

export function reduce(state: TurnState, ev: AguiEvent): TurnState {
  switch (ev.type) {
    case "RUN_STARTED":
      return { ...state, phase: "running" };

    case "RUN_FINISHED": {
      const usage = asRecord(ev.metadata?.[QI_CUSTOM.usage]);
      return {
        ...state,
        phase: "ok",
        rows: settle(state.rows),
        usage: Object.keys(usage).length > 0 ? usage : state.usage,
      };
    }

    case "RUN_ERROR":
      return {
        ...state,
        phase: "error",
        rows: [
          ...settle(state.rows),
          { kind: "error", key: nextKey("err"), text: ev.message },
        ],
      };

    // ── 文本 ──
    case "TEXT_MESSAGE_START": {
      const key = nextKey("say");
      return {
        ...state,
        ids: { ...state.ids, [ev.messageId]: key },
        rows: [
          ...state.rows,
          { kind: "say", key, agent: null, text: "", live: true, tone: "final" },
        ],
      };
    }
    case "TEXT_MESSAGE_CONTENT": {
      const key = state.ids[ev.messageId];
      if (!key) return state;
      return {
        ...state,
        rows: patchByKey(state.rows, key, (row) =>
          row.kind === "say" ? { ...row, text: row.text + ev.delta } : row,
        ),
      };
    }
    case "TEXT_MESSAGE_END": {
      const key = state.ids[ev.messageId];
      if (!key) return state;
      return {
        ...state,
        rows: patchByKey(state.rows, key, (row) =>
          row.kind === "say" ? { ...row, live: false } : row,
        ),
      };
    }

    // ── 思考(AG-UI 的 THINKING_* 已弃用,现行 REASONING_*)──
    case "REASONING_MESSAGE_START": {
      const key = nextKey("think");
      return {
        ...state,
        ids: { ...state.ids, [ev.messageId]: key },
        rows: [...state.rows, { kind: "think", key, text: "", live: true }],
      };
    }
    case "REASONING_MESSAGE_CONTENT": {
      const key = state.ids[ev.messageId];
      if (!key) return state;
      return {
        ...state,
        rows: patchByKey(state.rows, key, (row) =>
          row.kind === "think" ? { ...row, text: row.text + ev.delta } : row,
        ),
      };
    }
    case "REASONING_MESSAGE_END": {
      const key = state.ids[ev.messageId];
      if (!key) return state;
      return {
        ...state,
        rows: patchByKey(state.rows, key, (row) =>
          row.kind === "think" ? { ...row, live: false } : row,
        ),
      };
    }
    // REASONING_START / REASONING_END 只是包住整块的括号,没有可展示内容
    case "REASONING_START":
    case "REASONING_END":
      return state;

    // ── 工具 ──
    case "TOOL_CALL_START": {
      const key = nextKey("tool");
      return {
        ...state,
        ids: { ...state.ids, [ev.toolCallId]: key },
        rows: [
          ...state.rows,
          {
            kind: "tool",
            key,
            callId: ev.toolCallId,
            tool: ev.toolCallName,
            args: {},
            status: "running",
            durationMs: null,
            exitCode: null,
            error: null,
            result: "",
          },
        ],
      };
    }
    case "TOOL_CALL_ARGS": {
      const key = state.ids[ev.toolCallId];
      if (!key) return state;
      // qi 在 TOOL_CALL_START 时已拿到完整参数,所以这里的 delta 是一整块 JSON;
      // 但仍按"增量拼接"处理,以免将来后端改成真增量就得改前端。
      return {
        ...state,
        rows: patchByKey(state.rows, key, (row) => {
          if (row.kind !== "tool") return row;
          const merged = row.args[RAW_ARGS] === undefined ? {} : row.args;
          return { ...row, args: { ...merged, ...parseArgs(ev.delta) } };
        }),
      };
    }
    case "TOOL_CALL_END":
      // 只表示"参数发完了";qi 的执行结果在 TOOL_CALL_RESULT。
      return state;

    case "TOOL_CALL_RESULT": {
      const key = state.ids[ev.toolCallId];
      if (!key) return state;
      const meta = asRecord(ev.metadata?.[QI_CUSTOM.toolMeta]);
      const status = asStr(meta.status, "ok");
      return {
        ...state,
        rows: patchByKey(state.rows, key, (row) =>
          row.kind === "tool"
            ? {
                ...row,
                status: status === "error" ? "error" : "ok",
                durationMs:
                  typeof meta.duration_ms === "number" ? meta.duration_ms : null,
                exitCode:
                  typeof meta.exit_code === "number" ? meta.exit_code : null,
                error: typeof meta.error === "string" ? meta.error : null,
                result: ev.content,
              }
            : row,
        ),
      };
    }

    // ── 快照 ──
    case "STATE_SNAPSHOT": {
      const snap = asRecord(ev.snapshot);
      return {
        ...state,
        host: {
          cwd: typeof snap.cwd === "string" ? snap.cwd : undefined,
          title: typeof snap.title === "string" ? snap.title : undefined,
        },
      };
    }
    case "MESSAGES_SNAPSHOT":
      // 刻意不用:它是 AG-UI 的通用形状,只装"会进对话的消息",
      // 装不下 dispatch / tool / 叙述。qi 用下面那个更完整的 qi.history。
      return state;

    case "CUSTOM":
      return applyCustom(state, ev.name, asRecord(ev.value));

    case "STEP_STARTED":
    case "STEP_FINISHED":
    case "STATE_DELTA":
    case "ACTIVITY_SNAPSHOT":
    case "ACTIVITY_DELTA":
    case "RAW":
      // 显式忽略:qi 目前不发这些;但留着分支,这样将来后端一开始发,
      // 编译器不会因为"没处理"而报错,我们也不会以为它没被覆盖过。
      return state;
  }
}

/**
 * AG-UI 的 `CUSTOM` 分支。
 *
 * 单独成函数而不是塞在 `reduce` 的 case 里:那一块有九个 qi 自己的名字,
 * 留在 switch 里会让 `reduce` 长到读不动,而且"case 结尾必须有 return"
 * 这类检查也没法通过(内层 switch 穷尽,但外层看不到)。
 */
function applyCustom(
  state: TurnState,
  name: string,
  value: Record<string, unknown>,
): TurnState {
  switch (name) {
    case QI_CUSTOM.history: {
      const entries = Array.isArray(value.entries)
        ? (value.entries as Entry[])
        : [];
      return hydrate(state, entries);
    }
    case QI_CUSTOM.dispatch:
      return {
        ...state,
        rows: [
          ...state.rows,
          {
            kind: "route",
            key: nextKey("route"),
            agent: typeof value.agent === "string" ? value.agent : null,
            label: asStr(value.display_name, asStr(value.agent, "?")),
            confidence: asNum(value.confidence),
            source: asStr(value.source),
            reasoning: asStr(value.reasoning),
          },
        ],
      };
    case QI_CUSTOM.opening:
      return {
        ...state,
        rows: [
          ...state.rows,
          {
            kind: "say",
            key: nextKey("say"),
            agent: typeof value.agent === "string" ? value.agent : null,
            text: asStr(value.text),
            live: false,
            tone: "opening",
          },
        ],
      };
    case QI_CUSTOM.narration: {
      // 把"刚流完的那条文本"重新定性:宣布了工具调用的是**过程**,否则是**结论**。
      // 这正是 qi 的核心区分,也是让转录可扫读的关键。
      const calls = Array.isArray(value.tool_calls) ? value.tool_calls : [];
      const tone = calls.length > 0 ? "narration" : "final";
      const last = findLast(state.rows, "say");
      if (!last) return state;
      return {
        ...state,
        rows: patchByKey(state.rows, last.key, (row) =>
          row.kind === "say" ? { ...row, tone } : row,
        ),
      };
    }
    case QI_CUSTOM.compaction:
      return {
        ...state,
        rows: [
          ...state.rows,
          {
            kind: "note",
            key: nextKey("note"),
            label:
              asStr(value.phase) === "start" ? "正在压缩上下文" : "上下文已压缩",
            detail: "",
          },
        ],
      };
    case QI_CUSTOM.branch:
      return {
        ...state,
        rows: [
          ...state.rows,
          {
            kind: "note",
            key: nextKey("note"),
            label: "会话分支",
            detail: JSON.stringify(value).slice(0, 200),
          },
        ],
      };
    default:
      // AG-UI 允许任意 CUSTOM。不认识的用 note 显示名字,而不是静默丢弃 ——
      // 丢掉会让"插件加了一个 Custom 但没人看见"变成不可诊断的问题。
      return {
        ...state,
        rows: [
          ...state.rows,
          {
            kind: "note",
            key: nextKey("note"),
            label: asStr(name, "自定义事件"),
            detail: JSON.stringify(value).slice(0, 200),
          },
        ],
      };
  }
}

/** TOOL_CALL_ARGS 的 delta 是 JSON 字符串;解析失败就当空对象。 */
const RAW_ARGS = "__raw";
function parseArgs(delta: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(delta);
    return asRecord(parsed);
  } catch {
    return { [RAW_ARGS]: delta };
  }
}
