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

/**
 * 这个技能能不能在本页删,能删的话落在哪个 scope。
 *
 * 只有 `~/.qi/agent/skills`(`source="qi-global"`)归 qi 管 —— 跨工具层
 * (`~/.agents/skills`)是别的工具的家,settings 的 `skills[]` 是用户手指定的路径
 * (可能 glob 到仓库外),后端两个写 scope 都不收它们。所以这里也只给 `qi-global`
 * 发删除键。
 */
export function skillDeleteScope(skill: SkillInfo): "global" | null {
   return skill.source === "qi-global" ? "global" : null;
}

/** 角色:`source` 的词汇是 qi-agents 的(`builtin` / `user` / `project` / `a2a`)。 */
export function isGlobalAgent(agent: AgentInfo): boolean {
   return agent.source !== "project";
}

/**
 * MCP:只留**全局**层。
 *
 * 角色私有(`scope="role"`)看似“属于哪个角色”,实际上仍是一份跟着角色目录走的
 * 声明 —— 角色可能来自项目,而且同一份会出现在多个角色卡下。这个控制台只摆
 * 一处就能管完的全局声明,所以角色层与项目层一起不显示。
 */
export function isGlobalMcpSource(source: McpSource): boolean {
   return source.scope === "global";
}

/** 过滤后的 MCP 清单(`null` 原样传回,调用方不用多一个判空分支)。 */
export function globalMcpList(mcp: McpList | null): McpList | null {
   if (mcp === null) return null;
   return { ...mcp, sources: mcp.sources.filter(isGlobalMcpSource) };
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
