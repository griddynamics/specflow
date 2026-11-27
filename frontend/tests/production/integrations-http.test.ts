// docs/mission-control/integrations-api.md: the production IntegrationsPort and the Slack test message against the stub API
// (tests/support/stub-integrations.mjs). The real `lib/api.ts` and the adapters run; only `fetch` is replaced (tests/support/stub-fetch.ts).
// Layer U: the wire parsers. Layer P: each route's request, answer and error rows. Port 3290 (stub API).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHttpIntegrations, toIntegration, toIntegrations } from "@/lib/data/http/integrations";
import { createHttpProjects } from "@/lib/data/http/projects";
import { createHttpSlack, toSlackTestResult } from "@/lib/data/http/slack";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const PORT = 3290;
const ctx = serveStub(PORT, (api) => {
  api.github.reset();
  api.linear.reset();
  api.box.seed({ reset: true });
  api.dropbox.seed({ reset: true });
  api.jira.state.mode = "oauth";
  api.jira.state.connection = null;
  api.integrations.seed({ reset: true });
});
const { sent } = ctx;
const integrations = createHttpIntegrations();
const slack = createHttpSlack();
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response");
};
const GITHUB = { status: "active", login: "octo" } as const;

async function ownerProject() {
  const p = await createProject.create({ name: "P" });
  sent.length = 0;
  return p.id;
}

// ---- Layer U ----

const wire = (over: Record<string, unknown> = {}) => ({
  app: "github",
  available: true,
  connected_by: { user_id: "u1", name: "Alex Kim" },
  workspace: { id: "octo", name: "octo" },
  connected_at: "2026-11-20T09:30:00Z",
  health: "active",
  ...over,
});

test("an integration keeps the member, the workspace and the health word of the API", () => {
  assert.deepEqual(toIntegration(wire()), {
    app: "github",
    available: true,
    connectedBy: { userId: "u1", name: "Alex Kim" },
    workspace: { id: "octo", name: "octo" },
    connectedAt: "2026-11-20T09:30:00Z",
    health: "active",
  });
  const none = toIntegration(wire({ app: "slack", connected_by: null, workspace: null, connected_at: null, health: "none" }));
  assert.equal(none.connectedBy, null);
  assert.equal(none.workspace, null);
  assert.equal(none.connectedAt, null);
});

test("any other shape is invalid_response", () => {
  invalid(() => toIntegration(null));
  invalid(() => toIntegration(wire({ app: "notion" })));
  invalid(() => toIntegration(wire({ health: "fine" })));
  invalid(() => toIntegration(wire({ available: "yes" })));
  invalid(() => toIntegration(wire({ connected_by: { user_id: "u1" } })));
  invalid(() => toIntegration(wire({ connected_at: "yesterday" })));
  invalid(() => toIntegrations({ integrations: [wire({ app: "atlassian" })] }));
  invalid(() => toIntegrations({ integrations: [wire({ app: "github" }), wire({ app: "atlassian" }), wire({ app: "slack" })] }));
  invalid(() => toIntegrations({}));
});

test("the Slack test answer needs posted true and a channel", () => {
  assert.deepEqual(toSlackTestResult({ posted: true, channel: { id: "C1", name: "gaps", private: false } }), {
    posted: true,
    channel: { id: "C1", name: "gaps", private: false },
  });
  invalid(() => toSlackTestResult({ posted: false, channel: { id: "C1", name: "gaps", private: false } }));
  invalid(() => toSlackTestResult({ posted: true }));
});

// ---- Layer P ----

test("GET lists the six apps in order; nothing is connected on a fresh project", async () => {
  const pid = await ownerProject();
  const rows = await integrations.list(pid);
  assert.deepEqual(
    rows.map((r) => [r.app, r.connectedBy, r.health]),
    [
      ["atlassian", null, "none"],
      ["github", null, "none"],
      ["slack", null, "none"],
      ["linear", null, "none"],
      ["box", null, "none"],
      ["dropbox", null, "none"],
    ],
  );
  assert.deepEqual(last(), { method: "GET", path: `/api/backend/projects/${pid}/integrations`, body: null });
});

test("GET of a project that is not the caller's is not_found", async () => {
  await rejects(integrations.list("00000000-0000-4000-8000-000000000000"), "not_found", "project_not_found");
});

test("PUT binds the caller's own connection: Atlassian by the cloud site, GitHub by the login", async () => {
  const pid = await ownerProject();
  ctx.api.jira.state.connection = { status: "active", account_name: "Dev" };
  ctx.api.github.state.connection = GITHUB;
  const atlassian = await integrations.connect(pid, "atlassian");
  assert.deepEqual(last(), { method: "PUT", path: `/api/backend/projects/${pid}/integrations/atlassian`, body: null });
  assert.equal(atlassian.connectedBy?.name, "dev");
  assert.equal(atlassian.health, "active");
  assert.equal(atlassian.workspace?.name, "example.atlassian.net");
  const github = await integrations.connect(pid, "github");
  assert.deepEqual(github.workspace, { id: "octo", name: "octo" });
  assert.deepEqual(
    (await integrations.list(pid)).map((r) => r.health),
    ["active", "active", "none", "none", "none", "none"],
  );
});

test("PUT binds the caller's own Box connection by the enterprise (a personal account by its name)", async () => {
  const pid = await ownerProject();
  await rejects(integrations.connect(pid, "box"), "conflict", "box_not_connected");
  ctx.api.box.state.connection = { status: "active", name: "Dana Example", enterprise_name: "Acme Corp" };
  const bound = await integrations.connect(pid, "box");
  assert.deepEqual(last(), { method: "PUT", path: `/api/backend/projects/${pid}/integrations/box`, body: null });
  assert.deepEqual(bound.workspace, { id: "enterprise-stub", name: "Acme Corp" });
  assert.equal(bound.health, "active");
  ctx.api.box.state.enabled = false;
  await rejects(integrations.connect(pid, "box"), "unavailable", "box_unavailable");
});

test("PUT binds the caller's own Dropbox connection by the team (a personal account by its name)", async () => {
  const pid = await ownerProject();
  await rejects(integrations.connect(pid, "dropbox"), "conflict", "dropbox_not_connected");
  ctx.api.dropbox.state.connection = { status: "active", name: "Dana Example", team_name: "Acme Team" };
  const bound = await integrations.connect(pid, "dropbox");
  assert.deepEqual(last(), { method: "PUT", path: `/api/backend/projects/${pid}/integrations/dropbox`, body: null });
  assert.deepEqual(bound.workspace, { id: "team-stub", name: "Acme Team" });
  assert.equal(bound.health, "active");
  ctx.api.dropbox.state.enabled = false;
  await rejects(integrations.connect(pid, "dropbox"), "unavailable", "dropbox_unavailable");
});

test("PUT needs the caller's own active connection and the app on the server", async () => {
  const pid = await ownerProject();
  await rejects(integrations.connect(pid, "github"), "conflict", "github_not_connected");
  ctx.api.github.state.connection = { status: "expired", login: "octo" };
  await rejects(integrations.connect(pid, "github"), "conflict", "github_not_connected");
  await rejects(integrations.connect(pid, "atlassian"), "conflict", "atlassian_not_connected");
  ctx.api.github.state.enabled = false;
  await rejects(integrations.connect(pid, "github"), "unavailable", "github_unavailable");
  ctx.api.jira.state.mode = "token";
  ctx.api.box.state.enabled = false;
  ctx.api.dropbox.state.enabled = false;
  await rejects(integrations.connect(pid, "atlassian"), "unavailable", "atlassian_oauth_unavailable");
  await rejects(integrations.connect(pid, "linear"), "conflict", "linear_not_connected");
  ctx.api.linear.state.available = false;
  await rejects(integrations.connect(pid, "linear"), "unavailable", "linear_unavailable");
  assert.equal(
    (await integrations.list(pid)).every((r) => !r.available || r.app === "slack"),
    true,
  );
});

test("PUT binds the caller's own Linear connection by its workspace, and an expired one reads as reconnect_required", async () => {
  const pid = await ownerProject();
  ctx.api.linear.seed({ connection: { status: "active" } });
  const linear = await integrations.connect(pid, "linear");
  assert.deepEqual(linear.workspace, { id: "org-stub", name: "Acme" });
  assert.equal(linear.health, "active");
  ctx.api.linear.seed({ connection: { status: "expired" } });
  assert.equal((await integrations.list(pid))[3].health, "reconnect_required");
});

test("an expired connection of the member who connected it shows as reconnect_required", async () => {
  const pid = await ownerProject();
  ctx.api.github.state.connection = GITHUB;
  await integrations.connect(pid, "github");
  ctx.api.github.state.connection = { status: "expired", login: "octo" };
  assert.equal((await integrations.list(pid))[1].health, "reconnect_required");
});

test("PUT to a second workspace is refused while a member acts on the first", async () => {
  const pid = await ownerProject();
  ctx.api.github.state.connection = GITHUB;
  await integrations.connect(pid, "github");
  ctx.api.github.state.connection = { status: "active", login: "someone-else" };
  await rejects(integrations.connect(pid, "github"), "conflict", "integration_workspace_mismatch");
});

test("DELETE removes it for the member who connected it; a missing one is not_found", async () => {
  const pid = await ownerProject();
  ctx.api.github.state.connection = GITHUB;
  await integrations.connect(pid, "github");
  await integrations.disconnect(pid, "github");
  assert.deepEqual(last(), { method: "DELETE", path: `/api/backend/projects/${pid}/integrations/github`, body: null });
  assert.equal((await integrations.list(pid))[1].connectedBy, null);
  await rejects(integrations.disconnect(pid, "github"), "not_found", "integration_not_found");
});

test("DELETE of another member's binding: the owner may, a collaborator may not", async () => {
  const { id: owned } = ctx.api.seedProject("Mine", { role: "owner" });
  const { id: shared } = ctx.api.seedProject("Theirs", { role: "collaborator" });
  const other = { user_id: "someone", name: "Sam Ortiz" };
  ctx.api.integrations.seed({ pid: owned, atlassian: { by: other } });
  ctx.api.integrations.seed({ pid: shared, atlassian: { by: other } });
  await rejects(integrations.disconnect(shared, "atlassian"), "forbidden", "integration_forbidden");
  await integrations.disconnect(owned, "atlassian");
  assert.equal((await integrations.list(owned))[0].connectedBy, null);
});

test("a binding whose member left keeps its workspace and reads none", async () => {
  const { id } = ctx.api.seedProject("Left", { role: "owner" });
  ctx.api.integrations.seed({ pid: id, github: { by: null, workspace: { id: "octo", name: "octo" } } });
  const row = (await integrations.list(id))[1];
  assert.equal(row.connectedBy, null);
  assert.equal(row.workspace?.name, "octo");
  assert.equal(row.health, "none");
});

test("Slack: the channel, the test message and the disconnect any member may do", async () => {
  const { id } = ctx.api.seedProject("Chat", { role: "collaborator" });
  assert.deepEqual(await slack.get(id), { available: true, channel: null, editable: true });
  await rejects(slack.test(id), "conflict", "slack_not_connected");
  ctx.api.integrations.seed({ pid: id, slack: { channel: { id: "C1", name: "gaps", private: true } } });
  assert.equal((await integrations.list(id))[2].health, "active");
  sent.length = 0;
  assert.deepEqual(await slack.test(id), { posted: true, channel: { id: "C1", name: "gaps", private: true } });
  assert.deepEqual(last(), { method: "POST", path: `/api/backend/projects/${id}/slack/test`, body: null });
  await slack.unbind(id);
  assert.deepEqual(last(), { method: "DELETE", path: `/api/backend/projects/${id}/slack`, body: null });
  assert.equal((await slack.get(id)).channel, null);
  assert.equal((await integrations.list(id))[2].connectedBy, null);
});

test("Slack test failures carry the API code", async () => {
  const { id } = ctx.api.seedProject("Chat 2", { role: "owner" });
  ctx.api.integrations.seed({ pid: id, slack: { channel: { id: "C1", name: "gaps", private: false } }, slackTest: "slack_not_in_channel" });
  await rejects(slack.test(id), "conflict", "slack_not_in_channel");
  ctx.api.integrations.seed({ slackTest: "slack_unreachable" });
  await rejects(slack.test(id), "unavailable", "slack_unreachable");
  ctx.api.integrations.seed({ slackAvailable: false });
  await rejects(slack.test(id), "unavailable", "slack_unavailable");
  assert.equal((await integrations.list(id))[2].available, false);
});
