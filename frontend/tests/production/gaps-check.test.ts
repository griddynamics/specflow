// The gap check status line of the Gaps screen: `GapsPort.check` over `GET .../gap/runs/latest` and `GET .../graph_build`, and its words.
// Layer U: the wire parser and the words. Layer P: the port against a fake `fetch`.
import { test, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import { createHttpGaps, toGapCheck } from "@/lib/data/http/gaps";
import type { GapCheck } from "@/lib/domain/gaps";
import { checkActive, checkFoundText, checkProgress, checkStepText, checkText, whenText } from "@/components/gaps/gaps-logic";

const PID = "11111111-1111-4111-8111-111111111111";
const RESULT = { run_id: "run-1", counts: { gaps_new: { total: 2 }, gaps_updated: { total: 9 }, close: { resolved: 1 } } };
const running = {
  status: "running",
  run_id: "run-2",
  step: "investigate",
  steps_done: 5,
  steps_total: 9,
  step_items_done: 7,
  step_items_total: 21,
  started_at: "2026-11-17T12:21:00Z",
  result: RESULT,
};
const completed = {
  status: "completed",
  run_id: "run-1",
  step: "record",
  steps_done: 9,
  steps_total: 9,
  finished_at: "2026-11-17T12:28:46Z",
  result: RESULT,
};

test("a running gap run is running, with its step, items and the last run's counts", () => {
  const c = toGapCheck(running, { status: "idle" });
  assert.equal(c.phase, "running");
  assert.deepEqual([c.step, c.stepsDone, c.stepsTotal, c.itemsDone, c.itemsTotal], ["investigate", 5, 9, 7, 21]);
  assert.deepEqual([c.newGaps, c.updatedGaps, c.resolvedGaps], [2, 9, 1]);
  assert.equal(checkActive(c), true);
  assert.equal(checkStepText(c), "Step 6 of 9: Investigating · 7 of 21 sites");
  assert.equal(checkProgress(c), 59); // (5 + 7/21) of 9
  assert.deepEqual(checkText(c), { title: "Looking for gaps", detail: "Step 6 of 9: Investigating · 7 of 21 sites" });
});

test("a running build before its gap run is building; a queued or running gap run wins over the build", () => {
  assert.equal(toGapCheck(completed, { status: "running" }).phase, "building");
  assert.equal(toGapCheck({ ...completed, status: "queued" }, { status: "running" }).phase, "queued");
  assert.equal(toGapCheck(running, { status: "running" }).phase, "running");
  assert.equal(toGapCheck(completed, null).phase, "done"); // the build state could not be read
  assert.equal(checkText(toGapCheck(completed, { status: "running" })).title, "Waiting for the knowledge build");
});

test("a finished run says when it finished and what it found", () => {
  const c = toGapCheck(completed, { status: "succeeded" });
  assert.equal(c.phase, "done");
  assert.equal(checkActive(c), false);
  assert.equal(checkFoundText(c), "2 new gaps, 9 updated, 1 resolved");
  assert.deepEqual(checkText(c), {
    title: `Gaps checked ${whenText("2026-11-17T12:28:46Z")}`,
    detail: "2 new gaps, 9 updated, 1 resolved",
  });
  const none = toGapCheck({ ...completed, result: { run_id: "run-3", counts: { gaps_new: { total: 0 } } } }, null);
  assert.equal(checkFoundText(none), "No new gaps");
});

test("before the first run, a failed run and a degraded run say so in words", () => {
  assert.deepEqual(checkText(toGapCheck({ status: "idle", result: null }, { status: "idle" })), {
    title: "No gap check yet",
    detail: "Specflow looks for gaps after each knowledge build.",
  });
  const failed = checkText(toGapCheck({ status: "failed", error_code: "gap_run_failed", result: null }, null));
  assert.equal(failed.title, "The last gap check failed");
  assert.match(failed.detail, /^Error code: gap_run_failed\. /);
  const degraded = checkText(toGapCheck({ ...completed, status: "degraded" }, null));
  assert.match(degraded.title, /^Gap check finished with problems /);
  assert.match(degraded.detail, /Some parts could not be checked/);
});

test("an unknown status is an invalid response", () => {
  assert.throws(() => toGapCheck({ status: "sleeping" }, null));
});

test("progress stays within 0..100 and a run without steps shows only its step", () => {
  const base: GapCheck = toGapCheck(running, null);
  assert.equal(checkProgress({ ...base, stepsTotal: 0 }), 0);
  assert.equal(checkProgress({ ...base, stepsDone: 9, itemsDone: 30 }), 100);
  assert.equal(checkStepText({ ...base, stepsTotal: 0, itemsTotal: null }), "Investigating");
  assert.equal(checkStepText({ ...base, step: null, itemsTotal: null }), "Step 6 of 9: Starting");
  assert.equal(checkStepText({ ...base, itemsTotal: 0 }), "Step 6 of 9: Investigating"); // a step with no sites
});

test("the check step counts sites too; a step not named shows bare counts and never more done than in all", () => {
  const base: GapCheck = toGapCheck(running, null);
  assert.equal(checkStepText({ ...base, step: "check", stepsDone: 6, itemsDone: 3 }), "Step 7 of 9: Checking the findings · 3 of 21 sites");
  assert.equal(
    checkStepText({ ...base, step: "route", stepsDone: 7, itemsDone: 2, itemsTotal: 4 }),
    "Step 8 of 9: Choosing who answers · 2 of 4",
  );
  assert.equal(checkStepText({ ...base, itemsDone: 30 }), "Step 6 of 9: Investigating · 21 of 21 sites");
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
