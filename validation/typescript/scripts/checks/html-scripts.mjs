#!/usr/bin/env node
/**
 * `html-scripts.mjs`: two invariants of the SPA entry document
 * (`--html`, default `index.html` under the target root).
 *
 * - `html-scripts-not-deferred`: every `<script>` tag is `type="module"`,
 *   `defer` or `async`, so no script blocks parsing.
 * - `html-scripts-third-party-origin`: no tag loads from another origin -- a
 *   `src` or `href` that is an absolute URL or a protocol-relative `//host`
 *   reference -- so first paint waits on no third-party stylesheet, font or
 *   script; such resources ship with the built assets instead.
 *
 * A tiny, purpose-built tag reader (attribute presence only, not full HTML
 * parsing): the entry document is one small, hand-written file, not arbitrary
 * HTML, and a full parser would answer nothing a bounded tag-attribute regex
 * does not.
 *
 * usage: node html-scripts.mjs [--root <dir>] [--html <file>]
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';

const DEFAULT_HTML = 'index.html';
const SCRIPT_TAG_RE = /<script\b([^>]*)>/gi;
const ANY_TAG_RE = /<([a-z][\w-]*)\b([^>]*)>/gi;
const COMMENT_RE = /<!--[\s\S]*?-->/g;
const URL_ATTRS = ['src', 'href'];
/** A network reference that names its own host: `https://host`, `http://host` or `//host`. */
const OTHER_ORIGIN_RE = /^(?:https?:)?\/\//i;
const ATTR_RE = /(\w[\w-]*)(?:\s*=\s*("([^"]*)"|'([^']*)'|[^\s"'>]+))?/g;

function parseAttrs(attrText) {
  const attrs = {};
  let m;
  ATTR_RE.lastIndex = 0;
  while ((m = ATTR_RE.exec(attrText))) {
    attrs[m[1].toLowerCase()] = m[3] ?? m[4] ?? m[2] ?? true;
  }
  return attrs;
}

function htmlPathOf(root, options) {
  return path.resolve(root, stringOption(options, 'html', DEFAULT_HTML));
}

export function check({ root, options = {} }) {
  const htmlPath = htmlPathOf(root, options);
  if (!existsSync(htmlPath)) return [];
  const text = readFileSync(htmlPath, 'utf8');
  const relFile = relPath(root, htmlPath);
  const findings = [];

  let m;
  SCRIPT_TAG_RE.lastIndex = 0;
  while ((m = SCRIPT_TAG_RE.exec(text))) {
    const attrs = parseAttrs(m[1]);
    const isModule = typeof attrs.type === 'string' && attrs.type.toLowerCase() === 'module';
    if (isModule || attrs.defer !== undefined || attrs.async !== undefined) continue;
    const line = text.slice(0, m.index).split('\n').length;
    findings.push(
      finding(
        relFile,
        line,
        1,
        'html-scripts-not-deferred',
        `<script> tag has none of type="module", defer or async: ${m[0]}`,
        { fix: 'add type="module", defer, or async' },
      ),
    );
  }

  // A commented-out tag loads nothing; blanking comments keeps every line number in place.
  const markup = text.replace(COMMENT_RE, (comment) => comment.replace(/[^\n]/g, ' '));
  ANY_TAG_RE.lastIndex = 0;
  while ((m = ANY_TAG_RE.exec(markup))) {
    const attrs = parseAttrs(m[2]);
    for (const name of URL_ATTRS) {
      const value = attrs[name];
      if (typeof value !== 'string' || !OTHER_ORIGIN_RE.test(value.trim())) continue;
      findings.push(
        finding(
          relFile,
          markup.slice(0, m.index).split('\n').length,
          1,
          'html-scripts-third-party-origin',
          `<${m[1].toLowerCase()} ${name}="${value}"> loads from another origin; first paint waits on no third-party request`,
          { fix: 'ship the resource with the built assets and reference it by a local path' },
        ),
      );
    }
  }

  return findings;
}

function skipIf({ root, options }) {
  const htmlPath = htmlPathOf(root, options);
  if (!existsSync(htmlPath)) return `no targets: ${relPath(root, htmlPath)} does not exist`;
  return null;
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
