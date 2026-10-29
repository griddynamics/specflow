// P16 fix round 1 (review + QA qa/P16-qa.md), what is left after K5 (GDRIVE plan 07): malformed 2xx bodies (QA B1) for projects, /me
// and the sources status. Stub API only (R11); faults are injected in the test's fetch layer. Ports 3256-3257.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { createHttpAdapter } from "@/lib/data/http";
import { isDataError, type DataError } from "@/lib/domain/errors";
import { jsonResponse, startEnv, withHook, type Env } from "./p16-support";

let env: Env;
before(async () => {
  env = await startEnv(3256, 3257);
});
after(async () => {
  await env.stop();
});

const newProject = async (name: string) => (await createHttpAdapter().createProject.create({ name, contributorIds: [] })).id;

async function rejection(p: Promise<unknown>): Promise<DataError> {
  try {
    await p;
  } catch (e) {
    assert.ok(isDataError(e), `a DataError, got ${String(e)}`);
    return e;
  }
  assert.fail("expected a rejection");
}

test("QA B1: malformed 2xx bodies reject invalid_response (projects list/get/create, /me, sources status); never a TypeError or undefined fields", async () => {
  const http = createHttpAdapter();
  const pid = await newProject("Malformed");
  // Bodies with no readable `processed_documents`: the gate rejects too.
  const noGate = [
    "{}",
    "[]",
    "null",
    "5",
    '{"sources":null}',
    '{"progress":{"listing_finished":true,"total":0,"finished":0},"sources":[],"removed_files":0}',
  ];
  // Bodies that are not the v2 shape but carry `processed_documents`: the status rejects, the gate still works (SPECS §9 rolling deploy).
  const gateOnly = [
    '{"progress":{"listing_finished":true,"total":0,"finished":0},"sources":[],"uploads":null,"processed_documents":0}', // the legacy body: no removed_files
    '{"progress":{"listing_finished":"yes","total":0,"finished":0},"sources":[],"removed_files":0,"processed_documents":0}',
    '{"progress":{"listing_finished":true,"total":0,"finished":0},"sources":[{"kind":"gdrive"}],"removed_files":0,"processed_documents":0}',
  ];
  for (const body of ["{}", "[]", '{"items":null}', "null", "5"]) {
    await withHook(
      env,
      (x) => (x.path === "/api/backend/projects?limit=100" ? jsonResponse(200, body) : undefined),
      async () => {
        assert.equal((await rejection(http.projects.list())).code, "invalid_response", `list ${body}`);
      },
    );
  }
  const status = (x: { method: string; path: string }) => x.method === "GET" && x.path === `/api/backend/projects/${pid}/sources?batches=1`;
  for (const body of [...noGate, ...gateOnly]) {
    await withHook(
      env,
      (x) => (status(x) ? jsonResponse(200, body) : undefined),
      async () => {
        assert.equal((await rejection(http.knowledge.status(pid))).code, "invalid_response", `status ${body}`);
        if (noGate.includes(body))
          assert.equal((await rejection(http.missionControl.snapshot(pid))).code, "invalid_response", `gate ${body}`);
        else assert.deepEqual(await http.missionControl.snapshot(pid), { phase: "empty" }, `gate ${body}`);
      },
    );
  }
  for (const body of ["{}", "[]", "null"]) {
    await withHook(
      env,
      (x) => (x.path === `/api/backend/projects/${pid}` ? jsonResponse(200, body) : undefined),
      async () => {
        assert.equal((await rejection(http.projects.get(pid))).code, "invalid_response", `get ${body}`);
      },
    );
    await withHook(
      env,
      (x) => (x.method === "POST" && x.path === "/api/backend/projects" ? jsonResponse(201, body) : undefined),
      async () => {
        assert.equal(
          (await rejection(http.createProject.create({ name: "Z", contributorIds: [] }))).code,
          "invalid_response",
          `create ${body}`,
        );
      },
    );
    await withHook(
      env,
      (x) => (x.path === "/api/backend/me" ? jsonResponse(200, body) : undefined),
      async () => {
        assert.equal((await rejection(createHttpAdapter().people.me())).code, "invalid_response", `me ${body}`);
      },
    );
  }
});
