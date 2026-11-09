// plans/connect-build-kg-button (CTORNDGAIN-2244 UI, CTORNDGAIN-2243 API): the "Build knowledge" button against `/graph_build`.
// Layer U (pure): toGraphBuild, deriveStatus merge rows, buildButtonView rows 1-7 and the import lock, estimateText, buildErrorReport.
// Layer P (port vs the stub API through the real BFF): the parallel reads, poll cadence, build() body and epoch, start failures, the stub itself.
// Titles name the acceptance criterion (SPECS §10). Faults are injected in the fetch layer (withHook); no sleeps, only waitFor / a fake clock.
// Ports 3264-3265 (the stub issuer on 3285, startEnv: API port + 20).
import { test, before, after, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import { createHttpAdapter, type HttpPorts } from "@/lib/data/http";
import { NO_GRAPH, deriveStatus, toGraphBuild, toStatus, type GraphBuildWire } from "@/lib/data/http/knowledge";
import { isDataError, type DataError } from "@/lib/domain/errors";
import type { BuildFailure, ProjectSourcesStatus, ReasoningLevel } from "@/lib/domain/sources";
import {
  REASONING_LEVELS,
  buildButtonView,
  buildErrorReport,
  clock,
  estimateText,
  when,
} from "@/components/mission-control/sources/kb-logic";
import { defined } from "../support/defined";
import { GRAPH_DEFAULTS, EXISTING_ENDPOINTS, jsonResponse, pdfFile, startEnv, waitFor, withHook, type Env } from "./p16-support";

let env: Env;
before(async () => {
  env = await startEnv(3264, 3265);
  env.api.setTiming({ parseMs: 50, summarizeMs: 50, parseOutcome: "parsed" });
});
after(async () => {
  await env.stop();
});
afterEach(() => {
  env.api.setTiming(GRAPH_DEFAULTS);
  env.hooks.length = 0;
});

// ---- fixtures ----

const BUILD_LABEL = "Build knowledge";
const COUNTS = { waiting: 0, processing: 0, done: 0, not_read: 0, skipped: 0, failed: 0, already_in_project: 0 };
const wireSource = (counts: Partial<typeof COUNTS> = { done: 4 }, over: Record<string, unknown> = {}) => {
  const c = { ...COUNTS, ...counts };
  return {
    id: "s1",
    kind: "upload",
    name: "Local files",
    name_suffix: 1,
    added_by: { id: "u1", display_name: "dev" },
    created_at: "2026-10-26T08:00:00Z",
    listing_finished: true,
    open: false,
    total: Object.values(c).reduce((n, x) => n + x, 0),
    counts: c,
    failure_reason: null,
    folder_id: null,
    link: null,
    ...over,
  };
};
/** A v2 status body; `processed` documents, `sources` default to one finished source of 4 done files. */
const v2 = (sources: ReturnType<typeof wireSource>[] = [wireSource()], processed = 4) => {
  const total = sources.reduce((n, s) => n + s.total, 0);
  const open = sources.reduce((n, s) => n + s.counts.waiting + s.counts.processing, 0);
  return {
    progress: { listing_finished: sources.every((s) => s.listing_finished), total, finished: total - open },
    processed_documents: processed,
    removed_files: 0,
    sources,
  };
};
const lean = (sources?: ReturnType<typeof wireSource>[], processed?: number) => toStatus(v2(sources, processed));

const STARTED = "2026-10-26T09:00:00Z";
const FINISHED = "2026-10-26T09:12:00Z";
const wire = (over: Partial<GraphBuildWire> = {}): GraphBuildWire => ({ ...NO_GRAPH.graph, ...over });
const running = wire({ status: "running", buildId: "gb-1", mode: "first", startedAt: STARTED });
const succeeded = wire({ status: "succeeded", buildId: "gb-1", mode: "first", startedAt: STARTED, finishedAt: FINISHED });
const failedGraph = wire({ status: "failed", buildId: "gb-2", mode: "first", startedAt: STARTED, finishedAt: FINISHED, errorCode: "boom" });
const startFailure = (over: Partial<BuildFailure> = {}): BuildFailure => ({
  code: "graph_build_unavailable",
  buildId: null,
  mode: null,
  startedAt: "2026-10-26T10:00:00Z",
  finishedAt: null,
  ...over,
});
const derive = (graph: GraphBuildWire, input: { sf?: BuildFailure | null; remembered?: boolean } = {}, l = lean()) =>
  deriveStatus(l, { graph, startFailure: input.sf ?? null, rememberedReady: input.remembered ?? false });

async function rejection(p: Promise<unknown>): Promise<DataError> {
  try {
    await p;
  } catch (e) {
    assert.ok(isDataError(e), `a DataError, got ${String(e)}`);
    return e;
  }
  assert.fail("expected a rejection");
}
const newProject = async (http: HttpPorts, name: string) => (await http.createProject.create({ name, contributorIds: [] })).id;
/** A project with one parsed document, so a build can start. */
async function projectWithDoc(http: HttpPorts, name: string): Promise<string> {
  const pid = await newProject(http, name);
  await http.sources.upload(pid, [pdfFile(`${name}.pdf`, 1200)]);
  await waitFor("the stub parsed the upload", () => [...env.api.docs.values()].some((d) => d.pid === pid && d.row.status === "parsed"));
  return pid;
}
const graphPath = (pid: string) => `/api/backend/projects/${pid}/graph_build`;
const isGraphGet = (pid: string) => (x: { method: string; path: string }) => x.method === "GET" && x.path === graphPath(pid);
const isGraphPost = (pid: string) => (x: { method: string; path: string }) => x.method === "POST" && x.path === graphPath(pid);
const graphBody = (g: Record<string, unknown>) =>
  JSON.stringify({ status: "idle", build_id: null, mode: null, started_at: null, finished_at: null, error_code: null, ...g });
const flush = async (n = 40) => {
  for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r));
};

// ======================================================================================================================
// U: toGraphBuild (SPECS §2.4)
// ======================================================================================================================

test("AC-D2: toGraphBuild maps every status, a known mode as it is, an unknown or missing mode to null; extra fields are ignored", () => {
  for (const status of ["idle", "running", "succeeded", "partially_succeeded", "failed"] as const)
    assert.equal(toGraphBuild({ status }).status, status);
  assert.deepEqual(
    toGraphBuild({
      status: "running",
      build_id: "b",
      mode: "update",
      started_at: STARTED,
      finished_at: null,
      error_code: null,
      extra: { x: 1 },
    }),
    { status: "running", buildId: "b", mode: "update", startedAt: STARTED, finishedAt: null, errorCode: null },
  );
  for (const mode of ["first", "update", "rebuild"] as const) assert.equal(toGraphBuild({ status: "idle", mode }).mode, mode);
  assert.equal(toGraphBuild({ status: "idle", mode: "turbo" }).mode, null, "an unknown mode is null (BuildMode, never a string)");
  assert.equal(toGraphBuild({ status: "idle" }).mode, null, "a missing mode is null");
  assert.deepEqual(toGraphBuild({ status: "idle" }), NO_GRAPH.graph, "absent fields are null");
});

test("AC-D2: toGraphBuild is strict on the status and on the field types; anything else is invalid_response", () => {
  const bad: unknown[] = [
    null,
    "idle",
    [],
    42,
    {},
    { status: "bogus" },
    { status: 1 },
    { status: "idle", build_id: 7 },
    { status: "idle", started_at: {} },
    { status: "idle", finished_at: 5 },
    { status: "failed", error_code: ["x"] },
  ];
  for (const b of bad) {
    try {
      toGraphBuild(b);
      assert.fail(`accepted ${JSON.stringify(b)}`);
    } catch (e) {
      assert.ok(isDataError(e) && e.code === "invalid_response", `invalid_response for ${JSON.stringify(b)}`);
    }
  }
});

// ======================================================================================================================
// U: deriveStatus merge rows (SPECS §2.4)
// ======================================================================================================================

test("AC-D2: idle graph = no build, no last build, not ready, first build next, no failure", () => {
  const s = derive(NO_GRAPH.graph);
  assert.deepEqual(
    [s.build, s.lastBuild, s.ready, s.nextBuild, s.buildFailure, s.phase],
    [null, null, false, "first", null, "ready_to_build"],
  );
});

test("AC-D2: a running graph is the running build (finishedAt and errorCode null), phase building, nothing is ready yet", () => {
  const s = derive(running);
  assert.deepEqual(s.build, { id: "gb-1", mode: "first", state: "running", startedAt: STARTED, finishedAt: null, errorCode: null });
  assert.equal(s.lastBuild, null);
  assert.equal(s.phase, "building");
  assert.equal(s.ready, false);
  assert.equal(s.buildFailure, null);
  // The graph's own finished fields never leak into a running build.
  assert.deepEqual(derive({ ...running, finishedAt: FINISHED, errorCode: "x" }).build, s.build);
});

test("AC-D2: succeeded and partially_succeeded are the last build, ready, update next, phase up_to_date", () => {
  for (const status of ["succeeded", "partially_succeeded"] as const) {
    const s = derive({ ...succeeded, status });
    assert.deepEqual(s.lastBuild, { id: "gb-1", mode: "first", state: status, startedAt: STARTED, finishedAt: FINISHED, errorCode: null });
    assert.equal(s.build, null);
    assert.deepEqual([s.ready, s.nextBuild, s.phase, s.buildFailure], [true, "update", "up_to_date", null], status);
  }
});

test("AC-D2, AC-D5: a failed graph is the last build and the build failure (errorCode, or 'unknown' without one); a first-mode failure is not ready", () => {
  const s = derive(failedGraph);
  assert.deepEqual(s.lastBuild, {
    id: "gb-2",
    mode: "first",
    state: "failed",
    startedAt: STARTED,
    finishedAt: FINISHED,
    errorCode: "boom",
  });
  assert.deepEqual(s.buildFailure, { code: "boom", buildId: "gb-2", mode: "first", startedAt: STARTED, finishedAt: FINISHED });
  assert.equal(s.ready, false);
  assert.equal(s.nextBuild, "first");
  assert.equal(derive({ ...failedGraph, errorCode: null }).buildFailure?.code, "unknown");
});

test("AC-D2: ready follows the mode too (update/rebuild = an earlier build succeeded, even when this one failed) and the remembered flag", () => {
  assert.equal(derive({ ...failedGraph, mode: "update" }).ready, true);
  assert.equal(derive({ ...failedGraph, mode: "rebuild" }).ready, true);
  assert.equal(derive({ ...running, mode: "update" }).ready, true);
  assert.equal(derive({ ...failedGraph, mode: null }).ready, false, "an unknown mode (null) tells nothing");
  const remembered = derive(NO_GRAPH.graph, { remembered: true });
  assert.deepEqual([remembered.ready, remembered.nextBuild, remembered.phase], [true, "update", "up_to_date"]);
  assert.equal(derive(failedGraph, { remembered: true }).ready, true, "a failed later build keeps the remembered success");
});

test("AC-D2: nextBuild is null with no sources, no processed document or an unfinished import; the phase says why", () => {
  const none = derive(NO_GRAPH.graph, {}, lean([], 0));
  assert.deepEqual([none.nextBuild, none.phase], [null, "no_sources"]);
  const zero = derive(NO_GRAPH.graph, {}, lean([wireSource({ skipped: 5 })], 0));
  assert.deepEqual([zero.nextBuild, zero.phase], [null, "collecting"]);
  const locked = derive(NO_GRAPH.graph, { remembered: true }, lean([wireSource({ done: 3, waiting: 2 })]));
  assert.deepEqual([locked.nextBuild, locked.phase], [null, "collecting"]);
  const listing = derive(NO_GRAPH.graph, {}, lean([wireSource({}, { listing_finished: false })]));
  assert.equal(listing.nextBuild, null);
  assert.equal(
    derive(running, {}, lean([wireSource({ done: 3, waiting: 2 })])).phase,
    "collecting",
    "the import lock wins over building (phase order)",
  );
  const attention = derive(NO_GRAPH.graph, {}, lean([wireSource({ done: 3, failed: 1 })]));
  assert.equal(attention.phase, "attention");
});

test("AC-D5: a start failure is on show unless a build runs or one began after it; unparseable or missing times never clear it", () => {
  const sf = startFailure();
  assert.deepEqual(derive(NO_GRAPH.graph, { sf }).buildFailure, sf, "idle graph: the failure shows");
  assert.equal(derive(running, { sf }).buildFailure, null, "running clears it");
  assert.equal(
    derive({ ...succeeded, startedAt: "2026-10-26T10:00:01Z" }, { sf }).buildFailure,
    null,
    "a build that began after it clears it",
  );
  assert.deepEqual(derive({ ...succeeded, startedAt: "2026-10-26T09:59:59Z" }, { sf }).buildFailure, sf, "an older build does not");
  assert.deepEqual(
    derive({ ...failedGraph, startedAt: "2026-10-26T09:59:59Z" }, { sf }).buildFailure,
    sf,
    "the start failure beats an older failed graph",
  );
  assert.equal(
    derive({ ...failedGraph, startedAt: "2026-10-26T11:00:00Z" }, { sf }).buildFailure?.code,
    "boom",
    "a newer failed graph shows its own code",
  );
  // finishedAt stands in when the graph has no startedAt.
  assert.equal(derive({ ...succeeded, startedAt: null, finishedAt: "2026-10-26T11:00:00Z" }, { sf }).buildFailure, null);
  // Unparseable dates: NaN compares false, the failure stays and nothing prints NaN.
  const odd = derive({ ...succeeded, startedAt: "not a date", finishedAt: "also not" }, { sf });
  assert.deepEqual(odd.buildFailure, sf);
  assert.deepEqual(derive({ ...succeeded, startedAt: null, finishedAt: null }, { sf }).buildFailure, sf);
  assert.deepEqual(derive(NO_GRAPH.graph, { sf: startFailure({ startedAt: "garbage" }) }).buildFailure?.code, "graph_build_unavailable");
  assert.ok(!JSON.stringify(odd).includes("NaN"));
});

test("AC-D7: deriveStatus with one argument = the sources-only status plus buildFailure: null", () => {
  const l = lean();
  const one = deriveStatus(l);
  assert.deepEqual(one, deriveStatus(l, NO_GRAPH));
  assert.deepEqual(
    [one.build, one.lastBuild, one.buildFailure, one.ready, one.nextBuild, one.phase],
    [null, null, null, false, "first", "ready_to_build"],
  );
  assert.deepEqual(one.totals, { documents: 4, skipped: 0, failed: 0, pending: 0 });
  assert.deepEqual(one.changes, { new: 0, changed: 0, removed: 0 });
});

test("AC-D6: the production knowledge port has no buildAvailable and build() never answers not_available", () => {
  const http = createHttpAdapter();
  assert.equal("buildAvailable" in http.knowledge, false);
  assert.deepEqual(
    Object.keys(http.knowledge).toSorted((a, b) => a.localeCompare(b)),
    ["build", "leave", "status", "subscribe"],
  );
});

// ======================================================================================================================
// U: buildButtonView rows 1-7 + the lock (SPECS §3)
// ======================================================================================================================

const view = (graph: GraphBuildWire, opts: { sf?: BuildFailure; remembered?: boolean; l?: ReturnType<typeof lean>; now?: Date } = {}) =>
  buildButtonView(derive(graph, { sf: opts.sf, remembered: opts.remembered }, opts.l ?? lean()), opts.now ?? new Date(FINISHED));
const LOCKED = lean([wireSource({ done: 12, waiting: 18 })]);
const at = (iso: string, plusMs: number) => new Date(Date.parse(iso) + plusMs);

test("AC-U1, AC-U2: row 1, a running build: 'Building…', off + busy, no click mode; the detail uses a fixed now (0 s, 6 min, startedAt null)", () => {
  const v = view(running, { now: at(STARTED, 0) });
  assert.deepEqual([v.label, v.mode, v.tone, v.busy, v.failed, v.blocked], ["Building…", null, "off", true, false, null]);
  assert.equal(v.detail, `Started ${clock(STARTED)} · less than a minute so far`);
  assert.equal(view(running, { now: at(STARTED, 59_999) }).detail, `Started ${clock(STARTED)} · less than a minute so far`);
  assert.equal(view(running, { now: at(STARTED, 60_000) }).detail, `Started ${clock(STARTED)} · 1 min so far`);
  assert.equal(view(running, { now: at(STARTED, 6 * 60_000 + 30_000) }).detail, `Started ${clock(STARTED)} · 6 min so far`);
  assert.equal(view({ ...running, startedAt: null }).detail, "Building knowledge");
  assert.equal(view({ ...running, startedAt: "never" }).detail, "Building knowledge", "unparseable: no NaN");
  // A clock behind the server (a negative age) still reads as under a minute.
  assert.equal(view(running, { now: at(STARTED, -5 * 60_000) }).detail, `Started ${clock(STARTED)} · less than a minute so far`);
});

test("AC-U4: row 1 ignores the import lock, and a running build wins over an error", () => {
  const v = view(running, { l: LOCKED, now: at(STARTED, 120_000) });
  assert.deepEqual([v.label, v.busy, v.detail], ["Building…", true, `Started ${clock(STARTED)} · 2 min so far`]);
  const both = view(running, { sf: startFailure(), now: at(STARTED, 0) });
  assert.deepEqual([both.label, both.failed, both.busy], ["Building…", false, true]);
});

test("AC-U1: row 3, no sources: 'Build knowledge' off, 'Add a source first.'", () => {
  const v = view(NO_GRAPH.graph, { l: lean([], 0) });
  assert.deepEqual([v.label, v.mode, v.tone, v.detail], [BUILD_LABEL, null, "off", "Add a source first."]);
});

test("AC-U1: row 4, a source still syncing while the progress says finished: the waiting label and detail, off", () => {
  const l = lean([wireSource({ done: 3, processing: 1 })]);
  const status: ProjectSourcesStatus = { ...derive(NO_GRAPH.graph, {}, l), progress: { listingFinished: true, total: 4, finished: 4 } };
  const v = buildButtonView(status);
  assert.deepEqual([v.label, v.mode, v.tone], [BUILD_LABEL, null, "off"]);
  assert.equal(v.detail, "Waits until 1 source finishes syncing · 4 documents so far");
  assert.equal(buildButtonView({ ...status, ready: true }).label, "Update knowledge");
});

test("AC-U1: row 5, the first build: 'Build knowledge', primary, mode first, 'N documents from M sources[ · K skipped]'", () => {
  const v = view(NO_GRAPH.graph);
  assert.deepEqual([v.label, v.mode, v.tone, v.busy, v.failed], [BUILD_LABEL, "first", "primary", false, false]);
  assert.equal(v.detail, "4 documents from 1 source");
  assert.equal(view(NO_GRAPH.graph, { l: lean([wireSource({ done: 1, skipped: 2 })], 1) }).detail, "1 document from 1 source · 2 skipped");
  const two = lean([wireSource({ done: 2 }), wireSource({ done: 3 }, { id: "s2" })], 5);
  assert.equal(view(NO_GRAPH.graph, { l: two }).detail, "5 documents from 2 sources");
});

test("AC-U1, AC-U11: row 6, update: 'Update knowledge', primary, mode update; the detail is 'Built <when>' and the problems phrase only for partially_succeeded", () => {
  const now = at(FINISHED, 1000);
  const ok = view(succeeded, { now });
  assert.deepEqual([ok.label, ok.mode, ok.tone, ok.failed], ["Update knowledge", "update", "primary", false]);
  assert.equal(ok.detail, `Built ${when(FINISHED, now)}`);
  assert.match(ok.detail, /^Built today \d\d:\d\d$/);
  const partial = view({ ...succeeded, status: "partially_succeeded" }, { now });
  assert.equal(partial.detail, `Built ${when(FINISHED, now)} · Last build finished with some problems`);
  assert.equal(partial.mode, "update");
  assert.equal(
    view({ ...succeeded, finishedAt: null }, { now }).detail,
    `Built ${when(STARTED, now)}`,
    "finishedAt missing: startedAt stands in",
  );
  // Parts that cannot be said are dropped: remembered success without a last build, an unparseable time.
  assert.equal(view(NO_GRAPH.graph, { remembered: true, now }).detail, "Ready to update");
  const odd = view({ ...succeeded, startedAt: "x", finishedAt: "not a date" }, { now });
  assert.equal(odd.detail, "Ready to update", "no 'Built NaN:NaN'");
  assert.equal(
    view({ ...succeeded, status: "partially_succeeded", finishedAt: "?", startedAt: "?" }, { now }).detail,
    "Last build finished with some problems",
  );
  const yesterdayAt = "2026-10-25T09:12:00Z";
  const yesterdayNow = new Date("2026-10-26T09:12:30Z");
  const yesterday = view({ ...succeeded, finishedAt: yesterdayAt }, { now: yesterdayNow });
  assert.equal(yesterday.detail, `Built ${when(yesterdayAt, yesterdayNow)}`);
  assert.match(when(yesterdayAt, yesterdayNow), /^yesterday \d\d:\d\d$/, "a day earlier reads 'yesterday', not a date");
});

test("AC-U1: row 7, nothing to build (no processed document): off, 'No documents to build yet.', the label follows ready", () => {
  const empty = lean([wireSource({ skipped: 5 })], 0);
  const first = view(NO_GRAPH.graph, { l: empty });
  assert.deepEqual([first.label, first.mode, first.tone, first.detail], [BUILD_LABEL, null, "off", "No documents to build yet."]);
  assert.equal(view(NO_GRAPH.graph, { l: empty, remembered: true }).label, "Update knowledge");
  assert.equal(view(NO_GRAPH.graph, { l: empty, remembered: true }).mode, null);
});

test("AC-U1: row 2, an Error: 'Error', danger, failed; the detail names the code; Try again starts the base mode", () => {
  // A refused start on the first build.
  const first = view(NO_GRAPH.graph, { sf: startFailure() });
  assert.deepEqual(
    [first.label, first.mode, first.tone, first.busy, first.failed, first.blocked],
    ["Error", "first", "danger", false, true, null],
  );
  assert.equal(first.detail, "The build failed (graph_build_unavailable). Open for details.");
  // A failed first build, and a failed update (mode and ready from the graph).
  const failedFirst = view(failedGraph);
  assert.deepEqual(
    [failedFirst.label, failedFirst.mode, failedFirst.detail],
    ["Error", "first", "The build failed (boom). Open for details."],
  );
  assert.equal(view({ ...failedGraph, mode: "update" }).mode, "update");
  assert.equal(view(NO_GRAPH.graph, { sf: startFailure(), remembered: true }).mode, "update");
  assert.equal(view({ ...failedGraph, errorCode: null }).detail, "The build failed (unknown). Open for details.");
});

test("AC-U15: Error with nothing to start: Try again is blocked and says why (no documents, no sources)", () => {
  const noDocs = view(NO_GRAPH.graph, { sf: startFailure({ code: "no_processed_documents" }), l: lean([wireSource({ skipped: 5 })], 0) });
  assert.deepEqual([noDocs.label, noDocs.mode, noDocs.failed, noDocs.blocked], ["Error", null, true, "No documents to build yet."]);
  const noSources = view(NO_GRAPH.graph, { sf: startFailure(), l: lean([], 0) });
  assert.deepEqual([noSources.mode, noSources.blocked], [null, "Add a source first."]);
});

test("AC-U4: the import lock blocks starts in rows 2-7 (mode null, off, the lock text); the label stays", () => {
  const lockText = /^Build knowledge starts after all files are imported\. 12 of 30 files imported\.$/;
  const first = buildButtonView(derive(NO_GRAPH.graph, {}, LOCKED));
  assert.deepEqual([first.label, first.mode, first.tone, first.busy], [BUILD_LABEL, null, "off", false]);
  assert.match(first.detail, lockText);
  const update = buildButtonView(derive(succeeded, {}, LOCKED), new Date(FINISHED));
  assert.deepEqual([update.label, update.mode, update.tone], ["Update knowledge", null, "off"]);
  assert.match(update.detail, lockText);
  const listing = buildButtonView(derive(NO_GRAPH.graph, {}, lean([wireSource({}, { listing_finished: false })])));
  assert.equal(listing.mode, null);
  assert.match(listing.detail, /^Build knowledge starts after all files are imported\. Listing files/);
  const empty = buildButtonView(derive(NO_GRAPH.graph, {}, lean([wireSource({ waiting: 2 })], 0)));
  assert.equal(empty.mode, null, "row 7 is locked too");
  assert.match(empty.detail, /starts after all files are imported/);
});

test("AC-U4, AC-U15: Error + lock: stays red, the detail stays, Try again is blocked by the lock text", () => {
  const v = buildButtonView(derive(NO_GRAPH.graph, { sf: startFailure() }, LOCKED));
  assert.deepEqual([v.label, v.tone, v.failed, v.mode], ["Error", "danger", true, null]);
  assert.equal(v.detail, "The build failed (graph_build_unavailable). Open for details.");
  assert.match(v.blocked ?? "", /^Build knowledge starts after all files are imported\. 12 of 30 files imported\.$/);
  const failed = buildButtonView(derive(failedGraph, {}, LOCKED));
  assert.deepEqual([failed.label, failed.mode], ["Error", null]);
  assert.match(failed.blocked ?? "", /starts after all files/);
});

test("AC-U1: only an Error has `blocked`; every other row has it null", () => {
  for (const v of [view(running), view(NO_GRAPH.graph), view(succeeded), view(NO_GRAPH.graph, { l: lean([], 0) })])
    assert.equal(v.blocked, null);
});

// ======================================================================================================================
// U: estimateText (SPECS §5), buildErrorReport (SPECS §7)
// ======================================================================================================================

test("AC-U6: estimateText reproduces the SPECS §5 table", () => {
  const table: [ReasoningLevel, number, string][] = [
    [2, 1, "about 2–4 minutes"],
    [2, 10, "about 4–9 minutes"],
    [2, 25, "about 8–18 minutes"],
    [2, 40, "about 13–27 minutes"],
    [2, 95, "about 28–60 minutes"],
    [2, 100, "more than an hour"],
    [1, 1, "about 1–2 minutes"],
    [1, 10, "about 3–5 minutes"],
    [3, 10, "about 6–13 minutes"],
    [4, 10, "about 8–18 minutes"],
    [5, 10, "about 12–25 minutes"],
    [5, 40, "more than an hour"],
  ];
  for (const [level, docs, text] of table)
    assert.equal(estimateText(docs, level), text, `level ${String(level)}, ${String(docs)} documents`);
});

test("AC-U6: estimateText edges: 0 documents still gives a range of at least 1-2 minutes, the range grows with the level, an en dash separates it", () => {
  assert.equal(estimateText(0, 1), "about 1–2 minutes");
  assert.match(estimateText(0, 2), /^about 1–3 minutes$/);
  for (let level = 1 as ReasoningLevel; level <= 5; level++) {
    const m = /about (\d+)–(\d+) minutes/.exec(estimateText(10, level));
    assert.ok(m, `a range at level ${String(level)}`);
    assert.ok(Number(m[2]) > Number(m[1]), "high > low");
  }
  assert.equal(estimateText(95, 2).includes("–"), true);
  assert.deepEqual(REASONING_LEVELS, ["Low", "Medium", "High", "Extra high", "Max"]);
});

test("AC-U13: buildErrorReport is byte-exact for a fixed input; null or unparseable fields read as the dash; the page has no query", () => {
  const f: BuildFailure = { code: "graph_build_unavailable", buildId: "gb-7", mode: "update", startedAt: STARTED, finishedAt: FINISHED };
  assert.equal(
    buildErrorReport("p-123", f, "https://app.example.test/projects/p-123/sources"),
    [
      "Specflow knowledge build failed",
      "Project: p-123",
      "Build: gb-7",
      "Mode: update",
      "Error code: graph_build_unavailable",
      "Started (UTC): 2026-10-26T09:00:00.000Z",
      "Finished (UTC): 2026-10-26T09:12:00.000Z",
      "Page: https://app.example.test/projects/p-123/sources",
    ].join("\n"),
  );
  const bare = buildErrorReport("p-1", { code: "x", buildId: null, mode: null, startedAt: "garbage", finishedAt: null }, "https://h/");
  assert.equal(
    bare,
    [
      "Specflow knowledge build failed",
      "Project: p-1",
      "Build: —",
      "Mode: —",
      "Error code: x",
      "Started (UTC): —",
      "Finished (UTC): —",
      "Page: https://h/",
    ].join("\n"),
  );
  assert.ok(!bare.includes("NaN"));
});

// ======================================================================================================================
// P: the port against the stub API
// ======================================================================================================================

test("AC-D1: every status() reads sources and graph_build; an idle project says idle (no build, not ready)", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Both reads");
  const from = env.sent.length;
  const s = await http.knowledge.status(pid);
  const mine = env.sent.slice(from).map((x) => `${x.method} ${x.path}`);
  assert.ok(mine.includes(`GET /api/backend/projects/${pid}/sources?batches=1`));
  assert.ok(mine.includes(`GET ${graphPath(pid)}`));
  assert.deepEqual([s.build, s.lastBuild, s.ready, s.buildFailure, s.nextBuild], [null, null, false, null, "first"]);
});

test("AC-D1: a graph GET that fails (503, 404, 500, network, a bad shape, a bad status) keeps the sources part and the last graph, idle the first time", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Graph fails");
  const faults: [string, () => Response | Promise<Response>][] = [
    ["503", () => jsonResponse(503, JSON.stringify({ error: { code: "graph_build_unavailable", message: "SERVER TEXT" } }))],
    ["404", () => jsonResponse(404, JSON.stringify({ error: { code: "not_found", message: "no route" } }))],
    ["500", () => jsonResponse(500, "<html>boom</html>")],
    ["network", () => Promise.reject(new TypeError("network down"))],
    ["bad shape", () => jsonResponse(200, "[]")],
    ["bad status", () => jsonResponse(200, graphBody({ status: "bogus" }))],
    ["not json", () => jsonResponse(200, "not json")],
  ];
  // First time: idle.
  for (const [name, fault] of faults) {
    await withHook(
      env,
      (x) => {
        if (isGraphGet(pid)(x)) return fault();
      },
      async () => {
        const s = await http.knowledge.status(pid);
        assert.deepEqual([s.build, s.ready, s.nextBuild, s.buildFailure], [null, false, "first", null], `idle first time (${name})`);
        assert.equal(s.totals.documents, 1, `the sources part still renders (${name})`);
        assert.equal(s.sources.length, 1);
      },
    );
  }
  // Later: the last good graph stays (a running build).
  env.api.setTiming({ graphMs: Infinity });
  await http.knowledge.build(pid, { reasoningLevel: 2 });
  assert.ok((await http.knowledge.status(pid)).build);
  for (const [name, fault] of faults) {
    await withHook(
      env,
      (x) => {
        if (isGraphGet(pid)(x)) return fault();
      },
      async () => {
        const s = await http.knowledge.status(pid);
        assert.equal(s.build?.state, "running", `the last graph is kept (${name})`);
        assert.equal(s.phase, "building");
        assert.equal(s.totals.documents, 1);
      },
    );
  }
});

test("AC-D1: a failing graph read does not back off the poll: the failed GET is not retried on its own, the sources read decides", async () => {
  const http = createHttpAdapter({ pollMs: 40, idleMs: 60 });
  const pid = await projectWithDoc(http, "No back-off");
  let graphGets = 0;
  let reads = 0;
  const load = () =>
    void http.knowledge.status(pid).then(
      () => reads++,
      () => undefined,
    );
  const off = http.knowledge.subscribe(pid, load);
  try {
    await withHook(
      env,
      (x) => {
        if (isGraphGet(pid)(x)) {
          graphGets++;
          return jsonResponse(503, JSON.stringify({ error: { code: "graph_build_unavailable" } }));
        }
      },
      async () => {
        load();
        await waitFor("four reads at the idle cadence, not at the growing back-off", () => reads >= 4, 5000);
      },
    );
    assert.ok(graphGets >= 4);
  } finally {
    off();
    http.knowledge.leave(pid);
  }
});

test("AC-D3: the poll runs every 3 s while the graph runs and every 5 s once it is idle (fake clock, answers faked)", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Cadence");
  let graph = graphBody({ status: "running", build_id: "gb-1", mode: "first", started_at: STARTED });
  let sourceGets = 0;
  const load = () => void http.knowledge.status(pid).catch(() => undefined);
  const off = http.knowledge.subscribe(pid, load);
  await withHook(
    env,
    (x) => {
      if (isGraphGet(pid)(x)) return jsonResponse(200, graph);
      if (x.method === "GET" && x.path === `/api/backend/projects/${pid}/sources?batches=1`) {
        sourceGets++;
        return jsonResponse(200, JSON.stringify(v2()));
      }
    },
    async () => {
      mock.timers.enable({ apis: ["setTimeout"] });
      try {
        load();
        await flush();
        assert.equal(sourceGets, 1);
        mock.timers.tick(2_999);
        await flush();
        assert.equal(sourceGets, 1, "running: not before 3 s");
        mock.timers.tick(1);
        await flush();
        assert.equal(sourceGets, 2, "running: at 3 s (the import is finished, so it is the graph that keeps pollMs)");
        graph = graphBody({ status: "succeeded", build_id: "gb-1", mode: "first", started_at: STARTED, finished_at: FINISHED });
        mock.timers.tick(3_000);
        await flush();
        assert.equal(sourceGets, 3, "the read that sees it finished");
        mock.timers.tick(4_999);
        await flush();
        assert.equal(sourceGets, 3, "idle: not before 5 s");
        mock.timers.tick(1);
        await flush();
        assert.equal(sourceGets, 4, "idle: at 5 s");
      } finally {
        mock.timers.reset();
        off();
      }
    },
  );
  http.knowledge.leave(pid);
});

test('AC-D4: build() POSTs {"reasoning_level":n} with X-Requested-With, tells subscribers, and the next status is running', async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Build body");
  env.api.setTiming({ graphMs: Infinity });
  let notified = 0;
  const off = http.knowledge.subscribe(pid, () => notified++);
  try {
    await http.knowledge.build(pid, { reasoningLevel: 3 });
    assert.equal(notified, 1);
    const post = env.sent.filter((x) => isGraphPost(pid)(x));
    assert.equal(post.length, 1);
    assert.equal(post[0].headers["x-requested-with"], "specflow");
    assert.deepEqual(JSON.parse(post[0].body ?? "{}"), { reasoning_level: 3 });
    const entry = env.api.log.filter((e) => e.method === "POST" && e.path === `/api/v1/projects/${pid}/graph_build`);
    assert.deepEqual(
      entry.map((e) => e.bodyText),
      ['{"reasoning_level":3}'],
      "the stub got the body through the BFF",
    );
    const s = await http.knowledge.status(pid);
    assert.equal(s.build?.state, "running");
    assert.equal(defined(s.build).mode, "first");
    assert.equal(s.phase, "building");
    assert.equal(buildButtonView(s).label, "Building…");
  } finally {
    off();
    http.knowledge.leave(pid);
  }
});

test("AC-D4: a status read that began before the POST cannot overwrite the started build (epoch)", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Stale read");
  env.api.setTiming({ graphMs: Infinity });
  let release: () => void = () => undefined;
  const gate = new Promise<void>((r) => (release = r));
  let held = 0;
  await withHook(
    env,
    async (x) => {
      if (isGraphGet(pid)(x) && held++ === 0) {
        await gate; // the first graph read stays in flight and answers "idle" when it is let go
        return jsonResponse(200, graphBody({}));
      }
    },
    async () => {
      const stale = http.knowledge.status(pid);
      await waitFor("the read is in flight", () => held === 1);
      await http.knowledge.build(pid, { reasoningLevel: 2 });
      release();
      const s = await stale;
      assert.equal(s.build?.state, "running", "the stale idle answer was discarded; the POST's own answer stands");
      assert.equal(s.buildFailure, null);
    },
  );
  assert.equal((await http.knowledge.status(pid)).build?.state, "running", "and a fresh read agrees");
});

test("AC-D4: a 202 body that cannot be read still resolves; no failure is recorded for it", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Odd 202");
  env.api.setTiming({ graphMs: Infinity });
  await withHook(
    env,
    (x) => (isGraphPost(pid)(x) ? jsonResponse(202, '{"unexpected":true}') : undefined),
    async () => {
      await http.knowledge.build(pid, { reasoningLevel: 2 }); // the hook swallows the POST: nothing reaches the stub
    },
  );
  assert.equal((await http.knowledge.status(pid)).buildFailure, null, "no failure for an unreadable success");
});

test("AC-D5: a refused start (409 no_processed_documents) rejects and the next status carries buildFailure.code = the API code", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "No documents");
  const e = await rejection(http.knowledge.build(pid, { reasoningLevel: 2 }));
  assert.deepEqual([e.code, e.detail?.apiCode, e.detail?.status], ["conflict", "no_processed_documents", 409]);
  const s = await http.knowledge.status(pid);
  assert.equal(s.buildFailure?.code, "no_processed_documents");
  const refusal = defined(s.buildFailure, "the refusal");
  assert.deepEqual([refusal.buildId, refusal.mode, refusal.finishedAt], [null, null, null]);
  assert.ok(!Number.isNaN(Date.parse(refusal.startedAt ?? "")), "startedAt is the client time of the refusal");
  assert.equal(buildButtonView(s).failed, true, "the refusal shows as Error even though nothing can be started");
});

test("AC-D5: 503 graph_build_unavailable and a network failure are kept as the failure; no toast data, the error is on the status only", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Start fails");
  env.api.setTiming({ graphStartFail: "graph_build_unavailable" });
  const e = await rejection(http.knowledge.build(pid, { reasoningLevel: 4 }));
  assert.deepEqual([e.code, e.detail?.apiCode], ["unavailable", "graph_build_unavailable"]);
  const s = await http.knowledge.status(pid);
  assert.equal(s.buildFailure?.code, "graph_build_unavailable");
  const v = buildButtonView(s);
  assert.deepEqual([v.label, v.failed, v.mode, v.blocked], ["Error", true, "first", null]);
  // A transport failure: the DataError code is the failure code.
  env.api.setTiming(GRAPH_DEFAULTS);
  await withHook(
    env,
    (x) => {
      if (isGraphPost(pid)(x)) throw new TypeError("network down");
      return undefined;
    },
    async () => {
      assert.equal((await rejection(http.knowledge.build(pid, { reasoningLevel: 2 }))).code, "network");
    },
  );
  assert.equal((await http.knowledge.status(pid)).buildFailure?.code, "network", "the newer refusal replaced the older one");
  // A 422 (a body the API rejects) is a failure too, with its API code.
  await withHook(
    env,
    (x) => (isGraphPost(pid)(x) ? jsonResponse(422, JSON.stringify({ error: { code: "validation_failed" } })) : undefined),
    async () => {
      const bad = await rejection(http.knowledge.build(pid, { reasoningLevel: 2 }));
      assert.equal(bad.code, "validation");
    },
  );
  assert.equal((await http.knowledge.status(pid)).buildFailure?.code, "validation_failed");
});

test("AC-D5: a start failure is cleared by a successful POST", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Cleared by POST");
  env.api.setTiming({ graphStartFail: "graph_build_unavailable" });
  await rejection(http.knowledge.build(pid, { reasoningLevel: 2 }));
  assert.equal((await http.knowledge.status(pid)).buildFailure?.code, "graph_build_unavailable");
  env.api.setTiming({ graphStartFail: null, graphMs: Infinity });
  await http.knowledge.build(pid, { reasoningLevel: 2 });
  const s = await http.knowledge.status(pid);
  assert.deepEqual([s.buildFailure, s.build?.state], [null, "running"]);
});

test("AC-D5: a start failure is cleared by a running GET, and by a GET build newer than the failure (not by an older one)", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Cleared by GET");
  env.api.setTiming({ graphStartFail: "graph_build_unavailable" });
  await rejection(http.knowledge.build(pid, { reasoningLevel: 2 }));
  env.api.setTiming(GRAPH_DEFAULTS);
  const answer = (g: Record<string, unknown>) => (x: { method: string; path: string }) =>
    isGraphGet(pid)(x) ? jsonResponse(200, graphBody(g)) : undefined;
  const hour = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString();
  // An older finished build: the failure stays.
  await withHook(env, answer({ status: "succeeded", mode: "first", started_at: hour(-2), finished_at: hour(-1) }), async () => {
    assert.equal((await http.knowledge.status(pid)).buildFailure?.code, "graph_build_unavailable");
  });
  // A running build (any start time): cleared for good.
  await withHook(env, answer({ status: "running", build_id: "gb-9", mode: "first", started_at: hour(-2) }), async () => {
    assert.equal((await http.knowledge.status(pid)).buildFailure, null);
  });
  assert.equal((await http.knowledge.status(pid)).buildFailure, null, "stays cleared (the entry was deleted, not just hidden)");
  // Again: a failure, then a newer finished build clears it.
  env.api.setTiming({ graphStartFail: "graph_build_unavailable" });
  await rejection(http.knowledge.build(pid, { reasoningLevel: 2 }));
  env.api.setTiming(GRAPH_DEFAULTS);
  await withHook(env, answer({ status: "succeeded", mode: "first", started_at: hour(1), finished_at: hour(2) }), async () => {
    const s = await http.knowledge.status(pid);
    assert.deepEqual([s.buildFailure, s.lastBuild?.state, s.ready], [null, "succeeded", true]);
  });
  // The unparseable edge: a graph without usable dates never clears a failure.
  env.api.setTiming({ graphStartFail: "graph_build_unavailable" });
  await rejection(http.knowledge.build(pid, { reasoningLevel: 2 }));
  env.api.setTiming(GRAPH_DEFAULTS);
  await withHook(env, answer({ status: "succeeded", mode: "update", started_at: "soon", finished_at: "later" }), async () => {
    assert.equal((await http.knowledge.status(pid)).buildFailure?.code, "graph_build_unavailable");
  });
});

test("AC-D5: a failed graph build (GET) is the build failure with the server's error code; a later success clears it", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Failed build");
  env.api.setTiming({ graphMs: 0, graphOutcome: "failed" });
  await http.knowledge.build(pid, { reasoningLevel: 2 });
  const s = await waitStatus(http, pid, (x) => x.buildFailure !== null);
  assert.equal(s.buildFailure?.code, "stub_failure");
  assert.equal(s.lastBuild?.state, "failed");
  assert.equal(s.ready, false, "a failed first build is not ready");
  env.api.setTiming({ graphOutcome: "succeeded" });
  await http.knowledge.build(pid, { reasoningLevel: 2 });
  const ok = await waitStatus(http, pid, (x) => x.lastBuild?.state === "succeeded");
  assert.deepEqual([ok.buildFailure, ok.ready, ok.nextBuild], [null, true, "update"]);
});

async function waitStatus(http: HttpPorts, pid: string, pred: (s: ProjectSourcesStatus) => boolean): Promise<ProjectSourcesStatus> {
  let matched: ProjectSourcesStatus | undefined;
  await waitFor(
    "the status matches",
    () => {
      void http.knowledge.status(pid).then((x) => {
        if (pred(x)) matched = x;
      });
      return matched !== undefined;
    },
    5000,
  );
  return defined(matched, "the matching status");
}

test("AC-D4, AC-D2: ready is remembered across a failing graph read; the update build after a success is mode update", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Remembered");
  env.api.setTiming({ graphMs: 0 });
  await http.knowledge.build(pid, { reasoningLevel: 2 });
  assert.equal((await waitStatus(http, pid, (s) => s.ready)).lastBuild?.state, "succeeded");
  // The graph read breaks: the last graph (succeeded) is kept, and ready with it.
  await withHook(
    env,
    (x) => (isGraphGet(pid)(x) ? jsonResponse(503, JSON.stringify({ error: { code: "graph_build_unavailable" } })) : undefined),
    async () => {
      const s = await http.knowledge.status(pid);
      assert.deepEqual([s.ready, s.nextBuild, s.buildFailure], [true, "update", null]);
    },
  );
  env.api.setTiming({ graphMs: Infinity });
  await http.knowledge.build(pid, { reasoningLevel: 2 });
  const s = await http.knowledge.status(pid);
  assert.deepEqual([defined(s.build).mode, s.ready], ["update", true]);
});

test("AC-D4: the Ready gate (missionControl.snapshot) never calls graph_build", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Gate");
  const from = env.sent.length;
  assert.deepEqual(await http.missionControl.snapshot(pid), { phase: "ready" });
  assert.equal(env.sent.slice(from).filter((x) => x.path.includes("graph_build")).length, 0);
});

// ---- the stub itself (AC-S1) ----

const post = (pid: string, body?: string) =>
  fetch(graphPath(pid), {
    method: "POST",
    headers: { "X-Requested-With": "specflow", "Content-Type": "application/json" },
    body,
  });
const get = async (pid: string) => (await fetch(graphPath(pid))).json() as Promise<Record<string, unknown>>;

test("AC-S1: the stub validates the POST body: empty, {}, null and 1..5 are accepted; 0, 6, 'high', 2.5, an extra key, a non-object are 422 validation_failed", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Stub 422");
  env.api.setTiming({ graphMs: Infinity });
  const bad = [
    "0",
    "6",
    '"high"',
    "2.5",
    "{}x",
    "[1]",
    "null",
    '{"x":1}',
    '{"reasoning_level":0}',
    '{"reasoning_level":6}',
    '{"reasoning_level":"high"}',
    '{"reasoning_level":2.5}',
    '{"reasoning_level":1,"x":1}',
  ];
  const from = env.api.log.length;
  for (const b of bad) {
    const res = await post(pid, b);
    assert.equal(res.status, 422, b);
    assert.equal(((await res.json()) as { error: { code: string } }).error.code, "validation_failed", b);
  }
  assert.equal((await get(pid)).status, "idle", "no 422 started a build");
  assert.ok(
    env.api.log
      .slice(from)
      .filter((e) => e.method === "POST")
      .every((e) => e.bodyText === undefined),
    "a 422 records no body",
  );
  for (const ok of [undefined, "", "{}", '{"reasoning_level":null}', '{"reasoning_level":1}', '{"reasoning_level":5}']) {
    env.api.setTiming({ graphMs: Infinity });
    const res = await post(pid, ok);
    assert.equal(res.status, 202, String(ok));
  }
});

test("AC-S1: 409 without a processed document; 503 with graphStartFail (after the body check, before the 409); the log entry holds the body text", async () => {
  const http = createHttpAdapter();
  const empty = await newProject(http, "Stub 409");
  const r409 = await post(empty, "{}");
  assert.equal(r409.status, 409);
  assert.equal(((await r409.json()) as { error: { code: string } }).error.code, "no_processed_documents");
  env.api.setTiming({ graphStartFail: "graph_build_unavailable" });
  const r503 = await post(empty, '{"reasoning_level":3}');
  assert.equal(r503.status, 503);
  assert.equal(((await r503.json()) as { error: { code: string } }).error.code, "graph_build_unavailable", "503 wins over the 409");
  assert.equal((await post(empty, '{"reasoning_level":9}')).status, 422, "the body check comes first");
  const bodies = env.api.log
    .filter((e) => e.method === "POST" && e.path === `/api/v1/projects/${empty}/graph_build`)
    .map((e) => e.bodyText);
  assert.deepEqual(bodies, ["{}", '{"reasoning_level":3}', undefined]);
  assert.equal((await get(empty)).status, "idle");
});

test("AC-S1: the build resolves lazily on GET from the current graphMs (Infinity stays running, 0 resolves at once, no timer); an already running POST answers the same build", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Stub lazy");
  env.api.setTiming({ graphMs: Infinity });
  const first = await post(pid, '{"reasoning_level":2}');
  assert.equal(first.status, 202);
  const started = (await first.json()) as Record<string, unknown>;
  assert.deepEqual([started.status, started.mode, started.finished_at, started.error_code], ["running", "first", null, null]);
  assert.match(String(started.build_id), /^gb-\d+$/);
  assert.ok(!Number.isNaN(Date.parse(String(started.started_at))));
  assert.deepEqual(
    Object.keys(started).toSorted((a, b) => a.localeCompare(b)),
    ["build_id", "error_code", "finished_at", "mode", "started_at", "status"],
  );
  assert.equal((await get(pid)).status, "running");
  const again = (await (await post(pid, "{}")).json()) as Record<string, unknown>;
  assert.equal(again.build_id, started.build_id, "already running: the same build");
  env.api.setTiming({ graphMs: 0 });
  const done = await get(pid);
  assert.deepEqual([done.status, done.build_id, done.error_code], ["succeeded", started.build_id, null]);
  assert.ok(!Number.isNaN(Date.parse(String(done.finished_at))));
  const next = (await (await post(pid, "{}")).json()) as Record<string, unknown>;
  assert.deepEqual([next.status, next.mode], ["running", "update"], "after a success the next build is an update");
  assert.notEqual(next.build_id, started.build_id);
});

test("AC-S1: graphOutcome partially_succeeded and failed (error_code stub_failure); a failed build is not a success for the next mode", async () => {
  const http = createHttpAdapter();
  const pid = await projectWithDoc(http, "Stub outcomes");
  env.api.setTiming({ graphMs: 0, graphOutcome: "partially_succeeded" });
  await post(pid, "{}");
  const partial = await get(pid);
  assert.deepEqual([partial.status, partial.error_code], ["partially_succeeded", null]);
  env.api.setTiming({ graphOutcome: "failed" });
  const second = (await (await post(pid, "{}")).json()) as Record<string, unknown>;
  assert.equal(second.mode, "update", "a partial success counts");
  const failed = await get(pid);
  assert.deepEqual([failed.status, failed.error_code], ["failed", "stub_failure"]);
  const other = await projectWithDoc(http, "Stub failed first");
  await post(other, "{}");
  assert.equal((await get(other)).status, "failed");
  const retry = (await (await post(other, "{}")).json()) as Record<string, unknown>;
  assert.equal(retry.mode, "first", "no success yet: still the first build");
});

test("AC-S1: the stub's graph state is per project and idle at first; the knobs carry the set timing", async () => {
  const http = createHttpAdapter();
  const a = await projectWithDoc(http, "Stub A");
  const b = await projectWithDoc(http, "Stub B");
  env.api.setTiming({ graphMs: Infinity });
  await post(a, "{}");
  assert.equal((await get(a)).status, "running");
  assert.equal((await get(b)).status, "idle");
  assert.deepEqual([env.api.state.graphMs, env.api.state.graphOutcome, env.api.state.graphStartFail].map(String), [
    "Infinity",
    "succeeded",
    "null",
  ]);
});

test("AC-S1: the endpoint allowlist knows graph_build GET and POST (and nothing near it)", () => {
  const known = (method: string, path: string) => EXISTING_ENDPOINTS.some(([m, re]) => m === method && re.test(path));
  const id = "0123456789abcdef0123456789abcdef0123";
  assert.equal(known("GET", `/api/v1/projects/${id}/graph_build`), true);
  assert.equal(known("POST", `/api/v1/projects/${id}/graph_build`), true);
  assert.equal(known("DELETE", `/api/v1/projects/${id}/graph_build`), false);
  assert.equal(known("GET", `/api/v1/projects/${id}/graph_build/x`), false);
});
