// FE-MC-08, FE-REV-03 (PLAN a.7): pure helpers of the Files view. Node-testable: no React, no DOM.
import type { Comment, Thread } from "@/lib/domain/comments";
import type { FileTreeNode } from "@/lib/domain/files";
import type { Person } from "@/lib/domain/people";

/** PA9: the first file in tree order (depth first, folders open), or null for an empty tree. */
export function firstFilePath(nodes: FileTreeNode[]): string | null {
  for (const n of nodes) {
    if (n.type === "file" && n.path !== undefined && n.path !== "") return n.path;
    const inner = n.children ? firstFilePath(n.children) : null;
    if (inner !== null) return inner;
  }
  return null;
}

/** The chain of tree nodes from the top to the file with this path (folders first), or null. */
export function nodeChain(nodes: FileTreeNode[], path: string): FileTreeNode[] | null {
  for (const n of nodes) {
    if (n.type === "file" && n.path === path) return [n];
    const inner = n.children ? nodeChain(n.children, path) : null;
    if (inner) return [n, ...inner];
  }
  return null;
}

/** The last segment of a document path (the file name). */
export function baseName(file: string): string {
  return file.split("/").pop() ?? file;
}

export const folderIds = (nodes: FileTreeNode[]): string[] =>
  nodes.flatMap((n) => (n.type === "folder" || n.children ? [n.id, ...folderIds(n.children ?? [])] : []));

export const filePaths = (nodes: FileTreeNode[]): string[] =>
  nodes.flatMap((n) => (n.type === "file" && n.path !== undefined && n.path !== "" ? [n.path] : filePaths(n.children ?? [])));

/** "Sep 18, 10:12" in UTC, fixed so the same thread reads the same everywhere (seed times are UTC, MOCKS §8.1). */
export function commentTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const month = d.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${month} ${String(d.getUTCDate())}, ${hh}:${mm}`;
}

export interface CommentRow {
  author: string;
  time: string;
  text: string;
  kind?: "bot";
  typing?: boolean;
}

/** Comments of a thread as CommentThread rows (author names from the people list), plus the bot's typing row at the end. */
export function commentRows(comments: Comment[], people: Person[], typing: boolean): CommentRow[] {
  const nameOf = (c: Comment) => people.find((p) => p.id === c.authorId)?.name ?? (c.kind === "bot" ? "SpecBot" : "Unknown");
  const rows: CommentRow[] = comments.map((c) => ({
    author: nameOf(c),
    time: commentTime(c.createdAt),
    text: c.text,
    kind: c.kind === "bot" ? ("bot" as const) : undefined,
  }));
  if (typing) {
    const bot = people.find((p) => p.kind === "bot");
    rows.push({ author: bot?.name ?? "SpecBot", time: "", text: "", kind: "bot", typing: true });
  }
  return rows;
}

/** A highlight to draw: a saved thread (found again by its text quote) or the draft of a thread not sent yet (exact block and offsets). */
export type Highlight =
  | { key: string; threadId: string; section: string; quote: string; prefix?: string; suffix?: string }
  | { key: "draft"; threadId: null; blockId: number; start: number; end: number };

/** Open threads become highlights; resolved ones do not. */
export function threadHighlights(threads: Thread[]): Highlight[] {
  return threads
    .filter((t) => t.status === "open")
    .map((t) => ({
      key: t.id,
      threadId: t.id,
      section: t.anchor.section,
      quote: t.anchor.quote,
      prefix: t.anchor.prefix,
      suffix: t.anchor.suffix,
    }));
}
