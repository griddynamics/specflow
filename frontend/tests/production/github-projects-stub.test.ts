// docs/github/projects.md: the stub of the GitHub Projects destination (tests/support/stub-github-projects.mjs) answers the four routes of
// `/projects/{pid}/github-projects` as the API does, so the canvas form can be built and tested against it. Only `fetch` is replaced.
// Port 3294 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHttpProjects } from "@/lib/data/http/projects";
import { serveStub } from "../support/stub-fetch";

const ctx = serveStub(3294, (api) => {
  api.githubProjects.seed({ available: true, organization: "acme" });
});
const { createProject } = createHttpProjects();

async function setup(bound = true) {
  const p = await createProject.create({ name: "P" });
  if (bound) ctx.api.integrations.seed({ pid: p.id, github: { health: "active" } });
  ctx.api.github.seed({ pid: p.id, repositories: [{ full_name: "acme/shop" }] });
  const listed = await call("GET", `projects/${p.id}/repositories`);
  const [repository] = listed.body.repositories as { id: string }[];
  ctx.sent.length = 0;
  return { pid: p.id, rid: repository.id };
}

const call = async (method: string, path: string, body?: unknown) => {
  const response = await fetch(`/api/backend/${path}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
};

test("a project with no destination reads as available and empty", async () => {
  const { pid } = await setup();

  const { status, body } = await call("GET", `projects/${pid}/github-projects`);

  assert.equal(status, 200);
  assert.deepEqual([body.available, body.enabled, body.repository, body.project_title, body.sync], [true, false, null, null, null]);
});

test("the projects of the repository's organization are listed; a user's repository has none", async () => {
  const { pid, rid } = await setup();

  const listed = await call("GET", `projects/${pid}/github-projects/projects?repository_id=${rid}`);
  ctx.api.githubProjects.seed({ organization: null });
  const user = await call("GET", `projects/${pid}/github-projects/projects?repository_id=${rid}`);

  assert.deepEqual([listed.body.organization, (listed.body.projects as unknown[]).length], ["acme", 2]);
  assert.deepEqual([user.body.organization, user.body.projects], [null, []]);
});

test("PUT saves a picked project or creates one by title, and the target reads back with its sync status", async () => {
  const { pid, rid } = await setup();

  const picked = await call("PUT", `projects/${pid}/github-projects`, { repository_id: rid, project_node_id: "PVT_stub1", enabled: true });
  const made = await call("PUT", `projects/${pid}/github-projects`, {
    repository_id: rid,
    new_project_title: " Shop plan ",
    enabled: true,
  });

  assert.deepEqual([picked.status, picked.body.project_title, picked.body.repository], [200, "Plan", "acme/shop"]);
  assert.deepEqual([made.body.project_title, String(made.body.project_node_id).startsWith("PVT_new")], ["Shop plan", true]);
  const sync = made.body.sync as Record<string, unknown>;
  assert.deepEqual([sync.status, sync.repository, sync.error_code], ["succeeded", "acme/shop", null]);
  assert.deepEqual((await call("GET", `projects/${pid}/github-projects`)).body, made.body);
});

test("PUT refuses a body that names both or neither project, an unknown field, an unknown repository and a missing binding", async () => {
  const { pid, rid } = await setup();
  const url = `projects/${pid}/github-projects`;
  const both = { repository_id: rid, project_node_id: "PVT_stub1", new_project_title: "X", enabled: true };

  assert.equal((await call("PUT", url, both)).status, 422);
  assert.equal((await call("PUT", url, { repository_id: rid, enabled: true })).status, 422);
  assert.equal((await call("PUT", url, { repository_id: rid, project_node_id: "PVT_stub1", enabled: true, x: 1 })).status, 422);
  const stranger = await call("PUT", url, { repository_id: "nope", project_node_id: "PVT_stub1", enabled: true });
  assert.deepEqual([stranger.status, (stranger.body.error as { code: string }).code], [404, "repository_not_found"]);
  const open = await setup(false);
  const unbound = await call("PUT", `projects/${open.pid}/github-projects`, {
    repository_id: open.rid,
    project_node_id: "PVT_stub1",
    enabled: true,
  });
  assert.deepEqual([unbound.status, (unbound.body.error as { code: string }).code], [409, "github_not_connected"]);
});

test("the access check answers ok, a scripted refusal with its field, and a missing binding as an answer", async () => {
  const { pid, rid } = await setup();
  const url = `projects/${pid}/github-projects/access-check`;

  const ok = await call("POST", url, { repository_id: rid, project_node_id: "PVT_stub2" });
  ctx.api.githubProjects.seed({ pid, access: { reason: "github_project_not_found" } });
  const refused = await call("POST", url, { repository_id: rid, project_node_id: "PVT_stub2" });
  ctx.api.githubProjects.seed({ pid, access: { reason: "github_permissions_missing" } });
  const permissions = await call("POST", url, { repository_id: rid, new_project_title: "T" });

  assert.deepEqual(ok.body, { result: "ok", organization: "acme", project_title: "Roadmap" });
  assert.deepEqual(refused.body, { result: "failed", reason: "github_project_not_found", field: "project" });
  assert.deepEqual(permissions.body, { result: "failed", reason: "github_permissions_missing", field: null });
});

test("a server without GitHub says so: an unavailable read and a 503", async () => {
  const { pid, rid } = await setup();
  ctx.api.githubProjects.seed({ available: false });

  const read = await call("GET", `projects/${pid}/github-projects`);
  const put = await call("PUT", `projects/${pid}/github-projects`, { repository_id: rid, project_node_id: "PVT_stub1", enabled: true });

  assert.equal((read.body as { available: boolean }).available, false);
  assert.deepEqual([put.status, (put.body.error as { code: string }).code], [503, "github_unavailable"]);
});

test("a seeded target shows its sync status, including a closed error code", async () => {
  const { pid, rid } = await setup();
  ctx.api.githubProjects.seed({
    pid,
    target: {
      repository_id: rid,
      repository: "acme/shop",
      project_node_id: "PVT_stub1",
      project_title: "Plan",
      sync: { status: "failed", error_code: "github_permissions_missing" },
    },
  });

  const { body } = await call("GET", `projects/${pid}/github-projects`);

  assert.deepEqual(
    [body.enabled, (body.sync as Record<string, unknown>).status, (body.sync as Record<string, unknown>).error_code],
    [true, "failed", "github_permissions_missing"],
  );
});
