import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * 构建产物直接落到 python 包内(`extensions/qi-lab/qi_lab/static/`),
 * 这样 `qi lab` 无需额外搬运即可托管,也不会污染仓库根。
 * 开发时用 `npm run dev`,把 /api 代理到宿主端口。
 */
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../qi_lab/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5273,
    proxy: {
      // `changeOrigin: true` 必须开:开发时从局域网 IP 打开 Vite(如
      // http://192.168.x.x:5273)时,浏览器会把 Host 写成那个 IP;而 qi-lab 的
      // guard 只认回环/绑定地址/白名单。代理改写成目标 Host 后,/api 才不会被 400。
      "/api": { target: "http://127.0.0.1:3456", changeOrigin: true },
    },
  },
});
