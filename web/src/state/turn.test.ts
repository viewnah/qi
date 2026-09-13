/**
 * 事件归约测试。
 *
 * 重点锁三件事(都是我在实现时真正纠结过、也最容易改坏的地方):
 *   1. **因果顺序**:条目严格按到达顺序排列,叙述必须在它触发的工具卡**之前**;
 *   2. **权威文本**:回合末尾的 `text` 覆盖流式累积(防止丢帧导致展示与落盘不一致);
 *   3. **脏数据容忍**:data 来自网络,字段缺失/类型不对都不能把视图模型带脏。
 *
 * 其中 `REAL_RUN` 是**真实一轮**的原始事件(用本机真实模型跑出来的,逐字未改),
 * 所以这组断言同时是"前后端契约有没有漂移"的回归网。
 */
import { describe, expect, it } from "vitest";
import type { AgentEvent, Entry } from "../api/types";
import {
  emptyTurn,
  fromEntries,
  reduce,
  withUserMessage,
  type Item,
  type TurnState,
} from "./turn";

/** 真实一轮:node 侧 `qi web` + 真实模型(router 决策 → ls → 总结)。 */
const REAL_RUN: AgentEvent[] = [
  {
    seq: 0,
    kind: "dispatch",
    agent: "general",
    tool: null,
    text: "qi (router, 0.90)",
    data: {
      confidence: 0.9,
      source: "router",
      agent: "general",
      display_name: "qi",
      reasoning: "任务为查看仓库顶层目录并用一句话总结,选择 general。",
    },
  },
  {
    seq: 1,
    kind: "agent_start",
    agent: "general",
    tool: null,
    text: "",
    data: {},
  },
  {
    seq: 2,
    kind: "text_delta",
    agent: "general",
    tool: null,
    text: "I",
    data: {},
  },
  {
    seq: 3,
    kind: "text_delta",
    agent: "general",
    tool: null,
    text: "'ll",
    data: {},
  },
  {
    seq: 4,
    kind: "text_delta",
    agent: "general",
    tool: null,
    text: " look",
    data: {},
  },
  {
    seq: 5,
    kind: "assistant_message",
    agent: "general",
    tool: null,
    text: "I'll look",
    data: { step: 1, tool_calls: ["ls"] },
  },
  {
    seq: 6,
    kind: "tool_start",
    agent: "general",
    tool: "ls",
    text: "",
    data: { args: { path: "." } },
  },
  {
    seq: 7,
    kind: "tool_end",
    agent: "general",
    tool: "ls",
    text: "# /Users/hanwei/Desktop/qi (15 项)\nd .git",
    data: { status: "ok", duration_ms: 0, exit_code: null, error: null },
  },
  {
    seq: 8,
    kind: "text_delta",
    agent: "general",
    tool: null,
    text: "仓库顶层",
    data: {},
  },
  {
    seq: 9,
    kind: "text_delta",
    agent: "general",
    tool: null,
    text: "是个项目",
    data: {},
  },
  {
    seq: 10,
    kind: "assistant_message",
    agent: "general",
    tool: null,
    text: "仓库顶层是个项目",
    data: { step: 2, tool_calls: [] },
  },
  {
    seq: 11,
    kind: "text",
    agent: "general",
    tool: null,
    text: "仓库顶层是个项目",
    data: {},
  },
  {
    seq: 12,
    kind: "agent_end",
    agent: "general",
    tool: null,
    text: "仓库顶层是个项目",
    data: {
      messages: [],
      usage: { turns: 2, llm_calls: 2, total_tokens: 1687 },
    },
  },
];

const fold = (events: AgentEvent[], start: TurnState = emptyTurn): TurnState =>
  events.reduce(reduce, start);

const kinds = (state: TurnState): string[] => state.items.map((i) => i.kind);
const messages = (state: TurnState) =>
  state.items.flatMap((i) => (i.kind === "message" ? [i.message] : []));
const tools = (state: TurnState) =>
  state.items.flatMap((i) => (i.kind === "tool" ? [i.tool] : []));

describe("reduce:真实一轮的因果顺序", () => {
  it("条目顺序与到达顺序一致,叙述在工具卡之前", () => {
    const state = fold(REAL_RUN);
    expect(kinds(state)).toEqual(["dispatch", "message", "tool", "message"]);

    const [dispatch, narration, tool, answer] = state.items as [
      Extract<Item, { kind: "dispatch" }>,
      Extract<Item, { kind: "message" }>,
      Extract<Item, { kind: "tool" }>,
      Extract<Item, { kind: "message" }>,
    ];
    expect(dispatch.dispatch.data.display_name).toBe("qi");
    expect(dispatch.dispatch.data.source).toBe("router");
    expect(narration.message.role).toBe("narration"); // 宣布了工具调用 → 过程
    expect(tool.tool.tool).toBe("ls");
    expect(answer.message.role).toBe("assistant");
  });

  it("流式累积与最终文本一致(真实数据里两者本来就相等)", () => {
    const streaming = fold(REAL_RUN.slice(0, 5)); // 只到第 1 轮的 assistant_message
    const narration = messages(streaming).at(-1);
    expect(narration?.text).toBe("I'll look");

    const answer = messages(fold(REAL_RUN)).at(-1);
    expect(answer?.text).toBe("仓库顶层是个项目");
    expect(answer?.streaming).toBe(false);
  });

  it("工具结果按结构化字段落到视图(status/duration/exit_code/result)", () => {
    const [tool] = tools(fold(REAL_RUN));
    expect(tool).toMatchObject({
      tool: "ls",
      status: "ok",
      durationMs: 0,
      exitCode: null,
      error: null,
    });
    expect(tool?.result).toContain("/Users/hanwei/Desktop/qi");
    expect(tool?.args).toEqual({ path: "." });
  });

  it("usage 原样透出(不在前端做白名单)", () => {
    expect(fold(REAL_RUN).usage).toEqual({
      turns: 2,
      llm_calls: 2,
      total_tokens: 1687,
    });
  });

  it("未处理的事件不产出条目、不改状态(agent_start / 未知 kind)", () => {
    const before = fold(REAL_RUN.slice(0, 2));
    const after = reduce(before, REAL_RUN[1] as AgentEvent); // agent_start
    expect(after).toBe(before); // 同一引用:避免无意义重渲染

    const unknown: AgentEvent = {
      seq: 99,
      kind: "some.future.kind",
      agent: null,
      tool: null,
      text: "x",
      data: {},
    };
    expect(reduce(before, unknown)).toBe(before);
  });
});

describe("reduce:流式与定稿", () => {
  it("连续 text_delta 累积进**同一条**流式消息", () => {
    const state = fold([
      {
        seq: 0,
        kind: "text_delta",
        agent: "a",
        tool: null,
        text: "你",
        data: {},
      },
      {
        seq: 1,
        kind: "text_delta",
        agent: "a",
        tool: null,
        text: "好",
        data: {},
      },
    ]);
    expect(state.items).toHaveLength(1);
    expect(messages(state)[0]).toMatchObject({ text: "你好", streaming: true });
  });

  it("末尾 text 覆盖流式累积(权威值),并结束流式态", () => {
    const state = fold([
      {
        seq: 0,
        kind: "text_delta",
        agent: "a",
        tool: null,
        text: "丢了一",
        data: {},
      },
      {
        seq: 1,
        kind: "text",
        agent: "a",
        tool: null,
        text: "完整文本",
        data: {},
      },
    ]);
    expect(messages(state)[0]).toMatchObject({
      text: "完整文本",
      streaming: false,
    });
  });

  it("text 在没有流式消息时是空操作(不凭空造条目)", () => {
    const state = reduce(emptyTurn, {
      seq: 0,
      kind: "text",
      agent: "a",
      tool: null,
      text: "孤立文本",
      data: {},
    });
    expect(state).toBe(emptyTurn);
  });

  it("assistant_message 无工具调用 → assistant;有工具调用 → narration", () => {
    const plain = reduce(emptyTurn, {
      seq: 0,
      kind: "assistant_message",
      agent: "a",
      tool: null,
      text: "结论",
      data: { step: 2, tool_calls: [] },
    });
    expect(messages(plain)[0]?.role).toBe("assistant");

    const narrate = reduce(emptyTurn, {
      seq: 0,
      kind: "assistant_message",
      agent: "a",
      tool: null,
      text: "我先看看",
      data: { step: 1, tool_calls: ["read"] },
    });
    expect(messages(narrate)[0]?.role).toBe("narration");
  });

  it("空的 assistant_message 不产出条目", () => {
    const state = reduce(emptyTurn, {
      seq: 0,
      kind: "assistant_message",
      agent: "a",
      tool: null,
      text: "",
      data: { step: 1, tool_calls: [] },
    });
    expect(state.items).toHaveLength(0);
  });
});

describe("reduce:工具配对", () => {
  const start = (tool: string): AgentEvent => ({
    seq: 0,
    kind: "tool_start",
    agent: "a",
    tool,
    text: "",
    data: { args: {} },
  });
  const end = (status = "ok"): AgentEvent => ({
    seq: 0,
    kind: "tool_end",
    agent: "a",
    tool: "x",
    text: "输出",
    data: { status, duration_ms: 12, exit_code: null, error: null },
  });

  it("tool_end 落到**最后一个** running 工具上(连续调用不串位)", () => {
    let state = reduce(emptyTurn, start("read"));
    state = reduce(state, end()); // read 完成
    state = reduce(state, start("grep"));
    state = reduce(state, end("error"));

    const [read, grep] = tools(state);
    expect(read).toMatchObject({ tool: "read", status: "ok", result: "输出" });
    expect(grep).toMatchObject({ tool: "grep", status: "error" });
  });

  it("没有 running 工具时 tool_end 是空操作", () => {
    const state = reduce(emptyTurn, end());
    expect(state).toBe(emptyTurn);
  });

  it("缺失/错类型的 data 也不崩(逐字段收窄)", () => {
    const state = reduce(emptyTurn, {
      seq: 0,
      kind: "tool_start",
      agent: null,
      tool: "x",
      text: "",
      data: { args: "不是对象" }, // 脏数据:args 应为对象
    });
    const after = reduce(state, {
      seq: 1,
      kind: "tool_end",
      agent: null,
      tool: "x",
      text: "",
      data: {},
    });
    expect(tools(after)[0]).toMatchObject({
      tool: "x",
      args: {}, // 脏 args 被丢弃
      status: "ok",
      durationMs: 0,
      exitCode: null,
      error: null,
    });
  });
});

describe("reduce:脏数据与不可变", () => {
  it("dispatch 字段全缺时给安全默认值", () => {
    const state = reduce(emptyTurn, {
      seq: 0,
      kind: "dispatch",
      agent: null,
      tool: null,
      text: "",
      data: {},
    });
    expect(state.items[0]).toMatchObject({
      kind: "dispatch",
      dispatch: {
        data: {
          agent: null,
          display_name: "",
          source: "",
          confidence: 0,
          reasoning: "",
        },
      },
    });
  });

  it("opening 的 suggestions 只保留字符串项", () => {
    const state = reduce(emptyTurn, {
      seq: 0,
      kind: "opening",
      agent: "a",
      tool: null,
      text: "从哪开始?",
      data: { suggestions: ["分析仓库", 42, null, "写测试"] },
    });
    const message = messages(state)[0];
    expect(message?.suggestions).toEqual(["分析仓库", "写测试"]);
  });

  it("usage 不是对象时置空,而不是把脏值塞进视图", () => {
    const state = reduce(emptyTurn, {
      seq: 0,
      kind: "agent_end",
      agent: null,
      tool: null,
      text: "",
      data: { usage: "oops" },
    });
    expect(state.usage).toBeUndefined();
  });

  it("reduce 不修改传入状态(React 依赖不可变)", () => {
    const state = fold([start0()]); // 工具处于 running:tool_end 才会真的改写
    const snapshot = JSON.parse(JSON.stringify(state)) as TurnState;
    const next = reduce(state, end0());

    expect(state).toEqual(snapshot); // 原状态未被就地修改
    expect(next).not.toBe(state); // 产出新对象(引用变化才能触发重渲染)
    expect(tools(state)[0]?.status).toBe("running"); // 原工具仍是 running
    expect(tools(next)[0]?.status).toBe("ok");
  });

  it("tool_end 只与 running 的工具配对 —— 孤立 finish 是空操作", () => {
    // 这是真实的线格式约束:tool_end 不带 id,只能对应"最后一个 running 工具"。
    // 若服务端漏发 tool_start,前端宁可少一个卡片,也不能凭空造一个。
    const orphan = reduce(emptyTurn, end0());
    expect(orphan).toBe(emptyTurn);

    const finished = fold([start0(), end0()]);
    expect(reduce(finished, end0())).toBe(finished); // 重复 finish 也不再改
  });

  it("withUserMessage 追加用户消息并把状态置为 running", () => {
    const state = withUserMessage(emptyTurn, "你好");
    expect(state.status).toBe("running");
    expect(messages(state)[0]).toMatchObject({ role: "user", text: "你好" });
  });
});

function start0(): AgentEvent {
  return {
    seq: 0,
    kind: "tool_start",
    agent: "a",
    tool: "ls",
    text: "",
    data: { args: {} },
  };
}
function end0(): AgentEvent {
  return {
    seq: 1,
    kind: "tool_end",
    agent: "a",
    tool: "ls",
    text: "out",
    data: { status: "ok", duration_ms: 1, exit_code: 0, error: null },
  };
}

describe("fromEntries:刷新/切换会话后的回放", () => {
  const entries: Entry[] = [
    {
      type: "session",
      id: "s1",
      title: "会话",
      created_at: "2026-09-13T22:06:37",
      cwd: "/x",
    },
    {
      type: "dispatch",
      agent: "general",
      display_name: "qi",
      confidence: 0.9,
      source: "router",
      reasoning: "选择 general",
    },
    { type: "state", key: "active_agent", value: "general" },
    {
      type: "message",
      role: "user",
      content: "用 ls 看一下",
      agent_id: "general",
    },
    {
      type: "custom",
      custom_type: "assistant_narration",
      agent: "general",
      content: "我先看看",
    },
    {
      type: "tool",
      agent: "general",
      tool: "bash",
      args: { command: "git status" },
      status: "error",
      duration_ms: 18,
      exit_code: 128,
      error: null,
      result: "exit=128\nfatal: not a git repository",
    },
    {
      type: "message",
      role: "assistant",
      content: "不是 git 仓库",
      agent_id: "general",
    },
  ];

  it("顺序与落盘一致(session header 与 state 不渲染)", () => {
    const state = fromEntries(entries);
    expect(kinds(state)).toEqual([
      "dispatch",
      "message",
      "message",
      "tool",
      "message",
    ]);
  });

  it("叙述条目的角色是 narration,位置在工具卡之前", () => {
    const state = fromEntries(entries);
    const narrationIndex = state.items.findIndex(
      (i) => i.kind === "message" && i.message.role === "narration",
    );
    const toolIndex = state.items.findIndex((i) => i.kind === "tool");
    expect(narrationIndex).toBeGreaterThanOrEqual(0);
    expect(narrationIndex).toBeLessThan(toolIndex);

    const narration = state.items[narrationIndex];
    expect(narration?.kind === "message" ? narration.message.text : null).toBe(
      "我先看看",
    );
  });

  it("工具条目带结构化字段(状态/耗时/退出码),可直接渲染工具行", () => {
    const [tool] = tools(fromEntries(entries));
    expect(tool).toMatchObject({
      tool: "bash",
      status: "error",
      durationMs: 18,
      exitCode: 128,
    });
    expect(tool?.result).toContain("fatal: not a git repository");
  });

  it("system 消息与没有 agent 的 dispatch 不渲染", () => {
    const state = fromEntries([
      { type: "message", role: "system", content: "系统提示词" },
      { type: "dispatch", agent: null, display_name: null, confidence: 0 },
    ]);
    expect(state.items).toEqual([]);
  });
});
