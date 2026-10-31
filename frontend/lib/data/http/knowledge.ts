// FR-GDUI-0018, FR-GDUI-0021, FR-SRC-0007, FR-SRC-0012, FR-SRC-0025 (GDRIVE plan 07; sources-ux SPECS §7.3; plans/connect-build-kg-button):
// the production `KnowledgePort` and the Ready gate. Every `status()` reads `GET /projects/{id}/sources?batches=1` and `GET
// /projects/{id}/graph_build` in parallel and merges them.
//  - The screen's rich `ProjectSourcesStatus` is derived from the v2 status: `sources[]` (Drive folders and Local files batches,
//    in added order), `progress` -> the Build knowledge lock (FR-GDUI-0021), `removed_files` -> `changes.removed`.
//    The body is parsed strictly: any other shape (the legacy body of an old API included) is `invalid_response`.
//  - `graph_build` gives `build`, `lastBuild`, `ready` and `buildFailure`. A failed graph read of any kind keeps the last graph state
//    (idle the first time) and never touches the sources path, its back-off or its error UI. Start failures of `build()` are kept per
//    project until a running or newer build is read, so the status shows them (no 503-specific handling).
//  - Ready gate (owner): a project is Ready when it has at least one processed document (`processed_documents` >= 1). Plan and
//    Decisions read it through `missionControl.snapshot` (components/shell/use-ready-gate.ts), which reads ONLY that field and never
//    calls graph_build, so the gate works with either body shape. `ProjectSourcesStatus.ready` is a different thing: a build succeeded.
//  - `build` POSTs `graph_build` with the reasoning level; it never runs a summary step.
//  - Polling: while a screen listens, every `pollMs` (3 s) while the import is unfinished or a build runs and every `idleMs` (5 s)
//    otherwise (R13: a change made elsewhere shows within 10 s); a status read that fails is retried with a back-off (3 s, 6 s, then
//    10 s), and not at all on `unauthenticated`. Changes made by this app (save, upload, remove, build) notify at once.
//  - A read that fails (or is `invalid_response`) after a status was shown keeps that status: no error UI (FR-SRC-0025.AC2, R25).
import { getGraphBuild, getSourcesStatus, startGraphBuild } from "@/lib/api";
import type { Unsubscribe } from "@/lib/domain/common";
import { DataError, isDataError } from "@/lib/domain/errors";
import type { MissionControlPort, MissionControlSnapshot } from "@/lib/domain/pipeline";
import type { ProjectStatus } from "@/lib/domain/projects";
import type {
  BuildFailure,
  BuildMode,
  BuildState,
  ImportProgress,
  KnowledgeBuild,
  KnowledgePhase,
  KnowledgePort,
  ProjectSourcesStatus,
  Source,
} from "@/lib/domain/sources";
import { checked, expectShape, guard, isRecord, isString } from "./errors";
import { isUnknownKind, toProgress, toSource } from "./sources-wire";

/** FR-SRC-0007: the status is read again every 3 s while an import runs. */
export const DEFAULT_POLL_MS = 3_000;
/** FR-SRC-0012: and every 5 s when nothing runs, so a change made in another tab shows within 10 s. */
export const DEFAULT_IDLE_MS = 5_000;
const MAX_POLL_MS = 10_000;

/** What the v2 status says, after the shape check. */
export interface StatusV2 {
  progress: ImportProgress;
  sources: Source[];
  processedDocuments: number;
  removedFiles: number;
}

const isCount = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;

/** `GET /projects/{pid}/sources?batches=1` body -> `StatusV2`; any other shape is `invalid_response`. */
export function toStatus(body: unknown): StatusV2 {
  return checked(() => {
    expectShape(isRecord(body) && Array.isArray(body.sources) && isCount(body.processed_documents) && isCount(body.removed_files));
    const progress = toProgress(body.progress);
    expectShape(progress);
    return {
      progress,
      sources: body.sources.filter((s) => !isUnknownKind(s)).map(toSource),
      processedDocuments: body.processed_documents,
      removedFiles: body.removed_files,
    };
  });
}

/** The Ready rule (owner 2026-10-19). Pure. */
export const isReady = (s: Pick<StatusV2, "processedDocuments">): boolean => s.processedDocuments >= 1;

const unfinished = (p: ImportProgress): boolean => !p.listingFinished || p.finished < p.total;

const sum = (sources: Source[], pick: (s: Source) => number): number => sources.reduce((n, s) => n + pick(s), 0);

/** `GET /projects/{pid}/graph_build`, after the shape check. */
export interface GraphBuildWire {
  status: "idle" | "running" | "succeeded" | "partially_succeeded" | "failed";
  buildId: string | null;
  mode: BuildMode | null;
  startedAt: string | null;
  finishedAt: string | null;
  errorCode: string | null;
}

const GRAPH_STATUSES: readonly GraphBuildWire["status"][] = ["idle", "running", "succeeded", "partially_succeeded", "failed"];
const BUILD_MODES: readonly BuildMode[] = ["first", "update", "rebuild"];

const text = (v: unknown): string | null => {
  if (v === undefined || v === null) return null;
  expectShape(isString(v));
  return v;
};

/** `graph_build` body -> `GraphBuildWire`: `status` is strict, an unknown or missing `mode` is null, extra fields are ignored. */
export function toGraphBuild(body: unknown): GraphBuildWire {
  return checked(() => {
    expectShape(isRecord(body));
    const status = GRAPH_STATUSES.find((v) => v === body.status);
    expectShape(status);
    return {
      status,
      buildId: text(body.build_id),
      mode: BUILD_MODES.find((m) => m === body.mode) ?? null,
      startedAt: text(body.started_at),
      finishedAt: text(body.finished_at),
      errorCode: text(body.error_code),
    };
  });
}

/** What `deriveStatus` knows beyond the sources: the graph read, a start failure still on show, and whether a build succeeded before. */
export interface GraphInput {
  graph: GraphBuildWire;
  startFailure: BuildFailure | null;
  rememberedReady: boolean;
}

const IDLE_GRAPH: GraphBuildWire = { status: "idle", buildId: null, mode: null, startedAt: null, finishedAt: null, errorCode: null };
export const NO_GRAPH: GraphInput = { graph: IDLE_GRAPH, startFailure: null, rememberedReady: false };

const ms = (t: string | null): number => (t === null ? Number.NaN : Date.parse(t));
/** The graph state began after the start failure: the failure is old news. Unparseable times never clear it. */
const newer = (g: GraphBuildWire, f: BuildFailure): boolean => ms(g.startedAt ?? g.finishedAt) > ms(f.startedAt);

const buildOf = (g: GraphBuildWire, state: BuildState, finished: boolean): KnowledgeBuild => ({
  id: g.buildId,
  mode: g.mode,
  state,
  startedAt: g.startedAt,
  finishedAt: finished ? g.finishedAt : null,
  errorCode: finished ? g.errorCode : null,
});

/** `ready` of the screen: a build succeeded (now, or as an update/rebuild the graph says, or remembered from an earlier read). */
const isBuilt = (g: GraphBuildWire, remembered: boolean): boolean =>
  g.status === "succeeded" || g.status === "partially_succeeded" || g.mode === "update" || g.mode === "rebuild" || remembered;

/** What the button would start: nothing while the import runs, without sources or without a processed document. */
function nextBuildOf(s: StatusV2, ready: boolean): ProjectSourcesStatus["nextBuild"] {
  if (unfinished(s.progress) || s.sources.length === 0 || s.processedDocuments === 0) return null;
  return ready ? "update" : "first";
}

/** A refused start stays on show unless a build runs or one began after it; else a failed graph build is the failure. */
function buildFailureOf(g: GraphBuildWire, startFailure: BuildFailure | null): BuildFailure | null {
  if (g.status === "running") return null;
  if (startFailure && !newer(g, startFailure)) return startFailure;
  if (g.status !== "failed") return null;
  return { code: g.errorCode ?? "unknown", buildId: g.buildId, mode: g.mode, startedAt: g.startedAt, finishedAt: g.finishedAt };
}

function phaseOf(s: StatusV2, g: GraphBuildWire, ready: boolean, nextBuild: ProjectSourcesStatus["nextBuild"]): KnowledgePhase {
  if (s.sources.length === 0) return "no_sources";
  if (unfinished(s.progress)) return "collecting";
  if (g.status === "running") return "building";
  if (s.sources.some((c) => c.failure !== null || c.counts.failed > 0)) return "attention";
  if (ready) return "up_to_date";
  return nextBuild ? "ready_to_build" : "collecting";
}

/** Pure: the v2 status and the graph state -> the screen's status. Without a graph it is the sources-only status. */
export function deriveStatus(s: StatusV2, input: GraphInput = NO_GRAPH): ProjectSourcesStatus {
  const { graph: g } = input;
  const failedSources = s.sources.filter((c) => c.failure !== null).length;
  const failedFiles = sum(s.sources, (c) => c.counts.failed);
  const finishedState: BuildState | null = g.status === "idle" || g.status === "running" ? null : g.status;
  const ready = isBuilt(g, input.rememberedReady);
  const nextBuild = nextBuildOf(s, ready);
  return {
    phase: phaseOf(s, g, ready, nextBuild),
    ready,
    sources: s.sources,
    progress: s.progress,
    totals: {
      documents: s.processedDocuments,
      skipped: sum(s.sources, (c) => c.counts.skipped),
      failed: failedFiles,
      pending: Math.max(0, s.progress.total - s.progress.finished),
    },
    changes: { new: 0, changed: 0, removed: s.removedFiles },
    attention: { failedItems: failedFiles, needsAccess: 0, needsRelink: 0, errors: failedSources },
    nextBuild,
    build: g.status === "running" ? buildOf(g, "running", false) : null,
    lastBuild: finishedState ? buildOf(g, finishedState, true) : null,
    buildFailure: buildFailureOf(g, input.startFailure),
  };
}

function projectStatusOf(s: StatusV2, status: ProjectSourcesStatus): ProjectStatus {
  if (isReady(s)) return "ready";
  if (unfinished(s.progress)) return "processing";
  return status.phase === "attention" ? "attention" : "empty";
}

export interface ChangeBus {
  notify(projectId: string): void;
  subscribe(projectId: string, listener: () => void): Unsubscribe;
}

export function createChangeBus(): ChangeBus {
  const listeners = new Map<string, Set<() => void>>();
  return {
    notify: (projectId) =>
      listeners.get(projectId)?.forEach((l) => {
        l();
      }),
    subscribe(projectId, listener) {
      const set = listeners.get(projectId) ?? new Set();
      set.add(listener);
      listeners.set(projectId, set);
      return () => {
        set.delete(listener);
        if (set.size === 0) listeners.delete(projectId);
      };
    },
  };
}

export interface HttpKnowledge {
  knowledge: KnowledgePort;
  missionControl: MissionControlPort;
  changes: ChangeBus;
}

/** `onStatus` feeds the project list (TECH-SPECS §3.5); `pollMs` and `idleMs` are constructor options for the stub-API tests. */
export function createHttpKnowledge(
  opts: { onStatus?: (projectId: string, status: ProjectStatus) => void; pollMs?: number; idleMs?: number; changes?: ChangeBus } = {},
): HttpKnowledge {
  const pollMs = opts.pollMs ?? DEFAULT_POLL_MS;
  const idleMs = opts.idleMs ?? DEFAULT_IDLE_MS;
  const changes = opts.changes ?? createChangeBus();
  const timers = new Map<string, ReturnType<typeof setTimeout>>();

  const stop = (projectId: string) => {
    const t = timers.get(projectId);
    if (t !== undefined) clearTimeout(t);
    timers.delete(projectId);
  };
  /** One more read after `pollMs`, but only while a screen listens (and no timer is waiting already). */
  const again = (projectId: string, listening: Set<() => void> | undefined, delay: number = pollMs) => {
    if (!listening || listening.size === 0 || timers.has(projectId)) return;
    timers.set(
      projectId,
      setTimeout(() => {
        timers.delete(projectId);
        changes.notify(projectId);
      }, delay),
    );
  };
  const failures = new Map<string, number>(); // consecutive failed reads per project
  const shown = new Map<string, ProjectSourcesStatus>(); // the last status a screen got (R25: kept when a later read fails)
  // Graph state per project; none is cleared by `leave()`, so they last for the tab (a remounted screen knows the last build).
  const lastGraph = new Map<string, GraphBuildWire>(); // the last good `graph_build` read, or the answer of a started build
  const startFailures = new Map<string, BuildFailure>(); // a refused start, on show until a running or newer build is read
  const readyOf = new Map<string, boolean>(); // the last derived `ready`, for a later read that cannot see the graph
  const epoch = new Map<string, number>(); // bumped by `build()` so a graph read that began before the POST cannot overwrite it

  const listeningFor = new Map<string, Set<() => void>>(); // this port's own screen listeners (the poll runs only for them)

  /** A graph read never rejects: any failure (HTTP code, network, bad shape) is null and the caller keeps the last graph state. */
  async function readGraph(projectId: string): Promise<GraphBuildWire | null> {
    try {
      return toGraphBuild(await guard(() => getGraphBuild(projectId)));
    } catch {
      return null;
    }
  }

  async function read(projectId: string): Promise<{ lean: StatusV2; status: ProjectSourcesStatus; graph: GraphBuildWire }> {
    const started = epoch.get(projectId) ?? 0;
    const [body, graphRead] = await Promise.all([guard(() => getSourcesStatus(projectId)), readGraph(projectId)]);
    const lean = toStatus(body);
    const fresh = (epoch.get(projectId) ?? 0) === started ? graphRead : null; // a POST finished meanwhile: this read may be older than it
    const graph = fresh ?? lastGraph.get(projectId) ?? NO_GRAPH.graph;
    if (fresh) {
      lastGraph.set(projectId, fresh);
      const failure = startFailures.get(projectId);
      if (failure && (fresh.status === "running" || newer(fresh, failure))) startFailures.delete(projectId);
    }
    const status = deriveStatus(lean, {
      graph,
      startFailure: startFailures.get(projectId) ?? null,
      rememberedReady: readyOf.get(projectId) ?? false,
    });
    readyOf.set(projectId, status.ready);
    opts.onStatus?.(projectId, projectStatusOf(lean, status));
    return { lean, status, graph };
  }

  const knowledge: KnowledgePort = {
    async status(projectId) {
      try {
        const { lean, status, graph } = await read(projectId);
        failures.delete(projectId);
        shown.set(projectId, status);
        again(projectId, listeningFor.get(projectId), unfinished(lean.progress) || graph.status === "running" ? pollMs : idleMs);
        return status;
      } catch (e) {
        // A failed read is tried again while the screen is open: pollMs, 2 x, 4 x ... at most 10 s. A lost sign-in is not retried.
        const lost = isDataError(e) && e.code === "unauthenticated";
        if (!lost) {
          const n = failures.get(projectId) ?? 0;
          failures.set(projectId, n + 1);
          again(projectId, listeningFor.get(projectId), Math.min(MAX_POLL_MS, pollMs * 2 ** n));
        }
        const last = shown.get(projectId);
        if (last && !lost) return last; // FR-SRC-0025.AC2: no error UI once a status has been shown
        throw e;
      }
    },
    subscribe(projectId, listener): Unsubscribe {
      const mine = listeningFor.get(projectId) ?? new Set<() => void>();
      mine.add(listener);
      listeningFor.set(projectId, mine);
      const off = changes.subscribe(projectId, listener);
      return () => {
        off();
        mine.delete(listener);
        if (mine.size === 0) {
          listeningFor.delete(projectId);
          stop(projectId);
        }
      };
    },
    async build(projectId, { reasoningLevel }) {
      let body: unknown;
      try {
        body = await guard(() => startGraphBuild(projectId, reasoningLevel));
      } catch (e) {
        const err = isDataError(e) ? e : new DataError("unknown", false);
        startFailures.set(projectId, {
          code: err.detail?.apiCode ?? err.code,
          buildId: null,
          mode: null,
          startedAt: new Date().toISOString(),
          finishedAt: null,
        });
        changes.notify(projectId);
        throw err;
      }
      epoch.set(projectId, (epoch.get(projectId) ?? 0) + 1);
      startFailures.delete(projectId);
      try {
        lastGraph.set(projectId, toGraphBuild(body)); // the 202 body is the running build; an unreadable one waits for the next poll
      } catch {
        // nothing to remember
      }
      changes.notify(projectId);
    },
    leave: (projectId) => {
      stop(projectId);
      failures.delete(projectId);
      shown.delete(projectId);
    },
  };

  // The Ready gate for Plan and Decisions (FE-PLAN-10, FE-DEC-02.AC5): Ready = at least one processed document. Everything that
  // started, retried or dismissed a browser-driven run is gone (K5); those calls are not available.
  const missionControl: MissionControlPort = {
    async snapshot(projectId): Promise<MissionControlSnapshot> {
      // Only `processed_documents` decides, so the gate works with the legacy body of an old API too (SPECS §9).
      const body: unknown = await guard(() => getSourcesStatus(projectId));
      const processed = checked(() => {
        expectShape(isRecord(body) && isCount(body.processed_documents));
        return body.processed_documents;
      });
      const ready = isReady({ processedDocuments: processed });
      try {
        const lean = toStatus(body); // feeds the project list when the body is the v2 shape
        opts.onStatus?.(projectId, projectStatusOf(lean, deriveStatus(lean)));
      } catch {
        if (ready) opts.onStatus?.(projectId, "ready");
      }
      return ready ? { phase: "ready" } : { phase: "empty" };
    },
    subscribe: () => () => undefined,
    start: () => Promise.reject(new DataError("not_available", false)),
    retry: () => Promise.reject(new DataError("not_available", false)),
    dismiss: () => Promise.reject(new DataError("not_available", false)),
    leave: () => undefined,
  };

  return { knowledge, missionControl, changes };
}
