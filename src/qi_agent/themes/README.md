# 内置调色板(来自上游 pi)

`dark.json` / `light.json` 这两份调色板**移植自上游项目 pi**的
`packages/coding-agent/src/modes/interactive/theme/dark.json` / `light.json`:
键名与语义一致,qi 侧只调整了个别颜色值(`scrollbarThumb` / `scrollbarTrack`)。

- 上游:<https://github.com/earendil-works/pi>
- 许可:**MIT License, Copyright (c) 2025 Mario Zechner**
- 声明与许可全文:仓库根目录的 [`THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md)
  (它也会随 wheel 打进 `qi_agent/THIRD_PARTY_NOTICES.md`)

调色板的结构、`load_palette` 的解析规则与"qi 不支持自定义主题"的现状见
[`docs/themes.md`](../../../docs/themes.md)。
