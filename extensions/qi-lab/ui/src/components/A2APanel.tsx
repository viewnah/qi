/**
 * A2A 条目面板(设置页的一节,与 `McpPanel` 同形但有自己的两件东西)。
 *
 * 与 MCP 的差别,决定这里为什么不是直接复用:
 *
 * 1. **只摆全局层**。a2a.json 磁盘上仍是两层,但项目层跟着仓库走、不归这个控制台
 *    管(由 `state/scope.ts` 过滤)。所以这里只有一块“全局层”,不再画深合并后的
 *    “生效条目” —— 那是“项目层写了什么”的答案,不是这一节要回答的问题。
 * 2. **探测**。配置对不对只有真去拉一次名片才知道,所以每个条目带一个「探测」,
 *    结果就地展开(不弹层:探测是只读的,弹层会挡着列表)。
 */
import { useMemo, useState } from "react";

import type {
   A2AEntryInfo,
   A2AEntryInput,
   A2AList,
   A2AProbeResult,
   A2AScope,
   A2ASource,
} from "../api/types";
import { ConfirmDialog } from "./ConfirmDialog";

function scopeLabel(scope: string): string {
   return scope === "global" ? "全局" : "项目";
}

/** 条目的**端点**那一行:三种写法里写了哪个就给哪个(它们是结构,不是密钥)。 */
function endpointOf(entry: A2AEntryInfo): { label: string; value: string } {
   if (entry.card_url) return { label: "card", value: entry.card_url };
   if (entry.base_url) return { label: "base", value: entry.base_url };
   if (entry.rpc_url) return { label: "rpc", value: entry.rpc_url };
   return { label: "端点", value: "(未声明)" };
}

/** 这个条目"长什么样"的一句话(卡片标题右侧的灰色注解)。 */
function behaviorOf(entry: A2AEntryInfo): string {
   const bits = [`${Math.round(entry.timeout_ms / 1000)}s`];
   bits.push(entry.streaming ? "流式" : "非流式");
   if (entry.discovery !== "auto") bits.push(`discovery=${entry.discovery}`);
   return bits.join(" · ");
}

export function A2ASection({
   a2a,
   missing,
   canWrite,
   onAdd,
   onEdit,
   onDelete,
   onProbe,
}: {
   a2a: A2AList | null;
   /** 没装 qi-a2a → 空态说明,不把整页打成错误屏。 */
   missing: boolean;
   canWrite: boolean;
   onAdd: (src: A2ASource) => void;
   onEdit: (src: A2ASource, entry: A2AEntryInfo) => void;
   onDelete: (src: A2ASource, entry: A2AEntryInfo) => void;
   /** 探测一个条目。**失败也是正常返回**(`ok=false` + `error`)。 */
   onProbe: (name: string) => Promise<A2AProbeResult>;
}) {
   /** 探测结果按条目名存;`null` = 这个条目还没探测过。 */
   const [probes, setProbes] = useState<Record<string, A2AProbeResult>>({});
   const [probing, setProbing] = useState<string | null>(null);

   const runProbe = async (name: string) => {
      setProbing(name);
      try {
         const result = await onProbe(name);
         setProbes((prev) => ({ ...prev, [name]: result }));
      } finally {
         setProbing(null);
      }
   };

   if (missing) {
      return (
         <p className="set-sec__empty">
            没装 qi-a2a 扩展,读不到 A2A 条目。装它:
            <code>qi install ./extensions/qi-a2a</code>
         </p>
      );
   }
   if (a2a === null) return <p className="set-sec__empty">读取中…</p>;

   /** 设置页只管全局层(项目层已被 `state/scope.ts` 过滤)。 */
   const source = a2a.sources.find((item) => item.scope === "global") ?? null;

   const renderCard = (entry: A2AEntryInfo, scope: A2AScope) => {
      const endpoint = endpointOf(entry);
      const probe = probes[entry.name];
      const flags: React.ReactNode[] = [];
      if (entry.tenant) {
         flags.push(
            <span className="set-card__tag" key="tenant">
               tenant {entry.tenant}
            </span>,
         );
      }
      if (entry.disabled) {
         flags.push(
            <span className="set-card__tag set-card__tag--off" key="off">
               已关闭
            </span>,
         );
      }
      if (entry.tool_name) {
         flags.push(
            <span className="set-card__tag set-card__tag--on" key="direct">
               直连 {entry.tool_name}
            </span>,
         );
      }
      if (entry.unknown_fields.length > 0) {
         flags.push(
            <span className="set-card__tag set-card__tag--warn" key="unknown">
               未识别 {entry.unknown_fields.join(", ")}
            </span>,
         );
      }

      return (
         <li
            className={`set-card${entry.disabled ? " set-card--off" : ""}`}
            key={`${scope}:${entry.name}`}
         >
            <div className="set-card__head">
               <span className="set-card__name">{entry.name}</span>
               {flags}
               <span className="set-card__actions">
                  <button
                     type="button"
                     className="set-act"
                     disabled={probing === entry.name}
                     onClick={() => void runProbe(entry.name)}
                  >
                     {probing === entry.name ? "探测中…" : "探测"}
                  </button>
                  {canWrite && source ? (
                     <>
                        <button
                           type="button"
                           className="set-act"
                           onClick={() => onEdit(source, entry)}
                        >
                           编辑
                        </button>
                        <button
                           type="button"
                           className="set-act set-act--danger"
                           onClick={() => onDelete(source, entry)}
                        >
                           删除
                        </button>
                     </>
                  ) : null}
               </span>
            </div>
            {entry.description ? (
               <p className="set-card__desc">{entry.description}</p>
            ) : null}
            <div className="set-card__fields">
               <span className="kv">
                  <span className="kv__label">{endpoint.label}</span>
                  <span className="kv__value">{endpoint.value}</span>
               </span>
               <span className="kv">
                  <span className="kv__label">行为</span>
                  <span className="kv__value">{behaviorOf(entry)}</span>
               </span>
               {entry.header_keys.length > 0 ? (
                  <span className="kv">
                     <span className="kv__label">headers 键名</span>
                     <span className="kv__value">{entry.header_keys.join(", ")}</span>
                  </span>
               ) : null}
            </div>
            {probe ? <ProbeResult result={probe} /> : null}
         </li>
      );
   };

   if (source === null) {
      return <p className="set-sec__empty">没有读到全局 a2a.json。</p>;
   }

   return (
      <div className="a2a">
         <div className="a2a-block">
            <div className="a2a-block__head">
               <span className="a2a-block__title">全局层</span>
               <span className="a2a-block__path" title={source.path}>
                  {source.path}
               </span>
               {!source.exists ? (
                  <span className="set-card__tag">文件不存在</span>
               ) : null}
               {canWrite ? (
                  <span className="set-card__actions">
                     <button
                        type="button"
                        className="set-act"
                        onClick={() => onAdd(source)}
                     >
                        添加
                     </button>
                  </span>
               ) : null}
            </div>
            {source.entries.length === 0 ? (
               <p className="set-sec__empty">
                  {source.exists ? "文件在,但没有条目。" : "这里没有 a2a.json。"}
               </p>
            ) : (
               <ul className="set-cards">
                  {source.entries.map((entry) => renderCard(entry, source.scope))}
               </ul>
            )}
         </div>
      </div>
   );
}

/** 探测结果:成功给卡片(名字 / tenant / 端点 / 技能),失败给那句话。 */
function ProbeResult({ result }: { result: A2AProbeResult }) {
   if (!result.ok) {
      return (
         <div className="a2a-probe a2a-probe--bad">
            <div className="a2a-probe__head">
               <span className="a2a-probe__status">探测失败</span>
            </div>
            <p className="a2a-probe__error">{result.error}</p>
         </div>
      );
   }
   return (
      <div className="a2a-probe">
         <div className="a2a-probe__head">
            <span className="a2a-probe__status a2a-probe__status--ok">
               探测成功 · {result.elapsed_ms}ms · {result.cards.length} 张卡
            </span>
            <span className="a2a-probe__url">
               {result.discovery_url
                  ? `GET ${result.discovery_url}`
                  : "未走发现(用配置合成卡)"}
            </span>
         </div>
         <ul className="a2a-probe__cards">
            {result.cards.map((card) => (
               <li className="a2a-probe__card" key={card.tenant || card.name}>
                  <div className="a2a-probe__card-head">
                     <span className="a2a-probe__name">{card.name}</span>
                     {card.tenant ? (
                        <span className="set-card__tag">tenant {card.tenant}</span>
                     ) : null}
                  </div>
                  <div className="a2a-probe__rpc">{card.rpc_url}</div>
                  {card.skills.length > 0 ? (
                     <div className="a2a-probe__skills">
                        {card.skills.map((skill) => (
                           <span className="a2a-probe__skill" key={skill.id || skill.name}>
                              {skill.name || skill.id}
                           </span>
                        ))}
                     </div>
                  ) : null}
               </li>
            ))}
         </ul>
      </div>
   );
}

// ── 表单 ────────────────────────────────────────────────

/**
 * 表单的可编辑状态。`headers` 用**多行文本**而不是对象:它是"只写不回显"的字段,
 * 读端给不出值,所以只能表达"留空 = 保持 / 填了就整体替换 / 填 `-` = 清空"。
 */
export interface A2ADraft {
   name: string;
   card_url: string;
   base_url: string;
   rpc_url: string;
   tenant: string;
   description: string;
   headers: string;
   timeout_ms: string;
   discovery: string;
   streaming: "" | "true" | "false";
   direct_tools: boolean;
   tool_prefix: string;
   disabled: boolean;
}

export function initialDraft(entry: A2AEntryInfo | null): A2ADraft {
   if (!entry) {
      return {
         name: "",
         card_url: "",
         base_url: "",
         rpc_url: "",
         tenant: "",
         description: "",
         headers: "",
         timeout_ms: "",
         discovery: "",
         streaming: "",
         direct_tools: false,
         tool_prefix: "",
         disabled: false,
      };
   }
   return {
      name: entry.name,
      card_url: entry.card_url,
      base_url: entry.base_url,
      rpc_url: entry.rpc_url,
      tenant: entry.tenant,
      description: entry.description,
      headers: "",
      timeout_ms: String(entry.timeout_ms),
      discovery: entry.discovery === "auto" ? "" : entry.discovery,
      streaming: entry.streaming ? "true" : "false",
      direct_tools: Boolean(entry.direct_tools),
      tool_prefix: "",
      disabled: entry.disabled,
   };
}

/** 表单能提交的两种体:新建送整个条目,改只送动过的键。 */
type Built =
   | { ok: true; mode: "create"; entry: A2AEntryInput }
   | { ok: true; mode: "patch"; patch: Partial<A2AEntryInput> }
   | { ok: false; reason: string };

/** `KEY: VALUE` 每行一条。空行与 `#` 注释跳过。 */
function parseHeaders(text: string): Record<string, string> | string {
   const out: Record<string, string> = {};
   for (const raw of text.split("\n")) {
      const line = raw.trim();
      if (!line || line.startsWith("#")) continue;
      const index = line.indexOf(":");
      if (index <= 0) return `headers 里这行少了冒号:${line}`;
      out[line.slice(0, index).trim()] = line.slice(index + 1).trim();
   }
   return out;
}

export function buildA2ABody(
   name: string | null,
   draft: A2ADraft,
): Built {
   const trimmedName = draft.name.trim();
   if (!trimmedName) return { ok: false, reason: "条目名不能为空" };
   if (draft.name !== trimmedName) {
      return { ok: false, reason: "条目名不能带首尾空白" };
   }

   // 端点三个字段是空串 = 不写(而不是写一个空值:那会让"未声明"与"被改没了"分不清)。
   const fields: Partial<A2AEntryInput> = {
      name: trimmedName,
      card_url: draft.card_url.trim(),
      base_url: draft.base_url.trim(),
      rpc_url: draft.rpc_url.trim(),
      tenant: draft.tenant.trim(),
      description: draft.description.trim(),
      discovery: draft.discovery,
      tool_prefix: draft.tool_prefix.trim(),
   };

   const timeout = draft.timeout_ms.trim();
   if (timeout) {
      const value = Number(timeout);
      if (!Number.isFinite(value) || value <= 0 || !Number.isInteger(value)) {
         return { ok: false, reason: "timeoutMs 必须是正整数" };
      }
      fields.timeout_ms = value;
   } else if (name === null) {
      // 新建时不送这个键,让 qi-a2a 用它的默认值(默认值只有一份,不在这里抄)。
      delete fields.timeout_ms;
   } else {
      fields.timeout_ms = null; // PATCH 的 null = 不改
   }

   fields.streaming =
      draft.streaming === "" ? null : draft.streaming === "true";
   fields.direct_tools = draft.direct_tools;
   fields.disabled = draft.disabled;

   // `headers` 只在**填了东西**时才进请求:读端给不出值,留空必须是"保持"。
   const rawHeaders = draft.headers.trim();
   if (rawHeaders === "-") {
      fields.headers = {};
   } else if (rawHeaders) {
      const parsed = parseHeaders(draft.headers);
      if (typeof parsed === "string") return { ok: false, reason: parsed };
      fields.headers = parsed;
   }

   if (name === null) {
      const entry: Record<string, unknown> = { name: trimmedName };
      for (const [key, value] of Object.entries(fields)) {
         if (key === "name") continue;
         if (value === null || value === "") continue;
         entry[key] = value;
      }
      if (fields.headers) entry.headers = fields.headers;
      entry.disabled = draft.disabled;
      entry.direct_tools = draft.direct_tools;
      return { ok: true, mode: "create", entry: entry as unknown as A2AEntryInput };
   }

   // PATCH:只送"有意改过"的键。布尔与枚举总是送(取消勾选要真的把键删掉),
   // 字符串只在非空时送(空串在后端表示删键,但界面里它更常是"没填")。
   const patch: Partial<A2AEntryInput> = {};
   for (const key of ["card_url", "base_url", "rpc_url", "tenant",
                      "description", "discovery", "tool_prefix"] as const) {
      if (fields[key]) (patch as Record<string, unknown>)[key] = fields[key];
   }
   if (timeout) patch.timeout_ms = Number(timeout);
   if (fields.streaming !== null) patch.streaming = fields.streaming;
   patch.direct_tools = draft.direct_tools;
   patch.disabled = draft.disabled;
   if (fields.headers) patch.headers = fields.headers;
   if (trimmedName !== name) patch.name = trimmedName;
   return { ok: true, mode: "patch", patch };
}

export function A2ADialog({
   target,
   error,
   busy = false,
   onSubmit,
   onCancel,
}: {
   target: { scope: A2AScope; path: string; entry: A2AEntryInfo | null };
   error?: string | null;
   busy?: boolean;
   onSubmit: (body: A2AEntryInput | Partial<A2AEntryInput>) => void;
   onCancel: () => void;
}) {
   const initial = useMemo(() => initialDraft(target.entry), [target.entry]);
   const [draft, setDraft] = useState<A2ADraft>(initial);
   const built = buildA2ABody(target.entry?.name ?? null, draft);
   const creating = target.entry === null;
   const set = (patch: Partial<A2ADraft>) =>
      setDraft((prev) => ({ ...prev, ...patch }));

   return (
      <ConfirmDialog
         title={creating ? `在${scopeLabel(target.scope)}层新建条目?` : `改写 ${target.entry?.name ?? ""}?`}
         rows={[
            { label: "目标文件", value: target.path },
            {
               label: "条目",
               value: creating ? "(新建)" : `${target.entry?.name}(${scopeLabel(target.scope)})`,
            },
         ]}
         warning={
            creating
               ? "写进 a2a.json。headers 的值写得进去,但读不回来。"
               : "只改下面填过的框:headers 留空 = 保持原值,填 - = 清空整张表。"
         }
         confirmLabel={creating ? "确认写入" : "确认修改"}
         confirmDisabled={busy || !built.ok}
         onCancel={onCancel}
         onConfirm={() => {
            if (!built.ok) return;
            onSubmit(built.mode === "create" ? built.entry : built.patch);
         }}
      >
         <div className="a2a-form">
            <label className="a2a-form__row">
               <span className="a2a-form__label">条目名</span>
               <input
                  value={draft.name}
                  onChange={(event) => set({ name: event.target.value })}
                  placeholder="hikqin-analysis"
                  autoFocus
               />
            </label>
            <label className="a2a-form__row">
               <span className="a2a-form__label">tenant</span>
               <input
                  value={draft.tenant}
                  onChange={(event) => set({ tenant: event.target.value })}
                  placeholder="agent-4c4af5bf"
               />
               <span className="a2a-form__hint">固定到某个智能体;网关条目留空</span>
            </label>
            <label className="a2a-form__row">
               <span className="a2a-form__label">cardUrl</span>
               <input
                  value={draft.card_url}
                  onChange={(event) => set({ card_url: event.target.value })}
                  placeholder="http://host:8080/a2a/agents/agent-xxx/card.json"
               />
               <span className="a2a-form__hint">
                  给这个就够:端点与 tenant 都从名片读
               </span>
            </label>
            <label className="a2a-form__row">
               <span className="a2a-form__label">baseUrl</span>
               <input
                  value={draft.base_url}
                  onChange={(event) => set({ base_url: event.target.value })}
                  placeholder="http://host:8080"
               />
               <span className="a2a-form__hint">推导 /a2a 与 /a2a/agents</span>
            </label>
            <label className="a2a-form__row">
               <span className="a2a-form__label">rpcUrl</span>
               <input
                  value={draft.rpc_url}
                  onChange={(event) => set({ rpc_url: event.target.value })}
                  placeholder="http://host:8080/a2a"
               />
               <span className="a2a-form__hint">与 tenant 一起用可省掉名片发现</span>
            </label>
            <label className="a2a-form__row">
               <span className="a2a-form__label">description</span>
               <input
                  value={draft.description}
                  onChange={(event) => set({ description: event.target.value })}
                  placeholder="给人看的说明(不进系统提示词)"
               />
            </label>
            <label className="a2a-form__row a2a-form__row--top">
               <span className="a2a-form__label">headers</span>
               <textarea
                  value={draft.headers}
                  onChange={(event) => set({ headers: event.target.value })}
                  placeholder="Authorization: Bearer ${HIKQIN_A2A_TOKEN}"
                  rows={3}
               />
               <span className="a2a-form__hint">
                  {creating
                     ? "每行一条 键: 值;值支持 ${ENV}。只写、不回显。"
                     : "留空 = 保持不变;填了就整体替换;填 - 清空整张表。"}
               </span>
            </label>
            <label className="a2a-form__row">
               <span className="a2a-form__label">timeoutMs</span>
               <input
                  value={draft.timeout_ms}
                  onChange={(event) => set({ timeout_ms: event.target.value })}
                  placeholder="600000"
                  inputMode="numeric"
               />
            </label>
            <label className="a2a-form__row">
               <span className="a2a-form__label">discovery</span>
               <select
                  value={draft.discovery}
                  onChange={(event) => set({ discovery: event.target.value })}
               >
                  <option value="">默认(auto)</option>
                  <option value="single">single(只认单张卡)</option>
                  <option value="list">list(网关,数组)</option>
               </select>
            </label>
            <label className="a2a-form__row">
               <span className="a2a-form__label">streaming</span>
               <select
                  value={draft.streaming}
                  onChange={(event) =>
                     set({ streaming: event.target.value as A2ADraft["streaming"] })
                  }
               >
                  <option value="">默认(开)</option>
                  <option value="true">开</option>
                  <option value="false">关</option>
               </select>
               <span className="a2a-form__hint">名片没说能力时才由它决定</span>
            </label>
            <label className="a2a-form__row">
               <span className="a2a-form__label">toolPrefix</span>
               <input
                  value={draft.tool_prefix}
                  onChange={(event) => set({ tool_prefix: event.target.value })}
                  placeholder="a2a"
               />
            </label>
            <label className="a2a-form__check">
               <input
                  type="checkbox"
                  checked={draft.direct_tools}
                  onChange={(event) => set({ direct_tools: event.target.checked })}
               />
               为这个条目注册独立工具(directTools)
            </label>
            <label className="a2a-form__check">
               <input
                  type="checkbox"
                  checked={draft.disabled}
                  onChange={(event) => set({ disabled: event.target.checked })}
               />
               关掉它,但保留声明
            </label>
            {!built.ok ? (
               <p className="a2a-form__error">{built.reason}</p>
            ) : null}
            {error ? <div className="dialog__error">{error}</div> : null}
         </div>
      </ConfirmDialog>
   );
}

export function A2ADeleteDialog({
   target,
   entry,
   error,
   busy = false,
   onConfirm,
   onCancel,
}: {
   target: A2ASource;
   entry: A2AEntryInfo;
   error?: string | null;
   busy?: boolean;
   onConfirm: () => void;
   onCancel: () => void;
}) {
   return (
      <ConfirmDialog
         title={`从${scopeLabel(target.scope)}层删掉 ${entry.name}?`}
         rows={[
            { label: "条目", value: entry.name },
            { label: "目标文件", value: target.path },
         ]}
         warning="只删这一层的条目:同名的下层声明会露出来(深合并关系不变)。写错了可从 git 恢复。"
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

export type { A2AScope };
