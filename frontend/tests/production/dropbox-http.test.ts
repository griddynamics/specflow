// The Dropbox connection and the Dropbox link source against the stub API (tests/support/stub-dropbox.mjs): the wire parsers (`toDropboxConnection`,
// `toDropboxCheck`) and the check and add through the real `lib/api.ts`. S3 extends it. Port 3288 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHttpDropbox, toDropboxConnection } from "@/lib/data/http/dropbox";
import { createHttpProjects } from "@/lib/data/http/projects";
import { createHttpSources, toDropboxCheck } from "@/lib/data/http/sources";
import { isDataError } from "@/lib/domain/errors";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const ctx = serveStub(3288, (api) => {
  api.dropbox.seed({ reset: true });
});
const { sent } = ctx;
const dropbox = createHttpDropbox();
const sources = createHttpSources({ changed: () => undefined });
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response");
};

const wire = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  connected: true,
  status: "active",
  name: "Dana Example",
  team_name: "Acme Team",
  connected_at: "2026-11-20T09:30:00Z",
  last_used_at: null,
  ...over,
});

const file = (over: Record<string, unknown> = {}) => ({ id: "id:abc", name: "Spec.pdf", path: "design", category: "pdf", ...over });
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

test("a connection keeps enabled, status, the display name and the team; a personal account has no team", () => {
  assert.deepEqual(toDropboxConnection(wire()), {
    enabled: true,
    connected: true,
    status: "active",
    name: "Dana Example",
    teamName: "Acme Team",
    connectedAt: "2026-11-20T09:30:00Z",
    lastUsedAt: null,
  });
  assert.equal(toDropboxConnection(wire({ team_name: null })).teamName, null);
  for (const bad of [null, {}, wire({ status: "revoked" }), wire({ connected: false }), wire({ name: 3 })]) {
    invalid(() => toDropboxConnection(bad));
  }
});

test("a check answer keeps the listed files and the counts; any other shape is invalid_response", () => {
  assert.deepEqual(toDropboxCheck(check()), {
    kind: "folder",
    name: "Project files",
    files: [{ id: "id:abc", name: "Spec.pdf", path: "design", category: "pdf" }],
    filesCapped: false,
    skipped: 2,
    folders: 1,
    bytes: 2048,
    capped: false,
  });
  for (const bad of [
    null,
    check({ kind: "link" }),
    check({ files: [file({ category: "video" })] }),
    check({ files: [file({ id: "" })] }),
    check({ files: [file({ path: null })] }),
    check({ files_capped: "no" }),
    check({ bytes: -1 }),
    check({ files: undefined }),
  ]) {
    invalid(() => toDropboxCheck(bad));
  }
});

test("the connection, the sign-in URL (https on dropbox.com) and the disconnect go through the API", async () => {
  assert.equal((await dropbox.connection()).connected, false);
  const url = await dropbox.authorize("/projects/p1/integrations");
  assert.equal(new URL(url).hostname, "www.dropbox.com");
  assert.deepEqual(JSON.parse(String(last().body)), { return_to: "/projects/p1/integrations" });
  ctx.api.dropbox.state.connection = { status: "active", name: "Dana Example", team_name: null };
  assert.equal((await dropbox.connection()).status, "active");
  await dropbox.disconnect();
  assert.deepEqual([last().method, last().path], ["DELETE", "/api/backend/integrations/dropbox"]);
});

test("a check lists the files; an add sends the unchecked ids as exclude; a refusal carries the API code", async () => {
  const pid = (await createProject.create({ name: "P" })).id;
  const link = "https://www.dropbox.com/home/Projects/Specs";
  const found = await sources.checkDropbox(pid, link);
  assert.equal(found.files.length, 12);
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/dropbox/check`);
  await sources.addDropbox(pid, link, ["id:file1"]);
  assert.deepEqual(JSON.parse(String(last().body)), { link, exclude: ["id:file1"] });
  await rejects(sources.checkDropbox(pid, "https://www.dropbox.com/home"), "validation", "dropbox_link_invalid");
  await rejects(sources.checkDropbox(pid, "https://www.dropbox.com/home/x/403"), "conflict", "dropbox_no_access");
});

test("the stub's reserved links: root, no access, a shared file, a capped folder and a password link", async () => {
  const pid = (await createProject.create({ name: "P2" })).id;
  const at = (path: string) => sources.checkDropbox(pid, `https://www.dropbox.com${path}`);
  const file = await at("/scl/fi/abc/Spec.pdf");
  assert.deepEqual([file.kind, file.name, file.files.length], ["file", "Spec.pdf", 1]);
  const big = await at("/home/Archive/big");
  assert.deepEqual([big.files.length, big.filesCapped, big.capped, big.skipped], [1000, true, true, 20]);
  assert.equal((await at("/scl/fo/abc/Shared")).files.length, 5);
  await rejects(at("/scl/fo/password/Shared"), "validation", "dropbox_link_invalid");
  await rejects(at("/scl/fi/noaccess/Spec.pdf"), "conflict", "dropbox_no_access");
  await rejects(sources.checkDropbox(pid, "ht" + "tp://www.dropbox.com/home/x"), "validation", "dropbox_link_invalid");
});

test("an add with an id the check did not list leaves the rest alone; an add without exclude sends an empty list", async () => {
  const pid = (await createProject.create({ name: "P3" })).id;
  const link = "https://www.dropbox.com/home/Specs";
  await sources.addDropbox(pid, link);
  assert.deepEqual(JSON.parse(String(last().body)), { link, exclude: [] });
  await sources.addDropbox(pid, link, ["id:not-listed"]);
  assert.deepEqual(JSON.parse(String(last().body)), { link, exclude: ["id:not-listed"] });
});
