/**
 * 设置面的「知识库」一节:qi-kb 的 provider（hikqin 账号）+ knowledgeBases。
 *
 * 四条口径:
 *   1. **只摆全局层**:项目级 provider / KB 跟着仓库走,由 `state/scope.ts` 过滤掉;
 *   2. 账号在 qi-kb 自己的 `kb.json` / `kb-auth.json` 里,不在 `store.json`;
 *   3. 同一个 `(instance, kbId)` 只保留一份;重复安装先 `plan`,再确认是否覆盖;
 *   4. 写操作带 `X-Qi-Confirm: yes`,令牌不回显。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { KbItem, KbStatus } from "../api/types";
import { globalKbItems, globalKbProviders } from "../state/scope";

export function KbPanel({ canWrite, onChanged }: { canWrite: boolean; onChanged?: () => void }) {
   const [status, setStatus] = useState<KbStatus | null>(null);
   const [items, setItems] = useState<KbItem[]>([]);
   const [error, setError] = useState<string | null>(null);
   const [done, setDone] = useState<string | null>(null);
   const [busy, setBusy] = useState(false);
   const [provider, setProvider] = useState("");
   const [query, setQuery] = useState("");
   const [search, setSearch] = useState<string>("");
   const [askAnswer, setAskAnswer] = useState("");
   const [askSources, setAskSources] = useState("");
   const [askRunning, setAskRunning] = useState(false);
   const askAbort = useRef<(() => void) | null>(null);
   const [form, setForm] = useState({
      name: "",
      baseUrl: "",
      login: "",
      password: "",
   });

   const load = useCallback(async () => {
      try {
         const [s, list] = await Promise.all([
            api.kbStatus(),
            api.kbAvailable(undefined, undefined, false),
         ]);
         const providers = globalKbProviders(s.providers);
         setStatus({ ...s, providers });
         setItems(globalKbItems(list.items));
         if (!provider && providers[0]) setProvider(providers[0].name);
         if (!form.baseUrl && providers[0]) {
            setForm((f) => ({ ...f, baseUrl: providers[0].baseUrl }));
         }
      } catch (exc) {
         setError(exc instanceof ApiError ? exc.detail : String(exc));
      }
   }, [provider, form.baseUrl]);

   useEffect(() => {
      void load();
      // 只首屏加载；后续写操作自己刷新
      // eslint-disable-next-line react-hooks/exhaustive-deps
   }, []);

   const run = useCallback(
      async (label: string, fn: () => Promise<unknown>) => {
         setBusy(true);
         setError(null);
         setDone(null);
         try {
            await fn();
            setDone(label);
            await load();
            onChanged?.();
         } catch (exc) {
            setError(exc instanceof ApiError ? exc.detail : String(exc));
         } finally {
            setBusy(false);
         }
      },
      [load, onChanged],
   );

   const addProvider = useCallback(() => {
      if (!form.name.trim() || !form.baseUrl.trim()) return;
      void run("已添加 provider", () =>
         api.kbProviderAdd({
            name: form.name.trim(),
            baseUrl: form.baseUrl.trim(),
            login: form.login.trim() || undefined,
            scope: "global",
         }),
      );
   }, [form, run]);

   const loginProvider = useCallback(() => {
      if (!form.name.trim() || !form.password) return;
      void run("已登录 provider", () =>
         api.kbProviderLogin({
            name: form.name.trim(),
            login: form.login.trim() || undefined,
            password: form.password,
            baseUrl: form.baseUrl.trim() || undefined,
            scope: "global",
         }),
      );
      setForm((f) => ({ ...f, password: "" }));
   }, [form, run]);

   const install = useCallback(
      async (item: KbItem) => {
         const target = item.bound ? item.boundProvider : item.provider;
         const prov = provider || item.provider;
         if (!prov) return;
         setBusy(true);
         setError(null);
         try {
            const plan = await api.kbPlan({ kbId: item.id, provider: prov, scope: "global" });
            let rebind = false;
            if (plan.blocked || (plan.action === "rebind" && plan.existingProvider !== prov)) {
               rebind = window.confirm(
                  `该 KB 已由 ${plan.existingProvider || target} 安装（同实例同 KB）。\n` +
                     `覆盖为 ${prov}？\n` +
                     `覆盖只改归属 provider，不复制文档，也不删除上游数据。`,
               );
               if (!rebind) return;
            }
            await api.kbInstall({ kbId: item.id, provider: prov, rebind, scope: "global" });
            setDone(`已安装 ${item.name}`);
            await load();
            onChanged?.();
         } catch (exc) {
            setError(exc instanceof ApiError ? exc.detail : String(exc));
         } finally {
            setBusy(false);
         }
      },
      [provider, load, onChanged],
   );

   const uninstall = useCallback(
      (item: KbItem) => {
         if (!window.confirm(`卸载 ${item.name}？（只删本地绑定，不动上游）`)) return;
         void run("已卸载知识库", () => api.kbUninstall(item.id));
      },
      [run],
   );

   const removeProvider = useCallback(
      (name: string) => {
         if (!window.confirm(`删除 provider ${name}？有 KB 时会失败。`)) return;
         void run("已删除 provider", () => api.kbProviderRemove(name));
      },
      [run],
   );

   const doSearch = useCallback(() => {
      if (!query.trim()) return;
      setBusy(true);
      setError(null);
      setAskAnswer("");
      setAskSources("");
      void api
         .kbSearch({ query: query.trim(), provider: provider || undefined })
         .then((data) => {
            setSearch(JSON.stringify(data, null, 2));
         })
         .catch((exc) =>
            setError(exc instanceof ApiError ? exc.detail : String(exc)),
         )
         .finally(() => setBusy(false));
   }, [query, provider]);

   const stopAsk = useCallback(() => {
      askAbort.current?.();
      askAbort.current = null;
      setAskRunning(false);
   }, []);

   const doAsk = useCallback(() => {
      if (!query.trim()) return;
      stopAsk();
      setBusy(true);
      setError(null);
      setSearch("");
      setAskAnswer("");
      setAskSources("");
      setAskRunning(true);
      askAbort.current = api.kbAskStream(
         { question: query.trim(), provider: provider || undefined },
         (event) => {
            if (event.type === "token") {
               setAskAnswer((prev) => prev + String(event.content ?? ""));
            } else if (event.type === "sources") {
               setAskSources(JSON.stringify(event.content ?? [], null, 2));
            } else if (event.type === "done") {
               setAskRunning(false);
               setBusy(false);
            } else if (event.type === "error") {
               setError(String(event.message || "问答失败"));
               setAskRunning(false);
               setBusy(false);
            }
         },
         (err) => {
            setError(err.message);
            setAskRunning(false);
            setBusy(false);
         },
         () => {
            setAskRunning(false);
            setBusy(false);
         },
      );
   }, [query, provider, stopAsk]);

   if (status === null) {
      return <p className="set-sec__empty">{error || "读取中…"}</p>;
   }
   if (!status.available) {
      return (
         <p className="set-sec__empty">
            没装 qi-kb 扩展，读不到知识库配置。{status.error ? `（${status.error}）` : ""}
         </p>
      );
   }

   return (
      <div className="set-store">
         <div className="set-store__bar">
            <div className="set-store__src">
               {status.providers.length === 0 ? (
                  <span className="set-store__off">还没有 provider（hikqin 账号）</span>
               ) : (
                  <span className="set-store__on">
                     {status.providers.length} 个 provider ·{" "}
                     {status.providers.filter((p) => p.loggedIn).length} 个已登录
                  </span>
               )}
            </div>
         </div>

         {status.providers.length > 0 ? (
            <ul className="set-cards">
               {status.providers.map((p) => (
                  <li className="set-card" key={p.name}>
                     <div className="set-card__head">
                        <span className="set-card__name">{p.name}</span>
                        <span className="set-card__tag">
                           {p.loggedIn ? "已登录" : "未登录"}
                        </span>
                        <span className="set-card__actions">
                           <button
                              className="set-act"
                              type="button"
                              onClick={() => {
                                 setForm((f) => ({ ...f, name: p.name, baseUrl: p.baseUrl }));
                                 setProvider(p.name);
                              }}
                           >
                              选中
                           </button>
                           {canWrite ? (
                              <button
                                 className="set-act set-act--danger"
                                 type="button"
                                 disabled={busy}
                                 onClick={() => removeProvider(p.name)}
                              >
                                 删除
                              </button>
                           ) : null}
                        </span>
                     </div>
                     <div className="set-card__fields">
                        <span className="kv">
                           <span className="kv__label">地址</span>
                           <span className="kv__value">{p.baseUrl}</span>
                        </span>
                        <span className="kv">
                           <span className="kv__label">账号</span>
                           <span className="kv__value">{p.login || "-"}</span>
                        </span>
                        <span className="kv">
                           <span className="kv__label">KB</span>
                           <span className="kv__value">{p.kbCount}</span>
                        </span>
                     </div>
                  </li>
               ))}
            </ul>
         ) : null}

         {canWrite ? (
            <div className="set-store__login">
               <input
                  placeholder="provider 名（如 alice）"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
               />
               <input
                  placeholder="baseUrl（如 http://10.1.43.6:7091）"
                  value={form.baseUrl}
                  onChange={(e) => setForm({ ...form, baseUrl: e.target.value })}
               />
               <input
                  placeholder="登录名（可选，默认同 provider 名）"
                  value={form.login}
                  onChange={(e) => setForm({ ...form, login: e.target.value })}
               />
               <input
                  placeholder="密码（只用于登录）"
                  type="password"
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
               />
               <button className="set-act" type="button" disabled={busy} onClick={addProvider}>
                  添加 provider
               </button>
               <button className="set-act" type="button" disabled={busy} onClick={loginProvider}>
                  登录
               </button>
            </div>
         ) : null}

         {error ? <p className="set-store__error">{error}</p> : null}
         {done ? <p className="set-store__done">{done}</p> : null}

         <div className="set-store__bar">
            <input
               placeholder="搜索知识库名称 / 描述"
               value={query}
               onChange={(e) => setQuery(e.target.value)}
            />
            <button className="set-act" type="button" disabled={busy && !askRunning}
                    onClick={doSearch}>
               检索
            </button>
            <button className="set-act" type="button" disabled={busy && !askRunning}
                    onClick={doAsk}>
               流式问答
            </button>
            {askRunning ? (
               <button className="set-act set-act--danger" type="button" onClick={stopAsk}>
                  停止
               </button>
            ) : null}
            <select value={provider} onChange={(e) => setProvider(e.target.value)}>
               <option value="">全部 provider</option>
               {status.providers.map((p) => (
                  <option key={p.name} value={p.name}>
                     {p.name}
                  </option>
               ))}
            </select>
         </div>

         {search ? <pre className="set-store__path">{search}</pre> : null}
         {askAnswer || askSources || askRunning ? (
            <div className="set-store__login">
               <pre className="set-store__path">{askAnswer || "…"}</pre>
               {askSources ? (
                  <pre className="set-store__path">{`来源：
${askSources}`}</pre>
               ) : null}
            </div>
         ) : null}

         <table className="set-table">
            <thead>
               <tr>
                  <th>知识库</th>
                  <th>provider</th>
                  <th>docs / chunks</th>
                  <th>状态</th>
                  <th />
               </tr>
            </thead>
            <tbody>
               {items.map((item) => (
                  <tr key={`${item.provider}:${item.id}`}>
                     <td>
                        <div>{item.name}</div>
                        <span className="set-mono set-store__id">{item.id}</span>
                     </td>
                     <td className="set-mono">{item.provider}</td>
                     <td>
                        {item.docCount} / {item.chunkCount}
                     </td>
                     <td>
                        {item.bound ? (
                           <span className="set-store__on">已绑定 {item.boundProvider}</span>
                        ) : (
                           <span className="set-store__off">未绑定</span>
                        )}
                     </td>
                     <td>
                        {canWrite ? (
                           item.bound && item.boundProvider === (provider || item.provider) ? (
                              <button
                                 className="set-act set-act--danger"
                                 type="button"
                                 disabled={busy}
                                 onClick={() => uninstall(item)}
                              >
                                 卸载
                              </button>
                           ) : (
                              <button
                                 className="set-act"
                                 type="button"
                                 disabled={busy}
                                 onClick={() => void install(item)}
                              >
                                 {item.bound ? "覆盖/切换" : "安装"}
                              </button>
                           )
                        ) : null}
                     </td>
                  </tr>
               ))}
            </tbody>
         </table>
         {items.length === 0 ? (
            <p className="set-sec__empty">没有知识库。先添加 provider 并登录。</p>
         ) : null}
      </div>
   );
}
