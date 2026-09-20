/**
 * 一个很小的下拉菜单(portal + 视口翻转),qi 的两处菜单共用它:
 *
 *   · **行尾「…」**(默认触发件):会话行的重命名/分叉/删除、工作区行的改名/删除、
 *     未分组桶的清除 —— dsh `ui-workspace/rows/Rows.tsx` 那套;
 *   · **输入卡的「+」**:dsh 那个按钮的 label 是「添加文件或调用指令」
 *     (`aria-haspopup="listbox"`),打开的就是一个菜单 —— 里面"添加文件"那一行
 *     带**回形针**图标(`ui-conversation/apply.ts` 的 `FileCommandRegistry`),
 *     所以 qi 也把附件与命令放在同一张菜单里,不是并排两个按钮。
 *
 * 两处只是**触发件**与**对齐方式**不同(行尾靠右对齐,输入卡靠左对齐),面板与条目
 * 是同一套几何。
 *
 * 几何逐条对拍 dsh `ui-primitives/src/Menu.module.css`:
 * 面板 `padding: 4px`、圆角 20、`--dsw-specific-menu` 底 + `--dsw-elevation-prominent`;
 * 菜单项 `min-height: 40px`、`padding: 8px 10px`、圆角 10、字号 14/22、危险项用错误色。
 *
 * ## 为什么要 portal
 *
 * 侧栏列表是 `overflow-y: auto`、外层 `.rail` 还有 `overflow: hidden` —— 面板若留在行内,
 * 靠近底部的行会把菜单裁掉一半。所以它挂到 `document.body` 上、`position: fixed`,
 * 位置由触发按钮的 `getBoundingClientRect()` 算出,并在放不下时向上翻。
 * (dsh 也是 portal:`Menu` 组件带 `portal` 参数,理由一样。)
 *
 * 关闭方式三种,缺一个都会让人觉得"卡住了":点外面、Esc、选中一项。
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ReactNode } from "react";
import { IconEllipsisOutline16 } from "./icons";

export interface RowMenuItem {
  id: string;
  label: string;
  icon: ReactNode;
  /** 危险动作(删除):文字走错误色,与它上面的安全项拉开距离。 */
  danger?: boolean;
  /** 现在做不了(如"附件:qi 暂不支持"):置灰并保留在列表里 —— 别让人以为它不存在。 */
  disabled?: boolean;
  /** 行尾的补充说明(如「暂不支持」)。 */
  note?: string;
}

/** dsh `Menu.module.css` 的外卡片宽(含内衬)。 */
const MENU_WIDTH = 218;
/** 面板与触发按钮之间的空隙。 */
const GAP = 4;

export function RowMenu({
  subject,
  items,
  onSelect,
  triggerClassName = "rail__rowbtn",
  triggerGlyph,
  triggerLabel = "更多操作",
  align = "end",
}: {
  /** 面板的无障碍名:"会话「xxx」的操作"。 */
  subject: string;
  items: RowMenuItem[];
  onSelect: (id: string) => void;
  /** 触发件的类名(默认是行尾那个 22px 圆形「…」)。 */
  triggerClassName?: string;
  /** 触发件的图形(默认「…」)。 */
  triggerGlyph?: ReactNode;
  /** 触发件的无障碍名与悬停提示 —— 它回答的是"这个按钮做什么",与 `subject` 不同。 */
  triggerLabel?: string;
  /** 面板与触发件的对齐:`end` = 右对齐(行尾用),`start` = 左对齐(输入卡用)。 */
  align?: "start" | "end";
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ left: 0, top: 0 });

  // 打开后立刻量一次:先按"按钮下方"摆,放不下就往上翻(与 dsh 的 flip 同思路)。
  useLayoutEffect(() => {
    if (!open) return;
    const anchor = trigger.current?.getBoundingClientRect();
    if (!anchor) return;
    const height = panel.current?.offsetHeight ?? items.length * 40 + 8;
    const below = anchor.bottom + GAP;
    const flip = below + height > window.innerHeight - 8;
    const desired = align === "end" ? anchor.right - MENU_WIDTH : anchor.left;
    setPos({
      left: Math.max(8, Math.min(desired, window.innerWidth - MENU_WIDTH - 8)),
      top: flip ? Math.max(8, anchor.top - height - GAP) : below,
    });
  }, [align, open, items.length]);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (panel.current?.contains(target) || trigger.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className={triggerClassName}
        data-open={open}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={triggerLabel}
        title={triggerLabel}
        onClick={(event) => {
          // 一级行整行都是"折叠/展开"的点击区,`…` 必须自己吞掉这次点击。
          event.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        {triggerGlyph ?? <IconEllipsisOutline16 size={16} />}
      </button>

      {open
        ? createPortal(
            <div
              ref={panel}
              className="rowmenu"
              role="menu"
              aria-label={subject}
              style={{ left: pos.left, top: pos.top, width: MENU_WIDTH }}
              onClick={(event) => event.stopPropagation()}
            >
              {items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  role="menuitem"
                  className="rowmenu__item"
                  data-danger={item.danger === true}
                  disabled={item.disabled === true}
                  onClick={() => {
                    setOpen(false);
                    onSelect(item.id);
                  }}
                >
                  <span className="rowmenu__icon">{item.icon}</span>
                  <span className="rowmenu__label">{item.label}</span>
                  {item.note === undefined ? null : (
                    <span className="rowmenu__note">{item.note}</span>
                  )}
                </button>
              ))}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
