// FE-DATA-01 (TECH-SPECS §2.7). FileTreeNode follows the design-system type plus the document path on file nodes.
import type { Unsubscribe } from "./common";

export interface FileTreeNode {
  id: string;
  name: string;
  type: "folder" | "file";
  path?: string;
  children?: FileTreeNode[];
  badge?: number | string;
}
export type DocStatus = "Draft" | "In review" | "Approved"; // MOCKS §3.1
export interface ProjectDocument {
  path: string;
  title: string;
  body: string; // Markdown source
  version: number; // +1 on every saved edit
  ownerId?: string;
  contributorIds?: string[];
  status?: DocStatus;
  updatedAt?: string;
  uploaded?: true; // added by "Add files": body is empty; the view shows the note instead
}
/** One Markdown block as source offsets (from the mdast/hast `position` that react-markdown passes as `node`). */
export interface BlockRef {
  start: number;
  end: number;
}

export interface FilesPort {
  tree(projectId: string): Promise<FileTreeNode[]>;
  get(projectId: string, path: string): Promise<ProjectDocument>; // rejects not_found
  readme(projectId: string): Promise<ProjectDocument>; // MOCKS DOC-00
  updateBlock(projectId: string, path: string, block: BlockRef, markdown: string, baseVersion: number): Promise<ProjectDocument>; // conflict if baseVersion is stale
  addFiles(projectId: string, files: File[]): Promise<void>; // "Add files" (O8, PA21): file nodes under "Uploads"; no processing
  subscribe(projectId: string, listener: (path: string) => void): Unsubscribe;
}
