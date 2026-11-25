// The words and rules of the Jira epic source (docs/sources/importers.md "Jira epic"), free of React: what a failed check or add says (the
// closed codes of the API), what a passed check says, and what a node says when its import stopped.
import { isDataError } from "@/lib/domain/errors";
import { EPIC_ISSUE_CAP, type JiraEpicCheck } from "@/lib/domain/sources";
import { V2_COPY } from "../v2-copy";

const FAILURE: Record<string, string> = {
  atlassian_not_connected: "Connect Atlassian in Integrations first.",
  atlassian_reconnect_required: "The Atlassian connection needs a reconnect in Integrations.",
  atlassian_oauth_unavailable: "Atlassian is not set up on this server.",
  atlassian_site_mismatch: "This link is on another Atlassian site than the one connected to this project.",
  atlassian_unreachable: "Atlassian did not answer. Try again in a minute.",
  jira_epic_link_invalid: "This is not an epic key (such as ABC-12) or a link to one.",
  jira_epic_not_an_epic: "This issue is not an epic. Give the key of an epic.",
  jira_epic_not_found: "Jira has no issue with this key, or the connected account cannot see it.",
  jira_epic_forbidden: "The connected account is not allowed to read this epic.",
  source_not_found: "The source was removed. Add the epic again.",
};

/** What a failed check or add says, from the API's closed code; anything else is the general failure. */
export function jiraEpicFailureText(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

const plural = (n: number): string => `${String(n)} ${n === 1 ? "issue" : "issues"}`;

/** The line under a passed check: "NWO-12 · Checkout redesign · 18 issues", with the cap when the epic has more than an import takes. */
export function jiraEpicCheckText(c: JiraEpicCheck): string {
  const issues = c.issues > EPIC_ISSUE_CAP ? `${plural(c.issues)} (the first ${String(EPIC_ISSUE_CAP)} are read)` : plural(c.issues);
  return `${c.key} · ${c.summary} · ${issues}.`;
}

/** What a Jira epic node says when the API reports its import failed (`no_access`, or any other closed reason). */
export const JIRA_EPIC_WORDS = {
  noAccessMeta: "Cannot read it",
  noAccess: "The Atlassian account that connected it cannot read this epic now. Another member reconnects Atlassian.",
  failedMeta: "Import failed",
  failed: "The import failed. Add the epic again.",
} as const;
