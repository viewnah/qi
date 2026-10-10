/**
 * 确认层与请求体的**同源**测试。
 *
 * 这三个纯函数是"弹层里显示的"与"请求里发出去的"之间唯一的那根线:名字与层从同一
 * 个 `source` 推出来,所以不可能出现"弹层说删用户层、请求删了项目层"。这条错了不会
 * 报错 —— 它会安安静静地删掉另一层的那个角色,而 `project` 同名覆盖 `user`,被删的
 * 往往正是你本来想留下的那个。
 *
 * 另一半的真相在宿主侧:那半边由 `extensions/qi-lab/tests/test_lab_agent_write_api.py` 钉
 * (两层各自可删、只删该层、按显示名找、符号链接 400、428 闸、`agent_write` 能力位)。
 *
 * 按仓里的规矩只测纯函数:不引 jsdom、不渲染(`environment: "node"`)。
 */
import { describe, expect, it } from "vitest";
import type { AgentInfo } from "../api/types";
import {
   agentDeleteBody,
   agentDeleteRows,
   agentScopeOf,
} from "./AgentDelete";

/** 一份读端点给的角色(两个层各来一个)。 */
const USER_AGENT: AgentInfo = {
   name: "coordinator",
   description: "总协调",
   source: "user",
   tools: ["read", "subagent"],
   model: "",
   path: "/Users/me/.qi/agent/agents/coordinator/agent.md",
   mcp: 0,
};

const PROJECT_AGENT: AgentInfo = {
   ...USER_AGENT,
   name: "repo-helper",
   source: "project",
   path: "/w/p/.qi/agents/repo-helper/agent.md",
   mcp: 2,
};

describe("agentScopeOf", () => {
   it("只认 qi-agents 的两层(user / project)", () => {
      expect(agentScopeOf("user")).toBe("user");
      expect(agentScopeOf("project")).toBe("project");
   });

   it("别的取值都不是这个宿主管的", () => {
      // `global` 是**技能**那套词汇 —— 有人照它传 scope 会被后端 422,而界面根本不该
      // 给出那个键;`both` 是 discover() 的入参,不是某一层。
      for (const source of ["", "global", "both", "role", "package", "builtin"]) {
         expect(agentScopeOf(source)).toBeNull();
      }
   });
});

describe("agentDeleteBody", () => {
   it("scope 与 source 同一套词汇,名字原样发", () => {
      expect(agentDeleteBody(USER_AGENT)).toEqual({
         scope: "user",
         name: "coordinator",
      });
      expect(agentDeleteBody(PROJECT_AGENT)).toEqual({
         scope: "project",
         name: "repo-helper",
      });
   });

   it("不可删的层返回 null —— 调用方据此不渲染那个键", () => {
      expect(agentDeleteBody({ ...USER_AGENT, source: "builtin" })).toBeNull();
   });

   it("发的是 frontmatter 的显示名(不是目录名、不是 path)", () => {
      // 目录可以叫 weird-dir,读端点给的 name 才是后端 `discover()` 的查找键。
      const odd: AgentInfo = { ...USER_AGENT, name: "nice-name", path: "/x/weird-dir/agent.md" };
      expect(agentDeleteBody(odd)).toEqual({ scope: "user", name: "nice-name" });
   });
});

describe("agentDeleteRows", () => {
   it("层那一行写全落点,且与请求体的 scope 一一对应", () => {
      const rows = agentDeleteRows(USER_AGENT);
      expect(rows.find((row) => row.label === "层")?.value).toContain(
         "~/.qi/agent/agents/",
      );

      const projectRows = agentDeleteRows(PROJECT_AGENT);
      expect(projectRows.find((row) => row.label === "层")?.value).toContain(
         ".qi/agents/",
      );
      // 同源:rows 里的 scope 与 body 里的 scope 是同一个东西(不许一个说用户层、
      // 一个发项目层)
      expect(agentDeleteBody(USER_AGENT)?.scope).toBe("user");
      expect(agentDeleteBody(PROJECT_AGENT)?.scope).toBe("project");
   });

   it("角色名与位置照读端点给的显示", () => {
      const rows = agentDeleteRows(PROJECT_AGENT);
      expect(rows.find((row) => row.label === "角色")?.value).toBe("repo-helper");
      expect(rows.find((row) => row.label === "位置")?.value).toBe(
         PROJECT_AGENT.path,
      );
      expect(rows.map((row) => row.label)).toEqual(["角色", "层", "位置"]);
   });

   it("不可删的层写明原因,path 空时给占位", () => {
      const rows = agentDeleteRows({ ...USER_AGENT, source: "builtin", path: "" });
      expect(rows.find((row) => row.label === "层")?.value).toContain("不是这个宿主管");
      expect(rows.find((row) => row.label === "位置")?.value).toBe("(未知)");
   });
});
