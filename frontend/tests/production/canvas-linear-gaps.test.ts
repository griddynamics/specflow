// The Linear gap sub-issues on the canvas: what the node says in each state the API reports, the registry entry, the form's fields and the
// outcome of a check. Pure and server-rendered: no browser.
import "../support/css-stub-first.mjs"; // the components under test pull Graphico styles (.css)
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { CanvasInput, NodeView } from "@/components/v2/canvas/canvas-types";
import { LinearGapFormBody, outcomeOf, phaseOf, problemOf } from "@/components/v2/canvas/LinearGapForm";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataError } from "@/lib/domain/errors";
import type { LinearGaps } from "@/lib/domain/linear-gaps";
import { defined } from "../support/defined";
import { formProps, html } from "./canvas-form-support";
import { BUILT, GAPS, integration } from "./canvas-fixtures";
import { gapWaitTests } from "./canvas-node-support";

const TARGET: LinearGaps = {
  available: true,
  enabled: true,
  teamKey: "ENG",
  linearProjectId: null,
  grouping: "one",
  parentIdentifier: "ENG-5",
  parentUrl: "https://linear.app/northwind/issue/ENG-5",
  posted: 7,
  answered: 2,
  errorCode: null,
};
const nodeOf = (over: Partial<LinearGaps>, input: Partial<CanvasInput> = {}): NodeView =>
  defined(
    buildCanvasModel(BUILT({ gaps: GAPS, linearGaps: { ...TARGET, ...over }, ...input }), NODE_MAPPERS).zones.gap.nodes.find(
      (n) => n.kind === "linear_gaps",
    ),
  );

test("the node counts the questions posted and answered, under Gaps, with the Linear mark", () => {
  const n = nodeOf({});
  assert.deepEqual(
    [n.zone, n.mark, n.name, n.state, n.badge, n.meta, n.wire],
    ["gap", "linear", "Linear · ENG gaps", "on", "ok", "7 posted · 2 answered", "on"],
  );
  assert.match(n.detail.text, /sub-issues of one issue; answers come back as comments\. Issue ENG-5\./);
  assert.equal(n.detail.reconnect, null);
  assert.equal(nodeOf({ parentIdentifier: null }).detail.text.includes("Issue ENG"), false);
});

gapWaitTests({ kind: "linear_gaps", nodeOf, unset: { linearGaps: { ...TARGET, teamKey: null } } });

test("it stops only for what the API reports: a lost Linear connection offers Reconnect, another closed code says what it is", () => {
  for (const code of ["linear_not_connected", "linear_reconnect_required"]) {
    const n = nodeOf({ errorCode: code });
    assert.deepEqual([n.state, n.badge, n.wire, n.meta, n.detail.reconnect], ["stopped", "stop", "off", "Stopped · reconnect", "linear"]);
  }
  const bound = nodeOf({}, { integrations: [integration("linear", { health: "reconnect_required" })] });
  assert.deepEqual([bound.state, bound.detail.reconnect], ["stopped", "linear"]);
  const forbidden = nodeOf({ errorCode: "linear_forbidden" });
  assert.deepEqual([forbidden.state, forbidden.meta, forbidden.detail.reconnect], ["stopped", "Stopped", null]);
  assert.match(forbidden.detail.text, /not allowed to post/);
  assert.match(nodeOf({ errorCode: "linear_something_new" }).detail.text, /\w/);
});

test("the registry has the Linear sub-issues under Gaps, needing Linear, with the add label Add channel", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "linear_gaps"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["gap", "linear", "linear", "Linear issues"]);
  assert.deepEqual(spec.check, { test: "Check access", testing: "Checking…", add: "Add channel" });
});

test("the form asks for the team key, the optional project and the grouping, and Check access is off until there is a key", () => {
  const view = html(createElement(LinearGapFormBody, formProps("gap", "linear_gaps")));
  assert.match(view, />Linear team key<\/label>/);
  assert.match(view, />Linear project \(optional\)<\/label>/);
  assert.match(view, /<option value="one" selected="">One per question<\/option>/);
  assert.match(view, /<option value="area">One per work area, its questions numbered<\/option>/);
  assert.match(view, /disabled=""[^>]*>Check access<\/button>/);
});

test("a check answer is the names it passed with, or the words under the field the API named", () => {
  assert.deepEqual(outcomeOf({ result: "ok", teamName: "Engineering", linearProjectName: null }), {
    kind: "ok",
    teamName: "Engineering",
    projectName: null,
  });
  const team = outcomeOf({ result: "failed", reason: "linear_team_not_found", field: "team_key" });
  const project = outcomeOf({ result: "failed", reason: "linear_project_not_found", field: "linear_project" });
  const alert = outcomeOf({ result: "failed", reason: "linear_reconnect_required", field: null });
  assert.deepEqual(
    [team.kind === "problem" && team.texts.team, project.kind === "problem" && project.texts.project],
    [
      "Linear has no team with this key, or your account cannot see it.",
      "The team has no project with this name or link, or your account cannot see it.",
    ],
  );
  assert.deepEqual(alert, {
    kind: "problem",
    texts: { alert: "Your Linear connection expired. Connect Linear again under Integrations." },
  });
});

test("a failed save says what the API code means; the phase goes check, then save once a check passed", () => {
  const notFound = problemOf(new DataError("not_found", false, { apiCode: "linear_team_not_found" }));
  assert.deepEqual(notFound, { kind: "problem", texts: { team: "Linear has no team with this key, or your account cannot see it." } });
  const invalidKey = problemOf(
    new DataError("validation", false, { errors: [{ loc: ["body", "team_key"], type: "string_pattern_mismatch" }] }),
  );
  assert.deepEqual(invalidKey, { kind: "problem", texts: { team: "Enter the key of a Linear team, for example ENG." } });
  assert.deepEqual(problemOf(new Error("boom")), {
    kind: "problem",
    texts: { alert: "The Linear gap channel could not be saved. Try again." },
  });
  assert.deepEqual([phaseOf(null, null), phaseOf(null, "check"), phaseOf(null, "save")], ["check", "checking", "saving"]);
  assert.equal(phaseOf({ kind: "ok", teamName: "T", projectName: null }, null), "save");
});
