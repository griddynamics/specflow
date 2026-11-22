// Test helpers (not a test file): run the stub API for one test file and send the `/api/backend/*` URLs of `lib/api.ts` to it as
// `/api/v1/*`, recording each request, so the real `lib/api.ts` and an http adapter run against the stub with no BFF and no browser.
import assert from "node:assert/strict";
import { after, before, beforeEach } from "node:test";
import { isDataError } from "@/lib/domain/errors";
import { startStubApi } from "./stub-api.mjs";

export interface Sent {
  method: string;
  path: string;
  body: string | null;
}
type Stub = Awaited<ReturnType<typeof startStubApi>>;

const urlOf = (input: RequestInfo | URL): string => {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
};

/** Replaces `globalThis.fetch` and a stub `window`; the returned function puts both back. Requests are appended to `sent`. */
function routeBackendTo(api: { url: string }, sent: Sent[]): () => void {
  const realFetch = globalThis.fetch;
  Object.defineProperty(globalThis, "window", { value: new EventTarget(), configurable: true, writable: true });
  globalThis.fetch = (input: RequestInfo | URL, init: RequestInit = {}) => {
    const raw = urlOf(input);
    if (!raw.startsWith("/api/backend/")) return realFetch(input, init);
    sent.push({ method: (init.method ?? "GET").toUpperCase(), path: raw, body: typeof init.body === "string" ? init.body : null });
    const headers = new Headers(init.headers);
    headers.set("authorization", "Bearer test");
    return realFetch(`${api.url}/api/v1/${raw.slice("/api/backend/".length)}`, { ...init, headers });
  };
  return () => {
    globalThis.fetch = realFetch;
    Reflect.deleteProperty(globalThis, "window");
  };
}

/** Starts the stub on `port` for the file's tests; `reset` runs before each test, after `sent` is emptied. `ctx.api` works once started. */
export function serveStub(port: number, reset: (api: Stub) => void) {
  let started: Stub | undefined;
  let restore: (() => void) | undefined;
  const ctx = {
    get api(): Stub {
      assert.ok(started !== undefined, "the stub API is not started");
      return started;
    },
    sent: [] as Sent[],
  };
  before(async () => {
    started = await startStubApi({ port });
    restore = routeBackendTo(started, ctx.sent);
  });
  after(async () => {
    restore?.();
    await started?.close();
  });
  beforeEach(() => {
    ctx.sent.length = 0;
    reset(ctx.api);
  });
  return ctx;
}

/** Asserts that `p` rejects with a `DataError` of this `code` (and, when given, this API code). */
export const rejectsWith = (p: Promise<unknown>, code: string, apiCode?: string) =>
  assert.rejects(
    p,
    (e) => isDataError(e) && e.code === code && (apiCode === undefined || e.detail?.apiCode === apiCode),
    `${code} ${apiCode ?? ""}`,
  );
