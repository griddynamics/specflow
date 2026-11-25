// The GitHub Projects plan destination on the canvas (11bF): what the node says in each state the API reports, the words of a refusal (and
// what to approve when the permissions are missing), the line of a passed check, the registry entry and the form. Server-rendered: no browser.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { CanvasInput, NodeView } from "@/components/v2/canvas/canvas-types";
import { accessOutcome, githubPassText, githubPlanCheckText, githubPlanFailureText } from "@/components/v2/canvas/github-plan-logic";
import { GitHubPlanFormBody } from "@/components/v2/canvas/GitHubPlanForm";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { COPY } from "@/lib/copy";
import { DataError } from "@/lib/domain/errors";
import type { GitHubProjects } from "@/lib/domain/github-projects";
import { defined } from "../support/defined";
import { formProps, html } from "./canvas-form-support";
import { BUILT, GAPS, integration, READY_PLAN, repo } from "./canvas-fixtures";

const COUNTS = { created: 0, updated: 0, commented: 0, removed: 0, ranked: 0, unchanged: 0, failed: 0 };
const TARGET: GitHubProjects = {
  available: true,
  repositoryId: "r1",
  repository: "northwind/ordering",
  projectNodeId: "PVT_1",
  projectTitle: "Plan",
  projectUrl: "https://github.com/orgs/northwind/projects/1",
  enabled: true,
  connectedByName: "Alex Kim",
  connectedByMe: false,
  sync: { status: "succeeded", queued: false, finishedAt: "2026-11-25T09:00:00Z", errorCode: null, counts: { ...COUNTS, created: 6 } },
};
const nodeOf = (over: Partial<GitHubProjects>, input: Partial<CanvasInput> = {}): NodeView =>
  defined(
    buildCanvasModel(
      BUILT({ gaps: GAPS, plan: READY_PLAN, githubProjects: { ...TARGET, ...over }, ...input }),
      NODE_MAPPERS,
    ).zones.plan.nodes.find((n) => n.kind === "github_plan"),
  );
const sync = (over: Partial<NonNullable<GitHubProjects["sync"]>>) => ({ sync: { ...defined(TARGET.sync), ...over } });

test("the node counts the issues made, under Plan, with the GitHub mark", () => {
  const n = nodeOf({});
  assert.deepEqual(
    [n.zone, n.mark, n.name, n.state, n.badge, n.meta, n.wire],
    ["plan", "github", "GitHub · Plan", "on", "ok", "6 issues · GitHub", "on"],
  );
  assert.equal(nodeOf(sync({ counts: { ...COUNTS, created: 1 } })).meta, "1 issue · GitHub");
  assert.equal(nodeOf(sync({ counts: COUNTS })).meta, "In sync");
  const never = nodeOf(sync({ status: "idle", counts: null }));
  assert.deepEqual([never.meta, never.badge, never.state], ["Not synced yet", null, "on"]);
  assert.match(nodeOf(sync({ status: "partial" })).detail.text, /could not do everything/);
});

test("the wire moves only while a pass runs", () => {
  const run = nodeOf(sync({ status: "running", counts: null }));
  assert.deepEqual([run.state, run.badge, run.wire, run.meta], ["busy", "spin", "flow", "Syncing…"]);
  assert.equal(nodeOf({}).wire, "on");
});

test("it waits for the plan, and while it is off", () => {
  const waiting = nodeOf({}, { plan: null });
  assert.deepEqual([waiting.state, waiting.wire, waiting.meta], ["wait", "off", "Waits for the plan"]);
  assert.deepEqual([nodeOf({ enabled: false }).state, nodeOf({ enabled: false }).meta], ["wait", "Sync is off"]);
  const none = buildCanvasModel(BUILT({ gaps: GAPS, plan: READY_PLAN, githubProjects: { ...TARGET, repository: null } }), NODE_MAPPERS);
  assert.equal(
    none.zones.plan.nodes.some((n) => n.kind === "github_plan"),
    false,
  );
  assert.equal(
    buildCanvasModel(BUILT({ gaps: GAPS, plan: READY_PLAN }), NODE_MAPPERS).zones.plan.nodes.some((n) => n.kind === "github_plan"),
    false,
  );
});

test("it stops only for what the API reports: a lost GitHub connection offers Reconnect, the codes of a failed pass say what they are", () => {
  for (const code of ["github_not_connected", "github_reconnect_required"]) {
    const n = nodeOf(sync({ status: "failed", errorCode: code }));
    assert.deepEqual([n.state, n.badge, n.wire, n.meta, n.detail.reconnect], ["stopped", "stop", "off", "Stopped · reconnect", "github"]);
  }
  const bound = nodeOf({}, { integrations: [integration("github", { health: "reconnect_required" })] });
  assert.deepEqual([bound.state, bound.detail.reconnect], ["stopped", "github"]);
  const permissions = nodeOf(sync({ status: "failed", errorCode: "github_permissions_missing" }));
  assert.deepEqual([permissions.state, permissions.meta, permissions.detail.reconnect], ["stopped", "Stopped", null]);
  assert.match(permissions.detail.text, /Approve the new permissions .* Issues .* Organization projects/);
  assert.equal(nodeOf(sync({ status: "failed", errorCode: "github_issue_gone" })).detail.text, githubPassText("github_issue_gone"));
  // A pass that is only waiting for the plan is not a stop.
  assert.equal(nodeOf(sync({ status: "skipped", errorCode: "no_plan", counts: null })).state, "on");
});

test("each closed code has its own words; github_permissions_missing says what to approve; an unknown one is the general failure", () => {
  const failure = (apiCode: string) => githubPlanFailureText(new DataError("conflict", false, { apiCode }));
  assert.equal(failure("github_not_connected"), COPY.githubNotConnected);
  assert.equal(failure("github_reconnect_required"), COPY.githubReconnectRequired);
  assert.match(failure("github_permissions_missing"), /Issues \(read and write\) and Organization projects \(read and write\)/);
  assert.match(failure("github_not_an_organization"), /belongs to a person/);
  assert.match(failure("github_repo_read_only"), /cannot push/);
  assert.match(failure("github_project_not_found"), /no open project with this title/);
  assert.match(failure("repository_not_found"), /not in the project any more/);
  assert.equal(failure("github_unavailable"), COPY.githubUnavailable);
  assert.equal(failure("something_new"), "Something went wrong. Try again.");
  assert.throws(
    () => accessOutcome({ result: "failed", reason: "github_permissions_missing", field: null }),
    (e) => e instanceof DataError && e.detail?.apiCode === "github_permissions_missing",
  );
  assert.deepEqual(accessOutcome({ result: "ok", organization: "acme", projectTitle: null }), { organization: "acme", projectTitle: null });
});

test("a passed check names the organization and the project used or the one to be made", () => {
  const found = { organization: "acme", projectTitle: "Plan" };
  assert.equal(githubPlanCheckText(found, { repositoryId: "r", project: { nodeId: "PVT_1" } }), "Organization acme. Project “Plan”.");
  assert.equal(
    githubPlanCheckText({ organization: "acme", projectTitle: null }, { repositoryId: "r", project: { newTitle: "Shop" } }),
    "Organization acme. A project named “Shop” will be created.",
  );
});

test("the registry has GitHub Projects under Plan, needing GitHub, with the add label Add destination", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "github_plan"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["plan", "github", "github", "GitHub Projects"]);
  assert.deepEqual(spec.check, { test: "Check access", testing: "Checking…", add: "Add destination" });
});

test("the form offers the project's repositories and, by default, a project named after this one; Check access is on", () => {
  const view = html(
    createElement(GitHubPlanFormBody, {
      ...formProps("plan", "github_plan"),
      repositories: [repo({ id: "r1", fullName: "northwind/ordering" }), repo({ id: "r2", fullName: "northwind/deploy" })],
      name: "Northwind shop",
    }),
  );
  assert.match(view, />Repository<\/label>/);
  assert.match(view, /<option value="r1" selected="">northwind\/ordering<\/option>/);
  assert.match(view, /<option value="r2">northwind\/deploy<\/option>/);
  assert.match(view, />Project<\/label>/);
  assert.match(view, /<option value="new" selected="">Create “Northwind shop”<\/option>/);
  assert.match(view, /created in the organization of the repository/);
  assert.match(view, /Reading the projects…/);
  assert.doesNotMatch(view, /disabled=""[^>]*>Check access<\/button>/);
});
