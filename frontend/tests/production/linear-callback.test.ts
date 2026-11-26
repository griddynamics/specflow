// plans/LINEAR/LINEAR-PLAN.md stream S1: where the Linear return route sends the browser, decided by lib/linear-callback.ts on the shared rules
// of lib/oauth-callback.ts: success, Linear's own `?error=`, each API code, an unknown answer, the API down, no session, a malicious
// `return_to`. The route's wiring (session, token, redirect on PUBLIC_URL) is lib/oauth-route.ts, shared with Atlassian and GitHub. No server here.
import { test } from "node:test";
import assert from "node:assert/strict";
import { callbackDestination, LINEAR_CODES } from "@/lib/linear-callback";
import { api, appCallbackRulesTests, query } from "./oauth-callback-support";

const BACK = "/projects/p1/integrations?setup=linear";
const stateFor = (r: unknown) => `nonce.${Buffer.from(JSON.stringify({ u: "u1", r, e: 1 })).toString("base64url")}.sig`;
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

appCallbackRulesTests({ param: "linear", codeInvalid: "linear_code_invalid", back: BACK, stateFor, destination: callbackDestination });
