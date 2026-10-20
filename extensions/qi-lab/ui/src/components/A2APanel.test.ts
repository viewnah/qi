/**
 * A2A 表单的**请求体构造**。这是整块里唯一有分支的地方,也是最容易悄悄写错的地方:
 *
 * · 新建:不送 `null` / 空串(默认值只有 qi-a2a 一处,这里抄一份就会不同步);
 * · 编辑:没填的键**不送**(PATCH 只改送出去的键),`headers` 留空必须等于"不改";
 * · `headers` 填 `-` = 清空整张表(与后端的 `{}` 对齐);
 * · `timeoutMs` 不是正整数就拦住(它写坏了不会报错,只会让一次调用永远超时)。
 */
import { describe, expect, it } from "vitest";

import type { A2AEntryInfo } from "../api/types";
import { buildA2ABody, initialDraft, type A2ADraft } from "./A2APanel";

function entry(overrides: Partial<A2AEntryInfo> = {}): A2AEntryInfo {
   return {
      name: "hikqin",
      card_url: "http://gw/a2a/agents/a1/card.json",
      base_url: "",
      rpc_url: "",
      tenant: "agent-1",
      description: "",
      header_keys: ["Authorization"],
      timeout_ms: 600_000,
      streaming: true,
      discovery: "auto",
      direct_tools: false,
      tool_name: "",
      disabled: false,
      unknown_fields: [],
      source: "project",
      layers: ["project"],
      ...overrides,
   };
}

function draft(patch: Partial<A2ADraft> = {}): A2ADraft {
   return { ...initialDraft(null), name: "new-one", ...patch };
}

describe("A2A 表单 → 请求体", () => {
   it("新建只送填了的键,不把 null / 空串灌进声明", () => {
      const built = buildA2ABody(null, draft({ base_url: "http://gw:8080" }));

      expect(built.ok).toBe(true);
      if (!built.ok || built.mode !== "create") throw new Error("应当是新建体");
      expect(built.entry.name).toBe("new-one");
      expect(built.entry.base_url).toBe("http://gw:8080");
      expect(built.entry).not.toHaveProperty("timeout_ms");
      expect(built.entry).not.toHaveProperty("streaming");
      expect(built.entry).not.toHaveProperty("headers");
      expect(built.entry).not.toHaveProperty("tenant");
   });

   it("新建时 headers 按行解析,支持 ${ENV} 占位", () => {
      const built = buildA2ABody(
         null,
         draft({ headers: "Authorization: Bearer ${TOKEN}\n# 注释行\nX-Env: dev" }),
      );

      if (!built.ok || built.mode !== "create") throw new Error("应当是新建体");
      expect(built.entry.headers).toEqual({
         Authorization: "Bearer ${TOKEN}",
         "X-Env": "dev",
      });
   });

   it("headers 少写冒号会拦住,而不是悄悄丢掉这一行", () => {
      const built = buildA2ABody(null, draft({ headers: "Authorization Bearer x" }));

      expect(built.ok).toBe(false);
      if (built.ok) throw new Error("应当被拦住");
      expect(built.reason).toContain("冒号");
   });

   it("编辑时 headers 留空 = 不不送这个键(读端给不出值,送空就等于抹掉)", () => {
      const built = buildA2ABody("hikqin", initialDraft(entry()));

      if (!built.ok || built.mode !== "patch") throw new Error("应当是修改体");
      expect(built.patch).not.toHaveProperty("headers");
   });

   it("编辑时 headers 填 - = 清空整张表", () => {
      const built = buildA2ABody("hikqin", { ...initialDraft(entry()), headers: "-" });

      if (!built.ok || built.mode !== "patch") throw new Error("应当是修改体");
      expect(built.patch.headers).toEqual({});
   });

   it("编辑时不改名字就不把 name 塞进 patch", () => {
      const built = buildA2ABody("hikqin", initialDraft(entry()));

      if (!built.ok || built.mode !== "patch") throw new Error("应当是修改体");
      expect(built.patch).not.toHaveProperty("name");
   });

   it("改名会被带上(PATCH 顶层那个 name 是改前的名字)", () => {
      const built = buildA2ABody("hikqin", { ...initialDraft(entry()), name: "renamed" });

      if (!built.ok || built.mode !== "patch") throw new Error("应当是修改体");
      expect(built.patch.name).toBe("renamed");
   });

   it("timeoutMs 必须是正整数", () => {
      for (const value of ["0", "-1", "abc", "1.5"]) {
         const built = buildA2ABody(null, draft({ timeout_ms: value }));
         expect(built.ok, value).toBe(false);
      }
      const ok = buildA2ABody(null, draft({ timeout_ms: "900000" }));
      if (!ok.ok || ok.mode !== "create") throw new Error("应当是新建体");
      expect(ok.entry.timeout_ms).toBe(900000);
   });

   it("名字为空或带首尾空白都拦住(一个手滑的空格会造出两个同名条目)", () => {
      expect(buildA2ABody(null, draft({ name: "  " })).ok).toBe(false);
      expect(buildA2ABody(null, draft({ name: " x " })).ok).toBe(false);
   });

   it("两个勾选框总是送:取消勾选要真的把 disabled / directTools 删掉", () => {
      const built = buildA2ABody("hikqin", {
         ...initialDraft(entry({ disabled: true, direct_tools: true })),
         disabled: false,
         direct_tools: false,
      });

      if (!built.ok || built.mode !== "patch") throw new Error("应当是修改体");
      expect(built.patch.disabled).toBe(false);
      expect(built.patch.direct_tools).toBe(false);
   });
});
