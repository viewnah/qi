/**
 * 输入卡上方的**项目 chip** —— dsh `HeroShell` 里 workspace row 的同位。
 *
 * 它存在的理由不是装饰:qi 的"项目"就是会话的 `cwd`,而新建会话时必须先知道
 * 在哪个目录里建。dsh 把这句做成 chip + 菜单,qi 照做,只是数据源是派生的项目
 * 列表(见 `state/projects.ts`)。
 *
 * **两种形态**(照 dsh `WorkspaceChip` 的两种):
 *   · 可交互:还没有会话时 —— 选中的项目就是"下一个会话建在哪";
 *   · 静态回声:`disabled`,会话已经建好了。cwd 是**会话自身的属性**(落在
 *     header 里),此时换项目等于改历史,qi 不做这件事(后端 PATCH 也只支持改名)。
 *     所以这时它不给 hover 反馈、也不显示指针,而不是假装能点。
 */
import { useEffect, useRef, useState } from "react";
import {
  IconChevronDownOutline14,
  IconFolderClose16,
  IconFolderOpen16,
  IconProjectAddOutline16,
} from "./icons";

export interface ProjectOption {
  /** 目录全路径;它就是分组键。 */
  cwd: string;
  label: string;
}

export function ProjectMenu({
  options,
  value,
  disabled,
  onPick,
  onAddWorkspace,
}: {
  options: ProjectOption[];
  /** 当前选中的目录(空串 = 还没有默认目录)。 */
  value: string;
  disabled: boolean;
  onPick: (cwd: string) => void;
  /**
   * 「添加工作区…」——dsh 的 chip 菜单最后一项就是它(`menu.addWorkspace`),
   * 只不过 dsh 点开后弹的是操作系统的目录选择器,qi 弹的是服务器目录选择器。
   */
  onAddWorkspace: () => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement | null>(null);
  const current = options.find((o) => o.cwd === value) ?? null;

  // 点外面/Esc 关掉。菜单是**非模态**的浮层(不是 dialog):它不该锁住页面,
  // 也不该抢焦点,所以只用 pointerdown 监听,不引 ConfirmDialog 那一套遮罩。
  useEffect(() => {
    if (!open) return;
    const onDown = (event: PointerEvent) => {
      if (root.current?.contains(event.target as Node)) return;
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

  // 变成静态回声时,把还开着的菜单收掉(否则浮层会挂在一个 disabled 按钮上)。
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  return (
    <div className="projectchip" ref={root}>
      <button
        type="button"
        className="projectchip__btn"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="选择项目"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="projectchip__folder">
          {current === null ? (
            <IconFolderClose16 size={16} />
          ) : (
            <IconFolderOpen16 size={16} />
          )}
        </span>
        <span className="projectchip__label">
          {current?.label ?? "选择项目"}
        </span>
        <IconChevronDownOutline14 size={12} className="projectchip__chevron" />
      </button>

      {open ? (
        <div className="projectmenu" role="menu" aria-label="项目">
          {options.map((option) => (
            <button
              key={option.cwd}
              type="button"
              role="menuitemradio"
              aria-checked={option.cwd === value}
              className="projectmenu__item"
              onClick={() => {
                setOpen(false);
                onPick(option.cwd);
              }}
            >
              <span className="projectmenu__folder">
                <IconFolderOpen16 size={16} />
              </span>
              <span className="projectmenu__label">{option.label}</span>
              <span className="projectmenu__cwd" title={option.cwd}>
                {option.cwd}
              </span>
            </button>
          ))}
          {/* 分隔线 + 添加工作区(dsh 的菜单也是"列表 + 最后一项加号")。 */}
          <div className="projectmenu__sep" role="separator" />
          <button
            type="button"
            role="menuitem"
            className="projectmenu__item"
            onClick={() => {
              setOpen(false);
              onAddWorkspace();
            }}
          >
            <span className="projectmenu__folder">
              <IconProjectAddOutline16 size={16} />
            </span>
            <span className="projectmenu__label">添加工作区…</span>
          </button>
        </div>
      ) : null}
    </div>
  );
}
