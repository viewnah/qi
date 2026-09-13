/**
 * 轨迹视图 —— 把一轮会话的**事实**逐条摊开(审计/回放用)。
 *
 * dsh 的轨迹是**甘特式时间轴**(`TrajectoryTimeline.module.css`:50px 高的 plot、
 * 按类型着色的 span、拖拽平移/缩放/框选/搜索)。那要求每个 span 自带耗时与首字延迟;
 * qi 现在只有 `tool.duration_ms`、轮次级 `usage` 和事件 `seq`,**做不出真实甘特**。
 * 所以这里做的是**纵向审计线**:逐条列出会话事实,但沿用 dsh 的视觉语言
 * (10px 序号、徽章、按类型着色的 2px 色条、0.5px 发丝分隔),色条映射也与它一致:
 *   user=品牌蓝 · context=绿混灰 · tool=琥珀 · error=红 · 其余中性
 *
 * 好处是它**不需要任何新后端能力**:数据全在 `turn.items` 与 `turn.usage` 里,
 * 与对话视图解释同一份事实(只是投影方式不同)。
 */
import type { TurnState } from "../state/turn";

interface Row {
  key: string;
  tone: "user" | "assistant" | "context" | "tool" | "error" | "dispatch";
  badge: string;
  title: string;
  detail?: string;
  mono?: boolean;
}

function toolArgs(args: Record<string, unknown>): string {
  const parts = Object.entries(args).map(([key, value]) => {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return `${key}=${text}`;
  });
  const joined = parts.join(" ");
  return joined.length > 120 ? `${joined.slice(0, 120)}…` : joined;
}

function rowsOf(turn: TurnState): Row[] {
  return turn.items.map((item, index) => {
    if (item.kind === "dispatch") {
      const { data } = item.dispatch;
      return {
        key: `d-${index}`,
        tone: "dispatch",
        badge: "分派",
        title: `→ ${data.display_name || data.agent} · ${data.source} · ${data.confidence.toFixed(2)}`,
        detail: data.reasoning || undefined,
      };
    }
    if (item.kind === "tool") {
      const tool = item.tool;
      const mark =
        tool.status === "running" ? "…" : tool.status === "ok" ? "✓" : "✗";
      const cost = [
        typeof tool.durationMs === "number" ? `${tool.durationMs}ms` : "",
        typeof tool.exitCode === "number" && tool.exitCode !== 0
          ? `exit=${tool.exitCode}`
          : "",
      ]
        .filter(Boolean)
        .join(" ");
      return {
        key: `t-${index}`,
        tone: "tool",
        badge: "工具",
        title: `${mark} ${tool.tool}(${toolArgs(tool.args)})${cost ? ` · ${cost}` : ""}`,
        detail: tool.result
          ? tool.result.split("\n").slice(0, 4).join("\n")
          : undefined,
        mono: true,
      };
    }
    const message = item.message;
    const tone =
      message.role === "user"
        ? "user"
        : message.role === "error"
          ? "error"
          : message.role === "assistant"
            ? "assistant"
            : "context";
    const badge =
      message.role === "user"
        ? "用户"
        : message.role === "assistant"
          ? "助手"
          : message.role === "narration"
            ? "叙述"
            : message.role === "opening"
              ? "开场"
              : "错误";
    return {
      key: `m-${index}`,
      tone,
      badge,
      title: message.agent ? `${badge} · ${message.agent}` : badge,
      detail: message.text,
    };
  });
}

export function Trajectory({ turn }: { turn: TurnState }) {
  const rows = rowsOf(turn);
  const tools = rows.filter((row) => row.tone === "tool").length;
  const dispatches = rows.filter((row) => row.tone === "dispatch").length;
  const usage = turn.usage;

  return (
    <div className="trajectory">
      <div className="trajectory__toolbar">
        <span className="trajectory__count">{rows.length} 条事实</span>
        <span>分派 {dispatches}</span>
        <span>工具 {tools}</span>
        {typeof usage?.total_tokens === "number" ? (
          <span>{usage.total_tokens.toLocaleString()} tokens</span>
        ) : null}
        <span className="trajectory__spacer" />
        <span>与对话视图同源(同一份事件流)</span>
      </div>

      {rows.length === 0 ? (
        <div className="trajectory__empty">本轮还没有事实可展开。</div>
      ) : (
        <div className="trajectory__scroll">
          <ol className="trajectory__list">
            {rows.map((row, index) => (
              <li
                className="trajectory__row"
                data-tone={row.tone}
                key={row.key}
              >
                <span className="trajectory__index">{index}</span>
                <div className="trajectory__body">
                  <div className="trajectory__head">
                    <span className="trajectory__badge">{row.badge}</span>
                    <span className={row.mono ? "trajectory__mono" : undefined}>
                      {row.title}
                    </span>
                  </div>
                  {row.detail ? (
                    <div
                      className={`trajectory__detail${row.mono ? " trajectory__mono" : ""}`}
                    >
                      {row.detail}
                    </div>
                  ) : null}
                </div>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
