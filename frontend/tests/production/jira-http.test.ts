// docs/jira-sync/api.md#web-routes, ui.md#ports (FR-JIRA-UI-*, FR-JIRA-CFG client side): the production JiraPort over the three Jira
// routes, against the stub API (tests/support/stub-api.mjs, stub-jira.mjs). The real `lib/api.ts` and the adapter run; only `fetch` is
// replaced, to send the `/api/backend/*` URLs to the stub as `/api/v1/*` and to record every request. No BFF, no browser.
// Layer U: the wire parsers and the error map. Layer P: each route's request, response and error rows. Port 3296 (stub API).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHttpJira, toJiraAccess, toJiraSettings } from "@/lib/data/http/jira";
import { createHttpProjects } from "@/lib/data/http/projects";
import { toDataError } from "@/lib/data/http/errors";
import { ApiError } from "@/lib/api";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const PORT = 3296;
const ctx = serveStub(PORT, (api) => {
  Object.assign(api.jira.state, { available: true, mode: "token", connection: null, callbackError: null });
});
const { sent } = ctx;

const jira = createHttpJira();
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response" && e.retryable);
};

async function ownerProject() {
  const p = await createProject.create({ name: "P" });
  sent.length = 0;
  return p.id;
}
const collaboratorProject = () => {
  const { id } = ctx.api.seedProject("Shared", { role: "collaborator" });
  sent.length = 0;
  return id;
};

// ---- Layer U ----

const wireSettings = (over: Record<string, unknown> = {}) => ({
  available: true,
  editable: true,
  site_url: "https://example.atlassian.net",
  jira_project: "PAY",
  epic_key: null,
  created_epic_key: "PAY-9",
  enabled: true,
  sync: { project_id: "p", status: "succeeded", finished_at: "2026-01-01T00:00:00Z", error_code: null, counts: null },
  ...over,
});

test("toJiraSettings reads the settings and the sync status; any other shape is invalid_response", () => {
  assert.deepEqual(toJiraSettings(wireSettings()), {
    available: true,
    editable: true,
    siteUrl: "https://example.atlassian.net",
    jiraProject: "PAY",
    epicKey: null,
    createdEpicKey: "PAY-9",
    enabled: true,
    sync: { status: "succeeded", finishedAt: "2026-01-01T00:00:00Z", errorCode: null, progress: null },
    connection: null,
  });
  assert.equal(toJiraSettings(wireSettings({ sync: null, jira_project: null, enabled: false })).sync, null);
  invalid(() => toJiraSettings(wireSettings({ available: "yes" })));
  invalid(() => toJiraSettings(wireSettings({ editable: undefined })));
  invalid(() => toJiraSettings(wireSettings({ site_url: 3 })));
  invalid(() => toJiraSettings(wireSettings({ sync: { status: "sleeping", finished_at: null, error_code: null } })));
  invalid(() => toJiraSettings(wireSettings({ sync: { status: "idle", finished_at: 5, error_code: null } })));
  invalid(() => toJiraSettings(wireSettings({ sync: "idle" })));
  invalid(() => toJiraSettings(null));
});

test("toJiraSettings reads sync.progress: missing and null are none; a step with whole counts is kept; any other shape is invalid_response", () => {
  const withProgress = (progress: unknown) => wireSettings({ sync: { status: "running", finished_at: null, error_code: null, progress } });
  const progressOf = (progress: unknown) => toJiraSettings(withProgress(progress)).sync?.progress;
  assert.equal(progressOf(undefined), null); // an older server omits the field
  assert.equal(progressOf(null), null);
  assert.deepEqual(progressOf({ step: "issues", done: 23, total: 60 }), { step: "issues", done: 23, total: 60 });
  assert.deepEqual(progressOf({ step: "links", done: 0, total: 0 }), { step: "links", done: 0, total: 0 });
  assert.deepEqual(progressOf({ step: "order", done: 7, total: 5 }), { step: "order", done: 7, total: 5 }); // done > total is the view's to clamp
  for (const bad of [
    "issues",
    [],
    {},
    { step: "sorting", done: 1, total: 2 },
    { step: "issues", done: -1, total: 2 },
    { step: "issues", done: 1, total: -2 },
    { step: "issues", done: 1.5, total: 2 },
    { step: "issues", done: "1", total: 2 },
    { step: "issues", done: 1 },
    { step: "issues", total: 2 },
    { done: 1, total: 2 },
  ]) {
    invalid(() => toJiraSettings(withProgress(bad)));
  }
});

test("toJiraAccess reads an ok answer and a failed one (field may be null); any other shape is invalid_response", () => {
  assert.deepEqual(toJiraAccess({ result: "ok", project_name: "Pay", epic_summary: null }), {
    result: "ok",
    projectName: "Pay",
    epicSummary: null,
  });
  assert.deepEqual(toJiraAccess({ result: "failed", reason: "jira_not_found", field: "epic_key" }), {
    result: "failed",
    reason: "jira_not_found",
    field: "epic_key",
  });
  assert.equal(toJiraAccess({ result: "failed", reason: "jira_auth_failed", field: null }).result, "failed");
  invalid(() => toJiraAccess({ result: "ok", epic_summary: null }));
  invalid(() => toJiraAccess({ result: "ok", project_name: "Pay", epic_summary: 1 }));
  invalid(() => toJiraAccess({ result: "failed", reason: "", field: null }));
  invalid(() => toJiraAccess({ result: "failed", reason: "x", field: "summary" }));
  invalid(() => toJiraAccess({ result: "maybe" }));
});

test("error mapping: jira_unavailable is unavailable and retryable, jira_not_configured is conflict; both keep their API code", () => {
  const unavailable = toDataError(new ApiError(503, "jira_unavailable"));
  assert.equal(unavailable.code, "unavailable");
  assert.equal(unavailable.retryable, true);
  assert.equal(unavailable.detail?.apiCode, "jira_unavailable");
  const conflict = toDataError(new ApiError(409, "jira_not_configured"));
  assert.equal(conflict.code, "conflict");
  assert.equal(conflict.detail?.apiCode, "jira_not_configured");
});

// ---- Layer P: the routes ----

test("GET /projects/{pid}/jira: a project without a target is empty, editable for the owner, with the site address", async () => {
  const pid = await ownerProject();
  const s = await jira.get(pid);
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "GET", path: `/api/backend/projects/${pid}/jira`, body: null },
  );
  assert.deepEqual(s, {
    available: true,
    editable: true,
    siteUrl: "https://example.atlassian.net",
    jiraProject: null,
    epicKey: null,
    createdEpicKey: null,
    enabled: false,
    sync: null,
    connection: null,
  });
});

test("GET while the server has no Atlassian account: available false, no site address", async () => {
  const pid = await ownerProject();
  ctx.api.jira.state.available = false;
  const s = await jira.get(pid);
  assert.equal(s.available, false);
  assert.equal(s.siteUrl, null);
});

test("GET for a collaborator: editable false", async () => {
  const pid = collaboratorProject();
  assert.equal((await jira.get(pid)).editable, false);
});

test("PUT /projects/{pid}/jira {jira_project, epic_key, enabled}: the keys are normalised and the answer is the new state", async () => {
  const pid = await ownerProject();
  const s = await jira.save(pid, { jiraProject: " pay ", epicKey: "pay-12", enabled: false });
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    {
      method: "PUT",
      path: `/api/backend/projects/${pid}/jira`,
      body: JSON.stringify({ jira_project: " pay ", epic_key: "pay-12", enabled: false }),
    },
  );
  assert.equal(s.jiraProject, "PAY");
  assert.equal(s.epicKey, "PAY-12");
  assert.equal(s.enabled, false);
  assert.equal(s.sync?.status, "idle");
  const on = await jira.save(pid, { jiraProject: "PAY", epicKey: null, enabled: true });
  assert.equal(on.enabled, true);
  assert.equal(on.epicKey, null);
  assert.equal((await jira.get(pid)).enabled, true);
});

test("PUT errors: a bad key is validation with the field in details.errors; a collaborator is forbidden; no account is unavailable", async () => {
  const pid = await ownerProject();
  await assert.rejects(jira.save(pid, { jiraProject: "1", epicKey: "OTHER-1", enabled: true }), (e) => {
    return (
      isDataError(e) &&
      e.code === "validation" &&
      e.detail?.apiCode === "validation_failed" &&
      JSON.stringify(e.detail.errors?.map((x) => x.loc)) ===
        JSON.stringify([
          ["body", "jira_project"],
          ["body", "epic_key"],
        ])
    );
  });
  await rejects(jira.save(collaboratorProject(), { jiraProject: "PAY", epicKey: null, enabled: true }), "forbidden", "owner_required");
  ctx.api.jira.state.available = false;
  await rejects(jira.save(pid, { jiraProject: "PAY", epicKey: null, enabled: true }), "unavailable", "jira_unavailable");
});

test("POST /projects/{pid}/jira/access-check: ok names the project and the Epic; no Epic key means a null summary", async () => {
  const pid = await ownerProject();
  const a = await jira.checkAccess(pid, { jiraProject: "PAY", epicKey: "PAY-1" });
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    {
      method: "POST",
      path: `/api/backend/projects/${pid}/jira/access-check`,
      body: JSON.stringify({ jira_project: "PAY", epic_key: "PAY-1" }),
    },
  );
  assert.deepEqual(a, { result: "ok", projectName: "Payments platform", epicSummary: "Checkout revamp" });
  const none = await jira.checkAccess(pid, { jiraProject: "PAY", epicKey: null });
  assert.equal(none.result === "ok" ? none.epicSummary : "x", null);
});

test("a failed access check is an answer, not an error: reason and field come back", async () => {
  const pid = await ownerProject();
  ctx.api.jira.seed({ pid, access: { reason: "jira_forbidden", field: "jira_project" } });
  assert.deepEqual(await jira.checkAccess(pid, { jiraProject: "PAY", epicKey: null }), {
    result: "failed",
    reason: "jira_forbidden",
    field: "jira_project",
  });
  ctx.api.jira.seed({ pid, access: { reason: "jira_unreachable" } });
  assert.deepEqual(await jira.checkAccess(pid, { jiraProject: "PAY", epicKey: null }), {
    result: "failed",
    reason: "jira_unreachable",
    field: null,
  });
});

test("access-check errors: collaborator forbidden, no account unavailable, bad key validation", async () => {
  const pid = await ownerProject();
  await rejects(jira.checkAccess(collaboratorProject(), { jiraProject: "PAY", epicKey: null }), "forbidden", "owner_required");
  await rejects(jira.checkAccess(pid, { jiraProject: "p", epicKey: null }), "validation", "validation_failed");
  ctx.api.jira.state.available = false;
  await rejects(jira.checkAccess(pid, { jiraProject: "PAY", epicKey: null }), "unavailable", "jira_unavailable");
});

test("a project the caller is not in answers not_found with project_not_found", async () => {
  await rejects(jira.get("00000000-0000-4000-8000-000000000000"), "not_found", "project_not_found");
});

test("the seed seam: a target with a sync status and a created Epic, and null removes it", async () => {
  const pid = await ownerProject();
  ctx.api.jira.seed({
    pid,
    target: {
      jira_project: "PAY",
      epic_key: null,
      created_epic_key: "PAY-7",
      enabled: true,
      sync: { status: "failed", finished_at: "2026-02-01T10:00:00Z", error_code: "jira_auth_failed" },
    },
  });
  const s = await jira.get(pid);
  assert.equal(s.createdEpicKey, "PAY-7");
  assert.deepEqual(s.sync, { status: "failed", finishedAt: "2026-02-01T10:00:00Z", errorCode: "jira_auth_failed", progress: null });
  ctx.api.jira.seed({
    pid,
    target: {
      jira_project: "PAY",
      epic_key: null,
      enabled: true,
      sync: { status: "running", finished_at: null, error_code: null, progress: { step: "links", done: 3, total: 8 } },
    },
  });
  assert.deepEqual((await jira.get(pid)).sync?.progress, { step: "links", done: 3, total: 8 }); // the seed's progress reaches the port
  ctx.api.jira.seed({ pid, target: null });
  assert.equal((await jira.get(pid)).jiraProject, null);
});

test("U6, U12: a finished_at that is not a date is invalid_response; site_url keeps https only and loses a trailing slash", () => {
  invalid(() => toJiraSettings(wireSettings({ sync: { status: "idle", finished_at: "garbage", error_code: null } })));
  assert.equal(toJiraSettings(wireSettings({ site_url: "https://example.atlassian.net/" })).siteUrl, "https://example.atlassian.net");
  assert.equal(toJiraSettings(wireSettings({ site_url: "javascript:alert(1)" })).siteUrl, null);
  assert.equal(toJiraSettings(wireSettings({ site_url: ["http", "example.atlassian.net"].join("://") })).siteUrl, null);
  assert.equal(toJiraSettings(wireSettings({ site_url: "not a url" })).siteUrl, null);
});
