// FR-GWEB-04 (docs/graph/gap/api.md "Web routes", "Models"): the production `GapsPort`, read-only. `GET /projects/{pid}/gaps` (the
// filters as query parameters, a list filter repeating its parameter; `held_back` is always sent, so the inbox's default view is
// `held_back=false` and "More found in this run" is `held_back=true`, FR-GRTE-06), `GET /projects/{pid}/gaps/{gap_id}`,
// `GET /projects/{pid}/gap/runs/latest` (the noise report of the latest finished run) and `GET /projects/{pid}/documents` (the Document
// filter). Bodies are parsed strictly: any other shape is `invalid_response`. `503 gap_unavailable` rejects as `unavailable` with that
// API code; the screen shows its "not available" state for it.
import { getGap, getGaps, getLatestGapRun, listDocuments } from "@/lib/api";
import {
  CLARIFICATION_STATUSES,
  GAP_KINDS,
  GAP_ORIGINS,
  GAP_ROUTES,
  GAP_SEVERITIES,
  GAP_STATUSES,
  type Gap,
  type GapClarification,
  type GapDocument,
  type GapNode,
  type GapNoise,
  type GapOption,
  type GapQuery,
  type GapQuote,
  type GapReach,
  type GapsPort,
  type NoiseSection,
} from "@/lib/domain/gaps";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const RUN_STATUSES = ["idle", "queued", "running", "completed", "degraded", "failed"] as const;
const RELEVANCE = ["spec", "tracking", "mixed"] as const;
const DECIDED_BY = ["rules", "model", "person"] as const;
const CLARIFICATION_MODES = ["option", "own", "ai", "pointer"] as const;

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
  return { specId: v.spec_id, label: v.label, name: v.name };
}

function toQuote(v: unknown): GapQuote {
  expectShape(isRecord(v) && isString(v.fragment_id) && isString(v.quote));
  return { fragmentId: v.fragment_id, quote: v.quote, lineStart: lineOrNull(v.line_start), lineEnd: lineOrNull(v.line_end) };
}

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
  return {
    mode: oneOf(CLARIFICATION_MODES, v.mode),
    status: oneOf(CLARIFICATION_STATUSES, v.status),
    optionLabel: textOrNull(v.option_label),
    text: textOrNull(v.text),
    answeredBy: v.answered_by,
    answeredAt: v.answered_at,
  };
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
  expectShape(v.held_back === undefined || typeof v.held_back === "boolean");
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
    heldBack: v.held_back === true,
    createdAt: v.created_at,
    about: listOf(v.about, toNode),
    impacts: listOf(v.impacts, toNode),
    witness: listOf(v.witness, toQuote),
    options: options.map((o, i) => ({ ...o, recommended: i === first })),
    clarification: toClarification(v.clarification),
    reach: toReach(v.reach),
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
  if (q.document !== undefined) params.set("document", q.document);
  params.set("held_back", q.heldBack === true ? "true" : "false");
  if (q.cursor !== undefined) params.set("cursor", q.cursor);
  return params.toString();
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
    async documents(projectId) {
      const body = await guard(() => listDocuments(projectId));
      return checked((): GapDocument[] => {
        expectShape(isRecord(body) && Array.isArray(body.items));
        return body.items.map((d: unknown) => {
          expectShape(isRecord(d) && isString(d.id) && isString(d.original_filename));
          return { id: d.id, name: d.original_filename };
        });
      });
    },
  };
}
