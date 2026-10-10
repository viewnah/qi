/**
 * 输入卡「调用指令」菜单的**目录** —— 单一真相,两个消费方:
 * 菜单渲染(`Dock`)与 `/help` 的帮助文本(`App`)。
 *
 * 图标逐字节取自 dsh `ui-primitives` 的同一套字形(见 `components/icons.tsx`),
 * 所以这里只负责"哪条指令配哪个字形",不画路径。
 *
 * ## 取舍:只列 web **真能做**的
 *
 * qi 的 TUI 有 28 条命令,但**终端专属**或需要新能力的那些这里一条都不放:
 * `/quit` `/clear` `/hotkeys` `/changelog`(终端界面)、`/tree`(要一棵会话树 UI)、
 * `/thinking` `/mode` `/agent` `/scoped-models`(web 没有这些旋钮)、
 * `/reload` `/import` `/login`(需要新的写能力)。**菜单里放一条点了没反应的"命令"
 * 比不放更糟** —— 用户在菜单里看到的东西,就应该真的能用。
 *
 * 反过来,web 有的能力这里尽量都放进来:能做的每一条都对应一个已实现的调用
 * (见 `App.tsx` 的 `runCommand`)。
 *
 * 文案与 dsh 对齐:本地化标题 + **斜杠别名** + 右侧描述(`/file` 那条置灰,
 * 因为 qi 还没有上传端点 —— dsh 那个位置就是它,用户会来找)。
 */
import {
  IconAgentPresetOutline16,
  IconBranchOutline16,
  IconChecklistOutline14,
  IconClockOutline16,
  IconCompactOutline16,
  IconCopyOutline16,
  IconDownloadOutline16,
  IconEditOutline16,
  IconFolderOpen16,
  IconNewChatOutline16,
  IconPaperclipOutline16,
  IconQuestionOutline14,
  IconSettingsOutline16,
} from "./components/icons";
import type { CommandItem } from "./components/CommandMenu";

/** 两节的名字与 dsh 一致(「添加」/「指令」)。 */
export const SECTION_ADD = "添加";
export const SECTION_COMMANDS = "指令";

export const COMMANDS: CommandItem[] = [
  {
    id: "file",
    section: SECTION_ADD,
    label: "附件",
    alias: "/file",
    description: "把文件加进这一轮",
    icon: <IconPaperclipOutline16 size={16} />,
    disabled: true,
    disabledNote: "暂不支持",
  },
  {
    id: "new",
    section: SECTION_COMMANDS,
    label: "新会话",
    alias: "/new",
    description: "新建一条会话",
    icon: <IconNewChatOutline16 size={16} />,
  },
  {
    id: "fork",
    section: SECTION_COMMANDS,
    label: "分叉",
    alias: "/fork",
    description: "从当前分支复制出新会话",
    icon: <IconBranchOutline16 size={16} />,
  },
  {
    id: "compact",
    section: SECTION_COMMANDS,
    label: "压缩",
    alias: "/compact",
    description: "把旧消息摘要掉,腾出上下文",
    icon: <IconCompactOutline16 size={16} />,
  },
  {
    id: "name",
    section: SECTION_COMMANDS,
    label: "重命名",
    alias: "/name",
    description: "改当前会话的标题",
    icon: <IconEditOutline16 size={16} />,
  },
  {
    id: "export",
    section: SECTION_COMMANDS,
    label: "导出",
    alias: "/export",
    description: "把这会话的 JSONL 下载下来",
    icon: <IconDownloadOutline16 size={16} />,
  },
  {
    id: "copy",
    section: SECTION_COMMANDS,
    label: "复制回答",
    alias: "/copy",
    description: "复制最后一条回答到剪贴板",
    icon: <IconCopyOutline16 size={16} />,
  },
  {
    id: "info",
    section: SECTION_COMMANDS,
    label: "会话信息",
    alias: "/session",
    description: "这条会话的 id / 目录 / 条数",
    icon: <IconChecklistOutline14 size={16} />,
  },
  {
    id: "sessions",
    section: SECTION_COMMANDS,
    label: "最近会话",
    alias: "/sessions",
    description: "列出最近几条会话",
    icon: <IconClockOutline16 size={16} />,
  },
  {
    id: "agents",
    section: SECTION_COMMANDS,
    label: "Agent",
    alias: "/agents",
    description: "看已装载的 agent(设置页)",
    icon: <IconAgentPresetOutline16 size={16} />,
  },
  {
    id: "help",
    section: SECTION_COMMANDS,
    label: "帮助",
    alias: "/help",
    description: "列出可用指令",
    icon: <IconQuestionOutline14 size={16} />,
  },
  {
    id: "files",
    section: SECTION_COMMANDS,
    label: "文件",
    alias: "/files",
    description: "显示 / 隐藏右侧文件面板(会话目录)",
    icon: <IconFolderOpen16 size={16} />,
  },
  {
    id: "settings",
    section: SECTION_COMMANDS,
    label: "设置",
    alias: "/settings",
    description: "打开设置页",
    icon: <IconSettingsOutline16 size={16} />,
  },
];

/** `/help` 用的纯文本清单(标题 + 别名 + 描述,按节分组)。 */
export function commandHelpText(): string {
  const lines: string[] = [];
  for (const section of [SECTION_ADD, SECTION_COMMANDS]) {
    lines.push(`【${section}】`);
    for (const item of COMMANDS.filter((c) => c.section === section)) {
      const state = item.disabled === true ? `(${item.disabledNote ?? "不可用"})` : "";
      lines.push(`${item.alias}  ${item.label} — ${item.description}${state}`);
    }
  }
  return lines.join("\n");
}
