// FE-REV-02, FE-REV-03, FE-MC-08 (TECH-SPECS §2.7, §2.8): pure helpers for Markdown blocks, sections and text anchors.
// Offsets are UTF-16 code unit indexes into the document body, the same unit as the mdast `position.offset` that react-markdown
// passes as `node` (so CRLF counts as two and an emoji as two). Node-testable: no React, no DOM.
import GithubSlugger from "github-slugger";
import type { TextAnchor } from "@/lib/domain/comments";
import type { BlockRef } from "@/lib/domain/files";

export interface HeadingEntry {
  offset: number;
  level: number;
  slug: string;
  text: string;
}

/** The text of every `[text](target)` (or `![alt](target)` when `prefix` is "![") in `src`, in place of the whole construct. */
function unwrapLinks(src: string, prefix: "[" | "!["): string {
  let out = "";
  let at = 0;
  let close = -1; // the first "]" at or after the scan position, kept so a run of "[" is scanned once
  while (at < src.length) {
    const open = src.indexOf(prefix, at);
    if (open < 0) break;
    const textStart = open + prefix.length;
    if (close < textStart) close = src.indexOf("]", textStart);
    if (close < 0) break; // no "]" left: no construct can follow
    const end = src[close + 1] === "(" ? src.indexOf(")", close + 2) : -1;
    if (end < 0) {
      out += src.slice(at, open + 1);
      at = open + 1;
      continue;
    }
    out += src.slice(at, open) + src.slice(textStart, close);
    at = end + 1;
  }
  return out + src.slice(at);
}

/** `src` without HTML tags (`<` up to the next `>`, at least one character between). */
function stripTags(src: string): string {
  let out = "";
  let at = 0;
  while (at < src.length) {
    const open = src.indexOf("<", at);
    if (open < 0) break;
    const close = src.indexOf(">", open + 1);
    if (close < 0) break; // no ">" left: no tag can follow
    if (close === open + 1) {
      out += src.slice(at, open + 1); // "<>" is not a tag
      at = open + 1;
    } else {
      out += src.slice(at, open);
      at = close + 1;
    }
  }
  return out + src.slice(at);
}

const isWordChar = (c: string): boolean => /\w/.test(c);

/** `_em_` at word edges loses its underscores; intraword underscores stay, as in rehype-slug's text. */
function stripEdgeUnderscores(src: string): string {
  let out = "";
  let at = 0;
  while (at < src.length) {
    if (src[at] !== "_") {
      out += src[at];
      at++;
      continue;
    }
    let end = at;
    while (src[end] === "_") end++;
    const edgeBefore = at === 0 || !isWordChar(src[at - 1]);
    const edgeAfter = end === src.length || !isWordChar(src[end]);
    if (!edgeBefore && !edgeAfter) out += src.slice(at, end);
    at = end;
  }
  return out;
}

/** Plain text of an ATX heading's inline Markdown, like rehype-slug's `toString` (links, emphasis, code, entities). */
export function plainHeadingText(src: string): string {
  return stripEdgeUnderscores(
    stripTags(unwrapLinks(unwrapLinks(src, "!["), "["))
      .replace(/\\([!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~])/g, (_m, c: string) => String.fromCharCode(0xe000 + c.charCodeAt(0))) // escapes: kept literal, restored below
      .replace(/\*\*|__|\*|`/g, ""), // emphasis and code markers
  )
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/[\ue000-\ue0ff]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xe000))
    .trim();
}

const isBlank = (c: string | undefined): boolean => c === " " || c === "\t";

/**
 * An ATX heading line: up to three leading spaces, one to six `#`, a blank, the text, and an optional closing run of `#` after a
 * blank. Null when the line is not one.
 */
function atxHeading(line: string): { level: number; text: string } | null {
  let at = 0;
  while (at < 3 && line[at] === " ") at++;
  let level = 0;
  while (line[at + level] === "#") level++;
  if (level < 1 || level > 6 || !isBlank(line[at + level])) return null;
  const rest = line.slice(at + level);
  let from = 0;
  while (isBlank(rest[from])) from++;
  if (from === rest.length) return rest.length >= 2 ? { level, text: " " } : null; // blanks only: the last blank is the text
  let end = rest.length;
  while (isBlank(rest[end - 1])) end--;
  let hashes = end;
  while (rest[hashes - 1] === "#") hashes--;
  if (hashes < end && hashes > from && isBlank(rest[hashes - 1])) {
    end = hashes;
    while (isBlank(rest[end - 1])) end--; // the closing run and the blanks before it are not part of the text
  }
  return { level, text: rest.slice(from, end) };
}

/**
 * The ATX headings of a Markdown body with their start offset and the slug rehype-slug gives them (github-slugger, duplicates get
 * "-1"). Code fences are skipped. Setext headings (underlined) are not indexed; the fixtures use ATX only.
 */
export function headingIndex(body: string): HeadingEntry[] {
  const out: HeadingEntry[] = [];
  const slugger = new GithubSlugger();
  let fence: string | null = null;
  let offset = 0;
  for (const raw of body.split("\n")) {
    const line = raw.replace(/\r$/, "");
    const f = /^ {0,3}(`{3,}|~{3,})/.exec(line);
    if (f) {
      if (fence === null) fence = f[1][0];
      else if (f[1].startsWith(fence)) fence = null;
    } else if (fence === null) {
      const heading = atxHeading(line);
      if (heading) {
        const text = plainHeadingText(heading.text);
        out.push({ offset, level: heading.level, slug: slugger.slug(text), text });
      }
    }
    offset += raw.length + 1;
  }
  return out;
}

/** The slug of the section a source offset belongs to (the last heading at or before it); "" before the first heading. */
export function sectionAt(index: HeadingEntry[], offset: number): string {
  let slug = "";
  for (const h of index) {
    if (h.offset > offset) break;
    slug = h.slug;
  }
  return slug;
}

/** The source of one block as the editor shows it: line endings are `\n`. */
export function blockSource(body: string, block: BlockRef): string {
  return body.slice(block.start, block.end).replace(/\r\n/g, "\n");
}

export interface TextRange {
  start: number;
  end: number;
}

/** How much text before and after the quote an anchor keeps to find it again (TECH-SPECS §2.8, text-quote anchor). */
export const ANCHOR_CONTEXT = 24;

/** A text-quote anchor for `text.slice(start, end)` of a block of the section `section`. */
export function buildAnchor(path: string, section: string, text: string, range: TextRange): TextAnchor {
  return {
    path,
    section,
    quote: text.slice(range.start, range.end),
    prefix: text.slice(Math.max(0, range.start - ANCHOR_CONTEXT), range.start),
    suffix: text.slice(range.end, range.end + ANCHOR_CONTEXT),
  };
}

/** Whether one side of an anchor fits: null = unconstrained (undefined), the block edge for "", else the text comparison. */
function sideFit(side: string | undefined, atEdge: boolean, textMatches: boolean): boolean | null {
  if (side === undefined) return null;
  return side === "" ? atEdge : textMatches;
}

/**
 * Where an anchor's quote sits in the plain text of one block, or null.
 * - `prefix` / `suffix` undefined: that side is unconstrained (the seed anchor has neither: first occurrence counts).
 * - A non-empty side must fit. An EMPTY string means "the quote touches that edge of the block" (buildAnchor writes "" there), so
 *   with both empty the quote must be the whole block text (so a list item "Postgres" does not light up "Postgres is used ...").
 * - After edits the context may drift: a match whose NON-EMPTY side still fits is accepted when the other side no longer does,
 *   but an empty side never counts as context. Highlights therefore survive edits elsewhere in the same block.
 */
export function locateQuote(text: string, anchor: Pick<TextAnchor, "quote" | "prefix" | "suffix">): TextRange | null {
  const { quote, prefix, suffix } = anchor;
  if (!quote) return null;
  let partial: TextRange | null = null;
  for (let i = text.indexOf(quote); i >= 0; i = text.indexOf(quote, i + 1)) {
    const range = { start: i, end: i + quote.length };
    const before = text.slice(0, i);
    const after = text.slice(range.end);
    const pre = sideFit(prefix, before.trim() === "", prefix !== undefined && before.endsWith(prefix));
    const suf = sideFit(suffix, after.trim() === "", suffix !== undefined && after.startsWith(suffix));
    if (pre !== false && suf !== false) return range;
    const context = (pre === true && prefix !== undefined && prefix !== "") || (suf === true && suffix !== undefined && suffix !== "");
    if (context && !partial) partial = range;
  }
  return partial;
}

/** Sorts highlight ranges and drops any that overlap an earlier one, so a block never nests marks. */
export function disjointRanges<T extends TextRange>(ranges: T[]): T[] {
  const out: T[] = [];
  let edge = 0;
  for (const r of ranges.toSorted((a, b) => a.start - b.start || b.end - a.end)) {
    if (r.end <= r.start || r.start < edge) continue;
    out.push(r);
    edge = r.end;
  }
  return out;
}
