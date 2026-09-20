/// <reference types="vite/client" />

// Vite 的客户端类型声明了 `*.css` / `*.svg` 之类的副作用导入。
// 注意:`tsconfig.json` 里为了明确作用域写了 `"types": []`,这会让 TS **不自动**加载
// `vite/client`;所以这里用显式 reference 指令把它拉进来(比在 types 数组里放宽更精确)。
