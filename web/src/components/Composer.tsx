/**
 * 输入卡片 —— 照 dsh `InputBar.module.css` + `InputBar.tsx` 的结构复刻:
 *
 *   ┌ 卡片(radius 22 / 无 border / 0.5px box-shadow 环 / elevation-soft)─┐
 *   │  文本域(hero 态 min-height 52 = 两行;docked 态 36)               │
 *   │  ＋(28×28 selector 圆)   模型位            ( 发送 34×34 品牌蓝圆 ) │
 *   └───────────────────────────────────────────────────────────────────┘
 *
 * **工作区行不在这里**。dsh 的 `InputBar` 有一个可选的 `accessory` 插槽,但首页
 * 那条 workspace 行是 `ConversationRoot` 的 `heroWorkspaceRow`,渲染在**卡片上方**
 * 作为兄弟节点(`.composerHero { gap: 8px }` + 行自身 `margin-top: 4px`)。
 * 早先的实现把它塞进卡片的 accessory 里,位置和间距都与 dsh 不同,现已改正。
 *
 * 两处**刻意的**偏离(都写出来,不假装):
 *   · 「＋」附件:qi 还没有附件能力 → 按钮禁用并标注,不做假入口(dsh 是
 *     `IconPlusOutline16 size={14}` 打开指令菜单;qi 沿用同一个字形与尺寸,只是禁用);
 *   · 模型位:dsh 是可切换的 select;qi 的模型来自 settings.json、没有 per-session
 *     切换,所以只**展示**,不做"看起来能选但改了没用"的下拉。
 * 模式选择(dsh 的 agent preset)按你要求**整体不做**。
 * dsh 的按钮都包在 `Tooltip` 原语里;qi 用原生 `title`(悬停外观不是本次目标)。
 */
import { useEffect, useRef, useState } from "react";
import { IconPlusOutline16, SendArrowGlyph, StopGlyph } from "./icons";

export function Composer({
  disabled,
  running,
  variant,
  placeholder,
  model,
  onSend,
  onStop,
}: {
  disabled: boolean;
  running: boolean;
  /** `hero` = 首页(整块居中、输入框两行高)/ `session` = 会话态(吸底) */
  variant: "hero" | "session";
  placeholder: string;
  model: string | null;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const canSend = !disabled && text.trim().length > 0;

  // 随内容长高,上限 = dsh 的 --dsh-composer-text-max-height(336px = 14 行 × 24px)
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
    <div className={`composer ${variant === "hero" ? "composer--hero" : "composer--session"}`}>
      <div className="composer__card">
        <textarea
          ref={inputRef}
          className="composer__input"
          value={text}
          rows={1}
          placeholder={placeholder}
          aria-label={placeholder}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />

        <div className="composer__row">
          <div className="composer__tools">
            <button
              type="button"
              className="composer__add"
              disabled
              title="附件:qi 暂不支持"
              aria-label="添加文件或调用指令(暂不支持)"
            >
              <IconPlusOutline16 size={14} />
            </button>
          </div>
          <div className="composer__trailing">
            <span
              className="composer__mode"
              title="模型来自 settings.json(改模型请用 qi config / qi init)"
            >
              {model ?? "未配置模型"}
            </span>
            {running ? (
              <button
                type="button"
                className="composer__primary"
                onClick={onStop}
                title="停止生成"
                aria-label="停止生成"
              >
                <StopGlyph />
              </button>
            ) : (
              <button
                type="button"
                className="composer__primary"
                disabled={!canSend}
                onClick={submit}
                title="发送消息"
                aria-label="发送消息"
              >
                <SendArrowGlyph />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
