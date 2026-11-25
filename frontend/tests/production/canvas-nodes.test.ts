// The node mappers of 06b (GitHub repository, Slack channel, Jira plan) and the pieces around them: what each node says in each state the
// APIs report, that a stop is only an API-reported one, and which hub presses open which dialog. Pure: no React.
import assert from "node:assert/strict";
import { test } from "node:test";
import { openGapRows } from "@/components/v2/canvas/canvas-gaps";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { CanvasInput, NodeView } from "@/components/v2/canvas/canvas-types";
import { hubDialog } from "@/components/v2/canvas/CanvasScreen";
import { uploadName } from "@/components/v2/canvas/nodes-sources";
import { NODE_MAPPERS } from "@/components/v2/connectors";
import { defined } from "../support/defined";
import { BUILT, GAPS, gapOf, integration, jiraTarget, READY_PLAN, repo, repos, SCENE_3 } from "./canvas-fixtures";

const nodesOf = (i: CanvasInput, zone: "graph" | "gap" | "plan"): NodeView[] => buildCanvasModel(i, NODE_MAPPERS).zones[zone].nodes;
const only = (nodes: NodeView[], kind: string): NodeView => defined(nodes.find((n) => n.kind === kind));
const wire = (i: CanvasInput, key: string) => buildCanvasModel(i, NODE_MAPPERS).wires.find((w) => w.key === key);

// ---- GitHub repositories ----

test("a cloned repository is a node named after the repository, ink with a check; the build does not read it and the detail says so", () => {
  const node = only(nodesOf(BUILT({ github: repos([repo()]) }), "graph"), "github");
  assert.deepEqual([node.name, node.state, node.badge, node.wire], ["ordering", "on", "ok", "on"]);
  assert.equal(node.meta, "Cloned · GitHub");
  assert.match(node.detail.text, /Cloned\. 3f2a1c9 · 18 files\. The build does not read repositories yet\./);
  assert.deepEqual(
    [node.detail.app, node.detail.by, node.detail.remove, node.detail.reconnect],
    ["GitHub", "Added by Alex Kim", "repository", null],
  );
});

test("a repository adds nothing to the graph: with only repositories the Graph hub still waits for a source", () => {
  const m = buildCanvasModel(BUILT({ github: repos([repo()]) }), NODE_MAPPERS);
  assert.equal(m.hubs.graph.decision, undefined);
  const alone = buildCanvasModel({ ...SCENE_3, status: null, github: repos([repo()]) }, NODE_MAPPERS);
  assert.equal(alone.hubs.graph.decision, undefined);
  assert.equal(alone.hubs.graph.state, "off");
});

test("a repository that is cloning spins and moves its wire; a clone that failed is stopped with the API's reason", () => {
  const cloning = only(nodesOf(BUILT({ github: repos([repo({ cloneState: "cloning" })]) }), "graph"), "github");
  assert.deepEqual([cloning.state, cloning.badge, cloning.wire, cloning.meta], ["busy", "spin", "flow", "Cloning"]);
  const failed = only(
    nodesOf(BUILT({ github: repos([repo({ cloneState: "failed", failureCode: "github_repo_too_large" })]) }), "graph"),
    "github",
  );
  assert.deepEqual([failed.state, failed.badge, failed.wire], ["stopped", "stop", "off"]);
  assert.match(failed.detail.text, /larger than 1 GB/);
  assert.equal(failed.detail.reconnect, null, "a failed clone is not a lost account");
});

test("scene 8, a connection stopped: GitHub needs a reconnect, so every repository stops and offers to reconnect GitHub", () => {
  const input = BUILT({
    github: repos([repo(), repo({ id: "r2", fullName: "northwind/deploy" })]),
    integrations: [integration("github", { health: "reconnect_required" })],
  });
  const nodes = nodesOf(input, "graph").filter((n) => n.kind === "github");
  assert.deepEqual(
    nodes.map((n) => [n.name, n.state, n.badge, n.meta]),
    [
      ["ordering", "stopped", "stop", "Stopped · reconnect"],
      ["deploy", "stopped", "stop", "Stopped · reconnect"],
    ],
  );
  assert.deepEqual(
    nodes.map((n) => n.detail.reconnect),
    ["github", "github"],
  );
  assert.match(defined(nodes[0]).detail.text, /no access now/);
  assert.equal(wire(input, "r1")?.motion, "off");
  const working = nodesOf(BUILT({ github: repos([repo()]), integrations: [integration("github")] }), "graph").filter(
    (n) => n.kind === "github",
  );
  assert.equal(working[0]?.state, "on");
});

test("a reconnect-required clone failure offers the reconnect; a collaborator who cannot manage gets no Remove", () => {
  const failed = repo({ cloneState: "failed", failureCode: "github_reconnect_required" });
  assert.equal(only(nodesOf(BUILT({ github: repos([failed]) }), "graph"), "github").detail.reconnect, "github");
  assert.equal(only(nodesOf(BUILT({ github: repos([repo()], false) }), "graph"), "github").detail.remove, null);
});

// ---- Slack channel ----

const CHANNEL = { available: true, editable: true, channel: { id: "C1", name: "northwind-specs", private: false } };

test("a Slack channel waits for the gaps with a dashed wire, then shows what was found and answered", () => {
  const waiting = only(nodesOf(BUILT({ slack: CHANNEL }), "gap"), "slack");
  assert.deepEqual([waiting.name, waiting.state, waiting.wire, waiting.meta], ["#northwind-specs", "wait", "off", "Waits for gaps"]);
  const found = only(nodesOf(BUILT({ slack: CHANNEL, gaps: GAPS }), "gap"), "slack");
  assert.deepEqual([found.state, found.badge, found.meta, found.wire], ["on", "ok", "14 found · 2 answered", "on"]);
  assert.equal(found.detail.remove, null);
  const asking = only(nodesOf(BUILT({ slack: CHANNEL, gaps: { ...GAPS, phase: "running" } }), "gap"), "slack");
  assert.equal(asking.state, "wait", "a running gap run has not found its gaps yet");
});

test("no channel, no node; a private channel says so; one node per project", () => {
  assert.equal(nodesOf(BUILT({ slack: { ...CHANNEL, channel: null } }), "gap").length, 0);
  const priv = only(nodesOf(BUILT({ slack: { ...CHANNEL, channel: { id: "C2", name: "secret", private: true } } }), "gap"), "slack");
  assert.equal(priv.name, "#secret (private)");
});

// ---- Jira plan ----

const planned = (j: ReturnType<typeof jiraTarget>, over: Partial<CanvasInput> = {}) =>
  BUILT({ gaps: GAPS, plan: READY_PLAN, jira: j, ...over });

test("a Jira target waits for the plan; with a plan it shows its sync; the wire moves only while a pass runs", () => {
  const waiting = only(nodesOf(BUILT({ gaps: GAPS, jira: jiraTarget() }), "plan"), "jira");
  assert.deepEqual([waiting.name, waiting.state, waiting.meta, waiting.wire], ["Jira · NWO-1", "wait", "Waits for the plan", "off"]);
  const syncing = jiraTarget({
    sync: { status: "running", finishedAt: null, errorCode: null, progress: { step: "issues", done: 3, total: 12 } },
  });
  const run = only(nodesOf(planned(syncing), "plan"), "jira");
  assert.deepEqual([run.state, run.badge, run.meta, run.wire, run.progress], ["busy", "spin", "Syncing…", "flow", 0.25]);
  const done = only(
    nodesOf(
      planned(jiraTarget({ sync: { status: "succeeded", finishedAt: "2026-11-25T09:00:00Z", errorCode: null, progress: null } })),
      "plan",
    ),
    "jira",
  );
  assert.deepEqual([done.state, done.badge, done.meta, done.wire], ["on", "ok", "Synced", "on"]);
  const idle = only(nodesOf(planned(jiraTarget()), "plan"), "jira");
  assert.deepEqual([idle.state, idle.badge, idle.meta], ["on", null, "Not synced yet"]);
});

test("a Jira node stops only for what the API reports: an expired connection or a failed pass; its sync switch off waits", () => {
  const expired = only(
    nodesOf(planned(jiraTarget({ connection: { connectedByName: "Alex Kim", connectedByMe: false, status: "expired" } })), "plan"),
    "jira",
  );
  assert.deepEqual([expired.state, expired.badge, expired.detail.reconnect], ["stopped", "stop", "atlassian"]);
  const viaIntegration = only(
    nodesOf(planned(jiraTarget(), { integrations: [integration("atlassian", { health: "reconnect_required" })] }), "plan"),
    "jira",
  );
  assert.equal(viaIntegration.detail.reconnect, "atlassian");
  const failed = only(
    nodesOf(planned(jiraTarget({ sync: { status: "failed", finishedAt: null, errorCode: "jira_forbidden", progress: null } })), "plan"),
    "jira",
  );
  assert.deepEqual([failed.state, failed.badge, failed.detail.reconnect], ["stopped", "stop", null]);
  assert.match(failed.detail.text, /may not create/);
  const off = only(nodesOf(planned(jiraTarget({ enabled: false })), "plan"), "jira");
  assert.deepEqual([off.state, off.meta], ["wait", "Sync is off"]);
  assert.equal(
    nodesOf(planned(jiraTarget({ jiraProject: null, epicKey: null })), "plan").some((n) => n.kind === "jira"),
    false,
  );
  assert.equal(only(nodesOf(planned(jiraTarget({ epicKey: null, createdEpicKey: "NWO-77" })), "plan"), "jira").name, "Jira · NWO-77");
});

// ---- the gap run, the hubs, the wires ----

test("a failed gap run found nothing: no 'found', no yellow Build plan, no MCP; the hubs say so", () => {
  const input = BUILT({ gaps: { ...GAPS, phase: "failed" } });
  const m = buildCanvasModel(input, NODE_MAPPERS);
  assert.equal(m.hubs.plan.decision, undefined);
  assert.equal(m.hubs.plan.tooltip, "The gap run failed; the plan waits");
  assert.deepEqual([m.hubs.gap.state, m.hubs.gap.tooltip, m.hubs.gap.pill], ["wait", "The gap run failed", undefined]);
  assert.equal(m.zones.gap.nodes.length, 0);
  assert.equal(m.zones.gap.live, false);
});

test("a built graph whose gap run was never made: the Gap hub waits for the run (not on), nothing is found and Build plan is not offered", () => {
  const m = buildCanvasModel(BUILT({ gaps: { ...GAPS, phase: "never", total: 0, answered: 0, critical: 0 } }), NODE_MAPPERS);
  assert.deepEqual([m.hubs.gap.state, m.hubs.gap.tooltip, m.hubs.gap.pill], ["wait", "Waits for the gap run", undefined]);
  assert.equal(m.hubs.plan.decision, undefined);
  assert.equal(m.zones.gap.live, false);
  const done = buildCanvasModel(BUILT({ gaps: { ...GAPS, phase: "done" } }), NODE_MAPPERS);
  assert.equal(done.hubs.gap.state, "on");
});

test("the hub names are the stage names: Graph, Gap, Plan", () => {
  const m = buildCanvasModel(BUILT({ gaps: GAPS }), NODE_MAPPERS);
  assert.deepEqual(
    Object.values(m.hubs).map((h) => h.label),
    ["Graph", "Gap", "Plan"],
  );
});

test("wires carry the stage of their node, so the geometry (not the model) decides how a stage's wires run", () => {
  const input = BUILT({ gaps: GAPS, plan: READY_PLAN, slack: CHANNEL, jira: jiraTarget(), github: repos([repo()]) });
  const m = buildCanvasModel(input, NODE_MAPPERS);
  assert.equal(wire(input, "r1")?.zone, "graph");
  assert.equal(wire(input, "slack-C1")?.zone, "gap");
  assert.equal(wire(input, "jira-plan")?.zone, "plan");
  assert.deepEqual([wire(input, "graph-gap")?.zone, wire(input, "gap-plan")?.zone], [null, null]);
  assert.ok(m.wires.every((w) => !("down" in w)));
});

test("what a hub press opens: the decision it carries, the gaps once they run, nothing otherwise", () => {
  const open = buildCanvasModel(SCENE_3, NODE_MAPPERS);
  assert.deepEqual(hubDialog(open.hubs.graph), { type: "build" });
  assert.equal(hubDialog(open.hubs.gap), null);
  assert.equal(hubDialog(open.hubs.plan), null);
  const gaps = buildCanvasModel(BUILT({ gaps: GAPS }), NODE_MAPPERS);
  assert.deepEqual(hubDialog(gaps.hubs.plan), { type: "plan" });
  assert.deepEqual(hubDialog(gaps.hubs.gap), { type: "gaps" });
  assert.equal(hubDialog(gaps.hubs.graph), null);
});

// ---- the open gaps, the upload name ----

const gap = (id: string, status: "open" | "answered", severity: "blocker" | "major" | "minor") =>
  gapOf(status, severity, { id, title: `Gap ${id}` });

test("the open gaps list puts blockers first and leaves the settled ones out", () => {
  const rows = openGapRows({
    gaps: [gap("a", "open", "minor"), gap("b", "answered", "blocker"), gap("c", "open", "blocker"), gap("d", "open", "major")],
    next: null,
  });
  assert.deepEqual(
    rows.map((r) => r.id),
    ["c", "a", "d"],
  );
});

test("an upload is named short: the time today, the day before, a number for a same-minute batch", () => {
  const now = new Date(2026, 10, 25, 12, 0);
  const at = (d: Date, nameSuffix = 1) => uploadName({ addedAt: d.toISOString(), nameSuffix }, now);
  assert.equal(at(new Date(2026, 10, 25, 10, 42)), "Upload 10:42");
  assert.equal(at(new Date(2026, 10, 20, 10, 42)), "Upload 20 Nov");
  assert.equal(at(new Date(2026, 10, 25, 10, 42), 2), "Upload 10:42 (2)");
  assert.ok(at(new Date(2026, 10, 20, 10, 42), 12).length <= 20);
});
