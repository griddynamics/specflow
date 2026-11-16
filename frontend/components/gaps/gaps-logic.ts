// FR-GWEB-04 (docs/graph/gap/api.md "User interface", answers.md "The question card"): pure helpers of the read-only Gaps screen. No React,
// no fetch: the words of kinds, severities, origins and noise sections, the filters' options and the `list_gaps` query they make, and the
// inbox order.
import type { SeverityLevel } from "@/components/ui/SeverityBadge";
import { isDataError } from "@/lib/domain/errors";
import {
  GAP_KINDS,
  GAP_ORIGINS,
  GAP_SEVERITIES,
  type Gap,
  type GapClarification,
  type GapKind,
  type GapOrigin,
  type GapQuery,
  type GapQuote,
  type GapSeverity,
  type GapStatus,
  type NoiseSection,
} from "@/lib/domain/gaps";

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

export const STATUS_LABEL: Record<GapStatus, string> = { open: "Open", answered: "Answered", applying: "Applying", resolved: "Resolved" };

/** The inbox filters as the screen holds them; "all" (and "active" for the status) sends no parameter. */
export interface GapFilters {
  status: "active" | GapStatus | "all";
  kind: GapKind | "all";
  severity: GapSeverity | "all";
  origin: GapOrigin | "all";
  story: string; // a story id, or "all"
  document: string; // a document id, or "all"
}

export const DEFAULT_GAP_FILTERS: GapFilters = {
  status: "active",
  kind: "all",
  severity: "all",
  origin: "all",
  story: "all",
  document: "all",
};

type Options = { value: string; label: string }[];
const ALL = { value: "all", label: "All" };

export const STATUS_OPTIONS: Options = [
  { value: "active", label: "Not resolved" },
  { value: "open", label: "Open" },
  { value: "answered", label: "Answered" },
  { value: "applying", label: "Applying" },
  { value: "resolved", label: "Resolved" },
  { value: "all", label: "All" },
];
export const KIND_OPTIONS: Options = [ALL, ...Object.entries(KIND_LABEL).map(([value, label]) => ({ value, label }))];
export const SEVERITY_OPTIONS: Options = [ALL, ...Object.entries(SEVERITY_LABEL).map(([value, label]) => ({ value, label }))];
export const ORIGIN_OPTIONS: Options = [
  ALL,
  { value: "analysis", label: "Analysis" },
  { value: "development", label: "Coding agent" },
  { value: "person", label: "Person" },
];

/** "All" first, then the given items in their order. */
export function withAll(items: { id: string; name: string }[]): Options {
  return [ALL, ...items.map((i) => ({ value: i.id, label: i.name }))];
}

const STATUS_VALUES = ["active", "open", "answered", "applying", "resolved", "all"] as const;

/** A value from a filter control; anything outside the vocabulary means the default. */
export function patchOf(key: keyof GapFilters, v: string): Partial<GapFilters> {
  if (key === "status") return { status: STATUS_VALUES.find((x) => x === v) ?? "active" };
  if (key === "kind") return { kind: GAP_KINDS.find((x) => x === v) ?? "all" };
  if (key === "severity") return { severity: GAP_SEVERITIES.find((x) => x === v) ?? "all" };
  if (key === "origin") return { origin: GAP_ORIGINS.find((x) => x === v) ?? "all" };
  const value = v === "" ? "all" : v;
  return key === "story" ? { story: value } : { document: value };
}

/**
 * The `list_gaps` query of the inbox: route `ask` (the inbox shows the questions for people; AI answers are not in it), the chosen
 * filters, and `held_back` (false: the default view; true: "More found in this run", FR-GRTE-06).
 */
export function gapQuery(f: GapFilters, heldBack: boolean): GapQuery {
  const q: GapQuery = { route: ["ask"], heldBack };
  if (f.status === "all") q.status = ["open", "answered", "applying", "resolved"];
  else if (f.status !== "active") q.status = [f.status];
  if (f.kind !== "all") q.kind = [f.kind];
  if (f.severity !== "all") q.severity = [f.severity];
  if (f.origin !== "all") q.origin = [f.origin];
  if (f.story !== "all") q.storyId = f.story;
  if (f.document !== "all") q.document = f.document;
  return q;
}

/** The inbox order (api.md "User interface"): development gaps that hold tasks first, then the server's order (severity, newest). */
export function inboxOrder(gaps: Gap[]): Gap[] {
  const holds = (g: Gap) => g.origin === "development" && g.blocking;
  return gaps.toSorted((a, b) => Number(holds(b)) - Number(holds(a)));
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

/** Where a witness line is: its fragment and lines ("lines 12–14", "line 12"). */
export function quoteSource(q: GapQuote): string {
  if (q.lineStart === null) return q.fragmentId;
  const lines =
    q.lineEnd === null || q.lineEnd === q.lineStart ? `line ${String(q.lineStart)}` : `lines ${String(q.lineStart)}–${String(q.lineEnd)}`;
  return `${q.fragmentId} · ${lines}`;
}

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
