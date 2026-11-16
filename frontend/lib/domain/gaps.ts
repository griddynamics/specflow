// FR-GWEB-04 (docs/graph/gap/api.md "Models", "Gap service", "User interface"; answers.md "The question card"): the Gaps screen's types and
// its read-only port. The http adapter (`lib/data/http/gaps.ts`) maps the API's snake_case bodies (`/projects/{pid}/gaps*`,
// `/projects/{pid}/gap/runs/latest`) to these types. Types and constants only.

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

/** A spec node of an `ABOUT` or `IMPACTS` relation: its id, its type label and its name. */
export interface GapNode {
  specId: string;
  label: string;
  name: string;
}

/** A `CITES` witness line: the fragment, the verbatim quote and its line numbers when known. */
export interface GapQuote {
  fragmentId: string;
  quote: string;
  lineStart: number | null;
  lineEnd: number | null;
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

/** The current clarification of an answered gap. */
export interface GapClarification {
  mode: "option" | "own" | "ai" | "pointer";
  status: ClarificationStatus;
  optionLabel: string | null;
  text: string | null;
  answeredBy: string;
  answeredAt: string;
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
  about: GapNode[];
  impacts: GapNode[];
  witness: GapQuote[];
  options: GapOption[];
  clarification: GapClarification | null;
  reach: GapReach | null;
}

/** The `list_gaps` filters the screen sends; an absent filter is not sent (the server default applies). */
export interface GapQuery {
  status?: GapStatus[];
  kind?: GapKind[];
  severity?: GapSeverity[];
  origin?: GapOrigin[];
  route?: GapRoute[];
  storyId?: string;
  document?: string;
  /** false = the inbox's default view, true = only the questions a run held back. */
  heldBack?: boolean;
  cursor?: string;
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

/** A document of the project, for the Document filter. */
export interface GapDocument {
  id: string;
  name: string;
}

export interface GapsPort {
  list(projectId: string, query: GapQuery): Promise<GapPage>;
  get(projectId: string, gapId: string): Promise<Gap>;
  noise(projectId: string): Promise<GapNoise>;
  documents(projectId: string): Promise<GapDocument[]>;
}
