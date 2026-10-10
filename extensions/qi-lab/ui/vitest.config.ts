import { defineConfig } from "vitest/config";

/**
 * 前端单测配置。
 *
 * 只测**纯函数**(SSE 切帧、事件归约、契约常量),所以用 node 环境即可 ——
 * 不引 jsdom / Testing Library:组件测试是另一个层次的问题(渲染快照易碎),
 * 而真正的风险集中在"事件怎么变成视图"这段逻辑上。
 */
export default defineConfig({
 test: {
  environment: "node",
  include: ["src/**/*.test.ts"],
 },
});
