/**
 * 左栏二级目录的派生测试。
 *
 * 只测**纯函数**(分组 / 标签 / 相对时间):组件渲染的问题靠肉眼看更快,而真正会
 * 出错的是"哪个会话落到哪个组、组按什么顺序、行尾写几分钟"这几条规则。
 * 与 `turn.test.ts` 同一取舍(不引 jsdom)。
 */
import { describe, expect, it } from "vitest";
import type { SessionSummary } from "../api/types";
import {
  EMPTY_NAMES,
  filterGroups,
  groupByProject,
  parseStamp,
  projectLabel,
  relativeLabel,
  relativeTime,
  stampOf,
  UNGROUPED_KEY,
  UNGROUPED_LABEL,
} from "./projects";

/** 只填测试关心的字段,其余给稳定默认值。 */
function session(
  over: Partial<SessionSummary> & { id: string },
): SessionSummary {
  return {
    title: over.id,
    created_at: "2026-09-17T01:00:00",
    cwd: null,
    message_count: 0,
    running: false,
    path: `/tmp/${over.id}.jsonl`,
    ...over,
  };
}

const NOW = new Date(2026, 8, 17, 12, 0, 0).getTime(); // 本地时间,与宿主同源

describe("projectLabel", () => {
  it("取路径末段", () => {
    expect(projectLabel("/Users/han/Desktop/qi")).toBe("qi");
    expect(projectLabel("/Users/han/Desktop/qi/")).toBe("qi");
    expect(projectLabel("C:\\work\\proj")).toBe("proj");
  });

  it("根目录回原值,不落进「未分组」", () => {
    // `/` 去掉尾斜杠后什么都不剩。若回空串,根目录下的会话会被显示成「未分组」——
    // 那是"没有目录",不是"目录是根"。
    expect(projectLabel("/")).toBe("/");
    expect(projectLabel("C:\\")).toBe("C:");
  });

  it("没有目录时回空串(调用方据此用「未分组」)", () => {
    expect(projectLabel(null)).toBe("");
    expect(projectLabel("")).toBe("");
  });
});

describe("groupByProject", () => {
  it("同一 cwd 的会话进同一组,顺序保持后端给的顺序", () => {
    const groups = groupByProject(
      [
        session({ id: "a", cwd: "/w/qi", updated_at: "2026-09-17T11:00:00" }),
        session({ id: "b", cwd: "/w/other" }),
        session({ id: "c", cwd: "/w/qi" }),
      ],
      null,
    );
    expect(groups.map((g) => g.label)).toEqual(["qi", "other"]);
    expect(groups[0]?.sessions.map((s) => s.id)).toEqual(["a", "c"]);
    expect(groups[0]?.cwd).toBe("/w/qi");
  });

  it("组顺序 = 组内最近活跃的那条在列表里的位置(不按名字重排)", () => {
    const groups = groupByProject(
      [
        session({ id: "new-in-zeta", cwd: "/w/zeta" }),
        session({ id: "old-in-alpha", cwd: "/w/alpha" }),
      ],
      null,
    );
    expect(groups.map((g) => g.label)).toEqual(["zeta", "alpha"]);
  });

  it("没有 cwd 的旧会话进「未分组」,且不能在其中新建会话", () => {
    const groups = groupByProject([session({ id: "legacy", cwd: null })], null);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.key).toBe(UNGROUPED_KEY);
    expect(groups[0]?.label).toBe(UNGROUPED_LABEL);
    expect(groups[0]?.cwd).toBeNull();
  });

  it("containsCurrent 标出当前会话所在的一级行", () => {
    const groups = groupByProject(
      [
        session({ id: "a", cwd: "/w/qi" }),
        session({ id: "b", cwd: "/w/other" }),
      ],
      "b",
    );
    expect(groups.map((g) => g.containsCurrent)).toEqual([false, true]);
  });

  it("空列表回空数组(不造一个空组)", () => {
    expect(groupByProject([], null)).toEqual([]);
  });
});

describe("relativeTime(逐字移植 dsh 的分档)", () => {
  const MIN = 60_000;
  it("档位边界与向下取整", () => {
    expect(relativeTime(NOW - 0, NOW)).toEqual({ unit: "now", n: 0 });
    expect(relativeTime(NOW - (MIN - 1), NOW)).toEqual({ unit: "now", n: 0 });
    expect(relativeTime(NOW - MIN, NOW)).toEqual({ unit: "minutes", n: 1 });
    expect(relativeTime(NOW - 59 * MIN, NOW)).toEqual({
      unit: "minutes",
      n: 59,
    });
    expect(relativeTime(NOW - 60 * MIN, NOW)).toEqual({ unit: "hours", n: 1 });
    expect(relativeTime(NOW - 24 * 60 * MIN, NOW)).toEqual({
      unit: "days",
      n: 1,
    });
    expect(relativeTime(NOW - 30 * 24 * 60 * MIN, NOW)).toEqual({
      unit: "months",
      n: 1,
    });
    expect(relativeTime(NOW - 365 * 24 * 60 * MIN, NOW)).toEqual({
      unit: "years",
      n: 1,
    });
  });

  it("未来时刻被夹到 0(时钟漂移不该渲染成负数)", () => {
    expect(relativeTime(NOW + 10 * MIN, NOW)).toEqual({ unit: "now", n: 0 });
  });
});

describe("parseStamp", () => {
  it("按本地时间读宿主那种不带时区的格式", () => {
    // 宿主写的是 `%Y-%m-%dT%H:%M:%S`(本地时间),必须与写入侧同源。
    expect(parseStamp("2026-09-17T01:05:52")).toBe(
      new Date(2026, 8, 17, 1, 5, 52).getTime(),
    );
  });

  it("空值与坏值回 null", () => {
    expect(parseStamp("")).toBeNull();
    expect(parseStamp(null)).toBeNull();
    expect(parseStamp("not-a-date")).toBeNull();
  });
});

describe("relativeLabel / stampOf", () => {
  it("给出 dsh 的中文档位词", () => {
    expect(relativeLabel("2026-09-17T11:59:30", NOW)).toBe("刚刚");
    expect(relativeLabel("2026-09-17T11:45:00", NOW)).toBe("15分钟");
    expect(relativeLabel("2026-09-16T13:00:00", NOW)).toBe("23小时");
    expect(relativeLabel("2026-09-15T12:00:00", NOW)).toBe("2天");
  });

  it("时间戳不可解析时回空串(不显示 NaN)", () => {
    expect(relativeLabel("", NOW)).toBe("");
    expect(relativeLabel(undefined, NOW)).toBe("");
  });

  it("优先用最近活跃,缺失才回落创建时间", () => {
    expect(
      stampOf(
        session({
          id: "a",
          updated_at: "2026-09-17T11:00:00",
          created_at: "2020-01-01T00:00:00",
        }),
      ),
    ).toBe("2026-09-17T11:00:00");
    expect(
      stampOf(session({ id: "b", created_at: "2020-01-01T00:00:00" })),
    ).toBe("2020-01-01T00:00:00");
  });
});

describe("工作区改过的显示名", () => {
  it("覆盖目录名", () => {
    const groups = groupByProject(
      [session({ id: "a", cwd: "/w/qi" })],
      null,
      { names: { "/w/qi": "我的项目" } },
    );
    expect(groups[0]?.label).toBe("我的项目");
    expect(groups[0]?.cwd).toBe("/w/qi"); // 键仍是目录:改名只改显示名
  });

  it("没有条目的目录用目录名", () => {
    const groups = groupByProject(
      [session({ id: "a", cwd: "/w/qi" }), session({ id: "b", cwd: "/w/other" })],
      null,
      { names: { "/w/qi": "改过的" } },
    );
    expect(groups.map((g) => g.label)).toEqual(["改过的", "other"]);
  });

  it("没有 cwd 的旧会话仍然进「未分组」,且它没有 cwd(也就没有新建动作)", () => {
    const groups = groupByProject(
      [session({ id: "a", cwd: null }), session({ id: "b", cwd: "/w/live" })],
      null,
    );
    expect(groups.map((g) => g.label)).toEqual(["未分组", "live"]);
    expect(groups[0]?.cwd).toBeNull();
  });

  it("工作区被删掉之后它就不再成组(会话一起被删了,不是搬到别处)", () => {
    // 只剩别的目录的会话:被删的工作区不应以任何形式留在列表里 ——
    // 既不该单独成组,也不该折进「未分组」(早先那版会折进去)。
    const groups = groupByProject(
      [session({ id: "b", cwd: "/w/live" })],
      null,
      { names: {} },
    );
    expect(groups.map((g) => g.label)).toEqual(["live"]);
  });

  it("没有偏好(默认参数)时行为与前几轮完全一致", () => {
    const list = [session({ id: "a", cwd: "/w/qi" })];
    expect(groupByProject(list, null)).toEqual(
      groupByProject(list, null, EMPTY_NAMES),
    );
  });
});

describe("左栏搜索", () => {
  const list = [
    session({ id: "a", cwd: "/w/qi", title: "整页外壳对齐 dsh" }),
    session({ id: "b", cwd: "/w/qi", title: "左栏二级目录" }),
    session({ id: "c", cwd: "/w/Hermit", title: "HELLO" }),
  ];
  const groups = groupByProject(list, null);

  it("空查询原样返回(不复制、不改顺序)", () => {
    expect(filterGroups(groups, "")).toBe(groups);
    expect(filterGroups(groups, "   ")).toBe(groups);
  });

  it("按标题过滤,命中的会话留下、空掉的工作区整行消失", () => {
    const hit = filterGroups(groups, "二级");
    expect(hit.map((g) => g.label)).toEqual(["qi"]);
    expect(hit[0]?.sessions.map((s) => s.id)).toEqual(["b"]);
  });

  it("工作区名命中 → 该工作区下的会话全部保留", () => {
    const hit = filterGroups(groups, "hermit");
    expect(hit.map((g) => g.label)).toEqual(["Hermit"]);
    expect(hit[0]?.sessions.map((s) => s.id)).toEqual(["c"]);
  });

  it("大小写不敏感、首尾空白忽略", () => {
    expect(filterGroups(groups, "  hello ")[0]?.sessions.map((s) => s.id)).toEqual(["c"]);
    expect(filterGroups(groups, "DSH")[0]?.sessions.map((s) => s.id)).toEqual(["a"]);
  });

  it("都不命中时回空数组(界面据此显示「没有匹配」)", () => {
    expect(filterGroups(groups, "没有这条")).toEqual([]);
  });
});
