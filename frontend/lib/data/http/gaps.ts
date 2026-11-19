// FR-GWEB-04, FR-GWEB-06 (docs/graph/gap/api.md "Web routes", "Models"): the production `GapsPort`. Reads: `GET /projects/{pid}/gaps` (the
// filters as query parameters, a list filter repeating its parameter; the inbox sends `assigned_to` for the Assigned filter; the Plan
// screens send none), `GET .../gaps/{gap_id}`, `GET .../gap/runs/latest` (the noise report of the latest finished run, and with
// `GET .../graph_build` the gap check status line), `GET .../gaps/ai_answers` and `GET .../gaps/noise_rules`. Writes:
// `POST .../gaps/{gap_id}/edits_preview`, `.../answer`, `.../dismiss`, `PUT .../gaps/{gap_id}/assignee`, `POST .../gaps` (raise),
// `POST .../gaps/apply` ("Resolve gaps"), `POST .../gaps/clarifications/{clr_id}/revert`, `POST .../gaps/noise_rules` and
// `DELETE .../gaps/noise_rules/{rule_id}`. Bodies are parsed strictly: any other shape is `invalid_response`. Failures are `DataError`s
// with the API code and, for `400 answer_invalid` and `409 gap_wrong_state`, the `field`, `reason` and gap `state` of the envelope's
// details; `503 gap_unavailable` rejects as `unavailable` (the screen's "not available" state).
import {
  addNoiseRule,
  answerGap,
  applyGapAnswers,
  assignGap,
  dismissGap,
  getAiAnswers,
  getGap,
  getGaps,
  getGraphBuild,
  getLatestGapRun,
  getNoiseRules,
  previewGapEdits,
  raiseGap,
  removeNoiseRule,
  revertClarification,
} from "@/lib/api";
import {
  CLARIFICATION_PLACEMENTS,
  CLARIFICATION_STATUSES,
  GAP_KINDS,
  GAP_ORIGINS,
  GAP_ROUTES,
  GAP_SEVERITIES,
  GAP_STATUSES,
  NOISE_SCOPES,
  type AiAnswer,
  type AnswerInput,
  type AnswerResult,
  type ApplyResult,
  type BasisQuote,
  type EditPlacement,
  type EditPreview,
  type Gap,
  type GapAnswerChoice,
  type GapClarification,
  type GapEdit,
  type GapEvent,
  type GapFollowUp,
  type GapHistory,
  type GapNode,
  type GapCheck,
  type GapNoise,
  type GapOption,
  type GapQuery,
  type GapQuote,
  type GapReach,
  type GapsPort,
  type NoiseRule,
  type NoiseSection,
  type PreviewSection,
  type RevertResult,
} from "@/lib/domain/gaps";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const RUN_STATUSES = ["idle", "queued", "running", "completed", "degraded", "failed"] as const;
const RELEVANCE = ["spec", "tracking", "mixed"] as const;
const DECIDED_BY = ["rules", "model", "person"] as const;
const CLARIFICATION_MODES = ["option", "own", "ai", "pointer"] as const;
const EDIT_PLACEMENTS = ["amend", "document"] as const;
const OPERATIONS = ["insert_after", "replace", "append_section"] as const;
const DIRECTIONS = ["continues", "continued_by"] as const;

const oneOf = <T extends string>(values: readonly T[], v: unknown): T => {
  const found = values.find((x) => x === v);
  expectShape(found !== undefined);
  return found;
};
const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || v === undefined || isString(v));
  return isString(v) ? v : null;
};
const textOr = (v: unknown, fallback: string): string => textOrNull(v) ?? fallback;
const lineOrNull = (v: unknown): number | null => {
  expectShape(v === null || v === undefined || (typeof v === "number" && Number.isInteger(v)));
  return typeof v === "number" ? v : null;
};
const listOf = <T>(v: unknown, item: (x: unknown) => T): T[] => {
  if (v === undefined) return [];
  expectShape(Array.isArray(v));
  return v.map(item);
};
const text = (v: unknown): string => {
  expectShape(isString(v));
  return v;
};

function toNode(v: unknown): GapNode {
  expectShape(isRecord(v) && isString(v.spec_id) && isString(v.label) && isString(v.name));
  return { specId: v.spec_id, label: v.label, name: v.name, aliases: listOf(v.aliases ?? undefined, text) }; // aliases: absent or null in older APIs
}

function toQuote(v: unknown): GapQuote {
  expectShape(isRecord(v) && isString(v.fragment_id) && isString(v.quote));
  return {
    fragmentId: v.fragment_id,
    quote: v.quote,
    lineStart: lineOrNull(v.line_start),
    lineEnd: lineOrNull(v.line_end),
    documentId: textOrNull(v.document_id),
    documentTitle: textOrNull(v.document_title),
    documentPath: textOrNull(v.document_path),
    headingPath: listOf(v.heading_path, text),
  };
}

function toBasis(v: unknown): BasisQuote {
  expectShape(isRecord(v) && isString(v.fragment_id) && isString(v.quote));
  return { fragmentId: v.fragment_id, quote: v.quote };
}

const flag = (v: unknown): boolean => {
  expectShape(v === undefined || typeof v === "boolean");
  return v === true;
};
const count = (v: unknown): number => {
  expectShape(v === undefined || (typeof v === "number" && Number.isInteger(v) && v >= 0));
  return typeof v === "number" ? v : 0;
};

function toOption(v: unknown): GapOption {
  expectShape(isRecord(v) && typeof v.rank === "number" && Number.isInteger(v.rank) && isString(v.label));
  expectShape(typeof v.recommended === "boolean");
  return {
    rank: v.rank,
    label: v.label,
    detail: textOr(v.detail, ""),
    recommended: v.recommended,
    edits: listOf(v.edits, (e) => e).length,
  };
}

function toClarification(v: unknown): GapClarification | null {
  if (v === null || v === undefined) return null;
  expectShape(isRecord(v) && isString(v.answered_by) && isString(v.answered_at));
  expectShape(v.confidence === undefined || v.confidence === null || (typeof v.confidence === "number" && Number.isFinite(v.confidence)));
  const c: GapClarification = {
    mode: oneOf(CLARIFICATION_MODES, v.mode),
    status: oneOf(CLARIFICATION_STATUSES, v.status),
    optionLabel: textOrNull(v.option_label),
    text: textOrNull(v.text),
    answeredBy: v.answered_by,
    answeredAt: v.answered_at,
    appliedBuildId: textOrNull(v.applied_build_id),
    basis: listOf(v.basis, toBasis),
    confidence: typeof v.confidence === "number" ? v.confidence : null,
  };
  if (v.clr_id !== undefined) c.clrId = text(v.clr_id);
  if (v.placement !== undefined) c.placement = oneOf(CLARIFICATION_PLACEMENTS, v.placement);
  return c;
}

function toEvent(v: unknown): GapEvent {
  expectShape(isRecord(v) && isString(v.at) && isString(v.actor) && isString(v.event));
  expectShape(v.detail === undefined || isRecord(v.detail));
  return {
    gapId: textOrNull(v.gap_id),
    clrId: textOrNull(v.clr_id),
    at: v.at,
    actor: v.actor,
    event: v.event,
    detail: isRecord(v.detail) ? v.detail : {},
  };
}

function toFollowUp(v: unknown): GapFollowUp {
  expectShape(isRecord(v) && isString(v.gap_id) && isString(v.reason));
  return { gapId: v.gap_id, reason: v.reason, direction: oneOf(DIRECTIONS, v.direction) };
}

function toHistory(v: unknown): GapHistory | null {
  if (v === null || v === undefined) return null;
  expectShape(isRecord(v));
  // Time order, oldest first, whatever order the API sent them in.
  const events = listOf(v.events, toEvent).toSorted((a, b) => a.at.localeCompare(b.at));
  return { events, followUps: listOf(v.follow_ups, toFollowUp) };
}

function toReach(v: unknown): GapReach | null {
  if (v === null || v === undefined) return null;
  expectShape(isRecord(v));
  return { stories: listOf(v.stories, text), tasks: listOf(v.tasks, text) };
}

/** One `GapView` of the API. */
export function toGap(v: unknown): Gap {
  expectShape(isRecord(v) && isString(v.gap_id) && isString(v.title) && isString(v.question) && isString(v.created_at));
  expectShape(v.blocking === undefined || typeof v.blocking === "boolean");
  expectShape(v.updated_at === undefined || isString(v.updated_at));
  expectShape(v.assigned_to === undefined || v.assigned_to === null || isString(v.assigned_to));
  // A card never shows more than one recommendation (answers.md "The question card"): the first recommended option keeps the mark.
  const options = listOf(v.options, toOption).toSorted((a, b) => a.rank - b.rank);
  const first = options.findIndex((o) => o.recommended);
  return {
    id: v.gap_id,
    kind: oneOf(GAP_KINDS, v.kind),
    severity: oneOf(GAP_SEVERITIES, v.severity),
    status: oneOf(GAP_STATUSES, v.status),
    origin: oneOf(GAP_ORIGINS, v.origin),
    route: oneOf(GAP_ROUTES, v.route),
    title: v.title,
    question: v.question,
    why: textOr(v.why, ""),
    blocking: v.blocking === true,
    createdAt: v.created_at,
    updatedAt: isString(v.updated_at) ? v.updated_at : "",
    about: listOf(v.about, toNode),
    impacts: listOf(v.impacts, toNode),
    witness: listOf(v.witness, toQuote),
    options: options.map((o, i) => ({ ...o, recommended: i === first })),
    clarification: toClarification(v.clarification),
    reach: toReach(v.reach),
    history: toHistory(v.history),
    assignedTo: isString(v.assigned_to) ? v.assigned_to : null,
  };
}

function toSection(v: unknown): NoiseSection {
  expectShape(isRecord(v) && isString(v.lineage));
  return {
    lineage: v.lineage,
    headingPath: listOf(v.heading_path, text),
    relevance: oneOf(RELEVANCE, v.relevance),
    by: oneOf(DECIDED_BY, v.by),
    signals: listOf(v.signals, text),
    removed: listOf(v.removed, text),
  };
}

/** `GapRunStatus`: the run status and, once a run finished, its noise report. */
export function toNoise(v: unknown): GapNoise {
  expectShape(isRecord(v));
  const status = oneOf(RUN_STATUSES, v.status);
  if (v.result === null || v.result === undefined) return { status, runId: null, sections: [] };
  expectShape(isRecord(v.result) && isString(v.result.run_id));
  const noise = v.result.noise;
  expectShape(noise === undefined || isRecord(noise));
  return { status, runId: v.result.run_id, sections: listOf(noise?.sections, toSection) };
}

/** A count of the run result (`counts.gaps_new.total`, `counts.close.resolved`): null when the run has no such number. */
function resultCount(counts: unknown, group: string, key: string): number | null {
  if (!isRecord(counts)) return null;
  const inner = counts[group];
  if (!isRecord(inner)) return null;
  const n = inner[key];
  return typeof n === "number" && Number.isInteger(n) && n >= 0 ? n : null;
}

const PHASE_OF_RUN = {
  idle: "never",
  queued: "queued",
  running: "running",
  completed: "done",
  degraded: "degraded",
  failed: "failed",
} as const;

/**
 * `GET .../gap/runs/latest` and the knowledge build's status (`GET .../graph_build`, null when it could not be read) as one `GapCheck`: a
 * queued or running gap run wins; else a running build is `building` (its gap run follows); else the latest run's outcome.
 */
export function toGapCheck(run: unknown, build: unknown): GapCheck {
  expectShape(isRecord(run));
  const status = oneOf(RUN_STATUSES, run.status);
  const building = isRecord(build) && build.status === "running";
  const active = status === "queued" || status === "running";
  const counts = isRecord(run.result) ? run.result.counts : undefined;
  return {
    phase: !active && building ? "building" : PHASE_OF_RUN[status],
    step: textOrNull(run.step),
    stepsDone: count(run.steps_done),
    stepsTotal: count(run.steps_total),
    itemsDone: lineOrNull(run.step_items_done),
    itemsTotal: lineOrNull(run.step_items_total),
    startedAt: textOrNull(run.started_at),
    finishedAt: textOrNull(run.finished_at),
    newGaps: resultCount(counts, "gaps_new", "total"),
    updatedGaps: resultCount(counts, "gaps_updated", "total"),
    resolvedGaps: resultCount(counts, "close", "resolved"),
    errorCode: textOrNull(run.error_code),
  };
}

/** The query string of `list_gaps`: one parameter per value of a list filter. */
export function gapQueryString(q: GapQuery): string {
  const params = new URLSearchParams();
  const many = (name: string, values: readonly string[] | undefined) => {
    for (const value of values ?? []) params.append(name, value);
  };
  many("status", q.status);
  many("kind", q.kind);
  many("severity", q.severity);
  many("origin", q.origin);
  many("route", q.route);
  if (q.storyId !== undefined) params.set("story_id", q.storyId);
  if (q.assignedTo !== undefined) params.set("assigned_to", q.assignedTo);
  if (q.cursor !== undefined) params.set("cursor", q.cursor);
  if (q.limit !== undefined) params.set("limit", String(q.limit));
  return params.toString();
}

// ---- writes ----

function toEdit(v: unknown): GapEdit {
  expectShape(isRecord(v) && isString(v.text) && isRecord(v.target));
  const t = v.target;
  return {
    placement: oneOf(EDIT_PLACEMENTS, v.placement),
    target: { fragmentId: textOrNull(t.fragment_id), anchorQuote: textOrNull(t.anchor_quote), heading: textOrNull(t.heading) },
    operation: oneOf(OPERATIONS, v.operation),
    text: v.text,
  };
}

/** An edit back on the wire, exactly as the preview sent it (unset target fields are left out). */
export function editWire(e: GapEdit): Record<string, unknown> {
  const target: Record<string, string> = {};
  if (e.target.fragmentId !== null) target.fragment_id = e.target.fragmentId;
  if (e.target.anchorQuote !== null) target.anchor_quote = e.target.anchorQuote;
  if (e.target.heading !== null) target.heading = e.target.heading;
  return { placement: e.placement, target, operation: e.operation, text: e.text };
}

function toPreviewSection(v: unknown): PreviewSection {
  expectShape(isRecord(v) && isString(v.after));
  return {
    fragmentId: textOrNull(v.fragment_id),
    lineage: textOrNull(v.lineage),
    headingPath: listOf(v.heading_path, text),
    before: textOr(v.before, ""),
    after: v.after,
  };
}

/** `EditPreview`. */
export function toPreview(v: unknown): EditPreview {
  expectShape(isRecord(v) && isString(v.gap_id) && Array.isArray(v.edits) && Array.isArray(v.sections));
  return {
    gapId: v.gap_id,
    placement: oneOf(EDIT_PLACEMENTS, v.placement),
    edits: v.edits.map(toEdit),
    sections: v.sections.map(toPreviewSection),
    drafted: flag(v.drafted),
    fallback: textOrNull(v.fallback),
  };
}

/** The body of an answer or a preview: the option rank or the own text, and the placement when one is given. */
export function choiceWire(choice: GapAnswerChoice, placement: EditPlacement | undefined): Record<string, unknown> {
  const body: Record<string, unknown> = "optionRank" in choice ? { option_rank: choice.optionRank } : { own_text: choice.ownText };
  if (placement !== undefined) body.placement = placement;
  return body;
}

/** `POST .../answer`: the choice, the placement, the preview's edits when it has some, and the gap's `updated_at` as read. */
export function answerWire(input: AnswerInput): Record<string, unknown> {
  const body = choiceWire(input.choice, input.placement);
  if (input.edits.length > 0) body.edits = input.edits.map(editWire);
  body.updated_at = input.updatedAt;
  return body;
}

const ids = (v: unknown): string[] => listOf(v, text);

function toAnswerResult(v: unknown): AnswerResult {
  expectShape(isRecord(v) && isString(v.gap_id) && isString(v.clr_id));
  return {
    gapId: v.gap_id,
    clrId: v.clr_id,
    replaced: textOrNull(v.replaced),
    placement: oneOf(CLARIFICATION_PLACEMENTS, v.placement),
    amendments: ids(v.amendments),
    applyAt: textOrNull(v.apply_at),
    immediate: flag(v.immediate),
  };
}

function toApplyResult(v: unknown): ApplyResult {
  expectShape(isRecord(v));
  return { gapIds: ids(v.gap_ids), clrIds: ids(v.clr_ids), amendments: ids(v.amendments), build: textOrNull(v.build) };
}

function toAiAnswer(v: unknown): AiAnswer {
  expectShape(isRecord(v) && isString(v.gap_id));
  const c = toClarification(v.clarification);
  expectShape(c?.clrId !== undefined);
  return {
    gapId: v.gap_id,
    title: textOr(v.title, ""),
    question: textOr(v.question, ""),
    gapStatus: oneOf(GAP_STATUSES, v.gap_status),
    clarification: { ...c, clrId: c.clrId },
    followUp: textOrNull(v.follow_up),
  };
}

function toRevertResult(v: unknown): RevertResult {
  expectShape(isRecord(v) && isString(v.clr_id) && isString(v.gap_id) && typeof v.follow_up === "boolean");
  return { clrId: v.clr_id, gapId: v.gap_id, followUp: v.follow_up, amendments: ids(v.amendments), build: textOrNull(v.build) };
}

function toNoiseRule(v: unknown): NoiseRule {
  expectShape(isRecord(v) && isString(v.rule_id) && isString(v.value) && isString(v.reason) && isString(v.created_at));
  return {
    ruleId: v.rule_id,
    scope: oneOf(NOISE_SCOPES, v.scope),
    value: v.value,
    reason: v.reason,
    createdBy: textOr(v.created_by, ""),
    createdAt: v.created_at,
  };
}

export function createHttpGaps(): GapsPort {
  return {
    async list(projectId, query) {
      const body = await guard(() => getGaps(projectId, gapQueryString(query)));
      return checked(() => {
        expectShape(isRecord(body) && Array.isArray(body.gaps));
        return { gaps: body.gaps.map(toGap), next: textOrNull(body.next) };
      });
    },
    async get(projectId, gapId) {
      const body = await guard(() => getGap(projectId, gapId));
      return checked(() => toGap(body));
    },
    async noise(projectId) {
      const body = await guard(() => getLatestGapRun(projectId));
      return checked(() => toNoise(body));
    },
    async check(projectId) {
      const [run, build] = await Promise.all([
        guard(() => getLatestGapRun(projectId)),
        guard(() => getGraphBuild(projectId)).catch(() => null), // the build's state only adds "building"
      ]);
      return checked(() => toGapCheck(run, build));
    },
    async previewEdits(projectId, gapId, choice, placement) {
      const body = await guard(() => previewGapEdits(projectId, gapId, choiceWire(choice, placement)));
      return checked(() => toPreview(body));
    },
    async answer(projectId, gapId, input) {
      const body = await guard(() => answerGap(projectId, gapId, answerWire(input)));
      return checked(() => toAnswerResult(body));
    },
    async dismiss(projectId, gapId, reason) {
      await guard(() => dismissGap(projectId, gapId, reason));
    },
    async assign(projectId, gapId, userId) {
      const body = await guard(() => assignGap(projectId, gapId, userId));
      return checked(() => toGap(body));
    },
    async raise(projectId, input) {
      const wire: Record<string, unknown> = { question: input.question, kind: input.kind, about: input.about };
      if (input.severity !== undefined) wire.severity = input.severity;
      const body = await guard(() => raiseGap(projectId, wire));
      return checked(() => {
        expectShape(isRecord(body) && isString(body.gap_id));
        return { gapId: body.gap_id, created: body.created !== false };
      });
    },
    async applyNow(projectId) {
      const body = await guard(() => applyGapAnswers(projectId));
      return checked(() => toApplyResult(body));
    },
    async aiAnswers(projectId) {
      const body = await guard(() => getAiAnswers(projectId));
      return checked(() => {
        expectShape(isRecord(body));
        return listOf(body.answers, toAiAnswer);
      });
    },
    async revert(projectId, clrId, reason) {
      const body = await guard(() => revertClarification(projectId, clrId, reason));
      return checked(() => toRevertResult(body));
    },
    async noiseRules(projectId) {
      const body = await guard(() => getNoiseRules(projectId));
      return checked(() => {
        expectShape(isRecord(body) && Array.isArray(body.rules));
        return body.rules.map(toNoiseRule);
      });
    },
    async addNoiseRule(projectId, rule) {
      const body = await guard(() => addNoiseRule(projectId, { scope: rule.scope, value: rule.value, reason: rule.reason }));
      return checked(() => {
        expectShape(isRecord(body) && isString(body.rule_id));
        return body.rule_id;
      });
    },
    async removeNoiseRule(projectId, ruleId) {
      await guard(() => removeNoiseRule(projectId, ruleId));
    },
  };
}
