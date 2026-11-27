"use client";
// FR-GWEB-04, FR-GRTE-06 (docs/graph/gap/api.md "User interface"): the Gaps inbox list. The first page on every filter change (with a
// loading state), "Show more" for the next one, and, after a write, when a run ends and each time a running gap run records another
// site or moves to its next step (`checkPulse`), a quiet re-read of the range already loaded: as many pages as are shown, plus the rows new gaps pushed past their end, so new gaps appear in
// their place and no shown row or loaded page goes away. A re-read keeps the shown list when it fails or brings nothing new, and an
// answer older than a newer read is dropped. The rules live in `createInbox`, which has no React in it; the hook only wires it to state.
import { useEffect, useMemo, useRef, useState } from "react";
import type { Gap, GapPage, GapQuery, GapsPort } from "@/lib/domain/gaps";
import { failureOf, gapQuery, type GapFilters } from "@/components/shared/gaps/gaps-logic";

/** The largest page `list_gaps` answers. */
const MAX_LIMIT = 200;

export interface InboxList {
  gaps: Gap[];
  next: string | null;
  phase: "loading" | "ready" | "failed" | "unavailable";
}

export const LOADING_LIST: InboxList = { gaps: [], next: null, phase: "loading" };

export interface Inbox {
  /** The first page of `query` (a filter change, "Try again"), with the loading state until it answers. */
  load: (query: GapQuery) => void;
  /** "Show more": the page after the loaded ones; a failed read leaves the button for another try. */
  more: () => void;
  /** Reads the loaded range again without a loading state. */
  refresh: () => void;
  /** Drops every answer still on its way. */
  dispose: () => void;
}

/** `rows` without the gaps already in `shown`, after them. */
function appendNew(shown: readonly Gap[], rows: readonly Gap[]): Gap[] {
  const ids = new Set(shown.map((g) => g.id));
  return [...shown, ...rows.filter((g) => !ids.has(g.id))];
}

/**
 * The first `pages` pages of `query` again, then, when gaps not in `shown` pushed shown rows past the last page, one more read of as
 * many rows as they are, so those rows stay in the list.
 */
export async function readRange(
  list: (q: GapQuery) => Promise<GapPage>,
  query: GapQuery,
  pages: number,
  shown: readonly Gap[],
): Promise<GapPage> {
  let page = await list(query);
  let rows = page.gaps;
  for (let n = 1; n < pages && page.next !== null; n++) {
    page = await list({ ...query, cursor: page.next });
    rows = appendNew(rows, page.gaps);
  }
  const known = new Set(shown.map((g) => g.id));
  const fresh = rows.filter((g) => !known.has(g.id)).length;
  if (page.next === null || fresh === 0 || rows.length - fresh >= known.size) return { gaps: rows, next: page.next };
  const rest = await list({ ...query, cursor: page.next, limit: Math.min(fresh, MAX_LIMIT) });
  return { gaps: appendNew(rows, rest.gaps), next: rest.next };
}

const sameList = (a: InboxList, b: InboxList): boolean =>
  a.phase === b.phase && a.next === b.next && JSON.stringify(a.gaps) === JSON.stringify(b.gaps);

export function createInbox(opts: { list: (q: GapQuery) => Promise<GapPage>; onList: (list: InboxList) => void }): Inbox {
  let query: GapQuery = {};
  let shown: InboxList = LOADING_LIST;
  let pages = 1;
  let latest = 0; // the id of the newest read; an answer to an older one is stale
  let reading: "load" | "more" | "refresh" | null = null;
  let again = false; // a refresh asked for while another read was on its way

  const show = (next: InboxList) => {
    if (sameList(shown, next)) return;
    shown = next;
    opts.onList(next);
  };
  /** Runs one read; its answer (a list and the pages it holds, or null to keep the shown one) counts only while it is the newest. */
  const run = (kind: "load" | "more" | "refresh", read: () => Promise<{ list: InboxList; pages: number } | null>) => {
    const id = ++latest;
    reading = kind;
    void read()
      .catch(() => null)
      .then((answer) => {
        if (id !== latest) return;
        reading = null;
        if (answer !== null) {
          pages = answer.pages;
          show(answer.list);
        }
        if (again) {
          again = false;
          refresh();
        }
      });
  };
  const refresh = () => {
    if (reading === "load") return; // the first page on its way is fresh already
    if (reading !== null) {
      again = true;
      return;
    }
    const q = query;
    const n = pages;
    const before = shown.phase === "ready" ? shown.gaps : [];
    run("refresh", () => readRange(opts.list, q, n, before).then((page) => ({ list: { ...page, phase: "ready" }, pages: n })));
  };

  return {
    load(q) {
      query = q;
      again = false;
      show(LOADING_LIST);
      run("load", () =>
        opts.list(q).then(
          (page) => ({ list: { ...page, phase: "ready" }, pages: 1 }),
          (e: unknown) => ({ list: { gaps: [], next: null, phase: failureOf(e) }, pages: 1 }),
        ),
      );
    },
    more() {
      const cursor = shown.next;
      if (shown.phase !== "ready" || cursor === null || reading === "more") return;
      if (reading === "refresh") again = true;
      const before = shown.gaps;
      const n = pages + 1;
      run("more", () =>
        opts
          .list({ ...query, cursor })
          .then((page) => ({ list: { gaps: appendNew(before, page.gaps), next: page.next, phase: "ready" }, pages: n })),
      );
    },
    refresh,
    dispose() {
      latest++;
      reading = null;
      again = false;
    },
  };
}

/**
 * The inbox of the Gaps screen: `filters` and `tries` ("Try again") read the first page; a new `version` (a write, a finished run) and a
 * new `pulse` of the running gap run (`checkPulse`: another site recorded, the next step) re-read the loaded range.
 */
export function useGapInbox(gaps: GapsPort, projectId: string, filters: GapFilters, tries: number, version: number, pulse: string | null) {
  const [list, setList] = useState<InboxList>(LOADING_LIST);
  const inbox = useMemo(() => createInbox({ list: (q) => gaps.list(projectId, q), onList: setList }), [gaps, projectId]);
  const seen = useRef(version);
  useEffect(
    () => () => {
      inbox.dispose();
    },
    [inbox],
  );
  useEffect(() => {
    inbox.load(gapQuery(filters));
  }, [inbox, filters, tries]);
  useEffect(() => {
    if (seen.current === version) return;
    seen.current = version;
    inbox.refresh();
  }, [inbox, version]);
  useEffect(() => {
    if (pulse !== null) inbox.refresh();
  }, [inbox, pulse]);
  return { list, more: inbox.more };
}
