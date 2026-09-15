/**
 * 归约测试:AG-UI 事件 → 行。
 *
 * 只测**纯函数**(不引 jsdom、不做组件快照)——真正的风险集中在"事件怎么变成行"
 * 这段逻辑上,渲染问题靠肉眼看更快。
 *
 * 覆盖的是会**静默出错**的那些地方:三段式配对、流式累积、tone 重新定性、
 * 水合后把本轮输入补回去。
 */
import { describe, expect, it } from "vitest";
import type { AguiEvent, Entry } from "../api/types";
import {
  emptyTurn,
  fromEntries,
  hydrate,
  reduce,
  withUserMessage,
} from "./turn";
import type { Row, SayRow, ToolRowData } from "./turn";

/** 按序归约,返回终态。 */
const run = (...events: AguiEvent[]) => events.reduce(reduce, emptyTurn);

const write = (text: string): AguiEvent[] => [
  { type: "TEXT_MESSAGE_START", messageId: "m1", role: "assistant" },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: text.slice(0, 2) },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: text.slice(2) },
  { type: "TEXT_MESSAGE_END", messageId: "m1" },
];

const sayOf = (rows: Row[]): SayRow | undefined =>
  rows.find((r) => r.kind === "say") as SayRow | undefined;

describe("用户输入", () => {
  it("乐观上屏,并进入 running", () => {
    const s = withUserMessage(emptyTurn, "看下目录");
    expect(s.phase).toBe("running");
    expect(s.rows).toHaveLength(1);
    expect(s.rows[0]).toMatchObject({ kind: "you", text: "看下目录" });
    expect(s.pendingUser).toBe("看下目录");
  });
});

describe("文本流", () => {
  it("三段式合并成**一行**,而不是三行", () => {
    const s = run(...write("先看一下"));
    expect(s.rows.filter((r) => r.kind === "say")).toHaveLength(1);
    expect(sayOf(s.rows)?.text).toBe("先看一下");
  });

  it("END 之后不再是 live(光标要收掉)", () => {
    const s = run(...write("好了"));
    expect(sayOf(s.rows)?.live).toBe(false);
  });

  it("跨轮不串行:第二轮是新的一行(行 key 由前端生成,与 messageId 无关)", () => {
    // 关键回归点:后端每条流的 messageId 都从 msg_1 重来,
    // 若直接用 messageId 当行 key,第二轮会覆盖第一轮的行。
    const s = run(
      ...write("第一轮"),
      { type: "RUN_FINISHED", outcome: { type: "success" } },
      ...write("第二轮"),
    );
    const says = s.rows.filter((r) => r.kind === "say") as SayRow[];
    expect(says).toHaveLength(2);
    expect(says.map((r) => r.text)).toEqual(["第一轮", "第二轮"]);
  });
});

describe("tone 重新定性(qi 的核心区分)", () => {
  it("宣布了工具调用的那条是**过程**", () => {
    const s = run(...write("我先看一下"), {
      type: "CUSTOM",
      name: "qi.narration",
      value: { step: 1, tool_calls: ["ls"], is_final: false },
    });
    expect(sayOf(s.rows)?.tone).toBe("narration");
  });

  it("没有工具调用的那条是**结论**", () => {
    const s = run(...write("这是结论"), {
      type: "CUSTOM",
      name: "qi.narration",
      value: { step: 2, tool_calls: [] },
    });
    expect(sayOf(s.rows)?.tone).toBe("final");
  });
});

describe("思考流", () => {
  it("默认就是一行,且不 live 之后光标收掉", () => {
    const s = run(
      { type: "REASONING_START", messageId: "r1" },
      { type: "REASONING_MESSAGE_START", messageId: "r1", role: "reasoning" },
      { type: "REASONING_MESSAGE_CONTENT", messageId: "r1", delta: "用户" },
      { type: "REASONING_MESSAGE_CONTENT", messageId: "r1", delta: "想看目录" },
      { type: "REASONING_MESSAGE_END", messageId: "r1" },
      { type: "REASONING_END", messageId: "r1" },
    );
    const thinks = s.rows.filter((r) => r.kind === "think");
    expect(thinks).toHaveLength(1);
    expect(thinks[0]).toMatchObject({ text: "用户想看目录", live: false });
  });
});

describe("工具流", () => {
  const toolEvents: AguiEvent[] = [
    { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "ls" },
    { type: "TOOL_CALL_ARGS", toolCallId: "c1", delta: '{"path":"."}' },
    { type: "TOOL_CALL_END", toolCallId: "c1" },
  ];

  it("Start+Args+End 之后仍是 running(执行结果在 RESULT)", () => {
    const s = run(...toolEvents);
    const tool = s.rows.find((r) => r.kind === "tool") as ToolRowData;
    expect(tool).toMatchObject({
      tool: "ls",
      status: "running",
      args: { path: "." },
    });
  });

  it("RESULT 把状态与结构化结果填回去(metadata 里的 qi.tool)", () => {
    const s = run(...toolEvents, {
      type: "TOOL_CALL_RESULT",
      messageId: "tm1",
      toolCallId: "c1",
      content: "d home",
      metadata: {
        "qi.tool": {
          status: "ok",
          duration_ms: 12,
          exit_code: null,
          error: null,
        },
      },
    });
    const tool = s.rows.find((r) => r.kind === "tool") as ToolRowData;
    expect(tool).toMatchObject({
      status: "ok",
      durationMs: 12,
      result: "d home",
    });
  });

  it("错误工具落成 error 状态(界面据此画 ✗,不只靠颜色)", () => {
    const s = run(...toolEvents, {
      type: "TOOL_CALL_RESULT",
      messageId: "tm1",
      toolCallId: "c1",
      content: "boom",
      metadata: { "qi.tool": { status: "error", error: "tool_error" } },
    });
    expect((s.rows.find((r) => r.kind === "tool") as ToolRowData).status).toBe(
      "error",
    );
  });
});

describe("插件的 details(唯一 UI 下行通道)", () => {
  const base: AguiEvent[] = [
    { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "todo" },
    { type: "TOOL_CALL_ARGS", toolCallId: "c1", delta: "{}" },
    { type: "TOOL_CALL_END", toolCallId: "c1" },
  ];

  it("metadata[qi.tool].details 落到工具行上", () => {
    const details = {
      ui: [{ type: "list", items: [{ label: "写实现", state: "active" }] }],
    };
    const s = run(...base, {
      type: "TOOL_CALL_RESULT",
      messageId: "tm1",
      toolCallId: "c1",
      content: "ok",
      metadata: { "qi.tool": { status: "ok", details } },
    });
    const tool = s.rows.find((r) => r.kind === "tool") as ToolRowData;
    expect(tool.details).toEqual(details);
  });

  it("没有 details 时是 null(不造空对象)", () => {
    const s = run(...base, {
      type: "TOOL_CALL_RESULT",
      messageId: "tm1",
      toolCallId: "c1",
      content: "ok",
      metadata: { "qi.tool": { status: "ok" } },
    });
    expect(
      (s.rows.find((r) => r.kind === "tool") as ToolRowData).details,
    ).toBeNull();
  });

  it("历史回放也带上 details(否则刷新后插件 UI 消失)", () => {
    const rows = fromEntries([
      { type: "tool", tool: "todo", status: "ok", details: { ui: [] } },
    ]);
    expect((rows[0] as ToolRowData).details).toEqual({ ui: [] });
  });
});

describe("结束与错误", () => {
  it("RUN_FINISHED 收掉所有 live 并带出 usage", () => {
    const s = run(...write("好了"), {
      type: "RUN_FINISHED",
      outcome: { type: "success" },
      metadata: { "qi.usage": { turns: 2, total_tokens: 362 } },
    });
    expect(s.phase).toBe("ok");
    expect(sayOf(s.rows)?.live).toBe(false);
    expect(s.usage.total_tokens).toBe(362);
  });

  it("RUN_ERROR 落成一行 error,且不影响已有行", () => {
    const s = run(...write("半句"), { type: "RUN_ERROR", message: "boom" });
    expect(s.phase).toBe("error");
    expect(s.rows.filter((r) => r.kind === "error")).toHaveLength(1);
    expect(s.rows.filter((r) => r.kind === "say")).toHaveLength(1);
  });
});

describe("历史回放", () => {
  const entries: Entry[] = [
    { type: "session", id: "s1", title: "t" },
    {
      type: "dispatch",
      agent: "general",
      display_name: "qi",
      confidence: 0.9,
      source: "rule",
    },
    { type: "message", role: "user", content: "看下目录" },
    {
      type: "custom",
      custom_type: "assistant_narration",
      content: "我先看一下",
    },
    {
      type: "tool",
      tool: "ls",
      args: { path: "." },
      status: "ok",
      duration_ms: 3,
      result: "d home",
    },
    { type: "message", role: "assistant", content: "这是一个项目" },
  ];

  it("把五类 entry 映射成行(叙述与工具卡不能丢)", () => {
    const rows = fromEntries(entries);
    expect(rows.map((r) => r.kind)).toEqual([
      "route",
      "you",
      "say",
      "tool",
      "say",
    ]);
    // 叙述必须是 narration,不能与结论同级 —— 否则扫读会失效
    expect(sayOf(rows)?.tone).toBe("narration");
  });

  it("hydrate 会把本轮输入补回去(qi.history 抓的是 run 之前的 entries)", () => {
    const withPending = withUserMessage(emptyTurn, "新的一轮");
    const s = hydrate(withPending, entries);
    const you = s.rows.filter((r) => r.kind === "you");
    expect(you).toHaveLength(2);
    expect(you[you.length - 1]).toMatchObject({ text: "新的一轮" });
  });

  it("hydrate 不重复补(快照里已经有同一条时)", () => {
    const withPending = withUserMessage(emptyTurn, "看下目录");
    const s = hydrate(withPending, entries);
    expect(s.rows.filter((r) => r.kind === "you")).toHaveLength(1);
  });
});

describe("未知 CUSTOM 不被静默丢弃", () => {
  it("落到一行 note,带上名字", () => {
    const s = run({ type: "CUSTOM", name: "acme.widget", value: { x: 1 } });
    expect(s.rows[0]).toMatchObject({ kind: "note", label: "acme.widget" });
  });
});
