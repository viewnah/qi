#!/usr/bin/env node
/**
 * dsh 令牌保真:**逐条**把 qi 的 `--dsw-alias-*` / `--dsw-specific-*` 与 dsh 自己的
 * 源文件对拍,两个主题都比。
 *
 * 为什么不是手写一张期望值清单:`check-design.mjs` 原来只列了 22 个键,而
 * `design-platform.css` 里有 180 条 alias/specific —— 没被列到的键可以整条抄错
 * 而门禁全绿。事实上正是这样漏掉了两个真实的错:
 *   · `--dsw-alias-button-info-fill` 的明暗两值**抄反了**(浅色主题吃到深色值);
 *   · `--dsw-alias-button-info-hover` 被写成了和 fill 一样,于是浅色下发送按钮
 *     **没有 hover 态**。
 * 所以这里不维护清单:直接从 dsh 源码解析、解析 var() 链、归一成颜色再对拍。
 *
 * 参照物:dsh 仓库里 `packages/client/ui-theme/src/styles/design-platform.css`
 * (`body` = 浅色,`body[data-ds-dark-theme]` = 深色)。
 * 位置可用 `DSH_REPO` 覆盖;找不到就**跳过并明确说明**,不静默当作通过。
 *
 * 覆盖与不覆盖(写清楚,不假装):
 *   覆盖:alias / specific 两族在两个主题下的取值,以及 qi 缺失的键。
 *   不覆盖:`--dsw-static-*` 原始色板本身、字体栈、非颜色令牌、
 *           `--dsw-elevation-*` 这类不在 design-platform.css 里的派生值。
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DSH =
  process.env.DSH_REPO ?? join(ROOT, "..", "data", "deepseek-harness");
const DSH_TOKENS = join(
  DSH,
  "packages/client/ui-theme/src/styles/design-platform.css",
);
const QI_TOKENS = join(ROOT, "src/theme/tokens.css");

let failures = 0;
const fail = (msg) => {
  failures += 1;
  console.log(`  ✗ ${msg}`);
};

if (!existsSync(DSH_TOKENS)) {
  console.log(
    `\n[令牌保真] 跳过:没找到 dsh 源文件 ${DSH_TOKENS}\n` +
      `  参照物缺失时**不作通过判定**。clone dsh 到 data/deepseek-harness,` +
      `或设 DSH_REPO=<repo 路径>。`,
  );
  process.exit(0);
}

// ── 解析 ────────────────────────────────────────────────────────────

/** 去掉 /* *​/ 注释(保住换行,便于报错定位)。 */
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));

/**
 * 扫出所有 `--name: value;` 声明。手写扫描而不是正则:值里有嵌套括号
 * (`light-dark(var(...), var(...))`),正则会在第一个 `)` 处断掉。
 * @returns Map<name, value> —— 后者出现覆盖前者(与 CSS 层叠一致)。
 */
function declarations(css) {
  const text = stripComments(css);
  const out = new Map();
  const re = /(--[a-z0-9-]+)\s*:/g;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    const name = m[1];
    let i = re.lastIndex;
    let depth = 0;
    for (; i < text.length; i += 1) {
      const c = text[i];
      if (c === "(") depth += 1;
      else if (c === ")") depth -= 1;
      else if (c === ";" && depth === 0) break;
    }
    out.set(name, text.slice(re.lastIndex, i).trim().replace(/\s+/g, " "));
  }
  return out;
}

/** 按顶层逗号切分(用于 `light-dark(a, b)`)。 */
function splitTop(value) {
  const parts = [];
  let depth = 0;
  let cur = "";
  for (const c of value) {
    if (c === "(") depth += 1;
    if (c === ")") depth -= 1;
    if (c === "," && depth === 0) {
      parts.push(cur.trim());
      cur = "";
    } else cur += c;
  }
  parts.push(cur.trim());
  return parts;
}

/** `light-dark(a, b)` → [a, b];否则 [v, v](主题无关)。 */
function pairOf(value) {
  const m = /^light-dark\(([\s\S]*)\)$/.exec(value);
  if (!m) return [value, value];
  const parts = splitTop(m[1]);
  if (parts.length !== 2) throw new Error(`light-dark 参数不是 2 个: ${value}`);
  return parts;
}

/**
 * 颜色 → 规范形 `rgb(r, g, b)` / `rgb(r, g, b)/0.7`。非颜色返回 undefined。
 *
 * 透明度只保留 **2 位小数**,刻意如此:dsh 写 `rgba(255, 255, 255, 0.7)`,
 * 而 qi 的 tokens.css 习惯写成 8 位 hex `#ffffffb3`(179/255 = 0.702)。
 * 两者肉眼与规范都等价,但按位比较会因为取整差 1 而"失败"。归一到 2 位小数
 * 让两种写法对得上,同时仍然能抓住真正写错的颜色值(色相/明度差远大于 1/255)。
 */
function canonical(value) {
  const v = value.trim();
  let r, g, b, a;
  const hex = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.exec(v);
  if (hex) {
    let h = hex[1];
    if (h.length === 3) h = [...h].map((c) => c + c).join("");
    r = parseInt(h.slice(0, 2), 16);
    g = parseInt(h.slice(2, 4), 16);
    b = parseInt(h.slice(4, 6), 16);
    a = h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1;
  } else {
    const m = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,/\s]+([\d.]+%?))?\s*\)$/.exec(v);
    if (!m) return undefined;
    r = Number(m[1]);
    g = Number(m[2]);
    b = Number(m[3]);
    a = m[4] === undefined ? 1 : m[4].endsWith("%") ? Number(m[4].slice(0, -1)) / 100 : Number(m[4]);
  }
  const base = `rgb(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)})`;
  return a >= 0.995 ? base : `${base}/${(Math.round(a * 100) / 100).toFixed(2)}`;
}

/** 沿 var() 链解析到字面值;环或缺键返回 undefined。 */
function resolveVar(value, table, seen = new Set()) {
  const v = value.trim();
  const m = /^var\(\s*(--[a-z0-9-]+)\s*(?:,([\s\S]*))?\)$/.exec(v);
  if (!m) return canonical(v) === undefined ? undefined : v;
  const name = m[1];
  if (seen.has(name)) return undefined;
  const next = table.get(name);
  if (next === undefined) return m[2]?.trim();
  seen.add(name);
  return resolveVar(next, table, seen);
}

// ── dsh 侧:4 个块合成为两张表 ────────────────────────────────────────

const dshCss = readFileSync(DSH_TOKENS, "utf8");
/** 按块切:`body {...}`(浅色)与 `body[data-ds-dark-theme] {...}`(深色)。 */
function blocks(css) {
  const out = [];
  const re = /(body(?:\[data-ds-dark-theme\])?)\s*\{/g;
  for (let m = re.exec(css); m; m = re.exec(css)) {
    let i = re.lastIndex;
    let depth = 1;
    for (; i < css.length && depth > 0; i += 1) {
      if (css[i] === "{") depth += 1;
      else if (css[i] === "}") depth -= 1;
    }
    out.push({ selector: m[1], body: css.slice(re.lastIndex, i - 1) });
  }
  return out;
}

const dshBlocks = blocks(dshCss);
if (dshBlocks.length !== 4) {
  fail(`dsh 的 design-platform.css 期望 4 个块,解析到 ${dshBlocks.length} 个(源文件结构变了?)`);
}
const lightTables = dshBlocks.filter((b) => !b.selector.includes("dark"));
const darkTables = dshBlocks.filter((b) => b.selector.includes("dark"));

const merge = (list) => {
  const t = new Map();
  for (const b of list) for (const [k, v] of declarations(b.body)) t.set(k, v);
  return t;
};
const dshLight = merge(lightTables);
const dshDark = merge(darkTables);

const isTarget = (name) => /^--dsw-(alias|specific)-/.test(name);
const dshKeys = [...new Set([...dshLight.keys(), ...dshDark.keys()])]
  .filter(isTarget)
  .sort();

// ── qi 侧 ───────────────────────────────────────────────────────────

const qiTable = declarations(readFileSync(QI_TOKENS, "utf8"));

/**
 * 解析一个键在某一主题下的颜色。
 * @param table 变量表(含 `--dsw-static-*` 与 alias 自身)
 * @param theme 0 = 浅色,1 = 深色
 */
function themeColor(table, name, theme, seen = new Set()) {
  const raw = table.get(name);
  if (raw === undefined || seen.has(name)) return undefined;
  seen.add(name);
  // 值本身是 `var(--另一个别名)`(如 dsh 的 `--dsw-specific-menu` →
  // `--dsw-alias-bg-layer-3`):要在**同一主题**里继续往下走,
  // 而不是拿它当字面值 —— 否则这条会被误判成"qtokens 里不存在"。
  const indirect = /^var\(\s*(--[a-z0-9-]+)\s*\)$/.exec(raw);
  if (indirect) return themeColor(table, indirect[1], theme, seen);
  const [a, b] = pairOf(raw);
  const resolved = resolveVar(theme === 0 ? a : b, table);
  return resolved === undefined ? undefined : canonical(resolved) ?? `非颜色(${resolved})`;
}

console.log("\n[dsh 令牌保真] 逐条对拍 design-platform.css(浅色 / 深色)");
console.log(`  参照物 ${DSH_TOKENS}`);

const mismatched = [];
const missing = [];
const nonColor = [];
for (const key of dshKeys) {
  const want = [themeColor(dshLight, key, 0), themeColor(dshDark, key, 1)];
  if (want[0] === undefined && want[1] === undefined) {
    nonColor.push(key);
    continue;
  }
  const got = [themeColor(qiTable, key, 0), themeColor(qiTable, key, 1)];
  if (got[0] === undefined && got[1] === undefined) {
    missing.push(key);
    continue;
  }
  if (got[0] !== want[0] || got[1] !== want[1]) mismatched.push({ key, want, got });
}

console.log(`  对拍 ${dshKeys.length - nonColor.length} 条颜色令牌`);
if (mismatched.length === 0) {
  console.log("  ✓ 全部一致");
} else {
  for (const { key, want, got } of mismatched) {
    fail(`${key}\n      期望 ${want[0] ?? "—"} / ${want[1] ?? "—"}\n      实际 ${got[0] ?? "—"} / ${got[1] ?? "—"}`);
  }
}
if (missing.length > 0) {
  for (const key of missing) fail(`${key} 在 qi 的 tokens.css 里不存在`);
}
if (nonColor.length > 0) {
  console.log(`  · 跳过 ${nonColor.length} 条非颜色令牌:${nonColor.slice(0, 6).join(", ")}${nonColor.length > 6 ? " …" : ""}`);
}

// qi 自己加的键允许存在(语义别名),但必须是显式的:列出来供人过目。
const qiOnly = [...qiTable.keys()].filter((k) => isTarget(k) && !dshLight.has(k) && !dshDark.has(k));
if (qiOnly.length > 0) {
  console.log(`  · qi 自有令牌 ${qiOnly.length} 条(dsh 没有):${qiOnly.join(", ")}`);
}

console.log();
if (failures > 0) {
  console.log(`✗ dsh 令牌保真门禁失败(${failures} 项)\n`);
  process.exit(1);
}
console.log("✓ dsh 令牌保真门禁通过\n");
