/**
 * 角色删除的那一层确认,以及它背后的三个纯函数。
 *
 * 单独成文件的理由与 `McpPanel` 一样:凡是会落盘的动作都要过一道**可见**的确认
 * (design/web.md §8.3),而"确认层里显示的东西"必须与"请求里发出去的东西"同源 ——
 * 说的和发的对不上,这道闸就是摆设。同源线就是下面三个函数,`AgentDelete.test.ts` 钉住它。
 *
 * 三条从宿主继承的口径(见 `qi_lab/agents.py` 的模块说明):
 *
 *   1. **词汇是 qi-agents 的**:`scope` 取 `user` / `project`,与读模型 `AgentInfo.source`
 *      同一套 —— 界面显示什么词,请求就发什么词(技能那套 `global` 在这里 422);
 *   2. **只删一层**:项目层同名覆盖用户层,删 `user` 那份之后 `project` 的会*露出来*;
 *   3. **整目录**:角色目录里还带着自己的 `mcp.json`,确认层把这条写进 warning。
 *
 * 降级不归这里管:没装 qi-agents、或宿主没有 `agent_write` 能力位时,`Settings`
 * 压根不画删除键(节是只读的)—— 少一个键比点了才吃 404/428 好。
 */
import type { AgentDeleteBody, AgentInfo } from "../api/types";
import { ConfirmDialog } from "./ConfirmDialog";
import type { ConfirmRow } from "./ConfirmDialog";

/** 可删的两层(词汇 = qi-agents 的 `source`)。别的取值一律不是这个宿主管的。 */
export type AgentScope = "user" | "project";

/** 读模型的 `source` → 请求体的 `scope`;不是这两层就返回 null(不可删)。 */
export function agentScopeOf(source: string): AgentScope | null {
   return source === "user" || source === "project" ? source : null;
}

/**
 * `DELETE /api/agents` 的请求体。不可删的角色返回 `null` —— 调用方(按钮)据此
 * 不渲染那个键,而不是渲染了再让后端 422。
 */
export function agentDeleteBody(
   agent: Pick<AgentInfo, "name" | "source">,
): AgentDeleteBody | null {
   const scope = agentScopeOf(agent.source);
   return scope === null ? null : { scope, name: agent.name };
}

/** 层的展示文案。确认层要让人一眼看出"删的是哪一层"—— 落点路径写全。 */
const LAYER_LABEL: Record<AgentScope, string> = {
   user: "用户 ~/.qi/agent/agents/",
   project: "项目 <项目>/.qi/agents/",
};

/**
 * 确认层的行 —— **与请求体同一个函数族**:名字与层都从同一个 `source` 推出来,
 * 所以不可能出现"弹层说删用户层、请求删了项目层"。
 */
export function agentDeleteRows(agent: AgentInfo): ConfirmRow[] {
   const scope = agentScopeOf(agent.source);
   return [
      { label: "角色", value: agent.name },
      { label: "层", value: scope ? LAYER_LABEL[scope] : "(不是这个宿主管的层)" },
      { label: "位置", value: agent.path || "(未知)" },
   ];
}

/** 删除的确认层(不带表单:这里没有可填的东西,只有"删哪个角色、删哪一层")。 */
export function AgentDeleteDialog({
   agent,
   error,
   busy = false,
   onConfirm,
   onCancel,
}: {
   agent: AgentInfo;
   /** 删失败的原话(404 = 别的层那个还在;400 = 目标是越界的符号链接)—— 报在弹层里。 */
   error?: string | null;
   busy?: boolean;
   onConfirm: () => void;
   onCancel: () => void;
}) {
   return (
      <ConfirmDialog
         title={`删除角色 ${agent.name}?`}
         rows={agentDeleteRows(agent)}
         warning="会连同整个角色目录一起删除(含它自己的 mcp.json 与其它文件),不可撤销。"
         danger
         confirmLabel="确认删除"
         confirmDisabled={busy}
         onCancel={onCancel}
         onConfirm={onConfirm}
      >
         {error ? <div className="dialog__error">{error}</div> : null}
      </ConfirmDialog>
   );
}
