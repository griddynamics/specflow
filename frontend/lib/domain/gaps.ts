// FR-GWEB-04, FR-GWEB-06 (docs/graph/gap/api.md "Models", "Gap service", "Web routes", "User interface"; answers.md "The question card",
// "Answering", "Review list", "Revert", "Dismissing", "Raising a gap by hand", "Who answers"): the Gaps screen's types and its port, reads
// and writes. The http adapter (`lib/data/http/gaps.ts`) maps the API's snake_case bodies
// (`/projects/{pid}/gaps*`, `/projects/{pid}/gap/runs/latest`) to these types. Types and constants only.

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

/** A `CITES` witness line: the fragment, the verbatim quote and its line numbers when known. `get` also names its document (the lineage
 * as `documentId`, its title and path) and its section's heading path; a list item and an older API leave them null / empty. */
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
/** Where an answer's edits go: the source section (`amend`) or the generated clarifications document. */
export type EditPlacement = "amend" | "document";

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
  /** A new question its run held back from the inbox's default view until the next run (FR-GRTE-06). */
  heldBack: boolean;
  createdAt: string;
  /** The `updated_at` an answer sends back (a gap changed since then answers `409 gap_stale`); "" when the API sent none. */
  updatedAt: string;
  about: GapNode[];
  impacts: GapNode[];
  witness: GapQuote[];
  options: GapOption[];
  clarification: GapClarification | null;
  reach: GapReach | null;
  /** The gap's events and follow-ups (`get` only; null on a list item). */
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
  /** false = the inbox's default view, true = only the questions a run held back, undefined = not sent (both). */
  heldBack?: boolean;
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

/** The latest gap run and the noise report of the latest finished one (`runId` null: no run has finished yet). */
export interface GapNoise {
  status: "idle" | "queued" | "running" | "completed" | "degraded" | "failed";
  runId: string | null;
  sections: NoiseSection[];
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

// ---- writes ----

/** One edit of an answer, as the API checks it: where the text goes and how. Sent back unchanged from the preview to the answer. */
export interface GapEdit {
  placement: EditPlacement;
  target: { fragmentId: string | null; anchorQuote: string | null; heading: string | null };
  operation: "insert_after" | "replace" | "append_section";
  text: string;
}

/** The answer of a card: an offered option by rank, or the person's own text (exactly one). */
export type GapAnswerChoice = { optionRank: number } | { ownText: string };

/** One section of an edits preview, before and after the edits. */
export interface PreviewSection {
  fragmentId: string | null;
  lineage: string | null;
  headingPath: string[];
  before: string;
  after: string;
}

/** `EditPreview`: the edits an answer would make with the placement they use, shown as before/after per section. `drafted`: the edits
 * were drafted from own text; `fallback`: why the preview fell back to the clarifications document (a code), else null. */
export interface EditPreview {
  gapId: string;
  placement: EditPlacement;
  edits: GapEdit[];
  sections: PreviewSection[];
  drafted: boolean;
  fallback: string | null;
}

export interface AnswerInput {
  choice: GapAnswerChoice;
  /** The placement the preview showed (or the person chose); absent: the server's default for the answer. */
  placement?: EditPlacement;
  /** The preview's edits (sent only when it has some). */
  edits: GapEdit[];
  /** The gap's `updatedAt` as the card read it. */
  updatedAt: string;
}

export interface AnswerResult {
  gapId: string;
  clrId: string;
  replaced: string | null;
  placement: ClarificationPlacement;
  amendments: string[];
  applyAt: string;
  immediate: boolean;
}

export interface ApplyResult {
  gapIds: string[];
  clrIds: string[];
  amendments: string[];
  build: string | null;
}

/** A row of "Answered by AI": the gap, its AI (or pointer) clarification, and the follow-up gap a revert opened. */
export interface AiAnswer {
  gapId: string;
  title: string;
  question: string;
  gapStatus: GapStatus;
  clarification: GapClarification & { clrId: string };
  followUp: string | null;
}

export interface RevertResult {
  clrId: string;
  gapId: string;
  /** True when a follow-up gap opened (the reverted answer's gap was resolved). */
  followUp: boolean;
  amendments: string[];
  build: string | null;
}

export const NOISE_SCOPES = ["document", "section", "pattern"] as const;
export type NoiseScope = (typeof NOISE_SCOPES)[number];

export interface NoiseRule {
  ruleId: string;
  scope: NoiseScope;
  value: string;
  reason: string;
  createdBy: string;
  createdAt: string;
}

export interface RaiseInput {
  question: string;
  kind: GapKind;
  about: string[];
  severity?: GapSeverity;
}

export interface RaisedGap {
  gapId: string;
  created: boolean;
}

export interface GapsPort {
  list(projectId: string, query: GapQuery): Promise<GapPage>;
  get(projectId: string, gapId: string): Promise<Gap>;
  noise(projectId: string): Promise<GapNoise>;
  /** Whether the gap run (or the build it follows) runs now, and what the latest finished run found. */
  check(projectId: string): Promise<GapCheck>;
  previewEdits(projectId: string, gapId: string, choice: GapAnswerChoice, placement?: EditPlacement): Promise<EditPreview>;
  answer(projectId: string, gapId: string, input: AnswerInput): Promise<AnswerResult>;
  dismiss(projectId: string, gapId: string, reason: string): Promise<void>;
  /** Assigns the gap to a member (a user id) or clears it (null); resolves with the gap as `get` reads it. */
  assign(projectId: string, gapId: string, userId: string | null): Promise<Gap>;
  raise(projectId: string, input: RaiseInput): Promise<RaisedGap>;
  applyNow(projectId: string): Promise<ApplyResult>;
  aiAnswers(projectId: string): Promise<AiAnswer[]>;
  revert(projectId: string, clrId: string, reason: string): Promise<RevertResult>;
  noiseRules(projectId: string): Promise<NoiseRule[]>;
  addNoiseRule(projectId: string, rule: { scope: NoiseScope; value: string; reason: string }): Promise<string>;
  removeNoiseRule(projectId: string, ruleId: string): Promise<void>;
}
