// FE-REV-02, FE-REV-03 (PLAN a.7): maps a browser selection onto the blocks FilesDocument renders (`data-block-id`,
// `data-own-len`, `data-section`, `data-edit-start`, `data-edit-end` set by block-render.tsx). Read-only DOM access.
import type { TextRange } from "@/lib/markdown/blocks";

export interface SelectedBlock {
  el: HTMLElement;
  /** Source offset of the block node: the id highlights use. */
  blockId: number;
  section: string;
  /** Plain text of the block's own content (no nested list). */
  text: string;
  /** The selection inside `text`, trimmed of surrounding white space (null when nothing but white space is selected). */
  range: TextRange | null;
  /** Source range an Edit replaces; null for table cells. */
  edit: { start: number; end: number } | null;
}

const num = (v: string | undefined): number | null => (v === undefined || v === "" || Number.isNaN(Number(v)) ? null : Number(v));

/** The block element the selection starts in, or the first block it touches when it starts outside every block. */
function findBlockEl(range: Range, root: HTMLElement): { el: HTMLElement; startsInside: boolean } | null {
  const sc = range.startContainer;
  const startEl = sc instanceof Element ? sc : sc.parentElement;
  const inside = startEl?.closest<HTMLElement>("[data-block-id]") ?? null;
  if (inside && root.contains(inside)) return { el: inside, startsInside: true };
  const touched = [...root.querySelectorAll<HTMLElement>("[data-block-id]")].find((b) => range.intersectsNode(b)) ?? null;
  return touched ? { el: touched, startsInside: false } : null;
}

/** Where the selection starts inside one text node of the block, or -1 when the node is before it. */
function startOffsetIn(n: Text, range: Range, startsInside: boolean): number {
  if (n === range.startContainer) return range.startOffset;
  return !startsInside || range.comparePoint(n, 0) === 0 ? 0 : -1;
}

/** The selected part of the block's own text (the first `own` characters): start and end offsets, null when nothing is selected. */
function selectedSpan(el: HTMLElement, range: Range, startsInside: boolean, own: number): { start: number | null; end: number | null } {
  let pos = 0;
  let start: number | null = null;
  let end: number | null = null;
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode() as Text | null; n; n = walker.nextNode() as Text | null) {
    const len = n.data.length;
    const a = startOffsetIn(n, range, startsInside);
    if (a >= 0 && (n === range.startContainer || range.intersectsNode(n))) {
      const e = n === range.endContainer ? range.endOffset : len;
      if (pos + a < own && e > a) {
        start ??= pos + a;
        end = Math.min(pos + e, own);
      }
    }
    pos += len;
  }
  return { start, end };
}

/** The span without the white space around it; null when only white space is selected. */
function trimSpan(text: string, start: number | null, end: number | null): TextRange | null {
  if (start === null || end === null) return null;
  let s = start;
  let e = end;
  while (s < e && /\s/.test(text[s])) s++;
  while (e > s && /\s/.test(text[e - 1])) e--;
  return e > s ? { start: s, end: e } : null;
}

/** The block where the selection starts (PA11). The selection is clamped to that block: it never reaches into the next one. */
export function selectedBlock(range: Range, root: HTMLElement): SelectedBlock | null {
  const found = findBlockEl(range, root);
  if (!found) return null;
  const { el, startsInside } = found;
  const own = num(el.dataset.ownLen);
  const blockId = num(el.dataset.blockId);
  if (own === null || blockId === null) return null;
  const text = el.textContent.slice(0, own);

  const { start, end } = selectedSpan(el, range, startsInside, own);
  const trimmed = trimSpan(text, start, end);
  const es = num(el.dataset.editStart);
  const ee = num(el.dataset.editEnd);
  return {
    el,
    blockId,
    section: el.dataset.section ?? "",
    text,
    range: trimmed,
    edit: es !== null && ee !== null ? { start: es, end: ee } : null,
  };
}
