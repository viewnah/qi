/**
 * qi 的 hero 品牌标 —— 首页 headline 左边那个 mark。
 *
 * **规格照搬** dsh `ui-conversation/src/client/skeleton/EmptyHero.tsx` 的 `HeroFish`,
 * **图形是 qi 自己的**(不是 dsh 的鲸鱼):
 *   · 渲染盒就是 dsh 的 fish 盒:`viewBox 0 0 23.16 17.04` + `width={34}` → 高 25.02;
 *   · 静止态是静态填充;`overflow: visible`,因为形变曲线会顶出 viewBox 边缘;
 *   · hover 时 SMIL 在「静止 / 抬尾 / 垂尾」三帧间插值 `d`(1.6s、无限循环),
 *     同时 hitbox 上的 CSS 给一个 1.6s 的头部上浮摇摆 —— 两者同周期,所以
 *     "摆尾"与"浮沉"读起来是一个动作;
 *   · 只在 `(hover: hover) and (prefers-reduced-motion: no-preference)` 下动:
 *     触摸设备与"减少动效"偏好下,hover 保持静态填充;
 *   · `aria-hidden`,`transform-origin: 50% 60%`(碗心偏下,旋转像踩水)。
 * 图形读作 "q":一个环形碗 + 一条向右下摆出的尾。
 *
 * 三段 `d` 是**脚本生成**的,不是手调坐标:把尾的 4 个角点绕碗心旋转
 * θ(抬尾 −8°、垂尾 +7°),其余命令逐字不变,于是三帧命令结构完全一致 ——
 * 这是 SMIL 能插值 `d` 的前提。坐标由该脚本拟合进 dsh 的 viewBox
 * (等比缩放 + 平移,绘制范围 21.54 × 16.02,长宽比 1.345 对盒子 1.359)。
 *
 * 一个已经踩过并修掉的坑:尾部的**绕向必须与外壳椭圆一致**。绕反了,
 * `fill-rule: nonzero` 会在「尾 ∩ 壳」处抵消,把尾部根部挖出一个透镜形的
 * 洞(实测 0.61 unit²,并且只有那一个区域的绕数为 0)。
 */

/** 静止帧。 */
const QI_MARK_PATH =
  "M21.315 7.198A9.2,6 0 1 1 0.809 7.198A9.2,6 0 1 1 21.315 7.198ZM18.083 7.198A6.3,3.1 0 1 0 4.041 7.198A6.3,3.1 0 1 0 18.083 7.198ZM18.59 10.958L21.395 14.79L20.929 15.387L16.532 13.593Z";

/** 抬尾帧:尾部绕碗心 −8°(整体上浮)。 */
const QI_SWIM_UP_PATH =
  "M21.315 7.198A9.2,6 0 1 1 0.809 7.198A9.2,6 0 1 1 21.315 7.198ZM18.083 7.198A6.3,3.1 0 1 0 4.041 7.198A6.3,3.1 0 1 0 18.083 7.198ZM19.04 9.874L22.351 13.278L21.972 13.935L17.369 12.77Z";

/** 垂尾帧:尾部绕碗心 +7°(整体下沉)。 */
const QI_SWIM_DOWN_PATH =
  "M21.315 7.198A9.2,6 0 1 1 0.809 7.198A9.2,6 0 1 1 21.315 7.198ZM18.083 7.198A6.3,3.1 0 1 0 4.041 7.198A6.3,3.1 0 1 0 18.083 7.198ZM18.076 11.848L20.393 15.993L19.857 16.529L15.712 14.212Z";

/**
 * 渲染 qi 的 hero 品牌标。
 * @param props.hovering - 由父级 hitbox 的指针状态驱动;`true` 时挂上形变动画。
 * @returns 品牌标的 svg 元素。
 */
export function HeroMark({ hovering }: { hovering: boolean }) {
  return (
    <svg
      className="hero__mark"
      width={34}
      height={(34 * 17.04) / 23.16}
      viewBox="0 0 23.16 17.04"
      fill="none"
      aria-hidden="true"
    >
      <path d={QI_MARK_PATH} fill="currentColor">
        {hovering && (
          <animate
            attributeName="d"
            values={`${QI_MARK_PATH};${QI_SWIM_UP_PATH};${QI_MARK_PATH};${QI_SWIM_DOWN_PATH};${QI_MARK_PATH}`}
            keyTimes="0;0.35;0.55;0.75;1"
            calcMode="spline"
            keySplines="0.45 0 0.55 1;0.45 0 0.55 1;0.45 0 0.55 1;0.45 0 0.55 1"
            dur="1.6s"
            repeatCount="indefinite"
          />
        )}
      </path>
    </svg>
  );
}
