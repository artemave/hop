import { describe, expect, test } from "bun:test";
import { existsSync, readFileSync } from "node:fs";

describe("repository smoke checks", () => {
  test("canonical project files exist", () => {
    expect(existsSync("hop_spec.md")).toBe(true);
    expect(existsSync(".dust/config/settings.json")).toBe(true);
  });

  test("dust test check runs bun test", () => {
    const settings = readFileSync(".dust/config/settings.json", "utf8");

    expect(settings).toContain('"name": "test"');
    expect(settings).toContain('"command": "bun test"');
  });
});
