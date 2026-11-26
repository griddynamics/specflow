// The words and rules of the Linear project source (docs/sources/importers.md "Linear"), free of React: what a failed check or add says (the
// closed codes of the API), what a passed check says, what the issue list says it will take, and what a node says when its import stopped.
import { isDataError } from "@/lib/domain/errors";
import type { LinearSourceCheck, LinearSourceItem } from "@/lib/domain/linear-source";
import { V2_COPY } from "../v2-copy";
import { pickedCount, type Pickable, type Unchecked } from "./pick-logic";

/** What a Linear project node says when the API reports its import failed (`no_access`, or any other closed reason). */
export const LINEAR_SOURCE_WORDS = {
  noAccessMeta: "Cannot read it",
  noAccess: "The Linear account that connected it cannot read this project now. Another member reconnects Linear.",
  failedMeta: "Import failed",
  failed: "The import failed. Add the project again.",
} as const;

/** The words of the form: its field, the list of issues and what it says about the cap. */
export const LINEAR_SOURCE_COPY = {
  field: "Linear project, issue link or identifier",
  hint: "A project is read with its issues and documents, an issue with its sub-issues, with their comments, once when it is added.",
  issues: "Issues to add",
  selectAll: "Select all",
  selectNone: "Select none",
  capped: "Only the first 200 issues are listed and imported.",
} as const;

const FAILURE: Record<string, string> = {
  linear_unavailable: "Linear is not set up on this server.",
  linear_not_connected: "Connect Linear in Integrations first.",
  linear_reconnect_required: "The Linear connection needs a reconnect in Integrations.",
  linear_link_invalid: "This is not a Linear project or issue link, or an identifier such as P-ENG-1 or ENG-12.",
  linear_not_found: "Linear has no such project or issue, or the connected account cannot see it.",
  linear_forbidden: "The connected account is not allowed to read this in Linear.",
  linear_rate_limited: "Linear is limiting requests. Try again in a minute.",
  linear_unreachable: "Linear did not answer. Try again in a minute.",
  source_not_found: "The source was removed. Add the project again.",
};

/** What a failed check or add says, from the API's closed code; anything else is the general failure. */
export function linearSourceFailureText(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

const plural = (n: number, one: string): string => [String(n), n === 1 ? one : one + "s"].join(" ");

/** The line under a passed check: "Checkout · 18 issues · 2 documents." or "GRA-12 Card flow · 3 sub-issues." The cap is said once, in the list's note. */
export function linearSourceCheckText(c: LinearSourceCheck): string {
  const parts = [c.name, plural(c.issues, c.kind === "issue" ? "sub-issue" : "issue")];
  if (c.documents > 0) parts.push(plural(c.documents, "document"));
  return `${parts.join(" · ")}.`;
}

/** What an Add takes besides the listed issues: the issue of an issue source, or the project's documents; `null` when there is none. */
function alwaysTaken(c: LinearSourceCheck): string | null {
  if (c.kind === "issue") return "the issue";
  return c.documents > 0 ? plural(c.documents, "document") : null;
}

/** What an Add would take: "12 of 14 issues, and 2 documents", "Only 2 documents", or for an issue "3 of 5 sub-issues, and the issue". */
export function linearSelectionText(c: LinearSourceCheck, unchecked: Unchecked): string {
  const picked = pickedCount(c.items.map(idOfItem), unchecked);
  const extra = alwaysTaken(c);
  if (picked === 0) return extra === null ? "Nothing to add" : `Only ${extra}`;
  const listed = `${String(picked)} of ${plural(c.items.length, c.kind === "issue" ? "sub-issue" : "issue")}`;
  return extra === null ? listed : `${listed}, and ${extra}`;
}

/** An issue is picked by its identifier. */
export const idOfItem = (item: LinearSourceItem): Pickable => ({ id: item.identifier });

/** Whether an Add has anything to take: a project with nothing in it, or everything left out and no documents, cannot be added. */
export function linearAddable(c: LinearSourceCheck, unchecked: Unchecked): boolean {
  return alwaysTaken(c) !== null || pickedCount(c.items.map(idOfItem), unchecked) > 0;
}
