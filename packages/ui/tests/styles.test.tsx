// The stylesheet contract: one root class, light and dark under it, nothing global.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const styles = path.join(path.dirname(fileURLToPath(import.meta.url)), "../src/styles");
const read = (name: string): string => readFileSync(path.join(styles, name), "utf8");
const SHEETS = ["tokens.css", "base.css", "components.css"];
const strip = (css: string): string => css.replaceAll(/\/\*[^*]*(?:\*(?!\/)[^*]*)*\*\//g, "");

test("every rule of every stylesheet is scoped under .gx or a gx- class", () => {
  for (const file of SHEETS) {
    const css = strip(read(file));
    const selectors = css
      .split("{")
      .slice(0, -1)
      .map((part) => part.slice(part.lastIndexOf("}") + 1).trim())
      .filter((part) => part !== "" && !part.startsWith("@"));
    assert.ok(selectors.length > 0, file);
    for (const list of selectors) {
      for (const selector of list
        .replaceAll(/\([^()]*\)/g, (group) => group.replaceAll(",", ";"))
        .split(",")
        .map((s) => s.trim())) {
        assert.ok(/\.gx\b|:root\[data-theme="(?:light|dark)"\] \.gx|^\.gx-/.test(selector), `${file}: ${selector}`);
      }
    }
  }
});

test("tokens: each colour is a light-dark pair, the theme attribute picks one scheme, yellow is one value", () => {
  const css = strip(read("tokens.css"));
  for (const name of ["paper", "ink", "ink-2", "ink-3", "rule", "wash", "inv-bg", "inv-fg", "inv-mute"]) {
    assert.match(css, new RegExp(`--gx-${name}: light-dark\\(`), name);
  }
  assert.match(css, /--gx-yellow: #ffc002;/);
  assert.match(css, /\.gx\[data-theme="dark"\],\s*:root\[data-theme="dark"\] \.gx \{\s*color-scheme: dark;/);
  assert.match(css, /\.gx\[data-theme="light"\],\s*:root\[data-theme="light"\] \.gx \{\s*color-scheme: light;/);
});

test("the entry stylesheet imports the three files and the base switches motion off for reduced-motion", () => {
  const index = read("index.css");
  for (const file of SHEETS) assert.ok(index.includes(`@import "./${file}";`), file);
  assert.match(read("base.css"), /@media \(prefers-reduced-motion: reduce\)[\s\S]*animation: none !important/);
});

// The kit's `tokens` check reads only a `:root` block; the tokens here sit on `.gx`, so this test is its counterpart.
test("every var(--gx-*) in the stylesheets names a token that tokens.css declares", () => {
  const declared = new Set([...strip(read("tokens.css")).matchAll(/(--gx-[a-z0-9-]+):/g)].map((m) => m[1]));
  for (const file of SHEETS) {
    for (const [, name] of strip(read(file)).matchAll(/var\((--gx-[a-z0-9-]+)/g)) {
      assert.ok(declared.has(name ?? ""), `${file}: ${String(name)}`);
    }
  }
});
