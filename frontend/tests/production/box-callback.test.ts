// docs/box/oauth.md "Connect flow": where the Box return route sends the browser, decided by lib/box-callback.ts on the shared rules of
// lib/oauth-callback.ts: success, Box's own `?error=`, each API code, an unknown answer, the API down, no session, a malicious `return_to`.
// The route's wiring (session, token, redirect on PUBLIC_URL) is `oauthReturnRoute`, covered with GitHub's. No server here.
import { test } from "node:test";
import assert from "node:assert/strict";
import { BOX_CODES, callbackDestination } from "@/lib/box-callback";
import { locationOn, type CallbackAnswer } from "@/lib/oauth-callback";

const BACK = "/projects/p1/settings?tab=integrations";
const stateFor = (r: unknown) => `nonce.${Buffer.from(JSON.stringify({ u: "u1", r, p: "authorize", e: 1 })).toString("base64url")}.sig`;
const query = (q: Record<string, string>) => new URLSearchParams(q);
const EVIL = ["//evil.example/x", "https://evil.example/x", "/\\evil.example", "/.//evil.example", "/%2e%2e//evil.example", "/%2f/evil"];

function api(answer: CallbackAnswer | Error) {
  const calls: unknown[][] = [];
  const post = (...args: string[]) => {
    calls.push(args);
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
  };
  return { calls, post };
}

test("success: code and state go to the API once; the browser lands on the API's return_to with ?box=connected", async () => {
  const fake = api({ status: 200, body: { connection: {}, return_to: BACK } });
  const state = stateFor(BACK);
  assert.equal(await callbackDestination(query({ code: "c-1", state }), true, fake.post), `${BACK}&box=connected`);
  assert.deepEqual(fake.calls, [["c-1", state]]);
});

test("Box's ?error= never reaches the API and is box_consent_denied on the state's return_to", async () => {
  const fake = api({ status: 200, body: {} });
  assert.equal(
    await callbackDestination(query({ error: "access_denied", state: stateFor(BACK) }), true, fake.post),
    `${BACK}&box=box_consent_denied`,
  );
  assert.equal(fake.calls.length, 0);
});

test("each API code lands with that code; any other answer, and the API down, is box_unexpected", async () => {
  const state = stateFor(BACK);
  for (const [status, code] of [
    [400, "box_state_invalid"],
    [400, "box_code_invalid"],
    [502, "box_unreachable"],
    [502, "box_auth_failed"],
    [503, "box_unavailable"],
  ] as const) {
    assert.ok(BOX_CODES.includes(code));
    assert.equal(
      await callbackDestination(query({ code: "c", state }), true, api({ status, body: { error: { code } } }).post),
      `${BACK}&box=${code}`,
    );
  }
  for (const answer of [
    { status: 422, body: { error: { code: "validation_failed" } } },
    { status: 500, body: null },
    { status: 400, body: { error: { code: "connected" } } },
    { status: 400, body: { error: { code: "github_code_invalid" } } },
  ]) {
    assert.equal(await callbackDestination(query({ code: "c", state }), true, api(answer).post), `${BACK}&box=box_unexpected`);
  }
  assert.equal(await callbackDestination(query({ code: "c", state }), true, api(new TypeError("down")).post), `${BACK}&box=box_unexpected`);
});

test("no session, or a 401 from the API, goes to sign-in; no code or state is box_unexpected without a call", async () => {
  const fake = api({ status: 200, body: {} });
  assert.equal(await callbackDestination(query({ code: "c", state: stateFor(BACK) }), false, fake.post), "/login");
  assert.equal(await callbackDestination(query({ state: stateFor(BACK) }), true, fake.post), `${BACK}&box=box_unexpected`);
  assert.equal(await callbackDestination(query({ code: "c" }), true, fake.post), "/?box=box_unexpected");
  assert.equal(fake.calls.length, 0);
  assert.equal(
    await callbackDestination(query({ code: "c", state: stateFor(BACK) }), true, api({ status: 401, body: null }).post),
    "/login",
  );
});

test("a malicious return_to (in the state or in the answer) never sends the browser off the site", async () => {
  const site = "https://app.example";
  for (const r of EVIL) {
    const state = stateFor(r);
    const denied = await callbackDestination(query({ error: "x", state }), true, api({ status: 200, body: {} }).post);
    const connected = await callbackDestination(query({ code: "c", state }), true, api({ status: 200, body: { return_to: r } }).post);
    assert.equal(denied, "/?box=box_consent_denied", r);
    assert.equal(connected, "/?box=connected", r);
    for (const to of [denied, connected]) assert.equal(new URL(locationOn(to, site)).origin, site, r);
  }
});
