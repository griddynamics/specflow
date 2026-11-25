// The words and rules of the Notion plan destination (docs/notion/{plan,codes}.md), free of React: what a failed check or save says (the closed
// codes of the API), what a passed check says, what a node says when its last pass ended on a code, and the count line of a finished pass.
import { DataError, isDataError } from "@/lib/domain/errors";
import type { NotionPlanAccess, NotionPlanCounts } from "@/lib/domain/notion-plan";
import { V2_COPY } from "../v2-copy";

/** The words this destination adds to `V2_COPY` and `CANVAS_COPY` (both are frozen). */
export const NOTION_PLAN_WORDS = {
  parentHint: "The link of a page you shared with Graphico in Notion.",
  checkedHint: "The plan database is created under it.",
  more: "More rows follow in the next pass.",
  failedRows: "Notion refused some rows.",
} as const;

const FAILURE: Record<string, string> = {
  notion_not_connected: "Connect Notion in Integrations first.",
  notion_reconnect_required: "The Notion connection needs a reconnect in Integrations.",
  notion_unavailable: V2_COPY.notionPlanUnavailable,
  notion_unreachable: "Notion did not answer. Try again in a minute.",
  notion_auth_failed: "Notion refused the request of this server.",
  notion_rate_limited: "Notion is limiting requests just now. Try again in a minute.",
  notion_link_invalid: "This is not a link to a Notion page.",
  notion_not_shared: "Notion has no page at this link, or you did not share it with Graphico when you connected Notion.",
  notion_parent_not_a_page: "This is a link to a database. Use the link of a page; the plan database is created under it.",
  notion_permissions_missing: "The Notion connection may not add or change content. Reconnect Notion and allow it.",
};

/** Codes a pass ends on that are not a failure of the connection: the pass waits for the plan or was overtaken, or one row or page is lost. */
const PASS_TEXT: Record<string, string> = {
  notion_page_gone: "A row was deleted on Notion. It is not made again.",
  notion_database_gone: "The plan database was deleted on Notion. Set the destination up again.",
  notion_item_invalid: "Notion did not accept a row of the plan.",
  notion_plan_not_configured: "The destination is not set up.",
  no_plan: "Waits for the plan",
  plan_applying: "The plan is being applied",
  target_changed: "The destination changed; the next pass uses the new one",
  interrupted: "The last pass was interrupted. The next one continues.",
  unexpected_error: "The last pass failed for a reason Graphico does not know. The next pass tries again.",
};

const text = (map: Record<string, string>, code: string): string | undefined => (Object.hasOwn(map, code) ? map[code] : undefined);

/** What a failed check or save says, from the API's closed code; anything else is the general failure. */
export function notionPlanFailureText(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  return text(FAILURE, code) ?? V2_COPY.actionFailed;
}

/** What a node says of the code its last pass ended on (`sync.error_code`). */
export function notionPassText(code: string): string {
  return text(PASS_TEXT, code) ?? text(FAILURE, code) ?? V2_COPY.actionFailed;
}

/** The codes that need the connection of the member who connected Notion, not a retry: the node offers Reconnect Notion. */
export const NOTION_RECONNECT = new Set(["notion_not_connected", "notion_reconnect_required"]);

/** A failed access check as the refusal `useCheckAdd` shows: the reason is the API code the words are found by. */
export function notionAccessOutcome(access: NotionPlanAccess): { parentPageTitle: string } {
  if (access.result === "ok") return { parentPageTitle: access.parentPageTitle };
  throw new DataError("validation", false, { apiCode: access.reason });
}

/** The line under a passed check. */
export const notionPlanCheckText = (found: { parentPageTitle: string }): string =>
  `Page “${found.parentPageTitle}”. ${NOTION_PLAN_WORDS.checkedHint}`;

const rows = (n: number, verb: string): string => `${String(n)} ${n === 1 ? "row" : "rows"} ${verb}`;

/** What a finished pass did, for the node's second line: rows made and rows updated; "In sync" when it changed nothing. */
export function notionCountsText(counts: NotionPlanCounts): string {
  const { created, updated } = counts;
  if (created > 0 && updated > 0) return `${rows(created, "created")} · ${String(updated)} updated`;
  if (created > 0) return rows(created, "created");
  if (updated > 0) return rows(updated, "updated");
  return V2_COPY.notionPlanSynced;
}

/** The sentence of a finished pass in the node's dialog: what could not be done, and that the rest follows. */
export function notionPassNote(status: string, counts: NotionPlanCounts | null): string {
  const parts: string[] = [];
  if (status === "partial") parts.push(V2_COPY.notionPlanPartial);
  if (counts !== null && counts.failed > 0) parts.push(NOTION_PLAN_WORDS.failedRows);
  if (counts !== null && counts.deferred > 0) parts.push(NOTION_PLAN_WORDS.more);
  return parts.join(" ");
}
