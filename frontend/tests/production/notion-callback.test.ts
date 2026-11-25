// docs/notion/oauth.md#connect-flow: where the Notion OAuth return route sends the browser (lib/notion-callback.ts on the shared rules of
// lib/oauth-callback.ts): success, Notion's own `?error=`, each API code, an unknown answer, the API down, no session, a malicious return_to.
// No server here; the route's wiring (session, token, redirect on PUBLIC_URL) runs in the production e2e.
import { test } from "node:test";
import assert from "node:assert/strict";
import { callbackDestination, NOTION_CODES } from "@/lib/notion-callback";
import type { CallbackAnswer } from "@/lib/oauth-callback";
import { isSameSitePath } from "@/lib/same-site-path";

const BACK = "/projects/p1/integrations?setup=notion";
const stateFor = (r: unknown) => `nonce.${Buffer.from(JSON.stringify({ u: "u1", r, p: "authorize", e: 1 })).toString("base64url")}.sig`;
const query = (q: Record<string, string>) => new URLSearchParams(q);

function api(answer: CallbackAnswer | Error) {
  const calls: unknown[][] = [];
  const post = (...args: string[]) => {
    calls.push(args);
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
  };
  return { calls, post };
}

test("success: code and state go to the API once; the browser lands on the API's return_to with ?notion=connected", async () => {
  const fake = api({ status: 200, body: { connection: {}, return_to: BACK } });
  const state = stateFor(BACK);
  assert.equal(await callbackDestination(query({ code: "c-1", state }), true, fake.post), `${BACK}&notion=connected`);
  assert.deepEqual(fake.calls, [["c-1", state]]);
});

test("Notion's own ?error= never reaches the API and is notion_consent_denied on the state's return_to", async () => {
  const fake = api({ status: 200, body: {} });
  assert.equal(
    await callbackDestination(query({ error: "access_denied", state: stateFor(BACK) }), true, fake.post),
    `${BACK}&notion=notion_consent_denied`,
  );
  assert.equal(fake.calls.length, 0);
});

test("each API code lands with that code; any other answer, and the API down, is notion_unexpected", async () => {
  const state = stateFor(BACK);
  for (const [status, code] of [
    [400, "notion_state_invalid"],
    [400, "notion_code_invalid"],
    [502, "notion_unreachable"],
    [502, "notion_auth_failed"],
    [503, "notion_unavailable"],
  ] as const) {
    const fake = api({ status, body: { error: { code } } });
    assert.equal(await callbackDestination(query({ code: "c", state }), true, fake.post), `${BACK}&notion=${code}`);
  }
  for (const answer of [
    { status: 422, body: { error: { code: "validation_failed" } } },
    { status: 500, body: null },
    { status: 400, body: { error: { code: "connected" } } },
    { status: 400, body: { error: { code: "github_code_invalid" } } },
  ]) {
    const fake = api(answer);
    assert.equal(await callbackDestination(query({ code: "c", state }), true, fake.post), `${BACK}&notion=notion_unexpected`);
  }
  const secret = "SECRET-code-9f2";
  const spill = api({ status: 400, body: { error: { code: "notion_code_invalid", message: secret } } });
  const where = await callbackDestination(query({ code: secret, state }), true, spill.post);
  assert.ok(!where.includes(secret) && !where.includes(state), "code and state never reach the redirect");
  const down = api(new Error("connect ECONNREFUSED"));
  assert.equal(await callbackDestination(query({ code: "c", state }), true, down.post), `${BACK}&notion=notion_unexpected`);
});

test("no session goes to sign-in without calling the API; a refused session token (401) does too", async () => {
  const fake = api({ status: 200, body: {} });
  assert.equal(await callbackDestination(query({ code: "c", state: stateFor(BACK) }), false, fake.post), "/login");
  assert.equal(fake.calls.length, 0);
  const refused = api({ status: 401, body: {} });
  assert.equal(await callbackDestination(query({ code: "c", state: stateFor(BACK) }), true, refused.post), "/login");
});

test("a missing or empty code or state is notion_unexpected and the API is not called", async () => {
  const fake = api({ status: 200, body: {} });
  const state = stateFor(BACK);
  assert.equal(await callbackDestination(query({ state }), true, fake.post), `${BACK}&notion=notion_unexpected`);
  assert.equal(await callbackDestination(query({ code: "", state }), true, fake.post), `${BACK}&notion=notion_unexpected`);
  assert.equal(await callbackDestination(query({ code: "c" }), true, fake.post), "/?notion=notion_unexpected");
  assert.equal(await callbackDestination(query({ code: "c", state: "" }), true, fake.post), "/?notion=notion_unexpected");
  assert.equal(fake.calls.length, 0);
});

test("a return_to that is not a path of this site never leaves the site", async () => {
  for (const evil of ["//evil.example/x", "https://evil.example/x", "/\\evil.example", "/.//evil.example"]) {
    const refused = api({ status: 400, body: { error: { code: "notion_state_invalid" } } });
    const to = await callbackDestination(query({ code: "c", state: stateFor(evil) }), true, refused.post);
    assert.equal(to, "/?notion=notion_state_invalid");
    const ok = api({ status: 200, body: { return_to: evil } });
    const landed = await callbackDestination(query({ code: "c", state: stateFor(BACK) }), true, ok.post);
    assert.equal(landed, `${BACK}&notion=connected`);
    assert.ok(isSameSitePath(landed));
  }
});

test("the code list is closed and carries every code the page words", () => {
  assert.deepEqual([...NOTION_CODES].sort(), [
    "connected",
    "notion_auth_failed",
    "notion_code_invalid",
    "notion_consent_denied",
    "notion_state_invalid",
    "notion_unavailable",
    "notion_unexpected",
    "notion_unreachable",
  ]);
});
