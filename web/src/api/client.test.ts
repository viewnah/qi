/**
 * 切帧测试。
 *
 * AG-UI 的编码器只写 `data:`(`encodeSSE` = `` `data: ${JSON.stringify(e)}\n\n` ``),
 * 所以这里主要验证两件事:
 *   1. 不依赖 `event:` —— 类型在 JSON 里,切帧只看 `data:`;
 *   2. **半帧不能丢** —— 网络分块会从任意位置切断,尾巴必须原样留下次拼。
 */
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
    const r = parseFrames(': qi-web sse open\n\ndata: {"a":');
    expect(r.payloads).toEqual([]);
    expect(r.rest).toBe('data: {"a":');
  });

  it("半帧在下次拼接后完整取出", () => {
    const first = parseFrames('data: {"type":"TEXT_MESSAGE_CONT');
    expect(first.payloads).toEqual([]);
    const second = parseFrames(first.rest + 'ENT","delta":"你"}\n\n');
    expect(second.payloads).toEqual(['{"type":"TEXT_MESSAGE_CONTENT","delta":"你"}']);
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
