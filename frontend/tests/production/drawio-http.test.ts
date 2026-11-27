// The diagrams.net link source against the stub API (tests/support/stub-drawio.mjs): the production `SourcesPort`'s check and add through the
// real `lib/api.ts`, the wire parser, and the unified status that then lists the source as a node. Port 3300 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { getSourcesStatus } from "@/lib/api";
import { toDrawioCheck } from "@/lib/data/http/drawio-wire";
import { createHttpProjects } from "@/lib/data/http/projects";
import { createHttpSources } from "@/lib/data/http/sources";
import { isUnknownKind, toSource } from "@/lib/data/http/sources-wire";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const PORT = 3300;
const ctx = serveStub(PORT, () => undefined);
const { sent } = ctx;
const changed: string[] = [];
const sources = createHttpSources({ changed: (pid) => changed.push(pid) });
const { createProject } = createHttpProjects();
const BASE = "https://app.diagrams.net/";
const FLOW_NAME = "Release flow";
const LINK_INVALID = "drawio_link_invalid";
const PLAIN_SCHEME = ["ht", "tp"].join("");
const last = () => sent[sent.length - 1];

async function project() {
  const { id } = await createProject.create({ name: "Diagrams" });
  sent.length = 0;
  changed.length = 0;
  return id;
}
async function codeOf(run: () => Promise<unknown>) {
  let failure: unknown = null;
  try {
    await run();
  } catch (error) {
    failure = error;
  }
  assert.ok(isDataError(failure), "a DataError");
  return failure.detail?.apiCode;
}

// ---- the wire ----

test("a check answer keeps the name, the pages with their shapes and arrows, and the cap; any other shape is invalid_response", () => {
  assert.deepEqual(toDrawioCheck({ name: "N", capped: true, pages: [{ name: "P", shapes: 3, arrows: 2, extra: 1 }] }), {
    name: "N",
    capped: true,
    pages: [{ name: "P", shapes: 3, arrows: 2 }],
  });
  for (const bad of [
    null,
    {},
    { name: "N", capped: false },
    { name: "N", capped: "no", pages: [] },
    { name: "N", capped: false, pages: [{ name: "P", shapes: -1, arrows: 0 }] },
    { name: "N", capped: false, pages: [{ name: "P", shapes: 1.5, arrows: 0 }] },
    { name: "N", capped: false, pages: [null] },
  ]) {
    assert.throws(
      () => toDrawioCheck(bad),
      (e) => isDataError(e) && e.code === "invalid_response",
    );
  }
});

test("the status reads a diagrams.net source as a source", () => {
  assert.equal(isUnknownKind({ kind: "drawio" }), false);
  const wire = {
    id: "s1",
    kind: "drawio",
    name: FLOW_NAME,
    name_suffix: 1,
    added_by: { id: "u1", display_name: "Alex" },
    created_at: "2026-11-25T09:00:00Z",
    listing_finished: true,
    open: false,
    total: 2,
    counts: { waiting: 0, processing: 0, done: 2, not_read: 0, skipped: 0, failed: 0, already_in_project: 0 },
    failure_reason: null,
    folder_id: null,
    link: BASE,
  };
  assert.equal(toSource(wire).kind, "drawio");
});

// ---- the routes ----

test("check posts {link} and answers the diagram: its name, its pages and the cap; it changes nothing", async () => {
  const pid = await project();
  const link = `${BASE}#R${"A".repeat(20000)}`;
  const found = await sources.checkDrawio(pid, link);
  assert.deepEqual(found, {
    name: FLOW_NAME,
    pages: [
      { name: "Main", shapes: 41, arrows: 41 },
      { name: "Details", shapes: 12, arrows: 9 },
    ],
    capped: false,
  });
  assert.equal(last().method, "POST");
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/drawio/check`);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link }, "a very long link is sent whole");
  assert.equal(changed.length, 0, "a check changes nothing");
  assert.equal((await sources.checkDrawio(pid, `${BASE}#Rmany`)).capped, true);
  assert.equal((await sources.checkDrawio(pid, `${BASE}#Uhttps%3A%2F%2Fexample.com%2Fd.drawio`)).name, FLOW_NAME);
  for (const ok of [
    `${BASE}?url=https%3A%2F%2Fexample.com%2Fd.drawio`,
    "https://example.com/files/d.drawio",
    "https://app.diagrams.net:443/#Rabc",
    `${BASE}#P${encodeURIComponent(JSON.stringify({ data: "<mxfile/>" }))}`,
  ]) {
    assert.equal((await sources.checkDrawio(pid, ok)).name, FLOW_NAME, ok);
  }
  assert.equal((await sources.checkDrawio(pid, `${BASE}#P${encodeURIComponent(JSON.stringify({ data: "many" }))}`)).capped, true);
});

test("the closed codes reach the caller as the API's code", async () => {
  const pid = await project();
  const code = (link: string) => codeOf(() => sources.checkDrawio(pid, link));
  assert.equal(await code("not a link"), LINK_INVALID);
  assert.equal(await code("https://evil.example.com/#Rabc"), LINK_INVALID);
  assert.equal(await code(`${BASE}#Gabc`), "drawio_link_unsupported");
  assert.equal(await code(`${BASE}#Labc`), "drawio_link_unsupported");
  assert.equal(await code(`${BASE}#Dabc`), "drawio_link_unsupported");
  assert.equal(await code(`${BASE}#P${encodeURIComponent("{}")}`), LINK_INVALID);
  assert.equal(await code(`${BASE}#Pnot-json`), LINK_INVALID);
  assert.equal(await code(`${PLAIN_SCHEME}://app.diagrams.net/#Rabc`), LINK_INVALID);
  assert.equal(await code("https://app.diagrams.net:8443/#Rabc"), LINK_INVALID);
  assert.equal(await code(`${BASE}#R${"A".repeat(300 * 1024)}`), "drawio_too_large");
  assert.equal(await code(`${BASE}#Rbad`), "drawio_not_a_diagram");
  assert.equal(await code(`${BASE}#Rbig`), "drawio_too_large");
  assert.equal(await code(`${BASE}#Ufail`), "drawio_fetch_failed");
});

test("add posts the same body, tells the knowledge port the sources changed, and the source is in the status as kind drawio", async () => {
  const pid = await project();
  const link = `${BASE}#Rabc`;
  await sources.addDrawio(pid, link);
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/drawio`);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link });
  assert.deepEqual(changed, [pid]);
  const status = (await getSourcesStatus(pid)) as { sources: { kind: string; name: string; link: string }[] };
  const mine = status.sources.filter((s) => s.kind === "drawio");
  assert.equal(mine.length, 1);
  assert.deepEqual([mine[0]?.name, mine[0]?.link], [FLOW_NAME, BASE], "the stored link has no fragment");
});

test("a refused add still tells the port (the source may exist) and rejects with the code", async () => {
  const pid = await project();
  assert.equal(await codeOf(() => sources.addDrawio(pid, `${BASE}#Gabc`)), "drawio_link_unsupported");
  assert.deepEqual(changed, [pid]);
  await rejects(sources.checkDrawio(pid, `${BASE}#Rbig`), "file_too_large", "drawio_too_large");
});
