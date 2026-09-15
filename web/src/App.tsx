/**
 * 外壳:**左栏(会话) · 中栏(工作台) · 右栏(遥测)**。
 *
 * 三条不变量(其余都是它们的推论):
 *
 * 1. **会话归客户端**。UI 不在流里传历史 —— 后端从本地 JSONL 取上下文。
 *    所以重连语义就是"再发一次 run",不是续传。
 * 2. **乐观先行**。用户消息立刻上屏,不等服务端落盘;`qi.history` 到达时
 *    再用水合结果替换(它抓的是 run 开始前的 entries,所以 hydrate 会把
 *    本轮输入补回去 —— 见 turn.ts 的 pendingUser)。
 * 3. **流结束时以落盘为准**。run 结束后重拉会话明细,保证刷新前后一致。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, ContractError, run } from "./api/client";
import type { ConfigView, Meta, SessionSummary } from "./api/types";
import { Dock } from "./components/Dock";
import { Rail } from "./components/Rail";
import { RowView } from "./components/Rows";
import { Settings } from "./components/Settings";
import { Telemetry } from "./components/Telemetry";
import { HeroMark } from "./components/HeroMark";
import { emptyTurn, fromEntries, reduce, withUserMessage } from "./state/turn";
import type { TurnState } from "./state/turn";
import { applyTheme, cycleTheme, readTheme, themeLabel } from "./theme/theme";
import type { ThemeMode } from "./theme/theme";

const PLACEHOLDER = "描述你想做的事。Enter 发送,Shift+Enter 换行";

/** 从转录尾行推出"此刻在干什么" —— 活动条要显示的那句话。 */
function currentAction(turn: TurnState): string {
  const rows = turn.rows;
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const row = rows[i];
    if (!row) continue;
    if (row.kind === "tool" && row.status === "running") return `执行 ${row.tool}`;
    if (row.kind === "think" && row.live) return "思考中";
    if (row.kind === "say" && row.live) return "生成回答";
    if (row.kind === "route") return "已分派";
  }
  return "运行中";
}

export function App() {
  const [theme, setTheme] = useState<ThemeMode>(readTheme);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [turn, setTurn] = useState<TurnState>(emptyTurn);
  const [draft, setDraft] = useState("");
  const [view, setView] = useState<"work" | "settings">("work");
  const [tele, setTele] = useState(false);
  const abortRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const cwd = turn.host.cwd ?? meta?.default_cwd ?? "";

  useEffect(() => applyTheme(theme), [theme]);

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
        setMeta(await api.meta());
        await refreshSessions();
        setConfig(await api.config());
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

  const openSession = useCallback(async (id: string) => {
    setView("work");
    try {
      const detail = await api.session(id);
      setSessionId(detail.id);
      setTitle(detail.title);
      setTurn({ ...emptyTurn, rows: fromEntries(detail.entries), phase: "idle" });
    } catch (err) {
      setFatal(describe(err));
    }
  }, []);

  const createSession = useCallback(
    async (first?: string) => {
      try {
        const created = await api.createSession("", meta?.default_cwd || undefined);
        await refreshSessions();
        setSessionId(created.id);
        setTitle(created.title);
        setTurn(emptyTurn);
        if (first) void sendTo(created.id, first);
      } catch (err) {
        setFatal(describe(err));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [meta, refreshSessions],
  );

  /** 真正发起一轮:单次 POST,响应就是流。 */
  const sendTo = useCallback((id: string, text: string) => {
    abortRef.current?.();
    setTurn((prev) => withUserMessage(prev, text));
    abortRef.current = run(id, text, {
      onEvent: (ev) => setTurn((prev) => reduce(prev, ev)),
      onClose: () => {
        setTurn((prev) => ({ ...prev, phase: prev.phase === "running" ? "ok" : prev.phase }));
        void refreshSessions();
      },
      onError: (err) => {
        setTurn((prev) => reduce(prev, { type: "RUN_ERROR", message: describe(err) }));
      },
    });
  }, [refreshSessions]);

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    if (sessionId) sendTo(sessionId, text); else void createSession(text);
  }, [createSession, draft, sendTo, sessionId]);

  const stop = useCallback(() => {
    abortRef.current?.();
    abortRef.current = null;
    setTurn((prev) => ({ ...prev, phase: "idle" }));
  }, []);

  const running = turn.phase === "running";
  const contextWindow = 0; // 模型窗口目前不在 /api/config 里,取不到就不画占用条

  return (
    <div className="shell" data-rail="open" data-tele={tele ? "open" : "closed"}>
      <aside className="rail">
        <Rail
          sessions={sessions}
          current={sessionId}
          busy={running}
          onSelect={(id) => void openSession(id)}
          onCreate={() => void createSession()}
          onOpenSettings={() => setView((v) => (v === "settings" ? "work" : "settings"))}
          themeLabel={themeLabel(theme)}
          onCycleTheme={() => setTheme((m) => cycleTheme(m))}
        />
      </aside>

      <main className="work">
        <header className="work__head">
          <span className="work__title">{title || "新会话"}</span>
          <span className="work__cwd" title={cwd}>
            {cwd}
          </span>
          <span className="work__spacer" />
          <button
            type="button"
            className="ghost"
            aria-pressed={tele}
            onClick={() => setTele((v) => !v)}
            title="遥测:分派理由 / 上下文占用 / 动作计数"
          >
            遥测
          </button>
        </header>

        {view === "settings" ? (
          <Settings
            config={config}
            onChanged={() => void api.config().then(setConfig)}
            onError={setFatal}
          />
        ) : (
          <div className="work__body">
            {fatal ? (
              <div className="blank">
                <div className="blank__inner">
                  <div className="notice notice--error">{fatal}</div>
                  <button type="button" className="btn" onClick={() => window.location.reload()}>
                    重新加载
                  </button>
                </div>
              </div>
            ) : turn.rows.length === 0 ? (
              // 空态:工作台,不是广告牌。给"这里有什么"的信息,不给装饰。
              <div className="blank">
                <div className="blank__inner">
                  <span className="blank__mark">
                    <HeroMark hovering={false} />
                  </span>
                  <div className="blank__title">说一句话,让 qi 去分派</div>
                  <div className="blank__hint">
                    一个路由把任务派给最合适的 agent,然后由它自己调工具做完。
                  </div>
                  <div className="blank__facts">
                    <span className="fact">agent 自动分派</span>
                    <span className="fact">工具可折叠</span>
                    <span className="fact">思考可选展开</span>
                    {agentsCount(config) > 0 ? (
                      <span className="fact">{agentsCount(config)} 个可用 agent</span>
                    ) : null}
                  </div>
                </div>
              </div>
            ) : (
              <div className="transcript" ref={scrollRef} data-transcript="">
                <div className="transcript__inner">
                  {turn.rows.map((row) => (
                    <RowView key={row.key} row={row} />
                  ))}
                </div>
              </div>
            )}

            <Dock
              value={draft}
              onChange={setDraft}
              onSend={send}
              onStop={stop}
              running={running}
              disabled={fatal !== null}
              placeholder={PLACEHOLDER}
              hint={config?.default_model ?? "未配置模型"}
              current={currentAction(turn)}
            />
          </div>
        )}
      </main>

      <aside className="tele" hidden={!tele}>
        <Telemetry turn={turn} model={config?.default_model ?? null} contextWindow={contextWindow} />
      </aside>
    </div>
  );
}

/** 可用 agent 数只在空态用一次,单独抽出来避免在 JSX 里塞可选链。 */
function agentsCount(config: ConfigView | null): number {
  if (!config) return 0;
  return config.checks.find((c) => c.name === "默认模型")?.ok ? 1 : 0;
}
