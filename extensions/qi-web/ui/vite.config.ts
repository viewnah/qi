import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * 构建产物直接落到 python 包内(`extensions/qi-web/qi_web/static/`),
 * 这样 `qi web` 无需额外搬运即可托管,也不会污染仓库根。
 * 开发时用 `npm run dev`,把 /api 代理到宿主端口。
 */
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../qi_web/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5273,
    proxy: {
      "/api": { target: "http://127.0.0.1:30142", changeOrigin: false },
    },
  },
});
