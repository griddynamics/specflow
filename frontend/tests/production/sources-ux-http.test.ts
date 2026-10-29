// INT-SRC-0001..0004, FR-SRC-0007/0012/0019/0020/0024/0025, NFR-SEC-0002 (sources-ux SPECS §7.3, §8, §9): the production sources port against
// the stub API through the real BFF. Strict v2 shapes, the legacy body of an old API, removal (204, `source_not_found`, the old API's
// `not_found`), the paged file list, a stopped upload, the 3 s / 5 s polling and `changed`. Ports 3252-3253.
import { test, before, after, mock } from "node:test";
import assert from "node:assert/strict";
import { createHttpAdapter, type HttpPorts } from "@/lib/data/http";
import { isDataError, type DataError } from "@/lib/domain/errors";
import { jsonResponse, pdfFile, startEnv, withHook, type Env } from "./p16-support";

let env: Env;
before(async () => {
  env = await startEnv(3252, 3253);
  env.api.setTiming({ parseMs: 100, summarizeMs: 100, parseOutcome: "parsed" });
});
after(async () => {
  await env.stop();
});

const newProject = async (http: HttpPorts, name: string) => (await http.createProject.create({ name, contributorIds: [] })).id;
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
const statusPath = (pid: string) => `/api/backend/projects/${pid}/sources?batches=1`;
const isStatus = (pid: string) => (x: { method: string; path: string }) => x.method === "GET" && x.path === statusPath(pid);
const flush = async (n = 40) => {
  for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r));
};

const LEGACY = JSON.stringify({
  progress: { listing_finished: true, total: 0, finished: 0 },
  sources: [],
  uploads: null,
  processed_documents: 3,
});
const COUNTS = { waiting: 0, processing: 0, done: 0, not_read: 0, skipped: 0, failed: 0, already_in_project: 0 };
const wireSrc = (over: Record<string, unknown>) => ({
  id: "s",
  kind: "gdrive",
  name: "F",
  name_suffix: 1,
  added_by: { id: "u", display_name: "d" },
  created_at: "2026-10-21T10:00:00Z",
  listing_finished: true,
  open: false,
  total: 0,
  counts: COUNTS,
  failure_reason: null,
  folder_id: null,
  link: null,
  ...over,
});
const v2 = (over: Record<string, unknown> = {}) =>
  JSON.stringify({
    progress: { listing_finished: true, total: 0, finished: 0 },
    processed_documents: 0,
    removed_files: 0,
    sources: [],
    ...over,
  });

test("INT-SRC-0001: the status is read with ?batches=1 and carries the sources in added order", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Status v2");
  const a = env.api.model.seed(pid, {
    kind: "gdrive",
    name: "Folder A",
    files: [
      { name: "x.pdf", state: "done" },
      { name: "y.pdf", state: "waiting" },
    ],
  });
  const b = env.api.model.seed(pid, { kind: "upload", files: [{ name: "z.pdf", state: "failed", reason: "read_failed" }] });
  const s = await http.knowledge.status(pid);
  assert.ok(
    env.sent.some((x) => isStatus(pid)(x)),
    "GET sources?batches=1",
  );
  assert.deepEqual(
    s.sources.map((x) => [x.id, x.kind, x.total]),
    [
      [a, "gdrive", 2],
      [b, "upload", 1],
    ],
  );
  assert.deepEqual(s.progress, { listingFinished: true, total: 3, finished: 2 });
  assert.deepEqual(s.sources[0].addedBy, { id: "11111111-1111-4111-8111-111111111111", name: "dev" });
  assert.equal(s.changes.removed, 0);
});

test("INT-SRC-0001: a body that is not exactly the v2 shape is invalid_response (status), including the legacy body of an old API", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Strict");
  const src = wireSrc;
  const bad: [string, string][] = [
    ["legacy body", LEGACY],
    [
      "no removed_files",
      JSON.stringify({ progress: { listing_finished: true, total: 0, finished: 0 }, processed_documents: 0, sources: [] }),
    ],
    ["a count missing", v2({ sources: [src({ counts: { waiting: 0 } })] })],
    ["negative count", v2({ sources: [src({ counts: { ...COUNTS, done: -1 }, total: -1 })] })],
    ["name_suffix 0", v2({ sources: [src({ name_suffix: 0 })] })],
    ["added_by is a string", v2({ sources: [src({ added_by: "dev" })] })],
    [
      "a legacy SourceOut row",
      v2({
        sources: [
          {
            id: "s",
            kind: "gdrive",
            folder_id: "f",
            link: "https://drive.google.com/drive/folders/f",
            name: "F",
            state: "done",
            connected_by: "u",
            created_at: "t",
            progress: { listing_finished: true, total: 1, finished: 1 },
          },
        ],
      }),
    ],
  ];
  for (const [what, body] of bad) {
    await withHook(
      env,
      (x) => (isStatus(pid)(x) ? jsonResponse(200, body) : undefined),
      async () => {
        assert.equal((await rejection(http.knowledge.status(pid))).code, "invalid_response", what);
      },
    );
  }
  // A failed first read is an error (no status was shown yet).
  await withHook(
    env,
    (x) => (isStatus(pid)(x) ? jsonResponse(502, "<html>bad gateway</html>") : undefined),
    async () => {
      assert.equal((await rejection(http.knowledge.status(pid))).code, "unavailable");
    },
  );
});

test("FR-SRC-0025.AC2, SPECS §9: the Ready gate reads the legacy body, and a failed or legacy read after a shown status keeps the last status [R25]", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Keep last");
  env.api.model.seed(pid, { kind: "upload", files: [{ name: "a.pdf", state: "done" }] });
  await withHook(
    env,
    (x) => (isStatus(pid)(x) ? jsonResponse(200, LEGACY) : undefined),
    async () => {
      assert.deepEqual(await http.missionControl.snapshot(pid), { phase: "ready" }, "processed_documents = 3 is read from the legacy body");
    },
  );
  const good = await http.knowledge.status(pid);
  assert.equal(good.sources.length, 1);
  await withHook(
    env,
    (x) => (isStatus(pid)(x) ? jsonResponse(200, LEGACY) : undefined),
    async () => {
      assert.equal(await http.knowledge.status(pid), good, "the first legacy body");
      assert.equal(await http.knowledge.status(pid), good, "and the second");
    },
  );
  await withHook(
    env,
    (x) => (isStatus(pid)(x) ? jsonResponse(502, "<html>bad gateway</html>") : undefined),
    async () => {
      assert.equal(await http.knowledge.status(pid), good, "a 502");
    },
  );
  await withHook(
    env,
    (x) => {
      if (isStatus(pid)(x)) throw new TypeError("offline");
      return undefined;
    },
    async () => {
      assert.equal(await http.knowledge.status(pid), good, "a network failure");
    },
  );
  // A lost sign-in is still an error: the screen must go to /login.
  env.setLoggedIn(false);
  try {
    assert.equal((await rejection(http.knowledge.status(pid))).code, "unauthenticated");
  } finally {
    env.setLoggedIn(true);
  }
  http.knowledge.leave(pid);
  await withHook(
    env,
    (x) => (isStatus(pid)(x) ? jsonResponse(502, "x") : undefined),
    async () => {
      assert.equal(
        (await rejection(http.knowledge.status(pid))).code,
        "unavailable",
        "after the screen left, a failed first read is an error again",
      );
    },
  );
});

test("INT-SRC-0003, FR-SRC-0018/0019: remove succeeds for 204; a gone source rejects not_found with apiCode source_not_found; the old API's plain not_found is NOT that", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Remove");
  const sid = env.api.model.seed(pid, {
    kind: "upload",
    files: [
      { name: "a.pdf", state: "done" },
      { name: "b.pdf", state: "done" },
    ],
  });
  const other = await newProject(http, "Other project");
  let notified = 0;
  const off = http.knowledge.subscribe(pid, () => notified++);
  try {
    assert.equal((await rejection(http.sources.remove(other, sid))).detail?.apiCode, "source_not_found", "another project's source id");
    assert.equal((await http.knowledge.status(pid)).sources.length, 1, "the other project's call changed nothing");
    notified = 0;
    await http.sources.remove(pid, sid);
    assert.equal(notified, 1, "the screen reads the status again");
    const del = env.sent.filter((x) => x.method === "DELETE" && x.path === `/api/backend/projects/${pid}/sources/${sid}`);
    assert.equal(del.length, 1);
    assert.equal(del[0].headers["x-requested-with"], "specflow");
    const s = await http.knowledge.status(pid);
    assert.deepEqual([s.sources.length, s.changes.removed], [0, 2], "removed_files counts the files of the removed source");
    const gone = await rejection(http.sources.remove(pid, sid));
    assert.deepEqual([gone.code, gone.retryable, gone.detail?.apiCode], ["not_found", false, "source_not_found"]);
    // An old API answers an unknown route with the plain `not_found`: a failure, not a removal.
    await withHook(
      env,
      (x) => (x.method === "DELETE" ? jsonResponse(404, envelope("not_found")) : undefined),
      async () => {
        const old = await rejection(http.sources.remove(pid, sid));
        assert.equal(old.code, "not_found");
        assert.equal(old.detail?.apiCode, "not_found");
        assert.notEqual(old.detail.apiCode, "source_not_found");
      },
    );
    await withHook(
      env,
      (x) => (x.method === "DELETE" ? jsonResponse(503, envelope("removal_unavailable")) : undefined),
      async () => {
        const e = await rejection(http.sources.remove(pid, sid));
        assert.deepEqual([e.code, e.retryable, e.detail?.apiCode], ["unavailable", true, "removal_unavailable"]);
      },
    );
    // Only 204 is a removal.
    await withHook(
      env,
      (x) => (x.method === "DELETE" ? jsonResponse(200, "{}") : undefined),
      async () => {
        assert.equal((await rejection(http.sources.remove(pid, sid))).code, "invalid_response");
      },
    );
  } finally {
    off();
  }
});

test("INT-SRC-0002: files pages in group order (problems, Processing, Waiting, Done) of 200; a bad cursor and an unknown source reject", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Pages");
  const files = [
    ...Array.from({ length: 120 }, (_, i) => ({ name: `done-${String(i).padStart(3, "0")}.pdf`, state: "done" })),
    ...Array.from({ length: 150 }, (_, i) => ({ name: `wait-${String(i).padStart(3, "0")}.pdf`, state: "waiting" })),
    ...Array.from({ length: 100 }, (_, i) => ({ name: `proc-${String(i).padStart(3, "0")}.pdf`, state: "processing" })),
    ...Array.from({ length: 50 }, (_, i) => ({
      name: `bad-${String(i).padStart(3, "0")}.pdf`,
      state: i % 2 ? "failed" : "not_read",
      reason: i % 2 ? "read_failed" : "not_readable",
    })),
    ...Array.from({ length: 30 }, (_, i) => ({ name: `Dup-${String(i)}.pdf`, state: "already_in_project", reason: "already_in_project" })),
  ];
  const sid = env.api.model.seed(pid, { kind: "gdrive", name: "Big", files });
  const p1 = await http.sources.files(pid, sid);
  assert.equal(p1.items.length, 200);
  assert.ok(p1.nextCursor !== null);
  assert.equal(p1.source.id, sid);
  assert.equal(p1.source.total, 450);
  const p2 = await http.sources.files(pid, sid, p1.nextCursor);
  assert.equal(p2.items.length, 200);
  const p3 = await http.sources.files(pid, sid, p2.nextCursor);
  assert.deepEqual([p3.items.length, p3.nextCursor], [50, null]);
  const all = [...p1.items, ...p2.items, ...p3.items];
  assert.equal(new Set(all.map((f) => f.id)).size, 450, "no row twice");
  const groupRank = (state: string): number => {
    if (["not_read", "skipped", "failed", "already_in_project"].includes(state)) return 0;
    if (state === "processing") return 1;
    if (state === "waiting") return 2;
    return 3;
  };
  const groups = all.map((f) => groupRank(f.state));
  assert.deepEqual(
    groups,
    [...groups].toSorted((a, b) => a - b),
    "problems, Processing, Waiting, Done",
  );
  assert.equal(all[0].reason !== null, true);
  assert.ok(env.sent.some((x) => x.path === `/api/backend/projects/${pid}/sources/${sid}/files?limit=200`));
  assert.ok(env.sent.some((x) => x.path.startsWith(`/api/backend/projects/${pid}/sources/${sid}/files?limit=200&cursor=`)));
  const bad = await rejection(http.sources.files(pid, sid, "not-a-cursor"));
  assert.deepEqual([bad.code, bad.detail?.apiCode], ["validation", "validation_failed"]);
  const gone = await rejection(http.sources.files(pid, "00000000-0000-4000-8000-000000000000"));
  assert.deepEqual([gone.code, gone.detail?.apiCode], ["not_found", "source_not_found"]);
  // Strict shapes: an unknown state, a missing key, a holder without keys.
  const row = { id: "f", name: "a", category: "pdf", state: "done", reason: null, holder: null };
  const statusBody = JSON.parse(await (await fetch(`/api/backend/projects/${pid}/sources?batches=1`)).text()) as { sources: unknown[] };
  const source = statusBody.sources[0];
  for (const [what, items] of [
    ["unknown state", [{ ...row, state: "weird" }]],
    ["key missing", [{ id: "f", name: "a", category: "pdf", state: "done", reason: null }]],
    ["holder without keys", [{ ...row, holder: {} }]],
  ] as const) {
    await withHook(
      env,
      (x) =>
        x.path.startsWith(`/api/backend/projects/${pid}/sources/${sid}/files`)
          ? jsonResponse(200, JSON.stringify({ source, items, next_cursor: null }))
          : undefined,
      async () => {
        assert.equal((await rejection(http.sources.files(pid, sid))).code, "invalid_response", what);
      },
    );
  }
  // An unknown reason reads `unknown` (DATA-SRC-0001.AC4); an unknown category reads Other files.
  await withHook(
    env,
    (x) =>
      x.path.startsWith(`/api/backend/projects/${pid}/sources/${sid}/files`)
        ? jsonResponse(
            200,
            JSON.stringify({ source, items: [{ ...row, state: "failed", reason: "brand_new", category: "hologram" }], next_cursor: null }),
          )
        : undefined,
    async () => {
      const page = await http.sources.files(pid, sid);
      assert.deepEqual([page.items[0].reason, page.items[0].category], ["unknown", "other_files"]);
    },
  );
});

test("FR-SRC-0020.AC3: a stopped upload still ends the batch and counts the files that did not get in", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Stopped");
  const adds: string[] = [];
  const files = [pdfFile("one.pdf", 1100), pdfFile("two.pdf", 1200), pdfFile("three.pdf", 1300)];
  await withHook(
    env,
    (x) => {
      if (x.method === "POST" && /\/sources\/[0-9a-f-]{36}\/files\?/.test(x.path)) {
        adds.push(x.path);
        if (adds.length === 2) return jsonResponse(409, envelope("batch_ended"));
      }
    },
    async () => {
      const report = await http.sources.upload(pid, files);
      assert.deepEqual(
        { ...report, sourceId: null },
        { sourceId: null, picked: 3, added: 1, alreadyInProject: 0, readAgain: 0, duplicates: 0, notAdded: 2 },
      );
      assert.equal(adds.length, 2, "the third file is not sent");
      assert.ok(
        env.sent.some((x) => x.method === "POST" && x.path === `/api/backend/projects/${pid}/sources/${String(report.sourceId)}/end`),
        "end is still called",
      );
    },
  );
});

test("FR-SRC-0020.AC4: when the batch cannot start there is no source and every file counts as not added", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "No start");
  let calls = 0;
  await withHook(
    env,
    (x) =>
      x.method === "POST" && x.path === `/api/backend/projects/${pid}/sources/uploads`
        ? (calls++, jsonResponse(503, envelope("database_unavailable")))
        : undefined,
    async () => {
      const report = await http.sources.upload(pid, [pdfFile("a.pdf", 900), pdfFile("b.pdf", 950)]);
      assert.deepEqual(report, { sourceId: null, picked: 2, added: 0, alreadyInProject: 0, readAgain: 0, duplicates: 0, notAdded: 2 });
    },
  );
  assert.equal(calls, 1);
  assert.equal(
    env.sent.filter((x) => x.path.startsWith(`/api/backend/projects/${pid}/sources/`) && x.path.includes("/files?")).length,
    0,
    "no file was sent",
  );
  assert.equal((await http.knowledge.status(pid)).sources.length, 0);
});

test("FR-SRC-0024 [R20]: a saved Drive folder tells the screen at once, and the new card is the last one", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Save notifies");
  env.api.model.seed(pid, { kind: "upload", files: [{ name: "a.pdf", state: "done" }] });
  let notified = 0;
  const off = http.knowledge.subscribe(pid, () => notified++);
  try {
    const r = await http.sources.saveDrive(pid, "https://drive.google.com/drive/folders/fake-synthetic-30");
    assert.ok(r.ok);
    assert.equal(notified, 1);
    const s = await http.knowledge.status(pid);
    assert.deepEqual(
      s.sources.map((x) => [x.kind, x.listing]),
      [
        ["upload", false],
        ["gdrive", true],
      ],
    );
  } finally {
    off();
  }
});

test("FR-SRC-0007, FR-SRC-0012 [R13]: the status is read every 3 s while unfinished and every 5 s while idle (fake clock, answers faked)", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Clock");
  let body = v2({ progress: { listing_finished: true, total: 4, finished: 1 } });
  let gets = 0;
  const load = () => void http.knowledge.status(pid).catch(() => undefined);
  const off = http.knowledge.subscribe(pid, load);
  await withHook(
    env,
    (x) => (isStatus(pid)(x) ? (gets++, jsonResponse(200, body)) : undefined),
    async () => {
      mock.timers.enable({ apis: ["setTimeout"] });
      try {
        load();
        await flush();
        assert.equal(gets, 1);
        mock.timers.tick(2_999);
        await flush();
        assert.equal(gets, 1, "unfinished: not before 3 s");
        mock.timers.tick(1);
        await flush();
        assert.equal(gets, 2, "unfinished: at 3 s");
        body = v2(); // finished
        mock.timers.tick(3_000);
        await flush();
        assert.equal(gets, 3, "the read that sees it finished");
        mock.timers.tick(4_999);
        await flush();
        assert.equal(gets, 3, "idle: not before 5 s");
        mock.timers.tick(1);
        await flush();
        assert.equal(gets, 4, "idle: at 5 s");
        mock.timers.tick(5_000);
        await flush();
        assert.equal(gets, 5, "and again");
        off();
        mock.timers.tick(20_000);
        await flush();
        assert.equal(gets, 5, "no read once the screen left");
      } finally {
        mock.timers.reset();
      }
    },
  );
  http.knowledge.leave(pid);
});

test("INT-SRC-0001 (rolling deploy): an unknown kind is skipped, a counts-vs-total mismatch takes total = max, totals.skipped sums the skipped counts", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Relaxed");
  const body = v2({
    sources: [
      wireSrc({ id: "a", total: 3, counts: { ...COUNTS, skipped: 2, done: 2 } }),
      wireSrc({ id: "b", kind: "github" }),
      wireSrc({ id: "c", kind: "upload", total: 1, counts: { ...COUNTS, skipped: 1 } }),
    ],
  });
  await withHook(
    env,
    (x) => (isStatus(pid)(x) ? jsonResponse(200, body) : undefined),
    async () => {
      const s = await http.knowledge.status(pid);
      assert.deepEqual(
        s.sources.map((x) => [x.id, x.total]),
        [
          ["a", 4],
          ["c", 1],
        ],
      );
      assert.equal(s.totals.skipped, 3);
    },
  );
});

test("FR-SRC-0020: a start body that parses badly still ends the batch", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Odd start");
  await withHook(
    env,
    (x) => {
      if (x.method === "POST" && x.path === `/api/backend/projects/${pid}/sources/uploads`)
        return jsonResponse(201, JSON.stringify({ source: { id: "odd-id" } }));
      if (x.method === "POST" && x.path.startsWith(`/api/backend/projects/${pid}/sources/odd-id/files`))
        return jsonResponse(200, JSON.stringify({ outcome: "added" }));
      if (x.method === "POST" && x.path === `/api/backend/projects/${pid}/sources/odd-id/end`) return jsonResponse(200, "{}");
    },
    async () => {
      const report = await http.sources.upload(pid, [pdfFile("a.pdf", 900)]);
      assert.equal(report.sourceId, "odd-id");
      assert.equal(report.added, 1);
      assert.ok(
        env.sent.some((x) => x.path === `/api/backend/projects/${pid}/sources/odd-id/end`),
        "end is called",
      );
    },
  );
});

test("FR-SRC-0020: an add is retried once on 503; a malformed 2xx add body counts as added", async () => {
  const http = createHttpAdapter();
  const pid = await newProject(http, "Retry");
  const seen: Record<string, number> = {};
  await withHook(
    env,
    (x) => {
      if (x.method === "POST" && x.path === `/api/backend/projects/${pid}/sources/uploads`)
        return jsonResponse(201, JSON.stringify({ source: wireSrc({ id: "r1", kind: "upload", open: true }) }));
      if (x.method === "POST" && x.path === `/api/backend/projects/${pid}/sources/r1/end`) return jsonResponse(200, "{}");
      const m = /\/sources\/r1\/files\?filename=([^&]+)/.exec(x.path);
      if (x.method === "POST" && m) {
        const n = (seen[m[1]] = (seen[m[1]] ?? 0) + 1);
        if (m[1] === "a.pdf")
          return n === 1 ? jsonResponse(503, envelope("lock_timeout")) : jsonResponse(200, JSON.stringify({ outcome: "added" }));
        if (m[1] === "b.pdf") return jsonResponse(200, "<html>odd</html>");
        return jsonResponse(503, envelope("lock_timeout")); // c.pdf: 503 twice -> not added
      }
    },
    async () => {
      const report = await http.sources.upload(pid, [pdfFile("a.pdf", 900), pdfFile("b.pdf", 910), pdfFile("c.pdf", 920)]);
      assert.deepEqual({ added: report.added, notAdded: report.notAdded }, { added: 2, notAdded: 1 });
      assert.equal(seen["a.pdf"], 2);
      assert.equal(seen["b.pdf"], 1);
      assert.equal(seen["c.pdf"], 2);
    },
  );
});
