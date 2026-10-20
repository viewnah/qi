/**
 * 切帧测试。
 *
 * AG-UI 的编码器只写 `data:`(`encodeSSE` = `` `data: ${JSON.stringify(e)}\n\n` ``),
 * 所以这里主要验证两件事:
 *   1. 不依赖 `event:` —— 类型在 JSON 里,切帧只看 `data:`;
 *   2. **半帧不能丢** —— 网络分块会从任意位置切断,尾巴必须原样留下次拼。
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parseFrames } from "./client";

describe("parseFrames", () => {
  it("取出一条完整的 data 帧", () => {
    const r = parseFrames('data: {"type":"RUN_STARTED"}\n\n');
    expect(r.payloads).toEqual(['{"type":"RUN_STARTED"}']);
    expect(r.rest).toBe("");
  });

  it("一次切出多帧", () => {
    const r = parseFrames('data: {"a":1}\n\ndata: {"b":2}\n\n');
    expect(r.payloads).toEqual(['{"a":1}', '{"b":2}']);
  });

  it("忽略注释帧(心跳),但保留半帧", () => {
    const r = parseFrames(': qi-lab sse open\n\ndata: {"a":');
    expect(r.payloads).toEqual([]);
    expect(r.rest).toBe('data: {"a":');
  });

  it("半帧在下次拼接后完整取出", () => {
    const first = parseFrames('data: {"type":"TEXT_MESSAGE_CONT');
    expect(first.payloads).toEqual([]);
    const second = parseFrames(first.rest + 'ENT","delta":"你"}\n\n');
    expect(second.payloads).toEqual([
      '{"type":"TEXT_MESSAGE_CONTENT","delta":"你"}',
    ]);
  });

  it("按 SSE 规范把多行 data 用 \\n 连接", () => {
    // 官方编码器不会这么写,但规范允许。不处理的话会把载荷静默截断。
    const r = parseFrames("data: line1\ndata: line2\n\n");
    expect(r.payloads).toEqual(["line1\nline2"]);
  });

  it("容忍 `event:` 字段(SSE 允许,AG-UI 不写)但不据此丢帧", () => {
    const r = parseFrames('event: CUSTOM\ndata: {"type":"CUSTOM"}\n\n');
    expect(r.payloads).toEqual(['{"type":"CUSTOM"}']);
  });

  it("data 冒号后的一个空格被去掉(规范要求)", () => {
    const r = parseFrames('data:{"a":1}\n\n');
    expect(r.payloads).toEqual(['{"a":1}']);
  });
});

describe("模型面:客户端与宿主的两侧对齐", () => {
  /**
   * 这两条是**变异验证**:在 `app.py` 里改掉路由路径(或换个请求体字段名),
   * 它们必须变红。理由与 `contract.test.ts` 同一条 —— 前端与宿主之间没有类型系统,
   * 唯一的桥梁是字符串;而字符串写错了的症状是运行时 404/422,不是编译错误。
   */
  const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
  const appPy = readFileSync(resolve(ROOT, "qi_lab/app.py"), "utf8");
  /** 请求/响应模型在 `schemas.py` —— 字段名那一半的真相在那里。 */
  const schemasPy = readFileSync(resolve(ROOT, "qi_lab/schemas.py"), "utf8");
  const clientTs = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "client.ts"),
    "utf8",
  );

  it("宿主真的注册了 /api/models 与 /api/model", () => {
    expect(appPy).toContain('@app.get("/api/models"');
    expect(appPy).toContain('@app.post("/api/model"');
  });

  it("客户端打的就是这两个路径", () => {
    expect(clientTs).toContain("`/api/models");
    expect(clientTs).toContain('request<ModelCatalog>("/api/model"');
  });

  it("请求体字段名两侧一致(改一个而没改另一个 = 422)", () => {
    // 宿主用 pydantic 收这四个键;前端只要拼错一个,后端就收不到它 ——
    // 而症状是"点了没反应"(字段是可选时不会 422),不是编译错误。
    for (const field of ["provider", "model", "thinking_level", "session"]) {
      expect(schemasPy, `宿主不认识 ${field}`)
        .toContain(`${field}: str | None = None`);
    }
    for (const field of ["provider", "model", "thinking_level", "session"]) {
      expect(clientTs, `前端没送 ${field}`).toMatch(
        new RegExp(`${field}\\?: string`),
      );
    }
  });
});

describe("MCP 声明表:增删改的两侧对齐", () => {
  /**
   * 同一条“变异验证”:POST/PATCH/DELETE 这三个动词与 `X-Qi-Confirm` 头都是**字符串**,
   * 拼错了不报编译错 —— 症状是 404 / 428,也就是“点了没反应”。
   */
  const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
  const appPy = readFileSync(resolve(ROOT, "qi_lab/app.py"), "utf8");
  const schemasPy = readFileSync(resolve(ROOT, "qi_lab/schemas.py"), "utf8");
  const here = dirname(fileURLToPath(import.meta.url));
  const clientTs = readFileSync(resolve(here, "client.ts"), "utf8");
  const typesTs = readFileSync(resolve(here, "types.ts"), "utf8");

  it("宿主注册了三个写端点", () => {
    for (const verb of ["post", "patch", "delete"]) {
      expect(appPy, `宿主缺 @app.${verb}("/api/mcp")`).toContain(
        `@app.${verb}("/api/mcp"`,
      );
    }
  });

  it("客户端打的就是这三个动词,且都带确认头", () => {
    for (const [name, verb] of [
      ["createMcp", "POST"],
      ["patchMcp", "PATCH"],
      ["deleteMcp", "DELETE"],
    ] as const) {
      const at = clientTs.indexOf(`${name}:`);
      expect(at, `前端缺 ${name}`).toBeGreaterThan(-1);
      const block = clientTs.slice(at, at + 400);
      expect(block, `${name} 的动词不对`).toContain(`method: "${verb}"`);
      // 确认头少了 → 宿主回 428(§8.3:确认弹层与确认头成对出现)。
      expect(block, `${name} 没带确认头`).toContain('"X-Qi-Confirm": "yes"');
    }
  });

  it("写请求体的模型两侧都在(字段名以 schemas.py 为准)", () => {
    for (const cls of [
      "class McpServerEdit",
      "class McpWrite",
      "class McpPatch",
      "class McpDelete",
    ]) {
      expect(schemasPy, `宿主缺 ${cls}`).toContain(cls);
    }
    for (const field of [
      "name",
      "transport",
      "command",
      "env",
      "headers",
      "direct_tools",
    ]) {
      expect(typesTs, `前端写请求体缺 ${field}`).toContain(`${field}:`);
    }
  });

  it("传输闭集是同一份(差一个 = 要么 422,要么老声明存不回去)", () => {
    // `\r?\n`:Python 源码在 Windows 检出时是 CRLF(仓库里存 LF)。只认 `\n` 会让这条
    // 在 Windows 上**永远读不到**,而症状是"两边都对却报 0 个" —— 那种红会被人当成噪音。
    const host = [...(schemasPy.match(/MCP_TRANSPORTS[\s\S]{0,240}?\)\r?\n/) ?? "")]
      .flatMap((m) => [...m.matchAll(/"([a-z-]+)"/g)])
      .map((m) => m[1]);
    const ui = [...(typesTs.match(/MCP_TRANSPORTS = \[[\s\S]*?\] as const/) ?? "")]
      .flatMap((m) => [...m.matchAll(/"([a-z-]+)"/g)])
      .map((m) => m[1]);
    expect(host.length, "schemas.py 里的 MCP_TRANSPORTS 没读到").toBeGreaterThan(0);
    expect(ui).toEqual(host);
  });
});
