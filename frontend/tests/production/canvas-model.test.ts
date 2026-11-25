// The canvas view model (06a Do 1; AC9, AC10; README "The canvas", "Colour", "The flow" scenes 1-8): what each hub, node and wire shows for the
// state of the ports. Pure: no React, no DOM. The colour rules are asserted here as data: yellow only on the two decisions, never a status;
// a wire moves only while data moves; the stopped badge only for what the API reports.
import assert from "node:assert/strict";
import { test } from "node:test";
import { buildCanvasModel, graphDecisionOf, revealOf, stagesOf } from "@/components/v2/canvas/canvas-model";
import { NODE_MAPPERS } from "@/components/v2/connectors";
import type { CanvasModel } from "@/components/v2/canvas/canvas-types";
import { defined } from "../support/defined";
import { NO_GRAPH } from "@/lib/data/http/knowledge";
import {
  BUILT,
  FINISHED,
  GAPS,
  graphWire,
  input,
  NOW,
  READY_PLAN,
  RUNNING,
  SCENE_1,
  SCENE_2,
  SCENE_3,
  statusOf,
  wireSource,
} from "./canvas-fixtures";

const LATER = { created_at: "2026-10-26T09:30:00Z" };
const model = (i = SCENE_1): CanvasModel => buildCanvasModel(i, NODE_MAPPERS);
const wire = (m: CanvasModel, key: string) => m.wires.find((w) => w.key === key)?.motion;
const yellows = (m: CanvasModel) => Object.values(m.hubs).filter((h) => h.decision?.yellow === true).length;

test("scene 1, nothing connected: every hub grey, three node-sized plus tiles, every wire dashed, no decision", () => {
  const m = model();
  assert.deepEqual(
    Object.values(m.hubs).map((h) => h.state),
    ["off", "off", "off"],
  );
  assert.equal(m.hubs.graph.tooltip, "Add a source to start");
  assert.deepEqual(
    Object.values(m.zones).map((z) => z.bigAdd),
    [true, true, true],
  );
  assert.deepEqual(
    Object.values(m.zones).map((z) => z.nodes.length),
    [0, 0, 0],
  );
  assert.deepEqual(
    m.wires.map((w) => w.motion),
    ["off", "off"],
  );
  assert.equal(yellows(m), 0);
  assert.equal(m.reveal, 0);
  assert.deepEqual(
    Object.values(m.zones).map((z) => z.live),
    [false, false, false],
  );
});

test("a canvas whose reads have not come back shows the empty canvas, not an error", () => {
  const m = model(input());
  assert.equal(m.hubs.graph.state, "off");
  assert.equal(m.zones.graph.nodes.length, 0);
});

test("scene 2, importing: a node per source with its bar, the wires move, the Graph hub waits and says how many import", () => {
  const m = model(SCENE_2);
  const [drive, upload] = [defined(m.zones.graph.nodes[0]), defined(m.zones.graph.nodes[1])];
  assert.equal(drive.name, "Specs folder");
  assert.equal(drive.state, "busy");
  assert.equal(drive.badge, "spin");
  assert.equal(drive.meta, "6 of 24 files");
  assert.equal(drive.progress, 0.25);
  assert.equal(upload.meta, "4 of 6 files");
  assert.deepEqual([wire(m, "s1"), wire(m, "s2")], ["flow", "flow"]);
  assert.equal(m.hubs.graph.state, "wait");
  assert.equal(m.hubs.graph.tooltip, "Importing 2 of 2 sources…");
  assert.equal(m.hubs.graph.decision, undefined);
  assert.equal(m.zones.graph.bigAdd, false);
  assert.equal(m.zones.graph.live, true);
});

test("a source that still lists its files slides its bar and has no progress count", () => {
  const listing = input({ status: statusOf([wireSource("s1", "gdrive", {}, { listing_finished: false })], NO_GRAPH.graph, 0) });
  const node = defined(model(listing).zones.graph.nodes[0]);
  assert.equal(node.listing, true);
  assert.equal(node.meta, "Listing files…");
});

test("scene 3, ready to build: the Graph hub is the one yellow decision, 'Build graph'; the sources are ink with a check", () => {
  const m = model(SCENE_3);
  assert.deepEqual(m.hubs.graph.decision, { label: "Build graph", yellow: true });
  assert.equal(m.hubs.graph.state, "ready");
  assert.equal(yellows(m), 1);
  assert.deepEqual(
    m.zones.graph.nodes.map((n) => [n.state, n.badge, n.wire]),
    [
      ["on", "ok", "on"],
      ["on", "ok", "on"],
    ],
  );
  assert.equal(m.zones.graph.nodes[0]?.meta, "24 files · Google Drive");
  assert.equal(m.hubs.gap.state, "off");
  assert.equal(m.hubs.plan.decision, undefined);
});

test("the Graph decision comes from the V1 build button: 'Update graph' once built and sources changed; closed while a build runs", () => {
  const changed = BUILT({
    status: statusOf(
      [wireSource("s1", "gdrive", { done: 24 }), wireSource("s3", "upload", { done: 2 }, LATER)],
      graphWire({ status: "succeeded", mode: "update", startedAt: "2026-10-26T09:00:00Z", finishedAt: FINISHED }),
      26,
    ),
  });
  assert.equal(graphDecisionOf(changed)?.label, "Update graph");
  assert.equal(graphDecisionOf(BUILT()), null, "an up-to-date graph has no decision, though the V1 button stays active");
  assert.equal(graphDecisionOf(SCENE_1), null);
  assert.equal(graphDecisionOf(input({ status: statusOf([wireSource("s1", "gdrive", { done: 3 })], RUNNING) })), null);
});

test("building the graph: the hub is busy with a ring that turns, the wires into it move, the background starts to light, nothing is yellow", () => {
  const building = input({ status: statusOf([wireSource("s1", "gdrive", { done: 24 })], RUNNING) });
  const m = model(building);
  assert.equal(m.hubs.graph.state, "busy");
  assert.equal(m.hubs.graph.progress, "unknown");
  assert.equal(wire(m, "s1"), "flow");
  assert.equal(yellows(m), 0);
  assert.ok(m.reveal > 0 && m.reveal < 0.9, String(m.reveal));
  const later = revealOf({ ...building, now: new Date(NOW.getTime() + 3_600_000) }, stagesOf(building));
  assert.ok(later > m.reveal && later <= 0.9);
});

test("a graph that is built lights the whole background; an update build keeps it lit", () => {
  assert.equal(model(BUILT()).reveal, 1.01);
  const update = input({
    status: statusOf(
      [wireSource("s1", "gdrive", { done: 24 })],
      graphWire({ status: "running", mode: "update", startedAt: "2026-10-26T09:00:00Z" }),
    ),
  });
  assert.equal(model(update).reveal, 1.01);
});

test("a graph that is built waits for the gap run: Gaps busy, the wire into it moves, Plan waits, no MCP yet", () => {
  const m = model(BUILT({ gaps: { ...GAPS, phase: "running", stepsDone: 1, stepsTotal: 4 } }));
  assert.equal(m.hubs.gap.state, "busy");
  assert.equal(m.hubs.gap.progress, 0.25);
  assert.equal(wire(m, "graph-gap"), "flow");
  assert.equal(m.hubs.plan.state, "wait");
  assert.equal(m.hubs.plan.tooltip, "Waits while the gaps are found");
  assert.equal(m.zones.gap.nodes.length, 0);
  assert.equal(yellows(m), 0);
});

test("scene 5, gaps found: the critical count is a solid pill on Gaps, Plan is the yellow decision 'Build plan', MCP joins Gap by itself", () => {
  const m = model(BUILT({ gaps: GAPS }));
  assert.equal(m.hubs.gap.pill, "3 critical");
  assert.equal(m.hubs.gap.state, "on");
  assert.equal(m.hubs.gap.tooltip, "14 found · 2 answered");
  assert.deepEqual(m.hubs.plan.decision, { label: "Build plan", yellow: true });
  assert.equal(m.hubs.plan.tooltip, "Gaps found · 3 critical still open · ready to plan");
  assert.equal(yellows(m), 1);
  assert.deepEqual(
    m.zones.gap.nodes.map((n) => [n.id, n.kind, n.tag, n.wire]),
    [["mcp-gap", "mcp", "Automatic", "on"]],
  );
  assert.equal(m.zones.gap.bigAdd, true);
  assert.equal(m.zones.gap.live, true);
  assert.equal(wire(m, "mcp-gap"), "on");
  assert.equal(wire(m, "graph-gap"), "on");
  assert.equal(wire(m, "gap-plan"), "off");
});

test("no critical gap: no pill; a page of gaps that was full says 'at least'", () => {
  const m = model(BUILT({ gaps: { ...GAPS, critical: 0, total: 200, more: true } }));
  assert.equal(m.hubs.gap.pill, undefined);
  assert.equal(m.hubs.gap.tooltip, "200+ found · 2 answered");
  assert.equal(m.hubs.plan.tooltip, "Gaps found · ready to plan");
});

test("both decisions open at once: the graph build keeps the yellow, the plan button is ink", () => {
  const changed = input({
    status: statusOf(
      [wireSource("s1", "gdrive", { done: 24 }), wireSource("s3", "upload", { done: 2 }, LATER)],
      graphWire({ status: "succeeded", mode: "update", startedAt: "2026-10-26T09:00:00Z", finishedAt: FINISHED }),
      26,
    ),
    gaps: GAPS,
  });
  const m = model(changed);
  assert.deepEqual(m.hubs.graph.decision, { label: "Update graph", yellow: true });
  assert.deepEqual(m.hubs.plan.decision, { label: "Build plan", yellow: false });
  assert.equal(yellows(m), 1);
});

test("planning: Plan is busy with its steps as the ring, the wire from Gaps moves, nothing is yellow", () => {
  const m = model(BUILT({ gaps: GAPS, plan: { phase: "creating", runId: "r1", step: 2, total: 8, label: "Areas", items: null } }));
  assert.equal(m.hubs.plan.state, "busy");
  assert.equal(m.hubs.plan.progress, 0.25);
  assert.equal(wire(m, "gap-plan"), "flow");
  assert.equal(yellows(m), 0);
});

test("scene 6, the plan is built: Plan is ink with its counts, the wire is solid, MCP joins Plan, no decision", () => {
  const m = model(BUILT({ gaps: GAPS, plan: READY_PLAN }));
  assert.equal(m.hubs.plan.state, "on");
  assert.equal(m.hubs.plan.tooltip, "2 stories · 1 task");
  assert.equal(m.hubs.plan.decision, undefined);
  assert.equal(wire(m, "gap-plan"), "on");
  assert.deepEqual(
    m.zones.plan.nodes.map((n) => n.id),
    ["mcp-plan"],
  );
  assert.equal(m.zones.plan.bigAdd, false);
  assert.equal(m.zones.plan.live, true);
  assert.equal(yellows(m), 0);
});

test("scene 7, new information: with a plan the decision is 'Replan', yellow again", () => {
  const m = model(BUILT({ gaps: GAPS, plan: READY_PLAN, planStale: true }));
  assert.deepEqual(m.hubs.plan.decision, { label: "Replan", yellow: true });
  assert.equal(m.hubs.plan.state, "ready");
});

test("a source the API reports as failed is the one stopped state: a stop badge, grey, its wire dashed", () => {
  const failed = input({
    status: statusOf(
      [wireSource("s1", "gdrive", { failed: 3 }, { failure_reason: "no_access" }), wireSource("s2", "upload", { done: 2 })],
      NO_GRAPH.graph,
      2,
    ),
  });
  const nodes = model(failed).zones.graph.nodes;
  const [drive, upload] = [defined(nodes[0]), defined(nodes[1])];
  assert.deepEqual([drive.state, drive.badge, drive.meta, drive.wire], ["stopped", "stop", "Cannot open the folder", "off"]);
  assert.deepEqual([upload.state, upload.badge], ["on", "ok"]);
  assert.equal(
    model(SCENE_3).zones.graph.nodes.some((n) => n.badge === "stop"),
    false,
  );
});

test("nodes are one per object, in the order they were added; MCP stays last", () => {
  const m = model(BUILT({ gaps: GAPS, plan: READY_PLAN }));
  assert.deepEqual(
    m.zones.graph.nodes.map((n) => n.id),
    ["s1", "s2"],
  );
  const added = model(input({ status: statusOf([wireSource("s2", "upload", { done: 1 }), wireSource("s1", "gdrive", { done: 1 })]) }));
  assert.deepEqual(
    added.zones.graph.nodes.map((n) => n.id),
    ["s1", "s2"],
  );
});

test("every node of a source reads the same text as the V1 card and names its app", () => {
  const upload = defined(model(SCENE_3).zones.graph.nodes[1]);
  assert.equal(upload.mark, "upload");
  assert.match(upload.name, /^Upload /, "short enough for a node: no long stamp");
  assert.equal(upload.label.startsWith("Upload "), true);
});

test("a build just started here shows busy at once, with no decision, until the status catches up", () => {
  const m = model(input({ ...SCENE_3, pending: { build: true, plan: false } }));
  assert.equal(m.hubs.graph.state, "busy");
  assert.equal(m.hubs.graph.decision, undefined);
  assert.equal(m.hubs.graph.progress, "unknown");
  assert.equal(yellows(m), 0);
  assert.equal(wire(m, "s1"), "flow");
  assert.equal(model(SCENE_3).hubs.graph.decision?.label, "Build graph");
});

test("a plan just started here shows busy at once and offers no decision, with the wire from Gap moving", () => {
  const m = model(BUILT({ gaps: GAPS, pending: { build: false, plan: true } }));
  assert.equal(m.hubs.plan.state, "busy");
  assert.equal(m.hubs.plan.decision, undefined);
  assert.equal(wire(m, "gap-plan"), "flow");
  assert.equal(yellows(m), 0);
});
