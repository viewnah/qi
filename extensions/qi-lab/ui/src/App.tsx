/**
 * 外壳:**左栏(会话列表)· 中栏(工作台)· 右侧遥测抽屉**。
 *
 * 版式基线是 dsh 的网页面(`data/dsh.png` 与它的源码):
 *
 * ```text
 * ┌────────────┬──────────────────────────────┐
 * │ 侧栏 280   │ 工作台                        │
 * │ 品牌行 60   │   眉条 52(只有会话名,贴左)      │
 * │ 新会话 38   │   ┌─ 空态 = hero ─┐            │
 * │ 会话 36    │   │  mark + 标题   │           │
 * │   会话行 32 │   │  输入卡 + 统计行  │           │
 * │   ...      │   └───────────────┘           │
 * │ 遥测 / 设置 │                               │
 * └────────────┴──────────────────────────────┘
 * ```
 *
 * 工作目录由宿主固定(`qi lab --cwd <dir>`),所以左栏没有项目层级,
 * 也没有项目 chip / 添加工作区入口。
 *
 * 三条不变量(其余都是它们的推论):
 *
 * 1. **会话归客户端**。UI 不在流里传历史 —— 后端从本地 JSONL 取上下文。
 *    所以重连语义就是"再发一次 run",不是续传。
 * 2. **乐观先行**。用户消息立刻上屏,不等服务端落盘;`qi.history` 到达时
 *    再用水合结果替换(它抓的是 run 开始前的 entries,所以 hydrate 会把
 *    本轮输入补回去 —— 见 turn.ts 的 pendingUser)。
 * 3. **流结束时以落盘为准**。run 结束后重拉会话明细,保证刷新前后一致。
 *
 * 空态与对话态**共用同一个输入卡**(`Dock`),但位置不同:dsh 的空态把卡片
 * 放进了垂直居中的 hero stack,对话态把卡片停靠在底部。所以这里按
 * `rows.length === 0` 走两条分支 —— 草稿在 App 的 state 里,切换分支不会丢字。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, ContractError, run } from "./api/client";
import type {
  AgentInfo,
  ConfigView,
  KbItem,
  Meta,
  ModelCatalog,
  ModelOption,
  SessionSummary,
  UsageSummary,
} from "./api/types";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { IconPanelLeftOutline16 } from "./components/icons";
import { Dock } from "./components/Dock";
import { bareModelName } from "./components/ModelMenu";
import { Rail } from "./components/Rail";
import { BlockView } from "./components/Rows";
import { Settings } from "./components/Settings";
import type { SettingsSectionKey } from "./components/Settings";
import { FilePanel } from "./components/FilePanel";
import {
  emptyTurn,
  fromEntries,
  groupProcess,
  lastUsage,
  reduce,
  withNote,
  withUserMessage,
} from "./state/turn";
import type { TurnState } from "./state/turn";
import { parseSessionHash, sessionHash } from "./state/route";
import { commandHelpText } from "./commands";
import { applyTheme, readTheme } from "./theme/theme";

const PLACEHOLDER = "描述你想做的事。Enter 发送,Shift+Enter 换行";

/** 行尾相对时间的刷新周期。分钟级标签不需要秒级精度。 */
const CLOCK_MS = 30_000;

/** 自动命名的迟到轮询:每 1s 一次,共 15s;拿到名字就停。 */
const TITLE_POLL_MS = 1_000;
const TITLE_POLL_TRIES = 15;

/** 与 core 的 `has_title` 同口径:空串与历史默认名 `tui` 都不算名字。 */
function hasSessionName(title: string | null | undefined): boolean {
  const cleaned = (title ?? "").trim();
  return cleaned.length > 0 && cleaned !== "tui";
}

/**
 * 左栏四个动作各自的弹层。
 *
 * 为什么是"一个判别联合 + 一个 dialog state"而不是四个独立 state:同一时刻只可能
 * 有一个弹层开着,四个 boolean 会允许出现两个同时为真这种非法状态。
 */
type Dialog =
  | { kind: "rename-session"; session: SessionSummary }
  | { kind: "delete-session"; session: SessionSummary };

/** 从转录尾行推出"此刻在干什么" —— 活动条要显示的那句话。 */
function currentAction(turn: TurnState): string {
  const rows = turn.rows;
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const row = rows[i];
    if (!row) continue;
    if (row.kind === "tool" && row.status === "running")
      return `执行 ${row.tool}`;
    if (row.kind === "think" && row.live) return "思考中";
    if (row.kind === "say" && row.live) return "生成回答";
    if (row.kind === "route") return "已分派";
  }
  return "运行中";
}

/** 弹层标题。每个动作一句话,不共用"确认操作"。 */
const DIALOG_TITLE: Record<Dialog["kind"], string> = {
  "rename-session": "重命名会话",
  "delete-session": "删除会话",
};

/** 确认按钮的词:删除说"删除",改名说"保存"。 */
const CONFIRM_LABEL: Record<Dialog["kind"], string> = {
  "rename-session": "保存",
  "delete-session": "删除",
};

/** 会删数据的那几个:按钮走危险色。 */
const DANGEROUS: Dialog["kind"][] = ["delete-session"];

/** 弹层要显示的事实:让"我要改什么/删什么"一目了然(与凭证弹层同规矩)。 */
function dialogRows(dialog: Dialog): { label: string; value: string }[] {
  switch (dialog.kind) {
    case "rename-session":
      return [
        { label: "当前标题", value: dialog.session.title || "未命名" },
        { label: "会话", value: dialog.session.id },
      ];
    case "delete-session":
      return [
        { label: "标题", value: dialog.session.title || "未命名" },
        { label: "文件", value: dialog.session.path },
      ];
  }
}

/** 危险动作要把后果写在弹层里,而不是让人猜。 */
function dialogWarning(dialog: Dialog): string {
  switch (dialog.kind) {
    case "rename-session":
      return "";
    case "delete-session":
      return "会话文件会被删除,不可恢复。";
  }
}

/**
 * 把异常转成给人看的一句话。
 *
 * 提到组件外:它在多个 `useCallback` 里被用到,放进组件体会让每个回调都得把它列进
 * 依赖(否则就是过期闭包 —— 这个文件为此栽过一次,见 design/web.md §18.8)。
 * 它不读任何 state,所以放外面是诚实的做法,而不是把依赖数组糊过去。
 */
function describeError(err: unknown): string {
  if (err instanceof ContractError) return err.message;
  if (err instanceof ApiError) return `${err.detail}(HTTP ${err.status})`;
  return String(err);
}

export function App() {

  const [meta, setMeta] = useState<Meta | null>(null);
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  /**
   * 当前会话的用量汇总(后端算的落盘口径)。
   *
   * 为什么不在前端自己累加:这里手上只有**分页窗口**,长会话窗口外还有几千条 ——
   * 求和会静默少算。所以它在"打开会话"与"一轮结束"两个时点从 `/api/sessions/{id}`
   * 重取,不参与逐帧的流式状态。
   */
  const [sessionUsage, setSessionUsage] = useState<UsageSummary | null>(null);
  /**
   * 手动钉住的智能体:`null` = auto(默认,每轮由分派器决定)。
   *
   * 与 TUI 的 `--agent` / `/agent` 同一语义,也同一生命周期 —— **不落盘**:它是这个
   * 客户端的即时设置,刷新后回到 auto。会话文件里的 `active_agent` 是"上次分派到谁"
   * 的**结果**,拿它当设置显示会撒谎(见 AgentMenu 的注释)。
   */
  const [agent, setAgent] = useState<string | null>(null);
  /** 可选智能体清单(菜单用)。取不到就只剩 auto —— 不影响发消息。 */
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [kbItems, setKbItems] = useState<KbItem[]>([]);
  /** 会话级 KB 选择：空 = 用 qi-kb 的全局/项目绑定。 */
  const [kbIds, setKbIds] = useState<string[]>([]);
  /**
   * 模型清单 + 当前值(`GET /api/models`)。`null` = 取不到(老宿主 / 还没加载)→
   * 输入卡那个 chip 变成只读回声,不画 chevron、点不开(与"附件置灰"同一条规矩:
   * 界面上不做点了没反应的控件)。
   */
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog | null>(null);
  /** 一次模型/级别切换正在往返(禁重复点,免得两次请求互相覆盖)。 */
  const [modelBusy, setModelBusy] = useState(false);
  const [title, setTitle] = useState("");
  const [turn, setTurn] = useState<TurnState>(emptyTurn);
  const [draft, setDraft] = useState("");
  /** 设置**浮层**开着没有(§18.17:它不再是"一个页面",而是盖在工作台上的面板)。 */
  const [view, setView] = useState<"work" | "settings">("work");
  /** 设置浮层打开时落在哪一节(指令菜单的 `/agents` / `/settings` 用)。 */
  const [settingsSection, setSettingsSection] =
    useState<SettingsSectionKey>("models");
  /**
   * 左栏折叠成 56px 竖条(dsh 的 rail 规格,见 app.css 的 `data-rail="collapsed"`)。
   * **不落盘**:与"手动钉智能体"同一条规矩 —— 它是这个客户端的即时形态,刷新回默认。
   */
  const [railCollapsed, setRailCollapsed] = useState(false);
  /** 右侧文件面板开着没有(会话目录的树 + 预览,见 FilePanel)。 */
  const [filesOpen, setFilesOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [nameDraft, setNameDraft] = useState("");

  /**
   * 刷新时想打开的会话(只读一次)。
   *
   * **必须在任何 effect 之前取**:下面的同步 effect 会把 `sessionId=null` 写成 `#`,
   * 之后再去读 `location.hash` 就什么都没有了 —— 首次渲染时读进 ref 最稳。
   */
  const bootSessionRef = useRef(parseSessionHash(window.location.hash));
  /** 首屏"按 URL 打开会话"只做一次(列表可能在后续刷新里变)。 */
  const bootDone = useRef(false);

  const abortRef = useRef<(() => void) | null>(null);
  /**
   * 自动命名轮询的世代号。新的一轮开始时递增,旧的轮询看到世代不等就退出 ——
   * 避免多轮重叠时把旧请求的结果写回列表。
   */
  const titlePollRef = useRef(0);
  /** 左栏底部「设置」行:浮层关掉时焦点还给它(见 Settings 的 `returnFocusTo`)。 */
  const settingsTriggerRef = useRef<HTMLButtonElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const cwd = turn.host.cwd ?? meta?.default_cwd ?? "";

  // 主题只在开机应用一次:Web 端没有切换入口(见 theme.ts),值来自 localStorage / auto。
  useEffect(() => applyTheme(readTheme()), []);

  // 行尾相对时间自己走(不依赖别的 state 变化把时间戳刷新)。
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), CLOCK_MS);
    return () => window.clearInterval(timer);
  }, []);

  const describe = describeError;

  const refreshSessions = useCallback(async () => {
    try {
      const next = (await api.sessions()).sessions;
      setSessions(next);
      return next;
    } catch (err) {
      setFatal(describe(err));
      return null;
    }
  }, []);

  /**
   * 自动命名是**并行**跑的(core 最多等 2s,等不到就挂到回调上)。
   * 回调写盘时这一轮早已结束,`onClose` 里的那次刷新看不到新标题 ——
   * 所以对“还没有名字”的会话再轮询一小段时间:标题一落盘就停止。
   */
  const refreshUntilTitled = useCallback(async (id: string) => {
    const token = ++titlePollRef.current;
    for (let attempt = 0; attempt < TITLE_POLL_TRIES; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, TITLE_POLL_MS));
      if (titlePollRef.current !== token) return;
      try {
        const next = (await api.sessions()).sessions;
        if (titlePollRef.current !== token) return;
        setSessions(next);
        const row = next.find((session) => session.id === id);
        if (row === undefined || hasSessionName(row.title)) return;
      } catch {
        return;
      }
    }
  }, []);

  // 首屏:先探契约(不匹配要显式报错,不白屏),再拉会话与配置。
  useEffect(() => {
    void (async () => {
      try {
        const m = await api.meta();
        setMeta(m);
        await refreshSessions();
        setConfig(await api.config());
        // 智能体清单只喂那个下拉菜单:取不到不该把整页打成错误态。
        try {
          setAgents(await api.agentList());
        } catch {
          setAgents([]);
        }
        // 已绑定 KB 只喂输入卡那颗 chip；没装 qi-kb / 没登录都退回空。
        try {
          const bound = await api.kbAvailable(undefined, undefined, true);
          setKbItems(bound.items.filter((item) => item.bound));
        } catch {
          setKbItems([]);
        }
      } catch (err) {
        setFatal(describe(err));
      }
    })();
    return () => abortRef.current?.();
  }, [refreshSessions]);

  /**
   * 模型清单:**跟着会话走**。
   *
   * 为什么不能只在首屏取一次:"当前是哪个模型"是**会话级**的(续会话会按会话里记的
   * `model_change` 还原,未必等于 settings 默认),而后端要拿会话 id 才知道答哪一条。
   * 所以 `sessionId` 一变就重取 —— 与 `openSession` 拉明细同一个节奏。
   *
   * 取不到(老宿主 404 / 网络错)不当作致命:`modelCatalog=null` → chip 退回只读回声。
   * 这与 `/api/agents` 是同一条降级规矩 —— 少一个可选端点
   * 不该让整页白屏。
   */
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const catalog = await api.models(sessionId);
        if (alive) setModelCatalog(catalog);
      } catch {
        if (alive) setModelCatalog(null);
      }
    })();
    return () => {
      alive = false;
    };
  }, [sessionId]);

  /**
   * 切模型 / 切级别。
   *
   * 两件事共用一条路径,因为它们走同一个端点、共用同一份"切换中"状态,而且返回的
   * 都是更新后的清单 —— 界面直接拿它替换本地那份(不做乐观更新:清单里的 ✓ 与
   * 上下文窗口都要跟着变,自己拼容易漏)。
   *
   * **失败不静默**:用 note 把后端的原因说出来(缺凭证 / provider 不存在都会回 400
   * 带具体文字)。这比"点了没反应"好,也比把整页打成错误屏合适 —— 它只是这一步没成。
   */
  const switchModel = useCallback(
    async (body: {
      provider?: string;
      model?: string;
      thinking_level?: string;
    }) => {
      setModelBusy(true);
      try {
        const catalog = await api.setModel({ ...body, session: sessionId });
        setModelCatalog(catalog);
        if (body.thinking_level !== undefined) {
          // 级别不在输入卡上显示(它在模型菜单里),所以给一句反馈 ——
          // 否则用户看不到任何变化。
          setTurn((prev) =>
            withNote(prev, "思考级别", `已切到 ${catalog.thinking_level}`),
          );
        }
      } catch (err) {
        setTurn((prev) => withNote(prev, "切换模型失败", describe(err)));
      } finally {
        setModelBusy(false);
      }
    },
    [sessionId, describe],
  );

  const onPickModel = useCallback(
    (option: ModelOption) => {
      void switchModel({ provider: option.provider, model: option.id });
    },
    [switchModel],
  );

  const onPickLevel = useCallback(
    (level: string) => {
      void switchModel({ thinking_level: level });
    },
    [switchModel],
  );

  /**
   * 标题跟着**列表**走。
   *
   * 自动命名是后端在首轮结束后做的(它要调一次模型),落盘后列表里就有新标题了 ——
   * 这里把它同步到眉条,否则会出现"左栏已经叫「梳理仓库结构」、眉条还写着未命名"。
   * 手动改名也走同一条路(改完就刷新列表),所以这里是唯一需要同步的地方。
   */
  useEffect(() => {
    if (sessionId === null) return;
    const fresh = sessions.find((s) => s.id === sessionId);
    if (fresh !== undefined && fresh.title && fresh.title !== title) {
      setTitle(fresh.title);
    }
  }, [sessions, sessionId, title]);

  // 新行到达时贴底(只在用户本来就在底部时 —— 否则会打断向上翻阅)
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [turn.rows]);

  // 固定工作目录:列表与会话都只属于 `meta.default_cwd`,左栏不再按项目分组。

  const openSession = useCallback(async (id: string) => {
    setView("work");
    try {
      const detail = await api.session(id);
      setSessionId(detail.id);
      setTitle(detail.title);
      // 会话级用量:**后端算的落盘口径**(前端只有窗口,自己求和会少算),
      // 所以它只在"打开会话"与"一轮结束"两个时点刷新。
      // `?? null` 是给**老宿主**的:它的明细里没有 `usage` 字段,直接透传下去
      // 会让 Dock 读到 undefined 而抛错(整页白) —— 与可选端点同一条降级规矩。
      setSessionUsage(detail.usage ?? null);
      setTurn({
        ...emptyTurn,
        rows: fromEntries(detail.entries),
        // 历史里**最后一轮**的用量:让遥测抽屉的"上下文"在刷新/重开之后也有数
        // (`qi.usage` 那个流事件不会重放)。老会话没记过 → 保持空。
        usage: lastUsage(detail.entries) ?? {},
        phase: "idle",
      });
    } catch (err) {
      setFatal(describe(err));
    }
  }, []);


  /** 真正发起一轮:单次 POST,响应就是流。 */
  const sendTo = useCallback(
    (id: string, text: string) => {
      abortRef.current?.();
      setTurn((prev) => withUserMessage(prev, text));
      abortRef.current = run(id, text, {
        onEvent: (ev) => setTurn((prev) => reduce(prev, ev)),
        onClose: () => {
          setTurn((prev) => ({
            ...prev,
            phase: prev.phase === "running" ? "ok" : prev.phase,
          }));
          void refreshSessions().then((list) => {
            const row = list?.find((session) => session.id === id);
            // 只有“这一轮结束时还没有名字”才值得轮询;已有标题/会话被删就不追。
            if (row !== undefined && !hasSessionName(row.title)) {
              void refreshUntilTitled(id);
            }
          });
          // 这一轮已经落盘(带 usage),重拉一次明细拿新的会话级汇总 ——
          // 卡片下方那行统计因此在每轮结束后跟一步。
          void api
            .session(id)
            .then((detail) => setSessionUsage(detail.usage ?? null))
            .catch(() => {
              // 拿不到就保持上一轮的数字:宁可旧一点,也不要变成空白。
            });
        },
        onError: (err) => {
          setTurn((prev) =>
            reduce(prev, { type: "RUN_ERROR", message: describe(err) }),
          );
        },
      },
      // 第 4 个参数是**选项**:手动钉住时每轮都带过去(后端据此直派,source 记成 "manual")。
      // kbIds 只影响本轮的知识库范围；空数组 = 用 qi-kb 的全局/项目绑定。
      { agent, kbIds });
    },
    [agent, kbIds, refreshSessions, refreshUntilTitled],
  );

  /** 首屏会话列表到了之后,按 URL 打开它(**这就是"刷新回到当前会话"**)。 */
  useEffect(() => {
    if (bootDone.current || sessions.length === 0) return;
    bootDone.current = true;
    const wanted = bootSessionRef.current;
    if (wanted === null) return;
    if (sessions.some((s) => s.id === wanted)) {
      void openSession(wanted);
      return;
    }
    // 会话被删了、或这个 id 不属于这台机器:清掉死链,别让地址栏一直指着不存在的东西。
    bootSessionRef.current = null;
    window.history.replaceState(null, "", sessionHash(null));
  }, [sessions, openSession]);

  /**
   * 会话 → URL。用 `replaceState` 而不是 `location.hash = …`:
   * 后者会**新增历史记录**(点十个会话就堆十条)而且会触发 `hashchange`(回环)。
   */
  useEffect(() => {
    if (!bootDone.current && bootSessionRef.current !== null) return;   // 首屏还没决定
    if (parseSessionHash(window.location.hash) === sessionId) return;
    window.history.replaceState(null, "", sessionHash(sessionId));
  }, [sessionId]);

  /** URL → 会话:地址栏被手工改、或前进/后退。hash 是这份状态的唯一真相。 */
  useEffect(() => {
    const onHash = () => {
      const wanted = parseSessionHash(window.location.hash);
      if (wanted === null) {
        setSessionId(null);
        setTurn(emptyTurn);
        return;
      }
      if (wanted === sessionId) return;
      if (!sessions.some((s) => s.id === wanted)) {
        // 地址栏里的 id 这台宿主不认识(手打的、或从别的机器复制来的):
        // **把地址栏改回当前会话**,而不是拿它去请求一个必然 404 的会话 ——
        // 那会把整页打成错误屏(实测过);也不清视图:用户只是敲错了,没做错什么。
        window.history.replaceState(null, "", sessionHash(sessionId));
        return;
      }
      void openSession(wanted);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [openSession, sessions, sessionId]);

  /**
   * 回到**新会话页**(草稿态):只切视图,**不建会话文件**。
   *
   * 文件推迟到**第一句话发出去**时才建:连点两次「新会话」不会堆出一排空的
   * 「未命名」。工作目录由宿主固定,草稿页没有目录要选。
   */
  const openDraft = useCallback(() => {
    setView("work");
    setSessionId(null);
    setTitle("");
    setTurn(emptyTurn);
    setSessionUsage(null);
  }, []);

  /**
   * 建会话。**只有带着第一句话时才落文件** —— 不带就是 `openDraft()`(见它的注释)。
   */
  const createSession = useCallback(
    async (first?: string) => {
      if (first === undefined) {
        openDraft();
        return;
      }
      try {
        const created = await api.createSession("");
        await refreshSessions();
        setSessionId(created.id);
        setTitle(created.title);
        setTurn(emptyTurn);
        setSessionUsage(null);      // 新会话:还没有任何用量
        void sendTo(created.id, first);
      } catch (err) {
        setFatal(describe(err));
      }
    },
    // `sendTo` **必须**列上:它带着「当前智能体」这个设置,而 `sendTo` 在智能体变化时
    // 会重建。漏掉它 → 在 hero 态选好智能体再发第一句时,这里调用的是**旧闭包**
    // (`agent` 还是 null),手动选择静默失效(实测踩过:分派行写的是 fallback)。
    [openDraft, refreshSessions, sendTo],
  );

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    if (sessionId) sendTo(sessionId, text);
    else void createSession(text);
  }, [createSession, draft, sendTo, sessionId]);

  /** 智能体自带的快捷提问：点选即作为一条 user 消息提交（设计语义）。 */
  const sendSuggestion = useCallback(
    (text: string) => {
      const body = text.trim();
      if (!body) return;
      if (sessionId) sendTo(sessionId, body);
      else void createSession(body);
    },
    [createSession, sendTo, sessionId],
  );

  const stop = useCallback(() => {
    abortRef.current?.();
    abortRef.current = null;
    setTurn((prev) => ({ ...prev, phase: "idle" }));
  }, []);

  /**
   * 会话动作。
   *
   * 全部**先问后端、再以后端回的东西为准**(不做乐观更新):这些都是低频动作,
   * 少一次往返换不来什么,而本地先改再回滚会让"列表和磁盘不一致"这种最难查。
   */
  /**
   * 分叉:`at` 省略 = 当前节点(左栏行尾菜单用);给了 entry id = **从那条消息分叉**
   * (转录里每条消息下面那个按钮用,与 dsh 的 branch 同一语义)。
   *
   * 不论是否需要确认:**分叉只新增文件、不动任何已有数据** —— 与左栏那个分叉同一理由
   * (design/web.md §18.1)。
   */
  const forkSession = useCallback(
    async (id: string, at?: string) => {
      try {
        const forked = await api.forkSession(id, at);
        await refreshSessions();
        // 分叉完直接开新会话:用户点"分叉"要的就是接着新分支往下说(CLI/TUI 同款)。
        await openSession(forked.id);
      } catch (err) {
        setFatal(describe(err));
      }
    },
    [openSession, refreshSessions],
  );

  /**
   * 复制单条消息。**返回是否真的成功**:成功时反馈在按钮自己身上(图标换成勾),
   * 失败才甩一行 note —— 与 `/copy` 指令同规矩:剪贴板被拒是"这一步没成",
   * 不是"应用坏了";而没成功就不该显示"已复制"。
   */
  const copyMessage = useCallback(async (text: string): Promise<boolean> => {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      setTurn((prev) => withNote(prev, "复制失败", String(err)));
      return false;
    }
  }, []);

  const askRenameSession = useCallback((session: SessionSummary) => {
    setNameDraft(session.title);
    setDialog({ kind: "rename-session", session });
  }, []);

  const askDeleteSession = useCallback((session: SessionSummary) => {
    setDialog({ kind: "delete-session", session });
  }, []);

  /**
   * 删除之后的收尾:刷新列表,并在**当前正开着的会话也被删掉**时回到空态。
   *
   * 收尾漏了会留下一个指向已删文件的 sessionId,下一次发言会拿它去 append,
   * 报一个谁也看不懂的错。
   */
  const forgetSessions = useCallback(
    async (ids: string[]) => {
      await refreshSessions();
      if (sessionId !== null && ids.includes(sessionId)) {
        setSessionId(null);
        setTitle("");
        setTurn(emptyTurn);
        setSessionUsage(null);
      }
    },
    [refreshSessions, sessionId],
  );

  /** 弹层的"确认":每个动作共用一个入口,各自只做自己那一件事。 */
  const confirmDialog = useCallback(async () => {
    if (dialog === null) return;
    const target = dialog;

    setDialog(null);
    try {
      if (target.kind === "rename-session") {
        const name = nameDraft.trim();
        if (!name) return; // 空标题后端不收(SessionRename 要求 min_length=1)
        await api.renameSession(target.session.id, name);
        if (target.session.id === sessionId) setTitle(name);
        await refreshSessions();
      } else if (target.kind === "delete-session") {
        await api.deleteSession(target.session.id);
        await forgetSessions([target.session.id]);
      }
    } catch (err) {
      setFatal(describe(err));
    }
  }, [dialog, forgetSessions, nameDraft, refreshSessions, sessionId]);

  const running = turn.phase === "running";
  /**
   * 哪一行的动作**常显**:最后一条"带动作的行"(提问或回答)。
   *
   * 其余行的动作悬停才显形 —— 照 dsh 的 `data-actions-reveal`:老消息不该一直挂着
   * 一排按钮抢视线,而最新一条要能直接看见(那是刚说完、最可能想动的那条)。
   */
  const lastActionKey = useMemo(() => {
    for (let i = turn.rows.length - 1; i >= 0; i -= 1) {
      const row = turn.rows[i];
      if (row !== undefined && (row.kind === "you" || row.kind === "say")) {
        return row.key;
      }
    }
    return null;
  }, [turn.rows]);
  // 上下文窗口见下面 `contextWindow`(优先取**当前模型**的窗口,见那里的注释)。
  // 0 = 取不到,那时不画占用百分比 —— 而不是画一条永远 0% 的。
  const empty = turn.rows.length === 0;
  // 输入卡右下只显示**裸模型名**(`provider/model` 在窄窗口里放不下);完整标签
  // (`provider/model`)留给 tooltip 与遥测抽屉 —— 同 id 不同 provider 时靠它分辨。
  // `default_model_name` 是本次新加的字段,老宿主不给就回落完整标签(不会变空)。
  //
  // **当前值优先用 `modelCatalog.currently`**:那是**会话级**的真相(续会话会按会话里
  // 记的 `model_change` 还原,未必等于 settings 默认)。`/api/config` 那份是"默认",
  // 拿它当显示值会在会话换过模型之后说反话。清单取不到(老宿主)才回落它。
  const configModelName = config?.default_model_name || config?.default_model || "";
  const modelName = modelCatalog?.currently
    ? bareModelName(modelCatalog.currently)
    : configModelName;
  const modelFull = modelCatalog?.currently || config?.default_model || "";
  // 上下文窗口:同样以**当前模型**为准 —— 换到窗口更小的模型之后,占用条要跟着变
  // (否则它会拿默认模型的窗口去算比例,百分比就是错的)。
  const contextWindow =
    modelCatalog?.models.find((m) => `${m.provider}/${m.id}` === modelCatalog.currently)
      ?.context_window || config?.default_model_context_window || 0;

  /**
   * 输入卡「+」菜单里的命令。三条都映射到**已经实现**的动作:
   * `new` → 新建会话、`fork` → 分叉当前会话(与行尾菜单同一个调用)、
   * `settings` → 开设置页。`attach` 不在表里 —— 它那一行是禁用的(Dock 里注明暂不支持)。
   */
  const runCommand = useCallback(
    async (id: string) => {
      /** 指令的反馈:本地插一行 note(不经过模型、不进上下文)。 */
      const note = (label: string, detail: string) =>
        setTurn((prev) => withNote(prev, label, detail));

      if (id === "help") {
        note("可用指令", commandHelpText());
      } else if (id === "info") {
        const summary = sessions.find((s) => s.id === sessionId);
        note(
          "会话信息",
          [
            `会话 ${sessionId ?? "(还没有)"}`,
            `标题 ${summary?.title || "未命名"}`,
            `目录 ${cwd || "—"}`,
            `条数 ${turn.rows.length} 行 / ${summary?.message_count ?? 0} 条消息`,
            `模型 ${modelFull || "未配置"}`,
          ].join("\n"),
        );
      } else if (id === "sessions") {
        note(
          "最近会话",
          sessions
            .slice(0, 10)
            .map((one) => `${one.title || "未命名"} · ${one.id}`)
            .join("\n"),
        );
      } else if (id === "copy") {
        const last = [...turn.rows]
          .reverse()
          .find((row) => row.kind === "say" && row.tone === "final");
        if (last !== undefined && last.kind === "say") {
          try {
            await navigator.clipboard.writeText(last.text);
            note("已复制最后一条回答", last.text.slice(0, 120));
          } catch (err) {
            // 剪贴板被拒(无焦点/无权限)不该把整页打成错误屏 —— 这是"这一步没成",
            // 不是"应用坏了"。用 note 把原因说出来,界面照常能用。
            note("复制失败", describe(err));
          }
        }
      } else if (id === "export" && sessionId !== null) {
        try {
          const { blob, name } = await api.exportSession(sessionId);
          // 触发一次浏览器下载;对象 URL 用完就撤,免得一直占着内存。
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = url;
          link.download = name;
          document.body.appendChild(link);
          link.click();
          link.remove();
          URL.revokeObjectURL(url);
          note("已导出会话", name);
        } catch (err) {
          note("导出失败", describe(err));
        }
      } else if (id === "name" && sessionId !== null) {
        // 复用会话行「…」里的那个改名弹层:同一个动作不该有两套 UI。
        const summary = sessions.find((s) => s.id === sessionId);
        if (summary !== undefined) askRenameSession(summary);
      } else if (id === "agents") {
        setSettingsSection("agents");
        setView("settings");
      } else if (id === "new") {
        await createSession();
      } else if (id === "fork" && sessionId !== null) {
        await forkSession(sessionId);
      } else if (id === "compact" && sessionId !== null) {
        try {
          // `POST /api/sessions/{id}/compact` —— 与 TUI 的 `/compact` 同一个
          // `runtime.compact_session()`。运行中会被后端 409 拒掉(压缩要读整条分支
          // 并追加一条 entry,与正在写的回合会打架)。
          await api.compactSession(sessionId);
          // 压缩会往会话里**追加一条 compaction entry** —— 重取明细,转录里就会出现那一行
          // (否则用户点了"压缩"却看不到任何变化)。
          await openSession(sessionId);
        } catch (err) {
          // 运行中会被 409 拒掉(那是"现在不行"),一样用 note 说明,而不是错误屏。
          note("压缩失败", describe(err));
        }
      } else if (id === "files") {
        // 右侧文件面板:空态(hero)里没有眉条,所以那条开关够不着 —— 指令是第二个入口。
        setFilesOpen((v) => !v);
      } else if (id === "settings") {
        setSettingsSection("models");
        setView("settings");
      }
    },
    // 依赖列全:漏一个就会拿到过期闭包(这个文件里为此踩过一次,见 design/web.md §18.8)。
    [
      askRenameSession,
      createSession,
      cwd,
      forkSession,
      modelFull,
      openSession,
      sessionId,
      sessions,
      turn.rows,
    ],
  );

  /** 两个分支(dock / hero)共用同一个 Dock 描述,避免两处漂移。 */
  const dock = (
    <Dock
      value={draft}
      onChange={setDraft}
      onSend={send}
      onStop={stop}
      running={running}
      disabled={fatal !== null}
      placeholder={PLACEHOLDER}
      model={modelName}
      modelFull={modelFull}
      modelCatalog={modelCatalog}
      modelBusy={modelBusy}
      onPickModel={onPickModel}
      onPickLevel={onPickLevel}
      current={currentAction(turn)}
      canFork={sessionId !== null}
      canCompact={sessionId !== null && turn.rows.length > 0}
      onCommand={(id) => void runCommand(id)}
      usage={sessionUsage}
      contextWindow={contextWindow}
      agent={agent}
      agents={agents}
      onPickAgent={setAgent}
      onSuggest={sendSuggestion}
      kbItems={kbItems}
      kbIds={kbIds}
      onPickKbs={setKbIds}
    />
  );

  return (
    <div
      className="shell"
      {...(railCollapsed ? { "data-rail": "collapsed" } : {})}
      {...(filesOpen ? { "data-files": "open" } : {})}
    >
      <aside className="rail">
        <Rail
          sessions={sessions}
          current={sessionId}
          busy={running}
          now={now}
          onSelect={(id) => void openSession(id)}
          onCreate={() => void createSession()}
          onRenameSession={askRenameSession}
          onForkSession={(id) => void forkSession(id)}
          onDeleteSession={askDeleteSession}
          settingsOpen={view === "settings"}
          settingsTriggerRef={settingsTriggerRef}
          onOpenSettings={() => setView("settings")}
          railCollapsed={railCollapsed}
          onToggleCollapse={() => setRailCollapsed((v) => !v)}
          onExpandRail={() => setRailCollapsed(false)}
        />
      </aside>

      <main className="work">
        {/* 眉条:**只有会话名**,贴左(20px 内衬)、16px/500 —— 位置与字号都按 dsh 的
            会话头(`ConversationRoot.module.css` 的 `padding: ... 0 20px` + `.crumb` 14px
            那一档)来,不跟下面居中的正文对齐。

            使用反馈直接要的(先说"像 chat.deepseek.com 一样只显示会话名称",再校正为
            "不用和内容左对齐、要更左,字也别那么大")。旧版这里还挂着 cwd 与「遥测」开关:
            cwd 是诊断事实、不是标题(它在遥测抽屉的「环境」里看),开关挪到了左栏底部
            (与「设置」并列)—— 它是那个抽屉现在唯一的入口。
            `title` 属性给的是全名:名字长时会被省略号截断,悬停能看全。

            空态**整条不渲染**(§17.7):一屏白底 —— 居中 hero + 输入卡,顶上什么都没有。
            代价写进 design/web.md §17.4:第一次发言之前没有开遥测的入口(现在入口在左栏,
            所以这条代价已经不成立了)。 */}
        {empty ? null : (
          <header className="work__head">
            <span className="work__title" title={title || "未命名"}>
              {title || "未命名"}
            </span>
            {/* 右栏开关:dsh 把它放在会话头的角落座位(`ExpandButton`),
                图标是左栏那个 panel 图标的**镜像**(它的 `.icon { scaleX(-1) }`)。 */}
            <button
              type="button"
              className="work__files"
              aria-pressed={filesOpen}
              aria-haspopup="false"
              title={filesOpen ? "收起文件面板" : "展开文件面板"}
              aria-label={filesOpen ? "收起文件面板" : "展开文件面板"}
              onClick={() => setFilesOpen((v) => !v)}
            >
              <IconPanelLeftOutline16 size={15} className="work__filesicon" />
            </button>
          </header>
        )}

        <div className="work__body">
          {fatal ? (
            <div className="blank">
              <div className="blank__inner">
                <div className="notice notice--error">{fatal}</div>
                <button
                  type="button"
                  className="btn"
                  onClick={() => window.location.reload()}
                >
                  重新加载
                </button>
              </div>
            </div>
          ) : empty ? (
            // 空态 = dsh 的 hero:标题 → 输入卡。固定工作目录后不再有项目 chip。
            <div className="hero">
              <div className="hero__stack">
                <div className="hero__headline">Qi Web</div>
                <div className="hero__body">{dock}</div>
              </div>
            </div>
          ) : (
            <>
              <div className="transcript" ref={scrollRef} data-transcript="">
                <div className="transcript__inner">
                  {/* 按**块**渲染:一个回合的思考/工具/叙述合成一个可折叠的过程块
                      (见 `groupProcess`) —— 「哪些行收进块」只有那里一处真相。 */}
                  {groupProcess(turn.rows).map((block) => (
                    <BlockView
                      key={block.key}
                      block={block}
                      sessionId={sessionId}
                      revealActions={block.key === lastActionKey}
                      onCopy={copyMessage}
                      onFork={(entryId) =>
                        void forkSession(sessionId ?? "", entryId)
                      }
                    />
                  ))}
                </div>
              </div>
              {dock}
            </>
          )}
        </div>
      </main>

      {/* 右栏:会话目录的树 + 预览。列宽 0 ↔ clamp 过渡(见 `.shell[data-files]`),
          开着才挂载 —— 收起时不留一棵没人看的树在后台。 */}
      <aside className="files-side">
        {filesOpen ? (
          <FilePanel sessionId={sessionId} onClose={() => setFilesOpen(false)} />
        ) : null}
      </aside>

      {/* 设置浮层。**挂在 `shell` 这一层、而不是 `work` 里**:它要盖住整屏
          (左栏也归它管),而 dsh 的设置面板就是应用之上的一层。`key` 绑定
          落在哪一节 —— 指令菜单的 `/agents` 在浮层已经开着时也要能把它
          拨到 Agent 那一节(重挂载比写一个"同步 prop → state"的 effect 简单,
          而且顺带把滚动位置与未提交的输入都清干净)。 */}
      {view === "settings" ? (
        <Settings
          key={settingsSection}
          config={config}
          initialSection={settingsSection}
          returnFocusTo={settingsTriggerRef}
          onClose={() => setView("work")}
          onChanged={() => void api.config().then(setConfig)}
          /* 宿主支不支持**写** MCP 声明(`/api/meta` 的 `mcp_write`):
             没有它 → 那一节只读(老宿主 / 没装 qi-lab 的同款 UI)。 */
          mcpWrite={meta?.capabilities.mcp_write === true}
          /* 宿主支不支持改写 **A2A 条目**(`/api/meta` 的 `a2a_write`)。同上:没有它 →
             那一节只读 —— 读与「探测」一直在。 */
          a2aWrite={meta?.capabilities.a2a_write === true}
          /* 宿主支不支持**删角色**(`/api/meta` 的 `agent_write`):没有它 → Agent 那一节
             只读 —— 读端点一直在,所以只是少一个键(与 mcp_write 同一条降级规矩)。 */
          agentWrite={meta?.capabilities.agent_write === true}
          /* 宿主支不支持应用助手目录的写端点(`/api/meta` 的 `assistant_write`):
             没有它 → 设置页那一节只读 —— 读端点仍在,只是少一排写键。 */
          assistantWrite={meta?.capabilities.assistant_write === true}
          /* 宿主支不支持**新建 provider**(`/api/meta` 的 `provider_write`)。
             没有它 → 「添加 Provider」只保留“登录已有”。 */
          providerWrite={meta?.capabilities.provider_write === true}
          /* 装了 qi-store 才摆安装/卸载键(`/api/meta` 的 store_write):
             后端是真去 import 一次的结论,不是写死的 True。 */
          storeWrite={meta?.capabilities.store_write === true}
          kbWrite={meta?.capabilities.kb_write === true}
          onKbChanged={() => {
            void api
              .kbAvailable(undefined, undefined, true)
              .then((bound) => setKbItems(bound.items.filter((item) => item.bound)))
              .catch(() => setKbItems([]));
          }}
          /* 技能写端点(`/api/meta` 的 `skill_write`):没有它就不摆「重新扫描」。 */
          skillWrite={meta?.capabilities.skill_write === true}
          /* 角色删完回灌左下角那个角色菜单:它读的是 App 这份 state,不读设置面自己
             那份 —— 不通知它就会留着一个已经不存在的角色。失败时保持原样(不置空)。 */
          onAgentsChanged={() =>
            void api.agentList().then(setAgents).catch(() => undefined)
          }
          onError={setFatal}
        />
      ) : null}

      {/* 会话动作的弹层。写操作一律过二次确认(与凭证写入同一规矩);
          "分叉"是唯一不需要确认的 —— 它只新增文件,不动任何已有数据。 */}
      {dialog === null ? null : (
        <ConfirmDialog
          title={DIALOG_TITLE[dialog.kind]}
          rows={dialogRows(dialog)}
          warning={dialogWarning(dialog)}
          confirmLabel={CONFIRM_LABEL[dialog.kind]}
          danger={DANGEROUS.includes(dialog.kind)}
          onConfirm={() => void confirmDialog()}
          onCancel={() => setDialog(null)}
        >
          {dialog.kind === "rename-session" ? (
            <input
              className="dialog__input"
              value={nameDraft}
              autoFocus
              aria-label="新名称"
              onChange={(event) => setNameDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void confirmDialog();
              }}
            />
          ) : null}
        </ConfirmDialog>
      )}
    </div>
  );
}
