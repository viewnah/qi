/**
 * 契约守卫:**后端能发出的 AG-UI 事件类型,前端必须全部处理**。
 *
 * 存在理由(它真的抓到过东西):旧协议下后端加了 4 个 kind 而前端没声明,
 * 前端只是**静默丢掉**那些事件 —— 没有任何报错、没有测试变红,只是界面上少了东西。
 * 那次是靠这个守卫才发现的。改造后换成 AG-UI,但风险形状完全一样:
 * 后端多一个 `type`,前端 `switch` 走 default(或者干脆不处理)。
 *
 * 做法:从 python 侧源码里刮出所有 AG-UI 事件类型字面量,与前端声明的清单对拍。
 * 这是**变异验证**:在 `agui.py` 里加一个没声明的新类型,这个测试必须变红并指名道姓。
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const read = (p: string) => readFileSync(resolve(ROOT, p), "utf8");

/** 前端 `reduce` 真正会渲染的类型。 */
const HANDLED = [
  "RUN_STARTED",
  "RUN_FINISHED",
  "RUN_ERROR",
  "TEXT_MESSAGE_START",
  "TEXT_MESSAGE_CONTENT",
  "TEXT_MESSAGE_END",
  "REASONING_START",
  "REASONING_MESSAGE_START",
  "REASONING_MESSAGE_CONTENT",
  "REASONING_MESSAGE_END",
  "REASONING_END",
  "TOOL_CALL_START",
  "TOOL_CALL_ARGS",
  "TOOL_CALL_END",
  "TOOL_CALL_RESULT",
  "STATE_SNAPSHOT",
  "MESSAGES_SNAPSHOT",
  "CUSTOM",
];

/** 前端**按设计**不渲染的类型(有理由,不是漏掉)。 */
const IGNORED_BY_DESIGN = [
  "STEP_STARTED",
  "STEP_FINISHED",
  "STATE_DELTA",
  "ACTIVITY_SNAPSHOT",
  "ACTIVITY_DELTA",
  "RAW",
];

/** qi 自己的 CUSTOM 名字:每一个都必须在 turn.ts 里有分支,否则会落到 default。 */
const QI_CUSTOM_NAMES = [
  "qi.history",
  "qi.dispatch",
  "qi.opening",
  "qi.narration",
  "qi.compaction",
  "qi.branch",
];

describe("契约号", () => {
  it("两侧的 CONTRACT_VERSION 一致", () => {
    const py = read("src/qi_agent/web/schemas.py");
    const ts = read("web/src/api/types.ts");
    const host = /CONTRACT_VERSION = "([^"]+)"/.exec(py)?.[1];
    const ui = /CONTRACT_VERSION = "([^"]+)"/.exec(ts)?.[1];
    expect(host).toBeDefined();
    expect(ui).toBe(host);
  });

  it("AG-UI 改造是破坏性的,所以契约号必须是 2", () => {
    // 这条断言的意义:如果谁把版本号改回 1,说明他以为形状没变。
    expect(
      /CONTRACT_VERSION = "([^"]+)"/.exec(
        read("src/qi_agent/web/schemas.py"),
      )?.[1],
    ).toBe("2");
  });
});

describe("事件类型两侧对齐", () => {
  it("后端能发的 AG-UI 类型全部被处理或显式忽略", () => {
    const sources = [
      read("src/qi_agent/web/agui.py"),
      read("src/qi_agent/web/app.py"),
    ].join("\n");
    // agui.py 里事件类型只出现在 `"type": "XXX"` 或 `_base("XXX")` 两种位置
    const emitted = new Set<string>();
    for (const m of sources.matchAll(/_base\("([A-Z_]+)"\)/g))
      emitted.add(m[1] as string);
    for (const m of sources.matchAll(/"type": "([A-Z_]+)"/g))
      emitted.add(m[1] as string);
    expect(
      emitted.size,
      "刮不到任何事件类型,说明 agui.py 的写法变了,守卫已失效",
    ).toBeGreaterThan(8);

    const known = new Set([...HANDLED, ...IGNORED_BY_DESIGN]);
    const unaccounted = [...emitted].filter((t) => !known.has(t)).sort();
    expect(
      unaccounted,
      `后端新增了未声明的 AG-UI 类型:${unaccounted.join(", ")} —— 请在 turn.ts 里处理,或加进 IGNORED_BY_DESIGN 并写明理由`,
    ).toEqual([]);
  });

  it("声明清单里的每一项都能在后端找到出处(清单不许留陈迹)", () => {
    const sources = [
      read("src/qi_agent/web/agui.py"),
      read("src/qi_agent/web/app.py"),
    ].join("\n");
    for (const t of HANDLED.filter((x) => !IGNORED_BY_DESIGN.includes(x))) {
      // CUSTOM 由 applyCustom 组装,不在 _base 里,单独断言
      if (t === "CUSTOM") {
        expect(sources).toContain("CUSTOM");
        continue;
      }
      expect(sources, `前端声明处理 ${t},但后端没发`).toMatch(
        new RegExp(`_base\\("${t}"\\)|"type": "${t}"`),
      );
    }
  });

  it("qi 的 CUSTOM 名字在两侧一致(后端发 = 前端认)", () => {
    const py = read("src/qi_agent/web/agui.py");
    const ts = read("web/src/api/types.ts");
    for (const name of QI_CUSTOM_NAMES) {
      expect(py, `后端不发的 CUSTOM 名字:${name}`).toContain(`"${name}"`);
      expect(ts, `前端不认识但后端会发的 CUSTOM:${name}`).toContain(
        `"${name}"`,
      );
    }
  });
});
