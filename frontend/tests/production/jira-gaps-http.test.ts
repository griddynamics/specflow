// docs/jira-sync/gaps.md: the production JiraGapsPort over the three routes of `/projects/{pid}/jira-gaps`, against the stub API
// (tests/support/stub-jira-gaps.mjs). The real `lib/api.ts` and the adapter run; only `fetch` is replaced. Port 3293 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHttpJiraGaps, toJiraGaps } from "@/lib/data/http/jira-gaps";
import { createHttpProjects } from "@/lib/data/http/projects";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const ctx = serveStub(3293, (api) => {
  api.jiraGaps.state.available = true;
});
const { sent } = ctx;
const gaps = createHttpJiraGaps();
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response" && e.retryable);
};

async function project(bound = true) {
  const p = await createProject.create({ name: "P" });
  if (bound) ctx.api.integrations.seed({ pid: p.id, atlassian: { health: "active" } });
  sent.length = 0;
  return p.id;
}

const wire = (over: Record<string, unknown> = {}) => ({
  available: true,
  jira_project: "NWO",
  grouping: "area",
  enabled: true,
  story_key: "NWO-5",
  story_url: "https://northwind.atlassian.test/browse/NWO-5",
  posted: 7,
  answered: 2,
  last_polled_at: "2026-11-25T09:00:00Z",
  error_code: null,
  poll_seconds: 60,
  ...over,
});

test("toJiraGaps reads the target and its counts; an empty answer is nothing set up; any other shape is invalid_response", () => {
  assert.deepEqual(toJiraGaps(wire()), {
    available: true,
    jiraProject: "NWO",
    grouping: "area",
    enabled: true,
    storyKey: "NWO-5",
    storyUrl: "https://northwind.atlassian.test/browse/NWO-5",
    posted: 7,
    answered: 2,
    lastPolledAt: "2026-11-25T09:00:00Z",
    errorCode: null,
    pollSeconds: 60,
  });
  const empty = toJiraGaps(
    wire({
      jira_project: null,
      grouping: null,
      enabled: false,
      story_key: null,
      story_url: null,
      posted: 0,
      answered: 0,
      last_polled_at: null,
    }),
  );
  assert.deepEqual([empty.jiraProject, empty.grouping, empty.storyKey, empty.lastPolledAt], [null, null, null, null]);
  invalid(() => toJiraGaps(wire({ available: "yes" })));
  invalid(() => toJiraGaps(wire({ grouping: "all" })));
  invalid(() => toJiraGaps(wire({ posted: -1 })));
  invalid(() => toJiraGaps(wire({ answered: 1.5 })));
  invalid(() => toJiraGaps(wire({ poll_seconds: "60" })));
  invalid(() => toJiraGaps(wire({ last_polled_at: "yesterday" })));
  invalid(() => toJiraGaps(wire({ error_code: 4 })));
  invalid(() => toJiraGaps(null));
});

test("GET: a project with no target is empty and available, with the poll interval", async () => {
  const pid = await project();
  const s = await gaps.get(pid);
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "GET", path: `/api/backend/projects/${pid}/jira-gaps`, body: null },
  );
  assert.deepEqual([s.available, s.jiraProject, s.enabled, s.posted, s.pollSeconds], [true, null, false, 0, 60]);
});

test("PUT {jira_project, grouping, enabled}: the key is normalised and the answer is the new state; the same space keeps the counts, another forgets them", async () => {
  const pid = await project();
  const s = await gaps.save(pid, { jiraProject: " nwo ", grouping: "area", enabled: true });
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    {
      method: "PUT",
      path: `/api/backend/projects/${pid}/jira-gaps`,
      body: JSON.stringify({ jira_project: " nwo ", grouping: "area", enabled: true }),
    },
  );
  assert.deepEqual([s.jiraProject, s.grouping, s.enabled], ["NWO", "area", true]);
  ctx.api.jiraGaps.seed({
    pid,
    target: { jira_project: "NWO", grouping: "area", enabled: true, story_key: "NWO-5", posted: 3, answered: 1 },
  });
  assert.equal((await gaps.save(pid, { jiraProject: "NWO", grouping: "one", enabled: true })).posted, 3);
  assert.equal((await gaps.save(pid, { jiraProject: "OTHER", grouping: "one", enabled: true })).posted, 0);
});

test("PUT errors: a bad key is validation naming the field; no Atlassian binding is a conflict; a server without the channel is unavailable", async () => {
  const pid = await project();
  await assert.rejects(gaps.save(pid, { jiraProject: "1", grouping: "one", enabled: true }), (e) => {
    return isDataError(e) && e.code === "validation" && e.detail?.errors?.[0]?.loc[1] === "jira_project";
  });
  await rejects(gaps.save(await project(false), { jiraProject: "NWO", grouping: "one", enabled: true }), "conflict", "jira_not_connected");
  ctx.api.jiraGaps.state.available = false;
  await rejects(gaps.save(pid, { jiraProject: "NWO", grouping: "one", enabled: true }), "unavailable", "gap_unavailable");
});

test("POST access-check {jira_project}: ok, a scripted refusal that names the field, and no binding as an answer, not an error", async () => {
  const pid = await project();
  assert.deepEqual(await gaps.checkAccess(pid, "nwo"), { result: "ok", projectName: "Northwind", epicSummary: null });
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "POST", path: `/api/backend/projects/${pid}/jira-gaps/access-check`, body: JSON.stringify({ jira_project: "nwo" }) },
  );
  ctx.api.jiraGaps.seed({ pid, access: { reason: "jira_forbidden", field: "jira_project" } });
  assert.deepEqual(await gaps.checkAccess(pid, "NWO"), { result: "failed", reason: "jira_forbidden", field: "jira_project" });
  assert.deepEqual(await gaps.checkAccess(await project(false), "NWO"), { result: "failed", reason: "jira_not_connected", field: null });
});
