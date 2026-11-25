// docs/linear/gaps.md: the production LinearGapsPort over the three routes of `/projects/{pid}/linear-gaps`, against the stub API
// (tests/support/stub-linear-gaps.mjs). The real `lib/api.ts` and the adapter run; only `fetch` is replaced. Port 3297 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHttpLinearGaps, toLinearGaps, toLinearGapsAccess } from "@/lib/data/http/linear-gaps";
import { createHttpProjects } from "@/lib/data/http/projects";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const ctx = serveStub(3297, (api) => {
  api.linearGaps.state.available = true;
});
const { sent } = ctx;
const gaps = createHttpLinearGaps();
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response" && e.retryable);
};

async function project() {
  const p = await createProject.create({ name: "P" });
  sent.length = 0;
  return p.id;
}

const wire = (over: Record<string, unknown> = {}) => ({
  available: true,
  enabled: true,
  team_id: "team-ENG",
  team_key: "ENG",
  linear_project_id: "lp-1",
  grouping: "area",
  parent_identifier: "ENG-5",
  parent_url: "https://linear.app/northwind/issue/ENG-5",
  posted: 7,
  answered: 2,
  error_code: null,
  ...over,
});

test("toLinearGaps reads the target and its counts; an empty answer is nothing set up; any other shape is invalid_response", () => {
  assert.deepEqual(toLinearGaps(wire()), {
    available: true,
    enabled: true,
    teamKey: "ENG",
    linearProjectId: "lp-1",
    grouping: "area",
    parentIdentifier: "ENG-5",
    parentUrl: "https://linear.app/northwind/issue/ENG-5",
    posted: 7,
    answered: 2,
    errorCode: null,
  });
  const empty = toLinearGaps(
    wire({ team_key: null, linear_project_id: null, grouping: null, enabled: false, parent_identifier: null, parent_url: null }),
  );
  assert.deepEqual([empty.teamKey, empty.grouping, empty.parentIdentifier, empty.linearProjectId], [null, null, null, null]);
  invalid(() => toLinearGaps(wire({ available: "yes" })));
  invalid(() => toLinearGaps(wire({ grouping: "all" })));
  invalid(() => toLinearGaps(wire({ posted: -1 })));
  invalid(() => toLinearGaps(wire({ answered: 1.5 })));
  invalid(() => toLinearGaps(wire({ team_key: 4 })));
  invalid(() => toLinearGaps(wire({ error_code: 4 })));
  invalid(() => toLinearGaps(null));
});

test("toLinearGapsAccess reads an ok with the names and a failure with its field; any other shape is invalid_response", () => {
  assert.deepEqual(toLinearGapsAccess({ result: "ok", team_name: "Engineering", linear_project_name: null }), {
    result: "ok",
    teamName: "Engineering",
    linearProjectName: null,
  });
  assert.deepEqual(toLinearGapsAccess({ result: "failed", reason: "linear_team_not_found", field: "team_key" }), {
    result: "failed",
    reason: "linear_team_not_found",
    field: "team_key",
  });
  invalid(() => toLinearGapsAccess({ result: "failed", reason: "", field: null }));
  invalid(() => toLinearGapsAccess({ result: "failed", reason: "x", field: "other" }));
  invalid(() => toLinearGapsAccess({ result: "ok" }));
});

test("GET: a project with no target is empty and available", async () => {
  const pid = await project();
  const s = await gaps.get(pid);
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "GET", path: `/api/backend/projects/${pid}/linear-gaps`, body: null },
  );
  assert.deepEqual([s.available, s.teamKey, s.enabled, s.posted], [true, null, false, 0]);
});

test("PUT {team_key, linear_project, grouping, enabled}: the key is normalised and the answer is the new state; the same team keeps the counts, another forgets them", async () => {
  const pid = await project();
  const s = await gaps.save(pid, { teamKey: " eng ", linearProject: null, grouping: "area", enabled: true });
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    {
      method: "PUT",
      path: `/api/backend/projects/${pid}/linear-gaps`,
      body: JSON.stringify({ team_key: " eng ", linear_project: null, grouping: "area", enabled: true }),
    },
  );
  assert.deepEqual([s.teamKey, s.grouping, s.enabled], ["ENG", "area", true]);
  ctx.api.linearGaps.seed({
    pid,
    target: { team_key: "ENG", grouping: "area", enabled: true, parent_identifier: "ENG-5", posted: 3, answered: 1 },
  });
  assert.equal((await gaps.save(pid, { teamKey: "ENG", linearProject: null, grouping: "one", enabled: true })).posted, 3);
  assert.equal((await gaps.save(pid, { teamKey: "OPS", linearProject: null, grouping: "one", enabled: true })).posted, 0);
});

test("PUT errors: a bad key is validation naming the field; a server without the channel is unavailable", async () => {
  const pid = await project();
  await assert.rejects(gaps.save(pid, { teamKey: "1", linearProject: null, grouping: "one", enabled: true }), (e) => {
    return isDataError(e) && e.code === "validation" && e.detail?.errors?.[0]?.loc[1] === "team_key";
  });
  ctx.api.linearGaps.state.available = false;
  await rejects(gaps.save(pid, { teamKey: "ENG", linearProject: null, grouping: "one", enabled: true }), "unavailable", "gap_unavailable");
});

test("POST access-check {team_key, linear_project}: ok, and a scripted refusal that names the field as an answer, not an error", async () => {
  const pid = await project();
  assert.deepEqual(await gaps.checkAccess(pid, { teamKey: "eng", linearProject: "Board" }), {
    result: "ok",
    teamName: "Team ENG",
    linearProjectName: "Board",
  });
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    {
      method: "POST",
      path: `/api/backend/projects/${pid}/linear-gaps/access-check`,
      body: JSON.stringify({ team_key: "eng", linear_project: "Board" }),
    },
  );
  ctx.api.linearGaps.seed({ pid, access: { reason: "linear_project_not_found", field: "linear_project" } });
  assert.deepEqual(await gaps.checkAccess(pid, { teamKey: "ENG", linearProject: "Nope" }), {
    result: "failed",
    reason: "linear_project_not_found",
    field: "linear_project",
  });
});
