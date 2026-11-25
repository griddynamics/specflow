// plans/LINEAR/LINEAR-PLAN.md stream S1: where the Linear return route sends the browser, decided by lib/linear-callback.ts on the shared rules
// of lib/oauth-callback.ts: success, Linear's own `?error=`, each API code, an unknown answer, the API down, no session, a malicious
// `return_to`. The route's wiring (session, token, redirect on PUBLIC_URL) is lib/oauth-route.ts, shared with Atlassian and GitHub. No server here.
import { test } from "node:test";
import assert from "node:assert/strict";
import { callbackDestination, LINEAR_CODES } from "@/lib/linear-callback";
import { locationOn, type CallbackAnswer } from "@/lib/oauth-callback";

const BACK = "/projects/p1/integrations?setup=linear";
const stateFor = (r: unknown) => `nonce.${Buffer.from(JSON.stringify({ u: "u1", r, e: 1 })).toString("base64url")}.sig`;
const query = (q: Record<string, string>) => new URLSearchParams(q);
const PUBLIC = "https://app.example";
const EVIL = ["//evil.example/x", "https://evil.example/x", "/\\evil.example", "/.//evil.example", "/%2e%2e//evil.example"];

function api(answer: CallbackAnswer | Error) {
  const calls: unknown[][] = [];
  const post = (...args: string[]) => {
    calls.push(args);
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
  };
  return { calls, post };
}

test("success: code and state go to the API once; the browser lands on the API's return_to with ?linear=connected", async () => {
  const fake = api({ status: 200, body: { connection: {}, return_to: BACK } });
  const state = stateFor(BACK);
  assert.equal(await callbackDestination(query({ code: "c-1", state }), true, fake.post), `${BACK}&linear=connected`);
  assert.deepEqual(fake.calls, [["c-1", state]]);
});

test("Linear's own ?error= never reaches the API and is linear_consent_denied on the state's return_to", async () => {
  const fake = api({ status: 200, body: {} });
  const to = await callbackDestination(query({ error: "access_denied", state: stateFor(BACK) }), true, fake.post);
  assert.equal(to, `${BACK}&linear=linear_consent_denied`);
  assert.equal(fake.calls.length, 0);
});

test("each API code lands with that code; any other answer, and the API down, is linear_unexpected", async () => {
  const state = stateFor(BACK);
  const known = LINEAR_CODES.filter((c) => c !== "connected" && c !== "linear_consent_denied" && c !== "linear_unexpected");
  for (const code of known) {
    const fake = api({ status: 400, body: { error: { code } } });
    assert.equal(await callbackDestination(query({ code: "c", state }), true, fake.post), `${BACK}&linear=${code}`);
  }
  for (const answer of [
    { status: 422, body: { error: { code: "validation_failed" } } },
    { status: 500, body: null },
    { status: 400, body: { error: { code: "connected" } } },
    { status: 400, body: { error: { code: "github_code_invalid" } } },
  ]) {
    assert.equal(await callbackDestination(query({ code: "c", state }), true, api(answer).post), `${BACK}&linear=linear_unexpected`);
  }
  assert.equal(
    await callbackDestination(query({ code: "c", state }), true, api(new TypeError("down")).post),
    `${BACK}&linear=linear_unexpected`,
  );
});

test("no session, or a 401 from the API, goes to sign-in; no code or state is linear_unexpected without a call", async () => {
  const fake = api({ status: 200, body: {} });
  assert.equal(await callbackDestination(query({ code: "c", state: stateFor(BACK) }), false, fake.post), "/login");
  assert.equal(await callbackDestination(query({ state: stateFor(BACK) }), true, fake.post), `${BACK}&linear=linear_unexpected`);
  assert.equal(await callbackDestination(query({ code: "c" }), true, fake.post), "/?linear=linear_unexpected");
  assert.equal(fake.calls.length, 0);
  assert.equal(
    await callbackDestination(query({ code: "c", state: stateFor(BACK) }), true, api({ status: 401, body: null }).post),
    "/login",
  );
});

test("a malicious return_to (in the state or in the answer) never sends the browser off the site", async () => {
  for (const r of EVIL) {
    const state = stateFor(r);
    const denied = await callbackDestination(query({ error: "x", state }), true, api({ status: 200, body: {} }).post);
    const connected = await callbackDestination(query({ code: "c", state }), true, api({ status: 200, body: { return_to: r } }).post);
    assert.equal(denied, "/?linear=linear_consent_denied", r);
    assert.equal(connected, "/?linear=connected", r);
    for (const to of [denied, connected]) assert.equal(new URL(locationOn(to, PUBLIC)).origin, PUBLIC, r);
  }
});

test("the destination never carries the code or the state", async () => {
  const state = stateFor(BACK);
  for (const answer of [
    { status: 200, body: { return_to: BACK } },
    { status: 400, body: { error: { code: "linear_code_invalid" } } },
  ]) {
    const to = await callbackDestination(query({ code: "secret-code", state }), true, api(answer).post);
    assert.ok(!to.includes("secret-code") && !to.includes(state) && !to.includes("sig"), to);
  }
});
