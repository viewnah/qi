/**
 * 输入卡的**指令菜单** —— 样式与结构照 dsh `ui-input-trigger/MenuView.tsx` +
 * `MenuView.module.css`,以及 `ui-commands/src/client/presentation.ts` 的分节规则。
 *
 * 三条从 dsh 抄来的关键几何/行为:
 *
 *   1. **贴着输入卡上沿、与卡片同宽**(dsh 是 `bottom: calc(100% + 4px); left/right: 0`)
 *      —— 它不是"贴在某个按钮下面的小浮层",而是**属于输入卡的那块表面**。
 *      但 qi 把它 **portal 到 `body`**、用输入卡的 `getBoundingClientRect()` 定
 *      `left/width/bottom`,而不是留在卡片里:实测踩过 —— 留在卡片里就困在
 *      `.dock` 的层叠上下文(z-index 1)中,而与它同级、为"项目菜单压过输入卡"
 *      而抬到 z-index 10 的 `.hero__projectrow` 会盖在它上面(**看起来像菜单透明、
 *      底层文字透出来,而且鼠标点不中**)。dsh 同样是顶层 overlay 层思路。
 *   2. **分节点**:dsh 是「添加」+「指令」两节(`section.add` / `section.commands`),
 *      节标题 12/18 500、三级字色、`padding: 6px 10px 2px`。
 *   3. **行的读序是"标题优先"**:`[图标] 标题 /别名 ………… 描述(右对齐)`。
 *      描述右对齐是刻意的 —— 所有描述收在同一条右边缘上,标题因此读成一列。
 *      别名(如 `/fork`)用三级字色,让人顺便学会斜杠写法。
 *
 * **键盘归 Composer(Dock)管**:dsh 用的是 combobox 形态 —— 焦点**始终留在输入框**,
 * 菜单只画一个高亮行(`aria-activedescendant`)。所以这里不接受 focus,行也只在
 * `onMouseDown` 上取(`preventDefault` 保住输入框焦点),真正的 ↑/↓/Enter/Esc 在
 * `Dock` 的 textarea `onKeyDown` 里处理。
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ReactNode, RefObject } from "react";

export interface CommandItem {
  id: string;
  /** 分节名(与 dsh 一样只有两节:「添加」「指令」)。 */
  section: string;
  /** 本地化标题,如「分叉」。 */
  label: string;
  /** 斜杠写法,如 `/fork`;与 `label` 不同才显示。 */
  alias: string;
  /** 右侧那一列说明(右对齐)。 */
  description: string;
  icon: ReactNode;
  disabled?: boolean;
  /** 置灰的原因(如「暂不支持」),附在描述里。 */
  disabledNote?: string;
}

export function CommandMenu({
  items,
  active,
  onPick,
  onHover,
  emptyHint,
  /** 输入卡元素:菜单按它的 rect 定位(左/宽/贴在它上沿 4px)。 */
  anchor,
}: {
  /** 已过滤的条目(顺序即显示顺序,分节按 `section` 变化自动插入标题)。 */
  items: CommandItem[];
  /** 高亮行的下标;`-1` = 没有高亮。 */
  active: number;
  onPick: (index: number) => void;
  onHover: (index: number) => void;
  /** 无匹配时的提示(调用方给文案,这里不拼句子)。 */
  emptyHint: string;
  /** 输入卡的 ref:菜单按它的 rect 定位(左/宽/贴在它上沿 4px)。
   * 用 ref 而不是元素 —— 首帧 `ref.current` 还是 `null`,传元素会让菜单
   * 在第一帧拿不到位置(而且之后也不会自己重量)。 */
  anchor: RefObject<HTMLElement | null>;
}) {
  const optionId = (index: number) => `qi-command-${index}`;
  const [rect, setRect] = useState<DOMRect | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);

  /**
   * 把高亮行滚进视野。
   *
   * 12 条指令 + `max-height: 400px` 必然要滚动,而**高亮是虚拟的**(焦点始终在输入框,
   * 浏览器不会替你滚)—— 不滚的话 ↑↓ 能把高亮移到看不见的行上。dsh 的 `MenuView`
   * 同样是显式 `scrollIntoView({ block: "nearest" })`。
   */
  useEffect(() => {
    if (active < 0) return;
    panel.current
      ?.querySelector(`#${optionId(active)}`)
      ?.scrollIntoView({ block: "nearest" });
  }, [active]);

  // 定位:量输入卡。窗口尺寸变化时重量一次(面板是 fixed,不重量就会错位)。
  useEffect(() => {
    const measure = () => setRect(anchor.current?.getBoundingClientRect() ?? null);
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [anchor]);

  if (rect === null) return null;
  return createPortal(
    <div
      ref={panel}
      id="qi-command-menu"
      className="cmdmenu"
      role="listbox"
      aria-label="调用指令"
      // `position: fixed` + 从输入卡 rect 算出的三个值(portal 出去才压得住任何祖先的 z-index)。
      style={{
        left: rect.left,
        width: rect.width,
        bottom: window.innerHeight - rect.top + 4,
      }}
    >
      {items.length === 0 ? (
        <div className="cmdmenu__empty" role="status">
          {emptyHint}
        </div>
      ) : null}
      {items.map((item, index) => (
        <div key={item.id} className="cmdmenu__row">
          {/* 分节标题:与上一条不同才出现(dsh 的 `item.section !== prev.section`)。 */}
          {item.section === items[index - 1]?.section ? null : (
            <div className="cmdmenu__section" role="presentation">
              {item.section}
            </div>
          )}
          <button
            id={optionId(index)}
            type="button"
            role="option"
            aria-selected={index === active}
            className="cmdmenu__item"
            data-active={index === active}
            disabled={item.disabled === true}
            // mousedown 而不是 click:输入框要保持焦点(combobox 形态),
            // `preventDefault` 阻止焦点被抢走,取值也发生在任何 blur 收尾之前。
            onMouseDown={(event) => {
              event.preventDefault();
              onPick(index);
            }}
            // mousemove 而不是 mouseenter:只有**真的移动**才换高亮 ——
            // 键盘滚动时静止的指针不该把高亮抢回去。
            onMouseMove={index === active ? undefined : () => onHover(index)}
          >
            <span className="cmdmenu__icon" aria-hidden>
              {item.icon}
            </span>
            <span className="cmdmenu__name">{item.label}</span>
            {item.alias.slice(1).toLowerCase() === item.label.toLowerCase() ? null : (
              <span className="cmdmenu__alias">{item.alias}</span>
            )}
            <span className="cmdmenu__desc">
              {item.disabled === true && item.disabledNote !== undefined
                ? `${item.description} · ${item.disabledNote}`
                : item.description}
            </span>
          </button>
        </div>
      ))}
    </div>,
    document.body,
  );
}
