// FR-GDUI-0002, 0009, 0012, 0017, 0018, 0021, NFR-SEC-0001, INT-GDUI-0002, INT-GDUI-0004 (GDRIVE plan 07; owner decisions
// 2026-10-19): the production sources and knowledge ports against the stub API (no Google, no model call, R11): every reason code,
// the SourceOut -> Connection map (save), the v2 status -> Source map, the Build knowledge lock, the Ready rule, polling, Local files
// as a batch (start, add, end), Q12. Faults are injected in the test's fetch layer. Ports 3258-3259.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { createHttpAdapter, type HttpPorts } from "@/lib/data/http";
import { deriveStatus, isReady, toStatus } from "@/lib/data/http/knowledge";
import { sourceState } from "@/lib/domain/source-state";
import { buildButtonView } from "@/components/mission-control/sources/kb-logic";
import { isDataError, type DataError } from "@/lib/domain/errors";
import type { DriveReason } from "@/lib/domain/sources";
import { jsonResponse, pdfFile, sleep, startEnv, stubCalls, textFile, waitFor, withHook, type Env } from "./p16-support";
import { defined } from "../support/defined";

const DRIVE_FOLDER = "fake-synthetic-30";
let env: Env;
before(async () => {
  env = await startEnv(3258, 3259);
  env.api.setTiming({ parseMs: 100, summarizeMs: 100, parseOutcome: "parsed" });
});
after(async () => {
  await env.stop();
});

const folder = (id: string) => `https://drive.google.com/drive/folders/${id}`;
const newProject = async (http: HttpPorts, name: string) => (await http.createProject.create({ name, contributorIds: [] })).id;
const REASONS: DriveReason[] = ["no_access", "not_a_folder", "drive_unavailable", "setup_error", "too_slow", "unknown_error"];
const envelope = (code: string) => JSON.stringify({ error: { code, message: "SERVER TEXT MUST NOT SHOW", details: {} } });
async function rejection(p: Promise<unknown>): Promise<DataError> {
  try {
    await p;
  } catch (e) {
    assert.ok(isDataError(e), `a DataError, got ${String(e)}`);
    return e;
  }
  assert.fail("expected a rejection");
}

test("FR-GDUI-0002: identity returns the Drive email; an unreadable one is a reason (the code, not the status)", async () => {
  const http = createHttpAdapter();
  assert.deepEqual(await http.sources.driveIdentity(), { ok: true, email: env.api.drive.email });
  env.api.drive.identityReason = "setup_error";
  try {
    assert.deepEqual(await http.sources.driveIdentity(), { ok: false, reason: "setup_error" });
  } finally {
    env.api.drive.identityReason = null;
  }
  await withHook(
    env,
    (x) => (x.path === "/api/backend/drive/identity" ? jsonResponse(200, '{"email":""}') : undefined),
    async () => {
      assert.deepEqual(await http.sources.driveIdentity(), { ok: false, reason: "unknown_error" });
    },
  );
});

test("FR-GDUI-0009, INT-GDUI-0002: Test connection maps the ok answer (snake_case -> camelCase, API order kept)", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Test ok");
  const r = await http.sources.testDrive(pid, folder(DRIVE_FOLDER));
  assert.deepEqual(r, {
    ok: true,
    folderId: DRIVE_FOLDER,
    folderName: "Recorded folder",
    folderCount: 3,
    fileCount: 11,
    categories: [
      { category: "google_docs", processingClass: "processed", count: 5 },
      { category: "pdf", processingClass: "processed", count: 3 },
      { category: "word", processingClass: "stored_only", count: 2 },
      { category: "shortcuts", processingClass: "skipped", count: 1 },
    ],
  });
  assert.equal((await http.sources.testDrive(pid, folder("fake-empty-folder"))).ok, true);
  const sent = env.sent.filter((s) => s.path === `/api/backend/projects/${pid}/sources/gdrive/test`);
  assert.equal(sent.length, 2);
  assert.equal(sent[0].headers["x-requested-with"], "specflow", "POST goes through lib/api.ts (CSRF header)");
  assert.deepEqual(JSON.parse(sent[0].body ?? "{}"), { link: folder(DRIVE_FOLDER) });
  assert.equal(stubCalls(env.api, pid, "parse").length + stubCalls(env.api, pid, "summarize").length, 0);
});

test("FR-GDUI-0012, INT-GDUI-0002: every reason code of Test connection and Save, in the stub's own answers", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Reasons");
  for (const reason of REASONS) {
    assert.deepEqual(await http.sources.testDrive(pid, folder(`fake-reason-${reason}`)), { ok: false, reason }, `test ${reason}`);
    assert.deepEqual(await http.sources.saveDrive(pid, folder(`fake-reason-${reason}`)), { ok: false, reason }, `save ${reason}`);
  }
  // invalid_link (a link the rules reject) and no_access (any other valid id, as a real 404), then already_connected.
  assert.deepEqual(await http.sources.testDrive(pid, "https://example.com/x"), { ok: false, reason: "invalid_link" });
  assert.deepEqual(await http.sources.saveDrive(pid, "not a link"), { ok: false, reason: "invalid_link" });
  assert.deepEqual(await http.sources.testDrive(pid, folder("some-other-folder-id")), { ok: false, reason: "no_access" });
  const saved = await http.sources.saveDrive(pid, folder("fake-synthetic-23"));
  assert.equal(saved.ok, true);
  assert.deepEqual(await http.sources.testDrive(pid, folder("fake-synthetic-23")), { ok: false, reason: "already_connected" });
  assert.deepEqual(await http.sources.saveDrive(pid, folder("fake-synthetic-23")), { ok: false, reason: "already_connected" });
  assert.equal(env.api.sources.get(pid)?.length, 1, "only the one good save created a source");
});

test("INT-GDUI-0002, FR-GDUI-0012.AC5, NFR-SEC-0001: the envelope code decides, never the HTTP status or the message; anything else is unknown_error", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Codes");
  const save = (x: { method: string; path: string }) => x.method === "POST" && x.path === `/api/backend/projects/${pid}/sources/gdrive`;
  const test = (x: { method: string; path: string }) =>
    x.method === "POST" && x.path === `/api/backend/projects/${pid}/sources/gdrive/test`;
  // A reason code on an unusual status still reads as that reason.
  await withHook(
    env,
    (x) => (save(x) ? jsonResponse(500, envelope("no_access")) : undefined),
    async () => {
      assert.deepEqual(await http.sources.saveDrive(pid, folder(DRIVE_FOLDER)), { ok: false, reason: "no_access" });
    },
  );
  // A known status with a non-reason code, a missing project, an expired sign-in, an ingress HTML page: unknown_error.
  for (const [status, body] of [
    [409, envelope("workflow_start_failed")],
    [404, envelope("project_not_found")],
    [401, envelope("unauthenticated")],
    [502, "<html>Bad Gateway</html>"],
    [200, "not json"],
    [200, "{}"],
    [201, "{}"],
    [201, '{"source":{"kind":"gdrive"}}'],
  ] as const) {
    await withHook(
      env,
      (x) => (save(x) ? jsonResponse(status, body) : undefined),
      async () => {
        assert.deepEqual(
          await http.sources.saveDrive(pid, folder(DRIVE_FOLDER)),
          { ok: false, reason: "unknown_error" },
          `save ${String(status)} ${body.slice(0, 20)}`,
        );
      },
    );
  }
  for (const body of [
    '{"result":"failed","reason":"bogus"}',
    '{"result":"ok"}',
    '{"result":"ok","folder":{"id":"x","name":"n"},"folder_count":1,"file_count":1,"categories":[{"category":"nope","processing_class":"processed","count":1}]}',
    "[]",
  ]) {
    await withHook(
      env,
      (x) => (test(x) ? jsonResponse(200, body) : undefined),
      async () => {
        assert.deepEqual(
          await http.sources.testDrive(pid, folder(DRIVE_FOLDER)),
          { ok: false, reason: "unknown_error" },
          `test ${body.slice(0, 30)}`,
        );
      },
    );
  }
  // Transport failure (the fetch rejects): a reason, not an exception.
  await withHook(
    env,
    (x) => {
      if (test(x) || save(x)) throw new TypeError("network down");
      return undefined;
    },
    async () => {
      assert.deepEqual(await http.sources.testDrive(pid, folder(DRIVE_FOLDER)), { ok: false, reason: "unknown_error" });
      assert.deepEqual(await http.sources.saveDrive(pid, folder(DRIVE_FOLDER)), { ok: false, reason: "unknown_error" });
    },
  );
  assert.equal(env.api.sources.get(pid)?.length ?? 0, 0, "the faults created nothing");
});

test("FR-GDUI-0017, FR-GDUI-0018: Save returns the Connection from SourceOut (canonical link as it comes, state map, progress), and tells subscribers", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Save");
  let notified = 0;
  const off = http.knowledge.subscribe(pid, () => notified++);
  try {
    const r = await http.sources.saveDrive(pid, `${folder(DRIVE_FOLDER)}?usp=sharing`);
    assert.ok(r.ok);
    const c = r.connection;
    assert.equal(c.kind, "gdrive");
    assert.equal(c.url, folder(DRIVE_FOLDER), "SourceOut.link, not the pasted text");
    assert.equal(c.label, "Recorded folder");
    assert.equal(c.state, "syncing", "listing = syncing");
    assert.deepEqual(c.importProgress, { listingFinished: false, total: 0, finished: 0 });
    assert.equal(c.failure, null);
    assert.equal(notified, 1, "the screen reads the status again");
    assert.equal(c.access, null);
  } finally {
    off();
  }
});

test("FR-GDUI-0018, DATA-SRC-0002: the status maps a Drive source to its state (importing = processing, done, failed + the reason); a stray reason reads unknown_error", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "States");
  for (const id of [DRIVE_FOLDER, "fake-synthetic-23", "fake-empty-folder", "fake-forms-only"])
    await http.sources.saveDrive(pid, folder(id));
  const rows = defined(env.api.sources.get(pid));
  Object.assign(rows[0], { state: "importing", progress: { listing_finished: true, total: 30, finished: 12 } });
  Object.assign(rows[1], {
    state: "done",
    last_synced_at: "2026-10-19T10:00:00Z",
    progress: { listing_finished: true, total: 23, finished: 23 },
  });
  Object.assign(rows[2], { state: "failed", failure_reason: "no_access", progress: { listing_finished: true, total: 10, finished: 10 } });
  Object.assign(rows[3], { state: "failed", failure_reason: "something_new", progress: { listing_finished: true, total: 2, finished: 2 } });
  const s = await http.knowledge.status(pid);
  assert.deepEqual(
    s.sources.map((c) => [c.kind, sourceState(c), c.failure, c.total, c.counts.done, c.listing]),
    [
      ["gdrive", "processing", null, 30, 12, false],
      ["gdrive", "done", null, 23, 23, false],
      ["gdrive", "failed", "no_access", 10, 10, false],
      ["gdrive", "failed", "unknown_error", 2, 2, false],
    ],
  );
  assert.deepEqual(s.progress, { listingFinished: true, total: 65, finished: 47 });
  assert.equal(s.attention.errors, 2);
});

/** A v2 `SourceSummaryOut` (sources-ux SPECS §4.1) with `done`/`waiting` counts, for the pure derive tests. */
const summary = (over: Record<string, unknown> = {}, counts: Record<string, number> = {}) => {
  const c = { waiting: 0, processing: 0, done: 0, not_read: 0, skipped: 0, failed: 0, already_in_project: 0, ...counts };
  return {
    id: "s1",
    kind: "gdrive",
    name: "F",
    name_suffix: 1,
    added_by: { id: "u1", display_name: "dev" },
    created_at: "2026-10-19T09:00:00Z",
    listing_finished: true,
    open: false,
    total: Object.values(c).reduce((n, x) => n + x, 0),
    counts: c,
    failure_reason: null,
    folder_id: "f",
    link: folder("f"),
    ...over,
  };
};
const body = (sources: unknown[], extra: Record<string, unknown> = {}) => {
  const all = sources as ReturnType<typeof summary>[];
  const total = all.reduce((n, s) => n + s.total, 0);
  const open = all.reduce((n, s) => n + s.counts.waiting + s.counts.processing, 0);
  return {
    progress: { listing_finished: all.every((s) => s.listing_finished), total, finished: total - open },
    processed_documents: 4,
    removed_files: 0,
    sources,
    ...extra,
  };
};

test("FR-GDUI-0021, INT-GDUI-0004: the lock follows the project progress (listing, then finished < total); an ended or failed import unlocks", () => {
  const lean = (over: Record<string, unknown>, counts: Record<string, number>, extra: Record<string, unknown> = {}) =>
    deriveStatus(toStatus(body([summary(over, counts)], extra)));
  const listing = lean({ listing_finished: false }, {});
  assert.equal(buildButtonView(listing).mode, null);
  assert.match(buildButtonView(listing).detail, /^Build knowledge starts after all files are imported\. Listing files/);
  const part = lean({}, { waiting: 18, done: 12 });
  assert.equal(buildButtonView(part).mode, null);
  assert.match(buildButtonView(part).detail, /12 of 30 files imported/);
  assert.equal(part.phase, "collecting");
  const done = lean({}, { done: 30 });
  const v = buildButtonView(done);
  assert.deepEqual([v.label, v.mode], ["Build knowledge", "first"], "unlocked: a click is possible (and says 'not available yet')");
  assert.equal(done.ready, false, "no knowledge build exists in production (K1)");
  // Stored-only or skipped files only: unlocked, but nothing to build.
  const none = lean({}, { skipped: 5 }, { processed_documents: 0 });
  assert.equal(buildButtonView(none).mode, null);
  assert.equal(buildButtonView(none).detail, "No documents to build yet.");
});

test("FR-SRC-0006, FR-SRC-0019: a Local files batch is a source in added order; its counts feed the lock, the attention numbers and the removed counter", () => {
  const s = deriveStatus(
    toStatus(
      body(
        [
          summary({ id: "d", created_at: "2026-10-19T09:00:00Z" }, { done: 3 }),
          summary(
            { id: "b", kind: "upload", name: "Local files", open: true, folder_id: null, link: null },
            { done: 2, failed: 1, processing: 1 },
          ),
        ],
        { processed_documents: 5, removed_files: 7 },
      ),
    ),
  );
  assert.deepEqual(
    s.sources.map((x) => [x.id, x.kind, x.open]),
    [
      ["d", "gdrive", false],
      ["b", "upload", true],
    ],
  );
  assert.deepEqual(s.progress, { listingFinished: true, total: 7, finished: 6 });
  assert.equal(s.totals.pending, 1);
  assert.equal(buildButtonView(s).mode, null);
  assert.equal(s.attention.failedItems, 1);
  assert.equal(s.changes.removed, 7, "removed_files");
  assert.equal(s.phase, "collecting");
});

test("Ready rule (owner 2026-10-19): Plan and Decisions unlock with at least one processed document, from any source", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Ready");
  assert.deepEqual(await http.missionControl.snapshot(pid), { phase: "empty" }, "nothing yet");
  await http.sources.saveDrive(pid, folder(DRIVE_FOLDER));
  assert.deepEqual(await http.missionControl.snapshot(pid), { phase: "empty" }, "a source that still lists is not Ready");
  await http.sources.upload(pid, [pdfFile("req.pdf", 2048)]);
  assert.deepEqual(await http.missionControl.snapshot(pid), { phase: "empty" }, "the upload is still being parsed");
  await waitFor("the stub parsed the upload", () => [...env.api.docs.values()].every((d) => d.pid !== pid || d.row.status === "parsed"));
  assert.deepEqual(await http.missionControl.snapshot(pid), { phase: "ready" });
  assert.equal(isReady(toStatus(await (await fetch(`/api/backend/projects/${pid}/sources?batches=1`)).json())), true);
  // A failed parse does not count.
  const failing = await newProject(http, "Failed parse");
  env.api.setTiming({ parseOutcome: "failed" });
  try {
    await http.sources.upload(failing, [pdfFile("bad.pdf", 1024)]);
    await waitFor("failed", () => [...env.api.docs.values()].some((d) => d.pid === failing && d.row.status === "failed"));
    assert.deepEqual(await http.missionControl.snapshot(failing), { phase: "empty" });
  } finally {
    env.api.setTiming({ parseOutcome: "parsed" });
  }
  assert.equal(
    stubCalls(env.api, pid, "parse").length + stubCalls(env.api, pid, "summarize").length,
    0,
    "no browser-driven parse or summarize (K5)",
  );
  // A failed read blocks the gate (P21-2): it rejects.
  env.setLoggedIn(false);
  try {
    assert.equal((await rejection(http.missionControl.snapshot(pid))).code, "unauthenticated");
  } finally {
    env.setLoggedIn(true);
  }
});

test("Q12, K1: build never calls the API; after the unlock a click is DataError not_available (the screen shows the existing message)", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Build");
  const n = env.api.log.length;
  assert.equal(http.knowledge.buildAvailable, false, 'the screen shows "Not available yet" (no "demo" wording)');
  assert.equal(http.sources.pdfOnly, true);
  assert.equal((await rejection(http.knowledge.build(pid, "first"))).code, "not_available");
  assert.equal((await rejection(http.knowledge.build(pid, "update"))).code, "not_available");
  assert.equal(env.api.log.length, n, "no request at all, in particular no summarize");
  assert.equal(http.sources.githubEnabled, false, "K3: GitHub is hidden");
  assert.equal((await rejection(http.sources.add(pid, { kind: "github", url: "https://github.com/a/b" }))).code, "not_available");
});

test("FR-SRC-0020, FR-SRC-0022: Add files reports each outcome: added, read again (a failed file sent again moves), already in the project", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Re-upload");
  const file = pdfFile("retry.pdf", 1500);
  env.api.setTiming({ parseOutcome: "failed" });
  let first;
  try {
    first = await http.sources.upload(pid, [file]);
    assert.deepEqual(
      { ...first, sourceId: null },
      { sourceId: null, picked: 1, added: 1, alreadyInProject: 0, readAgain: 0, duplicates: 0, notAdded: 0 },
    );
    assert.ok(first.sourceId !== null);
    await waitFor("failed", () => [...env.api.docs.values()].some((d) => d.pid === pid && d.row.status === "failed"));
  } finally {
    env.api.setTiming({ parseOutcome: "parsed" });
  }
  const second = await http.sources.upload(pid, [file]);
  assert.deepEqual(
    { ...second, sourceId: null },
    { sourceId: null, picked: 1, added: 0, alreadyInProject: 0, readAgain: 1, duplicates: 0, notAdded: 0 },
  );
  assert.notEqual(second.sourceId, first.sourceId);
  assert.deepEqual(
    (await http.knowledge.status(pid)).sources.map((x) => x.id),
    [second.sourceId],
    "the emptied first batch is gone",
  );
  await waitFor("parsed", () => [...env.api.docs.values()].some((d) => d.pid === pid && d.row.status === "parsed"));
  const third = await http.sources.upload(pid, [file]);
  assert.deepEqual(
    { ...third, sourceId: null },
    { sourceId: null, picked: 1, added: 0, alreadyInProject: 1, readAgain: 0, duplicates: 0, notAdded: 0 },
  );
  const page = await http.sources.files(pid, defined(third.sourceId));
  assert.deepEqual(
    page.items.map((f) => [f.state, f.reason, f.holder?.id]),
    [["already_in_project", "already_in_project", second.sourceId]],
  );
  assert.equal([...env.api.docs.values()].filter((d) => d.pid === pid).length, 1, "one document for the same content");
  assert.equal(stubCalls(env.api, pid, "parse").length, 0, "the server starts the parse again, not the browser (K5)");
});

test("INT-SRC-0004, FR-SRC-0020: Local files = start, one request per file, end; a refused file does not stop the others; the screen is told after each step", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Local files");
  let notified = 0;
  const off = http.knowledge.subscribe(pid, () => notified++);
  try {
    const a = pdfFile("a.pdf", 1000);
    const report = await http.sources.upload(pid, [a, textFile("notes.txt"), pdfFile("b.pdf", 2000), a]);
    assert.deepEqual(
      { ...report, sourceId: null },
      { sourceId: null, picked: 4, added: 2, alreadyInProject: 0, readAgain: 0, duplicates: 1, notAdded: 1 },
    );
    const mine = env.api.log.filter((e) => e.path.split("?")[0].startsWith(`/api/v1/projects/${pid}/sources`));
    assert.deepEqual(
      mine.map((e) => {
        const sub = e.path.split("?")[0].replace(`/api/v1/projects/${pid}/sources`, "").replace(defined(report.sourceId), ":sid");
        return `${e.method} ${sub}`;
      }),
      ["POST /uploads", "POST /:sid/files", "POST /:sid/files", "POST /:sid/files", "POST /:sid/files", "POST /:sid/end"],
    );
    assert.deepEqual(JSON.parse(env.sent.find((s) => s.path === `/api/backend/projects/${pid}/sources/uploads`)?.body ?? "{}"), {
      expected_files: 4,
    });
    assert.ok(mine.filter((e) => e.path.includes("/files")).every((u) => u.contentType === "application/pdf"));
    assert.equal(notified, 6, "start, four adds, end");
    await waitFor("parsed", () => [...env.api.docs.values()].filter((d) => d.pid === pid).every((d) => d.row.status === "parsed"));
    const s = await http.knowledge.status(pid);
    assert.deepEqual(
      s.sources.map((x) => [x.kind, x.open, x.total, x.counts.done]),
      [["upload", false, 2, 2]],
      "the duplicate adds nothing",
    );
    assert.equal(s.totals.documents, 2);
    assert.equal(buildButtonView(s).mode, "first");
    assert.equal(stubCalls(env.api, pid, "parse").length, 0);
    assert.equal(stubCalls(env.api, pid, "upload").length, 0, "the legacy upload endpoint is not used");
  } finally {
    off();
  }
});

test("FR-GDUI-0018.AC3, FR-SRC-0012: the status is read again every pollMs while a screen listens and the import runs, every idleMs when idle; it stops when unsubscribed", async () => {
  const http = createHttpAdapter({ pollMs: 40, idleMs: 120 });
  const pid = await newProject(http, "Poll");
  await http.sources.saveDrive(pid, folder(DRIVE_FOLDER));
  const gets = () => env.sent.filter((s) => s.method === "GET" && s.path === `/api/backend/projects/${pid}/sources?batches=1`).length;
  // The screen's pattern: the listener reads the status again.
  let reads = 0;
  const load = () =>
    void http.knowledge.status(pid).then(
      () => reads++,
      () => undefined,
    );
  const off = http.knowledge.subscribe(pid, load);
  load();
  await waitFor("three reads while the import runs", () => reads >= 3, 5000);
  // Finish the import: the next read is the last one.
  Object.assign(defined(env.api.sources.get(pid))[0], { state: "done", progress: { listing_finished: true, total: 2, finished: 2 } });
  await waitFor("the read after the change", () => reads >= 4, 5000);
  // R13: idle, the read still repeats (every idleMs), only slower than while the import runs.
  const settled = gets();
  await waitFor("an idle read", () => gets() > settled, 5000);
  // A restarted import polls again; unsubscribing stops it.
  Object.assign(defined(env.api.sources.get(pid))[0], { state: "importing", progress: { listing_finished: true, total: 4, finished: 1 } });
  load();
  await waitFor("polling again", () => gets() >= settled + 3, 5000);
  off();
  await sleep(120);
  const after = gets();
  await sleep(200);
  assert.equal(gets(), after, "no polling after the screen left");
  // Failed reads are retried while the screen listens, with a growing delay (F3: x2 each time, capped at 10 s).
  const off2 = http.knowledge.subscribe(pid, load);
  const stamps: number[] = [];
  await withHook(
    env,
    (x) =>
      x.method === "GET" && x.path === `/api/backend/projects/${pid}/sources?batches=1`
        ? (stamps.push(Date.now()), jsonResponse(502, "<html>bad gateway</html>"))
        : undefined,
    async () => {
      load();
      await waitFor("four attempts", () => stamps.length >= 4, 8000);
    },
  );
  off2();
  http.knowledge.leave(pid);
  const gaps = stamps.slice(1).map((t, i) => t - stamps[i]);
  assert.ok(gaps[1] > gaps[0] * 1.5 && gaps[2] > gaps[1] * 1.5, `back-off ${gaps.join(", ")} ms`);
  // A lost sign-in (401) is not retried.
  const off3 = http.knowledge.subscribe(pid, load);
  env.setLoggedIn(false);
  const before = gets();
  try {
    load();
    await sleep(400);
  } finally {
    env.setLoggedIn(true);
    off3();
    http.knowledge.leave(pid);
  }
  assert.equal(gets() - before, 1, "one attempt, no retry after unauthenticated");
});

test("FR-GDUI-0018.AC3, FR-SRC-0007, FR-SRC-0012: the default poll interval is 3 s and the idle one 5 s, both at most 10 s", async () => {
  const { DEFAULT_POLL_MS, DEFAULT_IDLE_MS } = await import("@/lib/data/http/knowledge");
  assert.deepEqual([DEFAULT_POLL_MS, DEFAULT_IDLE_MS], [3_000, 5_000]);
});

test("FE-BP-02 (project list): Mission Control's reading shows in the switcher status (ready / processing / empty)", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Switcher");
  await http.knowledge.status(pid);
  assert.equal((await http.projects.get(pid)).status, "empty");
  await http.sources.saveDrive(pid, folder(DRIVE_FOLDER));
  await http.knowledge.status(pid);
  assert.equal((await http.projects.get(pid)).status, "processing");
  await http.sources.upload(pid, [pdfFile("ok.pdf", 800)]);
  await waitFor("parsed", () => [...env.api.docs.values()].filter((d) => d.pid === pid).every((d) => d.row.status === "parsed"));
  await http.knowledge.status(pid);
  assert.equal((await http.projects.get(pid)).status, "ready");
});
