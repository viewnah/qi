/**
 * 左栏的二级目录:**项目(一级)→ 会话(二级)**。
 *
 * 「项目」在 qi 里**不是新概念**,就是会话自己的工作目录(`cwd`)。三处已有事实
 * 让它不需要任何后端改动:
 *   · 会话 header 里带 `cwd`(`session.Session.cwd`,API 已经回给前端);
 *   · `SessionStore.list()` 已按**文件 mtime 倒序**返回(最近活跃在前);
 *   · 新建会话时可以指定 `cwd`(`POST /api/sessions` 早就支持)。
 *
 * 为什么不像 dsh 那样把 workspace 做成持久化实体(名单 + 排序 + 自己创建):
 * 那是它的一份额外状态。qi 取**目录即项目**:用户不用先"添加工作区"才能开始工作,
 * 也不会出现"会话记录在、工作区名单没了"的两份真相。代价是不能给项目起别名、
 * 不能手工排序 —— 这两件事由 `/api/workspaces` 的两个「人工决定」补上:
 * **改过的显示名**与**「不再分组」**(被删掉的工作区)。
 *
 * 两条派生规则(刻意写死在这里,不在渲染里重复):
 *   1. **组的顺序 = 组内最近活跃的那条会话在列表里的位置**。后端已按 mtime 倒序,
 *      所以"第一个出现的组"就是最近动过的组,不额外排序,也不再按名字排。
 *   2. `cwd` 为空的旧会话进「未分组」(qi 早先版本的会话 header 里没有 cwd)。
 *
 * 另有一份**覆盖**:改过的显示名(`names[cwd]`,来自 `/api/workspaces`)。它派生不出来,
 * 所以后端记着;键就是目录本身,不另造 id(见 `src/qi_agent/workspaces.py`)。
 *
 * 注意这里**没有**"删过的工作区"这种东西:qi 的删除工作区是连会话一起删,所以那个目录
 * 名下不再有会话 → 组自然不存在。早先那版(min 保留会话、落到未分组)必须再存一个
 * "别再分组这些目录"的集合,现在连同它一起删掉了。
 */
import type { SessionSummary, WorkspaceNames } from "../api/types";

/** 无 `cwd` 的会话所在的桶。用空串而不是哨兵字符串:`cwd` 正常永远非空。 */
export const UNGROUPED_KEY = "";

/** 「未分组」的标签(dsh 的同名字符串)。 */
export const UNGROUPED_LABEL = "未分组";

export interface ProjectGroup {
  /** 分组键:`cwd`,未分组为 {@link UNGROUPED_KEY}。 */
  key: string;
  /** 一级目录显示的标签。 */
  label: string;
  /** 该组的目录;未分组为 `null`(不能在这个组里新建会话)。 */
  cwd: string | null;
  /** 组内会话,保持后端给的顺序(最近活跃在前)。 */
  sessions: SessionSummary[];
  /** 当前打开的会话是否在这个组里(一级行据此点亮文件夹图标)。 */
  containsCurrent: boolean;
}

/**
 * 目录 → 项目名:取末段(dsh 的 `workspaceTitleOf` 同义)。
 *
 * 只有一个特例:**根目录**。`/` 去掉尾斜杠后什么都不剩,此时回原值而不是空串,
 * 否则根目录下的会话会被显示成「未分组」——那是"没有目录",不是"目录是根"。
 * @param cwd - 会话的工作目录;`null`/空串回空串(调用方据此用「未分组」)。
 * @returns 项目名(可能为空串)。
 */
export function projectLabel(cwd: string | null | undefined): string {
  if (!cwd) return "";
  const trimmed = cwd.replace(/[\\/]+$/, "");
  const parts = trimmed.split(/[\\/]/).filter(Boolean);
  const last = parts.at(-1);
  return last === undefined ? cwd : last;
}

/** 拿不到 `/api/workspaces` 时的默认(首屏、或后端不可用时都是它)。 */
export const EMPTY_NAMES: WorkspaceNames = { names: {} };

/**
 * 会话列表 → 项目分组。
 *
 * 组**不**按 `running` 之类重排:那会让正在跑的会话在列表里跳位置,而它的位置
 * 本来就是"最近活跃"的表达。
 * @param sessions - `/api/sessions` 给的顺序(已按 mtime 倒序)。
 * @param current - 当前打开的会话 id(`null` = 还没有)。
 * @param overrides - `/api/workspaces` 给的显示名覆盖(改过名的目录)。
 * @returns 分组列表,顺序 = 各组首条会话在输入里的顺序。
 */
export function groupByProject(
  sessions: SessionSummary[],
  current: string | null,
  overrides: WorkspaceNames = EMPTY_NAMES,
): ProjectGroup[] {
  const groups = new Map<string, ProjectGroup>();
  for (const session of sessions) {
    const cwd = session.cwd ?? null;
    const folded = cwd === null || cwd === "";
    const key = folded ? UNGROUPED_KEY : cwd;
    let group: ProjectGroup | undefined = groups.get(key);
    if (group === undefined) {
      group = {
        key,
        label: folded
          ? UNGROUPED_LABEL
          : (overrides.names[cwd] ?? projectLabel(cwd)) || UNGROUPED_LABEL,
        // 未分组是"没有 cwd 的旧会话"的桶,没有目录 → 也就没有"在此新建会话"
        // 这个动作(dsh 的未分组桶同样没有动作菜单)。
        cwd: folded ? null : cwd,
        sessions: [],
        containsCurrent: false,
      };
      groups.set(key, group);
    }
    group.sessions.push(session);
    if (session.id === current) group.containsCurrent = true;
  }
  return [...groups.values()];
}

/**
 * 左栏搜索:按**会话标题**或**工作区名**过滤两层目录。
 *
 * 两条规则:
 *   1. 工作区名命中 → 该工作区下的会话**全部**保留(搜"qi"想看的是那个项目);
 *   2. 否则只留标题命中的会话,空掉的工作区整行消失。
 *
 * 刻意**不做**的事:不搜会话内容。那要读每个会话文件(或加后端索引接口),
 * 而左栏要的是"我那条会话跑哪去了"——标题足够,而且不必等 IO。
 * @param groups - `groupByProject` 的结果。
 * @param query - 用户输入;首尾空白忽略,大小写不敏感。
 * @returns 过滤后的分组(不改原数组)。
 */
export function filterGroups(
  groups: ProjectGroup[],
  query: string,
): ProjectGroup[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return groups;
  const out: ProjectGroup[] = [];
  for (const group of groups) {
    if (group.label.toLowerCase().includes(needle)) {
      out.push(group);
      continue;
    }
    const sessions = group.sessions.filter((session) =>
      (session.title || "").toLowerCase().includes(needle),
    );
    if (sessions.length > 0) out.push({ ...group, sessions });
  }
  return out;
}

/** 相对时间的档位。与 dsh 的 `RelativeTimeUnit` 同一套。 */
export type RelativeUnit =
  | "now"
  | "minutes"
  | "hours"
  | "days"
  | "months"
  | "years";

/**
 * 相对时间的**分档**——逐字移植 dsh `ui-primitives/src/relative-time.ts`
 * (分档放在共用函数里,是为了让"命名同一条会话的两个界面"给出同一个词)。
 *
 * 移植而不是自造的理由:档位边界(1 分钟 / 1 小时 / 1 天 / 30 天 / 1 年)
 * 与"向下取整"的取法本身就是设计,改一个数就会和左栏扫读的节奏不一致。
 * @param at - 被标注时刻的 epoch 毫秒。
 * @param now - 当前 epoch 毫秒(注入进来,渲染保持纯函数)。
 * @returns 档位与档位内的数值(`now` 档固定 0)。
 */
export function relativeTime(
  at: number,
  now: number,
): { unit: RelativeUnit; n: number } {
  const MIN = 60_000;
  const HOUR = 3_600_000;
  const DAY = 86_400_000;
  const diff = Math.max(0, now - at);
  if (diff < MIN) return { unit: "now", n: 0 };
  if (diff < HOUR) return { unit: "minutes", n: Math.floor(diff / MIN) };
  if (diff < DAY) return { unit: "hours", n: Math.floor(diff / HOUR) };
  if (diff < 30 * DAY) return { unit: "days", n: Math.floor(diff / DAY) };
  if (diff < 365 * DAY)
    return { unit: "months", n: Math.floor(diff / (30 * DAY)) };
  return { unit: "years", n: Math.floor(diff / (365 * DAY)) };
}

/**
 * 解析宿主给的时间戳。
 *
 * 宿主的格式是**不带时区的本地时间**(`session._now()` = `%Y-%m-%dT%H:%M:%S`),
 * 所以不能用 `Date.parse` 的宽泛实现(不同引擎对"无时区"的处理历史上有分歧)。
 * 手写解析:固定当本地时间读,与写入侧同源。
 * @param stamp - `YYYY-MM-DDTHH:MM:SS`;空/null 回 `null`。
 * @returns epoch 毫秒,或 `null`(无法解析)。
 */
export function parseStamp(stamp: string | null | undefined): number | null {
  if (!stamp) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/.exec(stamp);
  if (!m) return null;
  const [, y, mo, d, h, mi, s] = m;
  return new Date(
    Number(y),
    Number(mo) - 1,
    Number(d),
    Number(h),
    Number(mi),
    Number(s),
  ).getTime();
}

const LABELS: Record<RelativeUnit, string> = {
  now: "刚刚",
  minutes: "分钟",
  hours: "小时",
  days: "天",
  months: "个月",
  years: "年",
};

/**
 * 行尾那个短标签(「15分钟」「23小时」)。
 *
 * 取 `updated_at`(文件 mtime)而不是 `created_at`:列表的顺序本来就是 mtime,
 * 显示创建时间会让"排在前面的写着 3天前"这种自相矛盾的行出现。
 * @param stamp - 会话的 `updated_at`(缺失时回落到 `created_at`)。
 * @param now - 当前 epoch 毫秒。
 * @returns 标签;时间戳不可解析时回空串(不显示,而不是显示"NaN年")。
 */
export function relativeLabel(
  stamp: string | null | undefined,
  now: number,
): string {
  const at = parseStamp(stamp);
  if (at === null) return "";
  const { unit, n } = relativeTime(at, now);
  return unit === "now" ? LABELS.now : `${n}${LABELS[unit]}`;
}

/** 会话行尾要显示的时间戳:优先"最近活跃",回落"创建"。 */
export function stampOf(session: SessionSummary): string | null {
  return session.updated_at || session.created_at || null;
}
