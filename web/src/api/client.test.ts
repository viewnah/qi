/**
 * SSE 切帧测试。
 *
 * 这是手写的协议解析:服务端按 `\n\n` 分帧,心跳是 `: ...` 注释行。
 * 解析错了的表现是"页面偶尔少一条事件"或"流卡住",极难现场排查,所以逐条锁住行为。
 */
import { describe, expect, it } from "vitest";
import { parseFrames } from "./client";

describe("parseFrames", () => {
  it("解析一帧(带 id/event/data)", () => {
    const raw = 'id: 7\nevent: text_delta\ndata: {"kind":"text_delta"}\n\n';
    const { frames, rest } = parseFrames(raw);
    expect(rest).toBe("");
    expect(frames).toEqual([
      { id: 7, event: "text_delta", data: '{"kind":"text_delta"}' },
    ]);
  });

  it("一次到达多帧时按顺序全部产出", () => {
    const raw =
      ": qi-web sse open\n\n" +
      'event: snapshot\ndata: {"id":"abc"}\n\n' +
      'id: 0\nevent: dispatch\ndata: {"kind":"dispatch"}\n\n';
    const { frames, rest } = parseFrames(raw);
    expect(rest).toBe("");
    expect(frames.map((f) => f.event)).toEqual(["snapshot", "dispatch"]);
    expect(frames[0]?.id).toBeUndefined(); // 快照故意不带 id(避免与 seq 撞号)
    expect(frames[1]?.id).toBe(0);
  });

  it("半帧留在 rest 里,下次拼接后继续解析(跨 TCP 分片)", () => {
    const first = 'id: 1\nevent: text_delta\ndata: {"te';
    const { frames, rest } = parseFrames(first);
    expect(frames).toEqual([]);
    expect(rest).toBe(first); // 原样保留,不能丢字节

    const { frames: done } = parseFrames(rest + 'xt":"hi"}\n\n');
    expect(done).toEqual([
      { id: 1, event: "text_delta", data: '{"text":"hi"}' },
    ]);
  });

  it("心跳/注释帧不产出条目", () => {
    const { frames, rest } = parseFrames(": ping\n\n: idle\n\n");
    expect(frames).toEqual([]);
    expect(rest).toBe("");
  });

  it("多行 data 用换行拼回(SSE 规范)", () => {
    const raw = "event: x\ndata: line1\ndata: line2\n\n";
    const { frames } = parseFrames(raw);
    expect(frames[0]?.data).toBe("line1\nline2");
  });

  it("`data:` 无空格也接受;数据内的冒号不被截断", () => {
    const raw = 'event: x\ndata:{"url":"http://127.0.0.1:30142"}\n\n';
    const { frames } = parseFrames(raw);
    expect(frames[0]?.data).toBe('{"url":"http://127.0.0.1:30142"}');
  });

  it("没有 event 行时默认 event=message(不产出空帧)", () => {
    const { frames } = parseFrames('data: {"a":1}\n\n');
    expect(frames).toEqual([
      { id: undefined, event: "message", data: '{"a":1}' },
    ]);
  });

  it("只有注释与字段、没有 data 的帧不产出条目", () => {
    const { frames } = parseFrames("id: 3\nevent: text_delta\n\n");
    expect(frames).toEqual([]);
  });

  it("空输入是恒等(不会崩、不会产出)", () => {
    expect(parseFrames("")).toEqual({ frames: [], rest: "" });
  });

  it("非数字 id 得到 NaN —— 已知且无害(前端不用 id 续传,用的是 ?from=)", () => {
    const { frames } = parseFrames("id: not-a-number\nevent: x\ndata: {}\n\n");
    expect(Number.isNaN(frames[0]?.id as number)).toBe(true);
  });
});
