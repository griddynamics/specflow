// FR-GWEB-04, FR-GRTE-06: the live Gaps inbox. Which gaps are New (made by the latest gap run, at or after it started), when a running
// run asks the inbox to read again (`checkPulse`), and the inbox list (`createInbox`, against an in-memory `list_gaps`): the first page,
// "Show more", the quiet re-read of the loaded range (new gaps in place, no shown row or loaded page lost) and which answers count.
import { test } from "node:test";
import assert from "node:assert/strict";
import { checkPulse, isNewGap, newListedText } from "@/components/shared/gaps/gaps-logic";
import { LOADING_LIST, createInbox, readRange, type InboxList } from "@/components/gaps/use-gap-inbox";
import { DataError } from "@/lib/domain/errors";
import type { Gap, GapCheck, GapPage, GapQuery } from "@/lib/domain/gaps";

const STARTED = "2026-11-12T09:00:00Z";
const gap = (id: string, over: Partial<Gap> = {}): Gap => ({
  id,
  kind: "missing",
  severity: "major",
  status: "open",
  origin: "analysis",
  route: "ask",
  title: id,
  question: `${id}?`,
  why: "",
  blocking: false,
  createdAt: "2026-11-10T10:00:00Z",
  updatedAt: "2026-11-10T10:00:00Z",
  about: [],
  impacts: [],
  witness: [],
  options: [],
  clarification: null,
  reach: null,
  history: null,
  assignedTo: null,
  ...over,
});
const check = (over: Partial<GapCheck> = {}): GapCheck => ({
  phase: "running",
  step: "investigate",
  stepsDone: 5,
  stepsTotal: 7,
  itemsDone: 3,
  itemsTotal: 12,
  startedAt: STARTED,
  finishedAt: null,
  newGaps: null,
  updatedGaps: null,
  resolvedGaps: null,
  errorCode: null,
  ...over,
});

// ---- New ----

test("FR-GRTE-06: a gap is New when the latest run made it: at or after the run started, not before", () => {
  assert.equal(isNewGap(gap("A", { createdAt: "2026-11-12T08:59:59Z" }), check()), false);
  assert.equal(isNewGap(gap("A", { createdAt: STARTED }), check()), true);
  assert.equal(isNewGap(gap("A", { createdAt: "2026-11-12T09:00:00.250+00:00" }), check()), true);
  assert.equal(isNewGap(gap("A", { createdAt: "2026-11-12T09:07:00Z" }), check()), true);
});

test("FR-GRTE-06: nothing is New before the first run or before the check is read; an unreadable time is not New", () => {
  const made = gap("A", { createdAt: "2026-11-12T09:07:00Z" });
  assert.equal(isNewGap(made, null), false);
  assert.equal(isNewGap(made, check({ phase: "never", startedAt: null })), false);
  assert.equal(isNewGap(made, check({ startedAt: "soon" })), false);
  assert.equal(isNewGap(gap("A", { createdAt: "" }), check()), false);
});

test("FR-GRTE-06: New stays after the run ends (done, degraded, failed, the next build) and goes when the next run starts", () => {
  const made = gap("A", { createdAt: "2026-11-12T09:07:00Z" });
  for (const phase of ["done", "degraded", "failed", "building"] as const)
    assert.equal(isNewGap(made, check({ phase, finishedAt: "2026-11-12T09:20:00Z" })), true, phase);
  assert.equal(isNewGap(made, check({ startedAt: "2026-11-13T09:00:00Z" })), false);
});

test("FR-GRTE-06: a gap a person or a coding agent raised during the run is not New: only the run's own gaps are", () => {
  const at = { createdAt: "2026-11-12T09:07:00Z" };
  assert.equal(isNewGap(gap("A", { ...at, origin: "person" }), check()), false);
  assert.equal(isNewGap(gap("A", { ...at, origin: "development" }), check()), false);
});

test("FR-GRTE-06: the running check counts the New rows listed", () => {
  assert.equal(newListedText(1), "1 new gap listed so far");
  assert.equal(newListedText(4), "4 new gaps listed so far");
});

test("FR-GRTE-06: a running run asks the inbox to read again when it records another site or moves to its next step, never otherwise", () => {
  const pulse = checkPulse(check());
  assert.notEqual(pulse, null);
  assert.equal(checkPulse(check()), pulse, "the same progress: no new read");
  assert.notEqual(checkPulse(check({ itemsDone: 4 })), pulse, "a site recorded");
  assert.notEqual(checkPulse(check({ step: "finish", stepsDone: 6, itemsDone: null, itemsTotal: null })), pulse, "the next step");
  for (const phase of ["building", "queued", "done", "degraded", "failed", "never"] as const)
    assert.equal(checkPulse(check({ phase })), null, phase);
  assert.equal(checkPulse(null), null);
});

// ---- the inbox list ----

/** An in-memory `list_gaps`: pages of `size` (or `limit`), the cursor an offset, as the stub API; `calls` keeps each query. */
function server(rows: Gap[], size = 2) {
  const calls: GapQuery[] = [];
  const list = (q: GapQuery): Promise<GapPage> => {
    calls.push(q);
    const from = Number(q.cursor ?? 0);
    const n = q.limit ?? size;
    return Promise.resolve({ gaps: rows.slice(from, from + n), next: from + n < rows.length ? String(from + n) : null });
  };
  return { rows, calls, list };
}
const ids = (l: InboxList) => l.gaps.map((g) => g.id);
const settle = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
};
const Q: GapQuery = { status: ["open"], route: ["ask"] };

function setup(rows: Gap[], list?: (q: GapQuery) => Promise<GapPage>) {
  const s = server(rows);
  const seen: InboxList[] = [];
  const inbox = createInbox({ list: list ?? s.list, onList: (l) => seen.push(l) });
  const shown = () => seen.at(-1) ?? LOADING_LIST;
  return { s, seen, inbox, shown };
}
const done = (inbox: { dispose: () => void }) => {
  inbox.dispose();
};

test("FR-GWEB-04: the first page shows after the loading state; Show more adds the next page; a failed first page is failed or unavailable", async () => {
  const { s, seen, inbox, shown } = setup(["A", "B", "C"].map((id) => gap(id)));
  inbox.load(Q);
  await settle();
  assert.deepEqual(ids(shown()), ["A", "B"]);
  assert.equal(shown().next, "2");
  inbox.more();
  await settle();
  assert.deepEqual(ids(shown()), ["A", "B", "C"]);
  assert.equal(shown().next, null);
  assert.deepEqual(s.calls, [Q, { ...Q, cursor: "2" }]);
  assert.equal(seen.length, 2, "the two answers; the loading state was already shown");
  done(inbox);

  const failed = setup([], () => Promise.reject(new DataError("unavailable", true, { status: 503, apiCode: "gap_unavailable" })));
  failed.inbox.load(Q);
  await settle();
  assert.equal(failed.shown().phase, "unavailable");
  done(failed.inbox);
});

test("FR-GRTE-06: a re-read reads the loaded range again; a new gap shows in its place and every loaded page stays", async () => {
  const { s, inbox, shown } = setup(["A", "B", "C", "D", "E"].map((id) => gap(id)));
  inbox.load(Q);
  await settle();
  inbox.more();
  await settle();
  assert.deepEqual(ids(shown()), ["A", "B", "C", "D"]);
  s.rows.splice(1, 0, gap("NEW")); // the run records a gap that sorts second
  s.calls.length = 0;
  inbox.refresh();
  await settle();
  assert.deepEqual(ids(shown()), ["A", "NEW", "B", "C", "D"], "D, pushed past the second page, stays");
  assert.deepEqual(s.calls, [Q, { ...Q, cursor: "2" }, { ...Q, cursor: "4", limit: 1 }]);
  assert.equal(shown().next, "5");
  assert.equal(shown().phase, "ready");
  inbox.more();
  await settle();
  assert.deepEqual(ids(shown()), ["A", "NEW", "B", "C", "D", "E"]);
  done(inbox);
});

test("FR-GRTE-06: a re-read that brings nothing new shows nothing new; a failed one keeps the list; after dispose nothing shows", async () => {
  let fail = false;
  const s = server(["A", "B", "C"].map((id) => gap(id)));
  const { seen, inbox, shown } = setup([], (q) => (fail ? Promise.reject(new Error("offline")) : s.list(q)));
  inbox.load(Q);
  await settle();
  const count = seen.length;
  inbox.refresh();
  await settle();
  assert.equal(seen.length, count, "the same rows: no new list");
  fail = true;
  inbox.refresh();
  await settle();
  assert.deepEqual(ids(shown()), ["A", "B"]);
  assert.equal(shown().phase, "ready");
  fail = false;
  s.rows.unshift(gap("NEW"));
  inbox.refresh();
  inbox.dispose();
  await settle();
  assert.equal(seen.length, count, "the answer after dispose is dropped");
});

test("FR-GWEB-04: a re-read answer older than a filter change is dropped; one asked during Show more runs after it", async () => {
  const waiting: { q: GapQuery; resolve: (p: GapPage) => void }[] = [];
  const { inbox, shown } = setup([], (q) => new Promise<GapPage>((resolve) => waiting.push({ q, resolve })));
  inbox.load(Q);
  waiting.shift()?.resolve({ gaps: [gap("A")], next: "1" });
  await settle();
  inbox.refresh();
  const old = waiting.shift();
  inbox.load({ ...Q, kind: ["missing"] });
  assert.equal(shown().phase, "loading");
  old?.resolve({ gaps: [gap("STALE")], next: null });
  await settle();
  assert.equal(shown().phase, "loading", "the stale re-read is dropped");
  waiting.shift()?.resolve({ gaps: [gap("M")], next: "1" });
  await settle();
  assert.deepEqual(ids(shown()), ["M"]);

  inbox.more();
  inbox.refresh();
  assert.equal(waiting.length, 1, "the re-read waits for Show more");
  waiting.shift()?.resolve({ gaps: [gap("N")], next: null });
  await settle();
  assert.deepEqual(ids(shown()), ["M", "N"]);
  assert.deepEqual(
    waiting.map((w) => w.q),
    [{ ...Q, kind: ["missing"] }],
    "then the loaded range is read again",
  );
  waiting.shift()?.resolve({ gaps: [gap("M")], next: "1" });
  await settle();
  assert.deepEqual(
    waiting.map((w) => w.q),
    [{ ...Q, kind: ["missing"], cursor: "1" }],
  );
  waiting.shift()?.resolve({ gaps: [gap("N")], next: null });
  await settle();
  assert.deepEqual(ids(shown()), ["M", "N"]);
  done(inbox);
});

test("FR-GRTE-06: readRange reads as many pages as are loaded and, only when new gaps pushed loaded rows out, as many rows more", async () => {
  const s = server(["A", "B", "C", "D"].map((id) => gap(id)));
  const shown = [gap("A"), gap("B")];
  assert.deepEqual(ids({ ...(await readRange(s.list, Q, 1, shown)), phase: "ready" }), ["A", "B"]);
  s.rows.unshift(gap("N1"), gap("N2"));
  s.calls.length = 0;
  const page = await readRange(s.list, Q, 1, shown);
  assert.deepEqual(
    page.gaps.map((g) => g.id),
    ["N1", "N2", "A", "B"],
  );
  assert.deepEqual(s.calls, [Q, { ...Q, cursor: "2", limit: 2 }]);
  s.rows.splice(0, s.rows.length, gap("A"));
  assert.deepEqual(await readRange(s.list, Q, 3, shown), { gaps: [gap("A")], next: null }, "a removed row ends the read with the list");
});
