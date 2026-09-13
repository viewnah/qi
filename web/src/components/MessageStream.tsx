/**
 * 消息流:把 `Item[]` 按**到达顺序**渲染 —— 不做任何重排。
 *
 * 顺序就是后端 `runtime.stream()` 的因果顺序(叙述 → 工具 → 叙述 → … → 最终回答),
 * 与落盘顺序一致,所以刷新前后看到的排布相同(docs/web.md §13 的顺序不变量)。
 */
import { DispatchCard } from "./DispatchCard";
import { ToolRow } from "./ToolRow";
import type { Item, MessageView } from "../state/turn";

/** 说话人标签。用户消息**不标说话人**:agent_id 只是"将处理它的 agent",不是发言者。 */
function who(message: MessageView): string | null {
  switch (message.role) {
    case "user":
      return null;
    case "narration":
      return `${message.agent ?? "qi"} · 过程`;
    case "assistant":
      return message.agent ?? "qi";
    case "error":
      return "错误";
    case "opening":
      return null;
    default:
      return null;
  }
}

export function MessageStream({
  items,
  onSuggestion,
}: {
  items: Item[];
  onSuggestion: (text: string) => void;
}) {
  return (
    <div className="stream" role="log" aria-live="polite">
      <div className="stream__inner">
        {items.map((item) => {
          if (item.kind === "dispatch") {
            return (
              <DispatchCard key={item.dispatch.key} dispatch={item.dispatch} />
            );
          }
          if (item.kind === "tool") {
            return <ToolRow key={item.tool.key} tool={item.tool} />;
          }
          const message = item.message;
          const label = who(message);
          return (
            <div key={message.key} className={`msg msg--${message.role}`}>
              {label ? <div className="msg__who">{label}</div> : null}
              <div className="msg__body">
                {message.text}
                {message.streaming ? (
                  <span className="msg__caret" aria-hidden="true" />
                ) : null}
              </div>
              {message.suggestions && message.suggestions.length > 0 ? (
                <div className="suggestions">
                  {message.suggestions.map((text) => (
                    <button
                      key={text}
                      type="button"
                      className="chip"
                      onClick={() => onSuggestion(text)}
                    >
                      {text}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
