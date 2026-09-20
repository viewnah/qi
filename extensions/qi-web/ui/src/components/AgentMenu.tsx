/**
 * 输入卡右下角的**智能体 chip**(模型名左边)—— 显示当前用谁,并能切换。
 *
 * 语义与 TUI 的 `--agent` / `/agent` **完全一致**(照抄,不另发明):
 *   · **auto(默认)**:不指定,每一轮由分派器重新决定(读 keywords / Router-LLM);
 *   · **某个 agent**:接下来每一轮都直派它(`source: "manual"`,转录里的分派行会写出来)。
 *
 * 三条刻意的口径:
 *   1. **内置兜底 `general`(显示名就是「qi」)不单独列一项** —— 它与 auto 是**同一件事**:
 *      auto 每轮分派,匹配不到就落到它身上。菜单里再列一个「qi」只会让人以为
 *      "qi" 与 "auto" 是两个选择。它的注明写进了 auto 那一条。
 *      代价:想"强制用基座角色、不要路由"时,菜单里点不到了 —— 那条路仍在:
 *      消息里写 `@general`(分派器认 @ 点名,只作用于那一轮)。
 *   2. **不把"上次用到谁"显示成当前值**。会话文件里确实记着 `active_agent`(最近一次分派
 *      的结果),但那是**结果**不是**设置** —— 拿它当 chip 的值,界面会声称"现在钉在
 *      code-reviewer 上",而实际仍是 auto。所以 chip 只表达"设置成什么",默认就是 `auto`。
 *   3. **不落盘**(与 TUI 相同):手动选择是这个客户端的即时设置,刷新后回到 auto。
 *      要按会话粘住就得在后端开一个"会话级手动 agent"的口子,那是另一个决定。
 *   4. **panel portal 到 `body`**(与 `CommandMenu` 同一手法):菜单朝上开、要盖住输入卡
 *      上方的项目 chip 行,而那一行在 hero 态是 `z-index: 10`、`.dock` 只有 1 ——
 *      留在卡片里会被它盖住(§18.16 的指令菜单踩过同一个坑)。portal 出去 + `position: fixed`
 *      按 chip 的 rect 定位,谁的 z-index 都压不住它。
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AgentInfo } from "../api/types";
import {
  IconAgentPresetOutline16,
  IconChevronDownOutline14,
} from "./icons";

export function AgentMenu({
  agents,
  value,
  onPick,
}: {
  agents: AgentInfo[];
  /** 当前选择:agent 名;`null` = auto(分派器决定)。 */
  value: string | null;
  onPick: (name: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  /** 面板的视口坐标(portal 出去后 fixed 定位,只能自己量)。 */
  const [pos, setPos] = useState<{ right: number; bottom: number } | null>(null);
  const chip = useRef<HTMLButtonElement | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);
  const current = agents.find((a) => a.name === value) ?? null;
  // 内置兜底是 auto 的落点(见文件头第 1 条),所以不进菜单 —— 列表里只留"可以选的人"。
  const selectable = agents.filter(
    (a) => !(a.source === "builtin" && a.name === "general"),
  );

  // 量 chip 的位置:面板从它上沿往上长(输入卡在视口底部,朝下会顶出屏幕)。
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
    // 捕获阶段:输入卡内部的滚动/布局变化也要重量(panel 是 fixed,不量就会错位)
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [open]);

  // 点外面 / Esc 关掉。**非模态**浮层:不锁页面、不抢焦点(与项目 chip 同规矩)。
  // 注意 panel 已经 portal 出去,不在 chip 的 DOM 子树里 —— 两处都要判。
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

  // chip 上的字:auto 就说 auto;选了具体的 agent 优先用它的显示名。
  const label =
    value === null ? "auto" : (current?.name || value);

  return (
    <div className="agentchip">
      <button
        ref={chip}
        type="button"
        className="agentchip__btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`切换智能体(当前:${label})`}
        title={
          value === null
            ? "智能体:auto —— 每一轮由分派器按关键词/描述决定"
            : `智能体:${label} —— 每一轮直派它`
        }
        onClick={() => setOpen((v) => !v)}
      >
        <IconAgentPresetOutline16 size={14} />
        <span className="agentchip__label">{label}</span>
        <IconChevronDownOutline14 size={12} className="agentchip__chevron" />
      </button>

      {open && pos !== null
        ? createPortal(
            <div
              ref={panel}
              className="agentmenu"
              role="menu"
              aria-label="选择智能体"
              style={{ right: pos.right, bottom: pos.bottom }}
            >
              <button
                type="button"
                role="menuitemradio"
                aria-checked={value === null}
                className="agentmenu__item"
                onClick={() => {
                  setOpen(false);
                  onPick(null);
                }}
              >
                <span className="agentmenu__name">auto</span>
                <span className="agentmenu__note">
                  每轮由分派器决定,匹配不到就用 qi(内置 general)
                </span>
              </button>
              {selectable.length > 0 ? (
                <div className="projectmenu__sep" role="separator" />
              ) : null}
              {selectable.map((agent) => (
                <button
                  key={agent.name}
                  type="button"
                  role="menuitemradio"
                  aria-checked={agent.name === value}
                  className="agentmenu__item"
                  onClick={() => {
                    setOpen(false);
                    onPick(agent.name);
                  }}
                >
                  <span className="agentmenu__name">
                    {agent.name || agent.name}
                  </span>
                  <span className="agentmenu__note">
                    {agent.source === "project" ? "项目 · " : ""}
                    {agent.source}
                  </span>
                </button>
              ))}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
