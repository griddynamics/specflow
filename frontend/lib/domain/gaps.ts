// FR-GWEB-04 (docs/graph/gap/api.md "Models", "Gap service", "Web routes"): the canvas gaps inbox's types and its port — reads only:
// answering moved to the agents and the Slack bot (D10). The http adapter (`lib/data/http/gaps.ts`) maps the API's snake_case bodies
// (`/projects/{pid}/gaps`, `/projects/{pid}/gap/runs/latest`) to these types. Types and constants only; the words of the former Gaps
// screen (`components/shared/gaps/gaps-logic.ts`) still read the answer and apply results.

export const GAP_KINDS = ["missing", "ambiguous", "contradiction", "inconsistency", "open_question", "assumption"] as const;
export const GAP_SEVERITIES = ["blocker", "major", "minor"] as const;
export const GAP_STATUSES = ["open", "answered", "applying", "resolved"] as const;
export const GAP_ORIGINS = ["analysis", "development", "person"] as const;
export const GAP_ROUTES = ["ask", "ai"] as const;

export type GapKind = (typeof GAP_KINDS)[number];
export type GapSeverity = (typeof GAP_SEVERITIES)[number];
export type GapStatus = (typeof GAP_STATUSES)[number];
export type GapOrigin = (typeof GAP_ORIGINS)[number];
export type GapRoute = (typeof GAP_ROUTES)[number];

/** A spec node of an `ABOUT` or `IMPACTS` relation: its id, its type label, its name and the other spec ids it answers to (`aliases`,
 * empty when the API sent none). */
export interface GapNode {
  specId: string;
  label: string;
  name: string;
  aliases: string[];
}

/** A `CITES` witness line: the fragment, the verbatim quote and its line numbers when known. A full read also names its document (the
 * lineage as `documentId`, its title and path) and its section's heading path; a list item and an older API leave them null / empty. */
export interface GapQuote {
  fragmentId: string;
  quote: string;
  lineStart: number | null;
  lineEnd: number | null;
  documentId?: string | null;
  documentTitle?: string | null;
  documentPath?: string | null;
  headingPath?: string[];
}

/** One offered answer (`gap_option`); exactly one per gap is recommended. `edits` is how many edits the option would make. */
export interface GapOption {
  rank: number;
  label: string;
  detail: string;
  recommended: boolean;
  edits: number;
}

export const CLARIFICATION_STATUSES = ["pending", "applied", "absorbed", "conflict", "orphaned", "reverted", "superseded"] as const;
export type ClarificationStatus = (typeof CLARIFICATION_STATUSES)[number];

export const CLARIFICATION_PLACEMENTS = ["amend", "document", "none"] as const;
export type ClarificationPlacement = (typeof CLARIFICATION_PLACEMENTS)[number];

/** A basis quote of an AI or pointer answer. */
export interface BasisQuote {
  fragmentId: string;
  quote: string;
}

/** The current clarification of an answered gap. `clrId`, `placement`, `appliedBuildId`, `basis` and `confidence` are read when sent. */
export interface GapClarification {
  clrId?: string;
  mode: "option" | "own" | "ai" | "pointer";
  placement?: ClarificationPlacement;
  status: ClarificationStatus;
  optionLabel: string | null;
  text: string | null;
  answeredBy: string;
  answeredAt: string;
  appliedBuildId?: string | null;
  basis?: BasisQuote[];
  /** 0..1, the second check's confidence of an AI or pointer answer. */
  confidence?: number | null;
}

/** One `gap_event` row of a gap's history: who did what when; `detail` holds ids, codes and outcomes (never shown as raw JSON). */
export interface GapEvent {
  gapId: string | null;
  clrId: string | null;
  at: string;
  actor: string;
  event: string;
  detail: Readonly<Record<string, unknown>>;
}

/** A `FOLLOWS_UP` link: this gap continues another one (`continues`) or another gap continues this one (`continued_by`). */
export interface GapFollowUp {
  gapId: string;
  reason: string;
  direction: "continues" | "continued_by";
}

export interface GapHistory {
  events: GapEvent[];
  followUps: GapFollowUp[];
}

export interface GapReach {
  stories: string[];
  tasks: string[];
}

/** A gap as reads return it. A list item has no options and no reach (`options` is empty, `reach` null). */
export interface Gap {
  id: string;
  kind: GapKind;
  severity: GapSeverity;
  status: GapStatus;
  origin: GapOrigin;
  route: GapRoute;
  title: string;
  question: string;
  why: string;
  blocking: boolean;
  createdAt: string;
  /** The `updated_at` an answer sends back (a gap changed since then answers `409 gap_stale`); "" when the API sent none. */
  updatedAt: string;
  about: GapNode[];
  impacts: GapNode[];
  witness: GapQuote[];
  options: GapOption[];
  clarification: GapClarification | null;
  reach: GapReach | null;
  /** The gap's events and follow-ups (a full read only; null on a list item). */
  history?: GapHistory | null;
  /** The member who resolves the gap (a user id), null when nobody is assigned. */
  assignedTo: string | null;
}

/** The `list_gaps` filters the screen sends; an absent filter is not sent (the server default applies). */
export interface GapQuery {
  status?: GapStatus[];
  kind?: GapKind[];
  severity?: GapSeverity[];
  origin?: GapOrigin[];
  route?: GapRoute[];
  storyId?: string;
  /** "me" (the caller), "none" (nobody) or a member's user id. */
  assignedTo?: string;
  cursor?: string;
  /** Page size, 1..200 (the server default when absent). */
  limit?: number;
}

export interface GapPage {
  gaps: Gap[];
  next: string | null;
}

/** One section a gap run judged tracking or mixed, and the node ids it removed from structural seeds. */
export interface NoiseSection {
  lineage: string;
  headingPath: string[];
  relevance: "spec" | "tracking" | "mixed";
  by: "rules" | "model" | "person";
  signals: string[];
  removed: string[];
}

/**
 * Whether Specflow is looking for gaps now (docs/graph/gap/lifecycle.md: build, then gap run, then plan run): `building` while the knowledge
 * build runs (the gap run follows it), `queued` / `running` for the gap run, else the latest run's outcome (`done`, `degraded`, `failed`)
 * or `never`. `step` is the run's step name; `itemsDone` of `itemsTotal` while a step reports items. The counts are the latest finished
 * run's (null before one).
 */
export interface GapCheck {
  phase: "building" | "queued" | "running" | "done" | "degraded" | "failed" | "never";
  step: string | null;
  stepsDone: number;
  stepsTotal: number;
  itemsDone: number | null;
  itemsTotal: number | null;
  startedAt: string | null;
  finishedAt: string | null;
  newGaps: number | null;
  updatedGaps: number | null;
  resolvedGaps: number | null;
  errorCode: string | null;
}

// ---- answer and apply results ----
// The words of the former Gaps screen (`components/shared/gaps/gaps-logic.ts`) still format these; the writes themselves are the
// agents' and the Slack bot's (D10).

/** What an answer returns: a staged answer waits for "Resolve gaps" (`applyAt` null); a development gap that holds a coding agent's task
 * is applied at once (`immediate`, `applyAt` the time). */
export interface AnswerResult {
  gapId: string;
  clrId: string;
  replaced: string | null;
  placement: ClarificationPlacement;
  amendments: string[];
  applyAt: string | null;
  immediate: boolean;
}

export interface ApplyResult {
  gapIds: string[];
  clrIds: string[];
  amendments: string[];
  build: string | null;
}

export interface GapsPort {
  list(projectId: string, query: GapQuery): Promise<GapPage>;
  /** Whether the gap run (or the build it follows) runs now, and what the latest finished run found. */
  check(projectId: string): Promise<GapCheck>;
}
