// FR-GWEB-04, FR-GWEB-06 (docs/graph/gap/api.md "User interface", answers.md "The question card", "Answering", "Applying answers",
// "Placement rules", "Review list", "Dismissing", "Who answers"; detection.md "Relevance filter"): pure helpers of the Gaps screen and the
// Plan's gap links. No React, no fetch: the words of kinds, severities, origins, statuses (Open, Staged, Resolved), assignees, noise
// sections, placements, history events and failures, the filters' options and the `list_gaps` query they make, the inbox order, which
// gaps the latest run found ("New"), the card's answer button and the "Resolve gaps" button, the section noise-rule value, and which open
// gaps belong to a plan task or story.
import type { SeverityLevel } from "@/components/ui/SeverityBadge";
import { isDataError } from "@/lib/domain/errors";
import {
  GAP_KINDS,
  GAP_STATUSES,
  type AnswerResult,
  type ApplyResult,
  type ClarificationStatus,
  type EditPlacement,
  type GapEvent,
  GAP_SEVERITIES,
  type Gap,
  type GapCheck,
  type GapClarification,
  type GapKind,
  type GapOrigin,
  type GapQuery,
  type GapQuote,
  type GapSeverity,
  type GapStatus,
  type NoiseSection,
} from "@/lib/domain/gaps";
import type { Member } from "@/lib/domain/members";

export const KIND_LABEL: Record<GapKind, string> = {
  missing: "Missing",
  ambiguous: "Ambiguous",
  contradiction: "Contradiction",
  inconsistency: "Inconsistency",
  open_question: "Open question",
  assumption: "Assumption",
};

export const SEVERITY_LABEL: Record<GapSeverity, string> = { blocker: "Blocker", major: "Major", minor: "Minor" };

/** The SeverityBadge level of a gap severity: a blocker carries the Critical icon; the word is the gap's own. */
export const SEVERITY_LEVEL: Record<GapSeverity, SeverityLevel> = { blocker: "critical", major: "high", minor: "low" };

/** Who raised the gap (answers.md "The question card", Chips). */
export const ORIGIN_LABEL: Record<GapOrigin, string> = {
  analysis: "Found by analysis",
  development: "Reported by a coding agent",
  person: "Raised by a person",
};

/** A gap's status in words: an answered gap is Staged (its answer waits for "Resolve gaps"), and so is one whose rebuild runs
 * (`applying`, shown with the "Resolving…" tag). */
export const STATUS_LABEL: Record<GapStatus, string> = { open: "Open", answered: "Staged", applying: "Staged", resolved: "Resolved" };

/** The tag of a gap whose rebuild runs (`applying`), beside its spinner. */
export const RESOLVING_LABEL = "Resolving…";

/** The tag of a gap the latest gap run found. */
export const NEW_LABEL = "New";

const STATUS_FILTERS = ["open", "staged", "resolved", "all"] as const;
export type StatusFilter = (typeof STATUS_FILTERS)[number];

/** The stored statuses each Status filter value asks `list_gaps` for. */
export const STATUS_QUERY: Readonly<Record<StatusFilter, GapStatus[]>> = {
  open: ["open"],
  staged: ["answered", "applying"],
  resolved: ["resolved"],
  all: [...GAP_STATUSES],
};

/** The inbox filters as the screen holds them; "all" sends no parameter (the status always sends its stored statuses). */
export interface GapFilters {
  status: StatusFilter;
  kind: GapKind | "all";
  severity: GapSeverity | "all";
  story: string; // a story id, or "all"
  assigned: string; // "none", a member's user id, or "all"
}

export const DEFAULT_GAP_FILTERS: GapFilters = {
  status: "open",
  kind: "all",
  severity: "all",
  story: "all",
  assigned: "all",
};

/** The `assigned_to` value of the Assigned filter and the assignee menu for nobody (besides a member's user id). */
export const ASSIGNED_TO_NOBODY = "none";

type Options = { value: string; label: string }[];
const ALL = { value: "all", label: "All" };

export const STATUS_OPTIONS: Options = [
  { value: "open", label: "Open" },
  { value: "staged", label: "Staged" },
  { value: "resolved", label: "Resolved" },
  ALL,
];
export const KIND_OPTIONS: Options = [ALL, ...Object.entries(KIND_LABEL).map(([value, label]) => ({ value, label }))];
export const SEVERITY_OPTIONS: Options = [ALL, ...Object.entries(SEVERITY_LABEL).map(([value, label]) => ({ value, label }))];

/** A member as the Gaps screen names them: the display name, else the email. */
export const memberName = (m: Member): string => m.name ?? m.email;

/** A member as a choice: the name, the caller marked "(you)". */
const memberChoice = (m: Member) => ({ value: m.userId, label: m.isYou ? `${memberName(m)} (you)` : memberName(m) });

/** The assignee of a gap in words: the member's name, "Unassigned", or "A former member" for an id the members list does not hold. */
export function assigneeText(userId: string | null, members: readonly Member[]): string {
  if (userId === null) return "Unassigned";
  const m = members.find((x) => x.userId === userId);
  return m === undefined ? "A former member" : memberName(m);
}

/** The Assigned filter: All, Unassigned, then each member of the project (the caller marked "(you)", as in the assignee menu). */
export function assignedOptions(members: readonly Member[]): Options {
  return [ALL, { value: ASSIGNED_TO_NOBODY, label: "Unassigned" }, ...members.map(memberChoice)];
}

/** The assignee menu's choices: Unassigned, then each member (the caller marked "(you)"); a current assignee the list does not hold
 * stays a choice, so the menu still marks it. */
export function assigneeChoices(members: readonly Member[], current: string | null): Options {
  const choices = [{ value: ASSIGNED_TO_NOBODY, label: "Unassigned" }, ...members.map(memberChoice)];
  if (current !== null && !members.some((m) => m.userId === current)) choices.push({ value: current, label: "A former member" });
  return choices;
}

/** The names of the members by user id, for the history's words. */
export function memberNames(members: readonly Member[]): Readonly<Record<string, string>> {
  return Object.fromEntries(members.map((m) => [m.userId, memberName(m)]));
}

/** "All" first, then the given items in their order. */
export function withAll(items: { id: string; name: string }[]): Options {
  return [ALL, ...items.map((i) => ({ value: i.id, label: i.name }))];
}

/** A value from a filter control; anything outside the vocabulary means the default. */
export function patchOf(key: keyof GapFilters, v: string): Partial<GapFilters> {
  if (key === "status") return { status: STATUS_FILTERS.find((x) => x === v) ?? "open" };
  if (key === "kind") return { kind: GAP_KINDS.find((x) => x === v) ?? "all" };
  if (key === "severity") return { severity: GAP_SEVERITIES.find((x) => x === v) ?? "all" };
  const value = v === "" ? "all" : v;
  return key === "story" ? { story: value } : { assigned: value };
}

/**
 * The `list_gaps` query of the inbox: route `ask` (the inbox shows the questions for people; AI answers are not in it), the stored
 * statuses of the Status filter (Staged is `answered` and `applying`) and the other chosen filters.
 */
export function gapQuery(f: GapFilters): GapQuery {
  const q: GapQuery = { route: ["ask"], status: STATUS_QUERY[f.status] };
  if (f.kind !== "all") q.kind = [f.kind];
  if (f.severity !== "all") q.severity = [f.severity];
  if (f.story !== "all") q.storyId = f.story;
  if (f.assigned !== "all") q.assignedTo = f.assigned;
  return q;
}

/** A development gap that holds a coding agent's task: the inbox lists it first, and its answer is applied at once (with every other
 * staged answer of the project). */
export const holdsAgent = (g: Gap): boolean => g.origin === "development" && g.blocking;

/** The inbox order (api.md "User interface"): development gaps that hold tasks first, then the server's order (severity, reach, newest). */
export function inboxOrder(gaps: Gap[]): Gap[] {
  return gaps.toSorted((a, b) => Number(holdsAgent(b)) - Number(holdsAgent(a)));
}

/** The row's second line: the question, after the status when the gap is not open. */
export function rowSubtitle(g: Gap): string {
  return g.status === "open" ? g.question : `${STATUS_LABEL[g.status]} · ${g.question}`;
}

/** The count line announced to assistive technology (aria-live). */
export function countText(shown: number, more: boolean): string {
  const n = `${String(shown)}${more ? "+" : ""}`;
  return `${n} ${shown === 1 && !more ? "gap" : "gaps"}`;
}

/**
 * A gap the latest gap run found (FR-GRTE-06): a run's own gap (`analysis`, not one a person or a coding agent raised) created at or after
 * that run started. It stays New after the run ends, until the next run starts; before any run nothing is New.
 */
export function isNewGap(g: Gap, check: GapCheck | null): boolean {
  if (check?.startedAt === null || check?.startedAt === undefined || g.origin !== "analysis") return false;
  const started = Date.parse(check.startedAt);
  const created = Date.parse(g.createdAt);
  return Number.isFinite(started) && Number.isFinite(created) && created >= started;
}

/** The running check's line about the New rows the inbox lists: "2 new gaps listed so far". */
export const newListedText = (n: number): string => `${String(n)} new ${n === 1 ? "gap" : "gaps"} listed so far`;

/**
 * Where a witness line is: "<document title or path> › <section path> · line N" when the gap read named its document, else its fragment
 * and lines ("FRAG::x · lines 12–14", "FRAG::x · line 12").
 */
export function quoteSource(q: GapQuote): string {
  const doc = nonEmptyText(q.documentTitle) ?? nonEmptyText(q.documentPath);
  // A heading path starts with the document's own title heading when it has one: name the document once.
  const sections = (q.headingPath ?? []).filter((h, i) => !(i === 0 && h === doc));
  const where = doc === undefined ? q.fragmentId : [doc, ...sections].join(" › ");
  if (q.lineStart === null) return where;
  const lines =
    q.lineEnd === null || q.lineEnd === q.lineStart ? `line ${String(q.lineStart)}` : `lines ${String(q.lineStart)}–${String(q.lineEnd)}`;
  return `${where} · ${lines}`;
}

const nonEmptyText = (v: string | null | undefined): string | undefined => (v === null || v === undefined || v === "" ? undefined : v);

/** The recorded answer of an answered gap, in words. */
export function clarificationText(c: GapClarification): string {
  if (c.mode === "option" || (c.mode === "ai" && c.optionLabel !== null)) return c.optionLabel ?? c.text ?? "";
  return c.text ?? c.optionLabel ?? "";
}

export const RELEVANCE_LABEL: Record<NoiseSection["relevance"], string> = { spec: "Spec", tracking: "Tracking", mixed: "Mixed" };
export const DECIDED_BY_LABEL: Record<NoiseSection["by"], string> = { rules: "By rules", model: "By the model", person: "By a person" };

/** "Meeting notes › 2026-03-02 sync"; a section without a heading reads as its document's top. */
export function headingText(s: NoiseSection): string {
  return s.headingPath.length === 0 ? "Top of the document" : s.headingPath.join(" › ");
}

export function removedText(n: number): string {
  if (n === 0) return "Removed no nodes";
  return `Removed ${String(n)} ${n === 1 ? "node" : "nodes"} from the checks`;
}

/** A failed gap read: "unavailable" when gap runs are not set up on the server (503 gap_unavailable), else "failed" (retry). */
export function failureOf(e: unknown): "unavailable" | "failed" {
  return isDataError(e) && e.detail?.apiCode === "gap_unavailable" ? "unavailable" : "failed";
}

// ---- answering ----

export const PLACEMENT_LABEL: Record<EditPlacement, string> = {
  amend: "Amend the source section",
  document: "Clarifications document",
};

/**
 * The option preselected when the card opens (answers.md "The question card"): the recommended one for a `major` gap (blocker and minor
 * gaps wait for a deliberate choice); the recorded option of an answered gap wins.
 */
export function preselectedOption(g: Gap): number | undefined {
  const recorded = g.clarification?.optionLabel ?? null;
  const chosen = recorded === null ? undefined : g.options.find((o) => o.label === recorded);
  if (chosen) return chosen.rank;
  if (g.clarification !== null) return undefined;
  return g.severity === "major" ? g.options.find((o) => o.recommended)?.rank : undefined;
}

/** A gap can be answered while it is open or staged (`answered`: the staged answer is replaced). */
export const answerable = (g: Gap): boolean => g.status === "open" || g.status === "answered";

/** "Unstage" fits a staged answer (pending, gap answered), "Withdraw answer" an applied or absorbed one (gap resolved); a gap whose
 * rebuild runs (`applying`) is read-only. */
export function withdrawable(g: Gap): boolean {
  const c = g.clarification;
  if (c?.clrId === undefined || g.status === "applying") return false;
  const live: ClarificationStatus[] = ["pending", "applied", "absorbed"];
  return live.includes(c.status);
}

/** The card's answer button: "Change answer" for a staged gap; "Answer and resolve" for a gap a coding agent waits on (applied at
 * once); else "Stage answer". */
export function submitLabel(g: Gap): string {
  if (g.status === "answered") return "Change answer";
  return holdsAgent(g) ? "Answer and resolve" : "Stage answer";
}

/** The line under the answer of a gap a coding agent waits on. */
export const AT_ONCE_TEXT =
  "This answer applies at once because a coding agent waits on it, together with every staged answer of the project.";

/** The answer's own text is 1..4000 characters (after trimming); null when it fits, else the words to show. */
export function ownTextProblem(text: string): string | null {
  const n = text.trim().length;
  if (n === 0) return "Write your answer first.";
  if (n > 4000) return `Your answer is ${String(n)} characters; keep it to 4,000.`;
  return null;
}

const WHEN = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });

/** A timestamp in words ("12 Nov 2026, 14:05"); an unreadable one is shown as sent. */
export function whenText(iso: string): string {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : WHEN.format(new Date(t));
}

/** What happens to a recorded answer: applied at once (a coding agent waits on it), or staged until someone presses "Resolve gaps". */
export function answerOutcomeText(r: AnswerResult): string {
  if (r.immediate) return "Your answer is being applied now: a coding agent waits on it.";
  return "Staged: it reaches the spec when someone presses Resolve gaps.";
}

/** The clarification's status in words (the review list and the card). */
export const CLARIFICATION_STATUS_LABEL: Record<ClarificationStatus, string> = {
  pending: "Staged",
  applied: "Applied",
  absorbed: "In the source file",
  conflict: "In conflict with the source",
  orphaned: "Its section is gone",
  reverted: "Reverted",
  superseded: "Replaced by a newer answer",
};

/** The second check's confidence of an AI answer, in words ("87% confident"); null when there is none. */
export function confidenceText(c: number | null | undefined): string | null {
  if (c === null || c === undefined || !Number.isFinite(c)) return null;
  return `${String(Math.round(Math.min(1, Math.max(0, c)) * 100))}% confident`;
}

// ---- failures in words ----

const FIELD_WORDS: Record<string, string> = {
  own_text: "Your answer must be 1 to 4,000 characters.",
  option_rank: "That option is no longer offered. Reload the gap and choose again.",
  edits: "The proposed edits no longer fit the source. Preview them again, or choose the clarifications document.",
  answer: "Choose an option or write your own answer.",
  reason: "The reason must be 1 to 500 characters.",
  question: "The question must be 10 to 1,000 characters.",
  about: "Name 1 to 20 spec ids that exist in the graph.",
  quotes: "A quoted line was not found in its document.",
  value: "The value must be 1 to 500 characters.",
  scope: "Choose document, section or pattern.",
};
const PLACEMENT_WORDS: Record<string, string> = {
  contradiction_amends: "A contradiction is always fixed in its source section; it cannot go to the clarifications document.",
  no_anchor: "This answer has no line to amend in the source. Choose the clarifications document.",
};

const STATE_WORDS: Record<GapStatus, string> = { open: "open", answered: "staged", applying: "being resolved", resolved: "resolved" };

/** A refused write in words: `400 answer_invalid` by field and reason, `409 gap_wrong_state` by the gap's state, the other failures. */
export function failureText(e: unknown): string {
  if (!isDataError(e)) return "Something went wrong. Try again.";
  const api = e.detail?.apiCode;
  if (api === "answer_invalid") {
    const field = e.detail?.field ?? "";
    if (field === "placement") return PLACEMENT_WORDS[e.detail?.reason ?? ""] ?? "This placement does not fit this answer.";
    if (field === "pattern" || e.detail?.reason === "pattern") return "The pattern is not a valid regular expression.";
    return FIELD_WORDS[field] ?? "Check your input and try again.";
  }
  if (api === "gap_wrong_state") {
    const state = e.detail?.state;
    return state === undefined
      ? "This gap is no longer in a state this action applies to."
      : `This gap is ${STATE_WORDS[state]} now, so this action no longer applies.`;
  }
  if (api === "gap_stale") return "This gap changed since you opened it.";
  if (api === "gap_not_found") return "This gap no longer exists.";
  if (api === "noise_rule_not_found") return "This rule was already removed.";
  if (api === "gap_unavailable") return "Gaps are not available on this server.";
  if (e.code === "network" || e.code === "unavailable") return "The server could not be reached. Try again.";
  return "Something went wrong. Try again.";
}

/** True for `409 gap_stale`: the card shows "This gap changed since you opened it" with Reload. */
export const isStale = (e: unknown): boolean => isDataError(e) && e.detail?.apiCode === "gap_stale";

/** True for `409 gap_wrong_state` of "Resolve gaps": no staged answer waits. */
export const isNothingStaged = (e: unknown): boolean => isDataError(e) && e.detail?.apiCode === "gap_wrong_state";

// ---- staging and resolving ----

/** Who staged or gave an answer: "AI" for an AI or pointer answer, else the member by name (`names`) or "a person". */
export function answeredByText(c: GapClarification, names: Readonly<Record<string, string>> = {}): string {
  return c.mode === "ai" || c.mode === "pointer" ? "AI" : actorText(c.answeredBy, names);
}

/** The chosen answer of a staged gap in the "Resolve gaps" list: the option's label, else "Own answer". */
export const stagedChoiceText = (c: GapClarification): string => c.optionLabel ?? "Own answer";

/** The line under a recorded answer: who staged it and what waits ("Staged by Dana · waits for Resolve gaps"), or who answered it and
 * its status, then an AI answer's confidence and the build that applied it. */
export function answerMetaText(g: Gap, c: GapClarification, names: Readonly<Record<string, string>> = {}): string {
  const who = answeredByText(c, names);
  const parts =
    c.status === "pending"
      ? [`Staged by ${who}`, g.status === "applying" ? "being resolved" : "waits for Resolve gaps"]
      : [`Answered by ${who}`, CLARIFICATION_STATUS_LABEL[c.status]];
  const confidence = confidenceText(c.confidence);
  if (confidence !== null) parts.push(confidence);
  if (c.appliedBuildId !== null && c.appliedBuildId !== undefined) parts.push(`applied by build ${c.appliedBuildId}`);
  return parts.join(" · ");
}

/** "1 gap", "3 gaps". */
const gapsText = (n: number): string => `${String(n)} ${n === 1 ? "gap" : "gaps"}`;

/** The "Resolve gaps" button: its label, whether it is off or busy, and the reason it gives (tooltip and accessible description). */
export interface ResolveView {
  label: string;
  state: "ready" | "off" | "busy";
  hint: string;
}

/** The button for `staged` gaps waiting (`answered`), `resolving` while a gap's rebuild runs (`applying`); `failed` when the staged
 * answers could not be read. */
export function resolveView(staged: number, resolving: boolean, failed = false): ResolveView {
  if (resolving) {
    return {
      label: RESOLVING_LABEL,
      state: "busy",
      hint: "A rebuild applies the staged answers now. The gaps become Resolved when it ends.",
    };
  }
  if (failed) return { label: "Resolve gaps", state: "off", hint: "The staged answers could not be read. Reload the page to try again." };
  if (staged === 0) return { label: "Resolve gaps", state: "off", hint: "Stage an answer first." };
  return { label: `Resolve ${gapsText(staged)}`, state: "ready", hint: "Applies every staged answer of the project in one rebuild." };
}

/** The toast after "Resolve gaps" started the rebuild. */
export function resolvedToast(r: ApplyResult): { title: string; message: string } {
  return {
    title: `Resolving ${gapsText(r.gapIds.length)}`,
    message: "One rebuild applies the staged answers. The gaps become Resolved when it ends.",
  };
}

/** The toast when nothing waited any more (`409 gap_wrong_state`). */
export const NOTHING_STAGED_TOAST = {
  title: "Nothing to resolve",
  message: "No staged answers are waiting. Someone may have resolved them already.",
};

// ---- dismissing and noise rules ----

/** The reasons offered by "Not a gap" (answers.md "Dismissing"), sent as these words; "Other" sends the free text. */
export const DISMISS_REASONS = [
  { value: "not a gap", label: "Not a gap" },
  { value: "not spec", label: "Not spec" },
  { value: "duplicate", label: "Duplicate" },
  { value: "won't decide now", label: "Won't decide now" },
];

/** A `section` noise rule's value (detection.md "Relevance filter"): the lineage and the heading path joined by " › ". */
export function sectionRuleValue(lineage: string, headingPath: readonly string[]): string {
  return [lineage, ...headingPath].join(" › ");
}

/** The section of a gap's first witness that names its document and section, for "also mark the section as not spec"; else null. */
export function witnessSection(g: Gap): { value: string; label: string } | null {
  const w = g.witness.find((q) => nonEmptyText(q.documentId) !== undefined && (q.headingPath ?? []).length > 0);
  if (w?.documentId === null || w?.documentId === undefined) return null;
  const heading = w.headingPath ?? [];
  const doc = nonEmptyText(w.documentTitle) ?? nonEmptyText(w.documentPath) ?? w.documentId;
  return { value: sectionRuleValue(w.documentId, heading), label: [doc, ...heading].join(" › ") };
}

export const SCOPE_LABEL: Record<"document" | "section" | "pattern", string> = {
  document: "Document",
  section: "Section",
  pattern: "Pattern",
};

// ---- history ----

const OUTCOME_WORDS: Record<string, string> = {
  applied: "applied to the new source revision",
  reanchored: "moved to a changed line",
  absorbed: "found in the source file",
  conflict: "in conflict with the source file",
  orphaned: "its section is gone from the source",
  undecided: "not decided yet",
};
const EVENT_WORDS: Record<string, string> = {
  created: "Gap found",
  raised: "Gap raised",
  routed: "Routed",
  answered: "Answer staged",
  replaced: "Staged answer replaced",
  applying: "Resolve started",
  applied: "Answer applied",
  resolved: "Resolved",
  reverted: "Answer withdrawn",
  reconciled: "Checked against a new source revision",
  dismissed: "Dismissed",
  not_grounded: "The build did not find the answer",
  cite_removed: "A quoted line left the source",
  follow_up: "Follow-up gap opened",
  reopened: "Reopened",
  apply_failed: "Resolving failed",
  assigned: "Assigned",
  unassigned: "Unassigned",
};
const SYSTEM_ACTORS = new Set(["system", "reconcile", "gap_run", "gap-run", "run", "build", "apply", "specflow"]);

/** Who did it, in words: AI, Specflow (a run, the build, reconcile), a member by name (`names`) or a person. */
export function actorText(actor: string, names: Readonly<Record<string, string>> = {}): string {
  if (actor === "ai") return "AI";
  if (SYSTEM_ACTORS.has(actor) || actor.startsWith("run-") || actor.startsWith("gap")) return "Specflow";
  return names[actor] ?? "a person";
}

/** One history event in words, never raw JSON: the event, the reconcile outcome, the route, the dismissal reason and the assignee
 * (named from `names`) when present. */
export function eventText(e: GapEvent, names: Readonly<Record<string, string>> = {}): string {
  const base = EVENT_WORDS[e.event] ?? `${e.event.charAt(0).toUpperCase()}${e.event.slice(1).replaceAll("_", " ")}`;
  const d = e.detail;
  const word = (k: string): string | undefined => (typeof d[k] === "string" && d[k] !== "" ? d[k] : undefined);
  const assignee = word("user_id");
  if (e.event === "assigned" && assignee !== undefined) return `${base} to ${names[assignee] ?? "a member"}`;
  const outcome = word("outcome");
  if (e.event === "reconciled" && outcome !== undefined) return `${base}: ${OUTCOME_WORDS[outcome] ?? outcome.replaceAll("_", " ")}`;
  const route = word("route");
  if (e.event === "routed" && route !== undefined) return `${base} to ${route === "ai" ? "AI" : "people"}`;
  const reason = word("reason");
  if ((e.event === "dismissed" || e.event === "reverted") && reason !== undefined) return `${base}: ${reason}`;
  return base;
}

// ---- plan links ----

/** The open gaps about the spec ids a task implements (`ABOUT` ∩ `implements`); an `ABOUT` node meets an id by its spec id or an alias. */
export function gapsForSpecIds(gaps: readonly Gap[], specIds: readonly string[]): Gap[] {
  const ids = new Set(specIds);
  return gaps.filter((g) => g.about.some((n) => ids.has(n.specId) || n.aliases.some((a) => ids.has(a))));
}

/** The union of the gaps of several tasks, each gap once, in the first-seen order. */
export function unionGaps(lists: readonly (readonly Gap[])[]): Gap[] {
  const seen = new Map<string, Gap>();
  for (const list of lists) for (const g of list) if (!seen.has(g.id)) seen.set(g.id, g);
  return [...seen.values()];
}

/** "1 open gap", "3 open gaps". */
export const openGapsText = (n: number): string => `${String(n)} open ${n === 1 ? "gap" : "gaps"}`;

// ---- the gap check status line ----

/** The gap run's steps in words (docs/graph/gap/how-it-works.md; `RUN_STEPS` of graph.gap). */
export const GAP_STEP_LABEL: Readonly<Record<string, string>> = {
  scope: "Reading what changed",
  close: "Checking answered gaps",
  relevance: "Separating specification from notes",
  detect: "Looking for signals",
  sites: "Grouping the signals",
  investigate: "Investigating",
  finish: "Finishing",
};

/**
 * What changes while a gap run records: its step and the sites done (a site's gaps are in the list once it counts as done). null when no
 * run runs. The inbox reads its rows again whenever it changes.
 */
export function checkPulse(c: GapCheck | null): string | null {
  if (c?.phase !== "running") return null;
  return `${String(c.stepsDone)}|${c.step ?? ""}|${String(c.itemsDone ?? "")}`;
}

/** Whether the line must be read again soon: the build or the gap run is under way. */
export const checkActive = (c: GapCheck): boolean => c.phase === "building" || c.phase === "queued" || c.phase === "running";

/** How far the running gap run is, 0..100: its done steps plus the share of the current step's items. */
export function checkProgress(c: GapCheck): number {
  if (c.stepsTotal <= 0) return 0;
  const part = c.itemsTotal !== null && c.itemsTotal > 0 && c.itemsDone !== null ? Math.min(c.itemsDone / c.itemsTotal, 1) : 0;
  return Math.min(100, Math.round(((Math.min(c.stepsDone, c.stepsTotal) + part) / c.stepsTotal) * 100));
}

/** What the items of a step are (graph.gap reports the sites of these steps); a step not named here shows bare counts. */
const GAP_STEP_ITEMS: Readonly<Partial<Record<string, string>>> = { investigate: "sites" };

/** The step the running gap run is on, in words, its items as the plan shows them: "Step 6 of 9: Investigating · 7 of 21 sites". */
export function checkStepText(c: GapCheck): string {
  const label = c.step === null ? "Starting" : (GAP_STEP_LABEL[c.step] ?? c.step);
  const { itemsDone: done, itemsTotal: total } = c;
  const noun = c.step === null ? undefined : GAP_STEP_ITEMS[c.step];
  const what = noun === undefined ? "" : " " + noun;
  const items = done !== null && total !== null && total > 0 ? ` · ${String(Math.min(done, total))} of ${String(total)}${what}` : "";
  if (c.stepsTotal <= 0) return label + items;
  const n = Math.min(c.stepsDone + 1, c.stepsTotal);
  return `Step ${String(n)} of ${String(c.stepsTotal)}: ${label}${items}`;
}

/** "2 new gaps" / "1 new gap" / "no new gaps". */
function newGapsText(n: number): string {
  if (n === 0) return "no new gaps";
  return `${String(n)} new ${n === 1 ? "gap" : "gaps"}`;
}

/** What the latest finished gap run found: "2 new gaps, 9 updated, 1 resolved", or "No new gaps". */
export function checkFoundText(c: GapCheck): string {
  const parts: string[] = [];
  if (c.newGaps !== null) parts.push(newGapsText(c.newGaps));
  if (c.updatedGaps !== null && c.updatedGaps > 0) parts.push(`${String(c.updatedGaps)} updated`);
  if (c.resolvedGaps !== null && c.resolvedGaps > 0) parts.push(`${String(c.resolvedGaps)} resolved`);
  const text = parts.join(", ");
  return text === "" ? "" : text.charAt(0).toUpperCase() + text.slice(1);
}

/** The words of a finished check: when it finished and what it found; a degraded one says some parts were not checked. */
function finishedText(c: GapCheck): { title: string; detail: string } {
  const when = c.finishedAt === null ? "" : ` ${whenText(c.finishedAt)}`;
  const found = checkFoundText(c);
  if (c.phase !== "degraded") return { title: `Gaps checked${when}`, detail: found };
  const lead = found === "" ? "" : `${found}. `;
  return {
    title: `Gap check finished with problems${when}`,
    detail: `${lead}Some parts could not be checked; the next build tries again.`,
  };
}

/** The words of a failed check, with its error code when the API sent one. */
function failedText(c: GapCheck): { title: string; detail: string } {
  const code = c.errorCode === null ? "" : `Error code: ${c.errorCode}. `;
  return { title: "The last gap check failed", detail: `${code}The gaps of earlier checks are still shown. The next build checks again.` };
}

/** The status line's title and detail for each phase. */
export function checkText(c: GapCheck): { title: string; detail: string } {
  switch (c.phase) {
    case "building":
      return { title: "Waiting for the knowledge build", detail: "Specflow looks for gaps as soon as the build finishes." };
    case "queued":
      return { title: "Gap check starting", detail: "It starts in a moment." };
    case "running":
      return { title: "Looking for gaps", detail: checkStepText(c) };
    case "never":
      return { title: "No gap check yet", detail: "Specflow looks for gaps after each knowledge build." };
    case "failed":
      return failedText(c);
    case "done":
    case "degraded":
      return finishedText(c);
  }
}
