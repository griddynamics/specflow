// MCP-TOOLING session 10: the pure parts of the findings list and the claims board: labels, dates, safe links, the error words.
import { isDataError } from "@/lib/domain/errors";
import type { Claim, CloseAction, Finding, FindingFilter, FindingKind, FindingRef, Person } from "@/lib/domain/work";

export const KIND_LABELS: Record<FindingKind, string> = { gap: "Gap", conflict: "Conflict", question: "Question", note: "Note" };

export const FILTER_OPTIONS: { value: FindingFilter; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "resolved", label: "Resolved" },
  { value: "dismissed", label: "Dismissed" },
];

export const NOTE_LIMIT = 500;

/** A person's name, "you" for the signed-in person, and a plain word when the name is not known (a member who left, or an address). */
export function personName(p: Person): string {
  if (p.you) return p.name === null ? "You" : `${p.name} (you)`;
  return p.name ?? "A project member";
}

const DATE = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });
export const formatWhen = (iso: string): string => {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? iso : DATE.format(at);
};

/** A claim older than this many days is shown as old: the board exists to find the forgotten ones. */
export const STALE_DAYS = 7;
export const isStale = (iso: string, now: number): boolean => now - new Date(iso).getTime() > STALE_DAYS * 86_400_000;

/** "5 min", "3 h", "2 days": how long ago, for the "since" column; the exact time stays in the title. */
export function age(iso: string, now: number): string {
  const ms = now - new Date(iso).getTime();
  if (Number.isNaN(ms) || ms < 60_000) return "just now";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `${String(minutes)} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${String(hours)} h`;
  return `${String(Math.floor(hours / 24))} days`;
}

/** An external reference as a link when its value is an http(s) URL without credentials (a pull request may be a number); anything else is text. */
export function refHref(ref: FindingRef): string | null {
  if (ref.type !== "url" && ref.type !== "pr") return null;
  try {
    const url = new URL(ref.value);
    const web = url.protocol === "https:" || url.protocol === "http:";
    return web && url.username === "" && url.password === "" ? url.href : null;
  } catch {
    return null;
  }
}

export const REF_LABELS: Record<FindingRef["type"], string> = { pr: "Pull request", commit: "Commit", file: "File", url: "Link" };

export const taskCount = (n: number): string => `${String(n)} ${n === 1 ? "task" : "tasks"}`;

/** What the findings of an empty list say, per filter. */
export const EMPTY_FINDINGS: Record<FindingFilter, string> = {
  open: "No open findings",
  resolved: "No resolved findings",
  dismissed: "No dismissed findings",
  all: "No findings",
};

/** The line under the empty title, per filter. */
export const EMPTY_FINDINGS_HELP: Record<FindingFilter, string> = {
  open: "Agents add findings when the spec leaves a question open. They show here for you to answer.",
  resolved: "Resolved findings will show here.",
  dismissed: "Dismissed findings will show here.",
  all: "Findings agents report will show here.",
};

/** The title of the story an open blocking finding holds back; null when it blocks none, or the title is not known. */
export function blockedStory(f: Finding): string | null {
  return f.blocking && f.status === "open" ? f.storyTitle : null;
}

/** The words of a failed close, by the API code (never server text). */
export function closeError(e: unknown): { text: string; refresh: boolean } {
  const api = isDataError(e) ? e.detail?.apiCode : undefined;
  if (api === "finding_closed") return { text: "Someone has already answered this finding. The list is up to date now.", refresh: true };
  if (api === "finding_not_found") return { text: "This finding no longer exists. The list is up to date now.", refresh: true };
  if (api === "plan_applying") return { text: "The plan is being updated. Try again in a moment.", refresh: false };
  return { text: "The finding could not be saved. Check your connection and try again.", refresh: false };
}

export function releaseError(e: unknown): { text: string; refresh: boolean } {
  const api = isDataError(e) ? e.detail?.apiCode : undefined;
  if (api === "story_not_found" || api === "no_plan")
    return { text: "This story is not in the plan any more. The board is up to date now.", refresh: true };
  return { text: "The claim could not be released. Check your connection and try again.", refresh: false };
}

export const CLOSE_COPY: Record<CloseAction, { title: string; button: string; done: string; help: string }> = {
  resolve: {
    title: "Resolve finding",
    button: "Resolve",
    done: "Finding resolved",
    help: "Resolve: the question is answered or the gap is filled. Say how; the agent can read the note.",
  },
  dismiss: {
    title: "Dismiss finding",
    button: "Dismiss",
    done: "Finding dismissed",
    help: "Dismiss: it needs no change (not a problem, or out of scope). Say why; the agent can read the note.",
  },
};

export const isStoryClaim = (c: Claim): boolean => c.kind === "story";
