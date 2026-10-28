/**
 * 设置面的 **MCP 一节**:只摆**全局** `mcp.json` 的声明,以及它们的增删改。
 *
 * 项目级 `.qi/mcp.json` 与角色私有的声明都不在这里显示:它们跟着仓库 / 角色目录走,
 * 这个控制台只管一处就能管完的全局层(后端契约仍在,CLI 与其它宿主可以读写它们)。
 *
 * 三条从宿主继承的口径,界面必须照着显示:
 *
 *   1. **值不出宿主**:`env` / `headers` 只给键名,stdio 的 `command` / `args` 连给都不给。
 *      于是表单里那几个框在编辑态是「**留空 = 不改**」—— 看不见的值不能因为保存一次就没了;
 *   2. **写要过确认**:落盘的写一律弹层 + `X-Qi-Confirm: yes`(与写凭证同一条闸,
 *      后端也拦 428,双保险);
 *   3. **老宿主降级**:没有 `/api/mcp`、或 `/api/meta` 里没有 `mcp_write`,这一节就只读 ——
 *      少一节按钮比点了才吃 404 好。
 *
 * 单独成文件是因为 `Settings.tsx` 已经 800 行:列表 + 表单 + 三个确认层都塞进去,
 * 下一个改设置的人得先翻过整页才能碰到 MCP。
 */
import { useMemo, useState } from "react";
import { MCP_TRANSPORTS } from "../api/types";
import type {
   McpList,
   McpPatchBody,
   McpScope,
   McpServerInfo,
   McpServerInput,
   McpSource,
   McpWriteBody,
} from "../api/types";
import { ConfirmDialog } from "./ConfirmDialog";

/** 要动的那处声明表。`server === null` = 新建。 */
export interface McpTarget {
   scope: McpScope;
   role: string;
   path: string;
   server: McpServerInfo | null;
}

/** 来源层的展示名。设置页只摆全局层(角色私有与项目层都不显示)。 */
function scopeLabel(): string {
   return "全局";
}

/**
 * 一个 server 在界面上的**状态**。
 *
 * v1 的“绑定”概念随 agent 配置一起消失了(P-E4c):现在是“声明表 → 谁点名谁用”,
 * 而角色的 MCP 面由 `agent.md` 的 `tools:` 里有没有 mcp 条目决定。所以这里显示的是
 * qi-mcp 真实有的那几个状态位。
 */
function bindLabel(server: McpServerInfo): string {
   const bits: string[] = [];
   if (server.disabled) bits.push("已关闭");
   if (server.direct_tools) bits.push("直连注册");
   if (server.unknown_fields.length > 0)
      bits.push(`未识别字段 ${server.unknown_fields.length} 个`);
   if (bits.length === 0) bits.push("走代理");
   return bits.join(" · ");
}

/** HTTP 家族(都要 url)。与宿主 `_validate_mcp_config` 的判定同一份口径。 */
const HTTP_FAMILY: readonly string[] = ["streamable-http", "sse", "http"];

function isTransport(value: string): boolean {
   return (MCP_TRANSPORTS as readonly string[]).includes(value);
}

// ── 列表 ────────────────────────────────────────────────

export function McpSection({
   mcp,
   missing,
   canWrite,
   onAdd,
   onEdit,
   onDelete,
}: {
   mcp: McpList | null;
   /** 老宿主没这个端点,或 qi-mcp 没装 → 这一节只读/空态。 */
   missing: boolean;
   canWrite: boolean;
   onAdd: (src: McpSource) => void;
   onEdit: (src: McpSource, server: McpServerInfo) => void;
   onDelete: (src: McpSource, server: McpServerInfo) => void;
}) {
   if (missing) {
      return (
         <p className="set-sec__empty">没装 qi-mcp 扩展,读不到 MCP 声明。</p>
      );
   }
   if (mcp === null) return <p className="set-sec__empty">读取中…</p>;

   return (
      <div className="mcp">
         {mcp.sources.map((src) => (
            <div className="mcp-src" key={`${src.scope}:${src.role}`}>
               <div className="mcp-src__head">
                  <span className="set-card__tag">{scopeLabel()}</span>
                  <span className="mcp-src__path">{src.path}</span>
                  {canWrite ? (
                     <span className="set-card__actions">
                        <button
                           type="button"
                           className="set-act"
                           onClick={() => onAdd(src)}
                        >
                           添加
                        </button>
                     </span>
                  ) : null}
               </div>
               {src.servers.length === 0 ? (
                  <p className="set-sec__empty">
                     {src.exists ? "文件在,但没有声明任何 server。" : "这里没有 mcp.json。"}
                  </p>
               ) : (
                  <ul className="set-cards">
                     {src.servers.map((server) => (
                        <li className="set-card" key={server.name}>
                           <div className="set-card__head">
                              <span className="set-card__name" title={server.name}>
                                 {server.name}
                              </span>
                              <span className="set-card__tag">{scopeLabel()}</span>
                              <span className="set-card__actions">
                                 {canWrite ? (
                                    <>
                                       <button
                                          type="button"
                                          className="set-act"
                                          onClick={() => onEdit(src, server)}
                                       >
                                          编辑
                                       </button>
                                       <button
                                          type="button"
                                          className="set-act set-act--danger"
                                          onClick={() => onDelete(src, server)}
                                       >
                                          删除
                                       </button>
                                    </>
                                 ) : null}
                              </span>
                           </div>
                           <div className="set-card__fields">
                              <span className="kv">
                                 <span className="kv__label">传输</span>
                                 <span className="kv__value">
                                    {server.transport || "(未知)"}
                                 </span>
                              </span>
                              <span className="kv">
                                 <span className="kv__label">状态</span>
                                 <span className="kv__value">{bindLabel(server)}</span>
                              </span>
                              <span className="kv">
                                 <span className="kv__label">目标</span>
                                 <span className="kv__value">
                                    {server.transport === "socket"
                                       ? server.socket
                                       : server.url || "(未声明)"}
                                 </span>
                              </span>
                              {server.env_keys.length > 0 ? (
                                 <span className="kv">
                                    <span className="kv__label">env 键名</span>
                                    <span className="kv__value">
                                       {server.env_keys.join(", ")}
                                    </span>
                                 </span>
                              ) : null}
                              {server.header_keys.length > 0 ? (
                                 <span className="kv">
                                    <span className="kv__label">headers 键名</span>
                                    <span className="kv__value">
                                       {server.header_keys.join(", ")}
                                    </span>
                                 </span>
                              ) : null}
                           </div>
                        </li>
                     ))}
                  </ul>
               )}
            </div>
         ))}
      </div>
   );
}

// ── 表单(新建 / 编辑共用)────────────────────────────────

type DirectMode = "off" | "all" | "list";

/**
 * 表单草稿。**读不到的值在编辑态一律留空** —— 空 = 不改(见文件头第 1 条)。
 * 导出是为了让单测直接摆草稿(`McpPanel.test.ts`)—— “留空不改”这条是整个表单里
 * 唯一会**静默丢值**的地方,不能只靠手点。
 */
export interface Draft {
   name: string;
   transport: string;
   url: string;
   command: string;
   /** 每行一个参数 */
   args: string;
   cwd: string;
   socket: string;
   /** 每行 `KEY=VALUE` */
   env: string;
   /** 每行 `键: 值` */
   headers: string;
   disabled: boolean;
   direct: DirectMode;
   /** 逗号或换行分隔 */
   directList: string;
}

export function initialDraft(server: McpServerInfo | null): Draft {
   if (server === null) {
      return {
         name: "",
         transport: "stdio",
         url: "",
         command: "",
         args: "",
         cwd: "",
         socket: "",
         env: "",
         headers: "",
         disabled: false,
         direct: "off",
         directList: "",
      };
   }
   const direct = server.direct_tools;
   return {
      name: server.name,
      transport: server.transport,
      url: server.url,
      // 值读不回来 → 留空即“保持原样”,填了就是“整体替换”
      command: "",
      args: "",
      cwd: "",
      socket: server.socket,
      env: "",
      headers: "",
      disabled: server.disabled,
      direct: Array.isArray(direct) ? "list" : direct ? "all" : "off",
      directList: Array.isArray(direct) ? direct.join(", ") : "",
   };
}

type Built =
   | { ok: true; body: McpWriteBody | McpPatchBody }
   | { ok: false; error: string };

function splitLines(text: string): string[] {
   return text
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
}

/**
 * `每行 键<sep>值` → 对象;**返回字符串 = 报错**。
 *
 * 不抛异常:这个函数每次击键都要跑一遍(见确认键的 disabled),抛了就得在调用方
 * try/catch,而 try/catch 会把“表单没填完”和“真的崩了”混成一件事。
 */
function parsePairs(text: string, sep: string, shape: string): Record<string, string> | string {
   const out: Record<string, string> = {};
   for (const line of splitLines(text)) {
      const at = line.indexOf(sep);
      if (at <= 0) return `每行要写成 \`${shape}\`:${line}`;
      const key = line.slice(0, at).trim();
      if (!key) return `键名不能为空:${line}`;
      out[key] = line.slice(at + sep.length).trim();
   }
   return out;
}

function parseDirectList(text: string): string[] {
   return splitLines(text)
      .flatMap((line) => line.split(","))
      .map((name) => name.trim())
      .filter((name) => name.length > 0);
}

function directValue(draft: Draft, names: string[]): boolean | string[] {
   if (draft.direct === "all") return true;
   if (draft.direct === "list") return names;
   return false;
}

function roleOf(target: McpTarget): { role?: string } {
   return target.role ? { role: target.role } : {};
}

/**
 * 草稿 → 请求体。`ok: false` 时**确认键置灰**并在表单里显示原因。
 *
 * 导出给单测:它就是“哪些键会被送进文件”的唯一真相。
 *
 * 新建与编辑的差别只有一处:新建时载荷字段必须齐(连不上的声明不会报错,它只是
 * 永远不生效);编辑时**只送变过的键** —— 宿主按 `model_fields_set` 判,没送的原样留着。
 */
export function buildMcpBody(target: McpTarget, draft: Draft): Built {
   const name = draft.name.trim();
   if (!name) return { ok: false, error: "名字不能为空" };
   if (draft.name !== name) return { ok: false, error: "名字不能带首尾空白" };
   if (!isTransport(draft.transport)) {
      return { ok: false, error: `不认识的传输「${draft.transport}」,先挑一个传输。` };
   }
   const env = parsePairs(draft.env, "=", "KEY=VALUE");
   if (typeof env === "string") return { ok: false, error: env };
   const headers = parsePairs(draft.headers, ":", "键: 值");
   if (typeof headers === "string") return { ok: false, error: headers };
   const args = splitLines(draft.args);
   const names = parseDirectList(draft.directList);
   if (draft.direct === "list" && names.length === 0) {
      return { ok: false, error: "直连工具列表是空的:写几个工具名,或改选“走代理”。" };
   }
   const direct_tools = directValue(draft, names);

   /** 落盘之后的**有效**目标值(编辑态没填的那个框 = 沿用原值)。 */
   const keep = target.server !== null && draft.transport === target.server.transport;
   const nextUrl = draft.url.trim() || (keep ? target.server?.url ?? "" : "");
   const nextSocket = draft.socket.trim() || (keep ? target.server?.socket ?? "" : "");
   if (HTTP_FAMILY.includes(draft.transport) && !nextUrl)
      return { ok: false, error: `${draft.transport} 必须给 url` };
   if (draft.transport === "socket" && !nextSocket)
      return { ok: false, error: "socket 必须给套接字路径" };
   if (draft.transport === "stdio" && !keep && !draft.command.trim())
      return { ok: false, error: "stdio 必须给命令(command)" };

   if (target.server === null) {
      const server: McpServerInput = {
         name,
         transport: draft.transport,
         url: draft.url.trim(),
         command: draft.command,
         args,
         cwd: draft.cwd,
         socket: draft.socket,
         env,
         headers,
         disabled: draft.disabled,
         direct_tools,
      };
      return { ok: true, body: { scope: target.scope, ...roleOf(target), server } };
   }

   const original = target.server;
   const patch: Partial<McpServerInput> = {};
   if (name !== original.name) patch.name = name;
   if (draft.transport !== original.transport) patch.transport = draft.transport;
   if (draft.url !== original.url) patch.url = draft.url;
   if (draft.socket !== original.socket) patch.socket = draft.socket;
   if (draft.command) patch.command = draft.command;
   if (args.length > 0) patch.args = args;
   if (draft.cwd) patch.cwd = draft.cwd;
   if (Object.keys(env).length > 0) patch.env = env;
   if (Object.keys(headers).length > 0) patch.headers = headers;
   if (draft.disabled !== original.disabled) patch.disabled = draft.disabled;
   if (JSON.stringify(direct_tools) !== JSON.stringify(original.direct_tools))
      patch.direct_tools = direct_tools;

   return {
      ok: true,
      body: { scope: target.scope, ...roleOf(target), name: original.name, patch },
   };
}

/** 新建 / 编辑的确认层(表单在弹层**里面**,确认前就能看见会写什么)。 */
export function McpDialog({
   target,
   error,
   busy = false,
   onSubmit,
   onCancel,
}: {
   target: McpTarget;
   /** 宿主回的错误(名字重复、载荷对不上、428…)。**显示在弹层里** —— 顶上的
    *  notice 被遮罩挡着,弹层不关就看不见。 */
   error?: string | null;
   /** 在写中 → 确认键置灰,免得手抖提交两次(弹层本来也是“成功才关”)。 */
   busy?: boolean;
   onSubmit: (body: McpWriteBody | McpPatchBody) => void;
   onCancel: () => void;
}) {
   const initial = useMemo(() => initialDraft(target.server), [target.server]);
   const [draft, setDraft] = useState<Draft>(initial);
   const built = buildMcpBody(target, draft);
   /** 没动过就别把“名字不能为空”怼在用户脸上 —— 但确认键照样是灰的。 */
   const dirty = JSON.stringify(draft) !== JSON.stringify(initial);
   const set = (patch: Partial<Draft>) => setDraft((prev) => ({ ...prev, ...patch }));
   const creating = target.server === null;
   const transport = draft.transport;
   const transportOptions: string[] = [...MCP_TRANSPORTS];
   // 读端可能认出一个我们列表里没有的传输(老名字 / 写错的 type):选项得**照实给出**,
   // 否则那份声明在界面上根本改不了。
   if (!transportOptions.includes(transport)) transportOptions.push(transport);
   const withCarrier =
      transport === "stdio" || HTTP_FAMILY.includes(transport) || transport === "socket";

   return (
      <ConfirmDialog
         title={
            creating
              ? `在${scopeLabel()}层新建 server?`
              : `改写 ${target.server?.name ?? ""}?`
         }
         rows={[
            { label: "目标文件", value: target.path },
            ...(creating
              ? []
              : [
                   {
                      label: "server",
                      value: `${target.server?.name}(${scopeLabel()})`,
                   },
                ]),
         ]}
         warning={
            creating
               ? "写进 mcp.json(跟着仓库走,可能被提交)。env / headers / args 的值写得进去,但读不回来。"
               : "只改下面填过的框:env / headers / command / args 留空 = 保持原值。"
         }
         confirmLabel={creating ? "确认写入" : "确认修改"}
         confirmDisabled={busy || !built.ok}
         onCancel={onCancel}
         onConfirm={() => {
            if (built.ok) onSubmit(built.body);
         }}
      >
         <div className="mcp-form">
            <label className="mcp-form__row">
               <span className="mcp-form__label">名字</span>
               <input
                  className="dialog__input"
                  value={draft.name}
                  autoFocus
                  aria-label="server 名"
                  placeholder="github"
                  onChange={(event) => set({ name: event.target.value })}
               />
            </label>
            <label className="mcp-form__row">
               <span className="mcp-form__label">传输</span>
               <select
                  className="dialog__select"
                  value={transport}
                  aria-label="传输"
                  onChange={(event) => set({ transport: event.target.value })}
               >
                  {transportOptions.map((value) => (
                     <option key={value} value={value}>
                        {value}
                     </option>
                  ))}
               </select>
            </label>

            {transport === "stdio" ? (
               <>
                  <label className="mcp-form__row">
                     <span className="mcp-form__label">命令</span>
                     <input
                        className="dialog__input"
                        value={draft.command}
                        aria-label="command"
                        placeholder={creating ? "npx" : "(留空 = 保持不变)"}
                        onChange={(event) => set({ command: event.target.value })}
                     />
                  </label>
                  <label className="mcp-form__row mcp-form__row--top">
                     <span className="mcp-form__label">参数(每行一个)</span>
                     <textarea
                        className="dialog__area"
                        value={draft.args}
                        aria-label="args"
                        rows={3}
                        placeholder={creating ? "-y\n@mcp/server" : "(留空 = 保持不变)"}
                        onChange={(event) => set({ args: event.target.value })}
                     />
                  </label>
                  <label className="mcp-form__row">
                     <span className="mcp-form__label">工作目录</span>
                     <input
                        className="dialog__input"
                        value={draft.cwd}
                        aria-label="cwd"
                        placeholder={creating ? "(可空)" : "(留空 = 保持不变)"}
                        onChange={(event) => set({ cwd: event.target.value })}
                     />
                  </label>
               </>
            ) : null}

            {HTTP_FAMILY.includes(transport) ? (
               <label className="mcp-form__row">
                  <span className="mcp-form__label">url</span>
                  <input
                     className="dialog__input"
                     value={draft.url}
                     aria-label="url"
                     placeholder="https://…/mcp"
                     onChange={(event) => set({ url: event.target.value })}
                  />
               </label>
            ) : null}

            {transport === "socket" ? (
               <label className="mcp-form__row">
                  <span className="mcp-form__label">套接字路径</span>
                  <input
                     className="dialog__input"
                     value={draft.socket}
                     aria-label="socket"
                     placeholder="/tmp/mcp.sock"
                     onChange={(event) => set({ socket: event.target.value })}
                  />
               </label>
            ) : null}

            <label className="mcp-form__row mcp-form__row--top">
               <span className="mcp-form__label">env(每行 KEY=VALUE)</span>
               <textarea
                  className="dialog__area"
                  value={draft.env}
                  aria-label="env"
                  rows={3}
                  placeholder={
                     creating ? "GH_TOKEN=ghp_…" : "(留空 = 保持不变;填了就整体替换)"
                  }
                  onChange={(event) => set({ env: event.target.value })}
               />
            </label>
            <label className="mcp-form__row mcp-form__row--top">
               <span className="mcp-form__label">headers(每行 键: 值)</span>
               <textarea
                  className="dialog__area"
                  value={draft.headers}
                  aria-label="headers"
                  rows={2}
                  placeholder={
                     creating ? "Authorization: Bearer …" : "(留空 = 保持不变;填了就整体替换)"
                  }
                  onChange={(event) => set({ headers: event.target.value })}
               />
            </label>

            <label className="mcp-form__row">
               <span className="mcp-form__label">直连工具</span>
               <select
                  className="dialog__select"
                  value={draft.direct}
                  aria-label="directTools"
                  onChange={(event) =>
                     set({ direct: event.target.value as DirectMode })
                  }
               >
                  <option value="off">走代理(默认)</option>
                  <option value="all">直连注册全部</option>
                  <option value="list">直连指定的几个</option>
               </select>
            </label>
            {draft.direct === "list" ? (
               <label className="mcp-form__row mcp-form__row--top">
                  <span className="mcp-form__label">工具名(逗号/换行)</span>
                  <textarea
                     className="dialog__area"
                     value={draft.directList}
                     aria-label="直连工具名"
                     rows={2}
                     placeholder="read, grep"
                     onChange={(event) => set({ directList: event.target.value })}
                  />
               </label>
            ) : null}

            <label className="mcp-form__check">
               <input
                  type="checkbox"
                  checked={draft.disabled}
                  onChange={(event) => set({ disabled: event.target.checked })}
               />
               关掉这个 server(disabled: true,声明仍在、只是不连)
            </label>

            {transport === "sse" ? (
               <p className="mcp-form__hint">
                  提示:sse 这种传输 qi-mcp 认得出来、但**连不上**(尚未实现)。
               </p>
            ) : null}
            {error ? <div className="dialog__error">{error}</div> : null}
            {dirty && !built.ok ? (
               <div className="dialog__error">{built.error}</div>
            ) : null}
            {!withCarrier ? (
               <p className="mcp-form__hint">
                  这个声明不完整:挑一个传输,再填对应的目标(命令 / url / 套接字)。
               </p>
            ) : null}
         </div>
      </ConfirmDialog>
   );
}

/** 删除的确认层(不带表单:这里没有可填的东西,只有“删哪儿、删哪个”)。 */
export function McpDeleteDialog({
   target,
   server,
   error,
   busy = false,
   onConfirm,
   onCancel,
}: {
   target: McpSource;
   server: McpServerInfo;
   /** 删失败的原话(通常是要删的文件正在被别人写)—— 报在弹层里。 */
   error?: string | null;
   busy?: boolean;
   onConfirm: () => void;
   onCancel: () => void;
}) {
   return (
      <ConfirmDialog
         title={`从${scopeLabel()}删掉 ${server.name}?`}
         rows={[
            { label: "server", value: server.name },
            { label: "目标文件", value: target.path },
         ]}
         warning="只删全局层的这一条:项目层如果还有同名声明,运行时仍会生效(那个层不在这里显示)。写错了可从 git 恢复。"
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
