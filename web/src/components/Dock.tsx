/**
 * 输入坞:**活动条 + 输入卡片**。两者刻意合成一个视觉单元。
 *
 * 活动条不在页面底边,而是贴着输入框 —— 因为你想知道的是"**此刻**在干什么",
 * 那信息属于手边。底边状态条是终端时代的遗物:它离视线最远,而承载的却是
 * 最需要即时可见的东西。
 *
 * 输入框用 `<textarea>` 单行自增长(max-height 由 CSS 封顶),`Enter` 发送、
 * `Shift+Enter` 换行 —— 与主流 harness 一致。
 */
import { useEffect, useRef } from "react";
import { IconPlusOutline16, SendArrowGlyph, StopGlyph } from "./icons";

export function Dock({
  value,
  onChange,
  onSend,
  onStop,
  running,
  disabled,
  placeholder,
  hint,
  current,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  running: boolean;
  disabled: boolean;
  placeholder: string;
  hint: string;
  /** 当前动作(由转录的最后一行推出)。idle 时为空串。 */
  current: string;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const canSend = !disabled && value.trim().length > 0;

  // 自增长。必须在**行**容器里才有效:列容器里的 `flex:1` 会压过显式 height,
  // 于是 textarea 永远只有 min-height、长不高。
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 320)}px`;
  }, [value]);

  const submit = () => {
    if (!canSend) return;
    onSend();
  };

  return (
    <div className="dock">
      <div className="dock__inner">
        <div className="activity" data-live={running}>
          {running ? <i className="activity__pulse" /> : null}
          <span>{running ? current || "运行中" : "就绪"}</span>
          <span className="activity__spacer" />
          <span>{hint}</span>
        </div>

        <div className="composer">
          <textarea
            ref={ref}
            className="composer__input"
            value={value}
            rows={1}
            placeholder={placeholder}
            aria-label={placeholder}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <div className="composer__row">
            <div className="composer__tools">
              <button
                type="button"
                className="iconbtn"
                disabled
                title="附件:qi 暂不支持"
                aria-label="添加附件(暂不支持)"
              >
                <IconPlusOutline16 size={14} />
              </button>
            </div>
            <div className="composer__trailing">
              {running ? (
                <button
                  type="button"
                  className="sendbtn sendbtn--stop"
                  onClick={onStop}
                  title="停止生成"
                  aria-label="停止生成"
                >
                  <StopGlyph />
                </button>
              ) : (
                <button
                  type="button"
                  className="sendbtn"
                  disabled={!canSend}
                  onClick={submit}
                  title="发送"
                  aria-label="发送"
                >
                  <SendArrowGlyph />
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
