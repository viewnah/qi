/**
 * qi-lab 设置面只管**全局(用户级)资产**。
 *
 * 项目级资产跟着 cwd 仓库走(`<项目>/.qi/…`、`.agents/skills/`),由那个仓库自己的
 * 维护者管;把它们的增删改混进这个 Web 控制台,等于让一次点击作用到"当前打开的项目",
 * 而界面上根本看不出是哪一个。所以 Agent / 技能 / MCP / A2A / 知识库五节统一在
 * **展示层**把项目级那一层过滤掉 —— 后端契约不变(CLI / 其它宿主仍可读写项目层),
 * 这里只是"这台控制台不摆它们"。
 *
 * 过滤口径都写在这个文件里,不散在组件里:`source` / `scope` 的词汇来自三个不同的
 * 扩展(技能六层、qi-agents 两层、qi-mcp 三层),每个模块各写一遍迟早会漏一层。
 */
import type {
   A2AList,
   AgentInfo,
   KbItem,
   KbProvider,
   McpList,
   McpSource,
   SkillInfo,
} from "../api/types";

/** 技能的三个项目级来源(见 `qi_agent.loader.top_level_skill_dirs`)。 */
export const PROJECT_SKILL_SOURCES = [
   "agents-project",
   "qi-project",
   "settings-project",
] as const;

/** 技能:项目级三层的名字就在 `source` 里。 */
export function isGlobalSkill(skill: SkillInfo): boolean {
   return !(PROJECT_SKILL_SOURCES as readonly string[]).includes(skill.source);
}

/** 角色:`source` 的词汇是 qi-agents 的(`user` / `project` / `a2a`)。 */
export function isGlobalAgent(agent: AgentInfo): boolean {
   return agent.source !== "project";
}

/** `agent.md` 的父目录名(= 角色目录名,也是 MCP 角色层里的 `role`)。 */
function roleDirOf(agentPath: string): string {
   const parts = agentPath.split(/[\\/]+/).filter(Boolean);
   return parts.length >= 2 ? parts[parts.length - 2] : "";
}

/**
 * 项目级角色占用的目录名集合。MCP 的 `role` 层只给角色目录名、不给 `source`,
 * 所以用它来认出"这一份私有声明属于项目级角色"。
 */
export function projectRoleDirs(agents: readonly AgentInfo[]): Set<string> {
   return new Set(
      agents.filter((agent) => agent.source === "project").map((agent) => roleDirOf(agent.path)),
   );
}

/** MCP:项目层整层不要;角色私有层只留全局角色的。 */
export function isGlobalMcpSource(
   source: McpSource,
   projectRoles: ReadonlySet<string> = new Set(),
): boolean {
   if (source.scope === "project") return false;
   if (source.scope === "role") return !projectRoles.has(source.role);
   return true;
}

/** 过滤后的 MCP 清单(`null` 原样传回,调用方不用多一个判空分支)。 */
export function globalMcpList(
   mcp: McpList | null,
   agents: readonly AgentInfo[],
): McpList | null {
   if (mcp === null) return null;
   const roles = projectRoleDirs(agents);
   return { ...mcp, sources: mcp.sources.filter((item) => isGlobalMcpSource(item, roles)) };
}

/**
 * A2A:项目层整层不要。`resolved` 也去掉带项目层的条目 —— 那是"项目写了什么"的
 * 合并结果,这个控制台不展示它。
 */
export function globalA2AList(a2a: A2AList | null): A2AList | null {
   if (a2a === null) return null;
   return {
      ...a2a,
      sources: a2a.sources.filter((source) => source.scope !== "project"),
      resolved: a2a.resolved.filter((entry) => !entry.layers.includes("project")),
   };
}

/** 知识库:provider 与已绑定的 KB 都只留全局层。 */
export function globalKbProviders(providers: readonly KbProvider[]): KbProvider[] {
   return providers.filter((provider) => provider.scope !== "project");
}

export function globalKbItems(items: readonly KbItem[]): KbItem[] {
   return items.filter((item) => item.scope !== "project");
}
