/**
 * `bareModelName` 的单测。
 *
 * 它看着只有一行,但**只能按第一个斜杠切**这件事有个真实反例:模型 id 自己可以带斜杠
 * (`deepseek/deepseek-v4.1-flash`)。design/web.md §18.13 记过这条 —— 当时前端想省一个
 * 后端字段、自己 `split("/")` 取最后一段,那会把 id 切坏。所以这个函数是那条教训的固化,
 * 值得钉住。
 */
import { describe, expect, it } from "vitest";
import { bareModelName } from "./ModelMenu";

describe("bareModelName", () => {
  it("去掉 provider 那一段", () => {
    expect(bareModelName("alpha/m1")).toBe("m1");
  });

  it("模型 id 自己带斜杠时,只切第一段", () => {
    // `provider/model` 是后端拼的;模型 id 里出现斜杠是合法的(厂商自己的命名空间)。
    expect(bareModelName("commandcode/deepseek/deepseek-v4.1-flash")).toBe(
      "deepseek/deepseek-v4.1-flash",
    );
  });

  it("没有斜杠时原样返回(没配置 / 老宿主给的裸名)", () => {
    expect(bareModelName("m1")).toBe("m1");
    expect(bareModelName("")).toBe("");
  });
});
