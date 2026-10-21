/**
 * 设置面的 **商店一节**:把 hikqin 当应用商店(Agent / 技能 / MCP / A2A)。
 *
 * 界面按**四个标签**分:每个标签只放它那一类资源(能装什么 + 已装什么)。
 * 标签顺序与设置面左侧一致(Agent → 技能 → MCP → A2A)。
 *
 * 五条口径,界面必须照着显示,否则用户会以为"坏了":
 *
 *   1. **计划先行**:装配前先 `POST /api/store/plan`,确认层里显示的是**计划**
 *      (写哪里 / 会不会撞 / 要补什么),不是一句"确定吗";
 *   2. **登录在页面上,但密码不过夜**:`POST /api/store/login` 把账号交给上游就丢,
 *      响应只有登录态 —— 令牌一个字节都不回浏览器。写凭证要确认头;
 *   3. **`missing[]` 必须显示**:skill / mcp 需要登录、a2a 公开 —— 少列了两类却说
 *      "商店是空的"是最坏的误解;
 *   4. **`needsConfig` 是待办**:mcp 的令牌写的是环境变量名,装完要 `export`;
 *   5. **冲突要么覆盖、要么停**:`blocked=true` 时只有"覆盖(备份后)"与"取消"。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type {
   StoreCheck,
   StoreInstalled,
   StoreItemOut,
   StoreKind,
   StorePlan,
   StoreStatus,
} from "../api/types";
import { ConfirmDialog } from "./ConfirmDialog";

/** 四个标签:与设置面左侧的节名对齐(Agent / 技能 / MCP / A2A)。 */
const TABS: { kind: StoreKind | "agent"; label: string; hint: string }[] = [
   {
      kind: "agent",
      label: "Agent",
      hint: "hikqin 的智能体(取详情,再逐个取它的 MCP / 技能)→ qi 的角色目录:agent.md + 私有 mcp.json + 私有 skills/,子智能体摊平成同级角色。",
   },
   { kind: "skill", label: "技能", hint: "一个目录 + SKILL.md;装到 ~/.qi/agent/skills/。" },
   { kind: "mcp", label: "MCP", hint: "只写 mcp.json 里的一个键;令牌以环境变量名落盘。" },
   { kind: "a2a", label: "A2A", hint: "一张 Agent Card → a2a.json 的一个条目(公开,不需要登录)。" },
];

type Tab = (typeof TABS)[number];

type Pending =
   | { mode: "install"; ids: string[] }
   | { mode: "uninstall"; id: string }
   | null;

export function StorePanel({ canWrite }: { canWrite: boolean }) {
   const [tab, setTab] = useState<Tab>(TABS[1]);          // 默认落在「技能」:眼下唯一有内容的
   const [status, setStatus] = useState<StoreStatus | null>(null);
   const [installed, setInstalled] = useState<StoreInstalled | null>(null);
   const [check, setCheck] = useState<StoreCheck | null>(null);
   const [items, setItems] = useState<StoreItemOut[]>([]);
   const [missing, setMissing] = useState<{ kind: string; reason: string }[]>([]);
   const [query, setQuery] = useState("");
   const [error, setError] = useState<string | null>(null);
   const [busy, setBusy] = useState(false);
   const [plan, setPlan] = useState<StorePlan | null>(null);
   const [pending, setPending] = useState<Pending>(null);
   const [done, setDone] = useState<string | null>(null);
   const [showLogin, setShowLogin] = useState(false);
   const [form, setForm] = useState({ baseUrl: "", login: "", password: "" });

   const loadMeta = useCallback(async () => {
      try {
         const [s, i, c] = await Promise.all([
            api.storeStatus(),
            api.storeInstalled(),
            api.storeCheck(),
         ]);
         setStatus(s);
         setInstalled(i);
         setCheck(c);
         const first = s.sources[0];
         if (first) {
            setForm((f) => ({ ...f, baseUrl: f.baseUrl || first.baseUrl, login: f.login || first.login }));
         }
      } catch (exc) {
         setError(exc instanceof ApiError ? exc.detail : String(exc));
      }
   }, []);

   const search = useCallback(
      async (which: Tab = tab) => {
         setBusy(true);
         setError(null);
         try {
            const body = await api.storeSearch(query.trim(), which.kind);
            if (!body.available) {
               setError(body.error || "qi-store 不可用");
               setItems([]);
               setMissing([]);
               return;
            }
            setItems(body.items);
            setMissing(body.missing);
         } catch (exc) {
            setError(exc instanceof ApiError ? exc.detail : String(exc));
         } finally {
            setBusy(false);
         }
      },
      [query, tab],
   );

   useEffect(() => {
      void loadMeta();
   }, [loadMeta]);

   useEffect(() => {
      void search(tab);
      // 换标签就查这一类的目录(每个标签只放它那一类资源)
   }, [tab, search]);

   const startInstall = useCallback(async (ids: string[]) => {
      setError(null);
      setDone(null);
      try {
         const body = await api.storePlan({ ids });
         if (!body.available) {
            setError(body.error || "qi-store 不可用");
            return;
         }
         setPlan(body);
         setPending({ mode: "install", ids });
      } catch (exc) {
         setError(exc instanceof ApiError ? exc.detail : String(exc));
      }
   }, []);

   const confirm = useCallback(
      async (force: boolean) => {
         if (!pending) return;
         setBusy(true);
         setError(null);
         try {
            if (pending.mode === "install") {
               const body = await api.storeInstall({ ids: pending.ids, force });
               setDone(
                  `已安装 ${body.installed.length} 条` +
                     (body.skipped.length ? `,跳过 ${body.skipped.length} 条` : "") +
                     (body.needsConfig.length
                        ? `;还要 export ${body.needsConfig.join(", ")}`
                        : ""),
               );
            } else {
               const body = await api.storeUninstall(pending.id);
               setDone(`已卸载 ${body.id}(${body.removed.length} 处改动)`);
            }
            setPending(null);
            setPlan(null);
            await loadMeta();
            await search();
         } catch (exc) {
            setError(exc instanceof ApiError ? exc.detail : String(exc));
         } finally {
            setBusy(false);
         }
      },
      [pending, loadMeta, search],
   );

   const submitLogin = useCallback(async () => {
      setBusy(true);
      setError(null);
      try {
         const body = await api.storeLogin({
            login: form.login.trim(),
            password: form.password,
            baseUrl: form.baseUrl.trim(),
         });
         if (!body.available) {
            setError(body.error || "登录失败");
            return;
         }
         setForm((f) => ({ ...f, password: "" }));   // 密码不留在内存里
         setShowLogin(false);
         setStatus(body);
         setDone(`已登录 ${body.sources[0]?.login ?? ""}`);
         await loadMeta();
         await search();
      } catch (exc) {
         setError(exc instanceof ApiError ? exc.detail : String(exc));
      } finally {
         setBusy(false);
      }
   }, [form, loadMeta, search]);

   const logout = useCallback(async () => {
      setBusy(true);
      setError(null);
      try {
         const body = await api.storeLogout({});
         setStatus(body);
         setDone("已删除本地凭证(上游没有吊销接口)");
         await loadMeta();
         await search();
      } catch (exc) {
         setError(exc instanceof ApiError ? exc.detail : String(exc));
      } finally {
         setBusy(false);
      }
   }, [loadMeta, search]);

   const sources = status?.sources ?? [];
   const loggedIn = sources.some((s) => s.loggedIn);
   const source = sources[0];
   const installedHere = useMemo(
      () => (installed?.items ?? []).filter((item) => item.kind === tab.kind),
      [installed, tab.kind],
   );

   if (status !== null && !status.available) {
      return (
         <p className="set-sec__empty">
            商店不可用:{status.error || "未安装 qi-store"}。
            <br />
            安装:<code>qi install qi-store</code>
         </p>
      );
   }

   return (
      <>
         <div className="set-store">
            {/* 四个标签:每个标签只放它那一类资源 */}
            <div className="set-store__tabs" role="tablist" aria-label="商店分类">
               {TABS.map((item) => (
                  <button
                     key={item.kind}
                     type="button"
                     role="tab"
                     aria-selected={tab.kind === item.kind}
                     className={`set-store__tab${tab.kind === item.kind ? " is-on" : ""}`}
                     onClick={() => setTab(item)}
                  >
                     {item.label}
                     <span className="set-store__count">
                        {(installed?.items ?? []).filter((i) => i.kind === item.kind).length || ""}
                     </span>
                  </button>
               ))}
            </div>

            <div className="set-store__bar">
               <div className="set-store__src">
                  {sources.length === 0 ? (
                     <span className="set-store__off">还没有配置商店源</span>
                  ) : (
                     <span>
                        <code>{source.name}</code> {source.baseUrl}{" "}
                        {loggedIn ? (
                           <span className="set-store__on">
                              已登录 {source.login || ""}({source.role || "?"})
                           </span>
                        ) : (
                           <span className="set-store__off">
                              未登录 —— 技能与 MCP 看不到(a2a 是公开的)
                           </span>
                        )}
                     </span>
                  )}
                  {canWrite ? (
                     <span className="set-store__auth">
                        <button
                           type="button"
                           disabled={busy}
                           onClick={() => {
                              setError(null);
                              setShowLogin((v) => !v);
                           }}
                        >
                           {showLogin ? "收起" : loggedIn ? "换个账号" : "登录"}
                        </button>
                        {loggedIn ? (
                           <button type="button" disabled={busy} onClick={() => void logout()}>
                              退出
                           </button>
                        ) : null}
                     </span>
                  ) : null}
               </div>

               {(
                  <div className="set-store__search">
                     <input
                        value={query}
                        placeholder="搜索名称或描述"
                        onChange={(e) => setQuery(e.target.value)}
                        onKeyDown={(e) => {
                           if (e.key === "Enter") void search();
                        }}
                     />
                     <button type="button" onClick={() => void search()} disabled={busy}>
                        搜索
                     </button>
                  </div>
               )}
            </div>

            {/* 登录表单:密码只在提交那一跳存在(提交后立刻清空) */}
            {showLogin ? (
               <div className="set-store__login">
                  <label>
                     后端地址
                     <input
                        value={form.baseUrl}
                        placeholder="http://<host>:6091"
                        onChange={(e) => setForm({ ...form, baseUrl: e.target.value })}
                     />
                  </label>
                  <label>
                     用户名
                     <input
                        value={form.login}
                        onChange={(e) => setForm({ ...form, login: e.target.value })}
                     />
                  </label>
                  <label>
                     密码
                     <input
                        type="password"
                        value={form.password}
                        onChange={(e) => setForm({ ...form, password: e.target.value })}
                        onKeyDown={(e) => {
                           if (e.key === "Enter") void submitLogin();
                        }}
                     />
                  </label>
                  <button
                     type="button"
                     disabled={busy || !form.login.trim() || !form.password}
                     onClick={() => void submitLogin()}
                  >
                     登录
                  </button>
                  <p className="set-store__path">
                     密码只交给上游一次,不落盘:写进磁盘的是返回的令牌(用户级 store.json,0600)。
                  </p>
               </div>
            ) : null}

            {error ? <p className="set-store__error">{error}</p> : null}
            {done ? <p className="set-store__done">{done}</p> : null}

            {missing.length > 0 ? (
               <p className="set-store__warn">
                  {missing
                     .map((m) => `${m.kind} 没查成:${m.reason}`)
                     .join(" · ")}
                  {loggedIn ? "" : "(skill / mcp 需要登录,a2a 公开)"}
               </p>
            ) : null}

            <p className="set-store__hint">{tab.hint}</p>

            {(
               items.length === 0 ? (
                  <p className="set-sec__empty">
                     {busy ? "查询中…" : `这一类的目录里没有匹配的条目。`}
                  </p>
               ) : (
                  <table className="set-table">
                     <thead>
                        <tr>
                           <th>名称</th>
                           <th>描述</th>
                           <th>安全</th>
                           <th>状态</th>
                        </tr>
                     </thead>
                     <tbody>
                        {items.map((item) => (
                           <tr key={item.id}>
                              <td className="set-mono">
                                 {item.name}
                                 <br />
                                 <span className="set-store__id">{item.id}</span>
                              </td>
                              <td>{item.description}</td>
                              <td>{String(item.safety.scan_status ?? "-")}</td>
                              <td>
                                 {item.updateAvailable ? (
                                    <button
                                       type="button"
                                       disabled={!canWrite || busy}
                                       onClick={() => void startInstall([item.id])}
                                    >
                                       更新
                                    </button>
                                 ) : item.installed ? (
                                    <span className="set-store__on">已装</span>
                                 ) : (
                                    <button
                                       type="button"
                                       disabled={!canWrite || busy}
                                       onClick={() => void startInstall([item.id])}
                                    >
                                       安装
                                    </button>
                                 )}
                              </td>
                           </tr>
                        ))}
                     </tbody>
                  </table>
               )
            )}

            <h3 className="set-store__h">
               这一类的已装({installedHere.length})
            </h3>
            {installedHere.length === 0 ? (
               <p className="set-sec__empty">还没有装任何 {tab.label} 类的东西。</p>
            ) : (
               <table className="set-table">
                  <thead>
                     <tr>
                        <th>名称</th>
                        <th>作用域</th>
                        <th>待补配置</th>
                        <th>落点</th>
                        <th />
                     </tr>
                  </thead>
                  <tbody>
                     {installedHere.map((item) => {
                        const drift = check?.items.find((c) => c.id === item.id);
                        return (
                           <tr key={`${item.id}:${item.scope}`}>
                              <td className="set-mono">
                                 {item.name}
                                 {/*
                                   账本有、盘上没:别处删掉的(设置面 A2A/MCP 那两节、
                                   手改 json、rm -rf)。这一行存在的意义就是**别撒谎** ——
                                   上面「可装」里同一条已经变回「安装」了。
                                 */}
                                 {!item.present ? (
                                    <>
                                       <br />
                                       <span className="set-store__error">
                                          已不在配置里:{item.missingReason}
                                       </span>
                                    </>
                                 ) : null}
                              </td>
                              <td>{item.scope === "global" ? "全局" : "项目"}</td>
                              <td>
                                 {item.needsConfig.length === 0 ? (
                                    <span className="set-store__off">—</span>
                                 ) : (
                                    <span className="set-store__warn">
                                       export {item.needsConfig.join(" ")}
                                    </span>
                                 )}
                              </td>
                              <td className="set-mono set-store__path">{item.target}</td>
                              <td>
                                 {drift?.status === "update" ? (
                                    <button
                                       type="button"
                                       disabled={!canWrite || busy}
                                       onClick={() => void startInstall([item.id])}
                                    >
                                       更新
                                    </button>
                                 ) : null}
                                 <button
                                    type="button"
                                    disabled={!canWrite || busy}
                                    onClick={() => {
                                       setError(null);
                                       setPending({ mode: "uninstall", id: item.id });
                                    }}
                                 >
                                    {item.present ? "卸载" : "清理账本"}
                                 </button>
                              </td>
                           </tr>
                        );
                     })}
                  </tbody>
               </table>
            )}

            {check?.wantedMissing.length ? (
               <p className="set-store__warn">
                  项目声明了但没装:{check.wantedMissing.map((w) => w.spec).join("、")}
                  ——在终端跑 <code>qi store sync</code>
               </p>
            ) : null}
         </div>

         {pending?.mode === "install" && plan ? (
            <ConfirmDialog
               title="确认安装"
               rows={plan.items.map((item) => ({
                  label: item.id,
                  // ConfirmRow 的 value 是 **字符串**:弹层不是画布,一行一条事实
                  value: [
                     item.name,
                     item.conflict ? `冲突:${item.conflict}(将备份后覆盖)` : item.note,
                     `→ ${item.targets.join("、")}`,
                  ]
                     .filter(Boolean)
                     .join(" · "),
               }))}
               warning={
                  plan.needsConfig.length
                     ? `装完还要补环境变量:${plan.needsConfig.join(", ")}`
                     : undefined
               }
               confirmLabel={plan.blocked ? "覆盖并安装" : "安装"}
               danger={plan.items.some((item) => item.unsafe)}
               confirmDisabled={busy}
               onCancel={() => {
                  setPending(null);
                  setPlan(null);
               }}
               onConfirm={() => void confirm(plan.blocked)}
            />
         ) : null}

         {pending?.mode === "uninstall" ? (
            <ConfirmDialog
               title="确认卸载"
               rows={[{
                  label: pending.id,
                  value: (installed?.items ?? []).find((i) => i.id === pending.id)?.present === false
                     ? "磁盘上已经没有了 —— 只把这笔账从账本里划掉"
                     : "从账本移除并删掉它写的文件/键",
               }]}
               warning="只删这条账本登记过的东西;mcp.json / a2a.json 里只删它那一个键"
               danger
               confirmDisabled={busy}
               onCancel={() => setPending(null)}
               onConfirm={() => void confirm(false)}
            />
         ) : null}
      </>
   );
}
