// The return-route cases every code-flow OAuth app shares (lib/oauth-callback.ts `codeCallbackDestination`): success, the
// provider's own `?error=`, each API code, an unknown answer, the API down, no session, a malicious `return_to`. Each app's
// test runs `oauthReturnCases(...)` with its `?<param>=` name, its codes and its `callbackDestination`. No server here.
import assert from "node:assert/strict";
import { locationOn, type CallbackAnswer } from "@/lib/oauth-callback";

type Destination = (
  query: URLSearchParams,
  signedIn: boolean,
  post: (code: string, state: string) => Promise<CallbackAnswer>,
) => Promise<string>;

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

type Case = readonly [name: string, run: () => Promise<void>];

export function oauthReturnCases(param: string, codes: readonly string[], destination: Destination): Case[] {
  const cases: Case[] = [];
  const test = (name: string, run: () => Promise<void>) => cases.push([name, run]);
  const denied = `${param}_consent_denied`;
  const unexpected = `${param}_unexpected`;

  test(`success: code and state go to the API once; the browser lands on the API's return_to with ?${param}=connected`, async () => {
    const fake = api({ status: 200, body: { connection: {}, return_to: BACK } });
    const state = stateFor(BACK);
    assert.equal(await destination(query({ code: "c-1", state }), true, fake.post), `${BACK}&${param}=connected`);
    assert.deepEqual(fake.calls, [["c-1", state]]);
  });

  test(`the provider's ?error= never reaches the API and is ${denied} on the state's return_to`, async () => {
    const fake = api({ status: 200, body: {} });
    assert.equal(
      await destination(query({ error: "access_denied", state: stateFor(BACK) }), true, fake.post),
      `${BACK}&${param}=${denied}`,
    );
    assert.equal(fake.calls.length, 0);
  });

  test(`each API code lands with that code; any other answer, and the API down, is ${unexpected}`, async () => {
    const state = stateFor(BACK);
    for (const [status, suffix] of [
      [400, "state_invalid"],
      [400, "code_invalid"],
      [502, "unreachable"],
      [502, "auth_failed"],
      [503, "unavailable"],
    ] as const) {
      const code = `${param}_${suffix}`;
      assert.ok(codes.includes(code), code);
      assert.equal(
        await destination(query({ code: "c", state }), true, api({ status, body: { error: { code } } }).post),
        `${BACK}&${param}=${code}`,
      );
    }
    for (const answer of [
      { status: 422, body: { error: { code: "validation_failed" } } },
      { status: 500, body: null },
      { status: 400, body: { error: { code: "connected" } } },
      { status: 400, body: { error: { code: "github_code_invalid" } } },
    ]) {
      assert.equal(await destination(query({ code: "c", state }), true, api(answer).post), `${BACK}&${param}=${unexpected}`);
    }
    assert.equal(await destination(query({ code: "c", state }), true, api(new TypeError("down")).post), `${BACK}&${param}=${unexpected}`);
  });

  test(`no session, or a 401 from the API, goes to sign-in; no code or state is ${unexpected} without a call`, async () => {
    const fake = api({ status: 200, body: {} });
    assert.equal(await destination(query({ code: "c", state: stateFor(BACK) }), false, fake.post), "/login");
    assert.equal(await destination(query({ state: stateFor(BACK) }), true, fake.post), `${BACK}&${param}=${unexpected}`);
    assert.equal(await destination(query({ code: "c" }), true, fake.post), `/?${param}=${unexpected}`);
    assert.equal(fake.calls.length, 0);
    assert.equal(await destination(query({ code: "c", state: stateFor(BACK) }), true, api({ status: 401, body: null }).post), "/login");
  });

  test("a malicious return_to (in the state or in the answer) never sends the browser off the site", async () => {
    const site = "https://app.example";
    for (const r of EVIL) {
      const state = stateFor(r);
      const refused = await destination(query({ error: "x", state }), true, api({ status: 200, body: {} }).post);
      const connected = await destination(query({ code: "c", state }), true, api({ status: 200, body: { return_to: r } }).post);
      assert.equal(refused, `/?${param}=${denied}`, r);
      assert.equal(connected, `/?${param}=connected`, r);
      for (const to of [refused, connected]) assert.equal(new URL(locationOn(to, site)).origin, site, r);
    }
  });
  return cases;
}
