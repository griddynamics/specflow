// docs/github/projects.md: the production GitHubProjectsPort over the four routes of `/projects/{pid}/github-projects`, against the stub API
// (tests/support/stub-github-projects.mjs). The real `lib/api.ts` and the adapter run; only `fetch` is replaced. Port 3292 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHttpGitHubProjects, toGitHubProjectList, toGitHubProjects, toGitHubProjectsAccess } from "@/lib/data/http/github-projects";
import { createHttpGitHub } from "@/lib/data/http/github";
import { createHttpProjects } from "@/lib/data/http/projects";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const ctx = serveStub(3292, (api) => {
  api.githubProjects.seed({ available: true, organization: "acme" });
});
const { sent } = ctx;
const port = createHttpGitHubProjects();
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response" && e.retryable);
};

async function setup(bound = true) {
  const p = await createProject.create({ name: "P" });
  if (bound) ctx.api.integrations.seed({ pid: p.id, github: { health: "active" } });
  ctx.api.github.seed({ pid: p.id, repositories: [{ full_name: "acme/shop" }] });
  const [repo] = (await createHttpGitHub().list(p.id)).repositories;
  sent.length = 0;
  return { pid: p.id, rid: repo.id };
}

const wire = (over: Record<string, unknown> = {}) => ({
  available: true,
  repository_id: "r1",
  repository: "acme/shop",
  project_node_id: "PVT_stub1",
  project_title: "Plan",
  project_url: "https://github.com/orgs/acme/projects/1",
  enabled: true,
  connected_by_name: "dev",
  connected_by_me: true,
  sync: {
    status: "succeeded",
    queued: false,
    finished_at: "2026-11-25T09:00:05Z",
    error_code: null,
    counts: { created: 4, updated: 1, commented: 0, removed: 0, ranked: 5, unchanged: 0, failed: 0 },
  },
  ...over,
});

test("toGitHubProjects reads the destination and its pass; no sync or no counts is none; any other shape is invalid_response", () => {
  const read = toGitHubProjects(wire());
  assert.deepEqual(
    [read.repository, read.projectTitle, read.enabled, read.sync?.status, read.sync?.counts?.created],
    ["acme/shop", "Plan", true, "succeeded", 4],
  );
  assert.equal(toGitHubProjects(wire({ sync: null })).sync, null);
  assert.equal(
    toGitHubProjects(wire({ sync: { status: "idle", queued: true, finished_at: null, error_code: null, counts: null } })).sync?.counts,
    null,
  );
  const empty = toGitHubProjects(
    wire({
      repository_id: null,
      repository: null,
      project_node_id: null,
      project_title: null,
      project_url: null,
      enabled: false,
      sync: null,
    }),
  );
  assert.deepEqual([empty.repository, empty.projectNodeId, empty.sync], [null, null, null]);
  invalid(() => toGitHubProjects(wire({ available: 1 })));
  invalid(() => toGitHubProjects(wire({ connected_by_me: "yes" })));
  invalid(() => toGitHubProjects(wire({ sync: { status: "sleeping", queued: false, finished_at: null, error_code: null, counts: null } })));
  invalid(() => toGitHubProjects(wire({ sync: { status: "idle", queued: false, finished_at: "soon", error_code: null, counts: null } })));
  invalid(() =>
    toGitHubProjects(wire({ sync: { status: "idle", queued: false, finished_at: null, error_code: null, counts: { created: -1 } } })),
  );
  invalid(() => toGitHubProjects(null));
});

test("toGitHubProjectList and toGitHubProjectsAccess: the projects of an organization, an ok answer, a failed one with its field; other shapes are invalid_response", () => {
  const list = toGitHubProjectList({
    organization: "acme",
    projects: [{ node_id: "PVT_1", number: 1, title: "Plan", url: "https://github.com/orgs/acme/projects/1" }],
  });
  assert.deepEqual([list.organization, list.projects[0].nodeId, list.projects[0].number], ["acme", "PVT_1", 1]);
  assert.deepEqual(toGitHubProjectList({ organization: null, projects: [] }), { organization: null, projects: [] });
  invalid(() => toGitHubProjectList({ organization: "acme", projects: [{ node_id: "x" }] }));
  invalid(() => toGitHubProjectList({ organization: 4, projects: [] }));
  assert.deepEqual(toGitHubProjectsAccess({ result: "ok", organization: "acme", project_title: null }), {
    result: "ok",
    organization: "acme",
    projectTitle: null,
  });
  assert.deepEqual(toGitHubProjectsAccess({ result: "failed", reason: "github_project_not_found", field: "project" }), {
    result: "failed",
    reason: "github_project_not_found",
    field: "project",
  });
  assert.equal(toGitHubProjectsAccess({ result: "failed", reason: "github_permissions_missing", field: null }).result, "failed");
  invalid(() => toGitHubProjectsAccess({ result: "ok" }));
  invalid(() => toGitHubProjectsAccess({ result: "failed", reason: "", field: null }));
  invalid(() => toGitHubProjectsAccess({ result: "failed", reason: "x", field: "token" }));
  invalid(() => toGitHubProjectsAccess({ result: "maybe" }));
});

test("GET: a project with no destination is empty and available", async () => {
  const { pid } = await setup();
  const s = await port.get(pid);
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "GET", path: `/api/backend/projects/${pid}/github-projects`, body: null },
  );
  assert.deepEqual([s.available, s.repository, s.enabled, s.sync], [true, null, false, null]);
});

test("the project list asks with the repository id and answers the organization's open projects; a repository of a person has none", async () => {
  const { pid, rid } = await setup();
  const list = await port.projects(pid, rid);
  assert.equal(last().path, `/api/backend/projects/${pid}/github-projects/projects?repository_id=${rid}`);
  assert.deepEqual([list.organization, list.projects.map((p) => p.title)], ["acme", ["Plan", "Roadmap"]]);
  ctx.api.githubProjects.seed({ organization: null });
  assert.deepEqual(await port.projects(pid, rid), { organization: null, projects: [] });
});

test("PUT names exactly one of a picked project and a new title; the answer is the destination", async () => {
  const { pid, rid } = await setup();
  const picked = await port.save(pid, { repositoryId: rid, project: { nodeId: "PVT_stub2" }, enabled: true });
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    {
      method: "PUT",
      path: `/api/backend/projects/${pid}/github-projects`,
      body: JSON.stringify({ repository_id: rid, project_node_id: "PVT_stub2", enabled: true }),
    },
  );
  assert.deepEqual([picked.projectTitle, picked.enabled, picked.repository], ["Roadmap", true, "acme/shop"]);
  const made = await port.save(pid, { repositoryId: rid, project: { newTitle: "Shop" }, enabled: true });
  assert.equal(last().body, JSON.stringify({ repository_id: rid, new_project_title: "Shop", enabled: true }));
  assert.deepEqual([made.projectTitle, made.projectNodeId?.startsWith("PVT_new")], ["Shop", true]);
  assert.equal((await port.get(pid)).projectTitle, "Shop");
});

test("PUT errors: no GitHub binding and a missing permission are conflicts, an unknown repository is not found, a server without GitHub is unavailable", async () => {
  const { pid, rid } = await setup();
  const pick = { repositoryId: rid, project: { nodeId: "PVT_stub1" }, enabled: true };
  const open = await setup(false);
  await rejects(port.save(open.pid, { ...pick, repositoryId: open.rid }), "conflict", "github_not_connected");
  await rejects(port.save(pid, { ...pick, repositoryId: "00000000-0000-4000-8000-000000000000" }), "not_found", "repository_not_found");
  ctx.api.githubProjects.seed({ pid, access: { reason: "github_permissions_missing" } });
  await rejects(port.save(pid, pick), "conflict", "github_permissions_missing");
  ctx.api.githubProjects.seed({ available: false });
  await rejects(port.save(pid, pick), "unavailable", "github_unavailable");
});

test("POST access-check: ok with the organization, a scripted refusal that names its field, and no binding as an error", async () => {
  const { pid, rid } = await setup();
  assert.deepEqual(await port.checkAccess(pid, { repositoryId: rid, project: { newTitle: "Shop" } }), {
    result: "ok",
    organization: "acme",
    projectTitle: null,
  });
  assert.equal(last().body, JSON.stringify({ repository_id: rid, new_project_title: "Shop" }));
  assert.equal((await port.checkAccess(pid, { repositoryId: rid, project: { nodeId: "PVT_stub1" } })).result, "ok");
  ctx.api.githubProjects.seed({ pid, access: { reason: "github_project_not_found" } });
  assert.deepEqual(await port.checkAccess(pid, { repositoryId: rid, project: { nodeId: "PVT_x" } }), {
    result: "failed",
    reason: "github_project_not_found",
    field: "project",
  });
  const open = await setup(false);
  await rejects(
    port.checkAccess(open.pid, { repositoryId: open.rid, project: { nodeId: "PVT_stub1" } }),
    "conflict",
    "github_not_connected",
  );
});
