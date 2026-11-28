// The small rules the review of the canvas fixed (13): which answer a form may show, how often the gaps are read, which destinations the plan
// dialog names, when the dialog's focus moves, and what a refused build start says. Pure: no React.
import assert from "node:assert/strict";
import { test } from "node:test";
import "../support/css-stub-first.mjs"; // the components under test pull Graphico styles (.css)
import { buildStartFailure } from "@/components/v2/canvas/BuildDialog";
import { bodyKeyOf, destinationsOf } from "@/components/v2/canvas/CanvasDialogs";
import type { NodeView } from "@/components/v2/canvas/canvas-types";
import { signedFor } from "@/components/v2/canvas/signed";
import { gapsPollMs } from "@/components/v2/canvas/use-canvas-data";
import { DataError } from "@/lib/domain/errors";

test("an answer is shown only for the inputs it was asked with: a late check of an edited key is no check of the new key", () => {
  const held = { signature: "NWO|one", value: "ok" };
  assert.equal(signedFor(held, "NWO|one"), "ok");
  assert.equal(signedFor(held, "NWOX|one"), null, "another key");
  assert.equal(signedFor(held, "NWO|area"), null, "another grouping");
  assert.equal(signedFor(null, "NWO|one"), null);
});

test("the gaps are read every 5 s while a gap run or a build goes on (or nothing is known), every 30 s while nothing works", () => {
  for (const phase of ["queued", "running", "building"] as const) assert.equal(gapsPollMs(phase, false), 5000, phase);
  assert.equal(gapsPollMs(null, false), 5000, "nothing read yet");
  assert.equal(gapsPollMs("done", true), 5000, "a build is running: its gap run follows");
  for (const phase of ["never", "done", "degraded", "failed"] as const) assert.equal(gapsPollMs(phase, false), 30_000, phase);
});

const node = (kind: NodeView["kind"], name: string): Pick<NodeView, "kind" | "name"> => ({ kind, name });

test("the plan dialog names every plan destination (Jira, GitHub Projects), and nothing else", () => {
  assert.equal(destinationsOf([]), null);
  assert.equal(destinationsOf([node("jira", "Jira · NWO-1")]), "Jira · NWO-1");
  assert.equal(
    destinationsOf([node("jira", "Jira · NWO-1"), node("github_plan", "GitHub · Plan"), node("slack", "#x")]),
    "Jira · NWO-1, GitHub · Plan",
  );
  assert.equal(destinationsOf([node("github_plan", "GitHub · Plan")]), "GitHub · Plan");
});

test("the dialog's body key changes with the list, the form and back, so the focus moves; the same body keeps its key", () => {
  assert.equal(bodyKeyOf(null), null);
  const list = bodyKeyOf({ type: "add", zone: "graph" });
  const form = bodyKeyOf({ type: "form", kind: "jira", zone: "plan" });
  assert.notEqual(list, form);
  assert.notEqual(bodyKeyOf({ type: "add", zone: "gap" }), list);
  assert.equal(bodyKeyOf({ type: "gaps" }), "gaps");
  assert.equal(bodyKeyOf({ type: "form", kind: "jira", zone: "plan" }), form);
});

test("a build start the API refuses says why: 409 is a build already running, anything else a start that failed", () => {
  assert.match(buildStartFailure(new DataError("conflict", false)), /already running/);
  assert.match(buildStartFailure(new DataError("unavailable", true)), /could not be started/);
  assert.match(buildStartFailure(new Error("x")), /could not be started/);
});
