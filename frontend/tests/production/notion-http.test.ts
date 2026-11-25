// docs/notion/{oauth,source,plan}.md: the Notion source check and add, the plan destination port and the connection routes against the stub API
// (tests/support/stub-notion.mjs), through the real `lib/api.ts` and the http adapters; only `fetch` is replaced. Port 3300 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { authorizeNotion, disconnectNotion, getNotionConnection } from "@/lib/api";
import { createHttpNotionConnection, toNotionAuthorize, toNotionConnection } from "@/lib/data/http/notion-connection";
import { createHttpNotionPlan, toNotionPlan, toNotionPlanAccess } from "@/lib/data/http/notion-plan";
import { createHttpProjects } from "@/lib/data/http/projects";
import { createHttpSources, toNotionCheck } from "@/lib/data/http/sources";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const ctx = serveStub(3300, (api) => {
  api.notion.state.enabled = true;
  api.notion.state.connection = null;
  api.notionPlan.seed({ available: true });
});
const { sent } = ctx;
const changed: string[] = [];
const sources = createHttpSources({ changed: (pid) => changed.push(pid) });
const plan = createHttpNotionPlan();
const { createProject } = createHttpProjects();
const PAGE = "https://www.notion.so/Engineering-Wiki-423ed33606458137b924dd97aa16e0a9";
const DATABASE = "https://www.notion.so/Requirements-423ed3360645819b8a30d73a847000db";
const last = () => sent[sent.length - 1];

async function project(bound = true) {
  const p = await createProject.create({ name: "P" });
  if (bound) ctx.api.integrations.seed({ pid: p.id, notion: { health: "active" } });
  sent.length = 0;
  changed.length = 0;
  return p.id;
}

test("a page link is checked as a page with its sub-pages and a database link as a database", async () => {
  const pid = await project();

  const page = await sources.checkNotion(pid, PAGE);
  const database = await sources.checkNotion(pid, DATABASE);

  assert.deepEqual(page, { kind: "page", title: "Engineering Wiki", pages: 4, capped: false });
  assert.deepEqual([database.kind, database.title, database.pages], ["database", "Requirements", 42]);
  assert.deepEqual(last(), {
    method: "POST",
    path: `/api/backend/projects/${pid}/sources/notion/check`,
    body: JSON.stringify({ link: DATABASE }),
  });
});

test("a refused link rejects with the closed code of the API", async () => {
  const pid = await project();

  await rejects(sources.checkNotion(pid, "https://example.com/x-423ed33606458137b924dd97aa16e0a9"), "validation", "notion_link_invalid");
  await rejects(sources.checkNotion(pid, "https://www.notion.so/Gone-423ed33606458137b924dd97aa160404"), "conflict", "notion_not_shared");
});

test("adding a link starts the import and tells the knowledge port the sources changed", async () => {
  const pid = await project();

  await sources.addNotion(pid, PAGE);

  assert.deepEqual(last(), { method: "POST", path: `/api/backend/projects/${pid}/sources/notion`, body: JSON.stringify({ link: PAGE }) });
  assert.deepEqual(changed, [pid]);
});

test("the check shape: any other body is invalid_response", () => {
  assert.deepEqual(toNotionCheck({ kind: "page", title: "T", pages: 1, capped: true }), {
    kind: "page",
    title: "T",
    pages: 1,
    capped: true,
  });
  for (const bad of [
    null,
    { kind: "space", title: "T", pages: 1, capped: false },
    { kind: "page", title: "T", pages: -1, capped: false },
  ]) {
    assert.throws(
      () => toNotionCheck(bad),
      (e) => isDataError(e) && e.code === "invalid_response",
    );
  }
});

test("a project with no destination reads as available and empty; saving needs the project's Notion binding", async () => {
  const pid = await project(false);

  const empty = await plan.get(pid);
  await rejects(plan.save(pid, { link: PAGE, enabled: true }), "conflict", "notion_not_connected");

  assert.deepEqual([empty.available, empty.enabled, empty.parentPageTitle, empty.sync], [true, false, null, null]);
  assert.deepEqual(await plan.checkAccess(pid, PAGE), { result: "failed", reason: "notion_not_connected" });
  await rejects(sources.checkNotion(pid, PAGE), "conflict", "notion_not_connected");
});

test("an expired Notion connection: the source and the save are 409 notion_reconnect_required, the access check says it as an answer", async () => {
  const pid = await project(false);
  ctx.api.integrations.seed({ pid, notion: { health: "reconnect_required" } });

  await rejects(sources.checkNotion(pid, PAGE), "conflict", "notion_reconnect_required");
  await rejects(sources.addNotion(pid, PAGE), "conflict", "notion_reconnect_required");
  await rejects(plan.save(pid, { link: PAGE, enabled: true }), "conflict", "notion_reconnect_required");
  assert.deepEqual(await plan.checkAccess(pid, PAGE), { result: "failed", reason: "notion_reconnect_required" });
});

test("the connection port reads the caller's connection, asks Notion's page and forgets the connection", async () => {
  const port = createHttpNotionConnection();
  ctx.api.notion.state.connection = { status: "active", workspace_name: "Sample Workspace" };

  const mine = await port.connection();
  const asked = await port.authorize("/projects/p1/integrations");
  await port.disconnect();

  assert.deepEqual([mine.enabled, mine.connected, mine.status, mine.workspaceName], [true, true, "active", "Sample Workspace"]);
  assert.match(asked.url, /^https:\/\/api\.notion\.com\/v1\/oauth\/authorize\?state=/);
  assert.equal((await port.connection()).connected, false);
  for (const bad of [null, { enabled: true, connected: true, status: null }, { enabled: true, connected: false, status: "gone" }]) {
    assert.throws(
      () => toNotionConnection(bad),
      (e) => isDataError(e) && e.code === "invalid_response",
    );
  }
  assert.throws(() => toNotionAuthorize({ url: "http://x.test" }));
});

test("save stores the parent page, turns the pass on and the check answers ok or a closed reason", async () => {
  const pid = await project();

  const ok = await plan.checkAccess(pid, PAGE);
  const saved = await plan.save(pid, { link: PAGE, enabled: true });
  ctx.api.notionPlan.seed({ pid, access: { reason: "notion_permissions_missing" } });
  const refused = await plan.checkAccess(pid, PAGE);

  assert.deepEqual(ok, { result: "ok", parentPageTitle: "Plans" });
  assert.deepEqual([saved.enabled, saved.parentPageTitle, saved.databaseUrl, saved.sync?.status], [true, "Plans", null, "succeeded"]);
  assert.deepEqual(refused, { result: "failed", reason: "notion_permissions_missing" });
  assert.deepEqual(last(), {
    method: "POST",
    path: `/api/backend/projects/${pid}/notion-plan/access-check`,
    body: JSON.stringify({ link: PAGE }),
  });
});

test("the plan wire parsers reject any other shape", () => {
  const bad = (fn: () => unknown) => {
    assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response");
  };
  bad(() => toNotionPlan(null));
  bad(() => toNotionPlan({ available: true, enabled: "yes", connected_by_me: false }));
  bad(() => toNotionPlanAccess({ result: "failed", reason: "" }));
  bad(() => toNotionPlanAccess({ result: "ok" }));
});

test("the connection routes: authorize returns Notion's page, disconnect clears it, an app that is off is 503", async () => {
  const before = await getNotionConnection();
  const url = (await authorizeNotion("/projects/p1/integrations")) as { url: string };
  const status = await disconnectNotion();
  ctx.api.notion.state.enabled = false;

  assert.equal((before as { connected: boolean }).connected, false);
  assert.match(url.url, /^https:\/\/api\.notion\.com\/v1\/oauth\/authorize\?state=/);
  assert.equal(status, 204);
  await assert.rejects(authorizeNotion("/x"));
});
