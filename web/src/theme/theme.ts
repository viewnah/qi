export type ThemeMode = "auto" | "light" | "dark";

const STORAGE_KEY = "qi.theme";

/**
 * 读上次的选择;没存过就是 `auto`(跟随系统)。
 *
 * Web 端**不再有主题切换入口**(2026-09,按反馈把左栏底部那一行去掉):
 * 页面跟随系统,或者沿用你以前在 UI 里选过、已经落进 localStorage 的值。
 * 想强制固定:清掉这个键(回到 auto)或改系统外观。
 *
 * 这里**一个色值都没有** —— 颜色全在 `tokens.css` 里由 `color-scheme` +
 * `light-dark()` 解析,本模块只负责给 `<html>` 打 `data-theme`。
 */
export function readTheme(): ThemeMode {
  const raw = localStorage.getItem(STORAGE_KEY);
  return raw === "light" || raw === "dark" ? raw : "auto";
}

export function applyTheme(mode: ThemeMode): void {
  document.documentElement.dataset.theme = mode;
  localStorage.setItem(STORAGE_KEY, mode);
}
