// The Confluence link source against the stub API (tests/support/stub-confluence.mjs): the production `SourcesPort`'s check and add through the
// real `lib/api.ts`, the wire parsers, and the unified status that then lists the source. Port 3291 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { getSourcesStatus } from "@/lib/api";
import { createHttpProjects } from "@/lib/data/http/projects";
import { createHttpSources, toConfluenceCheck } from "@/lib/data/http/sources";
import { isUnknownKind, toSource } from "@/lib/data/http/sources-wire";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const PORT = 3291;
const ctx = serveStub(PORT, () => undefined);
const { sent } = ctx;
const changed: string[] = [];
const sources = createHttpSources({ changed: (pid) => changed.push(pid) });
const { createProject } = createHttpProjects();
const SITE = "https://northwind.atlassian.net";
const PAGE = `${SITE}/wiki/spaces/ORD/pages/12345/Checkout`;
const last = () => sent[sent.length - 1];

async function project() {
  const p = await createProject.create({ name: "P" });
  sent.length = 0;
  changed.length = 0;
  return p.id;
}
const codeOf = async (run: () => Promise<unknown>) => {
  const e = await run().then(
    () => null,
    (error: unknown) => error,
  );
  assert.ok(isDataError(e), "a DataError");
  return e.detail?.apiCode;
};

// ---- the wire ----

test("a check answer keeps kind, title, pages, the cap and the linked issues; any other shape is invalid_response", () => {
  assert.deepEqual(toConfluenceCheck({ kind: "page", title: "T", pages: 4, capped: false, linked_issues: 3 }), {
    kind: "page",
    title: "T",
    pages: 4,
    capped: false,
    linkedIssues: 3,
  });
  for (const bad of [
    null,
    {},
    { kind: "folder", title: "T", pages: 1, capped: false, linked_issues: 0 },
    { kind: "page", title: "T", pages: -1, capped: false, linked_issues: 0 },
    { kind: "page", title: "T", pages: 1, capped: "no", linked_issues: 0 },
  ]) {
    assert.throws(
      () => toConfluenceCheck(bad),
      (e) => isDataError(e) && e.code === "invalid_response",
    );
  }
});

test("the status reads a Confluence source as a source; a kind the client does not know (a Jira epic until its own session) is still skipped", () => {
  assert.equal(isUnknownKind({ kind: "confluence" }), false);
  assert.equal(isUnknownKind({ kind: "jira_epic" }), true);
  const wire = {
    id: "s1",
    kind: "confluence",
    name: "Ordering",
    name_suffix: 1,
    added_by: { id: "u1", display_name: "Alex" },
    created_at: "2026-11-25T09:00:00Z",
    listing_finished: false,
    open: false,
    total: 0,
    counts: { waiting: 0, processing: 0, done: 0, not_read: 0, skipped: 0, failed: 0, already_in_project: 0 },
    failure_reason: null,
    folder_id: null,
    link: "https://x",
  };
  assert.equal(toSource(wire).kind, "confluence");
});

// ---- the routes ----

test("check posts {link, follow} and answers the page: its title, the pages an import takes, and the linked issues only with follow", async () => {
  const pid = await project();
  const found = await sources.checkConfluence(pid, { link: PAGE, follow: true });
  assert.deepEqual(found, { kind: "page", title: "Checkout notes", pages: 4, capped: false, linkedIssues: 3 });
  assert.equal(last().method, "POST");
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/confluence/check`);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: PAGE, follow: true });
  assert.equal((await sources.checkConfluence(pid, { link: PAGE, follow: false })).linkedIssues, 0);
  assert.equal(changed.length, 0, "a check changes nothing");
});

test("a space answers its pages, and the cap says so when it was hit", async () => {
  const pid = await project();
  assert.deepEqual(await sources.checkConfluence(pid, { link: `${SITE}/wiki/spaces/ORD`, follow: true }), {
    kind: "space",
    title: "Ordering",
    pages: 42,
    capped: false,
    linkedIssues: 0,
  });
  const big = await sources.checkConfluence(pid, { link: `${SITE}/wiki/spaces/BIG`, follow: false });
  assert.deepEqual([big.pages, big.capped], [100, true]);
});

test("the closed codes reach the caller as the API's code: another site, a short link, not found, forbidden, not a link", async () => {
  const pid = await project();
  const code = (link: string) => codeOf(() => sources.checkConfluence(pid, { link, follow: false }));
  assert.equal(await code("https://evil.example.com/wiki/spaces/ORD"), "atlassian_site_mismatch");
  assert.equal(await code(`${SITE}/wiki/x/AbCdEf`), "confluence_short_link_unsupported");
  assert.equal(await code(`${SITE}/wiki/spaces/ORD/pages/404`), "confluence_not_found");
  assert.equal(await code(`${SITE}/wiki/spaces/ORD/pages/403`), "confluence_forbidden");
  assert.equal(await code("not a link"), "confluence_link_invalid");
  assert.equal(await code(`http://northwind.atlassian.net/wiki/spaces/ORD`), "confluence_link_invalid");
});

test("add posts the same body, tells the knowledge port the sources changed, and the source is in the status as kind confluence", async () => {
  const pid = await project();
  await sources.addConfluence(pid, { link: PAGE, follow: false });
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/confluence`);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: PAGE, follow: false });
  assert.deepEqual(changed, [pid]);
  const status = (await getSourcesStatus(pid)) as {
    sources: { kind: string; name: string; link: string; folder_id: string | null }[];
  };
  const mine = status.sources.filter((s) => s.kind === "confluence");
  assert.equal(mine.length, 1);
  assert.deepEqual([mine[0]?.name, mine[0]?.link, mine[0]?.folder_id], ["Checkout notes", PAGE, null]);
});

test("a refused add still tells the port (the source may exist) and rejects with the code", async () => {
  const pid = await project();
  assert.equal(
    await codeOf(() => sources.addConfluence(pid, { link: `${SITE}/wiki/spaces/ORD/pages/403`, follow: false })),
    "confluence_forbidden",
  );
  assert.deepEqual(changed, [pid]);
  await rejects(sources.checkConfluence(pid, { link: "", follow: false }), "validation", "confluence_link_invalid");
});
