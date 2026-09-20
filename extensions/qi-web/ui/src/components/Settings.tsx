/**
 * 设置面 —— **形状照 dsh 的设置面板**(`ui-settings-general/SettingsRoot.module.css`,
 * figma 501:29947):全屏遮罩 + 居中面板(800 宽、`min(800px, 100vh - 48px)` 高、
 * r32),左导轨 188(标题行 16/500 + 40px 导航格 + 16px 图标),右内容列
 * (54px 头 + 可滚动的 options,内容列 max-width 720)。
 *
 * 这里以前是**页内两栏**(`grid` + 卡片),三个实际缺陷促成了这次重做:
 *
 *   1. 它**占的是工作台的位置**。dsh 的设置是浮层 —— 底下那屏原样不动,
 *      关掉就回来;qi 原来是把 `work__body` 整个换掉,于是"打开设置"会丢
 *      掉当前正在看的东西(以及空态 hero)。
 *   2. 「标签/值」行的类名是 `.row` / `.row__label` / `.row__value`,而
 *      **app.css 里从来没有这三个类**(它有的是 `.rowline__*`)—— 那些行一直是
 *      裸 DOM:标签和值挤在一起、没有对齐、没有字号差。
 *   3. 表格 + `✓/✗` 文本不是 dsh 的语汇。dsh 的模型页是**卡片行 + 状态圆点**
 *      (圆点带 `aria-label`,状态不单靠颜色),`.table` 只用在模型目录那种
 *      真正的二维数据上。
 *
 * 内容仍然**只放 qi 真有的**:取值来自 `/api/config`、`/api/agents`、`/api/skills`、
 * `/api/extensions`、`/api/mcp`;没有的字段就不显示 —— 不编造 dsh 有而 qi 没有的节
 * (它的沙箱 / 审批 / 遥测开关 qi 都还没有)。
 *
 * 唯一可写动作是**凭证**:必须过二次确认层(显示目标文件绝对路径 + 掩码值),
 * 请求再带 `X-Qi-Confirm: yes`(后端也拦,双保险)。
 */
import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { api, ApiError } from "../api/client";
import type {
   AgentInfo,
   ConfigView,
   ExtensionList,
   McpList,
   McpServerInfo,
   McpSource,
   SkillInfo,
} from "../api/types";
import { ConfirmDialog } from "./ConfirmDialog";
import {
   IconAgentPresetOutline16,
   IconCloseOutline16,
   IconDataOutline16,
   IconFolderOpen16,
   IconGaugeOutline16,
   IconLinkOutline16,
   IconPersonalizationOutline16,
   IconSkillOutline16,
} from "./icons";

export type SettingsSectionKey =
   | "models"
   | "agents"
   | "skills"
   | "mcp"
   | "extensions"
   | "sessions"
   | "diagnostics";

/**
 * 分节表。图标**按节选语义**,与 dsh `SettingsRoot.tsx` 的 `navIcon()` 同规矩
 * (它那边 models → Data、agent-presets → AgentPreset、plugins → Personalization,
 * 未知 id 回落 Settings)。qi 的节名不同,所以"技能/诊断"两节是**从 dsh 的字形库里
 * 挑表意对的**(Skill / Gauge),不是照抄它的分节表 —— 它的分节表里没有这两节。
 */
const SECTIONS: {
   key: SettingsSectionKey;
   label: string;
   icon: ReactNode;
}[] = [
   { key: "models", label: "模型", icon: <IconDataOutline16 size={16} /> },
   { key: "agents", label: "Agent", icon: <IconAgentPresetOutline16 size={16} /> },
   { key: "skills", label: "技能", icon: <IconSkillOutline16 size={16} /> },
   { key: "mcp", label: "MCP", icon: <IconLinkOutline16 size={16} /> },
   {
      key: "extensions",
      label: "扩展",
      icon: <IconPersonalizationOutline16 size={16} />,
   },
   { key: "sessions", label: "会话与路径", icon: <IconFolderOpen16 size={16} /> },
   { key: "diagnostics", label: "诊断", icon: <IconGaugeOutline16 size={16} /> },
];

/** 分节壳:标题 16/500 lh24 + 引言 14/22 `label-tertiary`(dsh `.title` / `.intro`)。 */
function Section({
   title,
   intro,
   children,
}: {
   title: string;
   intro?: ReactNode;
   children: ReactNode;
}) {
   return (
      <section className="set-sec">
         <h2 className="set-sec__title">{title}</h2>
         {intro ? <p className="set-sec__intro">{intro}</p> : null}
         {children}
      </section>
   );
}

/** 标签/值行(dsh 的“字段行”语汇):标签 `label-tertiary`,值 `label-primary` 等宽。 */
function Field({ label, value }: { label: string; value: string }) {
   return (
      <div className="kv">
         <span className="kv__label">{label}</span>
         <span className="kv__value">{value}</span>
      </div>
   );
}

/**
 * 状态圆点。dsh 的规矩:圆点**自己带 accessible name**(`role="img"` +
 * `aria-label`),所以状态永远不只靠颜色 —— 色盲用户与读屏拿到的是一样的信息。
 */
function Dot({ ok, on, off }: { ok: boolean; on: string; off: string }) {
   return (
      <span
         className={`set-dot ${ok ? "set-dot--ok" : "set-dot--bad"}`}
         role="img"
         aria-label={ok ? on : off}
         title={ok ? on : off}
      />
   );
}

/** 来源层的展示名。agent 私有那层带上是谁的 —— 它可以有很多个。 */
function sourceLabel(src: McpSource): string {
   if (src.scope === "role") return `角色私有 · ${src.role}`;
   return src.scope === "global" ? "全局" : "项目";
}

/**
 * 一个 server 在界面上的**状态**。
 *
 * v1 的"绑定"概念随 agent 配置一起消失了(P-E4c):现在是"声明表 → 谁点名谁用",
 * 而角色的 MCP 面由 `agent.md` 的 `tools:` 里有没有 mcp 条目决定。所以这里显示的是
 * qi-mcp 真实有的那几个状态位。
 */
function bindLabel(src: McpSource, server: McpServerInfo): string {
   const bits: string[] = [];
   if (server.disabled) bits.push("已关闭");
   if (server.direct_tools) bits.push("直连注册");
   if (server.unknown_fields.length > 0)
      bits.push(`未识别字段 ${server.unknown_fields.length} 个`);
   if (bits.length === 0) bits.push(src.scope === "role" ? "跟随角色" : "走代理");
   return bits.join(" · ");
}

export function Settings({
   config,
   onChanged,
   onError,
   onClose,
   returnFocusTo,
   initialSection = "models",
}: {
   config: ConfigView | null;
   onChanged: () => void;
   /** 关闭浮层(`Esc`、右上角 ×、点遮罩都走它)。 */
   onClose: () => void;
   /**
    * 关掉时焦点回哪儿。**必须是那个触发器本身**(左栏底部「设置」行),
    * 不能指望 `document.activeElement`:macOS 上鼠标点按钮**不聚焦**,
    * 于是关闭时 `activeElement` 是 `<body>` —— 键盘用户得重新 Tab 穿过整个左栏。
    */
   returnFocusTo?: RefObject<HTMLElement | null>;
   /** 打开时落在哪一节(指令菜单的 `/agents`、`/settings` 要直接跳过去)。 */
   initialSection?: SettingsSectionKey;
   onError: (message: string) => void;
}) {
   const [section, setSection] = useState<SettingsSectionKey>(initialSection);
   const [agents, setAgents] = useState<AgentInfo[]>([]);
   const [skills, setSkills] = useState<SkillInfo[]>([]);
   const [extensions, setExtensions] = useState<string[]>([]);
   const [mcp, setMcp] = useState<McpList | null>(null);
   /** 老宿主没有 `/api/mcp` → 这一节降级显示,不把整页打成错误屏。 */
   const [mcpMissing, setMcpMissing] = useState(false);
   const [editing, setEditing] = useState<{
      provider: string;
      key: string;
   } | null>(null);
   const [removing, setRemoving] = useState<string | null>(null);

   const titleId = useId();
   const closeRef = useRef<HTMLButtonElement | null>(null);

   /**
    * 焦点归还:关闭后把焦点还给**打开它的那个触发器**
    *(dsh `SettingsPanel` 的焦点管理三条 = 进来聚焦关闭键 / `Esc` 关闭 / 关闭后归还,
    * 前两条在下面两个 effect 里)。这条必须用 `returnFocusTo` 而不是
    * `document.activeElement` —— 理由见 props 上的注释。
    */
   useEffect(() => {
      const previous = document.activeElement;
      return () => {
         if (returnFocusTo?.current) returnFocusTo.current.focus();
         else if (previous instanceof HTMLElement) previous.focus();
      };
   }, [returnFocusTo]);

   /* 进来聚焦关闭键(dsh 同款)。焦点落在面板里,也顺带给 `Esc` 一个可见的起点。 */
   useEffect(() => {
      closeRef.current?.focus();
   }, []);

   /**
    * `Esc` 关**最上面那一层**。
    *
    * 两个细节都不是可选的:
    *
    * 1. **捕获阶段**(第三个参数 `true`)。`ConfirmDialog` 自己也有 `Esc` 处理,
    *    而且它的回调**在 React 里**:React 18+ 会在离散事件里**同步冲刷**那次
    *    `setState` —— 于是当事件冒泡到 `document` 时,弹层已经卸载、我这个 effect
    *    已经被重建,读到的是"没有弹层"的新闭包,结果一个 `Esc` 把**两层一起关掉**
    *    (实测过一次:弹层与面板同时 `false`)。捕获阶段在 React 之前跑,
    *    读到的才是按键按下那一刻的层数。
    * 2. 分两层处理:有确认弹层就先关它,没有才关面板。
    */
   useEffect(() => {
      const onKeyDown = (event: KeyboardEvent) => {
         if (event.key !== "Escape") return;
         if (editing !== null) {
            setEditing(null);
            return;
         }
         if (removing !== null) {
            setRemoving(null);
            return;
         }
         onClose();
      };
      document.addEventListener("keydown", onKeyDown, true);
      return () => document.removeEventListener("keydown", onKeyDown, true);
   }, [onClose, editing, removing]);

   useEffect(() => {
      let alive = true;
      void (async () => {
         try {
            const [agentList, skillList, extensionList] = await Promise.all([
               api.agents(),
               api.skills(),
               api.extensions(),
            ]);
            if (!alive) return;
            setAgents(agentList.agents);
            setSkills(skillList.skills);
            setExtensions((extensionList as ExtensionList).extensions);
         } catch (err) {
            onError(err instanceof ApiError ? err.detail : String(err));
         }
      })();
      return () => {
         alive = false;
      };
   }, [onError]);

   /**
    * MCP 是**可选能力**:老宿主没这个端点(404)。单独一趟请求 + 单独处理失败
    * —— 与 App 里 `/api/workspaces` 的降级同一条规矩(docs/web.md §18.5):
    * 少一节内容比整页白屏好。
    */
   useEffect(() => {
      let alive = true;
      void (async () => {
         try {
            const list = await api.mcp();
            if (alive) setMcp(list);
         } catch {
            if (alive) setMcpMissing(true);
         }
      })();
      return () => {
         alive = false;
      };
   }, []);

   const runWrite = (action: () => Promise<void>) => {
      void (async () => {
         try {
            await action();
            onChanged();
         } catch (err) {
            onError(err instanceof ApiError ? err.detail : String(err));
         }
      })();
   };

   const authPath = config?.auth_file ?? "(未知)";
   const providers = config?.providers ?? [];
   const checks = config?.checks ?? [];
   /** 三处加起来声明了多少个 server(节标题里的那个数)。 */
   const mcpTotal = (mcp?.sources ?? []).reduce(
      (total, source) => total + source.servers.length,
      0,
   );
   /**
    * 项目层占用的名字。同名时项目覆盖全局(`mcp_name_index` 的后写覆盖),
    * 所以全局层那张卡上要标一下 —— 否则两行都写着"绑定:analyst",读者会以为
    * 两个都生效。
    */
   const projectNames = new Set(
      (mcp?.sources ?? [])
         .filter((source) => source.scope === "project")
         .flatMap((source) => source.servers.map((server) => server.name)),
   );

   return (
      <div className="set">
         {/* 遮罩照 dsh `.mask`:同一层 `bg-mask-1` + `--dsw-mask-blur`,
            点它关闭。`aria-hidden` 是因为"点外面关掉"有键盘等价物(`Esc`)。 */}
         <div className="set__mask" aria-hidden="true" onClick={onClose} />

         <div
            className="set__panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
         >
            <nav className="set__nav" aria-label="设置分区">
               <div className="set__nav-title" id={titleId}>
                  设置
               </div>
               <div className="set__nav-list">
                  {SECTIONS.map((item) => (
                     <button
                        key={item.key}
                        type="button"
                        className="set__nav-cell"
                        aria-current={section === item.key ? "true" : undefined}
                        onClick={() => setSection(item.key)}
                     >
                        <span className="set__nav-icon">{item.icon}</span>
                        <span className="set__nav-label">{item.label}</span>
                     </button>
                  ))}
               </div>
            </nav>

            <div className="set__content">
               <div className="set__head">
                  <button
                     ref={closeRef}
                     type="button"
                     className="set__close"
                     onClick={onClose}
                  >
                     <IconCloseOutline16 size={14} />
                     <span className="set__sr">关闭设置</span>
                  </button>
               </div>

               <div className="set__options">
                  {section === "models" ? (
                     <Section
                        title="模型与凭证"
                        intro={
                           <>
                              来自 <code>models.json</code> + <code>auth.json</code>
                              ;凭证只显示尾 4 位。
                           </>
                        }
                     >
                        {providers.length === 0 ? (
                           <p className="set-sec__empty">
                              没有读到任何 provider。先跑 <code>qi init</code>。
                           </p>
                        ) : (
                           <ul className="set-cards">
                              {providers.map((provider) => (
                                 <li className="set-card" key={provider.name}>
                                    <div className="set-card__head">
                                       <span className="set-card__name">
                                          {provider.name}
                                       </span>
                                       <Dot
                                          ok={provider.credential_ok}
                                          on={`${provider.name} 已配置凭证`}
                                          off={`${provider.name} 缺少凭证`}
                                       />
                                       <span className="set-card__actions">
                                          <button
                                             type="button"
                                             className="set-act"
                                             onClick={() =>
                                                setEditing({
                                                   provider: provider.name,
                                                   key: "",
                                                })
                                             }
                                          >
                                             设置凭证
                                          </button>
                                          {provider.credential_source ===
                                          "auth" ? (
                                             <button
                                                type="button"
                                                className="set-act set-act--danger"
                                                onClick={() =>
                                                   setRemoving(provider.name)
                                                }
                                             >
                                                清除
                                             </button>
                                          ) : null}
                                       </span>
                                    </div>
                                    <div className="set-card__fields">
                                       <Field
                                          label="baseUrl"
                                          value={provider.base_url ?? "(内置)"}
                                       />
                                       <Field
                                          label="api"
                                          value={provider.api ?? "—"}
                                       />
                                       <Field
                                          label="模型"
                                          value={String(provider.models)}
                                       />
                                       <Field
                                          label="凭证"
                                          value={
                                             provider.credential_ok
                                                ? provider.credential_masked ||
                                                  provider.credential_source
                                                : provider.credential_source
                                          }
                                       />
                                    </div>
                                 </li>
                              ))}
                           </ul>
                        )}

                        <div className="set-fields">
                           <Field
                              label="默认模型"
                              value={config?.default_model ?? "(未配置)"}
                           />
                           {config?.router_model ? (
                              <Field
                                 label="分派模型"
                                 value={config.router_model}
                              />
                           ) : null}
                           <Field
                              label="配置文件"
                              value={
                                 (config?.config_files ?? []).join("\n") ||
                                 "未找到"
                              }
                           />
                           <Field
                              label="设置文件"
                              value={
                                 (config?.settings_files ?? []).join("\n") ||
                                 "未找到"
                              }
                           />
                        </div>
                     </Section>
                  ) : null}

                  {section === "agents" ? (
                     <Section
                        title={`Agent(${agents.length})`}
                        intro={
                           <>
                              优先级:项目 <code>.qi/agents/</code> &gt; 用户{" "}
                              <code>~/.qi/agent/agents/</code> &gt; 包内置。tools
                              为 <code>*</code> 时展开成实际清单。
                           </>
                        }
                     >
                        {agents.length === 0 ? (
                           <p className="set-sec__empty">
                              没有发现 agent。没有装专职角色时,框架只用内置的
                              general 兜底。
                           </p>
                        ) : (
                           <ul className="set-cards">
                              {agents.map((agent) => (
                                 <li className="set-card" key={agent.name}>
                                    <div className="set-card__head">
                                       <span className="set-card__name">
                                          {agent.name}
                                       </span>
                                       <span className="set-card__route">
                                          {agent.name}
                                       </span>
                                       <span className="set-card__tag">
                                          {agent.source}
                                       </span>
                                    </div>
                                    {agent.description ? (
                                       <p className="set-card__desc">
                                          {agent.description}
                                       </p>
                                    ) : null}
                                    <div className="set-card__fields">
                                       <Field
                                          label="工具"
                                          value={agent.tools.join(", ") || "(无)"}
                                       />
                                       <Field
                                          label="MCP"
                                          value={`私有 ${agent.mcp} 个`}
                                       />
                                       {agent.model ? (
                                          <Field label="模型" value={agent.model} />
                                       ) : null}
                                       {agent.path ? (
                                          <Field label="位置" value={agent.path} />
                                       ) : null}
                                    </div>
                                 </li>
                              ))}
                           </ul>
                        )}
                     </Section>
                  ) : null}

                  {section === "skills" ? (
                     <Section
                        title={`技能(${skills.length})`}
                        intro={
                           <>
                              渐进披露:只有描述进系统提示词,正文由 agent 需要时用{" "}
                              <code>read</code> 读全文。
                           </>
                        }
                     >
                        {skills.length === 0 ? (
                           <p className="set-sec__empty">
                              没有发现技能。放在 <code>~/.qi/agent/skills/</code>
                              、项目的 <code>.qi/skills/</code>,或跨工具的{" "}
                              <code>.agents/skills/</code>。
                           </p>
                        ) : (
                           <table className="set-table">
                              <thead>
                                 <tr>
                                    <th>名称</th>
                                    <th>来源</th>
                                    <th>描述</th>
                                 </tr>
                              </thead>
                              <tbody>
                                 {skills.map((skill) => (
                                    <tr key={skill.path || skill.name}>
                                       <td className="set-mono">{skill.name}</td>
                                       <td>{skill.source}</td>
                                       <td>{skill.description}</td>
                                    </tr>
                                 ))}
                              </tbody>
                           </table>
                        )}
                     </Section>
                  ) : null}

                  {section === "mcp" ? (
                     <Section
                        title={`MCP(${mcpTotal})`}
                        intro={
                           <>
                              三处声明:全局 <code>~/.qi/agent/mcp.json</code>
                              、项目 <code>.qi/mcp.json</code>
                              、以及各 agent 目录里的私有 <code>mcp.json</code>。
                              v1 只做解析与门控(没有 MCP client,见 PLAN.md),
                              所以这里只有"声明"、没有"已连接";全局/项目里的 server
                              必须被某个 agent 的 <code>mcp_servers</code> 声明才算绑上,
                              同名时<strong>项目覆盖全局</strong>。
                              <code>env</code> / <code>headers</code> 只显示键名。
                           </>
                        }
                     >
                        {mcpMissing || mcp?.unavailable ? (
                           <p className="set-sec__empty">
                              没装 <code>qi-mcp</code> 扩展,读不到 MCP 声明。
                           </p>
                        ) : mcp === null ? (
                           <p className="set-sec__empty">读取中…</p>
                        ) : (
                           <div className="mcp">
                              {mcp.sources.map((src) => (
                                 <div
                                    className="mcp-src"
                                    key={`${src.scope}:${src.role}`}
                                 >
                                    <div className="mcp-src__head">
                                       <span className="set-card__tag">
                                          {sourceLabel(src)}
                                       </span>
                                       <span className="mcp-src__path">
                                          {src.path}
                                       </span>
                                    </div>
                                    {src.servers.length === 0 ? (
                                       <p className="set-sec__empty">
                                          {src.exists
                                             ? "文件在,但没有声明任何 server。"
                                             : "这里没有 mcp.json。"}
                                       </p>
                                    ) : (
                                       <ul className="set-cards">
                                          {src.servers.map((server) => (
                                             <li
                                                className="set-card"
                                                key={server.name}
                                             >
                                                <div className="set-card__head">
                                                   <span className="set-card__name">
                                                      {server.name}
                                                   </span>
                                                   {server.transport ? (
                                                      <span className="set-card__tag">
                                                         {server.transport}
                                                      </span>
                                                   ) : null}
                                                   {src.scope === "global" &&
                                                   projectNames.has(
                                                      server.name,
                                                   ) ? (
                                                      <span className="set-card__tag">
                                                         被项目同名覆盖
                                                      </span>
                                                   ) : null}
                                                   <span className="mcp-bind">
                                                      {bindLabel(src, server)}
                                                   </span>
                                                </div>
                                                <div className="set-card__fields">
                                                   <Field
                                                      label="目标"
                                                      value={
                                                         server.url ||
                                                         "(未声明)"
                                                      }
                                                   />
                                                   {server.env_keys.length >
                                                   0 ? (
                                                      <Field
                                                         label="env 键名"
                                                         value={server.env_keys.join(
                                                            ", ",
                                                         )}
                                                      />
                                                   ) : null}
                                                   {server.header_keys.length >
                                                   0 ? (
                                                      <Field
                                                         label="headers 键名"
                                                         value={server.header_keys.join(
                                                            ", ",
                                                         )}
                                                      />
                                                   ) : null}
                                                </div>
                                             </li>
                                          ))}
                                       </ul>
                                    )}
                                 </div>
                              ))}
                           </div>
                        )}
                     </Section>
                  ) : null}

                  {section === "extensions" ? (
                     <Section
                        title={`扩展(${extensions.length})`}
                        intro={
                           <>
                              两条通道:pip entry point <code>qi.extensions</code> +
                              本地目录:全局{" "}
                              <code>
                                 ~/.qi/agent/extensions/&lt;名&gt;/extension.py
                              </code>
                              ,项目级是{" "}
                              <code>&lt;项目&gt;/.qi/extensions/&lt;名&gt;/</code>
                              (需信任)。
                           </>
                        }
                     >
                        {extensions.length === 0 ? (
                           <p className="set-sec__empty">未装载任何扩展。</p>
                        ) : (
                           <ul className="set-rows">
                              {extensions.map((name) => (
                                 <li className="set-row" key={name}>
                                    <Dot
                                       ok
                                       on={`${name} 已装载`}
                                       off={`${name} 未装载`}
                                    />
                                    <span className="set-mono">{name}</span>
                                 </li>
                              ))}
                           </ul>
                        )}
                     </Section>
                  ) : null}

                  {section === "sessions" ? (
                     <Section
                        title="会话与路径"
                        intro="会话是 JSONL、每会话一个文件;settings.json 的 sessionDir 可覆盖目录。"
                     >
                        <div className="set-fields">
                           <Field
                              label="会话目录"
                              value={config?.session_dir ?? "(未知)"}
                           />
                           <Field label="凭证文件" value={authPath} />
                        </div>
                     </Section>
                  ) : null}

                  {section === "diagnostics" ? (
                     <Section
                        title="诊断"
                        intro={
                           <>
                              等价于 <code>qi doctor</code> 的核心检查项。
                           </>
                        }
                     >
                        {checks.length === 0 ? (
                           <p className="set-sec__empty">宿主没给检查项。</p>
                        ) : (
                           <ul className="set-rows">
                              {checks.map((check) => (
                                 <li className="set-row" key={check.name}>
                                    <Dot
                                       ok={check.ok}
                                       on={`${check.name} 通过`}
                                       off={`${check.name} 未通过`}
                                    />
                                    <span className="set-row__name">
                                       {check.name}
                                    </span>
                                    <span className="set-row__detail">
                                       {check.detail}
                                    </span>
                                 </li>
                              ))}
                           </ul>
                        )}
                     </Section>
                  ) : null}
               </div>
            </div>
         </div>

         {editing ? (
            <ConfirmDialog
               title={`写入 ${editing.provider} 的凭证?`}
               rows={[
                  { label: "provider", value: editing.provider },
                  { label: "目标文件", value: authPath },
                  {
                     label: "密钥",
                     value: editing.key ? `…${editing.key.slice(-4)}` : "(空)",
                  },
               ]}
               warning="会覆盖该 provider 现有凭证;文件权限置为 0600。"
               confirmLabel="确认写入"
               onCancel={() => setEditing(null)}
               onConfirm={() => {
                  const target = editing;
                  setEditing(null);
                  if (!target.key) return;
                  runWrite(() => api.setAuth(target.provider, target.key));
               }}
            >
               <input
                  className="dialog__input"
                  value={editing.key}
                  autoFocus
                  aria-label="API key"
                  placeholder="粘贴 API key"
                  onChange={(event) =>
                     setEditing({ ...editing, key: event.target.value })
                  }
               />
            </ConfirmDialog>
         ) : null}

         {removing ? (
            <ConfirmDialog
               title={`清除 ${removing} 的凭证?`}
               rows={[
                  { label: "provider", value: removing },
                  { label: "目标文件", value: authPath },
               ]}
               warning="清除后该 provider 会变成缺密钥状态。"
               danger
               confirmLabel="确认清除"
               onCancel={() => setRemoving(null)}
               onConfirm={() => {
                  const target = removing;
                  setRemoving(null);
                  runWrite(() => api.deleteAuth(target));
               }}
            />
         ) : null}
      </div>
   );
}
