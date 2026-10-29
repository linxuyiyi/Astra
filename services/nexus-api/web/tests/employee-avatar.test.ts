import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import {
  DEFAULT_EMPLOYEE_AVATAR,
  EMPLOYEE_APPEARANCES,
  employeeAppearance,
} from "../../shared/employeeAvatar.ts";

test("all twelve stable appearances resolve to existing local assets and office poses", () => {
  assert.equal(EMPLOYEE_APPEARANCES.length, 12);
  assert.equal(new Set(EMPLOYEE_APPEARANCES.map((item) => item.id)).size, 12);
  assert.equal(
    EMPLOYEE_APPEARANCES.filter((item) => item.family === "robot").length,
    6,
  );
  assert.equal(
    EMPLOYEE_APPEARANCES.filter((item) => item.family === "pixel").length,
    6,
  );
  assert.ok(employeeAppearance(DEFAULT_EMPLOYEE_AVATAR));
  for (const item of EMPLOYEE_APPEARANCES) {
    for (const asset of [item.portrait, ...item.office.frames]) {
      assert.match(asset, /^\/(office|avatars)\/[a-z0-9_-]+\.(png|svg)$/);
      assert.ok(
        fs.existsSync(new URL(`../public${asset}`, import.meta.url)),
        asset,
      );
    }
    assert.ok(item.office.width > 0 && item.office.height > 0);
  }
});
test("unknown and unsafe identities never resolve to an image source", () => {
  for (const id of ["", "🤖", "robot-v1-99", "https://example.com/x.svg", "/avatars/other.svg", "../secret", '<img src=x onerror=alert(1)>']) {
    assert.equal(employeeAppearance(id), undefined);
  }
});
test("BotPixel assets are transparent, local and attributed without remote or script content", () => {
  for (const item of EMPLOYEE_APPEARANCES.filter(
    (item) => item.family === "pixel",
  )) {
    const svg = fs.readFileSync(
      new URL(`../public${item.portrait}`, import.meta.url),
      "utf8",
    );
    assert.match(svg, /BotPixel.*0\.10\.2/);
    assert.doesNotMatch(
      svg,
      /<(script|image|foreignObject)\b|\bon\w+=|\bhref=/,
    );
  }
  assert.match(
    fs.readFileSync(
      new URL("../public/avatars/LICENSE-BotPixel.txt", import.meta.url),
      "utf8",
    ),
    /MIT License/,
  );
});
