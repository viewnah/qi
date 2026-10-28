/**
 * 委派结果的两份"事后可读"材料,都由这里解析:
 *
 *   1. **横幅正文**(`result` 文本)—— qi-agents `render.py` 的 `banner()` / `BANNER_RE`
 *      是唯一事实源:`── 名字 (深度N, 成功/失败) ──`,每段三区 `·` 活动 → `✎` 思考 → 输出。
 *      本文件的正则与那份契约**逐字对齐**(fixture 由 `_render()` 真实生成,见
 *      `segments.test.ts`),TUI 按同一契约上色重画,这里按同一契约切卡。
 *   2. **`details.ui` 的 list 项**(`UiNode` 词汇表,`design/web.md` §16.2)——
 *      层级既给 `depth` 字段,也留着 label 的前导空格(纯文本消费方只认后者)。
 *
 * 设计上的两条约束(动它之前先读 `design/web-lab.md` §5):
 *
 *   · **解析失败必须退回原文**:没有横幅、或首个横幅之前还有正文(说明这压根不是
 *     横幅文档,只是某行输出碰巧长得像),一律返回 `null` —— 调用方渲染原始 `<pre>`。
 *   · **不做完整重新排版**:这里只切区,不改写正文(它是引用,重缩进会改写它)。
 */

/** 一段 = 一个 agent 的一张卡(TUI 的 `AgentCard` 就是这个形状)。 */
export interface SubSegment {
   name: string;
   /** 委派深度(横幅自带,1 起)。 */
   depth: number;
   ok: boolean;
   /** 最近的上层调用者(预序遍历里最近的、深度更小的一段)。 */
   parent?: string;
   /** 三区:原文行照搬,不剥前缀(`·` / `✎` 就是可读符号)。 */
   activity: string[];
   thoughts: string[];
   /** 输出块保持原样(可能含空行);失败时它以 `[失败] ` 开头。 */
   output: string;
}

/** 与 `render.py` 的 `BANNER_RE` 同源:`── name (深度N, 成功|失败) ──`。 */
const BANNER = /^──\s*(.+?)\s*\(深度(\d+),\s*(成功|失败)\)\s*──$/;

const ACTIVITY = /^·\s/;
const THOUGHT = /^✎\s*第\d+轮[:：]/;

/**
 * 把委派正文切成 N 段。**没把握就返回 `null`**(调用方退回原文)。
 *
 * 分区规则:横幅定段;`·` 开头进活动区,`✎ 第N轮` 开头进思考区,**空行是分区符**,
 * 无前缀的非空行归当前区(没有区则开输出区)—— 这正好还原 `_render()` 的拼装:
 * 区块之间是空行,块内行是单换行。
 */
export function parseBanners(text: string): SubSegment[] | null {
   const lines = text.split(/\r?\n/);
   const heads: number[] = [];
   for (let i = 0; i < lines.length; i++) {
      if (BANNER.test(lines[i])) heads.push(i);
   }
   if (heads.length === 0) return null;
   // 首个横幅之前还有正文 → 不是横幅文档,整篇退回原文
   if (heads[0] > 0 && lines.slice(0, heads[0]).some((l) => l.trim() !== "")) {
      return null;
   }

   const segs: SubSegment[] = [];
   let stack: string[] = []; // 按深度对齐的祖先名,用来补 `parent`
   for (let s = 0; s < heads.length; s++) {
      const start = heads[s];
      const end = s + 1 < heads.length ? heads[s + 1] : lines.length;
      const m = BANNER.exec(lines[start]);
      if (!m) continue; // 上面刚 test 过,防御性分支
      const depth = Number(m[2]);
      const seg: SubSegment = {
         name: m[1],
         depth,
         ok: m[3] === "成功",
         parent: depth > 1 ? stack[depth - 2] : undefined,
         activity: [],
         thoughts: [],
         output: "",
      };
      // 祖先链:深度必然 ≤ 上一段深度 + 1(预序),截到 depth-1 即可
      stack = stack.slice(0, depth - 1);
      stack.push(seg.name);

      let zone: "activity" | "thought" | "output" | null = null;
      for (let i = start + 1; i < end; i++) {
         const line = lines[i];
         if (ACTIVITY.test(line)) {
            zone = "activity";
            seg.activity.push(line);
            continue;
         }
         if (THOUGHT.test(line)) {
            zone = "thought";
            seg.thoughts.push(line);
            continue;
         }
         if (line.trim() === "") {
            // 空行只在输出区里保留(否则丢),并解除"当前区"
            if (zone === "output") seg.output += "\n";
            zone = null;
            continue;
         }
         if (zone === "thought") {
            // 思考文本自己可能带换行:续行贴回当前轮
            seg.thoughts[seg.thoughts.length - 1] += "\n" + line;
            continue;
         }
         // 活动行是单行契约(json.dumps 不产换行),越过它就该进输出区了
         zone = "output";
         seg.output = seg.output ? `${seg.output}\n${line}` : line;
      }
      if (seg.output) seg.output = seg.output.replace(/\n+$/, "");
      segs.push(seg);
   }
   return segs.length > 0 ? segs : null;
}

/**
 * list 项的显示层级(1 起)。
 *
 * 优先用词汇表的 `depth`;老数据没有 `depth` 时回退到 label 的前导空格(两级一格,
 * 即 `_ui_items` 的 `"  " * indent`)。两个都没有 = 顶层。
 */
export function itemDepth(item: { label: string; depth?: number }): number {
   if (typeof item.depth === "number" && item.depth > 0) return item.depth;
   const lead = item.label.length - item.label.trimStart().length;
   return Math.floor(lead / 2) + 1;
}
