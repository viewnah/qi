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
  /** 落盘 entry 的 id。**直播中刚发出去的那句还没有**(乐观显示时还没落盘),
   *  所以它同时是"这条消息能不能被分叉"的开关(与 dsh 只给有 messageId 的消息
   *  挂动作同一条规矩)。 */
  entryId?: string;
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
  /** 落盘 entry 的 id(直播中那一条还没有 —— 见 `YouRow.entryId`)。 */
  entryId?: string;
}

/**
 * 一次子运行的直播状态(`ACTIVITY_SNAPSHOT.content`,design/web-lab.md 二期)。
 *
 * 一个 `run` 一条;嵌套委派**收敛到发起方那张工具卡**(后端用 `toolCallId` 归卡,
 * 嵌套子运行共用同一个),所以一棵委派树在界面上是一张卡里并列的几条。
 * `phase` 可能值见后端 `qi_lab/activity.PHASES`。
 */
export interface SubActivity {
  run: string;
  role: string;
  parent: string;
  depth: number;
  task: string;
  /**
   * **生命状态**(AG-UI 原生 `SUBAGENT_STARTED` / `SUBAGENT_FINISHED` /
   * `SUBAGENT_ERROR`)。界面“谁在跑”看它,不看 `phase`。
   */
  state: "running" | "done" | "error";
  /** 内容阶段(`ACTIVITY_SNAPSHOT`):`start` / `stream` / `act` / `think`;定格后 `done`。 */
  phase: string;
  /** 已发生的活动与思考行 —— 与结束后 result 段的同名行逐字同源(后端保证)。 */
  lines: { zone: "act" | "think"; text: string }[];
  /** `phase === "stream"` 时的当前轮缓冲(下一个 act/think 帧自然把它隐去)。 */
  text?: string;
  mode?: string;
  ok?: boolean;
  error?: string;
}

/** 后端 `qi_lab/activity.py` 的活动类型 —— 两处必须同值(有测试盯)。 */
export const ACTIVITY_TYPE = "qi.subagent";

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
  /** 插件给客户端看的自由结构;渲染见 Rows.tsx 的 `Details`。 */
  details: Record<string, unknown> | null;
  /**
   * 委派直播的活动(一个子 run 一条)。**结束即清**(`TOOL_CALL_RESULT`):
   * 结果回到 `result` 的横幅卡 —— 直播只是把同一批文字提前显示,留着就是
   * 同一段话在一张卡里出两遍。
   */
  activity: SubActivity[];
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

export type Row =
  | YouRow
  | RouteRow
  | ThinkRow
  | SayRow
  | ToolRowData
  | NoteRow
  | ErrorRow;

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
  /**
   * 直播帧早于工具卡到达时的暂存(后端同队列 FIFO保证正常不发生)。
   * 兕底而不是丢弃:归错卡或丢帧都是直播最难查的那类 bug。
   */
  pendingActivity: Record<string, AguiEvent[]>;
}

export const emptyTurn: TurnState = {
  rows: [],
  usage: {},
  host: {},
  phase: "idle",
  ids: {},
  pendingUser: null,
  pendingActivity: {},
};

/**
 * 一条直播事件 → 该子 run 的卡(**纯函数**)。
 *
 * 四类事件分两层,与协议同构:
 * · **生 / 死**:`SUBAGENT_STARTED` 建卡,`SUBAGENT_FINISHED` / `SUBAGENT_ERROR` 定格;
 * · **内容**:`ACTIVITY_SNAPSHOT` 的 act/think 追加一行、stream 覆盖当前轮缓冲。
 * 内容帧对没见过的 run 也建卡(兜底):只发内容不发生命的宿主不会丢人。
 *
 * 帧是**覆盖式全量**:没带的字段从前值继承;行不去重 —— 后端只发新发生的行,
 * 去重要靠序号,而帧里刻意没有序号。
 */
function mergeActivity(list: SubActivity[], ev: AguiEvent): SubActivity[] {
  if (ev.type === "SUBAGENT_FINISHED" || ev.type === "SUBAGENT_ERROR") {
    const failed = ev.type === "SUBAGENT_ERROR";
    return list.map((e) =>
      e.run === ev.subagentRunId
        ? {
            ...e,
            state: failed ? "error" : "done",
            phase: "done",
            ok: !failed,
            error: failed ? ev.message : e.error,
          }
        : e,
    );
  }
  if (ev.type === "SUBAGENT_STARTED") {
    const meta = asRecord(ev.metadata);
    return upsertActivity(list, {
      run: ev.subagentRunId,
      role: ev.name || ev.subagentRunId,
      parent: asStr(meta.parent),
      depth: asNum(meta.depth, 1),
      task: ev.description ?? "",
      phase: "start",
      state: "running",
      lines: [],
    });
  }
  if (ev.type !== "ACTIVITY_SNAPSHOT") return list;
  const raw = asRecord(ev.content);
  const run = asStr(raw.run);
  const phase = asStr(raw.phase, "act");
  const prev = list.find((e) => e.run === run);
  const next: SubActivity = prev
    ? { ...prev, phase }
    : {
        run,
        role: asStr(raw.role),
        parent: asStr(raw.parent),
        depth: asNum(raw.depth, 1),
        task: asStr(raw.task),
        phase,
        state: "running",
        lines: [],
      };
  if (phase === "act" || phase === "think") {
    const line = asStr(raw.line);
    if (line) next.lines = [...next.lines, { zone: phase, text: line }];
  } else if (phase === "stream") {
    next.text = asStr(raw.text);
    next.mode = asStr(raw.mode, "text");
  }
  return upsertActivity(list, next);
}

/** 同 run 替换、异 run 追加 —— 一张工具卡下多棵子树并存(`run` 是身份)。 */
function upsertActivity(list: SubActivity[], entry: SubActivity): SubActivity[] {
  return list.some((e) => e.run === entry.run)
    ? list.map((e) => (e.run === entry.run ? entry : e))
    : [...list, entry];
}

/**
 * 这条事件该落到哪张工具卡(null = 不是我们的 / 归不了卡)。
 *
 * 三把钥匙同一把锁 —— 都指向“哪次工具调用”:
 * · 出生:协议的 `parentToolCallId`(0.0.59 就有,正好指回发起它的工具卡);
 * · 内容:`content.toolCallId`(qi 抬上去的主键);
 * · 死亡:协议没给归属字段,qi 用 `metadata.toolCallId` 补上。
 * `activityType` / `kind` / `run` 的校验都在这里 —— 别的扩展的活动帧从这挡掉。
 */
function activityCallId(ev: AguiEvent): string | null {
  if (ev.type === "SUBAGENT_STARTED") {
    if (!ev.subagentRunId) return null;
    return ev.parentToolCallId ?? (asStr(asRecord(ev.metadata).toolCallId) || null);
  }
  if (ev.type === "SUBAGENT_FINISHED" || ev.type === "SUBAGENT_ERROR") {
    if (!ev.subagentRunId) return null;
    return asStr(asRecord(ev.metadata).toolCallId) || null;
  }
  if (ev.type === "ACTIVITY_SNAPSHOT") {
    if (ev.activityType !== ACTIVITY_TYPE) return null;
    const raw = asRecord(ev.content);
    if (asStr(raw.kind) !== "subagent" || !asStr(raw.run)) return null;
    return asStr(raw.toolCallId) || null;
  }
  return null;
}

/** 把一条直播事件落到正确的工具卡上;卡还没建就暂存(见 `pendingActivity`)。 */
function applyActivity(state: TurnState, ev: AguiEvent): TurnState {
  const callId = activityCallId(ev);
  if (!callId) return state;
  const key = state.ids[callId];
  if (!key) {
    return {
      ...state,
      pendingActivity: {
        ...state.pendingActivity,
        [callId]: [...(state.pendingActivity[callId] ?? []), ev],
      },
    };
  }
  return {
    ...state,
    rows: patchByKey(state.rows, key, (row) =>
      row.kind === "tool"
        ? { ...row, activity: mergeActivity(row.activity, ev) }
        : row,
    ),
  };
}

/** 折叠行上的“最新一行直播”(运行中代替 result 的位置 —— 那时还没有 result)。 */
export function latestActivityLine(entries: SubActivity[]): string | null {
  const last = entries[entries.length - 1];
  if (!last) return null;
  if (last.phase === "stream" && last.text) return last.text;
  const line = last.lines[last.lines.length - 1]?.text;
  if (line) return line;
  return last.task ? `${last.role || last.run} ${last.task}` : null;
}

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
/**
 * 本地插一行 `note`(命令的反馈)。
 *
 * 为什么要有这个:`/help`、`/session` 这类指令的结果**不来自模型**,也不该进 LLM
 * 上下文(那是会话文件的事)。它们只是界面上的一句回答 —— 复用既有的 `note` 行
 * (转录里本来就有这种"标记行"),不新增行类型、不碰后端。
 */
export function withNote(state: TurnState, label: string, detail: string): TurnState {
  return {
    ...state,
    rows: [
      ...state.rows,
      { kind: "note", key: nextKey("note"), label, detail },
    ],
  };
}

/**
 * 回放历史里**最后一轮的用量**(落盘在助手消息 entry 上)。
 *
 * 为什么需要它:打开一个已存在的会话时,`qi.usage` 那个流事件**不会重放** ——
 * 于是遥测抽屉会写着"本轮还没有用量",而数据其实就在 entry 里。
 *
 * 取最后一轮而不是累加:抽屉那条进度问的是"现在上下文里装着多少",
 * 那是最后一步的 prompt 大小(与后端的 `context_tokens` 同一个意思)。
 *
 * @param entries 会话的可见 entries(时间升序)。
 * @returns 最后一次记下的 usage;一条都没有就 `null`(老会话 —— 不造数)。
 */
export function lastUsage(entries: Entry[]): Record<string, unknown> | null {
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const usage = entries[i]?.usage;
    if (usage !== undefined && usage !== null && typeof usage === "object") {
      return usage;
    }
  }
  return null;
}

/**
 * 过程行:思考 / 工具 / 工具之间的叙述。
 *
 * 它们**折叠进同一个块**,不再各自成行。
 */
export type ProcessRow = ThinkRow | ToolRowData | SayRow;

/** 转录的渲染单元:单行,或一个可折叠的过程块。 */
export type Block =
  | { kind: "row"; key: string; row: Row }
  | { kind: "process"; key: string; rows: ProcessRow[] };

function isProcessRow(row: Row): row is ProcessRow {
  return (
    row.kind === "think" ||
    row.kind === "tool" ||
    (row.kind === "say" && row.tone === "narration")
  );
}

/**
 * 把行折成块:**一个回合的过程合成一个可折叠的块**。
 *
 * 为什么合并(使用反馈:「一次会话只有一个总的思考过程,可以折叠起来到一起,
 * 不用区分思考和过程」):思考、工具、工具之间的叙述原来是各自一行,一轮里出现
 * 七八行"过程"就把结论埋了;而"思考"与"过程"这两个标签本身也没有区分价值 ——
 * 读者对它们的关心程度是同一个:平时不关心,需要时才展开。
 *
 * 规则只有一条:**连续的过程行合成一个块**。于是每个回合的形态稳定为
 * `提问 → 分派 → [过程] → 回答`,中间夹着的工具往返全在块里。
 * `final` 回答 / `opening` 开场白 / 分派 / 标记 / 错误都**不进块** ——
 * 它们是结论或元信息,本来就该单独可见。
 */
export function groupProcess(rows: Row[]): Block[] {
  const blocks: Block[] = [];
  let pending: ProcessRow[] = [];
  let pendingKey = "";
  const flush = () => {
    if (pending.length === 0) return;
    blocks.push({ kind: "process", key: pendingKey, rows: pending });
    pending = [];
  };
  for (const row of rows) {
    if (isProcessRow(row)) {
      if (pending.length === 0) pendingKey = row.key;
      pending.push(row);
      continue;
    }
    flush();
    blocks.push({ kind: "row", key: row.key, row });
  }
  flush();
  return blocks;
}

export function fromEntries(entries: Entry[]): Row[] {
  const rows: Row[] = [];
  /**
   * 已读到的分派行,等着排到**它回答的那个提问**后面。
   *
   * 落盘顺序是 `dispatch → message(user) → …`(`runtime.stream` 先落决策再落提问),
   * 直接按文件顺序画就会出现"分派在提问上方"—— 读起来像"先选人、再说话"。直播路径
   * 恰好相反(提问是乐观插入的,事件在它后面到),所以两边顺序不一致。这里把回放
   * 对齐到直播:**提问在前,分派紧跟其后**(它回答的是"谁在回答这个问题")。
   */
  const pendingRoutes: Row[] = [];
  for (const e of entries) {
    switch (e.type) {
      case "message": {
        const text = e.content ?? "";
        if (!text) break;
        if (e.role === "user") {
          rows.push({
            kind: "you",
            key: nextKey("you"),
            text,
            entryId: e.id,
          });
          // 这个提问的分派决策紧跟在它后面(不是前面)—— 见 pendingRoutes 的注释。
          rows.push(...pendingRoutes.splice(0));
        } else if (e.role === "assistant") {
          rows.push({
            kind: "say",
            key: nextKey("say"),
            agent: e.agent_id ?? null,
            text,
            live: false,
            tone: "final",
            entryId: e.id,
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
        // 思考同理(后端 `_persist_thinking`),它是"思考 → 回答"那条分隔线的上半截。
        if (e.custom_type === "assistant_thinking" && e.content) {
          rows.push({
            kind: "think",
            key: nextKey("think"),
            text: e.content,
            live: false,
          });
        }
        break;
      }
      case "dispatch":
        pendingRoutes.push({
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
          details: e.details ?? null,
          activity: [],        // 回放没有直播:活动只活在运行中的那一轮
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
          {
            kind: "say",
            key,
            agent: null,
            text: "",
            live: true,
            tone: "final",
          },
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
      const pending = state.pendingActivity[ev.toolCallId];
      const pendingActivity = { ...state.pendingActivity };
      delete pendingActivity[ev.toolCallId];
      // 早到的事件先在暂存里攒着,卡一建好就注入(不丢帧)
      const activity = (pending ?? []).reduce(
        (acc, ev) => mergeActivity(acc, ev),
        [] as SubActivity[],
      );
      return {
        ...state,
        ids: { ...state.ids, [ev.toolCallId]: key },
        pendingActivity,
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
            details: null,
            activity,
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
      // 插件唯一的 UI 下行通道。这一条不加,details 就只在后端活着、到前端被丢。
      const details =
        Object.keys(asRecord(meta.details)).length > 0
          ? asRecord(meta.details)
          : null;
      return {
        ...state,
        rows: patchByKey(state.rows, key, (row) =>
          row.kind === "tool"
            ? {
                ...row,
                status: status === "error" ? "error" : "ok",
                durationMs:
                  typeof meta.duration_ms === "number"
                    ? meta.duration_ms
                    : null,
                exitCode:
                  typeof meta.exit_code === "number" ? meta.exit_code : null,
                error: typeof meta.error === "string" ? meta.error : null,
                result: ev.content,
                details,
                // 直播退场:结果由横幅卡接管(同一批文字,不显两遍)
                activity: [],
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
    case "ACTIVITY_DELTA":
    case "RAW":
      // 显式忽略:ACTIVITY_DELTA 不发(qi 的直播每帧都是**覆盖式全量**,增量合并
      // 是另一套语义,没需要就不做);其余几个 qi 目前不发。留着分支,这样将来
      // 后端一开始发,编译器不会因为"没处理"而报错,我们也不会以为它没被覆盖过。
      return state;

    case "SUBAGENT_STARTED":
    case "SUBAGENT_FINISHED":
    case "SUBAGENT_ERROR":
      // 子智能体的生 / 死(AG-UI 多智能体事件族,0.0.59 就有)。生按协议的
      // `parentToolCallId` 归卡、死按 qi 补的 `metadata.toolCallId` 归卡 ——
      // 先有一张“谁被派了单”的卡,内容再由 ACTIVITY_SNAPSHOT 往里面长。
      return applyActivity(state, ev);

    case "ACTIVITY_SNAPSHOT":
      // 直播的内容通道:认领 `activityType` 是 qi.subagent 的那些(别的类型在
      // `activityCallId` 里被挡掉 —— ACTIVITY_* 是通用通道,别人也在用)。
      return applyActivity(state, ev);
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
              asStr(value.phase) === "start"
                ? "正在压缩上下文"
                : "上下文已压缩",
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
