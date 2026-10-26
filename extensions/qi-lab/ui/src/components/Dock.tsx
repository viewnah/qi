/**
 * 输入坞:**活动条 + 输入卡片**。
 *
 * 版式照 dsh 的 `InputBar`(`ui-conversation/src/client/skeleton/InputBar.module.css`):
 * 卡片半径 22、上内衬 8、文本区与按钮行之间 12、按钮行内衬 `2px 8px 6px`,
 * 发送是 **34px 圆 + `button-info-fill` 底 + 静态白箭头**(不是"主按钮墨色"),
 * 输入区最小高度在 hero 态 52、停靠态 36。
 *
 * 工具行左边是**两颗 28px 圆钮**:**加号 = 调用指令**(打开命令菜单)、
 * **回形针 = 附件**。
 *
 * 这条是**量截图**定的,不是读仓库源码定的 —— 教训记在这里:那个仓库副本的
 * `InputBar.tsx` 里只有一颗可见的 `+`(label「添加文件或调用指令」,旁边是 hidden
 * 的 `<input type="file">`),而 `data/dsh.png` 里明明是两颗:dsh.png 的按钮行左起
 * 三个 28px 圆钮,中心在 CSS `427.2 / 467.2 / 508.2`(间距 40–41 = 28 + 12 gap),
 * 之后才是文字「工作区内修改」(那是它的权限 chip,qi 没有这个功能)。
 * 也就是说**截图的版本比仓库副本新**:加号已经只管指令,附件分到了右边的回形针。
 * 两边冲突时以截图为准(用户看的就是它)。
 *
 * 两处**qi 自己的**东西,都留着:
 *   1. **活动条**(`activity`):回答"此刻在干什么"。dsh 的同一位置(卡片**上方**)
 *      是它自己的状态条(status strip);qi 用它报**动作**。
 *   2. **停止按钮**:dsh 没有(它没有"取消这一轮"的出口)。qi 的取消=客户端断开,
 *      所以运行中发送键变成停止键,**同一个圆**,不新增控件。
 *
 * **卡片下方那行统计**(`stats`)是这次加的,形状照 dsh 的 `StatsPills`
 * (`ui-chat/src/client/chat/StatsPills.module.css`):两颗图标药丸 —— 仪表盘那颗是
 * 会话计数,数据库那颗是 token 用量。三条与 dsh 一致的做法:
 *   · **有数据才画**(它那边是 `steps===0 && !hasTokens` 时整行不渲染);行不在时
 *     底部间隙是 8px,在时收到 4px(行自带 4px 上内衬),见 app.css 的 `:has()` 规则;
 *   · 药丸是**静态读数**而不是按钮:qi 没有 dsh 那种展开详情面板(那颗面板是它自己的
 *     时间 / 用量对话框),所以不做"点了没反应的控件";
 *   · 图标沿用 dsh 那两颗(Gauge / Database),尺寸 14。
 *
 * 一处**口径差异**,写清楚:dsh 的行靠活投影,流式途中数字就在跳;qi 的行读的是
 * **落盘口径**(`/api/sessions/{id}` 的 `usage`)—— 运行中那一轮要等它落盘才计入,
 * 所以它永远比屏幕上的对话慢一轮。换来的是刷新/重开之后数字不会变。
 *
 * 文本域用 `<textarea>` 自增长(max-height 由 CSS 封顶):`Enter` 发送、
 * `Shift+Enter` 换行。dsh 用 contenteditable(它要内联 chip),qi 不需要。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentMenu } from "./AgentMenu";
import { KbMenu } from "./KbMenu";
import { CommandMenu } from "./CommandMenu";
import { ModelMenu } from "./ModelMenu";
import { COMMANDS } from "../commands";
import { contextShare, countsLabel, formatTokens } from "../state/stats";
import type { AgentInfo, KbItem, ModelCatalog, ModelOption, UsageSummary } from "../api/types";
import {
  IconDatabaseOutline16,
  IconGaugeOutline16,
  IconPaperclipOutline16,
  IconPlusOutline16,
  SendArrowGlyph,
  StopGlyph,
} from "./icons";

export function Dock({
  value,
  onChange,
  onSend,
  onStop,
  running,
  disabled,
  placeholder,
  model,
  modelFull,
  modelCatalog,
  modelBusy,
  onPickModel,
  onPickLevel,
  current,
  canFork,
  canCompact,
  onCommand,
  usage,
  contextWindow,
  agent,
  agents,
  onPickAgent,
  onSuggest,
  kbItems,
  kbIds,
  onPickKbs,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  running: boolean;
  disabled: boolean;
  placeholder: string;
  /** 右下的模型名(**不带 provider**,放不下)。 */
  model: string;
  /** 鼠标悬停时显示的完整标签 `provider/model` —— 同 id 不同 provider 时靠它分辨。 */
  modelFull: string;
  /** 模型清单 + 当前值(`GET /api/models`)。null = 取不到 → chip 只读(不画 chevron)。 */
  modelCatalog: ModelCatalog | null;
  /** 一次切换正在往返(禁用重复点)。 */
  modelBusy: boolean;
  onPickModel: (option: ModelOption) => void;
  onPickLevel: (level: string) => void;
  /** 当前动作(由转录的最后一行推出)。idle 时为空串。 */
  current: string;
  /** 有会话才能分叉(命令菜单里那一条据此置灰)。 */
  canFork: boolean;
  /** 有内容才值得压缩(空会话的"压缩"点了没有任何反应)。 */
  canCompact: boolean;
  /** 命令菜单选中某一条(动作在 App 里:`new` / `fork` / `settings`)。 */
  onCommand: (id: string) => void;
  /** 会话级用量(后端算的**落盘口径**)。null = 还没选中会话。 */
  usage: UsageSummary | null;
  /** 默认模型的上下文窗口。0 = 取不到 → 不画占用百分比(不是"窗口是 0")。 */
  contextWindow: number;
  /** 当前钉住的智能体(null = auto,由分派器每轮决定)。 */
  agent: string | null;
  /** 可选的智能体清单(菜单里列出来;取不到就只剩 auto)。 */
  agents: AgentInfo[];
  onPickAgent: (name: string | null) => void;
  /** 点选智能体自带的一条快捷提问:App 直接把它当成一条 user 消息提交。 */
  onSuggest: (text: string) => void;
  /** 已绑定 KB（会话级选择用；空 = 用全局绑定）。 */
  kbItems: KbItem[];
  kbIds: string[];
  onPickKbs: (ids: string[]) => void;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  /** 输入卡本身:指令菜单按它的 rect 定位(见 CommandMenu 的文件头)。 */
  const cardRef = useRef<HTMLDivElement | null>(null);
  /** 菜单面板(portal 到 body):判断“点外部 / 焦点离开”时要用它。 */
  const menuRef = useRef<HTMLDivElement | null>(null);
  /** 「+」触发件:点它交给自己的 toggle,不被“点外部关闭”抢走。 */
  const addRef = useRef<HTMLButtonElement | null>(null);
  const canSend = !disabled && value.trim().length > 0;
  /** 当前智能体的 `opening`（选择 agent 时就能拿到，不依赖 run）。 */
  const opening = agent === null
    ? null
    : agents.find((item) => item.name === agent)?.opening ?? null;
  /** 一级分组：无标题组 = 全域基线；有标题组 = 可筛选的作战样式。 */
  const allGroups = (opening?.groups ?? [])
    .map((group) => ({
      ...group,
      title: group.title?.trim() ?? "",
      items: group.items?.filter((text) => text.trim().length > 0) ?? [],
    }))
    .filter((group) => group.items.length > 0);
  const baseline = allGroups
    .filter((group) => group.title.length === 0)
    .flatMap((group) => group.items);
  const groups = allGroups.filter((group) => group.title.length > 0);
  const grouped = groups.length > 0;
  /**
   * 已选一级标题。**初始为空 = 只显示标题，不显示任何二级问题**。
   * 支持多选，再点一次取消；切换智能体时回到未选状态。
   */
  const [selectedGroups, setSelectedGroups] = useState<number[]>([]);
  useEffect(() => {
    setSelectedGroups([]);
  }, [agent]);
  const chosenGroups = groups.filter((_, index) => selectedGroups.includes(index));
  const scoped = chosenGroups.flatMap((group) => group.items);
  /** 全部一级标题都选中时就是“全域”——全域是合集语义，不是一级标题。 */
  const allSelected = groups.length > 0 && chosenGroups.length === groups.length;
  const scopeLabel = chosenGroups.length === 0
    ? ""
    : allSelected
      ? "全域"
      : chosenGroups.map((group) => group.title).join("、");
  /** 二级问题：未选标题 = 空；选中标题 = 该标题下的问题（+ 无标题的全域基线）。 */
  const suggestions = grouped
    ? (selectedGroups.length === 0
      ? []
      : Array.from(new Set([...baseline, ...scoped])))
    : (opening?.suggestions?.filter((text) => text.trim().length > 0) ?? []);
  const scopeHint = chosenGroups.length === 1 ? chosenGroups[0].description : "";
  const toggleGroup = (index: number) => {
    setSelectedGroups((prev) => prev.includes(index)
      ? prev.filter((item) => item !== index)
      : [...prev, index]);
  };
  /** 卡片下方那行统计的文案(没东西可报时是 null → 整行不渲染)。 */
  const counts = usage === null ? null : countsLabel(usage);
  const share =
    usage === null ? null : contextShare(usage.context_tokens, contextWindow);
  const hasTokens =
    usage !== null && (usage.total_tokens > 0 || usage.context_tokens > 0);
  /**
   * 有轮数、但一个 token 都没记 —— 那是**本次改动之前**跑过的会话。
   *
   * 这时不能什么都不写:用户看到的是"有统计行、但没有 token",而他无从知道为什么。
   * 把事实写出来("用量未记录")比留白好 —— 也不编一个估值填上去。
   */
  const usageMissing = usage !== null && usage.turns > 0 && !hasTokens;

  // 指令菜单:combobox 形态 —— 焦点始终在输入框,菜单只画一个高亮行。
  const [menuOpen, setMenuOpen] = useState(false);
  /** `/` 之后的过滤串;由「+」打开时为空(显示全部)。 */
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  /**
   * 这次 `/token` 是否已被用户**显式关掉**(Esc / 点外部 / 再点「+」)。
   *
   * 没有它的话,Esc 关掉菜单后输入框里还留着 `/foo` —— 再敲一个字又会立刻弹回来,
   * 用户会觉得“退出没生效”。清掉 `/`、或换一段普通文本后重置。
   */
  const [dismissed, setDismissed] = useState(false);

  // 输入被清空 / 变成普通文本时,**解除**“已忽略”状态:否则换会话或清空草稿后
  // 第一次输入 `/` 会打不开菜单。
  useEffect(() => {
    if (!/^\/(\S*)$/.test(value)) setDismissed(false);
  }, [value]);

  /** 可用性 + 过滤。`/` 之后的串按标题、别名、描述三处匹配(大小写不敏感)。 */
  const items = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return COMMANDS.map((item) => {
      if (item.id === "fork" && !canFork) return { ...item, disabled: true };
      if (item.id === "compact" && !canCompact) {
        return { ...item, disabled: true, disabledNote: "还没有可压缩的内容" };
      }
      return item;
    }).filter((item) => {
      if (needle === "") return true;
      return [item.label, item.alias, item.description].some((text) =>
        text.toLowerCase().includes(needle),
      );
    });
  }, [canCompact, canFork, query]);

  const closeMenu = useCallback(
    (reason: "pick" | "submit" | "dismiss" = "pick") => {
      setMenuOpen(false);
      setQuery("");
      setActive(0);
      // 只有“用户主动关掉”才抑制重新弹出;执行指令 / 发送消息都不抑制。
      setDismissed(reason === "dismiss" && /^\/(\S*)$/.test(value));
    },
    [value],
  );

  /**
   * 菜单开着时的三条**全局**退出路径(Esc / 点外部 / 焦点离开);
   * “再点 +”与“选中一条”在各自 handler 里。
   *
   * `pointerdown` 而不是 `click`:它早于目标控件的 click,关闭不会晚一帧;
   * 且这里**不 preventDefault**,所以被点中的控件照常工作。
   */
  useEffect(() => {
    if (!menuOpen) return;
    const inside = (target: EventTarget | null) => {
      if (!(target instanceof Node)) return false;
      return Boolean(
        menuRef.current?.contains(target) ||
          ref.current?.contains(target) ||
          addRef.current?.contains(target),
      );
    };
    const dismiss = () => closeMenu("dismiss");
    const onPointerDown = (event: PointerEvent) => {
      if (!inside(event.target)) dismiss();
    };
    const onFocusIn = (event: FocusEvent) => {
      if (!inside(event.target)) dismiss();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      dismiss();
      ref.current?.focus();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("focusin", onFocusIn);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("focusin", onFocusIn);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [closeMenu, menuOpen]);

  /**
   * 执行一项指令。
   *
   * 斜杠触发时输入框里那段 `/xxx` 是**命令**而不是要发给模型的消息 ——
   * 所以执行完把它清掉(这也是 dsh 的行为:斜杠菜单选中即执行,不留文本)。
   */
  const pick = (index: number) => {
    const item = items[index];
    if (item === undefined || item.disabled === true) return;
    closeMenu("pick");
    if (value.startsWith("/")) onChange("");
    onCommand(item.id);
  };

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
    // 发送是“离开菜单”的动作:即使菜单里没有可选项(Enter 落到发送),也要收起来。
    closeMenu("submit");
    onSend();
  };

  return (
    <div className="dock">
      <div className="dock__inner">
        {/* 智能体的开场白 + 内置提问:**选择 agent 时就有**，不走 AG-UI。运行中收起，
            避免把“这一轮还没结束”与“再来一条”混在一起。 */}
        {(opening?.message || suggestions.length > 0 || grouped) && !running ? (
          <div className="suggestions" aria-label="智能体开场">
            {opening?.message ? (
              <p className="suggestions__message">{opening.message}</p>
            ) : null}
            {grouped ? (
              <div className="suggestions__nav">
                <div className="suggestions__groups" aria-label="一级标题（可多选）">
                  {groups.map((group, index) => (
                    <button
                      key={`${index}-${group.title}`}
                      type="button"
                      aria-pressed={selectedGroups.includes(index)}
                      className="suggestions__group"
                      onClick={() => toggleGroup(index)}
                      title={group.description || group.title}
                    >
                      {group.title}
                    </button>
                  ))}
                </div>
                {chosenGroups.length === 0 ? (
                  <p className="suggestions__hint">选择一级标题后，显示该标题下的推荐问题。</p>
                ) : (
                  <>
                    <p className="suggestions__caption">推荐问题（{scopeLabel}）</p>
                    {scopeHint ? (
                      <p className="suggestions__hint">{scopeHint}</p>
                    ) : null}
                  </>
                )}
              </div>
            ) : null}
            {suggestions.length > 0 ? (
              <div className="suggestions__list">
                {suggestions.map((text, index) => (
                  <button
                    key={`${index}-${text}`}
                    type="button"
                    className="suggestions__item"
                    onClick={() => onSuggest(text)}
                    title={text}
                  >
                    {text}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
        {/* 活动条**只在运行时存在**。dsh 的同一位置是 stats pills,也是"有数据才画"。
            空闲时写一个"就绪"只会把 hero 里的项目 chip 与输入卡拆开(它俩本该
            隔 12px),还会在没话可说时占走 28px 高。 */}
        {running ? (
          <div className="activity" data-live="true">
            <i className="activity__pulse" />
            <span>{current || "运行中"}</span>
            <span className="activity__spacer" />
          </div>
        ) : null}

        <div className="composer" ref={cardRef}>
          {/* 菜单贴着卡片上沿、与卡片同宽(dsh `.menu`),但 **portal 到 body** ——
              留在卡片里会困在 `.dock` 的层叠上下文里,被抬到 z-index 10 的
              `.hero__projectrow` 盖住(实测:看起来透明、且鼠标点不中)。 */}
          {menuOpen ? (
            <CommandMenu
              items={items}
              active={active}
              onPick={pick}
              onHover={setActive}
              emptyHint={`没有匹配「${query}」的指令`}
              anchor={cardRef}
              panelRef={menuRef}
            />
          ) : null}
          <textarea
            ref={ref}
            className="composer__input"
            value={value}
            rows={1}
            placeholder={placeholder}
            aria-label={placeholder}
            aria-expanded={menuOpen}
            aria-haspopup="listbox"
            // combobox 形态:`aria-activedescendant` 挂在**输入框**上(焦点在这里),
            // 而不是 listbox 上 —— 后者不可聚焦,挂上去既不合规范也点不亮高亮。
            aria-controls={menuOpen ? "qi-command-menu" : undefined}
            aria-activedescendant={
              menuOpen && items[active] !== undefined
                ? `qi-command-${active}`
                : undefined
            }
            onChange={(e) => {
              const next = e.target.value;
              onChange(next);
              // 整段就是 `/token` 才当命令:普通句子里出现的 `/` 不弹菜单。
              const slash = /^\/(\S*)$/.exec(next);
              if (slash) {
                // 被 Esc / 点外部关掉之后,继续在同一个 `/token` 上敲字不再弹回来。
                if (dismissed) return;
                setQuery(slash[1] ?? "");
                setActive(0);
                setMenuOpen(true);
              } else {
                setDismissed(false);
                if (menuOpen) closeMenu("pick");
              }
            }}
            onKeyDown={(e) => {
              if (menuOpen && items.length > 0) {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setActive((i) => (i + 1) % items.length);
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setActive((i) => (i - 1 + items.length) % items.length);
                  return;
                }
                if (e.key === "Enter" && !e.shiftKey) {
                  const item = items[active];
                  // 高亮项被置灰/不存在时**不吞**这次 Enter:让消息正常发出去。
                  if (item !== undefined && item.disabled !== true) {
                    e.preventDefault();
                    pick(active);
                    return;
                  }
                }
                // Esc 由 document 级监听统一处理(焦点不在输入框时也能关)。
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <div className="composer__row">
            <div className="composer__tools">
              {/* 左:调用指令。点一下**直接弹出指令列表**(`aria-haspopup="listbox"` +
                  `aria-expanded`,与 dsh 那颗加号一致);再点一下收起来。 */}
              <button
                ref={addRef}
                type="button"
                className="composer__add"
                aria-haspopup="listbox"
                aria-expanded={menuOpen}
                title="调用指令"
                aria-label="调用指令"
                data-open={menuOpen}
                // **焦点留在输入框**(combobox 形态,dsh 那颗加号上也是
                // `onMouseDown={keepFocus}`):否则点了加号之后焦点在按钮上,
                // ↑↓/Enter 根本进不到菜单 —— 实测踩过。
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  if (menuOpen) {
                    closeMenu("dismiss");
                  } else {
                    setDismissed(false);
                    setQuery("");
                    setActive(0);
                    setMenuOpen(true);
                    // 光标**放进输入框**:`preventDefault` 只保证"不抢走焦点",
                    // 首屏本来就没人持有焦点(body)——不主动聚焦,↑↓/Enter 就进不到菜单。
                    ref.current?.focus();
                  }
                }}
              >
                <IconPlusOutline16 size={14} />
              </button>
              {/* 右:附件(回形针)。qi 没有上传端点,所以**置灰并说明**,而不是把它藏起来:
                  dsh 这个位置就是它,用户会来找。 */}
              <button
                type="button"
                className="composer__add"
                disabled
                title="附件:qi 暂不支持"
                aria-label="添加附件(暂不支持)"
              >
                <IconPaperclipOutline16 size={14} />
              </button>
            </div>
            <div className="composer__trailing">
              {/* 知识库 chip:只影响这一轮的 KB 范围；空 = 全局绑定。 */}
              <KbMenu items={kbItems} value={kbIds} onChange={onPickKbs} />
              {/* 智能体 chip:模型名左边,和模型一样是"这一轮会怎么走"的设置(见 AgentMenu)。 */}
              <AgentMenu agents={agents} value={agent} onPick={onPickAgent} />
              {/* 模型 chip:与智能体 chip 同一形态(它以前是个只读 span,而"换模型在设置里"
                  那句是空话 —— 设置页没有这个入口)。见 ModelMenu 的文件头。 */}
              <ModelMenu
                catalog={modelCatalog}
                value={model}
                full={modelFull}
                busy={modelBusy}
                onPick={onPickModel}
                onPickLevel={onPickLevel}
              />
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

        {/* 会话统计 + token 用量。**有数据才画** —— 空会话（还没说过话）不占这
            22px,否则 hero 里的项目 chip 与输入卡会被它拆开（它俩本该隔 12px）。 */}
        {counts !== null || hasTokens || usageMissing ? (
          <div className="stats">
            {counts !== null && usage !== null ? (
              <span className="stats__pill">
                <IconGaugeOutline16 size={14} />
                <span className="stats__label">
                  {counts}
                  {usage.tool_failures > 0 ? (
                    <span className="stats__warn">
                      {` ${usage.tool_failures} 失败`}
                    </span>
                  ) : null}
                </span>
              </span>
            ) : null}
            {hasTokens && usage !== null ? (
              <span className="stats__pill">
                <IconDatabaseOutline16 size={14} />
                <span className="stats__label">
                  {`${formatTokens(usage.total_tokens)} tokens`}
                  {share === null ? null : (
                    <>
                      <span className="stats__sep" aria-hidden>
                        ·
                      </span>
                      {`上下文 ${Math.round(share * 100)}%`}
                    </>
                  )}
                </span>
              </span>
            ) : null}
            {usageMissing ? (
              <span
                className="stats__pill"
                title="这轮对话跑在记录用量之前:qi 当时没有把 usage 写进会话文件,事后也补算不出来。新的一轮开始就有了。"
              >
                <IconDatabaseOutline16 size={14} />
                <span className="stats__label">用量未记录</span>
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
