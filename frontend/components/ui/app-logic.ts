// FE-DS-02, FE-DOD-05 (P05, R5): the pure parts of the app components, split out so they run without a browser.
// Ports of the helper functions in bundle.js part 3. No hooks, no DOM, server-safe.
import { matchesQuery } from "./SearchField";
import { nonEmpty, truthy } from "./utils";

/** FileTree node (index.d.ts `FileTreeNode`); re-exported from FileTree.tsx. */
export interface FileTreeNode {
  id: string;
  name: string;
  type?: "folder" | "file";
  children?: FileTreeNode[];
  badge?: number | string;
}

/** Person as the design system defines it (index.d.ts `Person`); `kind` also allows "person" so the domain `Person` fits. */
export interface Person {
  id: string;
  name: string;
  email?: string;
  role?: string;
  handle?: string;
  kind?: "bot" | "person";
  src?: string;
}

/** `fmtSize` in bundle.js: strings pass through, numbers become "n KB" or "n.n MB". */
export function formatFileSize(bytes: number | string | null | undefined): string {
  if (bytes == null) return "";
  if (typeof bytes === "string") return bytes;
  return bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${String(Math.max(1, Math.round(bytes / 1024)))} KB`;
}

/** PeoplePicker suggestions: people not yet picked whose name, email or role contains the query (case-insensitive). */
export function filterPeople(people: Person[], picked: string[], query: string): Person[] {
  return people.filter((x) => !picked.includes(x.id) && (!query || matchesQuery(`${x.name} ${x.email ?? ""} ${x.role ?? ""}`, query)));
}

/** The `@query` the caret is in, or null: `@` at the start or after whitespace, then up to 24 name characters. */
export function mentionQuery(text: string, caret: number): string | null {
  const m = /(?:^|\s)@([\w .-]{0,24})$/.exec(text.slice(0, caret));
  return m ? m[1] : null;
}

/** MentionInput suggestions for a query: handle or name starts with it, or the name contains it. At most six. */
export function filterMentions(people: Person[], query: string | null): Person[] {
  if (query == null) return [];
  return people.filter((x) => matchesQuery(nonEmpty(x.handle) ?? x.name, query, "start") || matchesQuery(x.name, query)).slice(0, 6);
}

/** Replaces the `@query` before the caret with `@<handle or name>` plus one space (none added when whitespace already follows) and returns the new text and the caret after that space. */
export function applyMention(text: string, caret: number, person: Person): { text: string; caret: number } {
  const head = text.slice(0, caret).replace(/@([\w .-]{0,24})$/, `@${nonEmpty(person.handle) ?? person.name}`);
  const tail = text.slice(caret);
  return { text: /^\s/.test(tail) ? head + tail : `${head} ${tail}`, caret: head.length + 1 };
}

export interface MentionPart {
  text: string;
  mention: boolean;
}

/** `renderMentions` in bundle.js without the JSX: splits text into plain parts and `@mention` parts (known names first, longest first). */
export function splitMentions(text: string, people: Person[] = []): MentionPart[] {
  const names = people.map((x) => nonEmpty(x.handle) ?? x.name).toSorted((a, b) => b.length - a.length);
  const out: MentionPart[] = [];
  let rest = text;
  while (rest.length) {
    const at = rest.indexOf("@");
    if (at < 0) {
      out.push({ text: rest, mention: false });
      break;
    }
    if (at > 0) out.push({ text: rest.slice(0, at), mention: false });
    const after = rest.slice(at + 1);
    let hit: string | null = names.find((n) => after.startsWith(n)) ?? null;
    if (hit === null || hit === "") hit = /^[\w.-]+/.exec(after)?.[0] ?? null;
    if (hit !== null && hit !== "") {
      out.push({ text: `@${hit}`, mention: true });
      rest = after.slice(hit.length);
    } else {
      out.push({ text: "@", mention: false });
      rest = after;
    }
  }
  return out;
}

export const SEVERITY: Record<"critical" | "high" | "medium" | "low", { bars: number; word: string }> = {
  critical: { bars: 4, word: "Critical" },
  high: { bars: 3, word: "High" },
  medium: { bars: 2, word: "Medium" },
  low: { bars: 1, word: "Low" },
};

/** Toolbar anchor relative to the container, like `useTextSelection` in bundle.js (adds the container scroll). */
export function selectionAnchor(
  rect: { left: number; top: number; width: number; bottom: number },
  container: { left: number; top: number },
  scroll: { left: number; top: number },
): { x: number; y: number; bottom: number } {
  return {
    x: rect.left - container.left + rect.width / 2 + scroll.left,
    y: rect.top - container.top + scroll.top,
    bottom: rect.bottom - container.top + scroll.top,
  };
}

/** The open folders of a FileTree by node id (a missing id is closed). */
export type ExpandedMap = Partial<Record<string, boolean>>;

export const isFolder = (n: FileTreeNode): boolean => n.type === "folder" || !!n.children;

/** Rows of a FileTree that are on screen, in order, with their depth and parent id. */
export function visibleTreeRows(
  nodes: FileTreeNode[],
  expanded: ExpandedMap,
): { node: FileTreeNode; depth: number; parent: string | null }[] {
  const out: { node: FileTreeNode; depth: number; parent: string | null }[] = [];
  const walk = (list: FileTreeNode[], depth: number, parent: string | null) => {
    for (const node of list) {
      out.push({ node, depth, parent });
      if (isFolder(node) && expanded[node.id] === true) walk(node.children ?? [], depth + 1, node.id);
    }
  };
  walk(nodes, 0, null);
  return out;
}

export type TreeMove = { focus: string } | { toggle: string } | null;

/** ArrowRight on a folder: open it, or move to its first child when it is open. */
function openOrEnter(rows: ReturnType<typeof visibleTreeRows>, i: number, expanded: ExpandedMap): TreeMove {
  const row = rows[i];
  if (expanded[row.node.id] !== true) return { toggle: row.node.id };
  return truthy(rows[i + 1]) && rows[i + 1].parent === row.node.id ? { focus: rows[i + 1].node.id } : null;
}

/** ArrowLeft: close an open folder, else move to the parent. */
function closeOrLeave(row: ReturnType<typeof visibleTreeRows>[number], folder: boolean, expanded: ExpandedMap): TreeMove {
  if (folder && expanded[row.node.id] === true) return { toggle: row.node.id };
  return truthy(row.parent) ? { focus: row.parent } : null;
}

/** Arrow-key navigation of the tree (WAI-ARIA tree pattern): Up/Down/Home/End move, Right opens or enters, Left closes or leaves. */
export function treeKeyMove(key: string, rows: ReturnType<typeof visibleTreeRows>, currentId: string, expanded: ExpandedMap): TreeMove {
  const i = rows.findIndex((r) => r.node.id === currentId);
  if (i < 0) return null;
  const row = rows[i];
  const folder = isFolder(row.node);
  switch (key) {
    case "ArrowDown":
      return i + 1 < rows.length ? { focus: rows[i + 1].node.id } : null;
    case "ArrowUp":
      return i > 0 ? { focus: rows[i - 1].node.id } : null;
    case "Home":
      return { focus: rows[0].node.id };
    case "End":
      return { focus: rows[rows.length - 1].node.id };
    case "ArrowRight":
      return folder ? openOrEnter(rows, i, expanded) : null;
    case "ArrowLeft":
      return closeOrLeave(row, folder, expanded);
    default:
      return null;
  }
}

/** Letters of OptionCards: A, B, C, ... */
export const OPTION_LETTERS = "ABCDEFGH";

/** Wrap-around arrow navigation of a radio group: the index to move to, or null for other keys. */
export function radioMove(key: string, index: number, count: number): number | null {
  if (key === "ArrowDown" || key === "ArrowRight") return (index + 1) % count;
  if (key === "ArrowUp" || key === "ArrowLeft") return (index - 1 + count) % count;
  return null;
}

/** First letters of a user's name for the Sidebar footer avatar (max two), like bundle.js. */
export function sidebarInitials(name: string): string {
  return (name || "?")
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2);
}
