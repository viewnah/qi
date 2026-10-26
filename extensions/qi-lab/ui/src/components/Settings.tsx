/**
 * 设置面 —— **形状照 dsh 的设置面板**(`ui-settings-general/SettingsRoot.module.css`,
 * figma 501:29947):全屏遮罩 + 居中面板(800 宽、`min(800px, 100vh - 48px)` 高、
 * r32),左导轨 188(标题行 16/500 + 40px 导航格 + 16px 图标),右内容列
 * (54px 头 + 可滚动的 options,内容列 max-width 1440 —— 资产卡片网格按宽度自适应列数)。
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
 * **只管全局资产**:Agent / 技能 / MCP / A2A / 知识库五节里的项目级那一层(跟着
 * 当前 cwd 仓库走的 `.qi/…`、`.agents/skills/`)不展示、也不写 —— 过滤口径集中在
 * `state/scope.ts`。后端契约不变(CLI 与其它宿主仍可读写项目层)。
 *
 * **唯一的例外是「应用助手」**:它本来就只有项目级(`.qi/assistants.json`),没有全局层，
 * 所以这一节直接写当前 cwd 的那份文件,页头把绝对路径写出来。
 *
 * 唯一可写动作是**凭证**:必须过二次确认层(显示目标文件绝对路径 + 掩码值),
 * 请求再带 `X-Qi-Confirm: yes`(后端也拦,双保险)。
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
   globalA2AList,
   globalMcpList,
   isGlobalAgent,
   isGlobalSkill,
   skillDeleteScope,
} from "../state/scope";
import type { ReactNode, RefObject } from "react";
import { api, ApiError } from "../api/client";
import type {
   A2AEntryInfo,
   A2AEntryInput,
   A2AList,
   A2ASource,
   AgentInfo,
   ConfigView,
   ExtensionList,
   McpList,
   McpPatchBody,
   McpServerInfo,
   McpSource,
   McpWriteBody,
   SkillInfo,
} from "../api/types";
import { ConfirmDialog } from "./ConfirmDialog";
import { StorePanel } from "./StorePanel";
import { KbPanel } from "./KbPanel";
import { AssistantsPanel } from "./AssistantsPanel";
import {
   A2ADeleteDialog,
   A2ADialog,
   A2ASection,
} from "./A2APanel";
import {
   AgentDeleteDialog,
   agentDeleteBody,
   agentScopeOf,
} from "./AgentDelete";
import {
   McpDeleteDialog,
   McpDialog,
   McpSection,
   type McpTarget,
} from "./McpPanel";
import {
   IconAgentPresetOutline16,
   IconCloseOutline16,
   IconDataOutline16,
   IconDatabaseOutline16,
   IconFolderOpen16,
   IconGaugeOutline16,
   IconLinkOutline16,
   IconPersonalizationOutline16,
   IconShareOutline16,
   IconSkillOutline16,
} from "./icons";

export type SettingsSectionKey =
   | "models"
   | "agents"
   | "assistants"
   | "skills"
   | "store"
   | "kb"
   | "mcp"
   | "a2a"
   | "extensions"
   | "sessions"
   | "diagnostics";

/**
 * 分节表。图标**按节选语义**,与 dsh `SettingsRoot.tsx` 的 `navIcon()` 同规矩
 * (它那边 models → Data、agent-presets → AgentPreset、plugins → Personalization,
 * 未知 id 回落 Settings)。qi 的节名不同,所以"技能/诊断"两节是**从 dsh 的字形库里
 * 挑表意对的**(Skill / Gauge),不是照抄它的分节表 —— 它的分节表里没有这两节。
 */
/** 新建 provider 可选的 api 形态(与宿主 `config.SUPPORTED_APIS` 逐字同一份)。 */
const PROVIDER_APIS = [
   "openai-completions",
   "openai-responses",
   "anthropic-messages",
   "google-generative-ai",
] as const;

const SECTIONS: {
   key: SettingsSectionKey;
   label: string;
   icon: ReactNode;
}[] = [
   { key: "models", label: "模型", icon: <IconDataOutline16 size={16} /> },
   { key: "agents", label: "Agent", icon: <IconAgentPresetOutline16 size={16} /> },
   { key: "assistants", label: "应用助手", icon: <IconAgentPresetOutline16 size={16} /> },
   { key: "skills", label: "技能", icon: <IconSkillOutline16 size={16} /> },
   { key: "mcp", label: "MCP", icon: <IconLinkOutline16 size={16} /> },
   { key: "a2a", label: "A2A", icon: <IconShareOutline16 size={16} /> },
   { key: "kb", label: "知识库", icon: <IconDatabaseOutline16 size={16} /> },
   {
      key: "extensions",
      label: "扩展",
      icon: <IconPersonalizationOutline16 size={16} />,
   },
   // 商店排在「扩展」之后:它本身就是一个扩展(qi-store)提供的能力,
   // 放在扩展解释完之后出现,读起来是"扩展里还有一个叫商店的"
   { key: "store", label: "商店", icon: <IconShareOutline16 size={16} /> },
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

export function Settings({
   config,
   onChanged,
   onError,
   onClose,
   returnFocusTo,
   initialSection = "models",
   mcpWrite = false,
   a2aWrite = false,
   agentWrite = false,
   assistantWrite = false,
   providerWrite = false,
   storeWrite = false,
   kbWrite = false,
   onKbChanged,
   skillWrite = false,
   onAgentsChanged,
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
   /**
    * 宿主支不支持**写** MCP 声明(`/api/meta` 的 `mcp_write`)。老宿主 → 那一节只读:
    * 少一排按钮比点了才吃 404 好(与"没有 /api/mcp 就降级"同一条规矩)。
    */
   mcpWrite?: boolean;
   /**
    * 宿主支不支持改写 A2A 条目(`/api/meta` 的 `a2a_write`)。老宿主 → 那一节只读
    * (读端点与探测一直在)。
    */
   a2aWrite?: boolean;
   /**
    * 宿主支不支持**删角色**(`/api/meta` 的 `agent_write`)。同上:没有它 → Agent 那一节
    * 只读展示(读端点一直在)。
    */
   agentWrite?: boolean;
   /**
    * 宿主支不支持**应用助手目录的写端点**(`/api/meta` 的 `assistant_write`)。
    * 没有它 → 这一节只读(读端点仍在)。
    */
   assistantWrite?: boolean;
   /**
    * 宿主支不支持**新建 provider**(`/api/meta` 的 `provider_write`)。没有它 →
    * 「添加 Provider」只保留“登录已有 provider”那条路(写凭证的端点一直在)。
    */
   providerWrite?: boolean;
   /**
    * 宿主真的装上了 qi-store(`/api/meta` 的 `store_write`)。没有它 → 商店那一节
    * 只读展示(读端点一直在),不摆"点了才 404"的安装键。
    */
   storeWrite?: boolean;
   kbWrite?: boolean;
   onKbChanged?: () => void;
   /** 宿主支持写技能端点(`/api/meta` 的 `skill_write`)。没有它 → 「重新扫描」不摆。 */
   skillWrite?: boolean;
   /**
    * 角色删掉之后回调:上层(左下角的角色菜单)持的是**另一份**清单,不通知它就会
    * 出现"菜单里还留着一个已经不存在的角色"。`onChanged` 不管这个 —— 它只回读 `/api/config`。
    */
   onAgentsChanged?: () => void;
   onError: (message: string) => void;
}) {
   const [section, setSection] = useState<SettingsSectionKey>(initialSection);
   const [agents, setAgents] = useState<AgentInfo[]>([]);
   const [skills, setSkills] = useState<SkillInfo[]>([]);
   /** 「重新扫描」进行中 —— 只锁那一颗键,不锁整页 */
   const [skillsBusy, setSkillsBusy] = useState(false);
   /** 技能删除:待删的那个、错误与在写标记(错误**报在弹层里**)。 */
   const [skillRemoving, setSkillRemoving] = useState<SkillInfo | null>(null);
   const [skillError, setSkillError] = useState<string | null>(null);
   const [skillBusy, setSkillBusy] = useState(false);
   const [extensions, setExtensions] = useState<string[]>([]);
   const [mcp, setMcp] = useState<McpList | null>(null);
   /** 老宿主没有 `/api/mcp` → 这一节降级显示,不把整页打成错误屏。 */
   const [mcpMissing, setMcpMissing] = useState(false);
   /** A2A 条目(两层 a2a.json + 合并后的生效视图)。老宿主 / 没装 qi-a2a → 空态。 */
   const [a2a, setA2a] = useState<A2AList | null>(null);
   const [a2aMissing, setA2aMissing] = useState(false);
   /** 正在增 / 改的那个条目(`null` = 没开弹层;`entry === null` = 新建)。 */
   const [a2aTarget, setA2aTarget] = useState<{
      scope: A2ASource["scope"];
      path: string;
      entry: A2AEntryInfo | null;
   } | null>(null);
   const [a2aRemoving, setA2aRemoving] = useState<{
      src: A2ASource;
      entry: A2AEntryInfo;
   } | null>(null);
   const [a2aError, setA2aError] = useState<string | null>(null);
   const [a2aBusy, setA2aBusy] = useState(false);
   /** 正在增 / 改的那处声明表(`null` = 没开弹层);`server === null` = 新建。 */
   const [mcpTarget, setMcpTarget] = useState<McpTarget | null>(null);
   const [mcpRemoving, setMcpRemoving] = useState<{
      src: McpSource;
      server: McpServerInfo;
   } | null>(null);
   /**
    * 写操作的错误与在写标记 —— **留在弹层里报**。顶上那条 notice 被遮罩挡着,
    * 弹层不关就看不见,等于"点了没反应"。
    */
   const [mcpError, setMcpError] = useState<string | null>(null);
   const [mcpBusy, setMcpBusy] = useState(false);
   /**
    * 应用助手面板里压着一层弹层(新建 / 编辑 / 删除 / 导入)。
    * 它由面板自己管;外壳只知道"有弹层",`Esc` 时先让它关,别把整个设置一起关掉。
    */
   const [assistantModal, setAssistantModal] = useState(false);
   /** 角色删除:待删的那个、错误与在写标记(错误**报在弹层里** —— 顶部 notice 被遮罩挡着)。 */
   const [agentRemoving, setAgentRemoving] = useState<AgentInfo | null>(null);
   const [agentError, setAgentError] = useState<string | null>(null);
   const [agentBusy, setAgentBusy] = useState(false);
   const [editing, setEditing] = useState<{
      provider: string;
      key: string;
   } | null>(null);
   const [removing, setRemoving] = useState<string | null>(null);
   /** 添加 Provider:弹层开着没有、走“登录已有”还是“新建自定义”。 */
   const [providerAdding, setProviderAdding] = useState(false);
   const [providerMode, setProviderMode] = useState<"existing" | "custom">("existing");
   const [providerPick, setProviderPick] = useState("");
   const [providerDraft, setProviderDraft] = useState({
      name: "",
      baseUrl: "",
      api: "openai-completions",
      key: "",
      models: "",
   });
   const [providerError, setProviderError] = useState<string | null>(null);
   const [providerBusy, setProviderBusy] = useState(false);

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
         if (assistantModal) return;      // 面板自己的 ConfirmDialog 会处理这一下
         if (editing !== null) {
            setEditing(null);
            return;
         }
         if (providerAdding) {
            setProviderAdding(false);
            setProviderError(null);
            return;
         }
         if (removing !== null) {
            setRemoving(null);
            return;
         }
         if (skillRemoving !== null) {
            setSkillRemoving(null);
            setSkillError(null);
            return;
         }
         if (mcpTarget !== null) {
            setMcpTarget(null);
            setMcpError(null);
            return;
         }
         if (mcpRemoving !== null) {
            setMcpRemoving(null);
            setMcpError(null);
            return;
         }
         if (agentRemoving !== null) {
            setAgentRemoving(null);
            setAgentError(null);
            return;
         }
         onClose();
      };
      document.addEventListener("keydown", onKeyDown, true);
      return () => document.removeEventListener("keydown", onKeyDown, true);
   }, [onClose, assistantModal, editing, providerAdding, removing, skillRemoving,
       mcpTarget, mcpRemoving, agentRemoving]);

   /**
    * 技能清单**每次进这一节都重读**。
    *
    * 商店里装/卸技能会改磁盘与 `runtime.top_skills`(后端已在写端点里 `refresh_skills()`),
    * 而本组件只在**挂载**时拉过一次清单 —— 从商店切回「技能」时看到的是旧快照。
    * 一条请求换“装完立刻能看到”,值。
    */
   const reloadSkills = useCallback(async () => {
      setSkillsBusy(true);
      try {
         const list = await api.reloadSkills();
         setSkills(list.skills);
      } catch (err) {
         onError(err instanceof ApiError ? err.detail : String(err));
      } finally {
         setSkillsBusy(false);
      }
   }, [onError]);

   useEffect(() => {
      if (section !== "skills") return;
      let alive = true;
      void api
         .skills()
         .then((list) => {
            if (alive) setSkills(list.skills);
         })
         .catch(() => undefined);        // 读失败保持旧清单:这一节不该把整页打红
      return () => {
         alive = false;
      };
   }, [section]);

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
    * —— 与 App 里 \`/api/workspaces\` 的降级同一条规矩(design/web.md §18.5):
    * 少一节内容比整页白屏好。
    *
    * **每次进这一节重读**:商店里装的 MCP 不在本页写路径上,只靠挂载时那一次请求
    * 会让"装完了这节也看不到"。
    */
   useEffect(() => {
      if (section !== "mcp") return;
      let alive = true;
      void (async () => {
         try {
            const list = await api.mcp();
            if (alive) {
               setMcp(list);
               setMcpMissing(false);
            }
         } catch {
            if (alive) setMcpMissing(true);
         }
      })();
      return () => {
         alive = false;
      };
   }, [section]);

   /**
    * A2A 同上:老宿主没 \`/api/a2a\` 就只少一节,不白屏。同样**每次进这一节重读**
    * (商店装 A2A 条目走的是另一条写路径)。
    */
   useEffect(() => {
      if (section !== "a2a") return;
      let alive = true;
      void (async () => {
         try {
            const list = await api.a2a();
            if (alive) {
               setA2a(list);
               setA2aMissing(false);
            }
         } catch {
            if (alive) setA2aMissing(true);
         }
      })();
      return () => {
         alive = false;
      };
   }, [section]);

   /** 写完再读一遍:界面该显示宿主实际读到的样子(与 MCP 同一条规矩)。 */
   const refreshA2A = async () => {
      setA2a(await api.a2a());
   };

   /**
    * 新建 / 编辑:**成功才关表单** —— 失败时输入还在,不用从头填一遍。
    *
    * 新建与编辑的体不一样(整条 vs 只动过的键),由弹层自己判好后分开传。
    */
   const submitA2A = (body: A2AEntryInput | Partial<A2AEntryInput>) => {
      if (!a2aTarget) return;
      setA2aError(null);
      setA2aBusy(true);
      void (async () => {
         try {
            if (a2aTarget.entry === null) {
               await api.createA2A({
                  scope: a2aTarget.scope,
                  entry: body as A2AEntryInput,
               });
            } else {
               await api.patchA2A({
                  scope: a2aTarget.scope,
                  name: a2aTarget.entry.name,
                  patch: body,
               });
            }
            await refreshA2A();
            setA2aTarget(null);
         } catch (err) {
            setA2aError(err instanceof ApiError ? err.detail : String(err));
         } finally {
            setA2aBusy(false);
         }
      })();
   };

   const removeA2A = async (target: { src: A2ASource; entry: A2AEntryInfo }) => {
      setA2aError(null);
      setA2aBusy(true);
      try {
         await api.deleteA2A({ scope: target.src.scope, name: target.entry.name });
         await refreshA2A();
         setA2aRemoving(null);
      } catch (err) {
         setA2aError(err instanceof ApiError ? err.detail : String(err));
      } finally {
         setA2aBusy(false);
      }
   };

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

   /**
    * 写完**再读一遍**:界面该显示宿主实际读到的样子,而不是"我以为写进去的样子"。
    * (三个写端点其实也返回 `McpList`,但按"写与读同源"的规矩,刷新一律走 GET。)
    */
   const refreshMcp = async () => {
      setMcp(await api.mcp());
   };

   const mcpFailure = (err: unknown) =>
      err instanceof ApiError ? err.detail : String(err);

   /** 新建 / 编辑:**成功才关表单** —— 失败时输入还在,不用从头填一遍。 */
   const submitMcp = (body: McpWriteBody | McpPatchBody) => {
      setMcpError(null);
      setMcpBusy(true);
      void (async () => {
         try {
            if ("server" in body) await api.createMcp(body);
            else await api.patchMcp(body);
            await refreshMcp();
            setMcpTarget(null);
         } catch (err) {
            setMcpError(mcpFailure(err));
         } finally {
            setMcpBusy(false);
         }
      })();
   };

   /** 删一层声明(同名的下层会露出来,覆盖关系没变)—— 确认层里把目标文件写全。 */
   const removeMcp = async (target: {
      src: McpSource;
      server: McpServerInfo;
   }) => {
      setMcpError(null);
      setMcpBusy(true);
      try {
         await api.deleteMcp({
            scope: target.src.scope,
            ...(target.src.role ? { role: target.src.role } : {}),
            name: target.server.name,
         });
         await refreshMcp();
         setMcpRemoving(null);
      } catch (err) {
         setMcpError(mcpFailure(err));
      } finally {
         setMcpBusy(false);
      }
   };

   /**
    * 删角色(**整目录**,含它自己的 `mcp.json`)—— **成功才关弹层**:留在弹层里报错
    * 才看得见(顶部 notice 被遮罩挡着)。写完再读一遍(GET):与 MCP/技能同一条
    * "界面显示宿主实际读到的样子"的规矩。
    */
   const removeAgent = (agent: AgentInfo) => {
      setAgentError(null);
      setAgentBusy(true);
      void (async () => {
         try {
            const body = agentDeleteBody(agent);
            if (body === null) return;             // 不可删的层压根不该有那个键
            await api.deleteAgent(body);
            setAgents((await api.agents()).agents);
            onAgentsChanged?.();
            setAgentRemoving(null);
         } catch (err) {
            setAgentError(err instanceof ApiError ? err.detail : String(err));
         } finally {
            setAgentBusy(false);
         }
      })();
   };

   /**
    * 删一个技能(**整目录**,`SKILL.md` 与 references/ 等附属文件一起) ——
    * **成功才关弹层**:留在弹层里报错才看得见。端点在删完后会重扫 runtime 缓存,
    * 所以下一次对话就不会再看见它。
    */
   const removeSkill = (skill: SkillInfo) => {
      const scope = skillDeleteScope(skill);
      if (scope === null) return;              // 不可删的层压根不该有那个键
      setSkillError(null);
      setSkillBusy(true);
      void (async () => {
         try {
            const list = await api.deleteSkill({ scope, name: skill.name });
            setSkills(list.skills);
            setSkillRemoving(null);
         } catch (err) {
            setSkillError(err instanceof ApiError ? err.detail : String(err));
         } finally {
            setSkillBusy(false);
         }
      })();
   };

   /**
    * 打开「添加 Provider」:默认落在第一个**未登录**的已知 provider 上;
    * 一个都没有时直接进自定义表单。
    */
   const openProviderAdd = () => {
      const first = (config?.providers ?? []).find((p) => !p.credential_ok)?.name ?? "";
      setProviderMode(first || !providerWrite ? "existing" : "custom");
      setProviderPick(first);
      setProviderDraft({ name: "", baseUrl: "", api: "openai-completions", key: "", models: "" });
      setProviderError(null);
      setProviderAdding(true);
   };

   /** 两条路:给已有 provider 配凭证(写 auth.json)/ 新建一个用户级 provider(写 models.json)。 */
   const submitProvider = () => {
      setProviderError(null);
      const key = providerDraft.key.trim();
      if (providerMode === "existing") {
         if (!providerPick || !key) return;
         setProviderBusy(true);
         void (async () => {
            try {
               await api.setAuth(providerPick, key);
               setProviderAdding(false);
               setProviderDraft((draft) => ({ ...draft, key: "" }));
               onChanged();
            } catch (err) {
               setProviderError(err instanceof ApiError ? err.detail : String(err));
            } finally {
               setProviderBusy(false);
            }
         })();
         return;
      }

      const name = providerDraft.name.trim();
      const baseUrl = providerDraft.baseUrl.trim();
      const models = providerDraft.models
         .split(/[\n,]/)
         .map((item) => item.trim())
         .filter(Boolean);
      if (!name || !baseUrl || !key || models.length === 0) return;
      setProviderBusy(true);
      void (async () => {
         try {
            await api.addProvider({
               name,
               base_url: baseUrl,
               api: providerDraft.api,
               api_key: key,
               models,
            });
            setProviderAdding(false);
            setProviderDraft({ name: "", baseUrl: "", api: "openai-completions", key: "", models: "" });
            onChanged();
         } catch (err) {
            setProviderError(err instanceof ApiError ? err.detail : String(err));
         } finally {
            setProviderBusy(false);
         }
      })();
   };

   const authPath = config?.auth_file ?? "(未知)";
   const providers = config?.providers ?? [];
   /** 没登录的 provider 不在主列表里摆 —— 它们只能从「添加 Provider」进。 */
   const visibleProviders = providers.filter((provider) => provider.credential_ok);
   const hiddenProviders = providers.filter((provider) => !provider.credential_ok);
   /** 「添加 Provider」弹层里的模型 id(回车 / 逗号分隔)。 */
   const providerModels = providerDraft.models
      .split(/[\n,]/)
      .map((item) => item.trim())
      .filter(Boolean);
   const providerCanSubmit =
      providerMode === "existing"
         ? Boolean(providerPick && providerDraft.key.trim())
         : Boolean(
              providerDraft.name.trim() &&
              /^https?:\/\//.test(providerDraft.baseUrl.trim()) &&
              providerDraft.key.trim() &&
              providerModels.length > 0,
           );
   const checks = config?.checks ?? [];
   /*
    * 这个控制台只管**全局(用户级)资产**:项目级那一层(跟着当前 cwd 仓库走的
    * `.qi/…`、`.agents/skills/`)一律不摆 —— 口径集中在 `state/scope.ts`。后端契约
    * 不变,只是“设置页不显示 / 不写”它们。
    */
   const visibleAgents = agents.filter(isGlobalAgent);
   const visibleSkills = skills.filter(isGlobalSkill);
   const visibleMcp = globalMcpList(mcp);
   const visibleA2a = globalA2AList(a2a);
   /** 全局层加起来声明了多少个 server(节标题里的那个数)。 */
   const mcpTotal = (visibleMcp?.sources ?? []).reduce(
      (total, source) => total + source.servers.length,
      0,
   );
   /** 全局层 a2a.json 里有多少条目。 */
   const a2aTotal = (visibleA2a?.sources ?? []).reduce(
      (total, source) => total + source.entries.length,
      0,
   );
   return (
      <div className="set">
         {/* 设置是**整页**而不是弹层:它要与工作区并列(左栏、侧栏都让位),
            所以没有遮罩、没有"点外面关掉"—— 退出只走 `Esc` 与右上角的 ×。
            `aria-modal` 也去掉了:它不再是覆盖在内容之上的一层。 */}
         <div className="set__panel" aria-labelledby={titleId}>
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
                        title={`模型与凭证(${visibleProviders.length})`}
                        intro={
                           <>
                              只列<strong>已登录</strong>的 provider;每个 provider 下面
                              是它的模型。凭证只显示尾 4 位,没登录的从「添加 Provider」进。
                           </>
                        }
                     >
                        <div className="set-skill__bar">
                           <button
                              type="button"
                              className="btn"
                              onClick={openProviderAdd}
                           >
                              添加 Provider
                           </button>
                           <span className="set-skill__hint">
                              登录已有 provider(写 auth.json),或新建一个(写 models.json)
                           </span>
                        </div>

                        {visibleProviders.length === 0 ? (
                           <p className="set-sec__empty">
                              没有已登录的 provider。点「添加 Provider」登录一个,
                              或新建一个。
                           </p>
                        ) : (
                           <div className="model-groups">
                              {visibleProviders.map((provider) => {
                                 const ids = provider.model_ids ?? [];
                                 return (
                                    <div
                                       className="set-card model-group"
                                       key={provider.name}
                                    >
                                       <div className="set-card__head">
                                          <span
                                             className="set-card__name"
                                             title={provider.name}
                                          >
                                             {provider.name}
                                          </span>
                                          {provider.label ? (
                                             <span className="model-group__label">
                                                {provider.label}
                                             </span>
                                          ) : null}
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
                                             label="凭证"
                                             value={
                                                provider.credential_masked ||
                                                provider.credential_source
                                             }
                                          />
                                       </div>
                                       <ul className="model-group__models">
                                          {ids.length === 0 ? (
                                             <li className="model-row model-row--empty">
                                                这个 provider 没有声明模型
                                             </li>
                                          ) : (
                                             ids.map((id) => (
                                                <li className="model-row" key={id}>
                                                   <span
                                                      className="model-row__id"
                                                      title={id}
                                                   >
                                                      {id}
                                                   </span>
                                                   {config?.default_model ===
                                                   `${provider.name}/${id}` ? (
                                                      <span className="set-card__tag">
                                                         默认
                                                      </span>
                                                   ) : null}
                                                </li>
                                             ))
                                          )}
                                       </ul>
                                    </div>
                                 );
                              })}
                           </div>
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
                        title={`Agent(${visibleAgents.length})`}
                        intro={
                           <>
                              只列全局角色:<code>~/.qi/agent/agents/</code> 与远端 A2A
                              智能体。项目级角色跟着仓库走,不在这里管。tools 为{" "}
                              <code>*</code> 时展开成实际清单。
                           </>
                        }
                     >
                        {visibleAgents.length === 0 ? (
                           <p className="set-sec__empty">
                              没有发现全局角色。没有装专职角色时,框架只用内置的
                              general 兜底。
                           </p>
                        ) : (
                           <ul className="set-cards">
                              {visibleAgents.map((agent) => (
                                 <li className="set-card" key={agent.name}>
                                    <div className="set-card__head">
                                       <span
                                          className="set-card__name"
                                          title={agent.name}
                                       >
                                          {agent.name}
                                       </span>
                                       <span className="set-card__tag">
                                          {agent.source}
                                       </span>
                                       {/* 键只在**这个宿主管得了的两层**时出现
                                           (`source` 不是 user/project = 不是它写出来的);
                                           `agent_write` 是老宿主降级位,没有就整列只读。 */}
                                       {agentWrite &&
                                       agentScopeOf(agent.source) !== null ? (
                                          <span className="set-card__actions">
                                             <button
                                                type="button"
                                                className="set-act set-act--danger"
                                                onClick={() => {
                                                   setAgentError(null);
                                                   setAgentRemoving(agent);
                                                }}
                                             >
                                                删除
                                             </button>
                                          </span>
                                       ) : null}
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

                  {section === "assistants" ? (
                     <Section
                        title="应用助手"
                        intro={
                           <>
                              项目级目录（<code>.qi/assistants.json</code>）：
                              名称、描述、典型任务、核心产出与跳转链接。只有「启用」的会被
                              角色「训练评估」推荐；链接是外链，点击在新标签打开。
                           </>
                        }
                     >
                        <AssistantsPanel canWrite={assistantWrite}
                                         onModalChange={setAssistantModal} />
                     </Section>
                  ) : null}

                  {section === "skills" ? (
                     <Section
                        title={`技能(${visibleSkills.length})`}
                        intro={
                           <>
                              只列全局技能(<code>~/.qi/agent/skills/</code> 等)。
                              渐进披露:只有描述进系统提示词,正文由 agent 需要时用{" "}
                              <code>read</code> 读全文。
                           </>
                        }
                     >
                        {/*
                          「重新扫描」:清单读的是 runtime 的**缓存**(按 cwd,只加载一次),
                          所以"没经过本宿主"的变更(手工拷目录、另一个进程装的、改了
                          frontmatter)在这一页永远看不见。TUI 有 `/reload`,web 以前只能
                          重启进程 —— 现在有这一颗键。它**不改任何文件**。
                        */}
                        {skillWrite ? (
                           <div className="set-skill__bar">
                              <button
                                 type="button"
                                 className="btn"
                                 disabled={skillsBusy}
                                 onClick={() => void reloadSkills()}
                              >
                                 {skillsBusy ? "扫描中…" : "重新扫描"}
                              </button>
                              <span className="set-skill__hint">
                                 改了 <code>~/.qi/agent/skills/</code> 或从别处装了技能,
                                 点它让缓存跟上磁盘
                              </span>
                           </div>
                        ) : null}
                        {visibleSkills.length === 0 ? (
                           <p className="set-sec__empty">
                              没有发现全局技能。放在 <code>~/.qi/agent/skills/</code>{" "}
                              或跨工具的 <code>~/.agents/skills/</code>。
                           </p>
                        ) : (
                           <ul className="set-cards">
                              {visibleSkills.map((skill) => (
                                 <li
                                    className="set-card"
                                    key={skill.path || skill.name}
                                 >
                                    <div className="set-card__head">
                                       <span
                                          className="set-card__name"
                                          title={skill.name}
                                       >
                                          {skill.name}
                                       </span>
                                       <span className="set-card__tag">
                                          {skill.source}
                                       </span>
                                       {skillWrite &&
                                       skillDeleteScope(skill) !== null ? (
                                          <span className="set-card__actions">
                                             <button
                                                type="button"
                                                className="set-act set-act--danger"
                                                onClick={() => {
                                                   setSkillError(null);
                                                   setSkillRemoving(skill);
                                                }}
                                             >
                                                删除
                                             </button>
                                          </span>
                                       ) : null}
                                    </div>
                                    {skill.description ? (
                                       <p className="set-card__desc">
                                          {skill.description}
                                       </p>
                                    ) : null}
                                    {skill.path ? (
                                       <div className="set-card__fields">
                                          <span className="kv">
                                             <span className="kv__label">位置</span>
                                             <span className="kv__value">
                                                {skill.path}
                                             </span>
                                          </span>
                                       </div>
                                    ) : null}
                                 </li>
                              ))}
                           </ul>
                        )}
                     </Section>
                  ) : null}

                  {section === "mcp" ? (
                     <Section
                        title={`MCP(${mcpTotal})`}
                        intro={
                           <>
                              只有<strong>全局</strong> <code>mcp.json</code>:角色私有的
                              声明不在这个页面显示。项目级 <code>.qi/mcp.json</code>{" "}
                              跟着仓库走,同样不在这里管。
                              这里只有“声明”、没有“已连接”(server 按需启动);
                              谁能调用由角色的 <code>tools:</code> 决定 ——
                              写 <code>mcp</code> 拿代理工具,写{" "}
                              <code>mcp__server__*</code> 直连。
                              <code>env</code> / <code>headers</code> 只显示键名。
                           </>
                        }
                     >
                        <McpSection
                           mcp={visibleMcp}
                           missing={mcpMissing || (mcp?.unavailable ?? false)}
                           canWrite={mcpWrite}
                           onAdd={(src) => {
                              setMcpError(null);
                              setMcpTarget({
                                 scope: src.scope,
                                 role: src.role,
                                 path: src.path,
                                 server: null,
                              });
                           }}
                           onEdit={(src, server) => {
                              setMcpError(null);
                              setMcpTarget({
                                 scope: src.scope,
                                 role: src.role,
                                 path: src.path,
                                 server,
                              });
                           }}
                           onDelete={(src, server) => {
                              setMcpError(null);
                              setMcpRemoving({ src, server });
                           }}
                        />
                     </Section>
                  ) : null}

                  {section === "a2a" ? (
                     <Section
                        title={`A2A(${a2aTotal})`}
                        intro={
                           <>
                              全局 <code>a2a.json</code> 里的条目。一个条目通常是一张 Agent
                              Card;<code>headers</code> 只显示键名。项目级{" "}
                              <code>a2a.json</code> 跟着仓库走,不在这里管。
                           </>
                        }
                     >
                        <A2ASection
                           a2a={visibleA2a}
                           missing={a2aMissing || (a2a?.unavailable ?? false)}
                           canWrite={a2aWrite}
                           onAdd={(src) => {
                              setA2aError(null);
                              setA2aTarget({
                                 scope: src.scope,
                                 path: src.path,
                                 entry: null,
                              });
                           }}
                           onEdit={(src, entry) => {
                              setA2aError(null);
                              setA2aTarget({
                                 scope: src.scope,
                                 path: src.path,
                                 entry,
                              });
                           }}
                           onDelete={(src, entry) => {
                              setA2aError(null);
                              setA2aRemoving({ src, entry });
                           }}
                           onProbe={(name) => api.probeA2A({ server: name })}
                        />
                     </Section>
                  ) : null}

                  {a2aTarget ? (
                     <A2ADialog
                        target={a2aTarget}
                        error={a2aError}
                        busy={a2aBusy}
                        onCancel={() => {
                           setA2aTarget(null);
                           setA2aError(null);
                        }}
                        onSubmit={submitA2A}
                     />
                  ) : null}
                  {a2aRemoving ? (
                     <A2ADeleteDialog
                        target={a2aRemoving.src}
                        entry={a2aRemoving.entry}
                        error={a2aError}
                        busy={a2aBusy}
                        onCancel={() => {
                           setA2aRemoving(null);
                           setA2aError(null);
                        }}
                        onConfirm={() => {
                           void removeA2A(a2aRemoving);
                        }}
                     />
                  ) : null}

                  {mcpTarget ? (
                     <McpDialog
                        target={mcpTarget}
                        error={mcpError}
                        busy={mcpBusy}
                        onCancel={() => {
                           setMcpTarget(null);
                           setMcpError(null);
                        }}
                        onSubmit={submitMcp}
                     />
                  ) : null}
                  {mcpRemoving ? (
                     <McpDeleteDialog
                        target={mcpRemoving.src}
                        server={mcpRemoving.server}
                        error={mcpError}
                        busy={mcpBusy}
                        onCancel={() => {
                           setMcpRemoving(null);
                           setMcpError(null);
                        }}
                        onConfirm={() => {
                           void removeMcp(mcpRemoving);
                        }}
                     />
                  ) : null}

                  {/* 技能删除确认层。与 MCP / 角色的删除同一规矩:目标写全、错误报在
                      弹层里、成功才关。 */}
                  {skillRemoving ? (
                     <ConfirmDialog
                        title={`删除技能 ${skillRemoving.name}?`}
                        rows={[
                           { label: "技能", value: skillRemoving.name },
                           { label: "来源", value: skillRemoving.source },
                           { label: "目标目录", value: skillRemoving.path },
                        ]}
                        warning="整目录删除:SKILL.md 与 references/ 等附属文件一起没。写错了可从 git 恢复。"
                        danger
                        confirmLabel="确认删除"
                        confirmDisabled={skillBusy}
                        onCancel={() => {
                           setSkillRemoving(null);
                           setSkillError(null);
                        }}
                        onConfirm={() => removeSkill(skillRemoving)}
                     >
                        {skillError ? (
                           <div className="dialog__error">{skillError}</div>
                        ) : null}
                     </ConfirmDialog>
                  ) : null}

                  {/* 角色删除确认层(不带表单:只有"删哪个、删哪一层")——错误报在
                      弹层里,与 MCP 的两个弹层同一规矩。 */}
                  {agentRemoving ? (
                     <AgentDeleteDialog
                        agent={agentRemoving}
                        error={agentError}
                        busy={agentBusy}
                        onCancel={() => {
                           setAgentRemoving(null);
                           setAgentError(null);
                        }}
                        onConfirm={() => removeAgent(agentRemoving)}
                     />
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

                  {section === "store" ? (
                     <Section
                        title="商店"
                        intro={
                           <>
                              从 hikqin 应用商店装 MCP 声明 / 技能包 / A2A 名片。
                              装配前会先给出计划(写哪里 / 会不会撞 / 要补什么)。
                              登录在这里就能做;密码只交给上游一次,不落盘、不回显。
                           </>
                        }
                     >
                        <StorePanel canWrite={storeWrite} />
                     </Section>
                  ) : null}

                  {section === "kb" ? (
                     <Section
                        title="知识库"
                        intro={
                           <>
                              把 hikqin 的知识库接成 qi-kb 的 provider（账号）+
                              knowledgeBases（KB）。只列与只写<strong>全局</strong>配置；
                              项目级 KB 跟着仓库走，不在这里管。账号与令牌写在 qi-kb
                              自己的配置里，不在 store.json。同一个 KB 只保留一份；
                              重复安装会先问是否覆盖。
                           </>
                        }
                     >
                        <KbPanel canWrite={kbWrite} onChanged={onKbChanged} />
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

         {providerAdding ? (
            <ConfirmDialog
               title={
                  providerMode === "existing"
                     ? "登录已有 Provider?"
                     : "新建 Provider?"
               }
               rows={
                  providerMode === "existing"
                     ? [
                          { label: "provider", value: providerPick || "(未选)" },
                          { label: "目标文件", value: authPath },
                       ]
                     : [
                          {
                             label: "provider",
                             value: providerDraft.name.trim() || "(未填)",
                          },
                          {
                             label: "目标文件",
                             value:
                                config?.models_write_file ??
                                "~/.qi/agent/models.json",
                          },
                       ]
               }
               warning={
                  providerMode === "existing"
                     ? "只写凭证文件 auth.json;provider 的 baseUrl / 模型清单来自 models.json 或 qi 预置。"
                     : "会写用户级 models.json:baseUrl / api / 模型 id(上下文窗口用 core 默认值,之后可手改)。"
               }
               confirmLabel={
                  providerMode === "existing" ? "保存凭证" : "创建 Provider"
               }
               confirmDisabled={providerBusy || !providerCanSubmit}
               onCancel={() => {
                  setProviderAdding(false);
                  setProviderError(null);
               }}
               onConfirm={submitProvider}
            >
               <div className="dialog__fields">
                  <label className="dialog__field">
                     <span>添加方式</span>
                     <select
                        className="dialog__input"
                        value={providerMode}
                        onChange={(event) => {
                           setProviderMode(
                              event.target.value as "existing" | "custom",
                           );
                           setProviderError(null);
                        }}
                     >
                        <option value="existing">登录已有 provider</option>
                        {providerWrite ? (
                           <option value="custom">新建自定义 provider</option>
                        ) : null}
                     </select>
                  </label>

                  {providerMode === "existing" ? (
                     <>
                        {hiddenProviders.length === 0 ? (
                           <p className="dialog__hint">
                              所有已知 provider 都已登录。
                              {providerWrite
                                 ? "可以切到“新建自定义 provider”。"
                                 : "要加新的 provider 请跑 qi init。"}
                           </p>
                        ) : null}
                        <label className="dialog__field">
                           <span>provider</span>
                           <select
                              className="dialog__input"
                              value={providerPick}
                              onChange={(event) =>
                                 setProviderPick(event.target.value)
                              }
                              disabled={hiddenProviders.length === 0}
                           >
                              {hiddenProviders.length === 0 ? (
                                 <option value="">
                                    (没有待登录的 provider)
                                 </option>
                              ) : (
                                 hiddenProviders.map((provider) => (
                                    <option
                                       key={provider.name}
                                       value={provider.name}
                                    >
                                       {provider.name}
                                       {provider.label
                                          ? ` · ${provider.label}`
                                          : ""}
                                       {provider.base_url
                                          ? ` — ${provider.base_url}`
                                          : ""}
                                    </option>
                                 ))
                              )}
                           </select>
                        </label>
                        <label className="dialog__field">
                           <span>API key</span>
                           <input
                              className="dialog__input"
                              type="password"
                              value={providerDraft.key}
                              autoFocus
                              placeholder="粘贴 API key"
                              onChange={(event) =>
                                 setProviderDraft((draft) => ({
                                    ...draft,
                                    key: event.target.value,
                                 }))
                              }
                           />
                        </label>
                     </>
                  ) : (
                     <>
                        <label className="dialog__field">
                           <span>provider 名</span>
                           <input
                              className="dialog__input"
                              value={providerDraft.name}
                              autoFocus
                              placeholder="my-provider"
                              onChange={(event) =>
                                 setProviderDraft((draft) => ({
                                    ...draft,
                                    name: event.target.value,
                                 }))
                              }
                           />
                        </label>
                        <label className="dialog__field">
                           <span>baseUrl</span>
                           <input
                              className="dialog__input"
                              value={providerDraft.baseUrl}
                              placeholder="https://api.example.com/v1"
                              onChange={(event) =>
                                 setProviderDraft((draft) => ({
                                    ...draft,
                                    baseUrl: event.target.value,
                                 }))
                              }
                           />
                        </label>
                        <label className="dialog__field">
                           <span>api</span>
                           <select
                              className="dialog__input"
                              value={providerDraft.api}
                              onChange={(event) =>
                                 setProviderDraft((draft) => ({
                                    ...draft,
                                    api: event.target.value,
                                 }))
                              }
                           >
                              {PROVIDER_APIS.map((api) => (
                                 <option key={api} value={api}>
                                    {api}
                                 </option>
                              ))}
                           </select>
                        </label>
                        <label className="dialog__field">
                           <span>模型 id(一行一个)</span>
                           <textarea
                              className="dialog__input dialog__input--area"
                              value={providerDraft.models}
                              rows={3}
                              placeholder={"model-a\nmodel-b"}
                              onChange={(event) =>
                                 setProviderDraft((draft) => ({
                                    ...draft,
                                    models: event.target.value,
                                 }))
                              }
                           />
                        </label>
                        <label className="dialog__field">
                           <span>API key</span>
                           <input
                              className="dialog__input"
                              type="password"
                              value={providerDraft.key}
                              placeholder="粘贴 API key"
                              onChange={(event) =>
                                 setProviderDraft((draft) => ({
                                    ...draft,
                                    key: event.target.value,
                                 }))
                              }
                           />
                        </label>
                        <p className="dialog__hint">
                           已有同名 provider(项目 models.json / 环境变量文件里定义的)
                           会拒绝,去那个文件改。
                        </p>
                     </>
                  )}

                  {providerError ? (
                     <div className="dialog__error">{providerError}</div>
                  ) : null}
               </div>
            </ConfirmDialog>
         ) : null}

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
