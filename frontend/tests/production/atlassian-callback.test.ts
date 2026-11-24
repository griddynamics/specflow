// docs/jira-sync/oauth.md#connect-flow: where the Atlassian consent callback (app/api/integrations/atlassian/callback/route.ts) sends the
// browser, decided by lib/atlassian-callback.ts: success, Atlassian's own `?error=` (with a good and with a malicious `return_to` in
// `state`), each API error code, an unknown answer, the API down, and no session. The route's wiring (session, token, redirect on
// PUBLIC_URL) runs in the production e2e (settings-production.spec.ts). No server here.
import { test } from "node:test";
import assert from "node:assert/strict";
import { callbackDestination, withCode } from "@/lib/atlassian-callback";
import { isSameSitePath, locationOn, returnToOf, type CallbackAnswer } from "@/lib/oauth-callback";

const BACK = "/projects/p1/settings?tab=jira";
const stateFor = (r: unknown) => `nonce.${Buffer.from(JSON.stringify({ u: "u1", r, e: 1 })).toString("base64url")}.sig`;
const query = (q: Record<string, string>) => new URLSearchParams(q);
const PUBLIC = "https://app.example"; // the public origin of the redirects
/** Paths a URL parser normalizes to `//evil.example` (another host): dot segments, plain or percent-encoded, and encoded separators. */
const DOT_PATHS = [
  "/.//evil.example",
  "/..//evil.example",
  "/%2e%2e//evil.example",
  "/%2E//evil.example",
  "/.%2e//evil.example",
  "/p/../..//evil.example",
  "/p/.",
  "/%2f/evil.example",
  "/%2F%2Fevil.example",
  "/%5c%5cevil.example",
];

/** A fake API: records each call and answers `answer`. */
function api(answer: CallbackAnswer | Error) {
  const calls: { code: string; state: string }[] = [];
  const post = (code: string, state: string) => {
    calls.push({ code, state });
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
  };
  return { calls, post };
}

test("success: the code and state go to the API once; the browser lands on the API's return_to with ?atlassian=connected", async () => {
  const fake = api({ status: 200, body: { connection: {}, return_to: BACK } });
  const state = stateFor(BACK);
  assert.equal(
    await callbackDestination(query({ code: "c-1", state }), true, fake.post),
    "/projects/p1/settings?tab=jira&atlassian=connected",
  );
  assert.deepEqual(fake.calls, [{ code: "c-1", state }]);
});

test("success with a return_to that is not a same-site path falls back to the state's, then to /", async () => {
  const state = stateFor(BACK);
  const evil = api({ status: 200, body: { return_to: "//evil.example/x" } });
  assert.equal(await callbackDestination(query({ code: "c", state }), true, evil.post), `${BACK}&atlassian=connected`);
  assert.equal(
    await callbackDestination(query({ code: "c", state: stateFor("https://evil.example") }), true, evil.post),
    "/?atlassian=connected",
  );
});

test("Atlassian's ?error= never reaches the API and lands on the state's return_to as atlassian_consent_denied", async () => {
  const fake = api({ status: 200, body: {} });
  const to = await callbackDestination(query({ error: "access_denied", error_description: "x", state: stateFor(BACK) }), true, fake.post);
  assert.equal(to, `${BACK}&atlassian=atlassian_consent_denied`);
  assert.equal(fake.calls.length, 0);
});

test("a malicious return_to in the state is refused: the browser goes to / (never off the site)", async () => {
  const fake = api({ status: 200, body: {} });
  for (const r of [
    "//evil.example/x",
    "https://evil.example/x",
    "/\\evil.example",
    "\\\\evil.example",
    "javascript:alert(1)",
    "/a\nb",
    "/a b",
    "/café",
    "",
    42,
    null,
    `/${"a".repeat(2048)}`,
  ]) {
    assert.equal(
      await callbackDestination(query({ error: "access_denied", state: stateFor(r) }), true, fake.post),
      "/?atlassian=atlassian_consent_denied",
      String(r),
    );
  }
  for (const state of ["", "a.b", "a.!!!.c", `a.${Buffer.from("not json").toString("base64url")}.c`, "a.b.c.d"]) {
    assert.equal(returnToOf(state), "/", state);
  }
  assert.equal(returnToOf(null), "/");
});

test("isSameSitePath: a path on this site only", () => {
  for (const ok of ["/", BACK, "/projects/a%20b/settings", "/a.b/c..d/.e", "/p?next=/../x"]) assert.ok(isSameSitePath(ok), ok);
  for (const bad of ["", "x", "//x", "/\\x", "/\tx", "/\u0000", "https://x", ...DOT_PATHS]) assert.ok(!isSameSitePath(bad), bad);
});

test("withCode never yields a path a URL parser reads as another host; locationOn never leaves the public origin", () => {
  for (const r of ["/.//evil.example", "/..//evil.example", "/%2e%2e//evil.example"]) {
    assert.equal(withCode(r, "connected"), "/?atlassian=connected", r);
  }
  for (const r of DOT_PATHS) {
    const to = withCode(r, "connected");
    assert.ok(isSameSitePath(to), `${r} -> ${to}`);
    assert.equal(new URL(locationOn(to, PUBLIC)).origin, PUBLIC, r);
  }
  assert.equal(locationOn(`${BACK}&atlassian=connected`, `${PUBLIC}/`), `${PUBLIC}${BACK}&atlassian=connected`);
  for (const path of ["//evil.example/x", "https://evil.example/x", "/\\evil.example", "http://app.example/x"]) {
    assert.equal(locationOn(path, PUBLIC), `${PUBLIC}/`, path);
  }
});

test("a dot-segment return_to never sends the browser off the site, on the error path (unsigned state) or the success path", async () => {
  for (const r of DOT_PATHS) {
    const state = stateFor(r);
    const denied = await callbackDestination(query({ error: "access_denied", state }), true, api({ status: 200, body: {} }).post);
    assert.equal(denied, "/?atlassian=atlassian_consent_denied", r);
    const refused = await callbackDestination(
      query({ code: "c", state }),
      true,
      api({ status: 400, body: { error: { code: "atlassian_code_invalid" } } }).post,
    );
    assert.equal(refused, "/?atlassian=atlassian_code_invalid", r);
    const connected = await callbackDestination(query({ code: "c", state }), true, api({ status: 200, body: { return_to: r } }).post);
    assert.equal(connected, "/?atlassian=connected", r);
    for (const to of [denied, refused, connected]) assert.equal(new URL(locationOn(to, PUBLIC)).origin, PUBLIC);
  }
});

test("each API error code lands on the state's return_to with that code; any other answer is atlassian_unexpected", async () => {
  const state = stateFor(BACK);
  for (const [status, code] of [
    [400, "atlassian_state_invalid"],
    [400, "atlassian_code_invalid"],
    [403, "atlassian_site_not_granted"],
    [502, "atlassian_unreachable"],
    [503, "atlassian_oauth_unavailable"],
  ] as const) {
    const fake = api({ status, body: { error: { code, message: "m", details: {} } } });
    assert.equal(await callbackDestination(query({ code: "c", state }), true, fake.post), `${BACK}&atlassian=${code}`);
  }
  for (const answer of [
    { status: 422, body: { error: { code: "validation_failed" } } },
    { status: 500, body: null },
    { status: 400, body: { error: { code: "connected" } } },
    { status: 302, body: null },
  ]) {
    assert.equal(await callbackDestination(query({ code: "c", state }), true, api(answer).post), `${BACK}&atlassian=atlassian_unexpected`);
  }
  assert.equal(
    await callbackDestination(query({ code: "c", state }), true, api(new TypeError("fetch failed")).post),
    `${BACK}&atlassian=atlassian_unexpected`,
  );
});

test("no code or no state: atlassian_unexpected without calling the API", async () => {
  const fake = api({ status: 200, body: {} });
  assert.equal(await callbackDestination(query({ state: stateFor(BACK) }), true, fake.post), `${BACK}&atlassian=atlassian_unexpected`);
  assert.equal(await callbackDestination(query({ code: "c" }), true, fake.post), "/?atlassian=atlassian_unexpected");
  assert.equal(fake.calls.length, 0);
});

test("no session: sign-in, without calling the API; a 401 from the API (the token was refused) also goes to sign-in", async () => {
  const fake = api({ status: 200, body: {} });
  assert.equal(await callbackDestination(query({ code: "c", state: stateFor(BACK) }), false, fake.post), "/login");
  assert.equal(fake.calls.length, 0);
  assert.equal(
    await callbackDestination(query({ code: "c", state: stateFor(BACK) }), true, api({ status: 401, body: null }).post),
    "/login",
  );
});

test("the destination never carries the code or the state", async () => {
  const state = stateFor(BACK);
  for (const answer of [
    { status: 200, body: { return_to: BACK } },
    { status: 400, body: { error: { code: "atlassian_code_invalid" } } },
  ]) {
    const to = await callbackDestination(query({ code: "secret-code", state }), true, api(answer).post);
    assert.ok(!to.includes("secret-code") && !to.includes(state) && !to.includes("sig"), to);
  }
});
