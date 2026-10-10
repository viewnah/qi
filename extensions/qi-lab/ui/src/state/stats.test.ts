/**
 * 输入卡下方那行统计的显示口径测试。
 *
 * 只测**纯函数**(与 projects.test.ts 同一取舍:不引 jsdom)。这里会出错的地方
 * 不是"渲染成什么样",而是**数字怎么读**:12.4k 还是 12400、空上下文该显示 0%
 * 还是不显示 —— 后者一旦写错,界面上会出现一条"永远 0%"的进度条。
 */
import { describe, expect, it } from "vitest";
import type { UsageSummary } from "../api/types";
import { contextShare, countsLabel, formatTokens, processLabel } from "./stats";

function usage(partial: Partial<UsageSummary> = {}): UsageSummary {
   return {
      turns: 0,
      steps: 0,
      tools: 0,
      tool_failures: 0,
      llm_calls: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
      context_tokens: 0,
      ...partial,
   };
}

describe("formatTokens", () => {
   it("千位以下原样:不写 0.98K 这种更费劲的写法", () => {
      expect(formatTokens(0)).toBe("0");
      expect(formatTokens(7)).toBe("7");
      expect(formatTokens(999)).toBe("999");
   });

   it("k / m 的取位与 dsh 一致:>=100 取整,否则一位小数", () => {
      expect(formatTokens(1_000)).toBe("1K");
      expect(formatTokens(12_400)).toBe("12.4K");
      expect(formatTokens(124_000)).toBe("124K");
      expect(formatTokens(1_240_000)).toBe("1.2M");
      expect(formatTokens(124_000_000)).toBe("124M");
   });

   it("脏值不抛:NaN / 负数 / Infinity 都当 0", () => {
      expect(formatTokens(Number.NaN)).toBe("0");
      expect(formatTokens(-5)).toBe("0");
      expect(formatTokens(Number.POSITIVE_INFINITY)).toBe("0");
   });
});

describe("contextShare", () => {
   it("窗口未知或还没有用量 → null(画不了,不是 0)", () => {
      expect(contextShare(0, 128_000)).toBeNull();
      expect(contextShare(1_000, 0)).toBeNull();
      expect(contextShare(1_000, -1)).toBeNull();
   });

   it("超窗口时夹到 1:进度条不该溢出", () => {
      expect(contextShare(64_000, 128_000)).toBe(0.5);
      expect(contextShare(999_999, 128_000)).toBe(1);
   });
});

describe("countsLabel", () => {
   it("全空 → null(整行不渲染)", () => {
      expect(countsLabel(usage())).toBeNull();
   });

   it("有哪几项就报哪几项", () => {
      expect(countsLabel(usage({ turns: 3 }))).toBe("3 轮");
      expect(countsLabel(usage({ turns: 3, steps: 5, tools: 2 }))).toBe(
         "3 轮 · 5 步 · 工具 2",
      );
      // 只说了几句话、还没调过工具:不写"工具 0"。
      expect(countsLabel(usage({ turns: 1, steps: 1 }))).toBe("1 轮 · 1 步");
   });
});

describe("processLabel(过程块收起时的那行字)", () => {
  const think = (text: string) =>
    ({ kind: "think", key: "t", text, live: false }) as const;
  const tool = (status: "ok" | "error") =>
    ({
      kind: "tool",
      key: "x",
      callId: "",
      tool: "ls",
      args: {},
      status,
      durationMs: 1,
      exitCode: 0,
      error: null,
      result: "",
      details: null,
    }) as const;
  const narration = (text: string) =>
    ({ kind: "say", key: "s", agent: null, text, live: false, tone: "narration" }) as const;

  it("思考字数 + 工具次数(+ 失败次数)", () => {
    expect(processLabel([think("一二三四五"), tool("ok"), tool("error")])).toBe(
      "思考 5 字 · 工具 2 次 · 1 次失败",
    );
  });

  it("工具之间的叙述也算进思考字数(块内不区分两者)", () => {
    expect(processLabel([think("一二"), narration("三四五")])).toBe("思考 5 字");
  });

  it("只有工具 / 只有思考 都读得通", () => {
    expect(processLabel([tool("ok")])).toBe("工具 1 次");
    expect(processLabel([think("一二三")])).toBe("思考 3 字");
  });

  it("空块兜底(理论上不会出现)", () => {
    expect(processLabel([])).toBe("过程");
  });
});
