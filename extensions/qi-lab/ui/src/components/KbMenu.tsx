/**
 * 输入卡右下角的**知识库 chip**：这一轮只查哪些已绑定 KB。
 *
 * - 默认空 = 用 qi-kb 的全局/项目绑定；
 * - 选中若干条 = 通过 AG-UI 的 `forwardedProps.kbIds` 传给宿主，
 *   宿主只把它们记进当前会话的进程内 scope；刷新/换会话即回到全局绑定；
 * - 这里只列**已绑定**的 KB（未绑定的先到设置页 → 知识库安装）。
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { KbItem } from "../api/types";
import {
  IconChevronDownOutline14,
  IconDatabaseOutline16,
} from "./icons";

export function KbMenu({
  items,
  value,
  onChange,
}: {
  items: KbItem[];
  /** 选中的 kbId；空 = 用全局绑定。 */
  value: string[];
  onChange: (ids: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ right: number; bottom: number } | null>(null);
  const chip = useRef<HTMLButtonElement | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const measure = () => {
      const rect = chip.current?.getBoundingClientRect();
      if (!rect) return;
      setPos({
        right: Math.max(8, window.innerWidth - rect.right),
        bottom: Math.max(8, window.innerHeight - rect.top + 6),
      });
    };
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (chip.current?.contains(target) || panel.current?.contains(target)) return;
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

  const label = value.length ? `知识库 ${value.length}` : "知识库 auto";
  const toggle = (id: string) => {
    onChange(value.includes(id) ? value.filter((x) => x !== id) : [...value, id]);
  };

  return (
    <div className="agentchip">
      <button
        ref={chip}
        type="button"
        className="agentchip__btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`选择本轮知识库(当前:${label})`}
        title={
          value.length
            ? `本轮只查 ${value.length} 个知识库；清空 = 用全局绑定`
            : "本轮知识库:auto —— 用 qi-kb 的全局/项目绑定"
        }
        onClick={() => setOpen((v) => !v)}
      >
        <IconDatabaseOutline16 size={14} />
        <span className="agentchip__label">{label}</span>
        <IconChevronDownOutline14 size={12} className="agentchip__chevron" />
      </button>

      {open && pos !== null
        ? createPortal(
            <div
              ref={panel}
              className="agentmenu"
              role="menu"
              aria-label="选择本轮知识库"
              style={{ right: pos.right, bottom: pos.bottom }}
            >
              <button
                type="button"
                role="menuitemradio"
                aria-checked={value.length === 0}
                className="agentmenu__item"
                onClick={() => {
                  setOpen(false);
                  onChange([]);
                }}
              >
                <span className="agentmenu__name">auto</span>
                <span className="agentmenu__note">用全局/项目绑定</span>
              </button>
              {items.length > 0 ? (
                <div className="projectmenu__sep" role="separator" />
              ) : (
                <div className="agentmenu__item" role="presentation">
                  <span className="agentmenu__note">还没有已绑定知识库</span>
                </div>
              )}
              {items.map((item) => {
                const checked = value.includes(item.id);
                return (
                  <button
                    key={`${item.provider}:${item.id}`}
                    type="button"
                    role="menuitemcheckbox"
                    aria-checked={checked}
                    className="agentmenu__item"
                    onClick={() => toggle(item.id)}
                  >
                    <span className="agentmenu__name">
                      {checked ? "✓ " : ""}
                      {item.name}
                    </span>
                    <span className="agentmenu__note">
                      {item.provider} · {item.id.slice(0, 8)}
                    </span>
                  </button>
                );
              })}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
