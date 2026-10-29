"use client";
// FE-REV-02, FE-REV-03, FE-MC-08 (PLAN a.7, TECH-SPECS §2.7): the element overrides the Files view gives MarkdownProse
// (`components`). They add what react-markdown does not know: the source range of a block (from the hast `position` it passes as
// `node`), the section it belongs to, highlight marks for comment anchors, and the in-place editor of ONE block.
//
// Rules this file keeps (PLAN a.7 traps):
//  - The component types are created ONCE at module level and read their data from `DocContext`. A new component type per render
//    would remount the whole document and drop the selection and the highlights.
//  - Nothing touches the rendered DOM by hand. Highlights are React: the text children of a block are split around the anchors and
//    wrapped in <mark>. The anchors live in state (FilesDocument), not in the DOM.
//  - A heading gets `tabIndex={-1}` so the deep link can move focus to it.
import {
  Fragment,
  cloneElement,
  createContext,
  isValidElement,
  useContext,
  useLayoutEffect,
  useRef,
  useState,
  type ComponentType,
  type ElementType,
  type ReactElement,
  type ReactNode,
} from "react";
import type { Components } from "react-markdown";
import { Button } from "@/components/ui/Button";
import { disjointRanges, locateQuote, sectionAt, type HeadingEntry, type TextRange } from "@/lib/markdown/blocks";
import type { Highlight } from "./files-logic";
import { DataTable } from "@/components/ui/DataTable";

interface EditingRange {
  start: number;
  end: number;
}
export type EditingBlock = EditingRange;

interface DocContextShape {
  body: string;
  headings: HeadingEntry[];
  highlights: Highlight[];
  /** The block being edited (its source range), or null. */
  editing: EditingBlock | null;
  /** Source text of the block being edited (line endings `\n`). */
  editText: string;
  saving: boolean;
  editError: string | null;
  onSave: (text: string) => void;
  onCancel: () => void;
  /** Set after a save conflict: reloads the latest text and closes the editor. */
  onReload: (() => void) | null;
  onOpenThread: (threadId: string) => void;
}
export type DocContextValue = DocContextShape;

export const DocContext = createContext<DocContextValue | null>(null);

interface HNode {
  type: string;
  tagName?: string;
  value?: string;
  children?: HNode[];
  position?: { start: { offset?: number }; end: { offset?: number } };
}

const BLOCK_TAGS = new Set([
  "p",
  "ul",
  "ol",
  "blockquote",
  "pre",
  "table",
  "div",
  "hr",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "section",
  "details",
]);

/** Plain text of rendered children: what the DOM `textContent` of those children will be. */
export function reactText(n: ReactNode): string {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(reactText).join("");
  if (isValidElement(n)) return reactText((n.props as { children?: ReactNode }).children);
  return "";
}

type Mark = TextRange & { key: string; threadId: string | null };

function isNodeArray(n: ReactNode): n is ReactNode[] {
  return Array.isArray(n);
}

/** One highlight inside a text run: a button-like mark that opens its thread, or the plain mark of a draft. */
function renderMark(m: Mark, at: number, text: string, open: (id: string) => void): ReactNode {
  const key = `${m.key}-${String(at)}`;
  const threadId = m.threadId;
  if (threadId === null || threadId === "") {
    return (
      <mark key={key} className="gdg-mark" data-draft="" data-testid="doc-mark">
        {text}
      </mark>
    );
  }
  return (
    <mark
      key={key}
      className="gdg-mark"
      data-testid="doc-mark"
      role="button"
      tabIndex={0}
      data-thread={threadId}
      onClick={(e) => {
        e.stopPropagation(); // a highlight inside link text must not follow the link
        e.preventDefault();
        open(threadId);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          open(threadId);
        }
      }}
    >
      {text}
    </mark>
  );
}

/**
 * A string run starting at `from` in the plain text, split around the marks that touch it.
 * @returns the run itself when no mark touches it, else the pieces with the marked ones wrapped in `<mark>`
 */
function markText(s: string, from: number, marks: Mark[], open: (id: string) => void): ReactNode {
  const hit = marks.filter((m) => m.start < from + s.length && m.end > from);
  if (hit.length === 0) return s;
  const out: ReactNode[] = [];
  let at = 0;
  for (const m of hit) {
    const a = Math.max(m.start - from, 0);
    const b = Math.min(m.end - from, s.length);
    if (a > at) out.push(s.slice(at, a));
    out.push(renderMark(m, a, s.slice(a, b), open));
    at = b;
  }
  if (at < s.length) out.push(s.slice(at));
  return out.map((x, i) => <Fragment key={i}>{x}</Fragment>);
}

/**
 * Splits the string children around the marks. `st.pos` walks the plain text in document order.
 * @returns the children with the marked text wrapped in `<mark>`; nodes without text come back unchanged
 */
function markChildren(n: ReactNode, marks: Mark[], st: { pos: number }, open: (id: string) => void): ReactNode {
  if (n == null || typeof n === "boolean") return n;
  if (typeof n === "string" || typeof n === "number") {
    const s = String(n);
    const from = st.pos;
    st.pos += s.length;
    return markText(s, from, marks, open);
  }
  if (isNodeArray(n)) return n.map((c, i) => <Fragment key={i}>{markChildren(c, marks, st, open)}</Fragment>);
  if (isValidElement(n)) {
    const el = n as ReactElement<{ children?: ReactNode }>;
    if (el.props.children === undefined) return n;
    return cloneElement(el, undefined, markChildren(el.props.children, marks, st, open));
  }
  return n;
}

const offsetOf = (p: { offset?: number } | undefined): number | null => (typeof p?.offset === "number" ? p.offset : null);

/** The children as a list: an array stays, nothing becomes empty, a single node is wrapped. */
function toNodeList(children: ReactNode): ReactNode[] {
  if (isNodeArray(children)) return children;
  return children == null ? [] : [children];
}

/**
 * The inline part of a list item: the children up to the first nested block (a sublist or paragraph), and the source range of that
 * part. Only the item's own text is editable and commentable, never its nested list. Null range = no editable text of its own.
 */
function inlineOfItem(
  node: HNode | undefined,
  children: ReactNode,
): { inline: ReactNode[]; rest: ReactNode[]; range: EditingBlock | null } {
  const kids = toNodeList(children);
  const hast = node?.children ?? [];
  if (kids.length !== hast.length) return { inline: [], rest: kids, range: null };
  let cut = hast.findIndex((c) => c.type === "element" && BLOCK_TAGS.has(c.tagName ?? ""));
  if (cut < 0) cut = hast.length;
  let start: number | null = null;
  let end: number | null = null;
  for (let i = 0; i < cut; i++) {
    const c = hast[i];
    if (c.type === "text" && (c.value ?? "").trim() === "") continue;
    const s = offsetOf(c.position?.start);
    const e = offsetOf(c.position?.end);
    if (s === null || e === null) continue;
    start ??= s;
    end = e;
  }
  return { inline: kids.slice(0, cut), rest: kids.slice(cut), range: start !== null && end !== null ? { start, end } : null };
}

type Kind = "p" | "heading" | "li" | "cell";
type Props = { node?: HNode; children?: ReactNode } & Record<string, unknown>;

/** Inserts plain text at the caret of the editor, replacing the selection. */
function insertPlainText(text: string) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return;
  const range = sel.getRangeAt(0);
  range.deleteContents();
  const node = document.createTextNode(text);
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range);
}

/** The editor's text without the trailing line breaks the browser adds, and with plain spaces for no-break spaces. */
function readEditorText(el: HTMLElement | null): string {
  const raw = (el?.innerText ?? "").replace(/\xa0/g, " ");
  let end = raw.length;
  while (end > 0 && raw.charAt(end - 1) === "\n") end--;
  return raw.slice(0, end);
}

function BlockEditor({ kind, ctx }: Readonly<{ kind: Kind; ctx: DocContextValue }>) {
  const box = useRef<HTMLDivElement>(null);
  const [initialText] = useState(ctx.editText);
  // Runs once: the editor owns its own text from here on (React never renders children into the contenteditable).
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    el.textContent = initialText;
    el.focus();
    const sel = window.getSelection();
    if (sel) {
      const r = document.createRange();
      r.selectNodeContents(el);
      r.collapse(false);
      sel.removeAllRanges();
      sel.addRange(r);
    }
  }, [initialText]);
  const read = () => readEditorText(box.current);
  return (
    <div
      className="files-edit"
      data-testid="files-edit"
      data-kind={kind}
      role="presentation"
      onKeyDown={(e) => {
        // Esc cancels from the editor AND from the Save / Cancel buttons
        if (e.key === "Escape") {
          e.preventDefault();
          ctx.onCancel();
        }
      }}
    >
      <div
        ref={box}
        className="files-edit__box"
        contentEditable="plaintext-only"
        suppressContentEditableWarning
        role="textbox"
        tabIndex={0}
        aria-multiline="true"
        aria-label="Edit block"
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            ctx.onSave(read());
          }
        }}
        onPaste={(e) => {
          // plain text only: pasted rich text would put markup into the source
          e.preventDefault();
          insertPlainText(e.clipboardData.getData("text/plain"));
        }}
      />
      <p className="files-edit__hint body-sm">Editing Markdown source. Ctrl/⌘+Enter saves, Esc cancels.</p>
      {ctx.editError !== null && ctx.editError !== "" ? (
        <p className="files-edit__error body-sm" role="alert">
          {ctx.editError}
        </p>
      ) : null}
      <div className="files-edit__actions" role="group" aria-label="Edit actions">
        <Button
          variant="primary"
          size="sm"
          disabled={ctx.saving}
          onClick={() => {
            ctx.onSave(read());
          }}
        >
          Save
        </Button>
        <Button variant="glass" size="sm" disabled={ctx.saving} onClick={ctx.onCancel}>
          Cancel
        </Button>
        {ctx.onReload ? (
          <Button variant="plain" size="sm" onClick={ctx.onReload}>
            Reload
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/** The source range a block edits: none for a cell, the item's own text for a list item, else the whole node. */
function blockRange(
  kind: Kind,
  node: HNode | undefined,
  nodeStart: number,
  item: ReturnType<typeof inlineOfItem> | null,
): EditingBlock | null {
  if (kind === "cell") return null;
  if (item) return item.range;
  const end = offsetOf(node?.position?.end);
  return end === null ? null : { start: nodeStart, end };
}

/** The highlights that fall inside this block: draft marks by block id, thread marks by their quote in the block's text. */
function blockMarks(highlights: Highlight[], nodeStart: number, text: string, section: string): Mark[] {
  const marks: Mark[] = [];
  for (const h of highlights) {
    if (h.threadId === null) {
      if (h.blockId === nodeStart && h.end <= text.length) marks.push({ key: h.key, threadId: null, start: h.start, end: h.end });
    } else if (h.section === "" || h.section === section) {
      const r = locateQuote(text, h);
      if (r) marks.push({ key: h.key, threadId: h.threadId, ...r });
    }
  }
  return marks;
}

function makeBlock(Tag: keyof React.JSX.IntrinsicElements, kind: Kind): ComponentType<Props> {
  const Tagged: ElementType = Tag;
  const Block = ({ node, children, ...rest }: Props) => {
    const ctx = useContext(DocContext);
    const nodeStart = offsetOf(node?.position?.start);
    if (!ctx || nodeStart === null) return <Tagged {...rest}>{children}</Tagged>;

    const item = kind === "li" ? inlineOfItem(node, children) : null;
    const inline = item ? item.inline : children;
    const range = blockRange(kind, node, nodeStart, item);
    if (kind === "li" && !item?.range) return <Tagged {...rest}>{children}</Tagged>;

    const editingHere = range !== null && ctx.editing !== null && ctx.editing.start === range.start && ctx.editing.end === range.end;
    const text = reactText(inline);
    const section = sectionAt(ctx.headings, nodeStart);
    const ranges = disjointRanges(blockMarks(ctx.highlights, nodeStart, text, section));
    const shown = !editingHere && ranges.length > 0 ? markChildren(inline, ranges, { pos: 0 }, ctx.onOpenThread) : inline;

    const data = {
      "data-block-id": nodeStart,
      "data-own-len": text.length,
      "data-section": section,
      ...(range ? { "data-edit-start": range.start, "data-edit-end": range.end } : {}),
    };

    if (editingHere) {
      return kind === "li" ? (
        <li {...rest} {...data}>
          <BlockEditor kind={kind} ctx={ctx} />
          {item?.rest}
        </li>
      ) : (
        <div {...data}>
          <BlockEditor kind={kind} ctx={ctx} />
        </div>
      );
    }
    if (item) {
      return (
        <Tagged {...rest} {...data}>
          {shown}
          {item.rest}
        </Tagged>
      );
    }
    const extra = kind === "heading" ? { tabIndex: -1 } : {};
    return (
      <Tagged {...rest} {...extra} {...data}>
        {shown}
      </Tagged>
    );
  };
  Block.displayName = `Md.${Tag}`;
  return Block;
}

/** The props that belong on the DOM element: react-markdown's `node` is not one of them, and the children are passed on their own. */
function domPropsOf(props: Props): Record<string, unknown> {
  return Object.fromEntries(Object.entries(props).filter(([key]) => key !== "node" && key !== "children"));
}

/** Stable element overrides for MarkdownProse (identity never changes; data comes from `DocContext`). */
const BLOCKS = {
  p: makeBlock("p", "p"),
  h1: makeBlock("h1", "heading"),
  h2: makeBlock("h2", "heading"),
  h3: makeBlock("h3", "heading"),
  h4: makeBlock("h4", "heading"),
  h5: makeBlock("h5", "heading"),
  h6: makeBlock("h6", "heading"),
  li: makeBlock("li", "li"),
  table: function Table(props: Props) {
    const rest = domPropsOf(props);
    // a focusable, scrollable wrapper: wide tables scroll inside the column instead of being cut (FE-DOD-05)
    return (
      <div className="files__table" role="region" aria-label="Table" tabIndex={0}>
        <DataTable {...rest}>{props.children}</DataTable>
      </div>
    );
  },
  td: makeBlock("td", "cell"),
  th: makeBlock("th", "cell"),
};

export const BLOCK_COMPONENTS = BLOCKS as Components;
