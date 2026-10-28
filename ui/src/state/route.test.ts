/**
 * URL 会话路由的边界。
 *
 * 这份解析器短,但它决定"刷新之后落到哪个会话" —— 错了就回到空态(用户报过的现象),
 * 或者更糟:落进一个别人的/不存在的会话。所以边界都要钉住。
 */
import { describe, expect, it } from "vitest";
import { parseSessionHash, sessionHash } from "./route";

describe("parseSessionHash", () => {
  it("取出 #s=<id>", () => {
    expect(parseSessionHash("#s=abc123")).toBe("abc123");
    expect(parseSessionHash("s=abc123")).toBe("abc123");     // 没有 # 也认(手工拼的)
  });

  it("没有 / 别的键 / 空值 → null(回空态)", () => {
    for (const hash of ["", "#", "#s=", "#other=1", "#x=1&y=2", "#s", "#not-a-kv"]) {
      expect(parseSessionHash(hash)).toBeNull();
    }
  });

  it("在看别的键的同时也能取出它", () => {
    expect(parseSessionHash("#a=1&s=abc&b=2")).toBe("abc");
  });

  it("被编码的 id 解回来", () => {
    expect(parseSessionHash("#s=a%20b")).toBe("a b");
  });

  it("坏转义不抛,只当没有(手拼 URL 不该把页面搞崩)", () => {
    expect(parseSessionHash("#s=%")).toBeNull();
  });
});

describe("sessionHash", () => {
  it("生成与解析互为逆运算", () => {
    expect(sessionHash("abc123")).toBe("#s=abc123");
    expect(parseSessionHash(sessionHash("abc123"))).toBe("abc123");
    expect(sessionHash(null)).toBe("#");
    expect(parseSessionHash(sessionHash(null))).toBeNull();
  });

  it("特殊字符走百分号编码", () => {
    expect(parseSessionHash(sessionHash("a b&c"))).toBe("a b&c");
  });
});
