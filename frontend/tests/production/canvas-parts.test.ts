// The small pure parts of the canvas: the wire geometry, the background graph, the gap summary, the plan-due rule, the registry and the
// route query. The DOM-measuring hook of `canvas-geometry.ts` is exercised in the browser (e2e-production/canvas-production.spec.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import "../support/css-stub-first.mjs"; // the components under test pull Graphico styles (.css)
import { backdropOf } from "@/components/v2/canvas/canvas-backdrop";
import { summarizeGaps } from "@/components/v2/canvas/canvas-gaps";
import { wirePath, type Box } from "@/components/v2/canvas/canvas-geometry";
import { isPlanStale } from "@/components/v2/canvas/use-canvas-data";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import type { GapCheck } from "@/lib/domain/gaps";
import { parseCanvasQuery, routesFor } from "@/lib/routes";
import { gapOf } from "./canvas-fixtures";

const box = (l: number, t: number, size = 80): Box => ({ l, t, r: l + size, b: t + size, cx: l + size / 2, cy: t + size / 2 });

test("a wire to a tile on the right leaves the right edge sideways and ends at the left edge of the target", () => {
  const { d, ends } = wirePath(box(0, 100), box(300, 100), false);
  assert.deepEqual(ends, [
    [86, 140],
    [294, 140],
  ]);
  assert.match(d, /^M86 140 C\d+ 140 \d+ 140 294 140$/);
});

test("a wire marked down, or to a tile under the source, runs vertically from the bottom edge to the top edge", () => {
  const { ends } = wirePath(box(100, 0), box(100, 300), true);
  assert.deepEqual(ends, [
    [140, 86],
    [140, 294],
  ]);
  assert.deepEqual(wirePath(box(100, 300), box(100, 0), true).ends, [
    [140, 294],
    [140, 86],
  ]);
  assert.deepEqual(wirePath(box(100, 0), box(100, 300), false).ends, [
    [140, 86],
    [140, 294],
  ]);
});

test("the background graph is the same picture every time and lights up from the hub outward", () => {
  const input = { width: 1000, height: 600, hub: { cx: 800, cy: 300 }, holes: [], clearPaths: [] };
  const none = backdropOf({ ...input, reveal: 0 });
  const half = backdropOf({ ...input, reveal: 0.5 });
  const all = backdropOf({ ...input, reveal: 1.01 });
  assert.equal(none.dots.length, 60);
  assert.equal(
    none.dots.some((p) => p.lit),
    false,
  );
  assert.equal(
    all.dots.every((p) => p.lit),
    true,
  );
  assert.equal(
    all.lines.every((l) => l.lit),
    true,
  );
  const lit = half.dots.filter((p) => p.lit);
  assert.ok(lit.length > 0 && lit.length < 60);
  assert.deepEqual(backdropOf({ ...input, reveal: 0.5 }), half);
  const nearest = half.dots.toSorted((a, b) => Math.hypot(a.x - 800, a.y - 300) - Math.hypot(b.x - 800, b.y - 300));
  assert.equal(nearest[0]?.lit, true);
});

const check = (phase: GapCheck["phase"]): GapCheck => ({
  phase,
  step: null,
  stepsDone: 2,
  stepsTotal: 5,
  itemsDone: null,
  itemsTotal: null,
  startedAt: null,
  finishedAt: null,
  newGaps: null,
  updatedGaps: null,
  resolvedGaps: null,
  errorCode: null,
});

test("the gap summary counts what is found, what is answered and the open blockers; a full page says there are more", () => {
  const page = {
    gaps: [gapOf("open", "blocker"), gapOf("open", "major"), gapOf("answered", "blocker"), gapOf("resolved", "minor")],
    next: "c2",
  };
  assert.deepEqual(summarizeGaps(check("running"), page), {
    phase: "running",
    stepsDone: 2,
    stepsTotal: 5,
    total: 4,
    answered: 2,
    critical: 1,
    more: true,
  });
  assert.equal(summarizeGaps(check("done"), { gaps: [], next: null }).more, false);
});

test("the plan is due for a replan only when a graph build finished after this screen saw the plan", () => {
  assert.equal(isPlanStale(undefined, "t1", true), false, "not seen yet");
  assert.equal(isPlanStale("t1", "t1", true), false);
  assert.equal(isPlanStale("t1", "t2", true), true);
  assert.equal(isPlanStale(null, "t2", true), true, "the first build finished after a plan seen with no build time");
  assert.equal(isPlanStale("t1", "t2", false), false, "no plan");
});

test("the registry lists each kind once; only MCP is automatic, each kind has a mapper and the apps are the ones D20 and the Notion pull request allow", () => {
  assert.deepEqual(
    CONNECTORS.map((c) => c.kind),
    [
      "upload",
      "gdrive",
      "github",
      "confluence",
      "jira_epic",
      "drawio",
      "linear_project",
      "box",
      "dropbox",
      "notion",
      "slack",
      "jira_gap",
      "linear_gaps",
      "jira",
      "github_plan",
      "linear_plan",
      "notion_plan",
      "mcp",
    ],
  );
  assert.deepEqual(
    CONNECTORS.filter((c) => c.zone === null).map((c) => c.kind),
    ["mcp"],
  );
  assert.deepEqual(
    CONNECTORS.filter((c) => c.zone === "graph").map((c) => c.app),
    [null, null, "github", "atlassian", "atlassian", null, "linear", "box", "dropbox", "notion"],
  );
  assert.deepEqual(
    CONNECTORS.filter((c) => c.zone === "plan").map((c) => [c.kind, c.app]),
    [
      ["jira", "atlassian"],
      ["github_plan", "github"],
      ["linear_plan", "linear"],
      ["notion_plan", "notion"],
    ],
  );
  assert.equal(
    CONNECTORS.find((c) => c.kind === "slack")?.app,
    null,
    "the channel is the Slack integration: it is bound from inside Slack",
  );
  assert.equal(NODE_MAPPERS.length, 18);
  assert.equal(CONNECTORS.find((c) => c.kind === "gdrive")?.check?.add, "Add source");
  assert.equal(CONNECTORS.find((c) => c.kind === "jira")?.check?.test, "Check access");
  assert.equal(CONNECTORS.find((c) => c.kind === "notion")?.check, null, "Notion pages are ticked from a list: no check step");
  assert.deepEqual(CONNECTORS.find((c) => c.kind === "notion_plan")?.check, {
    test: "Check access",
    testing: "Checking…",
    add: "Add destination",
  });
  assert.deepEqual(CONNECTORS.find((c) => c.kind === "slack")?.check, { test: "Test connection", testing: "Posting…", add: "Add channel" });
});

test("the canvas route carries the form to open after the Integrations round trip", () => {
  const routes = routesFor("production");
  assert.equal(routes.canvas("p1"), "/projects/p1");
  assert.equal(routes.canvas("p1", { add: "slack" }), "/projects/p1?add=slack");
  assert.deepEqual(parseCanvasQuery(new URLSearchParams("add=slack")), { add: "slack" });
  assert.deepEqual(parseCanvasQuery(new URLSearchParams("")), { add: null });
  const next = routes.integrations("p1", { setup: "slack", next: routes.canvas("p1", { add: "slack" }) });
  assert.equal(new URLSearchParams(next.split("?")[1]).get("next"), "/projects/p1?add=slack");
});
