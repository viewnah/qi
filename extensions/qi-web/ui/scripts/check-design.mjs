#!/usr/bin/env node
/**
 * qi-web 设计门禁 —— 两条硬规则 + 一份对比度**信息**。
 *
 *   A. **保真**:`tokens.css` 里解析出来的浅/深色值必须等于 dsh 的实测值。
 *      期望值来自本机 dsh 客户端包(`dsh-client-ui-theme/lib/client.js`)的逐条抽取,
 *      所以这个门禁真正回答的是"**页面还是不是 dsh 那套**"。
 *   B. **纪律**:`web/src` 下除 `theme/tokens.css` 外不得出现具体色值。
 *   C. (信息)语义文字色在各级底色上的对比度 —— dsh 自己的取值有些低于 WCAG AA,
 *      这里只**如实报告**,不当作失败(保真优先,这是用户明确的取舍)。
 *
 *   node scripts/check-design.mjs
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(fileURLToPath(new URL(".", import.meta.url)), "..");
const TOKENS = join(ROOT, "src/theme/tokens.css");
const css = readFileSync(TOKENS, "utf8");

// ── 解析:静态色板 + 别名(light-dark 两值,允许跨行与 var() 间接)──────
const staticPalette = {};
for (const m of css.matchAll(
  /--dsw-static-([a-z0-9-]+):\s*(#[0-9a-fA-F]{3,8})\s*;/g,
)) {
  staticPalette[m[1]] = m[2].toLowerCase();
}

/**
 * 取 `--dsw-<name>` 的 `light-dark(a, b)` 两值。
 *
 * 必须按**括号配平**扫描,不能用 `[^)]+`:两值里常有 `var(--dsw-static-…)`,
 * 它自带右括号,用字符类会在那里提前断掉(第一版报“令牌缺失”就是这个原因)。
 */
function lightDarkOf(name) {
  const at = css.indexOf(`--dsw-${name}:`);
  if (at < 0) return null;
  const open = css.indexOf("light-dark(", at);
  if (open < 0) return null;
  let depth = 1;
  let body = "";
  for (let i = open + "light-dark(".length; i < css.length; i += 1) {
    const ch = css[i];
    if (ch === "(") depth += 1;
    else if (ch === ")") {
      depth -= 1;
      if (depth === 0) break;
    }
    body += ch;
  }
  // 在**顶层**逗号处切分
  let inner = 0;
  for (let i = 0; i < body.length; i += 1) {
    if (body[i] === "(") inner += 1;
    else if (body[i] === ")") inner -= 1;
    else if (body[i] === "," && inner === 0) {
      return [body.slice(0, i), body.slice(i + 1)];
    }
  }
  return null;
}

const resolve = (expr) => {
  const v = expr.trim();
  const varMatch = /^var\(\s*--dsw-static-([a-z0-9-]+)\s*\)$/.exec(v);
  if (varMatch)
    return staticPalette[varMatch[1]] ?? `unresolved(${varMatch[1]})`;
  return v.toLowerCase();
};

/** token → [light, dark] */
const themes = {};
for (const m of css.matchAll(/--dsw-(alias|specific)-([a-z0-9-]+):/g)) {
  const key = `${m[1]}:${m[2]}`;
  if (themes[key]) continue;
  const pair = lightDarkOf(`${m[1]}-${m[2]}`);
  if (pair) themes[key] = [resolve(pair[0]), resolve(pair[1])];
}

// ── A. 保真表:期望值 = dsh 实测值 ────────────────────────────────────
const EXPECTED = {
  "alias:bg-base": ["#ffffff", "#151517"],
  "alias:bg-layer-1": ["#ffffff", "#232324"],
  "alias:bg-layer-2": ["#ffffff", "#2c2c2e"],
  "alias:bg-layer-3": ["#ffffff", "#353638"],
  "alias:bg-module-platform": ["#f5f6f7", "#353638"],
  "alias:label-primary": ["#0f1115", "#f9fafb"],
  "alias:label-secondary": ["#61666b", "#cfd3d6"],
  "alias:label-tertiary": ["#81858c", "#adb2b8"],
  "alias:label-caption": ["#adb2b8", "#81858c"],
  "alias:label-dimmed": ["#e1e5ee", "#43454a"],
  "alias:border-l1": ["#0000000a", "#ffffff0f"],
  "alias:border-l2": ["#0000001a", "#ffffff1f"],
  "alias:border-l3": ["#0000001f", "#ffffff29"],
  "alias:state-business-primary": ["#4176e6", "#679efe"],
  "alias:state-success": ["#22c55e", "#4ed17e"],
  "alias:state-warning": ["#f59e0b", "#f7ad31"],
  "alias:state-error": ["#ec1313", "#f25a5a"],
  "specific:sidebar-fill": ["#f9fafb", "#1b1b1c"],
  "specific:selector": ["#f5f6f7", "#353638"],
  "specific:input-major": ["#ffffff", "#2c2c2e"],
  "specific:sidebar-nav-item-hover": ["#f1f3f5", "#2c2c2e"],
  "specific:sidebar-nav-item-active": ["#ebeef2", "#43454a"],
  "specific:bubble": ["#edf3fe", "#2c2c2e"],
};

let failures = 0;
console.log("\n[A] 保真:dsh 的色值有没有被改掉");
for (const [key, want] of Object.entries(EXPECTED)) {
  const got = themes[key];
  if (!got) {
    console.log(`  ✗ ${key.padEnd(34)} 令牌缺失`);
    failures += 1;
    continue;
  }
  const ok = got[0] === want[0] && got[1] === want[1];
  if (!ok) failures += 1;
  const detail = ok
    ? "= dsh"
    : `期望 ${want.join(" / ")} → 实际 ${got.join(" / ")}`;
  console.log(
    `  ${ok ? "✓" : "✗"} --dsw-${key.padEnd(32)} ${got[0]} / ${got[1]}  ${detail}`,
  );
}

// ── B. 色值纪律 ───────────────────────────────────────────────────────
const SKIP_DIRS = new Set(["node_modules", "dist", ".git"]);
const ALLOW = new Set([join("src", "theme", "tokens.css")]);
const offenders = [];
const walk = (dir) => {
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRS.has(name)) continue;
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      walk(path);
      continue;
    }
    const rel = relative(ROOT, path);
    if (ALLOW.has(rel)) continue;
    if (!/\.(css|ts|tsx|js|jsx|mjs)$/.test(name)) continue;
    readFileSync(path, "utf8")
      .split("\n")
      .forEach((line, index) => {
        const hit = line.match(/#[0-9A-Fa-f]{3,8}\b/);
        if (hit)
          offenders.push(`${rel.split(sep).join("/")}:${index + 1}  ${hit[0]}`);
      });
  }
};
walk(join(ROOT, "src"));

console.log("\n[B] 色值纪律:src/ 下只有 theme/tokens.css 允许写具体色值");
if (offenders.length === 0) {
  console.log("  ✓ 未见越界色值");
} else {
  failures += offenders.length;
  for (const item of offenders) console.log(`  ✗ ${item}`);
}

// ── C. 对比度(信息,不计失败)─────────────────────────────────────────
const toLin = (v) => {
  const c = v / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
};
const lum = (hex) => {
  const h = hex.slice(0, 7);
  const n = parseInt(h.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(toLin);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a, b) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

console.log("\n[C] 对比度实测(dsh 自有取值;低于 4.5 的已如实标出,不作失败)");
for (const theme of [0, 1]) {
  const name = theme === 0 ? "light" : "dark";
  const bg = themes["alias:bg-base"][theme];
  const panel = themes["alias:bg-layer-1"][theme];
  for (const label of [
    "label-primary",
    "label-secondary",
    "label-tertiary",
    "label-caption",
  ]) {
    const color = themes[`alias:${label}`][theme];
    const on = (surface) => ratio(color, surface);
    const min = Math.min(on(bg), on(panel));
    console.log(
      `  ${name.padEnd(5)} --dsw-${label.padEnd(18)} ${color}  on bg ${on(bg).toFixed(2)} / on layer-1 ${on(panel).toFixed(2)}${min < 4.5 ? "  ← 低于 AA" : ""}`,
    );
  }
}

console.log(
  `\n${failures === 0 ? "✓ 设计门禁通过" : `✗ 设计门禁失败:${failures} 项`}`,
);
process.exit(failures === 0 ? 0 : 1);
