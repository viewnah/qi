/**
 * 主题:auto / light / dark 三态。
 *
 * 值只写进 `<html data-theme>` —— 具体颜色全在 `tokens.css` 里由
 * `color-scheme` + `light-dark()` 解析,所以这里**一个色值都没有**。
 * 选择持久化到 localStorage;缺省 `auto`(跟随系统)。
 */
export type ThemeMode = "auto" | "light" | "dark";

export const THEME_ORDER: ThemeMode[] = ["auto", "light", "dark"];

const STORAGE_KEY = "qi.theme";

const LABELS: Record<ThemeMode, string> = {
 auto: "跟随系统",
 light: "浅色",
 dark: "深色",
};

export const themeLabel = (mode: ThemeMode): string => LABELS[mode];

export function readTheme(): ThemeMode {
 const raw = localStorage.getItem(STORAGE_KEY);
 return raw === "light" || raw === "dark" ? raw : "auto";
}

export function applyTheme(mode: ThemeMode): void {
 document.documentElement.dataset.theme = mode;
 localStorage.setItem(STORAGE_KEY, mode);
}

export function cycleTheme(mode: ThemeMode): ThemeMode {
 const index = THEME_ORDER.indexOf(mode);
 return THEME_ORDER[(index + 1) % THEME_ORDER.length] ?? "auto";
}
