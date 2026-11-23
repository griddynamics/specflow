// docs/jira-sync/oauth.md#routes: the caller's Atlassian connection through the production JiraPort (`connection`, `authorize`,
// `disconnect`) and the `connection` of the Jira settings in the `oauth` mode, against the stub API (tests/support/stub-jira.mjs). The real
// `lib/api.ts` and the adapter run; only `fetch` is replaced (tests/support/stub-fetch.ts). Layer U: the wire parsers and the error map.
// Layer P: each route's request, response and error rows. Port 3297 (stub API).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHttpJira, toAtlassianConnection, toJiraSettings } from "@/lib/data/http/jira";
import { createHttpProjects } from "@/lib/data/http/projects";
import { toDataError } from "@/lib/data/http/errors";
import { ApiError } from "@/lib/api";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const PORT = 3297;
const ctx = serveStub(PORT, (api) => {
  Object.assign(api.jira.state, { available: true, mode: "oauth", connection: null, callbackError: null });
});
const { sent } = ctx;
const jira = createHttpJira();
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response");
};
const ANN = { status: "active", account_name: "Ann Lee" } as const;

async function ownerProject() {
  const p = await createProject.create({ name: "P" });
  sent.length = 0;
  return p.id;
}

// ---- Layer U ----

const wire = (over: Record<string, unknown> = {}) => ({
  mode: "oauth",
  connected: true,
  status: "active",
  account_name: "Ann Lee",
  connected_at: "2026-11-20T09:30:00Z",
  last_used_at: null,
  ...over,
});

test("toAtlassianConnection reads every mode; a status that disagrees with `connected`, an unknown mode or a bad time is invalid", () => {
  assert.deepEqual(toAtlassianConnection(wire()), {
    mode: "oauth",
    connected: true,
    status: "active",
    accountName: "Ann Lee",
    connectedAt: "2026-11-20T09:30:00Z",
    lastUsedAt: null,
  });
  const off = { connected: false, status: null, account_name: null, connected_at: null };
  assert.equal(toAtlassianConnection(wire({ mode: "token", ...off })).mode, "token");
  assert.equal(toAtlassianConnection(wire({ mode: null, ...off })).mode, null);
  assert.equal(toAtlassianConnection(wire({ status: "expired" })).status, "expired");
  invalid(() => toAtlassianConnection(wire({ mode: "saml" })));
  invalid(() => toAtlassianConnection(wire({ status: null })));
  invalid(() => toAtlassianConnection(wire({ connected: false })));
  invalid(() => toAtlassianConnection(wire({ status: "revoked" })));
  invalid(() => toAtlassianConnection(wire({ connected_at: "yesterday" })));
  invalid(() => toAtlassianConnection(wire({ account_name: 7 })));
  invalid(() => toAtlassianConnection(null));
});

test("toJiraSettings reads `connection`: missing or null is none; the three statuses; any other shape is invalid", () => {
  const base = {
    available: true,
    editable: true,
    site_url: null,
    jira_project: "PAY",
    epic_key: null,
    created_epic_key: null,
    enabled: true,
    sync: null,
  };
  assert.equal(toJiraSettings(base).connection, null);
  assert.equal(toJiraSettings({ ...base, connection: null }).connection, null);
  assert.deepEqual(
    toJiraSettings({ ...base, connection: { connected_by_name: "Ann", connected_by_me: false, status: "expired" } }).connection,
    {
      connectedByName: "Ann",
      connectedByMe: false,
      status: "expired",
    },
  );
  assert.equal(
    toJiraSettings({ ...base, connection: { connected_by_name: null, connected_by_me: false, status: "missing" } }).connection?.status,
    "missing",
  );
  invalid(() => toJiraSettings({ ...base, connection: { connected_by_name: "Ann", connected_by_me: "no", status: "active" } }));
  invalid(() => toJiraSettings({ ...base, connection: { connected_by_name: "Ann", connected_by_me: true, status: "gone" } }));
  invalid(() => toJiraSettings({ ...base, connection: "Ann" }));
});

test("error mapping: jira_not_connected is conflict, atlassian_oauth_unavailable is unavailable; both keep their API code", () => {
  const conflict = toDataError(new ApiError(409, "jira_not_connected"));
  assert.equal(conflict.code, "conflict");
  assert.equal(conflict.detail?.apiCode, "jira_not_connected");
  const off = toDataError(new ApiError(503, "atlassian_oauth_unavailable"));
  assert.equal(off.code, "unavailable");
  assert.equal(off.detail?.apiCode, "atlassian_oauth_unavailable");
});

// ---- Layer P ----

test("GET /integrations/atlassian: not connected, then connected; the token mode answers no connection", async () => {
  assert.deepEqual(await jira.connection(), {
    mode: "oauth",
    connected: false,
    status: null,
    accountName: null,
    connectedAt: null,
    lastUsedAt: null,
  });
  assert.deepEqual({ method: last().method, path: last().path }, { method: "GET", path: "/api/backend/integrations/atlassian" });
  ctx.api.jira.state.connection = ANN;
  const on = await jira.connection();
  assert.equal(on.connected, true);
  assert.equal(on.accountName, "Ann Lee");
  ctx.api.jira.state.mode = "token";
  assert.equal((await jira.connection()).mode, "token");
  assert.equal((await jira.connection()).connected, false);
});

test("POST /integrations/atlassian/authorize {return_to}: the consent URL; outside the oauth mode 503; a foreign return_to 422", async () => {
  const url = await jira.authorize("/projects/p1/settings?tab=jira");
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    {
      method: "POST",
      path: "/api/backend/integrations/atlassian/authorize",
      body: JSON.stringify({ return_to: "/projects/p1/settings?tab=jira" }),
    },
  );
  assert.equal(new URL(url).origin, "https://auth.atlassian.com");
  assert.ok(new URL(url).searchParams.get("state")?.split(".").length === 3);
  await rejects(jira.authorize("//evil.example/x"), "validation", "validation_failed");
  ctx.api.jira.state.mode = "token";
  await rejects(jira.authorize("/"), "unavailable", "atlassian_oauth_unavailable");
});

test("DELETE /integrations/atlassian: 204 forgets the connection; outside the oauth mode 503", async () => {
  ctx.api.jira.state.connection = ANN;
  await jira.disconnect();
  assert.deepEqual({ method: last().method, path: last().path }, { method: "DELETE", path: "/api/backend/integrations/atlassian" });
  assert.equal((await jira.connection()).connected, false);
  ctx.api.jira.state.mode = "token";
  await rejects(jira.disconnect(), "unavailable", "atlassian_oauth_unavailable");
});

test("the oauth mode: the access check needs the caller's connection, a PUT 409s without an active one, and makes the saver the acting account", async () => {
  const pid = await ownerProject();
  assert.deepEqual(await jira.checkAccess(pid, { jiraProject: "PAY", epicKey: null }), {
    result: "failed",
    reason: "jira_not_connected",
    field: null,
  });
  await rejects(jira.save(pid, { jiraProject: "PAY", epicKey: null, enabled: true }), "conflict", "jira_not_connected");
  ctx.api.jira.state.connection = { status: "expired", account_name: "Ann Lee" };
  assert.deepEqual(await jira.checkAccess(pid, { jiraProject: "PAY", epicKey: null }), {
    result: "failed",
    reason: "jira_reconnect_required",
    field: null,
  });
  await rejects(jira.save(pid, { jiraProject: "PAY", epicKey: null, enabled: true }), "conflict", "jira_not_connected");
  ctx.api.jira.state.connection = ANN;
  assert.equal((await jira.get(pid)).connection, null); // no target yet
  const saved = await jira.save(pid, { jiraProject: "PAY", epicKey: null, enabled: true });
  assert.deepEqual(saved.connection, { connectedByName: "Ann Lee", connectedByMe: true, status: "active" });
  await jira.disconnect();
  assert.deepEqual((await jira.get(pid)).connection, { connectedByName: null, connectedByMe: true, status: "missing" });
});
