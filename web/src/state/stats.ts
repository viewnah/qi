/**
 * 用量数字的显示口径(纯函数,有单测)。
 *
 * 为什么单独一个模块:这些格式化会同时被输入卡下方那行统计与遥测抽屉用到,
 * 而"12.4k 还是 12400"这种事有**唯一**正确答案 —— 两处各写一遍就会分叉。
 */
import type { UsageSummary } from "../api/types";

/**
 * 紧凑 token 数。**逐条照 dsh `ui-chat/src/client/chat/token-format.ts` 的规则**:
 *
 *   `517` / `12.4K` / `517K` / `1.2M`
 *
 *   - 小于 1000 原样;
 *   - 千/百万位:换算后 `>= 100` 取整,否则留**一位小数**(`Math.round(x*10)/10`
 *     —— 所以 12.4K 而不是 12K,1.24M 会显示成 1.2M);
 *   - 单位用 dsh 的大写 `K` / `M`(它的 locale 文案就是这两个字母)。
 *
 * 口径不自己发明:这一行与 dsh 的那行是同一个位置,数字读法不一致会显得像两个产品。
 * 比 dsh 多一层保护:dsh 假定入参是非负安全整数,这里把 NaN / 负数 / Infinity 当 0
 * (用量字典里的字段来自 provider,key 可能是脏的)。
 */
export function formatTokens(value: number): string {
   if (!Number.isFinite(value) || value <= 0) return "0";
   if (value < 1_000) return String(Math.round(value));
   const [unit, scale] = value < 1_000_000 ? ["K", 1_000] : ["M", 1_000_000];
   return `${scaledDigits(value / scale)}${unit}`;
}

/** dsh 的取位:`>=100` 取整,否则一位小数。 */
function scaledDigits(candidate: number): string {
   return candidate >= 100
      ? String(Math.round(candidate))
      : String(Math.round(candidate * 10) / 10);
}

/**
 * 上下文占用比例(0~1)。返回 `null` = **画不了**(窗口未知或还没有用量)。
 *
 * 刻意不返回 0:0 是"上下文是空的"这个**结论**,而未知是"不知道"。
 * 调用方据此决定画不画那条进度,而不是画一条永远 0% 的。
 */
export function contextShare(
   contextTokens: number,
   contextWindow: number,
): number | null {
   if (contextWindow <= 0 || contextTokens <= 0) return null;
   return Math.min(1, contextTokens / contextWindow);
}

/** 一行统计里"轮数 / 步数 / 工具"那段的文案。 */
export function countsLabel(usage: UsageSummary): string | null {
   const parts: string[] = [];
   if (usage.turns > 0) parts.push(`${usage.turns} 轮`);
   if (usage.steps > 0) parts.push(`${usage.steps} 步`);
   if (usage.tools > 0) parts.push(`工具 ${usage.tools}`);
   return parts.length > 0 ? parts.join(" · ") : null;
}
