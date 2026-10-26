import { describe, expect, it } from "vitest";
import { cloudPath, pct, unionBox } from "./cloud";

describe("cloudPath", () => {
  it("draws a closed scalloped path around the box", () => {
    const d = cloudPath(10, 10, 100, 20);
    expect(d.startsWith("M")).toBe(true);
    expect(d.endsWith("Z")).toBe(true);
    expect((d.match(/ A /g) ?? []).length).toBeGreaterThan(8);
  });
  it("scales the number of scallops with the perimeter", () => {
    const small = (cloudPath(0, 0, 20, 10).match(/ A /g) ?? []).length;
    const big = (cloudPath(0, 0, 400, 10).match(/ A /g) ?? []).length;
    expect(big).toBeGreaterThan(small);
  });
});

describe("unionBox", () => {
  it("covers all token boxes", () => {
    expect(unionBox([[0, 0, 10, 10], [20, 5, 10, 10]])).toEqual([0, 0, 30, 15]);
    expect(unionBox([])).toBeNull();
  });
});

it("formats percentages", () => expect(pct(0.9968)).toBe("99.7%"));
