/**
 * 设置面的「应用助手」一节：项目级 `.qi/assistants.json` 的增删改查。
 *
 * 四条口径：
 *   1. **只有项目级一层** —— 文件路径在页头明写，写下去的是磁盘上哪一份；
 *   2. **`url` 是外链** —— 只接受 `http(s)://` 绝对地址，点击在新标签打开；
 *   3. 写操作全部带 `X-Qi-Confirm: yes`，并回整份列表视图（一次往返刷新完）；
 *   4. 没有 `assistant_write` 时整节只读 —— 不摆点了才 404 的键。
 *
 * 推荐不在这里：推荐是角色「训练评估」在对话里调工具完成的（卡片由
 * `details["ui"]` 的 `assistant_cards` 渲染）。这里只管目录本身。
 */
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { AssistantInfo, AssistantList, AssistantWriteBody } from "../api/types";
import { ConfirmDialog } from "./ConfirmDialog";

interface Draft {
   name: string;
   description: string;
   tasks: string;
   outputs: string;
   url: string;
   keywords: string;
   enabled: boolean;
}

function emptyDraft(): Draft {
   return {
      name: "",
      description: "",
      tasks: "",
      outputs: "",
      url: "",
      keywords: "",
      enabled: true,
   };
}

function draftOf(assistant: AssistantInfo): Draft {
   return {
      name: assistant.name,
      description: assistant.description,
      tasks: assistant.tasks.join("\n"),
      outputs: assistant.outputs.join("\n"),
      url: assistant.url,
      keywords: assistant.keywords.join("、"),
      enabled: assistant.enabled,
   };
}

function splitLines(text: string): string[] {
   return text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
}

function splitKeywords(text: string): string[] {
   return text
      .split(/[\n,，、]+/)
      .map((word) => word.trim())
      .filter(Boolean);
}

type Built =
   | { ok: true; body: AssistantWriteBody }
   | { ok: false; error: string };

/** 前端先挡一遍明显错误：后端仍会完整校验（这里是提示，不是唯一防线）。 */
function buildBody(draft: Draft): Built {
   const name = draft.name.trim();
   if (!name) return { ok: false, error: "名称不能为空" };
   if (name.length > 40) return { ok: false, error: "名称最多 40 字" };

   const description = draft.description.trim();
   if (!description) return { ok: false, error: "描述不能为空" };
   if (description.length > 400) return { ok: false, error: "描述最多 400 字" };

   const tasks = splitLines(draft.tasks);
   if (tasks.length === 0) return { ok: false, error: "典型任务至少 1 条（每行一条）" };
   if (tasks.length > 8) return { ok: false, error: "典型任务最多 8 条" };
   const longTask = tasks.find((task) => task.length > 60);
   if (longTask) return { ok: false, error: `典型任务单条最多 60 字：${longTask}` };

   const outputs = splitLines(draft.outputs);
   if (outputs.length === 0) return { ok: false, error: "核心产出至少 1 条（每行一条）" };
   if (outputs.length > 8) return { ok: false, error: "核心产出最多 8 条" };
   const longOutput = outputs.find((output) => output.length > 60);
   if (longOutput) return { ok: false, error: `核心产出单条最多 60 字：${longOutput}` };

   const url = draft.url.trim();
   if (!url) return { ok: false, error: "跳转链接不能为空" };
   let parsed: URL;
   try {
      parsed = new URL(url);
   } catch {
      return { ok: false, error: "跳转链接不是合法 URL" };
   }
   if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return { ok: false, error: "跳转链接只接受 http(s):// 绝对地址" };
   }
   if (parsed.username || parsed.password) {
      return { ok: false, error: "跳转链接不能带用户名/密码" };
   }

   const keywords = splitKeywords(draft.keywords);
   if (keywords.length > 12) return { ok: false, error: "关键词最多 12 个" };
   const longKeyword = keywords.find((word) => word.length > 20);
   if (longKeyword) return { ok: false, error: `单个关键词最多 20 字：${longKeyword}` };

   return {
      ok: true,
      body: { name, description, tasks, outputs, url, keywords, enabled: draft.enabled },
   };
}

function errorText(exc: unknown): string {
   return exc instanceof ApiError ? exc.detail : String(exc);
}

export function AssistantsPanel({
   canWrite,
   onChanged,
   onModalChange,
}: {
   canWrite: boolean;
   onChanged?: () => void;
   /** 把“设置页里还压着一层弹层”告诉外壳：`Esc` 先关弹层，别把整个设置一起关掉。 */
   onModalChange?: (open: boolean) => void;
}) {
   const [data, setData] = useState<AssistantList | null>(null);
   const [error, setError] = useState<string | null>(null);
   const [done, setDone] = useState<string | null>(null);
   const [busy, setBusy] = useState(false);
   const [editing, setEditing] = useState<{ assistant: AssistantInfo | null } | null>(null);
   const [draft, setDraft] = useState<Draft>(emptyDraft);
   const [formError, setFormError] = useState<string | null>(null);
   const [removing, setRemoving] = useState<AssistantInfo | null>(null);
   const [importing, setImporting] = useState(false);
   /** 删除 / 导入弹层里的错误：报在弹层里，别让遮罩挡住顶上的 notice。 */
   const [actionError, setActionError] = useState<string | null>(null);

   const load = useCallback(async () => {
      try {
         setData(await api.assistants());
      } catch (exc) {
         setError(errorText(exc));
      }
   }, []);

   useEffect(() => {
      void load();
   }, [load]);

   useEffect(() => {
      onModalChange?.(editing !== null || removing !== null || importing);
   }, [editing, removing, importing, onModalChange]);

   // 切走这一节时把“有弹层”的标志收回 —— 否则外壳会一直以为 `Esc` 该让给面板。
   useEffect(() => () => onModalChange?.(false), [onModalChange]);

   const apply = useCallback(
      (next: AssistantList, message: string) => {
         setData(next);
         setDone(message);
         setError(null);
         onChanged?.();
      },
      [onChanged],
   );

   const startCreate = useCallback(() => {
      setDraft(emptyDraft());
      setFormError(null);
      setEditing({ assistant: null });
   }, []);

   const startEdit = useCallback((assistant: AssistantInfo) => {
      setDraft(draftOf(assistant));
      setFormError(null);
      setEditing({ assistant });
   }, []);

   const submit = useCallback(async () => {
      if (!editing) return;
      const built = buildBody(draft);
      if (!built.ok) {
         setFormError(built.error);
         return;
      }
      setBusy(true);
      setFormError(null);
      try {
         const next = editing.assistant
            ? await api.updateAssistant({ id: editing.assistant.id, ...built.body })
            : await api.createAssistant(built.body);
         setEditing(null);
         apply(next, editing.assistant ? "已保存" : "已创建");
      } catch (exc) {
         // 错误留在弹层里：顶上那条 notice 被遮罩挡着，看不到就等于“点了没反应”。
         setFormError(errorText(exc));
      } finally {
         setBusy(false);
      }
   }, [apply, draft, editing]);

   const remove = useCallback(async () => {
      if (!removing) return;
      setBusy(true);
      try {
         apply(await api.deleteAssistant(removing.id), `已删除 ${removing.name}`);
         setRemoving(null);
      } catch (exc) {
         setActionError(errorText(exc));
      } finally {
         setBusy(false);
      }
   }, [apply, removing]);

   const importSeed = useCallback(async () => {
      setBusy(true);
      try {
         const next = await api.importAssistants("merge");
         apply(next, `已导入 ${next.created} 个，跳过 ${next.skipped} 个`);
         setImporting(false);
      } catch (exc) {
         setActionError(errorText(exc));
      } finally {
         setBusy(false);
      }
   }, [apply]);

   if (data === null) {
      return <p className="set-sec__empty">{error || "读取中…"}</p>;
   }
   if (data.unavailable) {
      return (
         <p className="set-sec__empty">
            没装 qi-assistants 扩展，读不到应用助手目录。
            {data.error ? `（${data.error}）` : ""}
         </p>
      );
   }

   const built = buildBody(draft);

   return (
      <div className="set-store">
         <div className="set-store__bar">
            <div className="set-store__src">
               <span className="set-store__on">
                  {data.assistants.length} 个助手 ·{" "}
                  {data.assistants.filter((item) => item.enabled).length} 个启用
               </span>
               <span className="set-store__id">{data.path}</span>
               {data.exists ? null : <span className="set-store__off">文件还没建</span>}
            </div>
            {canWrite ? (
               <span className="set-card__actions">
                  <button className="set-act" type="button" disabled={busy}
                          onClick={startCreate}>
                     新建
                  </button>
                  <button className="set-act" type="button" disabled={busy}
                          onClick={() => {
                             setActionError(null);
                             setImporting(true);
                          }}>
                     导入示例
                  </button>
               </span>
            ) : (
               <span className="set-store__off">宿主没有 assistant_write，这一节只读</span>
            )}
         </div>

         {error ? <p className="set-store__error">{error}</p> : null}
         {done ? <p className="set-store__done">{done}</p> : null}

         {data.assistants.length === 0 ? (
            <p className="set-sec__empty">
               目录还是空的。新建一条，或先导入 3 个示例再改。
            </p>
         ) : (
            <ul className="set-cards">
               {data.assistants.map((assistant) => (
                  <li
                     className={`set-card${assistant.enabled ? "" : " set-card--off"}`}
                     key={assistant.id}
                  >
                     <div className="set-card__head">
                        <span className="set-card__name" title={assistant.name}>
                           {assistant.name}
                        </span>
                        <span className={`set-card__tag ${assistant.enabled ? "set-card__tag--on" : "set-card__tag--off"}`}>
                           {assistant.enabled ? "启用" : "停用"}
                        </span>
                        {canWrite ? (
                           <span className="set-card__actions">
                              <button className="set-act" type="button" disabled={busy}
                                      onClick={() => startEdit(assistant)}>
                                 编辑
                              </button>
                              <button className="set-act set-act--danger" type="button"
                                      disabled={busy}
                                      onClick={() => {
                                         setActionError(null);
                                         setRemoving(assistant);
                                      }}>
                                 删除
                              </button>
                           </span>
                        ) : null}
                     </div>
                     <p className="set-card__desc">{assistant.description}</p>
                     <div className="set-card__fields">
                        <span className="kv">
                           <span className="kv__label">典型任务</span>
                           <span className="kv__value">{assistant.tasks.join("、")}</span>
                        </span>
                        <span className="kv">
                           <span className="kv__label">核心产出</span>
                           <span className="kv__value">{assistant.outputs.join("、")}</span>
                        </span>
                        <span className="kv">
                           <span className="kv__label">链接</span>
                           <span className="kv__value set-mono">{assistant.url}</span>
                        </span>
                     </div>
                     <span className="set-mono set-store__id">
                        {assistant.id}
                        {assistant.keywords.length > 0
                           ? ` · 关键词：${assistant.keywords.join("、")}`
                           : ""}
                     </span>
                  </li>
               ))}
            </ul>
         )}

         {editing !== null ? (
            <ConfirmDialog
               title={editing.assistant ? `编辑 ${editing.assistant.name}?` : "新建应用助手?"}
               rows={[
                  { label: "目标文件", value: data.path },
                  ...(editing.assistant
                     ? [{ label: "id", value: editing.assistant.id }]
                     : []),
               ]}
               warning="写进 .qi/assistants.json（跟着仓库走，可能被提交）。url 只接受 http(s) 外链。"
               confirmLabel={editing.assistant ? "保存" : "创建"}
               confirmDisabled={busy || !built.ok}
               onCancel={() => {
                  setEditing(null);
                  setFormError(null);
               }}
               onConfirm={() => void submit()}
            >
               <div className="mcp-form">
                  <label className="mcp-form__row">
                     <span className="mcp-form__label">名称</span>
                     <input
                        className="dialog__input"
                        value={draft.name}
                        aria-label="名称"
                        autoFocus
                        placeholder="评估方案生成助手"
                        onChange={(event) =>
                           setDraft((current) => ({ ...current, name: event.target.value }))
                        }
                     />
                  </label>
                  <label className="mcp-form__row mcp-form__row--top">
                     <span className="mcp-form__label">描述</span>
                     <textarea
                        className="dialog__area"
                        value={draft.description}
                        aria-label="描述"
                        rows={3}
                        placeholder="意图分析、评估要素确认，输出评估方案。"
                        onChange={(event) =>
                           setDraft((current) => ({
                              ...current,
                              description: event.target.value,
                           }))
                        }
                     />
                  </label>
                  <label className="mcp-form__row mcp-form__row--top">
                     <span className="mcp-form__label">典型任务(每行一条)</span>
                     <textarea
                        className="dialog__area"
                        value={draft.tasks}
                        aria-label="典型任务"
                        rows={3}
                        placeholder={"意图分析\n评估要素确认"}
                        onChange={(event) =>
                           setDraft((current) => ({ ...current, tasks: event.target.value }))
                        }
                     />
                  </label>
                  <label className="mcp-form__row mcp-form__row--top">
                     <span className="mcp-form__label">核心产出(每行一条)</span>
                     <textarea
                        className="dialog__area"
                        value={draft.outputs}
                        aria-label="核心产出"
                        rows={2}
                        placeholder="评估方案文档"
                        onChange={(event) =>
                           setDraft((current) => ({ ...current, outputs: event.target.value }))
                        }
                     />
                  </label>
                  <label className="mcp-form__row">
                     <span className="mcp-form__label">跳转链接</span>
                     <input
                        className="dialog__input"
                        value={draft.url}
                        aria-label="跳转链接"
                        placeholder="https://eval.example.com/assistants/plan"
                        onChange={(event) =>
                           setDraft((current) => ({ ...current, url: event.target.value }))
                        }
                     />
                  </label>
                  <label className="mcp-form__row">
                     <span className="mcp-form__label">关键词</span>
                     <input
                        className="dialog__input"
                        value={draft.keywords}
                        aria-label="关键词"
                        placeholder="方案、指标、评估要素（逗号或顿号分隔）"
                        onChange={(event) =>
                           setDraft((current) => ({
                              ...current,
                              keywords: event.target.value,
                           }))
                        }
                     />
                  </label>
                  <label className="mcp-form__check">
                     <input
                        type="checkbox"
                        checked={draft.enabled}
                        onChange={(event) =>
                           setDraft((current) => ({
                              ...current,
                              enabled: event.target.checked,
                           }))
                        }
                     />
                     启用（参与推荐；停用后不会出现在推荐里）
                  </label>

                  {built.ok ? (
                     <div className="set-card">
                        <div className="set-card__head">
                           <span className="set-card__name">{built.body.name}</span>
                           <span className={`set-card__tag ${built.body.enabled === false ? "set-card__tag--off" : "set-card__tag--on"}`}>
                              {built.body.enabled === false ? "停用" : "启用"}
                           </span>
                        </div>
                        <p className="set-card__desc">{built.body.description}</p>
                        <div className="set-card__fields">
                           <span className="kv">
                              <span className="kv__label">典型任务</span>
                              <span className="kv__value">{built.body.tasks.join("、")}</span>
                           </span>
                           <span className="kv">
                              <span className="kv__label">核心产出</span>
                              <span className="kv__value">{built.body.outputs.join("、")}</span>
                           </span>
                        </div>
                     </div>
                  ) : (
                     <p className="mcp-form__hint">{built.error}</p>
                  )}

                  {formError ? <div className="dialog__error">{formError}</div> : null}
               </div>
            </ConfirmDialog>
         ) : null}

         {removing !== null ? (
            <ConfirmDialog
               title={`删除 ${removing.name}?`}
               rows={[
                  { label: "名称", value: removing.name },
                  { label: "链接", value: removing.url },
                  { label: "目标文件", value: data.path },
               ]}
               warning="只删这一条记录，不可恢复。想保留但不再推荐，请把「启用」开关关掉。"
               confirmLabel="删除"
               danger
               confirmDisabled={busy}
               onCancel={() => {
                  setRemoving(null);
                  setActionError(null);
               }}
               onConfirm={() => void remove()}
            >
               {actionError ? <div className="dialog__error">{actionError}</div> : null}
            </ConfirmDialog>
         ) : null}

         {importing ? (
            <ConfirmDialog
               title="导入示例助手?"
               rows={[{ label: "目标文件", value: data.path }]}
               warning="merge 会跳过同名条目，不会覆盖已有内容；重复点不会产生重复项。"
               confirmLabel="导入"
               confirmDisabled={busy}
               onCancel={() => {
                  setImporting(false);
                  setActionError(null);
               }}
               onConfirm={() => void importSeed()}
            >
               {actionError ? <div className="dialog__error">{actionError}</div> : null}
            </ConfirmDialog>
         ) : null}
      </div>
   );
}
