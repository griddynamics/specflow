// Phase 1 Mission Control sources (DESIGN D4, D6-D9; owner redesign 2026-10-15; plans/connect-build-kg-button): the pure rules of the
// screen: the build button on the header row (its state, the confirm estimate, the error report) and the source cards. No React.
import { sourceState } from "@/lib/domain/source-state";
import type { BuildFailure, Connection, ProjectSourcesStatus, ReasoningLevel } from "@/lib/domain/sources";
import { importLock } from "./drive-logic";
import { truthy } from "@/components/ui/utils";

export const plural = (n: number, one: string, many = `${one}s`): string => `${String(n)} ${n === 1 ? one : many}`;

// ---- Time ----

const pad = (n: number) => String(n).padStart(2, "0");
export const clock = (iso: string): string => {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

/** "today 10:42", "yesterday 10:42" or "14 Oct 10:42" (local time). */
export function when(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (sameDay(d, now)) return `today ${clock(iso)}`;
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (sameDay(d, y)) return `yesterday ${clock(iso)}`;
  return `${DAY.format(d)} ${clock(iso)}`;
}

// ---- The build button (owner: one button beside the project dropdown; the label carries the state) ----

export interface BuildButtonView {
  label: string;
  /** What a click (or "Try again" after an error) starts; null = it starts nothing. */
  mode: "first" | "update" | null;
  /** The detail per state (counts, last build, error): the button's tooltip and its accessible description. */
  detail: string;
  tone: "primary" | "off" | "danger";
  /** A build runs: the button is off and spins. */
  busy: boolean;
  /** The last build failed: the button is red and opens the error details. */
  failed: boolean;
  /** Set only when `failed`: why "Try again" is off. */
  blocked: string | null;
}

const syncingCount = (s: ProjectSourcesStatus) => s.sources.filter((src) => sourceState(src) === "processing").length;
const sourceCount = (s: ProjectSourcesStatus) => s.sources.length;

const offView = (label: string, detail: string): BuildButtonView => ({
  label,
  mode: null,
  detail,
  tone: "off",
  busy: false,
  failed: false,
  blocked: null,
});

/**
 * BUILD KNOWLEDGE -> BUILDING… -> UPDATE KNOWLEDGE, or ERROR (a failed build or a refused start). Off while sources sync (it waits)
 * and while a build runs; after a success it stays active. The design's detail per state ("42 documents from 3 sources · 2 skipped",
 * …) is the tooltip.
 */
export function buildButtonView(s: ProjectSourcesStatus, now: Date = new Date(), pending = false): BuildButtonView {
  // `pending`: a build was just confirmed and no status shows it yet; the button already looks busy.
  if (s.build || pending) return runningView(s.build, now);
  const base = stateButtonView(s, now);
  // FR-GDUI-0021: while a source lists or a file is not finished, no build starts under any label and the tooltip says why.
  const lock = importLock(s.progress);
  const mode = truthy(lock) ? null : base.mode;
  if (s.buildFailure) {
    return {
      label: "Error",
      mode,
      detail: `The build failed (${s.buildFailure.code}). Open for details.`,
      tone: "danger",
      busy: false,
      failed: true,
      blocked: lock ?? (mode === null ? base.detail : null),
    };
  }
  return truthy(lock) ? { ...base, mode: null, tone: "off", detail: lock } : base;
}

/** A build runs: "Started 10:42 · 3 min so far". The import lock does not apply: the build already started. */
function runningView(build: ProjectSourcesStatus["build"], now: Date): BuildButtonView {
  const startedAt = build?.startedAt ?? null;
  const started = startedAt === null ? Number.NaN : Date.parse(startedAt);
  let detail = "Building knowledge";
  if (startedAt !== null && !Number.isNaN(started)) {
    const minutes = Math.floor((now.getTime() - started) / 60_000);
    const elapsed = minutes < 1 ? "less than a minute" : `${String(minutes)} min`;
    detail = `Started ${clock(startedAt)} · ${elapsed} so far`;
  }
  return { ...offView("Building…", detail), busy: true };
}

/** Something still syncs: normally the import lock answers first; this is the fallback when the progress says finished. */
function syncingView(s: ProjectSourcesStatus, syncing: number): BuildButtonView {
  const who = syncing === 1 ? "1 source finishes" : `${String(syncing)} sources finish`;
  return offView(
    s.ready ? "Update knowledge" : "Build knowledge",
    `Waits until ${who} syncing · ${plural(s.totals.documents, "document")} so far`,
  );
}

function firstBuildView(s: ProjectSourcesStatus): BuildButtonView {
  const skipped = s.totals.skipped ? ` · ${String(s.totals.skipped)} skipped` : "";
  return {
    ...offView("Build knowledge", `${plural(s.totals.documents, "document")} from ${plural(sourceCount(s), "source")}${skipped}`),
    mode: "first",
    tone: "primary",
  };
}

function updateView(s: ProjectSourcesStatus, now: Date): BuildButtonView {
  const last = s.lastBuild;
  const at = last ? (last.finishedAt ?? last.startedAt) : null;
  const detail = [
    at === null || Number.isNaN(Date.parse(at)) ? "" : `Built ${when(at, now)}`,
    last?.state === "partially_succeeded" ? "Last build finished with some problems" : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return { ...offView("Update knowledge", detail || "Ready to update"), mode: "update", tone: "primary" };
}

/** Rows 3-7 of the button table: the view without the build failure and the import lock. */
function stateButtonView(s: ProjectSourcesStatus, now: Date): BuildButtonView {
  if (sourceCount(s) === 0) return offView("Build knowledge", "Add a source first.");
  const syncing = syncingCount(s);
  if (syncing > 0) return syncingView(s, syncing);
  if (s.nextBuild === "first") return firstBuildView(s);
  if (s.nextBuild === "update") return updateView(s, now);
  return offView(s.ready ? "Update knowledge" : "Build knowledge", "No documents to build yet.");
}

// ---- The build dialogs ----

/** The reasoning level names; the level is the index + 1. */
export const REASONING_LEVELS = ["Low", "Medium", "High", "Extra high", "Max"] as const;
/** Time factor per level (Medium = 1). Calibrated on a 598 s corpus of about 25 documents; these constants exist only here. */
const FACTOR = [0.6, 1.0, 1.4, 2.0, 2.8] as const;

/** "about 4–9 minutes", or "more than an hour" above 60: a rough range from the document count and the level. */
export function estimateText(documents: number, level: ReasoningLevel): string {
  const base = (2 + 0.4 * documents) * FACTOR[level - 1];
  const low = Math.max(1, Math.round(base * 0.7));
  const high = Math.max(low + 1, Math.round(base * 1.5));
  return high > 60 ? "more than an hour" : `about ${String(low)}–${String(high)} minutes`;
}

const iso = (t: string | null): string => {
  const d = t === null ? null : new Date(t);
  return d === null || Number.isNaN(d.getTime()) ? "—" : d.toISOString();
};

/** The copyable text of a failed build: ids, codes and times only, never server text or file names. `page` has no query and no hash. */
export function buildErrorReport(projectId: string, f: BuildFailure, page: string): string {
  return [
    "Specflow knowledge build failed",
    `Project: ${projectId}`,
    `Build: ${f.buildId ?? "—"}`,
    `Mode: ${f.mode ?? "—"}`,
    `Error code: ${f.code}`,
    `Started (UTC): ${iso(f.startedAt)}`,
    `Finished (UTC): ${iso(f.finishedAt)}`,
    `Page: ${page}`,
  ].join("\n");
}

// ---- Sources ----

export const LOCAL_FILES = "Local files";

/** GitHub (demo build): the repository can be synced when it is at rest; a failed Drive import is saved again, not synced. */
export const canSyncNow = (c: Pick<Connection, "state" | "error"> & Partial<Pick<Connection, "failure">>): boolean =>
  c.state === "synced" || (c.state === "error" && c.error !== "unsupported_url" && !c.failure);
