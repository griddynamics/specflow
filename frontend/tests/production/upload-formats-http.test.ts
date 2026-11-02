// Local files against the stub API through the real lib/api.ts and BFF: each accepted type reaches the API with
// its canonical Content-Type (also when the browser says something else), a legacy type is refused, and a file over its cap comes
// back as `tooLarge` with the API's cap. The stub wants the canonical type exactly. Ports 3254-3255.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { createHttpAdapter } from "@/lib/data/http";
import { createHttpDocuments } from "@/lib/data/http/documents";
import { contentTypeFor, UPLOAD_FORMATS } from "@/lib/domain/upload-formats";
import { documentFile, startEnv, stubCalls, type Env } from "./p16-support";
import { defined } from "../support/defined";

let env: Env;
before(async () => {
  env = await startEnv(3254, 3255);
  env.api.setTiming({ parseMs: 50, summarizeMs: 50, parseOutcome: "parsed" });
});
after(async () => {
  await env.stop();
});

const MB = 1024 * 1024;
// What browsers say, per extension: Windows Chrome sends application/vnd.ms-excel for .csv, every browser "" for .md.
const BROWSER_TYPE: Record<string, string> = { ".md": "", ".csv": "application/vnd.ms-excel", ".html": "text/html" };

const filesOf = (pid: string) =>
  env.api.log.filter((e) => e.method === "POST" && e.path.startsWith(`/api/v1/projects/${pid}/sources/`) && e.path.includes("/files?"));

test("each accepted type is added to a Local files batch with its canonical Content-Type, whatever the browser said", async () => {
  const http = createHttpAdapter();
  const pid = (await http.createProject.create({ name: "All types", contributorIds: [] })).id;
  const picked = UPLOAD_FORMATS.map((f) => documentFile(`doc${f.extension}`, BROWSER_TYPE[f.extension] ?? f.mediaType));
  const report = await http.sources.upload(pid, picked);
  assert.equal(report.added, 7, "all seven are added");
  assert.equal(report.notAdded, 0);
  assert.equal(report.tooLarge, undefined);
  assert.deepEqual(
    filesOf(pid).map((e) => [new URLSearchParams(e.path.split("?")[1]).get("filename"), e.contentType]),
    UPLOAD_FORMATS.map((f) => [`doc${f.extension}`, f.mediaType]),
  );
});

test("the single-document upload sends the canonical type too (a .md the browser called empty)", async () => {
  const http = createHttpAdapter();
  const docs = createHttpDocuments();
  const pid = (await http.createProject.create({ name: "Docs md", contributorIds: [] })).id;
  const r = await docs.upload(pid, documentFile("notes.md", ""));
  assert.equal(r.outcome, "created");
  assert.equal(defined(stubCalls(env.api, pid, "upload").at(-1)).contentType, contentTypeFor("notes.md"));
});

test("a legacy type that reaches the port is refused by the API (415), counted as not added, and the others stay", async () => {
  const http = createHttpAdapter();
  const pid = (await http.createProject.create({ name: "Legacy", contributorIds: [] })).id;
  const legacy = new File([new Uint8Array([0xd0, 0xcf, 0x11, 0xe0])], "old.doc", { type: "application/msword" });
  const report = await http.sources.upload(pid, [legacy, documentFile("new.docx", "")]);
  assert.deepEqual([report.added, report.notAdded, report.tooLarge], [1, 1, undefined]);
  assert.equal(defined(filesOf(pid)[0]).contentType, "application/octet-stream", "an unaccepted name gets no canonical type");
});

test("a file over its cap is refused 413 with the cap of its type; the other files are still added", async () => {
  const http = createHttpAdapter();
  const pid = (await http.createProject.create({ name: "Too large", contributorIds: [] })).id;
  const big = documentFile("big.csv", "text/csv", 10 * MB); // the head is a few bytes: over the 10 MB cap
  const report = await http.sources.upload(pid, [big, documentFile("ok.md", "")]);
  assert.deepEqual([report.added, report.notAdded], [1, 1]);
  assert.deepEqual(report.tooLarge, [{ name: "big.csv", maxBytes: 10 * MB }]);
});
