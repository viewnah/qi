/**
 * 外壳:侧栏(280px) + 主区。首页照 dsh 的 `EmptyHero`:
 * 居中的 hero(品牌标 + headline) + 吸底的输入卡片(带 workspace chip)。
 *
 * 与 dsh 的差别(按你的要求):
 *   · **标题换成 qi 自己的**(dsh 是 `hero.headline = "探索未至之境"` + 官方 brand mark);
 *   · **不做模式选择**(dsh 的 agent preset 卡片)—— qi 的分派是 auto/Dispatcher,
 *     首页只保留工作区 chip。
 *
 * 两条沿用下来的设计决定:
 *   · `running` 是独立状态(不是 TurnState.status):用户消息先本地显示、再由服务端落盘,
 *     存在真实竞态,混在一起会让"快照到达时该不该覆盖本地条目"判断出错;
 *   · run 结束后以**落盘结果**为准(重拉会话明细),保证刷新前后一致。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, ContractError, stream } from "./api/client";
import type { ConfigView, Meta, SessionSummary } from "./api/types";
import { Composer } from "./components/Composer";
import { HeroMark } from "./components/HeroMark";
import { MessageStream } from "./components/MessageStream";
import { Settings } from "./components/Settings";
import { Sidebar } from "./components/Sidebar";
import { StatusBar, type Connection } from "./components/StatusBar";
import { Trajectory } from "./components/Trajectory";
import { WorkspaceChip, type WorkspaceOption } from "./components/WorkspaceChip";
import { emptyTurn, fromEntries, reduce, withUserMessage } from "./state/turn";
import type { TurnState } from "./state/turn";
import { applyTheme, cycleTheme, readTheme } from "./theme/theme";
import type { ThemeMode } from "./theme/theme";

interface SessionMeta {
  id: string;
  title: string;
  cwd: string | null;
}

const PLACEHOLDER_HOME = "描述你想要构建的内容, / 调用指令, @ 文件或对话";
const PLACEHOLDER_SESSION = "发消息或创建任务, / 调用指令, @ 文件或对话";

/** 路径 → chip 标签(末段;纯分隔符回落到原串,dsh 的 `workspaceLabel` 同义)。 */
function workspaceLabel(cwd: string): string {
  const parts = cwd.split("/").filter(Boolean);
  return parts.length > 0 ? (parts[parts.length - 1] as string) : cwd;
}

export function App() {
  const [theme, setTheme] = useState<ThemeMode>(readTheme);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [session, setSession] = useState<SessionMeta | null>(null);
  const [turn, setTurn] = useState<TurnState>(emptyTurn);
  const [running, setRunning] = useState(false);
  const [connection, setConnection] = useState<Connection>("idle");
  const [view, setView] = useState<"chat" | "trajectory" | "settings">("chat");
  const [workspace, setWorkspace] = useState<string | null>(null);
  /** mark 的 hover 态挂在 App(而不是 HeroMark 内部),照 dsh:命中区是 hitbox */
  const [markHovering, setMarkHovering] = useState(false);
  const abortRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const describe = (err: unknown): string => {
    if (err instanceof ContractError) return err.message;
    if (err instanceof ApiError) return `${err.detail}(HTTP ${err.status})`;
    return String(err);
  };

  const refreshSessions = useCallback(async () => {
    try {
      const list = await api.sessions();
      setSessions(list.sessions);
    } catch (err) {
      setFatal(describe(err));
    }
  }, []);

  const reloadConfig = useCallback(async () => {
    try {
      setConfig(await api.config());
    } catch (err) {
      setFatal(describe(err));
    }
  }, []);

  const subscribe = useCallback(
    (id: string, runId?: string) => {
      abortRef.current?.();
      abortRef.current = stream(
        id,
        {
          onOpen: () => setConnection("open"),
          onSnapshot: (snapshot) => {
            setSession({
              id: snapshot.id,
              title: snapshot.title,
              cwd: snapshot.cwd,
            });
            // running 时不覆盖本地条目:用户消息此刻可能还没落盘(真实竞态)
            setTurn((prev) =>
              prev.status === "idle"
                ? { ...fromEntries(snapshot.entries), status: "idle" }
                : prev,
            );
          },
          onEvent: (event) => setTurn((prev) => reduce(prev, event)),
          onFinished: (status) => {
            setRunning(false);
            setTurn((prev) => ({
              ...prev,
              status:
                status === "ok"
                  ? "ok"
                  : status === "cancelled"
                    ? "cancelled"
                    : "error",
            }));
            void refreshSessions();
            void api.session(id).then((detail) => {
              setTurn((prev) => ({
                ...fromEntries(detail.entries),
                usage: prev.usage,
                status: prev.status,
              }));
            });
          },
          onError: (err) => {
            setConnection("error");
            setRunning(false);
            setTurn((prev) =>
              reduce(prev, {
                seq: -1,
                kind: "error",
                agent: null,
                tool: null,
                text: describe(err),
                data: {},
              }),
            );
          },
        },
        { runId },
      );
    },
    [refreshSessions],
  );

  useEffect(() => {
    void (async () => {
      try {
        const loaded = await api.meta();
        setMeta(loaded);
        setWorkspace(loaded.default_cwd || null);
        await refreshSessions();
        setConfig(await api.config());
      } catch (err) {
        setFatal(describe(err));
      }
    })();
    return () => abortRef.current?.();
  }, [refreshSessions]);

  const openSession = useCallback(
    async (id: string) => {
      setView("chat");
      setConnection("idle");
      try {
        const detail = await api.session(id);
        setSession({ id: detail.id, title: detail.title, cwd: detail.cwd });
        setTurn({ ...fromEntries(detail.entries), status: "idle" });
        setRunning(detail.running);
        if (detail.running) subscribe(id);
      } catch (err) {
        setFatal(describe(err));
      }
    },
    [subscribe],
  );

  const createSession = useCallback(
    async (firstMessage?: string) => {
      try {
        const created = await api.createSession("", workspace ?? undefined);
        await refreshSessions();
        setSession({ id: created.id, title: created.title, cwd: created.cwd });
        setTurn(emptyTurn);
        if (firstMessage) {
          setRunning(true);
          setTurn((prev) => withUserMessage(prev, firstMessage));
          const accepted = await api.turn(created.id, firstMessage);
          subscribe(created.id, accepted.run_id);
        }
      } catch (err) {
        setFatal(describe(err));
      }
    },
    [refreshSessions, subscribe, workspace],
  );

  const send = useCallback(
    async (text: string) => {
      if (!session) {
        await createSession(text); // 首页直接发:先建会话再跑
        return;
      }
      setTurn((prev) => withUserMessage(prev, text));
      setRunning(true);
      try {
        const accepted = await api.turn(session.id, text);
        subscribe(session.id, accepted.run_id);
      } catch (err) {
        setRunning(false);
        setTurn((prev) =>
          reduce(prev, {
            seq: -1,
            kind: "error",
            agent: null,
            tool: null,
            text: describe(err),
            data: {},
          }),
        );
      }
    },
    [createSession, session, subscribe],
  );

  const stop = useCallback(async () => {
    if (!session) return;
    try {
      await api.cancel(session.id);
    } catch (err) {
      setFatal(describe(err));
    }
  }, [session]);

  const rename = useCallback(
    async (id: string, title: string) => {
      await api.renameSession(id, title);
      if (session?.id === id) setSession({ ...session, title });
      await refreshSessions();
    },
    [refreshSessions, session],
  );

  const remove = useCallback(
    async (id: string) => {
      await api.deleteSession(id);
      if (session?.id === id) {
        setSession(null);
        setTurn(emptyTurn);
      }
      await refreshSessions();
    },
    [refreshSessions, session],
  );

  /** 工作区候选:服务端默认 + 已有会话用过的目录(去重)。 */
  const workspaces: WorkspaceOption[] = (() => {
    const seen = new Map<string, WorkspaceOption>();
    if (meta?.default_cwd) {
      seen.set(meta.default_cwd, {
        cwd: meta.default_cwd,
        label: workspaceLabel(meta.default_cwd),
      });
    }
    for (const item of sessions) {
      if (item.cwd && !seen.has(item.cwd)) {
        seen.set(item.cwd, { cwd: item.cwd, label: workspaceLabel(item.cwd) });
      }
    }
    return [...seen.values()];
  })();

  const lastDispatch = (() => {
    for (let i = turn.items.length - 1; i >= 0; i -= 1) {
      const item = turn.items[i];
      if (item?.kind === "dispatch") return item.dispatch.data.display_name;
    }
    return null;
  })();

  const isHome = turn.items.length === 0;

  return (
    <div className="shell">
      <Sidebar
        sessions={sessions}
        current={session?.id ?? null}
        busy={running}
        onSelect={(id) => void openSession(id)}
        onCreate={() => void createSession()}
        onRename={(id, title) => void rename(id, title)}
        onDelete={(id) => void remove(id)}
        onOpenSettings={() => setView("settings")}
      />

      <main className="main">
        {view === "settings" ? (
          <Settings
            config={config}
            onChanged={() => void reloadConfig()}
            onError={setFatal}
          />
        ) : (
          <>
            {isHome ? null : (
              /* dsh 的 header:标题行(titleRow) + tab 行(tabs) */
              <div className="topbar">
                <div className="topbar__row">
                  <div className="topbar__title">
                    {session?.title || "新会话"}
                  </div>
                  <div className="topbar__cwd mono">
                    {session?.cwd ?? workspace ?? ""}
                  </div>
                </div>
                <div className="tabs" role="tablist">
                  <button
                    type="button"
                    className="tab"
                    role="tab"
                    aria-current={view === "chat"}
                    onClick={() => setView("chat")}
                  >
                    对话
                  </button>
                  <button
                    type="button"
                    className="tab"
                    role="tab"
                    aria-current={view === "trajectory"}
                    onClick={() => setView("trajectory")}
                  >
                    轨迹
                  </button>
                </div>
              </div>
            )}

            {fatal ? (
              <div className="stream">
                <div className="stream__inner">
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
            ) : isHome ? (
              /* 首页照 dsh:
                   1. `.hero` 居中列(= dsh `scrollBody[data-phase=hero]`)
                   2. `.hero__stack` = dsh 的 `composerStack.composerHero`
                   3. `.hero__shell > .hero__inner` = HeroShell 的 `root > stack`
                   4. `.hero__workspaceRow` 是**卡片的兄弟**(卡片上方),不是卡片里的 accessory
                 这条嵌套是刻意的:workspace 行的间距来自 stack 的 `gap: 8px` + 自身
                 `margin-top: 4px`,和 dsh 的 `composerHero` 一致。 */
              <div className="hero">
                <div className="hero__stack">
                  <div className="hero__shell">
                    <div className="hero__inner">
                      <div className="hero__headline">
                        {/* figma 34:10412: 34×25 的 mark 领着标题,gap 10 */}
                        <span
                          className="hero__markHitbox"
                          onMouseEnter={() => {
                            if (
                              window.matchMedia(
                                "(hover: hover) and (prefers-reduced-motion: no-preference)",
                              ).matches
                            ) {
                              setMarkHovering(true);
                            }
                          }}
                          onMouseLeave={() => setMarkHovering(false)}
                        >
                          <HeroMark hovering={markHovering} />
                        </span>
                        <span className="hero__titleGroup">
                          {/* 独立元素:让标题文案与徽章各自可寻址 */}
                          <span>今天想让我做什么?</span>
                          <span className="hero__badge">预览版</span>
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="hero__workspaceRow">
                    <WorkspaceChip
                      workspace={workspace}
                      workspaces={workspaces}
                      disabled={fatal !== null}
                      onPick={setWorkspace}
                    />
                  </div>

                  <Composer
                    disabled={fatal !== null}
                    running={running}
                    variant="hero"
                    placeholder={PLACEHOLDER_HOME}
                    model={config?.default_model ?? null}
                    onSend={(text) => void send(text)}
                    onStop={() => void stop()}
                  />
                </div>
              </div>
            ) : view === "trajectory" ? (
              <Trajectory turn={turn} />
            ) : (
              <>
                <MessageStream
                  items={turn.items}
                  onSuggestion={(text) => void send(text)}
                />
                <Composer
                  disabled={false}
                  running={running}
                  variant="session"
                  placeholder={PLACEHOLDER_SESSION}
                  model={config?.default_model ?? null}
                  onSend={(text) => void send(text)}
                  onStop={() => void stop()}
                />
              </>
            )}
          </>
        )}

        <StatusBar
          model={config?.default_model ?? null}
          agent={lastDispatch}
          usage={turn.usage}
          running={running}
          connection={connection}
          theme={theme}
          onCycleTheme={() => setTheme((mode) => cycleTheme(mode))}
          onToggleSettings={() =>
            setView((v) => (v === "settings" ? "chat" : "settings"))
          }
          view={view}
        />
      </main>
    </div>
  );
}
