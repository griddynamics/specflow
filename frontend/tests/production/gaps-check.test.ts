// The gap check the V2 canvas hub reads: `GapsPort.check` over `GET .../gap/runs/latest` and `GET .../graph_build`.
// Layer U: the wire parser (the status line's words were the V1 Gaps screen's and went with it). Layer P: the port against a fake `fetch`.
import { test, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import { createHttpGaps, toGapCheck } from "@/lib/data/http/gaps";
import { checkProgress } from "@/components/shared/gaps/gaps-logic";
import type { GapCheck } from "@/lib/domain/gaps";

const PID = "11111111-1111-4111-8111-111111111111";
const RESULT = { run_id: "run-1", counts: { gaps_new: { total: 2 }, gaps_updated: { total: 9 }, close: { resolved: 1 } } };
const running = {
  status: "running",
  run_id: "run-2",
  step: "investigate",
  steps_done: 5,
  steps_total: 7,
  step_items_done: 7,
  step_items_total: 21,
  started_at: "2026-11-17T12:21:00Z",
  result: RESULT,
};
const completed = {
  status: "completed",
  run_id: "run-1",
  step: "finish",
  steps_done: 7,
  steps_total: 7,
  finished_at: "2026-11-17T12:28:46Z",
  result: RESULT,
};

test("a running gap run is running, with its step, items and the last run's counts", () => {
  const c = toGapCheck(running, { status: "idle" });
  assert.equal(c.phase, "running");
  assert.deepEqual([c.step, c.stepsDone, c.stepsTotal, c.itemsDone, c.itemsTotal], ["investigate", 5, 7, 7, 21]);
  assert.deepEqual([c.newGaps, c.updatedGaps, c.resolvedGaps], [2, 9, 1]);
  assert.equal(checkProgress(c), 76); // (5 + 7/21) of 7
});

test("a running build before its gap run is building; a queued or running gap run wins over the build", () => {
  assert.equal(toGapCheck(completed, { status: "running" }).phase, "building");
  assert.equal(toGapCheck({ ...completed, status: "queued" }, { status: "running" }).phase, "queued");
  assert.equal(toGapCheck(running, { status: "running" }).phase, "running");
  assert.equal(toGapCheck(completed, null).phase, "done"); // the build state could not be read
});

test("a finished run keeps its counts; a result without some counts reads them as unknown", () => {
  const c = toGapCheck(completed, { status: "succeeded" });
  assert.equal(c.phase, "done");
  assert.deepEqual([c.newGaps, c.updatedGaps, c.resolvedGaps], [2, 9, 1]);
  const none = toGapCheck({ ...completed, result: { run_id: "run-3", counts: { gaps_new: { total: 0 } } } }, null);
  assert.deepEqual([none.newGaps, none.updatedGaps, none.resolvedGaps], [0, null, null]);
});

test("before the first run, a failed run and a degraded run are their own phases", () => {
  assert.equal(toGapCheck({ status: "idle", result: null }, { status: "idle" }).phase, "never");
  const failed = toGapCheck({ status: "failed", error_code: "gap_run_failed", result: null }, null);
  assert.equal(failed.phase, "failed");
  assert.equal(failed.errorCode, "gap_run_failed");
  assert.equal(toGapCheck({ ...completed, status: "degraded" }, null).phase, "degraded");
});

test("an unknown status is an invalid response", () => {
  assert.throws(() => toGapCheck({ status: "sleeping" }, null));
});

test("progress stays within 0..100", () => {
  const base: GapCheck = toGapCheck(running, null);
  assert.equal(checkProgress({ ...base, stepsTotal: 0 }), 0);
  assert.equal(checkProgress({ ...base, stepsDone: 7, itemsDone: 30 }), 100);
});

// ---- Layer P ----

const BASE = "/api/backend";
const calls: string[] = [];
afterEach(() => {
  mock.restoreAll();
});

test("check reads the latest gap run and the build state; a failed build read leaves the run's outcome", async () => {
  calls.length = 0;
  mock.method(globalThis, "fetch", (url: string, init?: RequestInit) => {
    const path = url.slice(BASE.length);
    calls.push(`${init?.method ?? "GET"} ${path}`);
    if (path.endsWith("/graph_build")) return Promise.resolve(new Response("{}", { status: 500 }));
    return Promise.resolve(new Response(JSON.stringify(completed), { status: 200 }));
  });
  const c = await createHttpGaps().check(PID);
  assert.equal(c.phase, "done");
  assert.deepEqual(
    calls.toSorted((a, b) => a.localeCompare(b)),
    [`GET /projects/${PID}/gap/runs/latest`, `GET /projects/${PID}/graph_build`],
  );
});
