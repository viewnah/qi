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
  formatElapsed,
  fromEntries,
  groupProcess,
  hydrate,
  lastUsage,
  latestActivityLine,
  reduce,
  withUserMessage,
} from "./turn";
import type { NoteRow, Row, SayRow, ToolRowData } from "./turn";

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
    // 分派行排在**提问之后**:落盘顺序是 dispatch → user(runtime 先落决策),
    // 但读者要的是"谁在回答这个问题" —— 直接按文件顺序画会让分派压在提问上方。
    expect(rows.map((r) => r.kind)).toEqual([
      "you",
      "route",
      "say",
      "tool",
      "say",
    ]);
    // 叙述必须是 narration,不能与结论同级 —— 否则扫读会失效
    expect(sayOf(rows)?.tone).toBe("narration");
  });

  it("分派行跟着它回答的那个提问(两个回合各归各的)", () => {
    const twoTurns: Entry[] = [
      { type: "dispatch", agent: "a", display_name: "甲", confidence: 1, source: "rule" },
      { type: "message", role: "user", content: "第一个问题" },
      { type: "message", role: "assistant", content: "第一个回答" },
      { type: "dispatch", agent: "b", display_name: "乙", confidence: 1, source: "rule" },
      { type: "message", role: "user", content: "第二个问题" },
      { type: "message", role: "assistant", content: "第二个回答" },
    ];
    const rows = fromEntries(twoTurns);
    expect(rows.map((r) => r.kind)).toEqual([
      "you", "route", "say", "you", "route", "say",
    ]);
    // 关键:第一个分派不能掉到第二个提问后面去
    expect(rows[1]).toMatchObject({ kind: "route", label: "甲" });
    expect(rows[4]).toMatchObject({ kind: "route", label: "乙" });
  });

  it("落盘的行带上 entry id(分叉按钮只给落盘过的消息)", () => {
    const rows = fromEntries([
      { type: "message", role: "user", content: "问", id: "e1" },
      { type: "message", role: "assistant", content: "答", id: "e2" },
    ]);
    expect(rows[0]).toMatchObject({ kind: "you", entryId: "e1" });
    expect(rows[1]).toMatchObject({ kind: "say", entryId: "e2" });
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

describe("lastUsage", () => {
  const entries: Entry[] = [
    { type: "message", role: "user", content: "你好" },
    {
      type: "message",
      role: "assistant",
      content: "第一轮",
      usage: { prompt_tokens: 100, context_tokens: 100 },
    },
    { type: "message", role: "user", content: "再来" },
    {
      type: "message",
      role: "assistant",
      content: "第二轮",
      usage: { prompt_tokens: 900, context_tokens: 300 },
    },
  ];

  it("取**最后一轮**,不是第一条、也不是累加", () => {
    expect(lastUsage(entries)).toEqual({ prompt_tokens: 900, context_tokens: 300 });
  });

  it("老会话(一条 usage 都没记过)→ null:不造数", () => {
    const old: Entry[] = [
      { type: "message", role: "user", content: "你好" },
      { type: "message", role: "assistant", content: "你好" },
    ];
    expect(lastUsage(old)).toBeNull();
  });

  it("后面的轮次没记 usage 时,回退到最近一条记过的", () => {
    const mixed: Entry[] = [
      ...entries,
      { type: "message", role: "assistant", content: "被中断的一轮" },
    ];
    expect(lastUsage(mixed)).toEqual({ prompt_tokens: 900, context_tokens: 300 });
  });
});

describe("groupProcess(把一轮的过程收成一个块)", () => {
  const row = (partial: Partial<Row> & { kind: Row["kind"]; key: string }): Row =>
    partial as Row;
  const think = (key: string): Row =>
    row({ kind: "think", key, text: "想", live: false });
  const tool = (key: string): Row =>
    row({
      kind: "tool",
      key,
      callId: "",
      tool: "ls",
      args: {},
      status: "ok",
      durationMs: 1,
      exitCode: 0,
      error: null,
      result: "",
      details: null,
      activity: [],
    });
  const say = (key: string, tone: "final" | "narration"): Row =>
    row({ kind: "say", key, agent: "a", text: "话", live: false, tone });

  it("思考 / 叙述 / 工具连续出现 → 合成**一个**块", () => {
    const blocks = groupProcess([
      row({ kind: "you", key: "y1", text: "问" }),
      row({ kind: "route", key: "r1", agent: "a", label: "甲", confidence: 1, source: "rule", reasoning: "" }),
      think("t1"),
      say("n1", "narration"),
      tool("x1"),
      say("f1", "final"),
    ]);
    expect(blocks.map((b) => b.kind)).toEqual(["row", "row", "process", "row"]);
    const block = blocks[2];
    if (block?.kind !== "process") throw new Error("第 3 块应当是过程块");
    // 块内保持原顺序,而且**不区分**种类(渲染层的事,这里只保证顺序)
    expect(block.rows.map((r) => r.kind)).toEqual(["think", "say", "tool"]);
    // 块的 key 取块内第一行:列表重排时 React 认的是它
    expect(block.key).toBe("t1");
  });

  it("分派 / 结论 / 标记**不进**块 —— 它们是元信息与结论", () => {
    const blocks = groupProcess([
      row({ kind: "note", key: "n1", label: "上下文已压缩", detail: "" }),
      say("f1", "final"),
      think("t1"),
      think("t2"),
    ]);
    expect(blocks.map((b) => b.kind)).toEqual(["row", "row", "process"]);
    const last = blocks[2];
    if (last?.kind !== "process") throw new Error("最后一块应当是过程块");
    expect(last.rows).toHaveLength(2);
  });

  it("没有过程行时一个块都不出", () => {
    const blocks = groupProcess([say("f1", "final")]);
    expect(blocks.map((b) => b.kind)).toEqual(["row"]);
  });

  it("两轮各自成块(中间隔着结论,不会跨轮合并)", () => {
    const blocks = groupProcess([
      think("t1"), say("f1", "final"),
      think("t2"), tool("x2"), say("f2", "final"),
    ]);
    expect(blocks.map((b) => b.kind)).toEqual([
      "process", "row", "process", "row",
    ]);
  });
});

describe("委派直播(SUBAGENT_* 生命 + ACTIVITY_SNAPSHOT 内容)", () => {
  const toolStart: AguiEvent[] = [
    { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "run_subagent" },
    { type: "TOOL_CALL_END", toolCallId: "c1" },
  ];
  /** 出生:AG-UI 原生的 SUBAGENT_STARTED(缺省 = 顶层 scout)。 */
  const born = (over: Record<string, unknown> = {}): AguiEvent =>
    ({
      type: "SUBAGENT_STARTED",
      subagentRunId: "scout#1",
      name: "scout",
      description: "读一遍",
      parentToolCallId: "c1",
      metadata: { toolCallId: "c1", depth: 1, parent: "qi" },
      ...over,
    }) as AguiEvent;
  /** 内容:ACTIVITY_SNAPSHOT(缺省 = 一条 act 行)。 */
  const snap = (over: Record<string, unknown> = {}): AguiEvent => {
    const c = {
      kind: "subagent", run: "scout#1", role: "scout", parent: "qi",
      depth: 1, task: "读一遍", phase: "act", toolCallId: "c1",
      line: '\u00b7 read {"path":"a.py"}', ...over,
    };
    return {
      type: "ACTIVITY_SNAPSHOT", messageId: `qi-act-${String(c.run)}`,
      activityType: "qi.subagent", subagentRunId: String(c.run),
      content: c, replace: true,
    };
  };
  const died = (over: Record<string, unknown> = {}): AguiEvent =>
    ({
      type: "SUBAGENT_FINISHED", subagentRunId: "scout#1",
      outcome: { type: "success" }, metadata: { toolCallId: "c1" }, ...over,
    }) as AguiEvent;
  const toolOf = (s: ReturnType<typeof reduce>): ToolRowData =>
    s.rows.find((r) => r.kind === "tool") as ToolRowData;

  it("生:SUBAGENT_STARTED 建卡(名字 / 任务 / 父 / 深度,状态 running)", () => {
    const s = run(...toolStart, born());
    expect(s.pendingActivity).toEqual({});
    expect(toolOf(s).activity).toHaveLength(1);
    expect(toolOf(s).activity[0]).toMatchObject({
      run: "scout#1", role: "scout", parent: "qi", depth: 1, task: "读一遍",
      state: "running", phase: "start",
    });
  });

  it("内容:act/think 追加一行、stream 覆盖当前轮;不改生命状态", () => {
    const s = run(...toolStart, born(),
      snap(),
      snap({ phase: "think", line: "\u270e \u7b2c1\u8f6e: \u5148\u5b9a\u8303\u56f4" }),
      snap({ phase: "stream", mode: "think", text: "\u5148\u5b9a\u8303\u56f4" }),
      snap({ phase: "stream", mode: "think", text: "\u5148\u5b9a\u8303\u56f4,\u518d\u770b\u7ec6\u8282" }));
    expect(toolOf(s).activity[0]).toMatchObject({
      state: "running",
      lines: [
        { zone: "act", text: '\u00b7 read {"path":"a.py"}' },
        { zone: "think", text: "\u270e \u7b2c1\u8f6e: \u5148\u5b9a\u8303\u56f4" },
      ],
      text: "\u5148\u5b9a\u8303\u56f4,\u518d\u770b\u7ec6\u8282",
    });
  });

  it("死:FINISHED 定格成功、ERROR 定格失败并带原因", () => {
    const ok = run(...toolStart, born(), snap(), died());
    expect(toolOf(ok).activity[0]).toMatchObject({
      state: "done", ok: true, phase: "done",
    });
    const bad = run(...toolStart, born(), died({
      type: "SUBAGENT_ERROR", message: "\u5b50\u8fd0\u884c\u5931\u8d25",
      code: "subagent_failed",
    }));
    expect(toolOf(bad).activity[0]).toMatchObject({
      state: "error", ok: false, error: "\u5b50\u8fd0\u884c\u5931\u8d25",
    });
  });

  it("嵌套:不同 run 在同一张卡里并列(depth 来自 metadata)", () => {
    const s = run(...toolStart,
      born(),
      born({ subagentRunId: "child#2", name: "librarian",
             parentSubagentRunId: "scout",
             metadata: { toolCallId: "c1", depth: 2, parent: "scout" } }));
    expect(toolOf(s).activity.map((e) => e.run)).toEqual(["scout#1", "child#2"]);
    expect(toolOf(s).activity[1]).toMatchObject({ depth: 2, parent: "scout" });
  });

  it("早到:出生事件早于工具卡 → 暂存,建卡时注入(不丢人)", () => {
    const early = run(born());
    expect(early.pendingActivity["c1"]).toHaveLength(1);
    expect(early.rows).toHaveLength(0);
    let s = early;
    for (const ev of toolStart) s = reduce(s, ev);
    expect(s.pendingActivity).toEqual({});
    expect(toolOf(s).activity).toHaveLength(1);
  });

  it("TOOL_CALL_RESULT 清空直播(结果由横幅卡接管,不显两遍)", () => {
    const s = run(...toolStart, born(), snap(), {
      type: "TOOL_CALL_RESULT", messageId: "tm1", toolCallId: "c1",
      content: "\u2500\u2500 scout (\u6df1\u5ea61, \u6210\u529f) \u2500\u2500",
      metadata: { "qi.tool": { status: "ok", duration_ms: 3, error: null } },
    });
    expect(toolOf(s).activity).toEqual([]);
    expect(toolOf(s).result).toContain("scout");
  });

  it("不是我们的活动 / 缺归卡字段 → 原样不动", () => {
    const base = run(...toolStart);
    const alien = reduce(base, {
      type: "ACTIVITY_SNAPSHOT", messageId: "x", activityType: "someone.else",
      content: { kind: "subagent", run: "r", toolCallId: "c1" },
    });
    expect(alien).toBe(base);
    const noRun = reduce(base, {
      type: "ACTIVITY_SNAPSHOT", messageId: "x", activityType: "qi.subagent",
      content: { kind: "subagent", phase: "act", toolCallId: "c1" },
    });
    expect(noRun).toBe(base);
    const noCard = reduce(base, {
      type: "ACTIVITY_SNAPSHOT", messageId: "x", activityType: "qi.subagent",
      content: { kind: "subagent", run: "r", phase: "act" },
    });
    expect(noCard).toBe(base);
  });

  it("latestActivityLine:当前流优先,其次最后一行,最后退到任务", () => {
    const s = run(...toolStart, born(), snap(),
      snap({ phase: "stream", text: "\u6b63\u5728\u751f\u6210" }));
    expect(latestActivityLine(toolOf(s).activity)).toBe("\u6b63\u5728\u751f\u6210");
    const s2 = run(...toolStart, born(), snap());
    expect(latestActivityLine(toolOf(s2).activity)).toBe('\u00b7 read {"path":"a.py"}');
    const s3 = run(...toolStart, born());
    expect(latestActivityLine(toolOf(s3).activity)).toBe("scout \u8bfb\u4e00\u904d");
    expect(latestActivityLine([])).toBeNull();
  });
});

describe("agent 级重试(qi.retry):撤回半截输出", () => {
  const retry = (over: Record<string, unknown> = {}): AguiEvent =>
    ({
      type: "CUSTOM", name: "qi.retry",
      value: { attempt: 1, max: 3, delay_ms: 2000,
               error: "RuntimeError: connection lost", ...over },
    }) as AguiEvent;
  const sayRows = (s: ReturnType<typeof reduce>) =>
    s.rows.filter((r) => r.kind === "say" || r.kind === "think");

  it("丢掉还在流的 say/think 行,留一行重试提示", () => {
    // 失败那次已经流出的半截:它不会进历史,屏幕上也不能留着 ——
    // 否则重试成功后同一段回答会出现两遍。
    const s = run(
      { type: "TEXT_MESSAGE_START", messageId: "m1", role: "assistant" },
      { type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: "半截回答" },
      retry(),
    );
    expect(sayRows(s)).toEqual([]);
    const note = s.rows.at(-1) as NoteRow;
    expect(note).toMatchObject({ kind: "note", label: "重试" });
    expect(note.detail).toContain("1/3");
    expect(note.detail).toContain("2s");   // 退避要显示:用户要知道等多久
    expect(note.detail).toContain("connection lost");
  });

  it("思考行同样撤回(它属于同一次失败的调用)", () => {
    const s = run(
      { type: "REASONING_MESSAGE_START", messageId: "r1", role: "reasoning" },
      { type: "REASONING_MESSAGE_CONTENT", messageId: "r1", delta: "想了一半" },
      retry(),
    );
    expect(sayRows(s)).toEqual([]);
  });

  it("已收尾的 say 行、工具行与工具结果都不动(保留已经发生的事)", () => {
    const s = run(
      { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "ls" },
      { type: "TOOL_CALL_END", toolCallId: "c1" },
      { type: "TOOL_CALL_RESULT", messageId: "tm1", toolCallId: "c1",
        content: "d home", metadata: { "qi.tool": { status: "ok" } } },
      // 已收尾的回答(END 之后不在"流中")
      { type: "TEXT_MESSAGE_START", messageId: "m2", role: "assistant" },
      { type: "TEXT_MESSAGE_CONTENT", messageId: "m2", delta: "上一轮的正文" },
      { type: "TEXT_MESSAGE_END", messageId: "m2" },
      retry(),
    );
    const tool = s.rows.find((r) => r.kind === "tool") as ToolRowData;
    expect(tool.result).toBe("d home");
    const says = sayRows(s).filter((r) => r.kind === "say") as SayRow[];
    expect(says.map((r) => r.text)).toEqual(["上一轮的正文"]);
  });
});

describe("子运行的状态与耗时", () => {
  it("定格时记下起止时间(完成的卡才显示得出“跑了多久”)", () => {
    const s = run(
      { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "subagent" },
      { type: "TOOL_CALL_END", toolCallId: "c1" },
      { type: "SUBAGENT_STARTED", subagentRunId: "scout#1", name: "scout",
        parentToolCallId: "c1", timestamp: 1000,
        metadata: { toolCallId: "c1", depth: 1 } } as AguiEvent,
      { type: "SUBAGENT_FINISHED", subagentRunId: "scout#1", timestamp: 3400,
        metadata: { toolCallId: "c1" } } as AguiEvent,
    );
    const tool = s.rows.find((r) => r.kind === "tool") as ToolRowData;
    expect(tool.activity[0]).toMatchObject({
      state: "done", startedAt: 1000, finishedAt: 3400,
    });
  });

  it("formatElapsed:毫秒 / 秒 / 分秒三档,坏值给空串", () => {
    expect(formatElapsed(120)).toBe("120ms");
    expect(formatElapsed(3400)).toBe("3.4s");
    expect(formatElapsed(83_000)).toBe("1m 23s");
    expect(formatElapsed(-1)).toBe("");
    expect(formatElapsed(Number.NaN)).toBe("");
  });
});
