/**
 * 表单 → 请求体的单测。
 *
 * 钉的是整个表单里**唯一会静默丢值**的那条规则:编辑态下,读不回来的值
 * (`command` / `args` / `env` / `headers` / `cwd`)必须「**留空 = 不改**」。
 * 这条错了不会报错 —— 保存一次,凭证就从 `mcp.json` 里消失了,而用户只会觉得
 * “界面能存,但连不上”。所以按仓里既有的规矩(只测纯函数、不引 jsdom)直接测
 * `buildMcpBody`:它就是“哪些键会被写进文件”的唯一真相。
 *
 * 另一半的真相在宿主侧:那半边由 `tests/test_lab_mcp_write_api.py` 钉
 * (按 `model_fields_set` 判、值不回显、428 闸)。
 */
import { describe, expect, it } from "vitest";
import type { McpServerInfo } from "../api/types";
import {
   buildMcpBody,
   initialDraft,
   type Draft,
   type McpTarget,
} from "./McpPanel";

/** 一份带 env/headers/disabled 的 stdio 声明(读端给的样子:值只剩键名)。 */
const STDIO: McpServerInfo = {
   name: "docs",
   transport: "stdio",
   url: "",
   socket: "",
   env_keys: ["TOK"],
   header_keys: ["Authorization"],
   disabled: false,
   direct_tools: false,
   unknown_fields: [],
   source: "project",
};

const HTTP: McpServerInfo = {
   ...STDIO,
   name: "remote",
   transport: "streamable-http",
   url: "https://mcp.example.com/mcp",
   env_keys: [],
   header_keys: [],
};

function target(server: McpServerInfo | null): McpTarget {
   return {
      scope: "project",
      role: "",
      path: "/tmp/x/.qi/mcp.json",
      server,
   };
}

/** 取出 patch;新建时这条断言会直接失败(没有 patch 就谈不上“改了什么”)。 */
function patchOf(server: McpServerInfo, draft: Draft) {
   const built = buildMcpBody(target(server), draft);
   if (!built.ok) throw new Error(built.error);
   if (!("patch" in built.body)) throw new Error("期望是 PATCH,却是 POST");
   return built.body.patch;
}

describe("编辑:留空 = 不改", () => {
   it("一动不动地保存,一个键都不送", () => {
      // 这是最关键的一条:env/headers 的值读不回来,草稿里是空的 ——
      // 空必须被理解成“别动它”,而不是“把它清掉”。
      const patch = patchOf(STDIO, initialDraft(STDIO));
      expect(Object.keys(patch)).toEqual([]);
   });

   it("没填 command/cwd/env/headers 时,这些键根本不出现", () => {
      const draft = { ...initialDraft(STDIO), name: "docs2", disabled: true };
      const patch = patchOf(STDIO, draft);
      expect(patch).toEqual({ name: "docs2", disabled: true });
      expect("command" in patch).toBe(false);
      expect("env" in patch).toBe(false);
      expect("headers" in patch).toBe(false);
   });

   it("填了 env 就是**整体替换**(不是合并)——所以草稿里要清掉旧的键", () => {
      const draft = { ...initialDraft(STDIO), env: "A=1\nB=2" };
      expect(patchOf(STDIO, draft)).toEqual({ env: { A: "1", B: "2" } });
   });

   it("headers 按第一个冒号切(值里可以再有冒号)", () => {
      const draft = { ...initialDraft(HTTP), headers: "Authorization: Bearer a:b" };
      expect(patchOf(HTTP, draft)).toEqual({
         headers: { Authorization: "Bearer a:b" },
      });
   });

   it("读不到的值不进草稿(进了就会被原样写回一个空值)", () => {
      const draft = initialDraft(STDIO);
      expect(draft.command).toBe("");
      expect(draft.args).toBe("");
      expect(draft.cwd).toBe("");
      expect(draft.env).toBe("");
      expect(draft.headers).toBe("");
      // 读得回来的照原样带出来
      expect(draft.name).toBe("docs");
      expect(draft.direct).toBe("off");
   });
});

describe("编辑:只送变过的键", () => {
   it("开关与直连方式变了才送", () => {
      const draft = { ...initialDraft(STDIO), disabled: true, direct: "all" as const };
      expect(patchOf(STDIO, draft)).toEqual({
         disabled: true,
         direct_tools: true,
      });
   });

   it("直连指定几个 = 送名字数组", () => {
      const draft = {
         ...initialDraft(STDIO),
         direct: "list" as const,
         directList: "read, grep\nwrite",
      };
      expect(patchOf(STDIO, draft).direct_tools).toEqual(["read", "grep", "write"]);
   });

   it("换传输时把 transport 送过去;旧 url 由宿主清(kind 变了就作废)", () => {
      const draft = { ...initialDraft(HTTP), transport: "stdio", command: "npx" };
      const patch = patchOf(HTTP, draft);
      expect(patch.transport).toBe("stdio");
      expect(patch.command).toBe("npx");
      // 旧 url 原样留着(没送 → 不改),但宿主见到 kind 变了会把它清掉 ——
      // 这一侧不能自作主张送 url="",那会把“没动过”也说成“删掉了”。
      expect("url" in patch).toBe(false);
   });
});

describe("新建:载荷必须齐", () => {
   it("stdio 少 command 不给提交", () => {
      const built = buildMcpBody(target(null), initialDraft(null));
      expect(built.ok).toBe(false); // 名字还空着
      const draft: Draft = { ...initialDraft(null), name: "a" };
      const noCommand = buildMcpBody(target(null), draft);
      expect(noCommand.ok).toBe(false);
      if (noCommand.ok) throw new Error("不该放行");
      expect(noCommand.error).toContain("command");
   });

   it("http 少 url 不给提交", () => {
      const draft: Draft = { ...initialDraft(null), name: "a", transport: "sse" };
      const built = buildMcpBody(target(null), draft);
      expect(built.ok).toBe(false);
      if (built.ok) throw new Error("不该放行");
      expect(built.error).toContain("url");
   });

   it("换到 stdio 却没给命令:即使是编辑态也不放行(旧命令不属于这个传输)", () => {
      const draft = { ...initialDraft(HTTP), transport: "stdio", command: "" };
      const built = buildMcpBody(target(HTTP), draft);
      expect(built.ok).toBe(false);
   });

   it("名字首尾有空白不放行(它会变成另一个 server 的名字)", () => {
      const draft: Draft = { ...initialDraft(null), name: " a " };
      const built = buildMcpBody(target(null), draft);
      expect(built.ok).toBe(false);
      if (built.ok) throw new Error("不该放行");
      expect(built.error).toContain("空白");
   });

   it("读端认出的陌生传输不放行(要用户挑一个真的)", () => {
      const draft: Draft = {
         ...initialDraft(null),
         name: "a",
         transport: "unknown",
      };
      const built = buildMcpBody(target(null), draft);
      expect(built.ok).toBe(false);
   });

   it("env 写成没有 = 的一行不放行", () => {
      const draft: Draft = {
         ...initialDraft(null),
         name: "a",
         env: "JUST_A_NAME",
      };
      const built = buildMcpBody(target(null), draft);
      expect(built.ok).toBe(false);
      if (built.ok) throw new Error("不该放行");
      expect(built.error).toContain("KEY=VALUE");
   });
});
