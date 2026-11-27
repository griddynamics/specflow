// The Box connection and the Box link source against the stub API (tests/support/stub-box.mjs): the production `BoxPort` and the sources port's check and
// add through the real `lib/api.ts`, the wire parsers and the error map. Port 3299 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHttpBox, toBoxConnection } from "@/lib/data/http/box";
import { createHttpProjects } from "@/lib/data/http/projects";
import { createHttpSources, toBoxCheck } from "@/lib/data/http/sources";
import { toSource } from "@/lib/data/http/sources-wire";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const ctx = serveStub(3299, (api) => {
  api.box.seed({ reset: true });
});
const { sent } = ctx;
const box = createHttpBox();
const changed: string[] = [];
const sources = createHttpSources({ changed: (pid) => changed.push(pid) });
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const FOLDER = "https://app.box.com/folder/12345";
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response");
};
const codeOf = async (run: () => Promise<unknown>) => {
  const e = await run().then(
    () => null,
    (error: unknown) => error,
  );
  assert.ok(isDataError(e), "a DataError");
  return e.detail?.apiCode;
};
async function project() {
  const p = await createProject.create({ name: "P" });
  sent.length = 0;
  changed.length = 0;
  return p.id;
}

// ---- the wire ----

const wire = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  connected: true,
  status: "active",
  name: "Dana Example",
  enterprise_name: "Acme Corp",
  connected_at: "2026-11-20T09:30:00Z",
  last_used_at: null,
  ...over,
});

test("a connection keeps enabled, status, the display name and the enterprise; a personal account has no enterprise", () => {
  assert.deepEqual(toBoxConnection(wire()), {
    enabled: true,
    connected: true,
    status: "active",
    name: "Dana Example",
    enterpriseName: "Acme Corp",
    connectedAt: "2026-11-20T09:30:00Z",
    lastUsedAt: null,
  });
  assert.equal(toBoxConnection(wire({ enterprise_name: null })).enterpriseName, null);
  assert.equal(toBoxConnection(wire({ status: "expired" })).status, "expired");
  const none = toBoxConnection(wire({ connected: false, status: null, name: null, enterprise_name: null, connected_at: null }));
  assert.deepEqual([none.connected, none.status, none.name], [false, null, null]);
});

test("any other shape of a connection is invalid_response", () => {
  for (const bad of [null, {}, wire({ status: "revoked" }), wire({ connected: false }), wire({ status: null }), wire({ name: 3 })]) {
    invalid(() => toBoxConnection(bad));
  }
});

const file = (over: Record<string, unknown> = {}) => ({ id: "4711", name: "Spec.pdf", path: "design", category: "pdf", ...over });
const check = (over: Record<string, unknown> = {}) => ({
  kind: "folder",
  name: "Project files",
  files: [file()],
  files_capped: false,
  skipped: 2,
  folders: 1,
  bytes: 2048,
  capped: false,
  ...over,
});

test("a check answer keeps the listed files and the counts; any other shape is invalid_response", () => {
  assert.deepEqual(toBoxCheck(check()), {
    kind: "folder",
    name: "Project files",
    files: [{ id: "4711", name: "Spec.pdf", path: "design", category: "pdf" }],
    filesCapped: false,
    skipped: 2,
    folders: 1,
    bytes: 2048,
    capped: false,
  });
  assert.equal(toBoxCheck(check({ files: [file({ category: "note" })] })).files[0]?.category, "note");
  for (const bad of [
    null,
    check({ kind: "page" }),
    check({ files: [file({ category: "video" })] }),
    check({ files: [file({ id: "" })] }),
    check({ files: [file({ path: null })] }),
    check({ files_capped: "no" }),
    check({ bytes: -1 }),
    check({ capped: "no" }),
    check({ name: 4 }),
    check({ files: undefined }),
  ]) {
    invalid(() => toBoxCheck(bad));
  }
});

test("a source keeps the link the API holds, for the node to open", () => {
  const w = {
    id: "s1",
    kind: "box",
    name: "Project files",
    name_suffix: 1,
    added_by: { id: "u1", display_name: "Alex" },
    created_at: "2026-11-25T09:00:00Z",
    listing_finished: true,
    open: false,
    total: 0,
    counts: { waiting: 0, processing: 0, done: 0, not_read: 0, skipped: 0, failed: 0, already_in_project: 0 },
    failure_reason: null,
    folder_id: null,
    link: FOLDER,
  };
  assert.deepEqual([toSource(w).kind, toSource(w).link], ["box", FOLDER]);
  assert.equal(toSource({ ...w, link: null }).link, null);
});

// ---- the connection routes ----

test("connection reads the caller's own connection, also when Box is not set up", async () => {
  const none = await box.connection();
  assert.deepEqual([none.enabled, none.connected, none.status], [true, false, null]);
  assert.equal(last().path, "/api/backend/integrations/box");
  ctx.api.box.seed({ connection: { status: "expired" } });
  assert.deepEqual([(await box.connection()).status, (await box.connection()).name], ["expired", "Dana Example"]);
  ctx.api.box.seed({ enabled: false });
  assert.equal((await box.connection()).enabled, false);
});

test("authorize posts the return path and answers a Box sign-in URL that asks for the one scope", async () => {
  const url = new URL(await box.authorize("/projects/p1/integrations?setup=box"));
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { return_to: "/projects/p1/integrations?setup=box" });
  assert.deepEqual([url.origin, url.searchParams.get("scope")], ["https://account.box.com", "root_readwrite"]);
  await rejects(box.authorize("https://evil.example.com"), "validation");
  ctx.api.box.seed({ enabled: false });
  await rejects(box.authorize("/x"), "unavailable", "box_unavailable");
});

test("disconnect forgets the connection", async () => {
  ctx.api.box.seed({ connection: { status: "active" } });
  await box.disconnect();
  assert.equal(last().method, "DELETE");
  assert.equal((await box.connection()).connected, false);
});

// ---- the source routes ----

test("check posts {link} and answers a folder: its name, the listed files, the skipped ones and the cap only when hit", async () => {
  const pid = await project();
  const found = await sources.checkBox(pid, FOLDER);
  assert.deepEqual(
    [found.kind, found.name, found.files.length, found.filesCapped, found.skipped, found.folders, found.bytes, found.capped],
    ["folder", "Project files", 12, false, 3, 2, 12 * 102400, false],
  );
  assert.deepEqual(found.files[2], { id: "1003", name: "File 3.pptx", path: "design", category: "powerpoint" });
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/box/check`);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: FOLDER });
  const big = await sources.checkBox(pid, "https://app.box.com/folder/9999");
  assert.deepEqual([big.files.length, big.filesCapped, big.capped, big.skipped], [1000, true, true, 20]);
  const one = await sources.checkBox(pid, "https://app.box.com/file/77");
  assert.deepEqual([one.kind, one.name, one.files.length, one.skipped, one.capped], ["file", "Spec.pdf", 1, 0, false]);
  assert.equal((await sources.checkBox(pid, "https://acme.app.box.com/s/abc")).name, "Shared folder");
  assert.equal(changed.length, 0, "a check changes nothing");
});

test("the closed codes reach the caller as the API's code: not a link, All Files, a password, no access", async () => {
  const pid = await project();
  const code = (link: string) => codeOf(() => sources.checkBox(pid, link));
  assert.equal(await code("not a link"), "box_link_invalid");
  assert.equal(await code("https://evil.example.com/folder/1"), "box_link_invalid");
  assert.equal(await code(`http://app.box.com/folder/1`), "box_link_invalid");
  assert.equal(await code("https://app.box.com/folder/0"), "box_link_invalid");
  assert.equal(await code("https://app.box.com/s/password"), "box_link_password");
  assert.equal(await code("https://app.box.com/folder/404"), "box_no_access");
  assert.equal(await code("https://app.box.com/file/403"), "box_no_access");
});

test("add posts the link with an empty exclude, tells the knowledge port the sources changed", async () => {
  const pid = await project();
  await sources.addBox(pid, FOLDER);
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/box`);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: FOLDER, exclude: [] });
  assert.deepEqual(changed, [pid]);
});

test("an add sends the unchecked ids as exclude, also ids the check did not list", async () => {
  const pid = await project();
  await sources.addBox(pid, FOLDER, ["1001"]);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: FOLDER, exclude: ["1001"] });
  await sources.addBox(pid, FOLDER, ["999999"]);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: FOLDER, exclude: ["999999"] });
  await rejects(sources.addBox(pid, FOLDER, ["id:1"]), "validation", "validation_failed");
});

test("a refused add still tells the port (the source may exist) and rejects with the code", async () => {
  const pid = await project();
  assert.equal(await codeOf(() => sources.addBox(pid, "https://app.box.com/folder/403")), "box_no_access");
  assert.deepEqual(changed, [pid]);
  await rejects(sources.checkBox(pid, ""), "validation", "box_link_invalid");
});
