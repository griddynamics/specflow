import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, test } from "node:test";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const out = path.join(root, "out");

function files(dir: string, extensions: string[]): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return files(full, extensions);
    return extensions.includes(path.extname(name)) ? [full] : [];
  });
}

/** The real repository URL and the team mailbox keep their names; the visible brand is Graphico. */
const KEPT = ["github.com/griddynamics/specflow", "specflow@griddynamics.com"];
const OLD_BRAND = /specflow|spec(graph|gap|plan)/i;
const DEEP_DIVES = /(graph|gap|plan)\.html|deep-dive/i;

function withoutKept(text: string): string {
  return KEPT.reduce((rest, kept) => rest.split(kept).join(""), text);
}

describe("source", () => {
  const sources = ["app", "components", "lib"].flatMap((dir) => files(path.join(root, dir), [".ts", ".tsx", ".css"]));

  test("has no old product names and no link to a deep-dive page", () => {
    for (const file of sources) {
      const text = withoutKept(readFileSync(file, "utf8"));
      assert.doesNotMatch(text, OLD_BRAND, path.relative(root, file));
      assert.doesNotMatch(text, DEEP_DIVES, path.relative(root, file));
    }
  });

  test("imports nothing from the app and calls no API or third-party script", () => {
    for (const file of sources) {
      const text = readFileSync(file, "utf8");
      assert.doesNotMatch(text, /from\s+["'][^"']*frontend\//, path.relative(root, file));
      assert.doesNotMatch(text, /\bfetch\(|XMLHttpRequest|analytics|gtag|googletagmanager/i, path.relative(root, file));
    }
  });
});

describe(
  "static export (run `npm run build` first)",
  { skip: !existsSync(path.join(out, "index.html")) && "site/out is not built" },
  () => {
    test("is the home page only", () => {
      const pages = readdirSync(out).filter((name) => name.endsWith(".html"));
      assert.deepEqual(pages.sort(), ["404.html", "_not-found.html", "index.html"]);
    });

    test("home page names Graphico, links the real repository and a Graphico demo mailto, and links no deep dive", () => {
      const html = readFileSync(path.join(out, "index.html"), "utf8");
      assert.match(html, /<title>Graphico/);
      assert.match(html, /href="https:\/\/github\.com\/griddynamics\/specflow"/);
      assert.match(html, /href="mailto:[^"]*subject=Graphico%20demo/);
      assert.doesNotMatch(withoutKept(html), OLD_BRAND);
      assert.doesNotMatch(html, DEEP_DIVES);
    });

    test("sets the theme before first paint and loads no script from a third party", () => {
      const html = readFileSync(path.join(out, "index.html"), "utf8");
      assert.match(html, /<script>\(function\(\)\{var t;try\{t=localStorage\.getItem\("graphico-theme"\)/);
      const hosts = new Set([...html.matchAll(/<(?:script|link)[^>]+(?:src|href)="(https?:\/\/[^"/]+)/g)].map((m) => m[1]));
      assert.deepEqual([...hosts].sort(), ["https://fonts.googleapis.com", "https://fonts.gstatic.com"]);
    });
  },
);
