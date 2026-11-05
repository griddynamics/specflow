// FE-PLAN-01, FE-DATA-01 (TECH-SPECS §2.15): the production `PlanPort` over the plan API (`/projects/{pid}/plan*`, docs/graph/plan/).
//  - `state` reads `GET /plan`: a running run is Stage 1 (`creating`, its step and label from the step name), a project with a plan is
//    Stage 2 (`ready`, the plan read from `GET /plan/tasks`), anything else is Stage 0 (`idle`, with the error code of a failed run).
//  - `create` POSTs `/plan` (`replan` from "Recreate Plan"); the screen then follows the run by polling: while a screen listens and the
//    last state is `creating`, `GET /plan` is read every `pollMs` (2 s) and each change is an event (`plan.step`, `plan.ready`,
//    `plan.failed`). A read that fails keeps the last state and is tried again at the next tick. `leave` stops the polling.
//  - `preview` reads `GET /plan/preview`. Task text editing is not available: `updateTaskBody` rejects with `not_available`.
// Bodies are parsed strictly: any other shape is `invalid_response`.
import { getPlan, getPlanPreview, getPlanTasks, startPlan } from "@/lib/api";
import type { Unsubscribe } from "@/lib/domain/common";
import { DataError } from "@/lib/domain/errors";
import { taskMarkdown } from "@/lib/domain/logic/plan";
import {
  PHASE_ORDER,
  type AreaKind,
  type Complexity,
  type NotBuildable,
  type PlanArea,
  type PlanEvent,
  type PlannedBy,
  type PlanPort,
  type PlanPreview,
  type PlanQuality,
  type PlanReviewIssue,
  type PlanSnapshot,
  type ReviewSeverity,
  type PlanState,
  type PlanStory,
  type PlanTask,
  type StoryKind,
  type StoryProgress,
  type TaskPhase,
  type TaskStatus,
} from "@/lib/domain/plan";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

/** The status is read again every 2 s while a run is going. */
export const DEFAULT_PLAN_POLL_MS = 2_000;

/** The label of each plan step (`graph.plan` STEPS), as the progress list shows it. */
export const STEP_LABELS: Record<string, string> = {
  snapshot: "Reading the knowledge graph",
  partition: "Grouping the specification into work areas",
  describe: "Describing the work areas",
  project: "Planning the project",
  tasks: "Writing the tasks",
  stitch: "Linking task dependencies",
  review: "Reviewing the tasks",
  diff: "Comparing with the current plan",
  persist: "Saving the plan",
  check: "Checking the plan",
};

const RUN_STATUSES = ["idle", "running", "succeeded", "partially_succeeded", "failed"] as const;
type RunStatus = (typeof RUN_STATUSES)[number];
const TASK_STATUSES: readonly TaskStatus[] = ["pending", "in_progress", "completed", "failed", "blocked", "superseded", "retired"];
const COMPLEXITIES: readonly Complexity[] = ["low", "medium", "high"];
const PROGRESS: readonly StoryProgress[] = ["pending", "in_progress", "blocked", "completed"];
const AREA_KINDS: readonly AreaKind[] = ["module", "project"];
const STORY_KINDS: readonly StoryKind[] = ["feature", "interface", "entity", "core", "chapter", "setup", "closing"];
const PLANNED_BY: readonly PlannedBy[] = ["model", "rules"];
const SEVERITIES: readonly ReviewSeverity[] = ["blocking", "advisory"];
const RESULT_STATUSES = ["succeeded", "partially_succeeded"] as const;
const PLANNERS = ["describe", "project", "tasks"] as const;

/** `GET /plan` after the shape check. */
export interface PlanStatusWire {
  status: RunStatus;
  hasPlan: boolean;
  runId: string | null;
  step: string | null;
  stepsDone: number;
  stepsTotal: number;
  stepItemsDone: number | null;
  stepItemsTotal: number | null;
  errorCode: string | null;
}

const isCount = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;
const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || v === undefined || isString(v));
  return isString(v) ? v : null;
};
const countOrNull = (v: unknown): number | null => {
  expectShape(v === null || v === undefined || isCount(v));
  return isCount(v) ? v : null;
};
const oneOf = <T extends string>(values: readonly T[], v: unknown): T => {
  const found = values.find((x) => x === v);
  expectShape(found !== undefined);
  return found;
};
const strings = (v: unknown): string[] => {
  expectShape(Array.isArray(v) && v.every(isString));
  return v;
};
const records = (v: unknown): Record<string, unknown>[] => {
  expectShape(Array.isArray(v) && v.every(isRecord));
  return v;
};

export function toPlanStatus(body: unknown): PlanStatusWire {
  return checked(() => {
    expectShape(isRecord(body) && typeof body.has_plan === "boolean" && isCount(body.steps_done) && isCount(body.steps_total));
    return {
      status: oneOf(RUN_STATUSES, body.status),
      hasPlan: body.has_plan,
      runId: textOrNull(body.run_id),
      step: textOrNull(body.step),
      stepsDone: body.steps_done,
      stepsTotal: body.steps_total,
      stepItemsDone: countOrNull(body.step_items_done),
      stepItemsTotal: countOrNull(body.step_items_total),
      errorCode: textOrNull(body.error_code),
    };
  });
}

function toTask(t: Record<string, unknown>): Omit<PlanTask, "body"> {
  expectShape(isString(t.task_id) && isString(t.area_id) && isString(t.story_id) && isString(t.title));
  expectShape(isCount(t.revision) && typeof t.ready === "boolean");
  return {
    taskId: t.task_id,
    areaId: t.area_id,
    storyId: t.story_id,
    title: t.title,
    description: textOrNull(t.description) ?? "",
    phase: oneOf<TaskPhase>(PHASE_ORDER, t.phase),
    complexity: oneOf(COMPLEXITIES, t.complexity),
    status: oneOf(TASK_STATUSES, t.status),
    doneWhen: records(t.done_when).map((d) => {
      expectShape(isString(d.text));
      return d.text;
    }),
    waitsFor: strings(t.waits_for),
    implements: records(t.implements).map((i) => {
      expectShape(isString(i.spec_id));
      return { specId: i.spec_id, name: textOrNull(i.name), label: textOrNull(i.label) };
    }),
    commit: textOrNull(t.commit),
    blockReason: textOrNull(t.block_reason),
    ready: t.ready,
    plannedBy: t.planned_by === undefined ? "model" : oneOf(PLANNED_BY, t.planned_by),
    version: t.revision,
  };
}

/** Counts by key: only the listed keys, each a whole number ≥ 0; others are ignored. */
function counts<K extends string>(v: unknown, keys: readonly K[]): Partial<Record<K, number>> {
  if (v === undefined || v === null) return {};
  expectShape(isRecord(v));
  const out: Partial<Record<K, number>> = {};
  for (const key of keys) {
    const n = v[key];
    if (n === undefined) continue;
    expectShape(isCount(n));
    out[key] = n;
  }
  return out;
}

function toQuality(v: unknown): PlanQuality | null {
  if (v === undefined || v === null) return null;
  expectShape(isRecord(v) && isString(v.run_id));
  const count = (n: unknown): number => {
    expectShape(n === undefined || isCount(n));
    return isCount(n) ? n : 0;
  };
  return {
    runId: v.run_id,
    status: oneOf(RESULT_STATUSES, v.status),
    fallbacks: counts(v.fallbacks, PLANNERS),
    review: counts(v.review, SEVERITIES),
    reviewFailed: v.review_failed === true,
    unplaced: count(v.unplaced),
    oversized: count(v.oversized),
  };
}

function toReview(v: unknown): PlanReviewIssue[] {
  if (v === undefined) return [];
  return records(v).map((r) => {
    expectShape(isString(r.area_id) && isString(r.problem));
    return { areaId: r.area_id, taskId: textOrNull(r.task_id), problem: r.problem, severity: oneOf(SEVERITIES, r.severity) };
  });
}

function toNotBuildable(v: unknown): NotBuildable[] {
  if (v === undefined) return [];
  return records(v).map((r) => {
    expectShape(isString(r.spec_id) && isString(r.reason));
    return { specId: r.spec_id, name: textOrNull(r.name), reason: r.reason, note: textOrNull(r.note) };
  });
}

/** `GET /plan/tasks` body -> `PlanSnapshot`; each task gets its read-only Markdown body. */
export function toPlanSnapshot(body: unknown): PlanSnapshot {
  return checked(() => {
    expectShape(isRecord(body) && isString(body.plan_id));
    const areas: PlanArea[] = records(body.areas).map((a) => {
      expectShape(isString(a.area_id));
      return {
        areaId: a.area_id,
        kind: oneOf(AREA_KINDS, a.kind),
        title: textOrNull(a.title),
        description: textOrNull(a.description),
        plannedBy: a.planned_by === undefined || a.planned_by === null ? null : oneOf(PLANNED_BY, a.planned_by),
        notBuildable: toNotBuildable(a.not_buildable),
      };
    });
    const stories: PlanStory[] = records(body.stories).map((s) => {
      expectShape(isString(s.story_id) && isString(s.area_id) && isString(s.title));
      return {
        storyId: s.story_id,
        areaId: s.area_id,
        kind: oneOf(STORY_KINDS, s.kind),
        title: s.title,
        summary: textOrNull(s.summary),
        progress: oneOf(PROGRESS, s.progress),
      };
    });
    const tasks = records(body.tasks).map(toTask);
    const titles = new Map(tasks.map((t) => [t.taskId, t.title]));
    return {
      planId: body.plan_id,
      areas,
      stories,
      tasks: tasks.map((t) => ({ ...t, body: taskMarkdown(t, titles) })),
      quality: toQuality(body.quality),
      review: toReview(body.review),
    };
  });
}

/** `GET /plan/preview` body -> `PlanPreview`. */
export function toPlanPreview(body: unknown): PlanPreview {
  return checked(() => {
    expectShape(isRecord(body));
    const count = (v: unknown): number | null => {
      expectShape(v === null || v === undefined || isCount(v));
      return isCount(v) ? v : null;
    };
    return {
      features: records(body.features).map((f) => {
        expectShape(isString(f.spec_id) && isString(f.name));
        return { specId: f.spec_id, name: f.name };
      }),
      calls: count(body.calls),
      inputTokens: count(body.input_tokens),
    };
  });
}

/** Stage 1 of a running run: the step being run (1-based, never past the total) and its label. */
export function creatingOf(s: PlanStatusWire): Extract<PlanState, { phase: "creating" }> {
  const total = s.stepsTotal > 0 ? s.stepsTotal : Object.keys(STEP_LABELS).length;
  const step = Math.min(s.stepsDone + 1, total);
  const name = s.step ?? "snapshot";
  const items =
    s.stepItemsTotal !== null && s.stepItemsTotal > 0 && s.stepItemsDone !== null
      ? { done: Math.min(s.stepItemsDone, s.stepItemsTotal), total: s.stepItemsTotal }
      : null;
  return { phase: "creating", runId: s.runId ?? "", step, total, label: STEP_LABELS[name] ?? name, items };
}

/** The event that moves a listener from `prev` to `next`; null when nothing it shows changed. */
export function eventOf(prev: PlanState | null, next: PlanState): PlanEvent["type"] | null {
  const wasCreating = prev?.phase === "creating";
  if (next.phase === "creating") {
    if (!wasCreating) return "plan.started";
    const sameItems = prev.items?.done === next.items?.done && prev.items?.total === next.items?.total;
    return prev.step === next.step && prev.label === next.label && sameItems ? null : "plan.step";
  }
  if (!wasCreating) return null;
  return next.phase === "ready" ? "plan.ready" : "plan.failed";
}

type Listener = (e: PlanEvent, next: PlanState) => void;

interface Watch {
  listeners: Set<Listener>;
  timer: ReturnType<typeof setTimeout> | null;
  last: PlanState | null;
}

export function createHttpPlan(opts?: { pollMs?: number }): PlanPort {
  const pollMs = opts?.pollMs ?? DEFAULT_PLAN_POLL_MS;
  const watches = new Map<string, Watch>();

  const watchOf = (projectId: string): Watch => {
    let w = watches.get(projectId);
    if (!w) {
      w = { listeners: new Set(), timer: null, last: null };
      watches.set(projectId, w);
    }
    return w;
  };

  async function read(projectId: string): Promise<PlanState> {
    const status = toPlanStatus(await guard(() => getPlan(projectId)));
    if (status.status === "running") return creatingOf(status);
    if (status.hasPlan) return { phase: "ready", plan: toPlanSnapshot(await guard(() => getPlanTasks(projectId))) };
    return status.status === "failed" && status.errorCode !== null ? { phase: "idle", failure: status.errorCode } : { phase: "idle" };
  }

  function stop(w: Watch) {
    if (w.timer !== null) clearTimeout(w.timer);
    w.timer = null;
  }

  /** Records `next` and tells the listeners when it changed what they show; polls again while a run is going. */
  function update(projectId: string, w: Watch, next: PlanState) {
    const type = eventOf(w.last, next);
    w.last = next;
    if (type !== null) {
      const runId = next.phase === "creating" ? next.runId : "";
      for (const l of [...w.listeners]) l({ type, runId }, next);
    }
    schedule(projectId, w);
  }

  function schedule(projectId: string, w: Watch) {
    if (w.timer !== null || w.listeners.size === 0 || w.last?.phase !== "creating") return;
    w.timer = setTimeout(() => {
      w.timer = null;
      read(projectId).then(
        (next) => {
          update(projectId, w, next);
        },
        () => {
          schedule(projectId, w); // keep the last state; try again at the next tick
        },
      );
    }, pollMs);
  }

  return {
    preview: async (projectId) => toPlanPreview(await guard(() => getPlanPreview(projectId))),
    async state(projectId) {
      const w = watchOf(projectId);
      const next = await read(projectId);
      w.last = next;
      schedule(projectId, w);
      return next;
    },
    subscribe(projectId, listener): Unsubscribe {
      const w = watchOf(projectId);
      w.listeners.add(listener);
      schedule(projectId, w);
      return () => {
        w.listeners.delete(listener);
        if (w.listeners.size === 0) stop(w);
      };
    },
    async create(projectId, createOpts) {
      const w = watchOf(projectId);
      const status = toPlanStatus(await guard(() => startPlan(projectId, createOpts?.replan ?? false)));
      const next = creatingOf(status);
      update(projectId, w, next);
      return { runId: next.runId };
    },
    leave(projectId) {
      const w = watches.get(projectId);
      if (w) stop(w);
      watches.delete(projectId);
    },
    updateTaskBody: () => Promise.reject(new DataError("not_available", false)),
  };
}
