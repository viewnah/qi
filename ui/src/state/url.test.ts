import { describe, expect, it } from "vitest";
import { isExternalHttpUrl } from "./url";

describe("isExternalHttpUrl", () => {
   it("接受 http(s) 绝对地址", () => {
      expect(isExternalHttpUrl("https://eval.example.com/plan")).toBe(true);
      expect(isExternalHttpUrl("http://127.0.0.1:8080/a?x=1#y")).toBe(true);
   });

   it("拒绝其它协议", () => {
      expect(isExternalHttpUrl("javascript:alert(1)")).toBe(false);
      expect(isExternalHttpUrl("data:text/html,x")).toBe(false);
      expect(isExternalHttpUrl("file:///etc/passwd")).toBe(false);
      expect(isExternalHttpUrl("ftp://example.com/x")).toBe(false);
   });

   it("拒绝相对路径、空白与反斜杠", () => {
      expect(isExternalHttpUrl("/assistants/plan")).toBe(false);
      expect(isExternalHttpUrl("https://example.com/a b")).toBe(false);
      expect(isExternalHttpUrl("https://example.com\\@evil.com")).toBe(false);
   });

   it("拒绝带用户名/密码的 URL", () => {
      expect(isExternalHttpUrl("https://user:pass@example.com/x")).toBe(false);
   });

   it("拒绝空串与垃圾", () => {
      expect(isExternalHttpUrl("")).toBe(false);
      expect(isExternalHttpUrl("not a url")).toBe(false);
   });
});
