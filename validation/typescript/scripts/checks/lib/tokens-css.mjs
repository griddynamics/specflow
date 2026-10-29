/**
 * Reads CSS custom-property declarations out of a theme stylesheet for
 * `tokens.mjs` and `contrast.mjs`: `:root { --x: value; }`, the one palette.
 * A small, purpose-built reader over exactly one flat selector block of
 * `--name: value;` declarations; a full CSS AST buys nothing here that two
 * regular expressions do not already give correctly.
 */

import { readFileSync } from 'node:fs';

/** Body of the first `{ ... }` block whose selector matches `selectorRe`,
 * or `null` when the selector is absent. */
function blockDeclarations(cssText, selectorRe) {
  const selMatch = selectorRe.exec(cssText);
  if (!selMatch) return null;
  const braceStart = cssText.indexOf('{', selMatch.index);
  if (braceStart === -1) return null;
  let depth = 1;
  let i = braceStart + 1;
  while (i < cssText.length && depth > 0) {
    if (cssText[i] === '{') depth++;
    else if (cssText[i] === '}') depth--;
    i++;
  }
  return cssText.slice(braceStart + 1, i - 1);
}

/** `{ "--name": {value, line} }` for every custom property declared in the
 * block matching `selectorRe`, or `{}` if the selector is absent. */
function readCustomProperties(cssText, selectorRe) {
  const body = blockDeclarations(cssText, selectorRe);
  if (body === null) return {};
  const out = {};
  const declRe = /(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g;
  const beforeBody = cssText.indexOf(body);
  let m;
  while ((m = declRe.exec(body))) {
    const line = cssText.slice(0, beforeBody + m.index).split('\n').length;
    out[m[1]] = { value: m[2].trim(), line };
  }
  return out;
}

/** The `:root` declarations of the theme stylesheet: the one palette. */
export function readTokensCss(cssPath) {
  // project: the design-system sheet declares its palette over several blocks, among them
  // `:root, [data-theme="light"] { ... }`; every block whose selector list starts with `:root` counts.
  const cssText = readFileSync(cssPath, 'utf8');
  const merged = {};
  const rootRe = /(^|\n)[ \t]*:root[ \t]*(?:,[^{}]*)?\{/g;
  let match;
  while ((match = rootRe.exec(cssText))) {
    const open = match.index + match[0].length - 1;
    let depth = 1;
    let end = open + 1;
    while (end < cssText.length && depth > 0) {
      if (cssText[end] === '{') depth++;
      else if (cssText[end] === '}') depth--;
      end++;
    }
    const declRe = /(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g;
    const body = cssText.slice(open + 1, end - 1);
    let decl;
    while ((decl = declRe.exec(body))) {
      const line = cssText.slice(0, open + 1 + decl.index).split('\n').length;
      merged[decl[1]] = { value: decl[2].trim(), line };
    }
  }
  return merged;
}
