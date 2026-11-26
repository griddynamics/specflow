// Not a test: what the callback tests of the OAuth apps (GitHub, Linear) share: the fake API, the state, and the rules every app's callback keeps
// (sign-in without a session, a malicious `return_to`, no code or state in the destination).
import assert from "node:assert/strict";
import { test } from "node:test";
import { locationOn, type CallbackAnswer } from "@/lib/oauth-callback";

export const query = (q: Record<string, string>) => new URLSearchParams(q);
export const PUBLIC = "https://app.example";
export const EVIL = [
  "//evil.example/x",
  "https://evil.example/x",
  "/\\evil.example",
  "/.//evil.example",
  "/%2e%2e//evil.example",
  "/%2f/evil",
];

export function api(answer: CallbackAnswer | Error) {
  const calls: unknown[][] = [];
  const post = (...args: string[]) => {
    calls.push(args);
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
  };
  return { calls, post };
}

type Post = (code: string, state: string) => Promise<CallbackAnswer>;

export interface AppCallback {
  /** The query parameter the destination carries ("github"), and the API code of an invalid code. */
  param: string;
  codeInvalid: string;
  back: string;
  stateFor: (returnTo: unknown) => string;
  destination: (query: URLSearchParams, signedIn: boolean, post: Post) => Promise<string>;
}

export function appCallbackRulesTests(app: AppCallback): void {
  const { param, back, stateFor, destination } = app;
  test(`${param} callback: no session, or a 401 from the API, goes to sign-in; no code or state is ${param}_unexpected without a call`, async () => {
    const fake = api({ status: 200, body: {} });
    assert.equal(await destination(query({ code: "c", state: stateFor(back) }), false, fake.post), "/login");
    assert.equal(await destination(query({ state: stateFor(back) }), true, fake.post), `${back}&${param}=${param}_unexpected`);
    assert.equal(await destination(query({ code: "c" }), true, fake.post), `/?${param}=${param}_unexpected`);
    assert.equal(fake.calls.length, 0);
    assert.equal(await destination(query({ code: "c", state: stateFor(back) }), true, api({ status: 401, body: null }).post), "/login");
  });

  test(`${param} callback: a malicious return_to (in the state or in the answer) never sends the browser off the site`, async () => {
    for (const r of EVIL) {
      const state = stateFor(r);
      const denied = await destination(query({ error: "x", state }), true, api({ status: 200, body: {} }).post);
      const connected = await destination(query({ code: "c", state }), true, api({ status: 200, body: { return_to: r } }).post);
      assert.equal(denied, `/?${param}=${param}_consent_denied`, r);
      assert.equal(connected, `/?${param}=connected`, r);
      for (const to of [denied, connected]) assert.equal(new URL(locationOn(to, PUBLIC)).origin, PUBLIC, r);
    }
  });

  test(`${param} callback: the destination never carries the code or the state`, async () => {
    const state = stateFor(back);
    for (const answer of [
      { status: 200, body: { return_to: back } },
      { status: 400, body: { error: { code: app.codeInvalid } } },
    ]) {
      const to = await destination(query({ code: "secret-code", state }), true, api(answer).post);
      assert.ok(!to.includes("secret-code") && !to.includes(state) && !to.includes("sig"), to);
    }
  });
}
