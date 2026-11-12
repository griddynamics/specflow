// MCP-TOOLING session 10: the production work port over `/projects/{pid}/findings*` and `/projects/{pid}/plan/claims*`, and the pure logic of
// the findings list and the claims board. Layer U: the wire parsers and the words. Layer P: the port against a fake `fetch`.
import { test, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import { createHttpWork, toClaim, toFinding } from "@/lib/data/http/work";
import { DataError, isDataError } from "@/lib/domain/errors";
import { age, closeError, personName, refHref, releaseError } from "@/components/plan/work/work-logic";

const PID = "11111111-1111-4111-8111-111111111111";
const alice = { name: "Alice", you: true, agent_label: null };
const wireFinding = (over: Record<string, unknown> = {}) => ({
  finding_id: "FND::1",
  kind: "question",
  text: "Which currencies does the API accept?",
  status: "open",
  blocking: true,
  about: ["FEAT::API"],
  refers_to: [{ type: "url", value: "https://example.com/spec" }],
  story_id: "STORY::A::STUCK",
  story_title: "Checkout",
  author: alice,
  created_at: "2026-01-01T10:00:00Z",
  resolved_by: null,
  resolved_at: null,
  resolution_note: null,
  orphaned: false,
  ...over,
});
const wireClaim = (over: Record<string, unknown> = {}) => ({
  kind: "story",
  story_id: "STORY::A::API",
  title: "API",
  task_id: null,
  holder: { name: "Ben", you: false, agent_label: "claude-code" },
  claimed_at: "2026-01-01T09:00:00Z",
  branch: "fix/api",
  tasks: 2,
  ...over,
});

const rejects = (p: Promise<unknown>, code: string) => assert.rejects(p, (e) => isDataError(e) && e.code === code);

// ---- Layer U ----

test("toFinding reads a finding with its nodes, references and author; any other shape is invalid_response", () => {
  const f = toFinding(wireFinding());
  assert.equal(f.findingId, "FND::1");
  assert.equal(f.storyTitle, "Checkout");
  assert.equal(toFinding(wireFinding({ story_title: undefined })).storyTitle, null);
  assert.deepEqual(f.about, ["FEAT::API"]);
  assert.deepEqual(f.refersTo, [{ type: "url", value: "https://example.com/spec" }]);
  assert.deepEqual(f.author, { name: "Alice", you: true, agentLabel: null });
  assert.equal(f.resolvedBy, null);
  assert.throws(
    () => toFinding(wireFinding({ kind: "idea" })),
    (e) => isDataError(e) && e.code === "invalid_response",
  );
  assert.throws(
    () => toFinding(wireFinding({ about: [1] })),
    (e) => isDataError(e) && e.code === "invalid_response",
  );
  assert.throws(
    () => toFinding({ ...wireFinding(), blocking: "yes" }),
    (e) => isDataError(e) && e.code === "invalid_response",
  );
});

test("toClaim reads a story claim and a task claim", () => {
  assert.deepEqual(toClaim(wireClaim()).holder, { name: "Ben", you: false, agentLabel: "claude-code" });
  const task = toClaim(wireClaim({ kind: "task", task_id: "TASK::1", branch: null, tasks: 1 }));
  assert.equal(task.taskId, "TASK::1");
  assert.equal(task.branch, null);
  assert.throws(
    () => toClaim(wireClaim({ tasks: -1 })),
    (e) => isDataError(e) && e.code === "invalid_response",
  );
});

test("a name that is not known reads as a project member; the signed-in person as you", () => {
  assert.equal(personName({ name: null, you: false, agentLabel: null }), "A project member");
  assert.equal(personName({ name: "Ben", you: false, agentLabel: null }), "Ben");
  assert.equal(personName({ name: "Alice", you: true, agentLabel: null }), "Alice (you)");
});

test("a reference is a link only for an http(s) URL; a commit, a file and a javascript: value stay text", () => {
  assert.equal(refHref({ type: "url", value: "https://example.com/a?b=1" }), "https://example.com/a?b=1");
  assert.equal(refHref({ type: "pr", value: "https://git.example.com/p/pull/4" }), "https://git.example.com/p/pull/4");
  assert.equal(refHref({ type: "pr", value: "42" }), null);
  assert.equal(refHref({ type: "commit", value: "e44f0c1" }), null);
  assert.equal(refHref({ type: "file", value: "src/a.py" }), null);
  assert.equal(refHref({ type: "url", value: "javascript:alert(1)" }), null);
});

test("age counts minutes, hours and days", () => {
  const now = Date.parse("2026-01-10T12:00:00Z");
  assert.equal(age("2026-01-10T11:59:40Z", now), "just now");
  assert.equal(age("2026-01-10T11:30:00Z", now), "30 min");
  assert.equal(age("2026-01-10T09:00:00Z", now), "3 h");
  assert.equal(age("2026-01-07T12:00:00Z", now), "3 days");
  assert.equal(age("nope", now), "just now");
});

test("the words of a failed answer come from the API code, never from server text", () => {
  const api = (apiCode: string) => new DataError("conflict", false, { status: 409, apiCode });
  assert.equal(closeError(api("finding_closed")).refresh, true);
  assert.equal(closeError(api("finding_not_found")).refresh, true);
  assert.match(closeError(api("plan_applying")).text, /plan is being updated/);
  assert.equal(closeError(api("plan_applying")).refresh, false);
  assert.match(closeError(new Error("boom")).text, /could not be saved/);
  assert.equal(releaseError(api("story_not_found")).refresh, true);
  assert.match(releaseError(new Error("boom")).text, /could not be released/);
});

// ---- Layer P ----

interface Call {
  method: string;
  path: string;
  body: unknown;
}
const calls: Call[] = [];
const BASE = "/api/backend";

/** Every request answers `status` and `body`; the requests are kept in `calls`. */
function answer(status: number, body: unknown) {
  calls.length = 0;
  mock.method(globalThis, "fetch", (url: string, init?: RequestInit) => {
    calls.push({
      method: init?.method ?? "GET",
      path: url.slice(BASE.length),
      body: typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined,
    });
    return Promise.resolve(new Response(JSON.stringify(body), { status }));
  });
}
const apiError = (code: string) => ({ error: { code, message: code, details: {} } });
afterEach(() => {
  mock.restoreAll();
});

test("findings asks for the open ones by default and for a node when given; `more` follows the cursor", async () => {
  answer(200, { findings: [wireFinding()], next: "abc" });
  const work = createHttpWork();
  const page = await work.findings(PID);
  assert.equal(page.findings.length, 1);
  assert.equal(page.more, true);
  await work.findings(PID, { status: "all", node: "FEAT::API" });
  assert.deepEqual(
    calls.map((c) => `${c.method} ${c.path}`),
    [`GET /projects/${PID}/findings?limit=200&status=open`, `GET /projects/${PID}/findings?limit=200&status=all&node=FEAT%3A%3AAPI`],
  );
});

test("close POSTs the note to resolve or dismiss and reads the closed finding and the unblocked count", async () => {
  answer(200, { finding: wireFinding({ status: "resolved" }), unblocked: 1 });
  const work = createHttpWork();
  const done = await work.close(PID, "FND::1", "resolve", "Euro only");
  assert.equal(done.unblocked, 1);
  assert.equal(done.finding.status, "resolved");
  await work.close(PID, "FND::1", "dismiss");
  assert.deepEqual(
    calls.map((c) => [c.method, c.path, c.body]),
    [
      ["POST", `/projects/${PID}/findings/FND%3A%3A1/resolve`, { note: "Euro only" }],
      ["POST", `/projects/${PID}/findings/FND%3A%3A1/dismiss`, { note: null }],
    ],
  );
});

test("a closed finding and a missing one reject with their API codes", async () => {
  answer(409, apiError("finding_closed"));
  await assert.rejects(
    createHttpWork().close(PID, "FND::1", "resolve"),
    (e) => isDataError(e) && e.code === "conflict" && e.detail?.apiCode === "finding_closed",
  );
  answer(503, apiError("graph_unavailable"));
  await rejects(createHttpWork().close(PID, "FND::1", "resolve"), "unavailable");
});

test("claims reads the board; a project without a plan has an empty one; another failure rejects", async () => {
  answer(200, { claims: [wireClaim(), wireClaim({ kind: "task", task_id: "TASK::1" })] });
  const work = createHttpWork();
  assert.deepEqual(
    (await work.claims(PID)).map((c) => c.kind),
    ["story", "task"],
  );
  answer(404, apiError("no_plan"));
  assert.deepEqual(await work.claims(PID), []);
  answer(503, apiError("graph_unavailable"));
  await rejects(work.claims(PID), "unavailable");
  answer(200, { claims: "none" });
  await rejects(work.claims(PID), "invalid_response");
});

test("release POSTs to the story and reads the count of freed tasks", async () => {
  answer(200, { released: 2 });
  assert.deepEqual(await createHttpWork().release(PID, "STORY::A::API"), { released: 2 });
  assert.deepEqual(
    calls.map((c) => `${c.method} ${c.path}`),
    [`POST /projects/${PID}/plan/claims/STORY%3A%3AA%3A%3AAPI/release`],
  );
});
