// R4, FE-DS-04, FE-DS-05, FE-DOD-03 (TECH-SPECS §10.5): `npm run check:tokens`. It enforces these property classes in app UI code, and only these:
// hex and rgb/hsl colours, `font-size`/`fontSize`, `font-family`/`fontFamily`, `font:` shorthand with px, `border-radius`/`borderRadius`,
// `box-shadow`/`boxShadow` and `blur()` literals. It does NOT enforce spacing or size literals (`gap`, `padding`, `margin`, `width`, `height`,
// `min-`/`max-` sizes, `rem`/`px` lengths) or `font-weight`; the ones in scope are listed in the P18 gaps list. Skips styles/ds/**.
// One escape hatch per line: `// tokens-ok: <reason>` (or `/* tokens-ok: <reason> */`); escapes are counted and printed.
// `--root <dir>` points the scan at another frontend folder (the self-test uses it).
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// P17 s09 (FE-DOD-03.AC1, FE-DS-01.AC1 "all app UI after P17"): every app route and layout (app/**, production tree and
// /login), every component (components/**, no hello exemption any more: the hello components are removed), React files under lib/,
// and app styles under styles/ (except the design-system copy styles/ds/**).
const IN_SCOPE = [
  (rel) => rel.startsWith("app/"),
  (rel) => rel.startsWith("components/"),
  (rel) => rel.startsWith("lib/") && rel.endsWith(".tsx"),
  (rel) => rel.startsWith("styles/"),
];
const EXT = /\.(css|ts|tsx|jsx)$/;
const SKIP_DIR = /(^|\/)(node_modules|\.next|styles\/ds)(\/|$)/;

// A value that is a token reference or a harmless keyword is fine.
const OK_VALUE = String.raw`(?!["'\x60]?(?:var\(|inherit\b|revert\b|initial\b|unset\b|none\b|0(?=\s*(?:[;,"'\x60}]|$))|currentcolor\b|transparent\b))`;
const RULES = [
  ["hex colour", /#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3,4}\b/],
  ["rgb/hsl colour", /\b(?:rgba?|hsla?)\(/],
  ["font-size literal", new RegExp(String.raw`\bfont-size\s*:\s*${OK_VALUE}[\d."']`)],
  ["font-size literal", new RegExp(String.raw`\bfontSize\s*:\s*${OK_VALUE}[\d."']`)],
  ["font shorthand with px", /\bfont\s*:[^;{}]*\d\s*px/],
  ["font-family literal", new RegExp(String.raw`\bfont-family\s*:\s*${OK_VALUE}\S`)],
  ["font-family literal", new RegExp(String.raw`\bfontFamily\s*:\s*${OK_VALUE}\S`)],
  ["radius literal", new RegExp(String.raw`\bborder-radius\s*:\s*${OK_VALUE}[\d."'%]`)],
  ["radius literal", new RegExp(String.raw`\bborderRadius\s*:\s*${OK_VALUE}[\d."'%]`)],
  ["shadow literal", new RegExp(String.raw`\bbox-shadow\s*:\s*${OK_VALUE}\S`)],
  ["shadow literal", new RegExp(String.raw`\bboxShadow\s*:\s*${OK_VALUE}\S`)],
  ["blur literal", /\bblur\(\s*[\d.]/],
];
const ESCAPE = /tokens-ok:\s*\S/;

function walk(dir, root, out = []) {
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    const rel = path.relative(root, full).split(path.sep).join("/");
    if (SKIP_DIR.test(rel)) continue;
    if (statSync(full).isDirectory()) walk(full, root, out);
    else if (EXT.test(name) && IN_SCOPE.some((f) => f(rel))) out.push(rel);
  }
  return out;
}

/** @returns {{hits: {file: string, line: number, rule: string}[], escapes: number}} */
export function checkSource(rel, text) {
  const hits = [];
  let escapes = 0;
  text.split("\n").forEach((raw, i) => {
    const trimmed = raw.trim();
    if (/^(\/\/|\/\*|\*)/.test(trimmed)) return; // comment-only line
    if (ESCAPE.test(raw)) {
      escapes += 1;
      return;
    }
    // Strip trailing comments so prose in them is not scanned.
    const code = raw.replace(/\/\*.*?\*\//g, "").replace(/(^|\s)\/\/.*$/, "");
    const seen = new Set();
    for (const [rule, re] of RULES) {
      if (!seen.has(rule) && re.test(code)) {
        seen.add(rule);
        hits.push({ file: rel, line: i + 1, rule });
      }
    }
  });
  return { hits, escapes };
}

export function run(root) {
  const files = ["app", "components", "lib", "styles"].flatMap((d) => {
    try {
      return walk(path.join(root, d), root);
    } catch {
      return [];
    }
  });
  const hits = [];
  let escapes = 0;
  for (const rel of files) {
    const r = checkSource(rel, readFileSync(path.join(root, rel), "utf8"));
    hits.push(...r.hits);
    escapes += r.escapes;
  }
  return { files: files.length, hits, escapes };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const i = process.argv.indexOf("--root");
  const root = i >= 0 ? path.resolve(process.argv[i + 1]) : path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const { files, hits, escapes } = run(root);
  for (const h of hits) console.log(`${h.file}:${h.line}: ${h.rule}`);
  console.log(`check:tokens: files=${files} hits=${hits.length} escapes=${escapes}`);
  process.exit(hits.length > 0 ? 1 : 0);
}
