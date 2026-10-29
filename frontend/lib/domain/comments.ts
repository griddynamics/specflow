// FE-DATA-01 (TECH-SPECS §2.8).
import type { DocRef, Unsubscribe } from "./common";

/** Text-quote anchor: find `quote` inside the section whose heading slug is `section`. */
export interface TextAnchor {
  path: string;
  section: string;
  quote: string;
  prefix?: string;
  suffix?: string;
}
export interface Comment {
  id: string;
  threadId: string;
  authorId: string;
  kind: "person" | "bot";
  text: string;
  createdAt: string; // seed: MOCKS §8.1 times written as UTC, e.g. "2026-09-18T10:12:00Z"
  sources?: DocRef[]; // bot replies (MOCKS §8.2 "Sources line")
}
export interface Thread {
  id: string;
  anchor: TextAnchor;
  status: "open" | "resolved";
  comments: Comment[];
}
export type ThreadEvent = { type: "thread.changed"; threadId: string } | { type: "bot.typing"; threadId: string; typing: boolean };

export interface CommentsPort {
  list(projectId: string, path: string): Promise<Thread[]>;
  create(projectId: string, anchor: TextAnchor, text: string): Promise<Thread>;
  reply(projectId: string, threadId: string, text: string): Promise<Comment>;
  resolve(projectId: string, threadId: string): Promise<void>;
  subscribe(projectId: string, listener: (e: ThreadEvent) => void): Unsubscribe;
}
