// docs/github/oauth.md#connect-flow: where the two GitHub return routes send the browser, decided by
// lib/github-callback.ts on the shared rules of lib/oauth-callback.ts. The OAuth callback: success, GitHub's own `?error=`, each API code,
// an unknown answer, the API down, no session, a malicious `return_to`. The setup route (after an app install): no `state`, success,
// a refused state, and GitHub's `installation_id` never reaching the API. The routes' wiring (session, token, redirect on PUBLIC_URL)
// runs in the production e2e (github-settings-production.spec.ts). No server here.
import { test } from "node:test";
import assert from "node:assert/strict";
import { callbackDestination, setupDestination } from "@/lib/github-callback";
import { codeOfAnswer, isSameSitePath, locationOn, withParam, type CallbackAnswer } from "@/lib/oauth-callback";

const BACK = "/projects/p1/settings?tab=github";
const stateFor = (r: unknown, p = "authorize") => `nonce.${Buffer.from(JSON.stringify({ u: "u1", r, p, e: 1 })).toString("base64url")}.sig`;
const query = (q: Record<string, string>) => new URLSearchParams(q);
const PUBLIC = "https://app.example";
const EVIL = ["//evil.example/x", "https://evil.example/x", "/\\evil.example", "/.//evil.example", "/%2e%2e//evil.example", "/%2f/evil"];

function api(answer: CallbackAnswer | Error) {
  const calls: unknown[][] = [];
  const post = (...args: string[]) => {
    calls.push(args);
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
  };
  return { calls, post };
}

// ---- The OAuth callback ----

test("callback success: code and state go to the API once; the browser lands on the API's return_to with ?github=connected", async () => {
  const fake = api({ status: 200, body: { connection: {}, return_to: BACK } });
  const state = stateFor(BACK);
  assert.equal(await callbackDestination(query({ code: "c-1", state }), true, fake.post), `${BACK}&github=connected`);
  assert.deepEqual(fake.calls, [["c-1", state]]);
});

test("callback: GitHub's ?error= never reaches the API and is github_consent_denied on the state's return_to", async () => {
  const fake = api({ status: 200, body: {} });
  const to = await callbackDestination(query({ error: "access_denied", state: stateFor(BACK) }), true, fake.post);
  assert.equal(to, `${BACK}&github=github_consent_denied`);
  assert.equal(fake.calls.length, 0);
});

test("callback: each API code lands with that code; any other answer, and the API down, is github_unexpected", async () => {
  const state = stateFor(BACK);
  for (const [status, code] of [
    [400, "github_state_invalid"],
    [400, "github_code_invalid"],
    [502, "github_unreachable"],
    [502, "github_auth_failed"],
    [503, "github_unavailable"],
  ] as const) {
    const fake = api({ status, body: { error: { code } } });
    assert.equal(await callbackDestination(query({ code: "c", state }), true, fake.post), `${BACK}&github=${code}`);
  }
  for (const answer of [
    { status: 422, body: { error: { code: "validation_failed" } } },
    { status: 500, body: null },
    { status: 400, body: { error: { code: "connected" } } },
    { status: 400, body: { error: { code: "atlassian_code_invalid" } } },
  ]) {
    assert.equal(await callbackDestination(query({ code: "c", state }), true, api(answer).post), `${BACK}&github=github_unexpected`);
  }
  assert.equal(
    await callbackDestination(query({ code: "c", state }), true, api(new TypeError("down")).post),
    `${BACK}&github=github_unexpected`,
  );
});

test("callback: no session, or a 401 from the API, goes to sign-in; no code or state is github_unexpected without a call", async () => {
  const fake = api({ status: 200, body: {} });
  assert.equal(await callbackDestination(query({ code: "c", state: stateFor(BACK) }), false, fake.post), "/login");
  assert.equal(await callbackDestination(query({ state: stateFor(BACK) }), true, fake.post), `${BACK}&github=github_unexpected`);
  assert.equal(await callbackDestination(query({ code: "c" }), true, fake.post), "/?github=github_unexpected");
  assert.equal(fake.calls.length, 0);
  assert.equal(
    await callbackDestination(query({ code: "c", state: stateFor(BACK) }), true, api({ status: 401, body: null }).post),
    "/login",
  );
});

test("callback: a malicious return_to (in the state or in the answer) never sends the browser off the site", async () => {
  for (const r of EVIL) {
    const state = stateFor(r);
    const denied = await callbackDestination(query({ error: "x", state }), true, api({ status: 200, body: {} }).post);
    const connected = await callbackDestination(query({ code: "c", state }), true, api({ status: 200, body: { return_to: r } }).post);
    assert.equal(denied, "/?github=github_consent_denied", r);
    assert.equal(connected, "/?github=connected", r);
    for (const to of [denied, connected]) assert.equal(new URL(locationOn(to, PUBLIC)).origin, PUBLIC, r);
  }
});

test("callback: the destination never carries the code or the state", async () => {
  const state = stateFor(BACK);
  for (const answer of [
    { status: 200, body: { return_to: BACK } },
    { status: 400, body: { error: { code: "github_code_invalid" } } },
  ]) {
    const to = await callbackDestination(query({ code: "secret-code", state }), true, api(answer).post);
    assert.ok(!to.includes("secret-code") && !to.includes(state) && !to.includes("sig"), to);
  }
});

// ---- The setup route ----

test("setup success: only the state goes to the API (never installation_id); the browser lands with ?github=installed", async () => {
  const fake = api({ status: 200, body: { return_to: BACK } });
  const state = stateFor(BACK, "install");
  const to = await setupDestination(query({ installation_id: "999", setup_action: "install", state }), true, fake.post);
  assert.equal(to, `${BACK}&github=installed`);
  assert.deepEqual(fake.calls, [[state]]);
  assert.ok(!to.includes("999"));
});

test("setup without a state (installed from GitHub's own pages) goes home without calling the API", async () => {
  const fake = api({ status: 200, body: { return_to: BACK } });
  assert.equal(await setupDestination(query({ installation_id: "1", setup_action: "install" }), true, fake.post), "/");
  assert.equal(await setupDestination(query({ state: "" }), true, fake.post), "/");
  assert.equal(fake.calls.length, 0);
});

test("setup: a refused state lands with its code; an unknown answer or the API down is github_unexpected; no session is sign-in", async () => {
  const state = stateFor(BACK, "install");
  const refused = api({ status: 400, body: { error: { code: "github_state_invalid" } } });
  assert.equal(await setupDestination(query({ state }), true, refused.post), `${BACK}&github=github_state_invalid`);
  assert.equal(await setupDestination(query({ state }), true, api({ status: 500, body: null }).post), `${BACK}&github=github_unexpected`);
  assert.equal(await setupDestination(query({ state }), true, api(new Error("down")).post), `${BACK}&github=github_unexpected`);
  const fake = api({ status: 200, body: {} });
  assert.equal(await setupDestination(query({ state }), false, fake.post), "/login");
  assert.equal(fake.calls.length, 0);
  assert.equal(await setupDestination(query({ state }), true, api({ status: 401, body: null }).post), "/login");
});

test("setup: a malicious return_to never sends the browser off the site", async () => {
  for (const r of EVIL) {
    const to = await setupDestination(query({ state: stateFor(r, "install") }), true, api({ status: 200, body: { return_to: r } }).post);
    assert.equal(to, "/?github=installed", r);
  }
});

// ---- The shared rules ----

test("withParam sets one query parameter on a same-site path and keeps the others; codeOfAnswer knows only the given codes", () => {
  assert.equal(withParam("/a?b=1#frag", "github", "connected"), "/a?b=1&github=connected");
  assert.equal(withParam("//evil.example", "github", "connected"), "/?github=connected");
  assert.ok(isSameSitePath(withParam("/x", "atlassian", "connected")));
  assert.equal(codeOfAnswer({ error: { code: "b" } }, ["a", "b"], "z"), "b");
  assert.equal(codeOfAnswer({ error: { code: "c" } }, ["a", "b"], "z"), "z");
  assert.equal(codeOfAnswer(null, ["a"], "z"), "z");
});
