// FR-SRC-0009, 0012, 0025 (INT-SRC-0002): the reader behind the details modal's file list, without React, so it runs in node too.
// The first page loads at once; the next page when `loadMore` is called; every `refreshMs` the loaded
// range is read again from the first page, in pages, and replaces the rows in one step (rows are keyed by file id, so the screen keeps its
// fold state and scroll position). A failed refresh keeps the last rows; a source that is gone ends the reader with `removed`. `stop`
// clears the timer and drops every answer still on its way.
import { isDataError } from "@/lib/domain/errors";
import { PAGE_LIMIT } from "@/lib/domain/source-state";
import type { FilePage, Source, SourceFile, SourcesPort } from "@/lib/domain/sources";
import { truthy } from "@/components/ui/utils";

/** FR-SRC-0012: a change shows within 10 s; the read repeats every 5 s, so a change shows within 5 s plus one request (SPECS §2). */
export const REFRESH_MS = 5_000;

export interface FilesState {
  phase: "loading" | "ready" | "error";
  /** The source as the last read saw it (header, counts); null before the first read. */
  source: Source | null;
  items: SourceFile[];
  hasMore: boolean;
  /** The next page is on its way. */
  loadingMore: boolean;
  /** The last page asked for failed: the list shows a retry (FR-SRC-0025.AC2) instead of a loading row that never ends. */
  moreFailed: boolean;
  /** The source is gone (another member removed it): FR-SRC-0025.AC3. */
  removed: boolean;
}

export const INITIAL: FilesState = {
  phase: "loading",
  source: null,
  items: [],
  hasMore: false,
  loadingMore: false,
  moreFailed: false,
  removed: false,
};

export interface FileReader {
  loadMore(): void;
  stop(): void;
}

/** The port's rejection for a source that is not there any more. */
export const isGone = (e: unknown): boolean => isDataError(e) && e.code === "not_found" && e.detail?.apiCode === "source_not_found";

const dedupe = (rows: SourceFile[]): SourceFile[] => {
  const seen = new Set<string>();
  return rows.filter((f) => (seen.has(f.id) ? false : (seen.add(f.id), true)));
};

export function startFileReader(
  port: SourcesPort,
  projectId: string,
  sourceId: string,
  emit: (s: FilesState) => void,
  refreshMs: number = REFRESH_MS,
): FileReader {
  let alive = true;
  const isAlive = () => alive; // read through a call: it changes while a read is awaited
  let busy = false;
  let wantMore = false; // a page was asked for while a read ran
  let cursor: string | null = null;
  let state: FilesState = INITIAL;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const set = (patch: Partial<FilesState>) => {
    state = { ...state, ...patch };
    if (isAlive()) emit(state);
  };
  const apply = (page: Pick<FilePage, "source">, items: SourceFile[], next: string | null) => {
    cursor = next;
    set({ source: page.source, items, hasMore: next !== null });
  };
  // A read ends here: a page that was asked for meanwhile runs now, and the refresh is planned.
  const done = () => {
    busy = false;
    if (isAlive() && wantMore && truthy(cursor)) {
      wantMore = false;
      void next();
    } else schedule();
  };
  // true = the source is gone and the reader ended
  const gone = (e: unknown): boolean => {
    if (!isGone(e)) return false;
    alive = false;
    clearTimeout(timer);
    state = { ...state, removed: true, loadingMore: false };
    emit(state);
    return true;
  };

  async function first() {
    busy = true;
    try {
      const page = await port.files(projectId, sourceId, null);
      if (!isAlive()) return;
      apply(page, page.items, page.nextCursor);
      set({ phase: "ready" });
    } catch (e) {
      if (!isAlive()) return;
      if (!gone(e)) set({ phase: "error" }); // FR-SRC-0025.AC1: the first read failed
    }
    if (isAlive()) done();
  }

  async function next() {
    if (busy || !truthy(cursor) || !isAlive()) return;
    busy = true;
    set({ loadingMore: true, moreFailed: false });
    try {
      const page = await port.files(projectId, sourceId, cursor);
      if (!isAlive()) return;
      const seen = new Set(state.items.map((f) => f.id));
      apply(page, [...state.items, ...page.items.filter((f) => !seen.has(f.id))], page.nextCursor);
    } catch (e) {
      if (isAlive() && !gone(e)) set({ moreFailed: true }); // the rows stay; the list offers "Try again"
    }
    if (isAlive()) {
      set({ loadingMore: false });
      done();
    }
  }

  // FR-SRC-0012: read the loaded range again, from the first page, as far as the rows reach (at least one page).
  async function refresh() {
    if (!isAlive()) return;
    if (busy || state.phase !== "ready") {
      schedule();
      return;
    }
    busy = true;
    const want = Math.max(state.items.length, PAGE_LIMIT);
    try {
      let page = await port.files(projectId, sourceId, null);
      let rows = [...page.items];
      while (truthy(page.nextCursor) && rows.length < want) {
        page = await port.files(projectId, sourceId, page.nextCursor);
        if (!isAlive()) return;
        rows = rows.concat(page.items);
      }
      if (!isAlive()) return;
      apply(page, dedupe(rows), page.nextCursor); // the last page read carries the freshest header
    } catch (e) {
      if (isAlive()) gone(e); // any other failure keeps what is shown (FR-SRC-0025.AC2); the next tick tries again
    }
    if (isAlive()) done();
  }

  function schedule() {
    clearTimeout(timer);
    if (isAlive()) timer = setTimeout(() => void refresh(), refreshMs);
  }

  emit(state);
  void first();
  return {
    loadMore() {
      if (!isAlive() || state.phase !== "ready" || !truthy(cursor)) return;
      if (!busy) void next();
      else if (!state.loadingMore) wantMore = true; // a read is running: the page follows it (a page already on its way is not asked twice)
    },
    stop() {
      alive = false;
      clearTimeout(timer);
    },
  };
}
