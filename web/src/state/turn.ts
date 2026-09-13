/**
 * 把 SSE 事件归约成"一屏"的视图模型。
 *
 * 关键不变量(与后端 `runtime.stream()` 落盘顺序一致):
 *   一屏内的条目按**到达顺序**排列,因为服务端就是按真实因果顺序发的:
 *   `text_delta*` → `assistant_message`(带 tool_calls 时是"叙述") → `tool_start` → `tool_end` → …
 *   所以前端不做任何重排 —— 一旦重排(比如把所有思考提到开头),就丢掉了因果顺序,
 *   这与 docs/web.md §13 记的顺序不变量冲突。
 */
import type { AgentEvent, DispatchData, Entry } from "../api/types";

export interface ToolView {
  key: string;
  tool: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "error";
  durationMs?: number;
  exitCode?: number | null;
  error?: string | null;
  result?: string;
}

export interface MessageView {
  key: string;
  role: "user" | "assistant" | "narration" | "error" | "opening";
  agent: string | null;
  text: string;
  /** 流式进行中(UI 显示光标/呼吸态) */
  streaming: boolean;
  suggestions?: string[];
}

export interface DispatchView {
  key: string;
  data: DispatchData;
}

export type Item =
  | { kind: "dispatch"; dispatch: DispatchView }
  | { kind: "message"; message: MessageView }
  | { kind: "tool"; tool: ToolView };

export interface TurnState {
  items: Item[];
  /** agent_end 透出的 usage 原样保存(键名由 provider 决定,不在前端做白名单) */
  usage?: Record<string, unknown>;
  status: "idle" | "running" | "ok" | "error" | "cancelled";
  runId?: string;
}

export const emptyTurn: TurnState = { items: [], status: "idle" };

let counter = 0;
const nextKey = (prefix: string) => `${prefix}-${++counter}`;

/** 事件 data 是从网络来的 JSON:逐字段收窄,而不是 `as` 硬转。
 *  转错了只会在渲染时才炸,而且会把脏数据带进视图模型。 */
const asNum = (v: unknown, fallback = 0): number =>
  typeof v === "number" ? v : fallback;
const asStr = (v: unknown, fallback = ""): string =>
  typeof v === "string" ? v : fallback;
const asStrArray = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** 会话历史(entries) → 初始条目列表。用于刷新/切换会话后的回放。 */
export function fromEntries(entries: Entry[]): TurnState {
  const items: Item[] = [];
  for (const e of entries) {
    switch (e.type) {
      case "message": {
        if (e.role === "system") break;
        items.push({
          kind: "message",
          message: {
            key: nextKey("m"),
            role: e.role === "user" ? "user" : "assistant",
            agent: e.agent_id ?? null,
            text: e.content ?? "",
            streaming: false,
          },
        });
        break;
      }
      case "custom": {
        // 工具调用**之前**的叙述:直播时是 text_delta,回放时靠这条保持同样顺序
        if (e.custom_type === "assistant_narration" && e.content) {
          items.push({
            kind: "message",
            message: {
              key: nextKey("n"),
              role: "narration",
              agent: e.agent ?? null,
              text: e.content,
              streaming: false,
            },
          });
        }
        break;
      }
      case "tool": {
        items.push({
          kind: "tool",
          tool: {
            key: nextKey("t"),
            tool: e.tool ?? "?",
            args: e.args ?? {},
            status: e.status === "error" ? "error" : "ok",
            durationMs: e.duration_ms,
            exitCode: e.exit_code ?? null,
            error: e.error ?? null,
            result: e.result ?? "",
          },
        });
        break;
      }
      case "dispatch": {
        if (!e.agent) break;
        items.push({
          kind: "dispatch",
          dispatch: {
            key: nextKey("d"),
            data: {
              agent: e.agent,
              display_name: e.display_name || e.agent,
              source: e.source ?? "",
              confidence: typeof e.confidence === "number" ? e.confidence : 0,
              reasoning: e.reasoning ?? "",
            },
          },
        });
        break;
      }
      default:
        break; // state / session header 不渲染
    }
  }
  return { items, status: "idle" };
}

/** 追加用户消息(本地先行,避免等服务端回显才看到自己说了什么)。 */
export function withUserMessage(state: TurnState, text: string): TurnState {
  return {
    ...state,
    status: "running",
    items: [
      ...state.items,
      {
        kind: "message",
        message: {
          key: nextKey("u"),
          role: "user",
          agent: null,
          text,
          streaming: false,
        },
      },
    ],
  };
}

export function newTurn(runId: string): TurnState {
  return { items: [], status: "running", runId };
}

/** 事件归约。纯函数:同样的事件序列一定得到同样的视图(便于回归)。 */
export function reduce(state: TurnState, ev: AgentEvent): TurnState {
  const items = state.items;

  const lastStreaming = (): MessageView | undefined => {
    for (let i = items.length - 1; i >= 0; i -= 1) {
      const item = items[i];
      if (item && item.kind === "message" && item.message.streaming)
        return item.message;
    }
    return undefined;
  };
  const replaceLast = (patch: (m: MessageView) => MessageView): Item[] => {
    for (let i = items.length - 1; i >= 0; i -= 1) {
      const item = items[i];
      if (item && item.kind === "message" && item.message.streaming) {
        const copy = items.slice();
        copy[i] = { kind: "message", message: patch(item.message) };
        return copy;
      }
    }
    return items;
  };

  switch (ev.kind) {
    case "dispatch": {
      const data: DispatchView["data"] = {
        agent: typeof ev.data.agent === "string" ? ev.data.agent : null,
        display_name: asStr(ev.data.display_name),
        source: asStr(ev.data.source),
        confidence: asNum(ev.data.confidence),
        reasoning: asStr(ev.data.reasoning),
      };
      return {
        ...state,
        items: [
          ...items,
          { kind: "dispatch", dispatch: { key: nextKey("d"), data } },
        ],
      };
    }

    case "text_delta": {
      const current = lastStreaming();
      if (current) {
        return {
          ...state,
          items: replaceLast((m) => ({ ...m, text: m.text + ev.text })),
        };
      }
      return {
        ...state,
        items: [
          ...items,
          {
            kind: "message",
            message: {
              key: nextKey("a"),
              role: "assistant",
              agent: ev.agent,
              text: ev.text,
              streaming: true,
            },
          },
        ],
      };
    }

    case "assistant_message": {
      // 宣布了工具调用的那条是"过程"(叙述样式);否则由末尾的 text 定稿
      const calls = asStrArray(ev.data.tool_calls);
      const isNarration = calls.length > 0;
      if (lastStreaming()) {
        return {
          ...state,
          items: replaceLast((m) => ({
            ...m,
            streaming: false,
            role: isNarration ? "narration" : "assistant",
            text: ev.text || m.text,
          })),
        };
      }
      if (!ev.text) return state;
      return {
        ...state,
        items: [
          ...items,
          {
            kind: "message",
            message: {
              key: nextKey(isNarration ? "n" : "a"),
              role: isNarration ? "narration" : "assistant",
              agent: ev.agent,
              text: ev.text,
              streaming: false,
            },
          },
        ],
      };
    }

    case "text": {
      // 末尾的完整文本是**权威值**:用它覆盖流式累积,防止流式丢帧导致展示与落盘不一致
      if (lastStreaming()) {
        return {
          ...state,
          items: replaceLast((m) => ({
            ...m,
            text: ev.text,
            streaming: false,
          })),
        };
      }
      return state;
    }

    case "tool_start": {
      const args: Record<string, unknown> = {};
      if (isRecord(ev.data.args)) Object.assign(args, ev.data.args);
      return {
        ...state,
        items: [
          ...items,
          {
            kind: "tool",
            tool: {
              key: nextKey("t"),
              tool: ev.tool ?? "?",
              args,
              status: "running",
            },
          },
        ],
      };
    }

    case "tool_end": {
      const status =
        ev.data.status === "error" ? ("error" as const) : ("ok" as const);
      const durationMs = asNum(ev.data.duration_ms);
      const exitCode =
        typeof ev.data.exit_code === "number" ? ev.data.exit_code : null;
      const error = typeof ev.data.error === "string" ? ev.data.error : null;
      for (let i = items.length - 1; i >= 0; i -= 1) {
        const item = items[i];
        if (item && item.kind === "tool" && item.tool.status === "running") {
          const copy = items.slice();
          copy[i] = {
            kind: "tool",
            tool: {
              ...item.tool,
              status,
              durationMs,
              exitCode,
              error,
              result: ev.text,
            },
          };
          return { ...state, items: copy };
        }
      }
      return state;
    }

    case "opening": {
      const suggestions = asStrArray(ev.data.suggestions);
      return {
        ...state,
        items: [
          ...items,
          {
            kind: "message",
            message: {
              key: nextKey("o"),
              role: "opening",
              agent: ev.agent,
              text: ev.text,
              streaming: false,
              suggestions,
            },
          },
        ],
      };
    }

    case "error": {
      return {
        ...state,
        items: [
          ...items,
          {
            kind: "message",
            message: {
              key: nextKey("e"),
              role: "error",
              agent: ev.agent,
              text: ev.text,
              streaming: false,
            },
          },
        ],
      };
    }

    case "agent_end": {
      const usage = ev.data.usage;
      return { ...state, usage: isRecord(usage) ? usage : undefined };
    }

    default:
      return state;
  }
}
