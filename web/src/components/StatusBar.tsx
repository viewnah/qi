/**
 * 状态条 —— dsh 首页的「统计行」。内容按 qi 的实际情况:
 * 分派模式、当前 agent、模型、token 用量、连接态。
 *
 * 缺值**留空**而不写 0:状态栏写 `0 tokens` 比不写更糟(诚实降级)。
 */
import type { ThemeMode } from "../theme/theme";
import { themeLabel } from "../theme/theme";

export type Connection = "idle" | "open" | "error";

const CONNECTION_LABEL: Record<Connection, string> = {
  idle: "",
  open: "已连接",
  error: "连接中断",
};

function tokenSummary(usage: Record<string, unknown> | undefined): string {
  if (!usage) return "";
  const total = usage.total_tokens;
  const calls = usage.llm_calls;
  const parts: string[] = [];
  if (typeof total === "number") parts.push(`${total.toLocaleString()} tokens`);
  if (typeof calls === "number") parts.push(`${calls} 次调用`);
  return parts.join(" · ");
}

export function StatusBar({
  model,
  agent,
  usage,
  running,
  connection,
  theme,
  onCycleTheme,
  onToggleSettings,
  view,
}: {
  model: string | null;
  agent: string | null;
  usage: Record<string, unknown> | undefined;
  running: boolean;
  connection: Connection;
  theme: ThemeMode;
  onCycleTheme: () => void;
  onToggleSettings: () => void;
  view: "chat" | "trajectory" | "settings";
}) {
  const tokens = tokenSummary(usage);
  return (
    <div className="statusbar">
      <div className="statusbar__inner">
        <span className="mode">auto</span>
        <span>{agent ?? "未分派"}</span>
        <span className="mono">{model ?? "未配置模型"}</span>
        {tokens ? <span>{tokens}</span> : null}
        <span className="statusbar__spacer" />
        {running ? <span>运行中…</span> : null}
        {connection === "idle" ? null : (
          <span>{CONNECTION_LABEL[connection]}</span>
        )}
        <button
          type="button"
          className="ghost"
          onClick={onToggleSettings}
          aria-pressed={view === "settings"}
        >
          {view === "settings" ? "返回会话" : "设置"}
        </button>
        <button
          type="button"
          className="ghost"
          onClick={onCycleTheme}
          title="切换主题"
        >
          {themeLabel(theme)}
        </button>
      </div>
    </div>
  );
}
