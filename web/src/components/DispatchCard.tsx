/**
 * 分派卡:qi 相对其它 harness 的差异化 —— 把 dispatcher 的决策摊开。
 *
 * 置信度默认只显示**二元/三元信号**(高/中/低),不把 `0.93` 当大数字摆出来:
 * 可用性结论是二元信号更能带来快速准确的干预决策。精确值放在 title 里。
 * 低置信时给一条**可执行的补救路径**(@点名),而不是只报忧。
 *
 * 阈值与后端一致:0.6 是 dispatcher 的 `confidence_min`(settings.json 可配),
 * 低于它即意味着"可能选错 agent";0.8 以上才算明确命中。
 */
import type { DispatchView } from "../state/turn";

function level(confidence: number): { label: string; soft: boolean } {
  if (confidence >= 0.8) return { label: "高置信", soft: false };
  if (confidence >= 0.6) return { label: "中置信", soft: false };
  return { label: "低置信", soft: true };
}

const SOURCE_LABEL: Record<string, string> = {
  router: "router",
  rules: "rules",
  sticky: "sticky",
  manual: "manual",
  fallback: "fallback",
};

export function DispatchCard({ dispatch }: { dispatch: DispatchView }) {
  const { data } = dispatch;
  const { label, soft } = level(data.confidence);
  const cls = [
    "dispatch",
    soft ? "dispatch--soft" : "",
    data.source === "manual" ? "dispatch--manual" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={cls} title={`置信度 ${data.confidence.toFixed(2)}`}>
      <div className="dispatch__head">
        → {data.display_name || data.agent}
        <span className="dispatch__conf">
          {" "}
          · {SOURCE_LABEL[data.source] ?? data.source} · {label}
        </span>
      </div>
      {data.reasoning ? (
        <div className="dispatch__reason">理由:{data.reasoning}</div>
      ) : null}
      {soft ? (
        <div className="dispatch__reason">
          不是你要的 agent?输入 @名字 直接点名。
        </div>
      ) : null}
    </div>
  );
}
