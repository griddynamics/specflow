// docs/github/api.md: the production GitHubPort against the stub API (tests/support/stub-github.mjs). The real
// `lib/api.ts` and the adapter run; only `fetch` is replaced (tests/support/stub-fetch.ts). Layer U: the wire parsers and the error map.
// Layer P: each route's request, response and error rows. Port 3298 (stub API).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createHttpGitHub,
  toGitHubConnection,
  toGitHubRepository,
  toProjectRepositories,
  toProjectRepository,
} from "@/lib/data/http/github";
import { createHttpProjects } from "@/lib/data/http/projects";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const PORT = 3298;
const ctx = serveStub(PORT, (api) => {
  api.github.reset();
  api.github.state.cloneMs = null; // a check stays pending unless a test says otherwise
});
const { sent } = ctx;
const github = createHttpGitHub();
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response");
};
const ACTIVE = { status: "active", login: "octo" } as const;
const MANAGE = "https://github.com/settings/installations";

async function ownerProject() {
  const p = await createProject.create({ name: "P" });
  sent.length = 0;
  return p.id;
}

// ---- Layer U ----

const wireConn = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  connected: true,
  status: "active",
  login: "octo",
  connected_at: "2026-11-20T09:30:00Z",
  last_used_at: null,
  manage_url: "https://github.com/settings/installations",
  ...over,
});
const wireRepo = (over: Record<string, unknown> = {}) => ({
  id: "r1",
  github_repo_id: 7,
  full_name: "acme/api",
  html_url: "https://github.com/acme/api",
  private: true,
  added_by_name: "Ann",
  added_by_me: false,
  clone_state: "cloned",
  failure_code: null,
  default_branch: "main",
  head_sha: "abcdef1234",
  file_count: 10,
  size_bytes: 2048,
  snapshot_bytes: 1024,
  checked_at: "2026-11-21T10:00:00Z",
  cloned_at: "2026-11-21T10:00:00Z",
  created_at: "2026-11-20T09:30:00Z",
  ...over,
});

test("toGitHubConnection: connected, off and expired read; a status that disagrees with `connected` or a non-GitHub manage URL is invalid", () => {
  assert.deepEqual(toGitHubConnection(wireConn()), {
    enabled: true,
    connected: true,
    status: "active",
    login: "octo",
    connectedAt: "2026-11-20T09:30:00Z",
    lastUsedAt: null,
    manageUrl: "https://github.com/settings/installations",
  });
  const off = { connected: false, status: null, login: null, connected_at: null };
  assert.equal(toGitHubConnection(wireConn({ enabled: false, manage_url: null, ...off })).enabled, false);
  assert.equal(toGitHubConnection(wireConn({ status: "expired" })).status, "expired");
  invalid(() => toGitHubConnection(wireConn({ status: null })));
  invalid(() => toGitHubConnection(wireConn({ status: "revoked" })));
  invalid(() => toGitHubConnection(wireConn({ manage_url: "https://evil.example/settings" })));
  const plain = new URL(MANAGE);
  plain.protocol = "http:"; // the same page over plain HTTP
  invalid(() => toGitHubConnection(wireConn({ manage_url: plain.href })));
  invalid(() => toGitHubConnection(wireConn({ connected_at: "yesterday" })));
});

test("toGitHubRepository and toProjectRepository read the contract; a bad id, URL, state or failure pairing is invalid", () => {
  assert.deepEqual(
    toGitHubRepository({
      github_repo_id: 1,
      full_name: "a/b",
      html_url: "https://github.com/a/b",
      private: false,
      default_branch: null,
      size_kb: 3,
      too_large: false,
    }),
    {
      githubRepoId: 1,
      fullName: "a/b",
      htmlUrl: "https://github.com/a/b",
      private: false,
      defaultBranch: null,
      sizeKb: 3,
      tooLarge: false,
    },
  );
  const r = toProjectRepository(wireRepo());
  assert.equal(r.cloneState, "cloned");
  assert.equal(r.headSha, "abcdef1234");
  assert.equal(
    toProjectRepository(wireRepo({ clone_state: "failed", failure_code: "github_clone_failed" })).failureCode,
    "github_clone_failed",
  );
  invalid(() => toProjectRepository(wireRepo({ clone_state: "failed" }))); // failed without a code
  invalid(() => toProjectRepository(wireRepo({ failure_code: "x" }))); // a code without failed
  invalid(() => toProjectRepository(wireRepo({ clone_state: "done" })));
  invalid(() => toProjectRepository(wireRepo({ github_repo_id: 0 })));
  invalid(() => toProjectRepository(wireRepo({ html_url: "javascript:alert(1)" })));
  invalid(() => toProjectRepository(wireRepo({ file_count: -1 })));
  invalid(() => toProjectRepository(wireRepo({ created_at: null })));
  invalid(() => toProjectRepositories({ enabled: true, can_manage: true }));
  assert.equal(toProjectRepositories({ enabled: true, can_manage: false, repositories: [wireRepo()] }).repositories.length, 1);
});

// ---- Layer P ----

test("connection, authorize and install: GET and POSTs with return_to; the URLs are GitHub's", async () => {
  const off = await github.connection();
  assert.equal(off.connected, false);
  assert.equal(off.manageUrl, "https://github.com/settings/installations");
  const back = "/projects/p1/settings?tab=github";
  const url = new URL(await github.authorize(back));
  assert.equal(url.origin + url.pathname, "https://github.com/login/oauth/authorize");
  assert.deepEqual(last(), {
    method: "POST",
    path: "/api/backend/integrations/github/authorize",
    body: JSON.stringify({ return_to: back }),
  });
  const install = new URL(await github.install(back));
  assert.match(install.pathname, /^\/apps\/[^/]+\/installations\/new$/);
  assert.deepEqual(last(), { method: "POST", path: "/api/backend/integrations/github/install", body: JSON.stringify({ return_to: back }) });
  await rejects(github.authorize("//evil"), "validation", "validation_failed");
});

test("GitHub off: the connection says so, the list is empty and not enabled, the writes are github_unavailable", async () => {
  const pid = await ownerProject();
  ctx.api.github.state.enabled = false;
  assert.equal((await github.connection()).enabled, false);
  assert.deepEqual(await github.list(pid), { enabled: false, canManage: true, repositories: [] });
  await rejects(github.authorize("/x"), "unavailable", "github_unavailable");
  await rejects(github.add(pid, 101), "unavailable", "github_unavailable");
});

test("available: not connected and expired are conflicts; connected lists the repositories with too_large", async () => {
  await rejects(github.available(), "conflict", "github_not_connected");
  ctx.api.github.state.connection = { status: "expired", login: "octo" };
  await rejects(github.available(), "conflict", "github_reconnect_required");
  ctx.api.github.state.connection = ACTIVE;
  const list = await github.available();
  assert.equal(list.truncated, false);
  assert.deepEqual(
    list.repositories.map((r) => [r.fullName, r.tooLarge]),
    [
      ["acme/payments-api", false],
      ["acme/web-app", false],
      ["acme/monorepo", true],
    ],
  );
  assert.equal(last().path, "/api/backend/integrations/github/repositories");
});

test("add and remove a project repository; the owner rule and the refusals carry their API codes", async () => {
  const pid = await ownerProject();
  ctx.api.github.state.connection = ACTIVE;
  const added = await github.add(pid, 101);
  assert.equal(added.cloneState, "pending");
  assert.equal(added.addedByMe, true);
  assert.deepEqual(last(), {
    method: "POST",
    path: `/api/backend/projects/${pid}/repositories`,
    body: JSON.stringify({ github_repo_id: 101 }),
  });
  assert.equal((await github.list(pid)).repositories.length, 1);
  await rejects(github.add(pid, 101), "conflict", "repository_exists");
  await rejects(github.add(pid, 103), "validation", "github_repo_too_large");
  await rejects(github.add(pid, 999), "not_found", "github_repo_not_accessible");
  await github.remove(pid, added.id);
  assert.deepEqual(last(), { method: "DELETE", path: `/api/backend/projects/${pid}/repositories/${added.id}`, body: null });
  assert.equal((await github.list(pid)).repositories.length, 0);
  await rejects(github.remove(pid, added.id), "not_found", "repository_not_found");

  const other = ctx.api.seedProject("Theirs", { role: "collaborator" });
  sent.length = 0;
  assert.equal((await github.list(other.id)).canManage, false);
  await rejects(github.add(other.id, 101), "forbidden", "owner_required");
});

test("the clone check a repository add starts moves it from pending to cloned with its snapshot facts", async () => {
  const pid = await ownerProject();
  ctx.api.github.state.connection = ACTIVE;
  ctx.api.github.state.cloneMs = 20;
  await github.add(pid, 102);
  await new Promise((r) => setTimeout(r, 60));
  const done = (await github.list(pid)).repositories[0];
  assert.equal(done.cloneState, "cloned");
  assert.equal(done.fileCount, 1204);
  assert.ok(done.headSha !== null && done.snapshotBytes !== null && done.clonedAt !== null);
});
