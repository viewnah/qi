/// <reference types="node" />
/**
 * 跨语言契约守卫。
 *
 * 前端与宿主之间是**手写镜像**(TS 类型 ←→ pydantic 模型),最容易出的事故是
 * "宿主换了字段名 / 加了新事件,前端静默丢掉"。这类事故在运行时只表现为
 * "界面少了一块",很难定位。所以这里直接把两侧对起来比:
 *
 *   1. `CONTRACT_VERSION` 两侧必须一致;
 *   2. 真实的 `/api/meta` 响应(录下来的一手数据)必须满足 `Meta` 的必要字段;
 *   3. **后端发出的每一个事件 kind,前端要么处理、要么显式声明"按设计忽略"**。
 *
 * 第 3 条是重点:新增事件类型时这个测试会红,逼作者当场决定它该怎么渲染。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { CONTRACT_VERSION, type Meta } from "./types";

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");

/** 相对本文件(`web/src/api/`)的仓库根。 */
const ROOT = "../../../";

describe("契约版本", () => {
  it("与 python 侧 schemas.py 一致", () => {
    const schemas = read(`${ROOT}src/qi_agent/web/schemas.py`);
    const match = /CONTRACT_VERSION\s*=\s*"([^"]+)"/.exec(schemas);
    expect(match, "schemas.py 里找不到 CONTRACT_VERSION").not.toBeNull();
    expect(match?.[1]).toBe(CONTRACT_VERSION);
  });
});

describe("/api/meta 的真实响应形状", () => {
  // 一线实录:`qi web` + 真实配置下 curl /api/meta 的原始响应(未改一个字节)。
  const RECORDED_META = `{
    "app": "qi-web",
    "contract": "1",
    "version": "0.1.0",
    "default_cwd": "/Users/hanwei/Desktop/qi",
    "auth_required": false,
    "static_ready": true,
    "capabilities": {
      "sse": true, "streaming": true, "cancel": true,
      "auth_write": true, "config_read": true, "trajectory": true
    }
  }`;

  it("必要字段齐全且契约号匹配(允许宿主新增字段)", () => {
    const meta = JSON.parse(RECORDED_META) as Partial<Meta>;
    expect(meta).toMatchObject({
      app: "qi-web",
      contract: CONTRACT_VERSION,
      auth_required: false,
      static_ready: true,
    });
    expect(typeof meta.version).toBe("string");
    expect(typeof meta.default_cwd).toBe("string");
    expect(meta.capabilities?.streaming).toBe(true);
  });
});

describe("事件 kind 两侧对齐", () => {
  /** 前端 `reduce` 会渲染的 kind。 */
  const HANDLED = [
    "dispatch",
    "text_delta",
    "assistant_message",
    "text",
    "tool_start",
    "tool_end",
    "opening",
    "error",
    "agent_end",
  ];
  /** 前端**按设计**不渲染的 kind(要么是传输层控制帧,要么已有别的投影)。 */
  const IGNORED_BY_DESIGN = [
    "agent_start", // 只表示"开始跑",没有可展示内容
    "snapshot", // 由 client.ts 单独处理(重建条目列表)
    "run.finished", // 由 client.ts 单独处理(收尾 + 重拉明细)
  ];

  it("后端发出的 kind 全部被处理或显式忽略", () => {
    const sources = [
      read(`${ROOT}src/qi_agent/runner.py`),
      read(`${ROOT}src/qi_agent/runtime.py`),
    ].join("\n");
    const emitted = new Set(
      [...sources.matchAll(/kind="([a-z_.]+)"/g)].map((m) => m[1] as string),
    );
    // 传输层那两个由 web 层产出,不在上面的两个文件里
    const webLayer = read(`${ROOT}src/qi_agent/web/state.py`);
    expect(webLayer).toContain('"run.finished"');
    emitted.add("run.finished");

    const known = new Set([...HANDLED, ...IGNORED_BY_DESIGN]);
    const unaccounted = [...emitted].filter((k) => !known.has(k)).sort();
    expect(
      unaccounted,
      `后端新增了未声明的 kind:${unaccounted.join(", ")}`,
    ).toEqual([]);
  });

  it("声明清单里的每一项都能在后端找到出处(清单不许留陈迹)", () => {
    const sources = [
      read(`${ROOT}src/qi_agent/runner.py`),
      read(`${ROOT}src/qi_agent/runtime.py`),
      read(`${ROOT}src/qi_agent/web/state.py`),
    ].join("\n");
    for (const kind of HANDLED.filter((k) => k !== "run.finished")) {
      expect(sources, `前端声明处理 ${kind},但后端没发`).toContain(`"${kind}"`);
    }
  });
});
