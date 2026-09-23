/**
 * 输入卡右下角的**模型 chip**(智能体 chip 右边)。
 *
 * 以前这里是一个**只读 `<span>`**,注释写着"换模型在「设置」里" ——
 * 而设置页只有 provider 卡片与凭证,**没有**换模型的入口。于是界面能看见模型、却换不了。
 * 这个组件把它补成一个真控件,形态与左侧的智能体 chip 完全一致(同一类东西:
 * 一个"这一轮会怎么走"的设置),菜单同一套 portal + `position: fixed` 定位手法,
 * 理由见 `AgentMenu` 的文件头。
 *
 * ## 三个决定
 *
 * 1. **模型与思考级别在同一个面板里**,分两节。它们是同一件事的两半:选了一个
 *    `reasoning: true` 的模型之后,"用什么档"是紧接着要定的下一个问题。
 *    分成两个 chip 会让输入卡右下变成四个控件(agent / 模型 / 级别 / 发送)。
 *    级别那一节只在**当前模型会思考**时出现 —— 对不思考的模型显示"思考级别"是纯粹的噪音。
 * 2. **显示的是裸模型名**(与 §18.13 一致):`provider/model` 在窄窗口里放不下,
 *    完整标签留给 `title`。菜单里则两者都给 —— 同 id 不同 provider 时靠它分辨。
 * 3. **只列能用的模型**:清单来自后端的 `selectable_models()`(解析得出凭证的
 *    provider,与 TUI 的 `/model` 同一条口径)。以前这里会照列缺凭证的条目并缀一句
 *    “· 缺凭证”,现在它们压根不进菜单 —— 要配凭证去设置页的 provider 卡片
 *    (那里仍旧标着状态)。用户的口径是“我把 mimo logout 了,它怎么还在”。
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ModelCatalog, ModelOption } from "../api/types";
import { IconChevronDownOutline14, IconDataOutline16 } from "./icons";

/** 裸模型名:`provider/model` 里去掉前缀。模型 id 自己**可能带斜杠**
 *  (`deepseek/deepseek-v4.1-flash`),所以按**第一个**斜杠切 —— 切的是 provider
 *  那一段,剩下的原样保留(与后端 `default_model_name` 的口径一致)。 */
export function bareModelName(label: string): string {
  const at = label.indexOf("/");
  return at < 0 ? label : label.slice(at + 1);
}

export function ModelMenu({
  catalog,
  value,
  full,
  busy,
  onPick,
  onPickLevel,
}: {
  /** 清单与当前值(`null` = 还没取到 → 只显示名字,点不开)。 */
  catalog: ModelCatalog | null;
  /** 显示用的裸模型名(取自 `/api/config` 的展示口径)。 */
  value: string;
  /** 完整标签 `provider/model` —— 同 id 不同 provider 时靠它分辨;也用于 tooltip。 */
  full: string;
  /** 一次切换正在往返(禁用重复点击,避免两次请求互相覆盖)。 */
  busy: boolean;
  onPick: (option: ModelOption) => void;
  onPickLevel: (level: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ right: number; bottom: number } | null>(null);
  const chip = useRef<HTMLButtonElement | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);

  // 量 chip 的位置。菜单朝上开(输入卡在视口底部),portal 到 body —— 与 AgentMenu 同一手法。
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

  const current = catalog?.models.find(
    (m) => `${m.provider}/${m.id}` === catalog.currently,
  );
  const reasoning = current?.context_window !== 0 && current !== undefined;
  const title = catalog?.currently
    ? `模型:${catalog.currently}`
    : full
      ? `模型:${full}(清单取不到:菜单只读)`
      : "模型(未配置)";

  return (
    <div className="agentchip">
      <button
        ref={chip}
        type="button"
        className="agentchip__btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`切换模型(当前:${value || "未配置"})`}
        title={title}
        disabled={catalog === null}
        // 与指令菜单同一个理由:`mousedown` 上 `preventDefault` 保住输入框焦点。
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => setOpen((v) => !v)}
      >
        <IconDataOutline16 size={14} />
        <span className="agentchip__label">{value || "未配置模型"}</span>
        {catalog === null ? null : (
          <IconChevronDownOutline14 size={12} className="agentchip__chevron" />
        )}
      </button>

      {open && pos !== null
        ? createPortal(
            <div
              ref={panel}
              className="agentmenu agentmenu--models"
              role="menu"
              aria-label="选择模型"
              style={{ right: pos.right, bottom: pos.bottom }}
              // 切换正在往返时暂停关掉:否则用户点了之后菜单先消失、值还没回来,
              // 看起来像"点了没反应"。
              onPointerDown={(event) => {
                if (busy) event.preventDefault();
              }}
            >
              {catalog === null ? null : (
                <>
                  {catalog.models.map((option) => {
                    const label = `${option.provider}/${option.id}`;
                    const active = label === catalog.currently;
                    return (
                      <button
                        key={label}
                        type="button"
                        role="menuitemradio"
                        aria-checked={active}
                        className="agentmenu__item"
                        onClick={() => {
                          setOpen(false);
                          onPick(option);
                        }}
                      >
                        <span className="agentmenu__name">
                          {active ? "✓ " : "  "}
                          {option.id}
                        </span>
                        <span className="agentmenu__note">
                          {option.provider}
                          {option.context_window > 0
                            ? ` · ${Math.round(option.context_window / 1000)}k`
                            : ""}
                        </span>
                      </button>
                    );
                  })}

                  {/* 思考级别:与模型同一面板的第二节。只在当前模型认这个参数时才画
                      (对不思考的模型显示它,是纯粹的噪音)。 */}
                  {reasoning && catalog.thinking_levels.length > 0 ? (
                    <>
                      <div className="projectmenu__sep" role="separator" />
                      <div className="agentmenu__section" role="presentation">
                        思考级别
                      </div>
                      {catalog.thinking_levels.map((level) => (
                        <button
                          key={level}
                          type="button"
                          role="menuitemradio"
                          aria-checked={level === catalog.thinking_level}
                          className="agentmenu__item"
                          onClick={() => {
                            setOpen(false);
                            onPickLevel(level);
                          }}
                        >
                          <span className="agentmenu__name">
                            {level === catalog.thinking_level ? "✓ " : "  "}
                            {level}
                          </span>
                        </button>
                      ))}
                    </>
                  ) : null}
                </>
              )}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
