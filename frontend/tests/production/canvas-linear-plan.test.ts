// The Linear plan destination on the canvas: what the node says in each state the API reports (counts, the link to the Linear project), the words
// of a refusal, the line of a passed check, the registry entry and the form. Server-rendered: no browser.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { CanvasInput, NodeView } from "@/components/v2/canvas/canvas-types";
import { LinearPlanFormBody } from "@/components/v2/canvas/LinearPlanForm";
import {
  linearAccessOutcome,
  linearCountsText,
  linearPassText,
  linearPlanCheckText,
  linearPlanFailureText,
} from "@/components/v2/canvas/linear-plan-logic";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataError } from "@/lib/domain/errors";
import type { LinearPlan } from "@/lib/domain/linear-plan";
import { defined } from "../support/defined";
import { formProps, html } from "./canvas-form-support";
import { BUILT, GAPS, integration, READY_PLAN } from "./canvas-fixtures";

const COUNTS = { created: 0, updated: 0, commented: 0, removed: 0, relationsAdded: 0, estimateRefused: 0, unchanged: 0, failed: 0 };
const PLAN: LinearPlan = {
  available: true,
  enabled: true,
  teamId: "t1",
  teamKey: "ENG",
  linearProjectId: "lp1",
  linearProjectName: "Roadmap",
  linearProjectUrl: "https://linear.app/acme/project/roadmap-1a2b3c",
  createdByGraphico: false,
  workspace: "Graphico",
  connectedByMe: false,
  sync: { status: "succeeded", queued: false, finishedAt: "2026-11-25T09:00:00Z", errorCode: null, counts: { ...COUNTS, created: 6 } },
};
const nodeOf = (over: Partial<LinearPlan>, input: Partial<CanvasInput> = {}): NodeView =>
  defined(
    buildCanvasModel(
      BUILT({ gaps: GAPS, plan: READY_PLAN, linearPlan: { ...PLAN, ...over }, ...input }),
      NODE_MAPPERS,
    ).zones.plan.nodes.find((n) => n.kind === "linear_plan"),
  );
const sync = (over: Partial<NonNullable<LinearPlan["sync"]>>) => ({ sync: { ...defined(PLAN.sync), ...over } });

test("the node counts the issues made, under Plan, with the Linear mark, and links to the Linear project", () => {
  const n = nodeOf({});
  assert.deepEqual(
    [n.zone, n.mark, n.name, n.state, n.badge, n.meta, n.wire],
    ["plan", "linear", "Linear · Roadmap", "on", "ok", "6 issues · Linear", "on"],
  );
  assert.deepEqual(n.detail.link, { href: "https://linear.app/acme/project/roadmap-1a2b3c", label: "Open in Linear" });
  assert.equal(nodeOf(sync({ counts: { ...COUNTS, created: 1 } })).meta, "1 issue · Linear");
  assert.equal(nodeOf(sync({ counts: COUNTS })).meta, "In sync");
  const never = nodeOf(sync({ status: "idle", counts: null }));
  assert.deepEqual([never.meta, never.badge, never.state], ["Not synced yet", null, "on"]);
  assert.equal(nodeOf({ linearProjectUrl: null }).detail.link, undefined);
});

test("the dialog says how the last pass went: created, updated, commented and failed", () => {
  const detail = nodeOf(sync({ status: "partial", counts: { ...COUNTS, created: 5, updated: 2, commented: 1, failed: 1 } })).detail.text;
  assert.match(detail, /could not do everything/);
  assert.match(detail, /Last pass: 5 created · 2 updated · 1 commented · 1 failed\./);
  assert.equal(linearCountsText(COUNTS), "Last pass: 0 created · 0 updated · 0 commented · 0 failed.");
  assert.match(nodeOf(sync({ errorCode: "linear_estimate_refused" })).detail.text, /estimate scale refused some points/);
});

test("the wire moves only while a pass runs", () => {
  const run = nodeOf(sync({ status: "running", counts: null }));
  assert.deepEqual([run.state, run.badge, run.wire, run.meta], ["busy", "spin", "flow", "Syncing…"]);
  assert.equal(nodeOf({}).wire, "on");
});

test("it waits for the plan, and while it is off; with no team there is no node", () => {
  const waiting = nodeOf({}, { plan: null });
  assert.deepEqual([waiting.state, waiting.wire, waiting.meta], ["wait", "off", "Waits for the plan"]);
  assert.deepEqual([nodeOf({ enabled: false }).state, nodeOf({ enabled: false }).meta], ["wait", "Sync is off"]);
  const none = buildCanvasModel(BUILT({ gaps: GAPS, plan: READY_PLAN, linearPlan: { ...PLAN, teamKey: null } }), NODE_MAPPERS);
  assert.equal(
    none.zones.plan.nodes.some((n) => n.kind === "linear_plan"),
    false,
  );
  assert.equal(
    buildCanvasModel(BUILT({ gaps: GAPS, plan: READY_PLAN }), NODE_MAPPERS).zones.plan.nodes.some((n) => n.kind === "linear_plan"),
    false,
  );
});

test("it stops only for what the API reports: a lost Linear connection offers Reconnect, the codes of a failed pass say what they are", () => {
  for (const code of ["linear_not_connected", "linear_reconnect_required"]) {
    const n = nodeOf(sync({ status: "failed", errorCode: code }));
    assert.deepEqual([n.state, n.badge, n.wire, n.meta, n.detail.reconnect], ["stopped", "stop", "off", "Stopped · reconnect", "linear"]);
  }
  const bound = nodeOf({}, { integrations: [integration("linear", { health: "reconnect_required" })] });
  assert.deepEqual([bound.state, bound.detail.reconnect], ["stopped", "linear"]);
  const missing = nodeOf(sync({ status: "failed", errorCode: "linear_project_not_found" }));
  assert.deepEqual([missing.state, missing.meta, missing.detail.reconnect], ["stopped", "Stopped", null]);
  assert.match(missing.detail.text, /Linear project does not exist any more/);
  const unknown = nodeOf(sync({ status: "failed", errorCode: "unexpected_error" }));
  assert.deepEqual([unknown.state, unknown.meta, unknown.detail.reconnect], ["stopped", "Stopped", null]);
  assert.match(unknown.detail.text, /reason Graphico does not know/);
});

test("a skipped pass is a wait with the words of its code, not a stop and not 'Not synced yet'", () => {
  const skipped = (errorCode: string) => nodeOf(sync({ status: "skipped", errorCode, counts: null }));
  for (const [code, words] of [
    ["no_plan", "Waits for the plan"],
    ["plan_applying", "The plan is being applied"],
    ["target_changed", "The destination changed; the next pass uses the new one"],
  ] as const) {
    const n = skipped(code);
    assert.deepEqual([n.state, n.badge, n.wire, n.meta], ["wait", null, "off", words]);
  }
  assert.match(linearPassText("linear_rate_limited"), /Graphico tries again by itself/);
});

test("each closed code has its own words; an unknown one is the general failure", () => {
  const failure = (apiCode: string) => linearPlanFailureText(new DataError("conflict", false, { apiCode }));
  assert.match(failure("linear_not_connected"), /Connect Linear to this project/);
  assert.match(failure("linear_reconnect_required"), /connection expired/);
  assert.match(failure("linear_team_not_found"), /team does not exist/);
  assert.match(failure("linear_project_not_found"), /project does not exist/);
  assert.match(failure("linear_forbidden"), /not allowed/);
  assert.match(failure("linear_unavailable"), /not available on this server/);
  assert.equal(failure("something_new"), "Something went wrong. Try again.");
  assert.throws(
    () => linearAccessOutcome({ result: "failed", reason: "linear_team_not_found", field: "team" }),
    (e) => e instanceof DataError && e.detail?.apiCode === "linear_team_not_found",
  );
  assert.deepEqual(linearAccessOutcome({ result: "ok", teamKey: "ENG", projectName: null }), { teamKey: "ENG", projectName: null });
});

test("a passed check names the team and the project used or the one to be made", () => {
  const found = { teamKey: "ENG", projectName: "Roadmap" };
  assert.equal(linearPlanCheckText(found, { teamId: "t", projectId: "lp1" }, "Shop"), "Team ENG. Project “Roadmap”.");
  assert.equal(
    linearPlanCheckText({ teamKey: "ENG", projectName: null }, { teamId: "t", projectId: null }, "Shop"),
    "Team ENG. A project named “Shop” will be created.",
  );
});

test("the registry has Linear under Plan, needing Linear, with the add label Add destination", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "linear_plan"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["plan", "linear", "linear", "Linear"]);
  assert.deepEqual(spec.check, { test: "Check access", testing: "Checking…", add: "Add destination" });
});

test("the form offers the member's teams and, by default, a project named after this one; Check access is on", () => {
  const view = html(
    createElement(LinearPlanFormBody, {
      ...formProps("plan", "linear_plan"),
      teams: [
        { id: "t1", key: "ENG", name: "Engineering" },
        { id: "t2", key: "OPS", name: "Operations" },
      ],
      name: "Northwind shop",
    }),
  );
  assert.match(view, />Team<\/label>/);
  assert.match(view, /<option value="t1" selected="">Engineering \(ENG\)<\/option>/);
  assert.match(view, /<option value="t2">Operations \(OPS\)<\/option>/);
  assert.match(view, />Project<\/label>/);
  assert.match(view, /<option value="new" selected="">Create “Northwind shop”<\/option>/);
  assert.match(view, /Graphico creates a Linear project with this name in the team/);
  assert.match(view, /Reading the projects…/);
  assert.doesNotMatch(view, /disabled=""[^>]*>Check access<\/button>/);
});
