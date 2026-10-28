/**
 * 外链校验（前端侧的第二道闸）。
 *
 * 后端已经校验过一遍；这里再校验一次，是因为卡片数据来自 `details`
 * （可被任何扩展写入），渲染端不能假设它一定来自本仓库的目录。
 * 只认 `http(s)://` 绝对地址，且不带用户名/密码。
 */
export function isExternalHttpUrl(raw: string): boolean {
   if (typeof raw !== "string") return false;
   const value = raw.trim();
   if (!value || /\s/.test(value) || value.includes("\\")) return false;
   let parsed: URL;
   try {
      parsed = new URL(value);
   } catch {
      return false;
   }
   if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
   if (parsed.username || parsed.password) return false;
   return true;
}
