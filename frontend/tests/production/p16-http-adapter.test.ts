// FE-BP-02, FE-PPL-01.AC3, FE-BP-08.AC1 (TECH-SPECS §3.3, §10.4): the http adapter's projects, /me and documents calls through
// the real lib/api.ts -> the real BFF (built server) -> the stub API. Ports 3250-3251. No model call (R11).
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { createProject as apiCreateProject } from "@/lib/api";
import { createHttpAdapter } from "@/lib/data/http";
import { createHttpDocuments } from "@/lib/data/http/documents";
import { toDataError } from "@/lib/data/http/errors";
import { isDataError, type DataError } from "@/lib/domain/errors";
import { errorCode, isExistingEndpoint, pdfFile, startEnv, stubCalls, textFile, type Env } from "./p16-support";
import { defined } from "../support/defined";

let env: Env;
before(async () => {
  env = await startEnv(3250, 3251);
});
after(async () => {
  await env.stop();
});

async function rejection(p: Promise<unknown>): Promise<DataError> {
  try {
    await p;
  } catch (e) {
    assert.ok(isDataError(e), `rejects with a DataError, got ${String(e)}`);
    return e;
  }
  assert.fail("expected a rejection");
}

test("FE-PPL-01.AC3, FE-BP-02.AC8: people.me reads GET /me once and maps display_name and email", async () => {
  const http = createHttpAdapter();
  const before = env.sent.filter((s) => s.path === "/api/backend/me").length;
  const me = await http.people.me();
  assert.deepEqual(me, { id: "u1", name: "dev", email: "dev@example.test", kind: "person" });
  await http.people.me();
  const calls = env.sent.filter((s) => s.path === "/api/backend/me");
  assert.equal(calls.length - before, 1, "GET /me is read once per adapter");
  assert.equal(defined(calls.at(-1)).method, "GET");
  assert.equal(defined(calls.at(-1)).headers["x-requested-with"], "specflow");
});

test('FE-BP-02.AC2 (A9): create sends exactly one POST /projects with the body {"name":"Pilot"} only and tells subscribers', async () => {
  const http = createHttpAdapter();
  let changed = 0;
  const off = http.projects.subscribe(() => changed++);
  const n = env.sent.length;
  const p = await http.createProject.create({
    name: "  Pilot ",
    contributorIds: ["user-2"],
  });
  off();
  const posts = env.sent.slice(n).filter((s) => s.method === "POST");
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, "/api/backend/projects");
  assert.equal(posts[0].body, JSON.stringify({ name: "Pilot" }));
  assert.equal(posts[0].headers["x-requested-with"], "specflow");
  assert.equal(posts[0].headers["content-type"], "application/json");
  assert.equal(p.name, "Pilot");
  assert.equal(p.status, "unknown");
  assert.match(p.id, /^[0-9a-f-]{36}$/);
  assert.equal(changed, 1);
});

test("FE-BP-02.AC1: list returns the projects of the projects list, newest first, status unknown (TECH-SPECS §3.5)", async () => {
  const http = createHttpAdapter();
  const a = await http.createProject.create({ name: "Alpha", contributorIds: [] });
  const b = await http.createProject.create({ name: "Beta", contributorIds: [] });
  const list = await http.projects.list();
  assert.deepEqual(
    list.slice(0, 2).map((p) => p.id),
    [b.id, a.id],
  );
  assert.ok(list.every((p) => p.status === "unknown"));
  assert.ok(env.sent.some((s) => s.path === "/api/backend/projects?limit=100" && s.method === "GET"));
});

test("FE-BP-02: get returns one project; an unknown or non-member id rejects not_found (TECH-SPECS §1.8)", async () => {
  const http = createHttpAdapter();
  const p = await http.createProject.create({ name: "Gamma", contributorIds: [] });
  assert.deepEqual(await http.projects.get(p.id), p);
  const e = await rejection(http.projects.get("00000000-0000-4000-8000-000000000000"));
  assert.equal(e.code, "not_found");
  assert.equal(e.detail?.apiCode, "project_not_found");
});

test("FE-BP-02.AC4: an invalid name rejects validation (locally, no request; and the stub's 422 maps to validation)", async () => {
  const http = createHttpAdapter();
  const n = env.sent.length;
  assert.equal((await rejection(http.createProject.create({ name: "   ", contributorIds: [] }))).code, "validation");
  assert.equal((await rejection(http.createProject.create({ name: "x".repeat(201), contributorIds: [] }))).code, "validation");
  assert.equal(env.sent.length, n, "no request for a name the backend would refuse");
  // The same rule on the server: lib/api.ts -> BFF -> stub 422 validation_failed -> validation.
  const e = toDataError(await apiCreateProject("").catch((x: unknown) => x));
  assert.equal(e.code, "validation");
  assert.equal(e.detail?.status, 422);
});

test("FE-BP-08.AC1: documents upload the raw file body with application/pdf and X-Requested-With, one file per request", async () => {
  const http = createHttpAdapter();
  const docs = createHttpDocuments();
  const p = await http.createProject.create({ name: "Docs", contributorIds: [] });
  const file = pdfFile("spec one.pdf", 4096);
  const n = env.sent.length;
  const first = await docs.upload(p.id, file);
  assert.equal(first.outcome, "created");
  assert.equal(first.document.original_filename, "spec one.pdf");
  assert.equal(first.document.status, "processing", "K5: the server starts the parse itself");
  const up = defined(env.sent.slice(n).find((s) => s.method === "POST"));
  assert.equal(up.path, `/api/backend/projects/${p.id}/documents?filename=spec%20one.pdf`);
  assert.equal(up.headers["content-type"], "application/pdf");
  assert.equal(up.headers["x-requested-with"], "specflow");
  assert.equal(up.bytes, file.size);
  const stubUp = defined(stubCalls(env.api, p.id, "upload").at(-1));
  assert.equal(stubUp.contentType, "application/pdf", "the BFF forwards the raw body as application/pdf");
  assert.equal(stubUp.bytes, file.size, "the raw body arrives whole (no multipart)");
  assert.equal(stubUp.head, "%PDF-");
  // Duplicate: same bytes again -> 200 { outcome: "duplicate" } (FE-BP-08.AC9).
  const again = await docs.upload(p.id, file);
  assert.equal(again.outcome, "duplicate");
  assert.equal(again.document.id, first.document.id);
  // Non-PDF -> 415 unsupported_format, not retryable (FE-BP-08.AC9, AC12).
  const bad = await rejection(docs.upload(p.id, textFile("notes.txt")));
  assert.deepEqual([bad.code, bad.retryable, bad.detail?.status], ["unsupported_format", false, 415]);
  assert.equal([...env.api.docs.values()].filter((d) => d.pid === p.id).length, 1, "the duplicate and the rejected file add no document");
  assert.equal(stubCalls(env.api, p.id, "parse").length, 0, "the browser never calls parse (K5)");
});

test("FE-BP-02 (BFF rules): a mutation without X-Requested-With is refused by the BFF (403 csrf_rejected -> unknown); lib/api.ts always sends it", async () => {
  const res = await fetch("/api/backend/projects", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: '{"name":"x"}',
  });
  assert.equal(res.status, 403);
  assert.equal(await errorCode(res), "csrf_rejected");
  const viaApi = env.sent.filter((s) => !(s.method === "POST" && s.body === '{"name":"x"}' && !s.headers["x-requested-with"]));
  assert.ok(viaApi.length > 0);
  assert.ok(
    viaApi.every((s) => s.headers["x-requested-with"] === "specflow"),
    "every adapter request carries X-Requested-With: specflow",
  );
});

test("NFR-SEC-0002.AC2: a DELETE of a source without X-Requested-With is refused by the BFF (403 csrf_rejected); the adapter's DELETE carries it", async () => {
  const http = createHttpAdapter();
  const p = await http.createProject.create({ name: "Delete CSRF", contributorIds: [] });
  const sid = env.api.model.seed(p.id, { kind: "upload", files: [{ name: "a.pdf", state: "done" }] });
  const path = `/api/backend/projects/${p.id}/sources/${sid}`;
  const n = env.api.log.length;
  const res = await fetch(path, { method: "DELETE" });
  assert.equal(res.status, 403);
  assert.equal(await errorCode(res), "csrf_rejected");
  assert.equal(env.api.log.length, n, "the refused DELETE never reached the API");
  assert.equal((await http.knowledge.status(p.id)).sources.length, 1, "the source is still there");
  await http.sources.remove(p.id, sid);
  const sent = env.sent.filter((s) => s.method === "DELETE" && s.path === path);
  assert.equal(sent.length, 2);
  assert.equal(sent[0].headers["x-requested-with"], undefined, "the raw request had no CSRF header");
  assert.equal(sent[1].headers["x-requested-with"], "specflow", "lib/api.ts sends it on DELETE");
  assert.equal((await http.knowledge.status(p.id)).sources.length, 0);
});

test("FE-BP-02: without a session every port call rejects unauthenticated (401)", async () => {
  const http = createHttpAdapter();
  env.setLoggedIn(false);
  try {
    assert.equal((await rejection(http.people.me())).code, "unauthenticated");
    assert.equal((await rejection(http.projects.list())).code, "unauthenticated");
    assert.equal((await rejection(http.missionControl.snapshot("00000000-0000-4000-8000-000000000000"))).code, "unauthenticated");
    assert.equal((await rejection(http.knowledge.status("00000000-0000-4000-8000-000000000000"))).code, "unauthenticated");
  } finally {
    env.setLoggedIn(true);
  }
  // A failed /me read is not cached: the next call asks again and succeeds.
  assert.equal((await http.people.me()).name, "dev");
});

test("FE-BP-02 (no backend change): every request the stub API received is one of the existing endpoints", () => {
  const seen = env.api.log.filter((e) => e.path.startsWith("/api/v1/"));
  assert.ok(seen.length > 10);
  const other = seen
    .filter((e) => !isExistingEndpoint(e.method, e.path))
    .map((e) => `${e.method} ${e.path.replace(/[0-9a-f-]{36}/g, ":id")}`);
  assert.deepEqual(other, []);
});
