// Phase 1 Mission Control sources (DESIGN D4, D6-D9; owner redesign 2026-10-15): the pure rules of the new screen. One build button
// on the header row, and the source cards. No React.
import { sourceState } from "@/lib/domain/source-state";
import type { Connection, ProjectSourcesStatus } from "@/lib/domain/sources";
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

export const REBUILD_LABEL = "Rebuild from scratch";

export interface BuildButtonView {
  label: string;
  /** What a click starts; null = the button is off (aria-disabled) and only says the state. */
  mode: "first" | "update" | null;
  /** The detail per state (counts, changes, last build): the button's tooltip and its accessible description. */
  detail: string;
  /** "⋯" with Rebuild from scratch (after the first build). */
  menu: boolean;
}

const syncingCount = (s: ProjectSourcesStatus) => s.sources.filter((src) => sourceState(src) === "processing").length;
const sourceCount = (s: ProjectSourcesStatus) => s.sources.length;

/** "4 new · 2 changed · 1 removed" (zero parts left out). */
export function changesText(c: ProjectSourcesStatus["changes"]): string {
  return [
    c.new ? `${String(c.new)} new` : "",
    c.changed ? `${String(c.changed)} changed` : "",
    c.removed ? `${String(c.removed)} removed` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

/**
 * BUILD KNOWLEDGE -> BUILDING 18/42 -> UP TO DATE -> UPDATE KNOWLEDGE (owner, 2026-10-15). Off while sources sync (it waits), while a
 * build runs, and when nothing changed. The design's detail per state ("42 documents from 3 sources · 2 skipped", …) is the tooltip.
 */
export function buildButtonView(s: ProjectSourcesStatus, now: Date = new Date()): BuildButtonView {
  // FR-GDUI-0021: while a source lists or a file is not finished, the button is off under any label and its tooltip says why.
  const lock = importLock(s.progress);
  const v = stateButtonView(s, now);
  return truthy(lock) ? { ...v, mode: null, detail: lock } : v;
}

/** A build runs: its counter is the label, its current step the detail. */
function runningView(build: NonNullable<ProjectSourcesStatus["build"]>): BuildButtonView {
  const unit = build.mode === "update" ? "change" : "document";
  const current = truthy(build.current) ? ` · now: ${build.current}` : "";
  return {
    label: `Building ${String(build.completed)}/${String(build.total)}`,
    mode: null,
    detail: `${String(build.completed)} of ${plural(build.total, unit)}${current}`,
    menu: false,
  };
}

/** Something still syncs: normally the import lock answers first; this is the fallback when the progress says finished. */
function syncingView(s: ProjectSourcesStatus, syncing: number): BuildButtonView {
  const who = syncing === 1 ? "1 source finishes" : `${String(syncing)} sources finish`;
  return {
    label: s.ready ? "Update knowledge" : "Build knowledge",
    mode: null,
    detail: `Waits until ${who} syncing · ${plural(s.totals.documents, "document")} so far`,
    menu: s.ready,
  };
}

function firstBuildView(s: ProjectSourcesStatus): BuildButtonView {
  const skipped = s.totals.skipped ? ` · ${String(s.totals.skipped)} skipped` : "";
  return {
    label: "Build knowledge",
    mode: "first",
    detail: `${plural(s.totals.documents, "document")} from ${plural(sourceCount(s), "source")}${skipped}`,
    menu: false,
  };
}

function stateButtonView(s: ProjectSourcesStatus, now: Date): BuildButtonView {
  if (s.build) return runningView(s.build);
  if (sourceCount(s) === 0) return { label: "Build knowledge", mode: null, detail: "Add a source first.", menu: false };
  const syncing = syncingCount(s);
  if (syncing > 0) return syncingView(s, syncing);
  if (s.nextBuild === "first") return firstBuildView(s);
  const built = s.lastBuild
    ? `built ${when(s.lastBuild.finishedAt ?? s.lastBuild.startedAt, now)} from ${plural(s.lastBuild.documentCount, "document")}`
    : "";
  if (s.nextBuild === "update")
    return { label: "Update knowledge", mode: "update", detail: [changesText(s.changes), built].filter(Boolean).join(" · "), menu: true };
  if (s.ready) return { label: "Up to date", mode: null, detail: built.charAt(0).toUpperCase() + built.slice(1), menu: true };
  return { label: "Build knowledge", mode: null, detail: "No documents to build yet.", menu: false };
}

// ---- Sources ----

export const LOCAL_FILES = "Local files";

/** GitHub (demo build): the repository can be synced when it is at rest; a failed Drive import is saved again, not synced. */
export const canSyncNow = (c: Pick<Connection, "state" | "error"> & Partial<Pick<Connection, "failure">>): boolean =>
  c.state === "synced" || (c.state === "error" && c.error !== "unsupported_url" && !c.failure);
