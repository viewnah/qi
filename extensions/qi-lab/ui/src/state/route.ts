/**
 * URL 里的**当前会话**:`#s=<id>`。
 *
 * 为什么需要它:在此之前"当前打开的是哪个会话"只活在 `App` 的 state 里 —— 刷新一下
 * 就回到空态(hero),用户得在左栏重新点一次。把会话放进 URL 之后,刷新、收藏、
 * 复制链接给别的窗口,落到的是同一个会话。
 *
 * 两个刻意的选择:
 *   · **用 hash 而不是 query**:`?session=` 会先发给服务端(hash 不会),而这是纯
 *     客户端状态;顺带也让静态托管不用为它做任何事。
 *   · **纯函数**:解析/生成都放在这里,渲染层只管调 —— 与仓库"只测纯函数"的口径一致
 *     (没有 DOM 也能测满边界:空 hash、别的键、被编码的 id)。
 */

const KEY = "s";

/** 从 `location.hash` 里取出会话 id。没有 / 不是这个键 → `null`。 */
export function parseSessionHash(hash: string): string | null {
  const body = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!body) return null;
  for (const part of body.split("&")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    if (part.slice(0, eq) !== KEY) continue;
    const raw = part.slice(eq + 1);
    if (!raw) return null;
    try {
      return decodeURIComponent(raw);
    } catch {
      // 手拼的坏转义(比如 `#s=%`)不该把整页搞崩:当成"没有会话"处理。
      return null;
    }
  }
  return null;
}

/** 会话 id → hash 片段。`null` 表示回到空态。 */
export function sessionHash(id: string | null): string {
  return id === null ? "#" : `#${KEY}=${encodeURIComponent(id)}`;
}
