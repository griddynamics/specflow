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
  notion_unavailable: V2_COPY.notionPlanUnavailable,
  notion_unreachable: "Notion did not answer. Try again in a minute.",
  notion_auth_failed: "Notion refused this server's request. Ask your admin to check the Notion setup.",
  notion_rate_limited: "Notion is busy right now. Try again in a minute.",
  notion_link_invalid: "This is not a link to a Notion page or database.",
  notion_not_shared: "No page or database at this link. Share it with Graphico when you connect Notion.",
  source_not_found: "The source was removed. Add the link again.",
};

/** What a failed check or add says, from the API's closed code; anything else is the general failure. */
export function notionFailureText(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  // A link over the length cap is a 422 `validation_failed` (the field is capped at 2048 too): it is not a link.
  if (isDataError(e) && e.code === "validation" && code === "validation_failed") return FAILURE.notion_link_invalid;
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

const plural = (n: number, one: string): string => `${String(n)} ${n === 1 ? one : one + "s"}`;

/**
 * The line under a passed check. A page counts itself and its sub-pages (`pages` includes the page): "Page Engineering Wiki · this page and
 * 3 sub-pages." A database counts its rows: "Database Requirements · 42 rows." When the cap was hit, it says the first ones are read.
 */
export function notionCheckText(c: NotionCheck): string {
  if (c.kind === "database") {
    const rows = c.capped ? `the first ${plural(c.pages, "row")} (more were found)` : plural(c.pages, "row");
    return `Database ${c.title} · ${rows}.`;
  }
  const subs = Math.max(c.pages - 1, 0);
  let what = "this page, no sub-pages";
  if (subs > 0) what = `this page and ${c.capped ? "the first " : ""}${plural(subs, "sub-page")}${c.capped ? " (more were found)" : ""}`;
  return `Page ${c.title} · ${what}.`;
}
