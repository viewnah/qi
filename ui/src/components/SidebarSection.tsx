/**
 * 「会话」区块标题:**标签 + 搜索**。
 *
 * qi-lab 固定工作目录后不再有「添加工作区」动作,所以这里只剩搜索。
 * 几何仍照 dsh 的 `sectionHeader`:
 *
 *   · 搜索引擎:收起时是 28px 圆钮 + 放大镜 **14px**;展开时容器变成 30px、
 *     圆角 10、发丝描边的输入框,放大镜缩到 **11px**(dsh 的
 *     `size={searchExpanded ? 11 : 14}`),右侧出现 24px 的 × 清空钮。
 *     关闭只有两条路:**Esc 与 ×**(dsh 也是这两条 —— 点击放大镜不负责关闭)。
 *
 * 展开时标签收掉、输入框占满,是 dsh 的 `sectionLabelHidden` 那套行为。
 *
 * 为什么单独成组件:`Rail` 已经有“会话列表 + 行内菜单 + 时间刷新”几件事,
 * 再加搜索状态就只剩堆叠了。这里把**搜索的 UI 状态**(是否展开、输入框 ref、
 * 展开即聚焦)整个收进来,`Rail` 只留"查询串"一个受控值去过滤列表。
 */
import { useEffect, useRef, useState } from "react";
import { IconCloseFill14, IconSearchOutline16 } from "./icons";

export function SidebarSection({
  query,
  onQueryChange,
  collapsed,
  onExpandRail,
}: {
  /** 搜索串(受控值由 Rail 持有 —— 它要用它过滤列表)。 */
  query: string;
  onQueryChange: (value: string) => void;
  /** 左栏正折叠成 56px 竖条:这一区只剩搜索圆钮(见 app.css 的 `data-rail`)。 */
  collapsed: boolean;
  /** 折叠态下点放大镜:先展开左栏(否则输入框没有地方放)。 */
  onExpandRail: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const input = useRef<HTMLInputElement | null>(null);

  // 展开就把光标放进输入框(dsh 在 onClick 里也调 focus):否则点一下还得再点一次。
  useEffect(() => {
    if (expanded) input.current?.focus();
  }, [expanded]);

  const close = () => {
    onQueryChange("");
    setExpanded(false);
  };

  /** 打开搜索。折叠态下先请 Rail 展开 —— 56px 里放不下输入框(dsh 点面板字形同理)。 */
  const open = () => {
    if (collapsed) onExpandRail();
    setExpanded(true);
  };

  return (
    <div className="rail__section" data-search={expanded ? "open" : "closed"}>
      <span className="rail__section-label">会话</span>
      <div className="rail__searchslot">
        {/* 整槽可点:dsh 把 onClick 挂在容器上(输入框未展开时也占着这一格)。 */}
        <div className="rail__search" data-expanded={expanded} onClick={open}>
          <button
            type="button"
            className="rail__searchbtn"
            aria-label="搜索会话"
            aria-expanded={expanded}
            title="搜索会话"
            onClick={open}
          >
            <IconSearchOutline16 size={expanded ? 11 : 14} />
          </button>
          {/* 输入框**常驻**(靠 opacity + pointer-events 收起,见 app.css):
              条件渲染会在两帧之间把宽度过渡打断。 */}
          <input
            ref={input}
            className="rail__searchinput"
            type="text"
            value={query}
            aria-label="按标题搜索会话"
            placeholder="搜索会话"
            tabIndex={expanded ? 0 : -1}
            onChange={(event) => onQueryChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              close();
            }}
          />
          {expanded ? (
            <button
              type="button"
              className="rail__clear"
              aria-label="清空搜索"
              title="清空"
              onClick={(event) => {
                // 别让这次点击冒泡到容器上又把面板"展开"一次。
                event.stopPropagation();
                close();
              }}
            >
              <IconCloseFill14 size={14} />
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
