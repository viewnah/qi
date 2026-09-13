/**
 * 输入卡片 —— 照 dsh `InputBar.module.css` 的结构复刻:
 *
 *   ┌ 卡片(radius 22 / 无 border / 0.5px box-shadow 环 / elevation-soft)─┐
 *   │  📁 workspace chip ▾        ← accessory 行(仅首页)               │
 *   │  文本域(首页 min-height 52)                                      │
 *   │  ＋        模型                 ( 发送 34×34 品牌蓝圆 )            │
 *   └───────────────────────────────────────────────────────────────────┘
 *
 * 两处**刻意的**偏离(都写出来,不假装):
 *   · 「＋」附件:qi 还没有附件能力 → 按钮禁用并标注,不做假入口;
 *   · 模型位:dsh 是可切换的 select;qi 的模型来自 settings.json、没有 per-session
 *     切换,所以只**展示**,不做"看起来能选但改了没用"的下拉。
 * 模式选择(dsh 的 agent preset)按你要求**整体不做**。
 */
import { useEffect, useRef, useState } from "react";
import { WorkspaceChip, type WorkspaceOption } from "./WorkspaceChip";

export function Composer({
  disabled,
  running,
  variant,
  placeholder,
  model,
  workspaces,
  workspace,
  onPickWorkspace,
  onSend,
  onStop,
}: {
  disabled: boolean;
  running: boolean;
  /** `hero` = 首页(整块居中、带 accessory 行、输入框更高) */
  variant: "hero" | "session";
  placeholder: string;
  model: string | null;
  workspaces: WorkspaceOption[];
  workspace: string | null;
  onPickWorkspace: (cwd: string | null) => void;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const canSend = !disabled && text.trim().length > 0;

  // 随内容长高,上限 = dsh 的 --dsh-composer-text-max-height(336px)
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 336)}px`;
  }, [text]);

  const submit = () => {
    if (!canSend) return;
    onSend(text.trim());
    setText("");
  };

  return (
    <div
      className={`composer ${variant === "hero" ? "composer--hero" : "composer--session"}`}
    >
      <div className="composer__card">
        {variant === "hero" ? (
          <div className="composer__accessory">
            <WorkspaceChip
              workspace={workspace}
              workspaces={workspaces}
              onPick={onPickWorkspace}
            />
          </div>
        ) : null}

        <div className="composer__inner">
          <textarea
            ref={inputRef}
            className="composer__input"
            value={text}
            rows={1}
            placeholder={placeholder}
            aria-label="输入消息"
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
          />
        </div>

        <div className="composer__row">
          <div className="composer__tools">
            <button
              type="button"
              className="composer__add"
              disabled
              title="附件:qi 暂不支持"
              aria-label="添加附件(暂不支持)"
            >
              ＋
            </button>
          </div>
          <div className="composer__modes">
            <span
              className="composer__mode"
              title="模型来自 settings.json(改模型请用 qi config / qi init)"
            >
              {model ?? "未配置模型"}
            </span>
          </div>
          <div className="composer__trailing">
            {running ? (
              <button
                type="button"
                className="composer__send"
                onClick={onStop}
                title="停止生成"
                aria-label="停止生成"
              >
                ■
              </button>
            ) : (
              <button
                type="button"
                className="composer__send"
                disabled={!canSend}
                onClick={submit}
                title="发送"
                aria-label="发送"
              >
                ↑
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
