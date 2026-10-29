// FR-GDUI-0018, FR-GDUI-0021, FR-SRC-0007, FR-SRC-0012, FR-SRC-0025 (GDRIVE plan 07; sources-ux SPECS §7.3): the production
// `KnowledgePort` and the Ready gate, both from ONE read of `GET /projects/{id}/sources?batches=1`.
//  - The screen's rich `ProjectSourcesStatus` is derived from the v2 status: `sources[]` (Drive folders and Local files batches,
//    in added order), `progress` -> the Build knowledge lock (FR-GDUI-0021), `removed_files` -> `changes.removed`.
//    The body is parsed strictly: any other shape (the legacy body of an old API included) is `invalid_response`.
//  - Ready (owner): a project is Ready when it has at least one processed document (`processed_documents` >= 1). Plan and Decisions
//    read it through `missionControl.snapshot` (components/shell/use-ready-gate.ts), which reads ONLY that field, so the gate works
//    with either body shape. The rich status keeps `ready: false` and `lastBuild: null`: no knowledge build exists in production (K1).
//  - `build` never runs a summary step; after the unlock a click shows the existing "not available yet" message (Q12): it rejects
//    with `DataError("not_available")`, which the screen turns into that toast.
//  - Polling: while a screen listens, every `pollMs` (3 s) while the import is unfinished and every `idleMs` (5 s) otherwise (R13: a
//    change made elsewhere shows within 10 s); a status read that fails is retried with a back-off (3 s, 6 s, then 10 s), and not
//    at all on `unauthenticated`. Changes made by this app (save, upload, remove) notify at once.
//  - A read that fails (or is `invalid_response`) after a status was shown keeps that status: no error UI (FR-SRC-0025.AC2, R25).
import { getSourcesStatus } from "@/lib/api";
import type { Unsubscribe } from "@/lib/domain/common";
import { DataError, isDataError } from "@/lib/domain/errors";
import type { MissionControlPort, MissionControlSnapshot } from "@/lib/domain/pipeline";
import type { ProjectStatus } from "@/lib/domain/projects";
import type { ImportProgress, KnowledgePhase, KnowledgePort, ProjectSourcesStatus, Source } from "@/lib/domain/sources";
import { checked, expectShape, guard, isRecord } from "./errors";
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

/** Pure: the v2 status -> the screen's status (see the header for what stays fixed in production). */
export function deriveStatus(s: StatusV2): ProjectSourcesStatus {
  const locked = unfinished(s.progress);
  const failedSources = s.sources.filter((c) => c.failure !== null).length;
  const failedFiles = sum(s.sources, (c) => c.counts.failed);
  const nextBuild = !locked && s.sources.length > 0 && s.processedDocuments > 0 ? ("first" as const) : null;
  let phase: KnowledgePhase;
  if (s.sources.length === 0) phase = "no_sources";
  else if (locked) phase = "collecting";
  else if (failedSources > 0 || failedFiles > 0) phase = "attention";
  else phase = nextBuild ? "ready_to_build" : "collecting";
  return {
    phase,
    ready: false, // no knowledge build in production (K1); the Ready gate is `isReady`
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
    build: null,
    lastBuild: null,
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

  const listeningFor = new Map<string, Set<() => void>>(); // this port's own screen listeners (the poll runs only for them)

  async function read(projectId: string): Promise<{ lean: StatusV2; status: ProjectSourcesStatus }> {
    const body: unknown = await guard(() => getSourcesStatus(projectId));
    const lean = toStatus(body);
    const status = deriveStatus(lean);
    opts.onStatus?.(projectId, projectStatusOf(lean, status));
    return { lean, status };
  }

  const knowledge: KnowledgePort = {
    buildAvailable: false,
    async status(projectId) {
      try {
        const { lean, status } = await read(projectId);
        failures.delete(projectId);
        shown.set(projectId, status);
        again(projectId, listeningFor.get(projectId), unfinished(lean.progress) ? pollMs : idleMs);
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
    build: () => Promise.reject(new DataError("not_available", false)), // Q12: no build in production; never the summary step (K1)
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
