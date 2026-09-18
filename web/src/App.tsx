/**
 * 外壳:**左栏(工作区 → 会话)· 中栏(工作台)· 右侧遥测抽屉**。
 *
 * 版式基线是 dsh 的网页面(`data/dsh.png` 与它的源码):
 *
 * ```text
 * ┌────────────┬──────────────────────────────┐
 * │ 侧栏 280   │ 工作台                        │
 * │ 品牌行 60   │   眉条 52(只有会话名,贴左)      │
 * │ 新会话 38   │   ┌─ 空态 = hero ─┐            │
 * │ 工作区 36   │   │  mark + 标题   │           │
 * │  项目 34    │   │  项目 chip      │           │
 * │   会话 32   │   │  输入卡 + 统计行  │           │
 * │   ...      │   └───────────────┘           │
 * │ 遥测 / 设置 │                               │
 * └────────────┴──────────────────────────────┘
 * ```
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
 * 放进了垂直居中的 hero stack(卡片上方 12px 是项目 chip),对话态把卡片停靠
 * 在底部。所以这里按 `rows.length === 0` 走两条分支 —— 草稿在 App 的 state 里,
 * 切换分支不会丢字。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, ContractError, run } from "./api/client";
import type {
  AgentInfo,
  ConfigView,
  Meta,
  SessionSummary,
  UsageSummary,
  WorkspaceNames,
} from "./api/types";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { DirPicker } from "./components/DirPicker";
import { Dock } from "./components/Dock";
import { Rail } from "./components/Rail";
import { BlockView } from "./components/Rows";
import { Settings } from "./components/Settings";
import type { SettingsSectionKey } from "./components/Settings";
import { Telemetry } from "./components/Telemetry";
import { ProjectMenu } from "./components/ProjectMenu";
import type { ProjectOption } from "./components/ProjectMenu";
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
import { EMPTY_NAMES, groupByProject, projectLabel } from "./state/projects";
import { commandHelpText } from "./commands";
import type { ProjectGroup } from "./state/projects";
import { applyTheme, readTheme } from "./theme/theme";

const PLACEHOLDER = "描述你想做的事。Enter 发送,Shift+Enter 换行";

/** 行尾相对时间的刷新周期。分钟级标签不需要秒级精度。 */
const CLOCK_MS = 30_000;

/**
 * 左栏四个动作各自的弹层。
 *
 * 为什么是"一个判别联合 + 一个 dialog state"而不是四个独立 state:同一时刻只可能
 * 有一个弹层开着,四个 boolean 会允许出现两个同时为真这种非法状态。
 */
type Dialog =
  | { kind: "rename-session"; session: SessionSummary }
  | { kind: "delete-session"; session: SessionSummary }
  | { kind: "rename-workspace"; group: ProjectGroup }
  | { kind: "delete-workspace"; group: ProjectGroup }
  /** 「未分组」桶的批量清除(那些会话没有 cwd,不属于任何工作区)。 */
  | { kind: "clear-ungrouped"; group: ProjectGroup }
  /** 添加工作区:qi 的工作区就是目录,所以这个动作 = 在某个目录下新建一条会话。 */
  | { kind: "add-workspace" };

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
  "rename-workspace": "重命名工作区",
  "delete-workspace": "删除工作区",
  "clear-ungrouped": "清除未分组的会话",
  "add-workspace": "添加工作区",
};

/** 确认按钮的词:删除说"删除",清除说"清除",改名说"保存"。 */
const CONFIRM_LABEL: Record<Dialog["kind"], string> = {
  "rename-session": "保存",
  "delete-session": "删除",
  "rename-workspace": "保存",
  "delete-workspace": "删除",
  "clear-ungrouped": "清除",
  "add-workspace": "添加",
};

/** 会删数据的那几个:按钮走危险色。 */
const DANGEROUS: Dialog["kind"][] = [
  "delete-session",
  "delete-workspace",
  "clear-ungrouped",
];

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
    case "rename-workspace":
    case "delete-workspace":
      return [
        { label: "目录", value: dialog.group.cwd ?? "(未分组)" },
        { label: "会话", value: `${dialog.group.sessions.length} 条` },
      ];
    case "add-workspace":
      // 没有 dt/dd 可列:这个动作还没有对象,它的"参数"在输入框里(见下面的 children)。
      return [];
    case "clear-ungrouped":
      // 把它们的前几条列出来:清空前至少让人认得出清的是哪些(标题可能是空/重复的)。
      return [
        { label: "会话", value: `${dialog.group.sessions.length} 条` },
        {
          label: "标题",
          value: dialog.group.sessions
            .slice(0, 5)
            .map((one) => one.title || "未命名")
            .join("、"),
        },
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
    case "rename-workspace":
      return "只改显示名,目录本身不动。留空即恢复成目录名。";
    case "delete-workspace":
      // 弹层是纯文本(不渲染 markdown)—— 强调靠措辞与位置,不要写 `**` 星号。
      return (
        "该工作区下的会话会一起被删除,不可恢复。" +
        "目录本身不会被删(qi 不动你的文件夹)。"
      );
    case "add-workspace":
      return "";
    case "clear-ungrouped":
      return (
        "这些会话会被一起删除,不可恢复。" +
        "它们没有工作目录(早先版本的会话 header 里没有 cwd)," +
        "不属于任何工作区 —— 所以只能在这里清掉。"
      );
  }
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
  const [title, setTitle] = useState("");
  const [turn, setTurn] = useState<TurnState>(emptyTurn);
  const [draft, setDraft] = useState("");
  /** 设置**浮层**开着没有(§18.17:它不再是"一个页面",而是盖在工作台上的面板)。 */
  const [view, setView] = useState<"work" | "settings">("work");
  /** 设置浮层打开时落在哪一节(指令菜单的 `/agents` / `/settings` 用)。 */
  const [settingsSection, setSettingsSection] =
    useState<SettingsSectionKey>("models");
  const [tele, setTele] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  /** 下一个新会话落在哪个目录(项目 chip 选的)。空串 = 还不知道,等 /api/meta。 */
  const [draftCwd, setDraftCwd] = useState("");
  /** 工作区改过的显示名(目录 → 名字),由 /api/workspaces 给。 */
  const [overrides, setOverrides] = useState<WorkspaceNames>(EMPTY_NAMES);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [nameDraft, setNameDraft] = useState("");

  const abortRef = useRef<(() => void) | null>(null);
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

  const describe = (err: unknown): string => {
    if (err instanceof ContractError) return err.message;
    if (err instanceof ApiError) return `${err.detail}(HTTP ${err.status})`;
    return String(err);
  };

  const refreshSessions = useCallback(async () => {
    try {
      setSessions((await api.sessions()).sessions);
    } catch (err) {
      setFatal(describe(err));
    }
  }, []);

  // 首屏:先探契约(不匹配要显式报错,不白屏),再拉会话与配置。
  useEffect(() => {
    void (async () => {
      try {
        const m = await api.meta();
        setMeta(m);
        setDraftCwd((prev) => prev || m.default_cwd);
        await refreshSessions();
        // 工作区偏好是**可选能力**,不能因为它把整页打成错误态:
        // 老宿主(或降级部署)没有 `/api/workspaces` 时会回 404,而此时页面
        // 该做的是"分组退回目录名",不是白屏 —— 这是显示名与分组,不是数据源。
        // (实测过:一个比端点更早启动的 `qi web` 进程 + 新前端,整页就死在这里。)
        try {
          setOverrides(await api.workspaces());
        } catch {
          setOverrides(EMPTY_NAMES);
        }
        setConfig(await api.config());
        // 智能体清单只喂那个下拉菜单:取不到不该把整页打成错误态。
        try {
          setAgents(await api.agentList());
        } catch {
          setAgents([]);
        }
      } catch (err) {
        setFatal(describe(err));
      }
    })();
    return () => abortRef.current?.();
  }, [refreshSessions]);

  // 新行到达时贴底(只在用户本来就在底部时 —— 否则会打断向上翻阅)
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [turn.rows]);

  /**
   * 左栏的二级目录:项目 → 会话。派生,不是新数据 —— 只是被两个「人工决定」
   * (改过的显示名、被删掉的工作区)覆盖一层。见 state/projects.ts。
   */
  const groups = useMemo(
    () => groupByProject(sessions, sessionId, overrides),
    [sessions, sessionId, overrides],
  );

  /**
   * 可以新建会话的项目 = 出现过的目录 + 默认目录。
   *
   * 默认目录必须补进来:一个会话都还没有时,`groups` 是空的,但用户得能新建 ——
   * 那时 chip 要显示 `/api/meta` 给的默认目录,而不是「选择项目」。
   */
  const projects = useMemo<ProjectOption[]>(() => {
    const list: ProjectOption[] = [];
    for (const group of groups) {
      if (group.cwd !== null) list.push({ cwd: group.cwd, label: group.label });
    }
    if (draftCwd && !list.some((p) => p.cwd === draftCwd)) {
      list.unshift({
        cwd: draftCwd,
        label: projectLabel(draftCwd) || draftCwd,
      });
    }
    return list;
  }, [groups, draftCwd]);

  const openSession = useCallback(async (id: string) => {
    setView("work");
    try {
      const detail = await api.session(id);
      setSessionId(detail.id);
      setTitle(detail.title);
      setDraftCwd(detail.cwd ?? "");
      // 会话级用量:**后端算的落盘口径**(前端只有窗口,自己求和会少算),
      // 所以它只在"打开会话"与"一轮结束"两个时点刷新。
      // `?? null` 是给**老宿主**的:它的明细里没有 `usage` 字段,直接透传下去
      // 会让 Dock 读到 undefined 而抛错(整页白) —— 与 `/api/workspaces` 同一条降级规矩。
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
          void refreshSessions();
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
      { agent });
    },
    [agent, refreshSessions],
  );

  const createSession = useCallback(
    async (first?: string, cwdForNew?: string) => {
      try {
        const created = await api.createSession(
          "",
          cwdForNew || draftCwd || meta?.default_cwd || undefined,
        );
        await refreshSessions();
        setSessionId(created.id);
        setTitle(created.title);
        setTurn(emptyTurn);
        setSessionUsage(null);      // 新会话:还没有任何用量
        if (first) void sendTo(created.id, first);
      } catch (err) {
        setFatal(describe(err));
      }
    },
    // `sendTo` **必须**列上:它带着「当前智能体」这个设置,而 `sendTo` 在智能体变化时
    // 会重建。漏掉它 → 在 hero 态选好智能体再发第一句时,这里调用的是**旧闭包**
    // (`agent` 还是 null),手动选择静默失效(实测踩过:分派行写的是 fallback)。
    [draftCwd, meta, refreshSessions, sendTo],
  );

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    if (sessionId) sendTo(sessionId, text);
    else void createSession(text);
  }, [createSession, draft, sendTo, sessionId]);

  const stop = useCallback(() => {
    abortRef.current?.();
    abortRef.current = null;
    setTurn((prev) => ({ ...prev, phase: "idle" }));
  }, []);

  /**
   * 左栏四个动作。
   *
   * 全部**先问后端、再以后端回的东西为准**(不做乐观更新):这些都是低频动作,
   * 少一次往返换不来什么,而本地先改再回滚会让"列表和磁盘不一致"这种最难查。
   */
  /**
   * 分叉:`at` 省略 = 当前节点(左栏行尾菜单用);给了 entry id = **从那条消息分叉**
   * (转录里每条消息下面那个按钮用,与 dsh 的 branch 同一语义)。
   *
   * 不论是否需要确认:**分叉只新增文件、不动任何已有数据** —— 与左栏那个分叉同一理由
   * (docs/web.md §18.1)。
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

  const askRenameWorkspace = useCallback((group: ProjectGroup) => {
    setNameDraft(group.label);
    setDialog({ kind: "rename-workspace", group });
  }, []);

  const askDeleteWorkspace = useCallback((group: ProjectGroup) => {
    setDialog({ kind: "delete-workspace", group });
  }, []);

  const askClearUngrouped = useCallback((group: ProjectGroup) => {
    setDialog({ kind: "clear-ungrouped", group });
  }, []);

  /** 添加工作区:预填默认工作目录(常见情况是"就在这儿"),错误先清掉。 */
  const askAddWorkspace = useCallback(() => {
    setDialog({ kind: "add-workspace" });
  }, []);

  /**
   * 目录选择器里点「选择此文件夹」之后:在这个目录下建一条会话并打开它。
   *
   * 回 `null` = 成功(选择器据此关闭自己);回字符串 = 失败,**留在选择器里显示** ——
   * 目录选完才失败(比如刚被删掉)是常见事,不该关掉弹层、更不该把整页打成错误屏。
   */
  const pickWorkspace = useCallback(
    async (path: string): Promise<string | null> => {
      try {
        const created = await api.createSession("", path);
        await refreshSessions();
        await openSession(created.id);
        return null;
      } catch (err) {
        return describe(err);
      }
    },
    [openSession, refreshSessions],
  );

  /**
   * 批量删除之后的收尾:刷新列表,并在**当前正开着的会话也被删掉**时回到空态。
   *
   * 三处删除(单条 / 删除工作区 / 清除未分组)共用它 —— 收尾漏一处就会留下一个
   * 指向已删文件的 sessionId,下一次发言会拿它去 append,报一个谁也看不懂的错。
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
      } else if (target.kind === "rename-workspace") {
        setOverrides(
          await api.renameWorkspace(target.group.cwd ?? "", nameDraft),
        );
      } else if (target.kind === "delete-workspace") {
        // 删除工作区 = **连同它的会话一起删**(不可恢复;目录本身不动)。
        const result = await api.deleteWorkspace(target.group.cwd ?? "");
        setOverrides({ names: result.names });
        await forgetSessions(result.ids);
      } else {
        // 清除「未分组」:那一桶会话一起删。
        const result = await api.clearUngrouped();
        await forgetSessions(result.ids);
      }
    } catch (err) {
      setFatal(describe(err));
    }
    // 依赖必须列全:`cwdDraft` 漏掉过一次,于是弹层里怎么改路径都没用 ——
    // 闭包捕获的是"弹层刚打开时"的值(实测:填了不存在的目录,会话却建到了预填目录里)。
    // `openSession` / `forgetSessions` 同理,它们也在分支里被调用。
  }, [dialog, forgetSessions, nameDraft, openSession, refreshSessions, sessionId]);

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
  // 上下文窗口来自 /api/config(默认模型的 `contextWindow`)。0 = 取不到,
  // 那时不画占用百分比 —— 而不是画一条永远 0% 的。
  const contextWindow = config?.default_model_context_window ?? 0;
  const empty = turn.rows.length === 0;
  // 输入卡右下只显示**裸模型名**(`provider/model` 在窄窗口里放不下);完整标签
  // (`provider/model`)留给 tooltip 与遥测抽屉 —— 同 id 不同 provider 时靠它分辨。
  // `default_model_name` 是本次新加的字段,老宿主不给就回落完整标签(不会变空)。
  const modelName = config?.default_model_name || config?.default_model || "";
  const modelFull = config?.default_model ?? "";

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
      } else if (id === "settings") {
        setSettingsSection("models");
        setView("settings");
      }
    },
    // 依赖列全:漏一个就会拿到过期闭包(这个文件里为此踩过一次,见 docs/web.md §18.8)。
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
      current={currentAction(turn)}
      canFork={sessionId !== null}
      canCompact={sessionId !== null && turn.rows.length > 0}
      onCommand={(id) => void runCommand(id)}
      usage={sessionUsage}
      contextWindow={contextWindow}
      agent={agent}
      agents={agents}
      onPickAgent={setAgent}
    />
  );

  return (
    <div className="shell" data-tele={tele ? "open" : "closed"}>
      <aside className="rail">
        <Rail
          groups={groups}
          current={sessionId}
          busy={running}
          now={now}
          onSelect={(id) => void openSession(id)}
          onCreate={(cwdForNew) => void createSession(undefined, cwdForNew)}
          onRenameSession={askRenameSession}
          onForkSession={(id) => void forkSession(id)}
          onDeleteSession={askDeleteSession}
          onRenameWorkspace={askRenameWorkspace}
          onDeleteWorkspace={askDeleteWorkspace}
          onClearUngrouped={askClearUngrouped}
          onAddWorkspace={askAddWorkspace}
          settingsOpen={view === "settings"}
          settingsTriggerRef={settingsTriggerRef}
          onOpenSettings={() => setView("settings")}
          telemetryOpen={tele}
          onToggleTelemetry={() => setTele((v) => !v)}
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
            代价写进 docs/web.md §17.4:第一次发言之前没有开遥测的入口(现在入口在左栏,
            所以这条代价已经不成立了)。 */}
        {empty ? null : (
          <header className="work__head">
            <span className="work__title" title={title || "未命名"}>
              {title || "未命名"}
            </span>
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
            // 空态 = dsh 的 hero:垂直居中,标题 → 项目 chip → 输入卡,间隔 12。
            // 标题就是字标「Qi Web」本身 —— 不放 mark、不放角标。
            <div className="hero">
              <div className="hero__stack">
                <div className="hero__headline">Qi Web</div>
                <div className="hero__body">
                  <div className="hero__projectrow">
                    <ProjectMenu
                      options={projects}
                      value={sessionId ? cwd : draftCwd}
                      // 会话一旦建好,cwd 就是它的属性了 —— chip 变成静态回声。
                      disabled={sessionId !== null || projects.length === 0}
                      onPick={setDraftCwd}
                      onAddWorkspace={askAddWorkspace}
                    />
                  </div>
                  {dock}
                </div>
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

      <aside className="tele">
        <div className="tele__head">
          <span className="tele__title">遥测</span>
          <button
            type="button"
            className="ghost"
            onClick={() => setTele(false)}
            aria-label="关闭遥测"
          >
            关闭
          </button>
        </div>
        <Telemetry
          turn={turn}
          model={config?.default_model ?? null}
          contextWindow={contextWindow}
        />
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
          onError={setFatal}
        />
      ) : null}

      {/* 四个动作的弹层。写操作一律过二次确认(与凭证写入同一规矩);
          "分叉"是唯一不需要确认的 —— 它只新增文件,不动任何已有数据。 */}
      {dialog !== null && dialog.kind === "add-workspace" ? (
        <DirPicker
          initialPath={draftCwd || meta?.default_cwd || ""}
          onPick={pickWorkspace}
          onClose={() => setDialog(null)}
        />
      ) : null}

      {dialog === null || dialog.kind === "add-workspace" ? null : (
        <ConfirmDialog
          title={DIALOG_TITLE[dialog.kind]}
          rows={dialogRows(dialog)}
          warning={dialogWarning(dialog)}
          confirmLabel={CONFIRM_LABEL[dialog.kind]}
          danger={DANGEROUS.includes(dialog.kind)}
          onConfirm={() => void confirmDialog()}
          onCancel={() => setDialog(null)}
        >
          {dialog.kind === "rename-session" ||
          dialog.kind === "rename-workspace" ? (
            <input
              className="dialog__input"
              value={nameDraft}
              autoFocus
              aria-label="新名称"
              placeholder={
                dialog.kind === "rename-workspace" ? "留空 = 用目录名" : ""
              }
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
