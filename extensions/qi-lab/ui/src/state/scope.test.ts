/**
 * 设置面"只留全局资产"的过滤口径。
 *
 * 这些函数没有 UI 依赖,但**一条漏掉就会把项目级资产漏回设置页**,所以按仓里
 * 既有规矩(只测纯函数、不引 jsdom)直接钉住每一层的来源词汇。
 */
import { describe, expect, it } from "vitest";

import type {
   A2AEntryInfo,
   A2AList,
   A2ASource,
   AgentInfo,
   KbItem,
   KbProvider,
   McpList,
   McpServerInfo,
   McpSource,
   SkillInfo,
} from "../api/types";
import {
   globalA2AList,
   globalKbItems,
   globalKbProviders,
   globalMcpList,
   isGlobalAgent,
   isGlobalSkill,
   projectRoleDirs,
} from "./scope";

function agent(over: Partial<AgentInfo> & { name: string; source: string }): AgentInfo {
   return {
      description: "",
      tools: [],
      model: "",
      path: `/home/u/.qi/agent/agents/${over.name}/agent.md`,
      mcp: 0,
      ...over,
   };
}

function skill(over: Partial<SkillInfo> & { name: string; source: string }): SkillInfo {
   return { description: "", path: `/skills/${over.name}/SKILL.md`, ...over };
}

const SERVER: McpServerInfo = {
   name: "docs",
   transport: "stdio",
   url: "",
   socket: "",
   env_keys: [],
   header_keys: [],
   disabled: false,
   direct_tools: false,
   unknown_fields: [],
   source: "global",
};

function mcpSource(over: Partial<McpSource> & { scope: McpSource["scope"] }): McpSource {
   return { role: "", path: "/tmp/mcp.json", exists: true, servers: [SERVER], ...over };
}

function entry(over: Partial<A2AEntryInfo> & { name: string }): A2AEntryInfo {
   return {
      card_url: "",
      base_url: "",
      rpc_url: "",
      tenant: "",
      description: "",
      header_keys: [],
      timeout_ms: 600_000,
      streaming: true,
      discovery: "auto",
      direct_tools: false,
      tool_name: "",
      disabled: false,
      unknown_fields: [],
      source: "global",
      layers: ["global"],
      ...over,
   };
}

function a2aSource(scope: A2ASource["scope"], entries: A2AEntryInfo[]): A2ASource {
   return { scope, path: `/a2a/${scope}.json`, exists: true, entries };
}

function kbProvider(over: Partial<KbProvider> & { name: string; scope: string }): KbProvider {
   return {
      baseUrl: "http://kb",
      apiPrefix: "/api",
      instance: "kb",
      login: "",
      role: "",
      loggedIn: true,
      expiresAt: null,
      expiresIn: null,
      kbCount: 0,
      ...over,
   };
}

function kbItem(over: Partial<KbItem> & { id: string; scope?: string }): KbItem {
   return {
      provider: "alice",
      name: over.id,
      description: "",
      embeddingModel: "",
      visibility: "private",
      docCount: 0,
      chunkCount: 0,
      updatedAt: "",
      icon: "",
      bound: true,
      boundProvider: "alice",
      ...over,
   };
}

describe("Agent", () => {
   it("只留下 user 与远端 a2a,项目级角色不出现", () => {
      expect(isGlobalAgent(agent({ name: "u", source: "user" }))).toBe(true);
      expect(isGlobalAgent(agent({ name: "r", source: "a2a" }))).toBe(true);
      expect(isGlobalAgent(agent({ name: "p", source: "project" }))).toBe(false);
   });
});

describe("技能", () => {
   it("六个来源里只认三个全局层,settings-global 不算项目", () => {
      for (const source of ["agents-global", "qi-global", "settings-global"]) {
         expect(isGlobalSkill(skill({ name: "s", source })), source).toBe(true);
      }
      for (const source of ["agents-project", "qi-project", "settings-project"]) {
         expect(isGlobalSkill(skill({ name: "s", source })), source).toBe(false);
      }
   });
});

describe("MCP", () => {
   it("项目层整层去掉,全局层与角色私有层保留", () => {
      const list: McpList = {
         unavailable: false,
         sources: [
            mcpSource({ scope: "global" }),
            mcpSource({ scope: "project" }),
            mcpSource({ scope: "role", role: "writer" }),
         ],
      };

      const filtered = globalMcpList(list, []);

      expect(filtered?.sources.map((source) => source.scope)).toEqual(["global", "role"]);
   });

   it("项目级角色的私有声明一起去掉(role 层不带 source,靠目录名认)", () => {
      const agents = [
         agent({ name: "writer", source: "user",
                 path: "C:\\Users\\me\\.qi\\agent\\agents\\writer\\agent.md" }),
         agent({ name: "repo", source: "project",
                 path: "C:\\w\\repo\\.qi\\agents\\repo-helper\\agent.md" }),
      ];
      const list: McpList = {
         unavailable: false,
         sources: [
            mcpSource({ scope: "role", role: "writer" }),
            mcpSource({ scope: "role", role: "repo-helper" }),
         ],
      };

      expect([...projectRoleDirs(agents)]).toEqual(["repo-helper"]);
      expect(globalMcpList(list, agents)?.sources.map((s) => s.role)).toEqual(["writer"]);
   });

   it("读不到清单时原样返回 null", () => {
      expect(globalMcpList(null, [])).toBeNull();
   });
});

describe("A2A", () => {
   it("项目层与原样条目去掉,合并自项目层的生效条目也不留", () => {
      const list: A2AList = {
         unavailable: false,
         sources: [
            a2aSource("global", [entry({ name: "g" })]),
            a2aSource("project", [entry({ name: "p", source: "project", layers: ["project"] })]),
         ],
         resolved: [
            entry({ name: "g" }),
            entry({ name: "p", source: "project", layers: ["project"] }),
            entry({ name: "mixed", source: "global", layers: ["global", "project"] }),
         ],
      };

      const filtered = globalA2AList(list);

      expect(filtered?.sources.map((source) => source.scope)).toEqual(["global"]);
      expect(filtered?.resolved.map((item) => item.name)).toEqual(["g"]);
   });

   it("读不到清单时原样返回 null", () => {
      expect(globalA2AList(null)).toBeNull();
   });
});

describe("知识库", () => {
   it("provider 与已绑定的 KB 都只留全局层", () => {
      expect(
         globalKbProviders([
            kbProvider({ name: "alice", scope: "global" }),
            kbProvider({ name: "bob", scope: "project" }),
         ]).map((provider) => provider.name),
      ).toEqual(["alice"]);

      expect(
         globalKbItems([
            kbItem({ id: "g", scope: "global" }),
            kbItem({ id: "p", scope: "project" }),
            kbItem({ id: "unbound", scope: undefined }),
         ]).map((item) => item.id),
      ).toEqual(["g", "unbound"]);
   });
});
