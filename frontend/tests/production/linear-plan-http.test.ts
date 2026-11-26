// plans/LINEAR/LINEAR-PLAN.md, stream S3: the production LinearPlanPort over the routes of `/projects/{pid}/linear-plan`, against the stub API
// (tests/support/stub-linear-plan.mjs). The real `lib/api.ts` and the adapter run; only `fetch` is replaced. Port 3281 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHttpLinearPlan, toLinearPlan, toLinearPlanAccess, toLinearProjects, toLinearTeams } from "@/lib/data/http/linear-plan";
import { createHttpProjects } from "@/lib/data/http/projects";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const ENG_TEAM = "team-eng";
const ROADMAP = "proj-roadmap";
const ctx = serveStub(3281, (api) => {
  api.linearPlan.seed({ available: true, workspace: "Graphico" });
});
const { sent } = ctx;
const port = createHttpLinearPlan();
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response" && e.retryable);
};

async function setup(bound = true) {
  const p = await createProject.create({ name: "P" });
  if (!bound) ctx.api.linearPlan.seed({ pid: p.id, bound: false });
  sent.length = 0;
  return { pid: p.id };
}

const wire = (over: Record<string, unknown> = {}) => ({
  available: true,
  team_id: ENG_TEAM,
  team_key: "ENG",
  project_id: ROADMAP,
  project_name: "Roadmap",
  project_url: "https://linear.app/acme/project/roadmap-1a2b3c",
  created_by_graphico: false,
  enabled: true,
  workspace: "Graphico",
  connected_by_me: true,
  sync: {
    status: "succeeded",
    queued: false,
    finished_at: "2026-11-25T09:00:05Z",
    error_code: null,
    counts: { created: 4, updated: 1, commented: 0, removed: 0, relations_added: 3, estimate_refused: 0, unchanged: 0, failed: 0 },
  },
  ...over,
});

test("toLinearPlan reads the destination and its pass; no sync or no counts is none; any other shape is invalid_response", () => {
  const read = toLinearPlan(wire());
  assert.deepEqual(
    [read.teamKey, read.linearProjectName, read.enabled, read.sync?.status, read.sync?.counts?.created, read.sync?.counts?.relationsAdded],
    ["ENG", "Roadmap", true, "succeeded", 4, 3],
  );
  assert.equal(toLinearPlan(wire({ sync: null })).sync, null);
  assert.equal(
    toLinearPlan(wire({ sync: { status: "idle", queued: true, finished_at: null, error_code: null, counts: null } })).sync?.counts,
    null,
  );
  const empty = toLinearPlan(
    wire({
      team_id: null,
      team_key: null,
      project_id: null,
      project_name: null,
      project_url: null,
      enabled: false,
      sync: null,
      workspace: null,
    }),
  );
  assert.deepEqual([empty.teamKey, empty.linearProjectId, empty.sync, empty.workspace], [null, null, null, null]);
  invalid(() => toLinearPlan(wire({ available: 1 })));
  invalid(() => toLinearPlan(wire({ connected_by_me: "yes" })));
  invalid(() => toLinearPlan(wire({ created_by_graphico: null })));
  invalid(() => toLinearPlan(wire({ sync: { status: "sleeping", queued: false, finished_at: null, error_code: null, counts: null } })));
  invalid(() => toLinearPlan(wire({ sync: { status: "idle", queued: false, finished_at: "soon", error_code: null, counts: null } })));
  invalid(() =>
    toLinearPlan(wire({ sync: { status: "idle", queued: false, finished_at: null, error_code: null, counts: { created: -1 } } })),
  );
  invalid(() => toLinearPlan(null));
});

test("the teams, the projects and the access answer are shape-checked", () => {
  assert.deepEqual(toLinearTeams({ workspace: "Graphico", teams: [{ id: "t", key: "ENG", name: "Engineering" }] }), {
    workspace: "Graphico",
    teams: [{ id: "t", key: "ENG", name: "Engineering" }],
  });
  assert.deepEqual(toLinearProjects({ projects: [{ id: "p", name: "Roadmap", url: "https://linear.app/a/project/r" }] }), [
    { id: "p", name: "Roadmap", url: "https://linear.app/a/project/r" },
  ]);
  invalid(() => toLinearTeams({ workspace: "Graphico", teams: [{ id: "t" }] }));
  invalid(() => toLinearProjects({ projects: [{ id: "p" }] }));
  assert.deepEqual(toLinearPlanAccess({ result: "ok", team_key: "ENG", project_name: null }), {
    result: "ok",
    teamKey: "ENG",
    projectName: null,
  });
  assert.deepEqual(toLinearPlanAccess({ result: "failed", reason: "linear_project_not_found", field: "project" }), {
    result: "failed",
    reason: "linear_project_not_found",
    field: "project",
  });
  invalid(() => toLinearPlanAccess({ result: "ok" }));
  invalid(() => toLinearPlanAccess({ result: "failed", reason: "", field: null }));
  invalid(() => toLinearPlanAccess({ result: "failed", reason: "x", field: "token" }));
  invalid(() => toLinearPlanAccess({ result: "maybe" }));
});

test("GET: a project with no destination is empty and available", async () => {
  const { pid } = await setup();
  const s = await port.get(pid);
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "GET", path: `/api/backend/projects/${pid}/linear-plan`, body: null },
  );
  assert.deepEqual([s.available, s.teamKey, s.enabled, s.sync], [true, null, false, null]);
});

test("the teams and the projects of a team are read from their routes", async () => {
  const { pid } = await setup();
  const teams = await port.teams(pid);
  assert.equal(last().path, `/api/backend/projects/${pid}/linear-plan/teams`);
  assert.deepEqual([teams.workspace, teams.teams.map((t) => t.key)], ["Graphico", ["ENG", "OPS"]]);
  const projects = await port.projects(pid, ENG_TEAM);
  assert.equal(last().path, `/api/backend/projects/${pid}/linear-plan/projects?team_id=team-eng`);
  assert.deepEqual(
    projects.map((p) => p.name),
    ["Roadmap"],
  );
});

test("PUT names a team and a picked project, or only the team to let Graphico create one; the answer is the destination", async () => {
  const { pid } = await setup();
  const picked = await port.save(pid, { teamId: ENG_TEAM, projectId: ROADMAP, enabled: true });
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    {
      method: "PUT",
      path: `/api/backend/projects/${pid}/linear-plan`,
      body: JSON.stringify({ team_id: ENG_TEAM, project_id: ROADMAP, enabled: true }),
    },
  );
  assert.deepEqual([picked.linearProjectName, picked.enabled, picked.teamKey, picked.createdByGraphico], ["Roadmap", true, "ENG", false]);
  const made = await port.save(pid, { teamId: "team-ops", projectId: null, enabled: true });
  assert.equal(last().body, JSON.stringify({ team_id: "team-ops", enabled: true }));
  assert.deepEqual([made.linearProjectName, made.createdByGraphico, made.teamKey], ["P", true, "OPS"]);
  assert.equal((await port.get(pid)).linearProjectName, "P");
});

test("PUT errors: no Linear binding is a conflict, an unknown team or project is not found, a server without Linear is unavailable", async () => {
  const { pid } = await setup();
  const pick = { teamId: ENG_TEAM, projectId: ROADMAP, enabled: true };
  const open = await setup(false);
  await rejects(port.save(open.pid, pick), "conflict", "linear_not_connected");
  await rejects(port.save(pid, { ...pick, teamId: "nope" }), "not_found", "linear_team_not_found");
  await rejects(port.save(pid, { ...pick, projectId: "proj-board" }), "not_found", "linear_project_not_found");
  ctx.api.linearPlan.seed({ pid, access: { reason: "linear_reconnect_required" } });
  await rejects(port.save(pid, pick), "conflict", "linear_reconnect_required");
  ctx.api.linearPlan.seed({ available: false });
  await rejects(port.save(pid, pick), "unavailable", "linear_unavailable");
});

test("POST access-check: ok with the team, a scripted refusal that names its field, and no binding as an error", async () => {
  const { pid } = await setup();
  assert.deepEqual(await port.checkAccess(pid, { teamId: ENG_TEAM, projectId: null }), {
    result: "ok",
    teamKey: "ENG",
    projectName: null,
  });
  assert.equal(last().body, JSON.stringify({ team_id: ENG_TEAM }));
  assert.deepEqual(await port.checkAccess(pid, { teamId: ENG_TEAM, projectId: ROADMAP }), {
    result: "ok",
    teamKey: "ENG",
    projectName: "Roadmap",
  });
  assert.deepEqual(await port.checkAccess(pid, { teamId: "nope", projectId: null }), {
    result: "failed",
    reason: "linear_team_not_found",
    field: "team",
  });
  ctx.api.linearPlan.seed({ pid, access: { reason: "linear_project_not_found" } });
  assert.deepEqual(await port.checkAccess(pid, { teamId: ENG_TEAM, projectId: "proj-x" }), {
    result: "failed",
    reason: "linear_project_not_found",
    field: "project",
  });
  const open = await setup(false);
  await rejects(port.checkAccess(open.pid, { teamId: ENG_TEAM, projectId: null }), "conflict", "linear_not_connected");
});
