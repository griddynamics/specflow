// The words and rules of the Notion link source (docs/notion/source.md), free of React: what a failed check or add says (the closed codes of
// the API), what a passed check says, and what a node says when its import stopped. Stream D2 owns this file and refines the words.
import { isDataError } from "@/lib/domain/errors";
import type { NotionCheck } from "@/lib/domain/sources";
import { V2_COPY } from "../v2-copy";

/** What a Notion node says when the API reports its import failed (`no_access`, or any other closed reason). */
export const NOTION_WORDS = {
  noAccessMeta: "Cannot read it",
  noAccess: "The Notion account that connected it cannot read this page or database now. Another member reconnects Notion.",
  failedMeta: "Import failed",
  failed: "The import failed. Add the link again.",
} as const;

const FAILURE: Record<string, string> = {
  notion_not_connected: "Connect Notion in Integrations first.",
  notion_reconnect_required: "The Notion connection needs a reconnect in Integrations.",
  notion_unavailable: "Notion is not set up on this server.",
  notion_unreachable: "Notion did not answer. Try again in a minute.",
  notion_auth_failed: "Notion refused the request of this server.",
  notion_link_invalid: "This is not a link to a Notion page or database.",
  notion_not_shared: "Notion has no page or database at this link, or you did not share it with Graphico when you connected Notion.",
  source_not_found: "The source was removed. Add the link again.",
};

/** What a failed check or add says, from the API's closed code; anything else is the general failure. */
export function notionFailureText(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

const plural = (n: number, one: string): string => `${String(n)} ${n === 1 ? one : one + "s"}`;

/** The line under a passed check: "Page Engineering Wiki · 4 pages", with the cap when it was hit. */
export function notionCheckText(c: NotionCheck): string {
  const what = c.kind === "database" ? "Database" : "Page";
  const count = plural(c.pages, "page");
  const shown = c.capped ? `${count} (the first ones; more were found)` : count;
  return `${what} ${c.title} · ${shown}.`;
}
