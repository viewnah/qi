import { describe, expect, it } from "vitest";
import { itemDepth, parseBanners } from "./segments";

/**
 * 下面这块 `RENDER` 是 **`qi_agents.subagent._render()` 的真实输出**(2026-10-10,
 * 三层树:scout 成功 → writer 失败 → scout 成功)。它不是随手编的样本:解析器的
 * 正则必须能原样吃下 TUI 契约(`render.py: banner()/BANNER_RE`)排出来的字。
 *
 * 换句话说 —— 改了 `banner()` 的格式而没同步这里,这个测试就该红。
 */
const RENDER = `── scout (深度1, 成功) ──
· read_file path=design/web.md
· grep pattern=委派

✎ 第1轮: 先看设计文档再动手
✎ 第2轮: 这层信息够了,汇总返回

上层结论:需要委派两个子任务。

── writer (深度2, 失败) ──
· edit_file path=a.py

✎ 第1轮: 先改再测

[失败] boom: 文件不存在

── scout (深度3, 成功) ──
· grep pattern=深度

✎ 第1轮: 继续查

底层结论。`;

describe("parseBanners —— 真实 _render() 输出", () => {
  const segs = parseBanners(RENDER)!;

  it("切出三段,名字 / 深度 / 成败都取自横幅", () => {
    expect(segs).toHaveLength(3);
    expect(segs.map((s) => [s.name, s.depth, s.ok])).toEqual([
      ["scout", 1, true],
      ["writer", 2, false],
      ["scout", 3, true],
    ]);
  });

  it("归属链 = 预序里最近的更浅一段(同名的两层也能分清)", () => {
    expect(segs[0].parent).toBeUndefined();
    expect(segs[1].parent).toBe("scout");
    expect(segs[2].parent).toBe("writer");
  });

  it("三区分开:活动行、各轮思考、输出,前缀原样保留", () => {
    const top = segs[0];
    expect(top.activity).toEqual([
      "· read_file path=design/web.md",
      "· grep pattern=委派",
    ]);
    expect(top.thoughts).toEqual([
      "✎ 第1轮: 先看设计文档再动手",
      "✎ 第2轮: 这层信息够了,汇总返回",
    ]);
    expect(top.output).toBe("上层结论:需要委派两个子任务。");
    expect(segs[2].activity).toEqual(["· grep pattern=深度"]);
    expect(segs[2].output).toBe("底层结论。");
  });

  it("失败段:ok=false,错误在输出区且以 [失败] 开头", () => {
    const bad = segs[1];
    expect(bad.ok).toBe(false);
    expect(bad.output).toBe("[失败] boom: 文件不存在");
    // 失败不吞活动:它已经干过的活照样看得见
    expect(bad.activity).toEqual(["· edit_file path=a.py"]);
  });
});

describe("parseBanners —— 边界", () => {
  it("没有横幅 → null(调用方退回原始 <pre>)", () => {
    expect(parseBanners("whatever\n工具输出而已")).toBeNull();
    expect(parseBanners("")).toBeNull();
  });

  it("横幅之前还有正文 → 整篇退回原文(那不是横幅文档)", () => {
    const text = `前置说明\n── a (深度1, 成功) ──\n输出`;
    expect(parseBanners(text)).toBeNull();
  });

  it("长得像横幅但状态词不对 → 不算横幅", () => {
    expect(parseBanners("── a (深度1, 完成) ──\n输出")).toBeNull();
  });

  it("输出区里的空行保留(正文是引用,不重排)", () => {
    const text = `── a (深度1, 成功) ──\n第一段\n\n第二段`;
    const segs = parseBanners(text)!;
    expect(segs[0].output).toBe("第一段\n\n第二段");
  });

  it("思考文本自带换行 → 续行贴回当轮,不掉进输出区", () => {
    const text = `── a (深度1, 成功) ──\n✎ 第1轮: 上半句\n续的下半句\n\n输出`;
    const segs = parseBanners(text)!;
    expect(segs[0].thoughts).toEqual(["✎ 第1轮: 上半句\n续的下半句"]);
    expect(segs[0].output).toBe("输出");
  });

  it("活动行紧跟无前缀行 → 无前缀行进输出区(活动是单行契约)", () => {
    const text = `── a (深度1, 成功) ──\n· bash cmd=ls\n直接输出`;
    const segs = parseBanners(text)!;
    expect(segs[0].activity).toEqual(["· bash cmd=ls"]);
    expect(segs[0].output).toBe("直接输出");
  });

  it("CRLF 也能切(Windows / 拷贝粘贴来的日志)", () => {
    const text = RENDER.replace(/\n/g, "\r\n");
    expect(parseBanners(text)!.map((s) => s.name)).toEqual([
      "scout",
      "writer",
      "scout",
    ]);
  });

  it("并列同名同深度:归属按 DFS 栈,不看名字", () => {
    const text = `── scout (深度1, 成功) ──\nA\n\n── scout (深度2, 成功) ──\nB\n\n── scout (深度2, 成功) ──\nC`;
    const segs = parseBanners(text)!;
    expect(segs.map((s) => s.parent)).toEqual([undefined, "scout", "scout"]);
  });
});

describe("itemDepth —— details 列表的层级", () => {
  it("优先用词汇表的 depth", () => {
    expect(itemDepth({ label: "scout · 查", depth: 3 })).toBe(3);
  });

  it("没有 depth → 回退 label 前导空格(两级一格)", () => {
    expect(itemDepth({ label: "scout · 查" })).toBe(1);
    expect(itemDepth({ label: "  writer · 写" })).toBe(2);
    expect(itemDepth({ label: "    scout · 再查" })).toBe(3);
  });

  it("两个都没有 → 顶层", () => {
    expect(itemDepth({ label: "writer", depth: 0 })).toBe(1);
  });
});
