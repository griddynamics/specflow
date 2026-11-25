// The words and rules of the Confluence link source (docs/sources/importers.md "Confluence link"), free of React: what a failed check or add
// says (the closed codes of the API), what a passed check says, and what a node says when its import stopped.
import { isDataError } from "@/lib/domain/errors";
import type { ConfluenceCheck } from "@/lib/domain/sources";
import { V2_COPY } from "../v2-copy";

/** What a Confluence node says when the API reports its import failed (`no_access`, or any other closed reason). */
export const CONFLUENCE_WORDS = {
  noAccessMeta: "Cannot read it",
  noAccess: "The Atlassian account that connected it cannot read this page or space now. Another member reconnects Atlassian.",
  failedMeta: "Import failed",
  failed: "The import failed. Add the link again.",
} as const;

const FAILURE: Record<string, string> = {
  atlassian_not_connected: "Connect Atlassian in Integrations first.",
  atlassian_reconnect_required: "The Atlassian connection needs a reconnect in Integrations.",
  atlassian_oauth_unavailable: "Atlassian is not set up on this server.",
  atlassian_site_mismatch: "This link is on another Atlassian site than the one connected to this project.",
  atlassian_unreachable: "Atlassian did not answer. Try again in a minute.",
  confluence_link_invalid: "This is not a link to a Confluence page or space.",
  confluence_short_link_unsupported: "Short links are not supported. Paste the full address of the page.",
  confluence_not_found: "Confluence has no page or space at this link, or the connected account cannot see it.",
  confluence_forbidden: "The connected account is not allowed to read this.",
  source_not_found: "The source was removed. Add the link again.",
};

/** What a failed check or add says, from the API's closed code; anything else is the general failure. */
export function confluenceFailureText(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

const plural = (n: number, one: string): string => `${String(n)} ${n === 1 ? one : one + "s"}`;

/** The line under a passed check: "Space Ordering · 42 pages", with the linked Jira issues when links are followed and the cap when it was hit. */
export function confluenceCheckText(c: ConfluenceCheck, follow: boolean): string {
  const what = c.kind === "space" ? "Space" : "Page";
  const count = plural(c.pages, "page");
  const pages = c.capped ? `${count} (the first ones; more were found)` : count;
  const issues = follow && c.kind === "page" ? `, ${plural(c.linkedIssues, "linked Jira issue")}` : "";
  return `${what} ${c.title} · ${pages}${issues}.`;
}
