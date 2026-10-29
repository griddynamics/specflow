// Shared helpers for tests/production/p16-*.test.ts (not a test file: the runner glob matches *.test.ts only).
// TECH-SPECS §10.4: the stub API (tests/support/stub-api.mjs, R11: no model call) + the BUILT frontend (dev auth) + a dev login,
// and `globalThis.fetch` replaced by a wrapper that sends the relative `/api/backend/*` URLs of `lib/api.ts` (and of the `/me`
// helper) to the built server with the session cookie and a same-origin `Origin`, as a browser would. The real `lib/api.ts`,
// the real BFF and the stub API run end to end; no browser and no temporary page.
import { devLogin } from "../support/session.mjs";
import { frontendEnv, randomSecret, startFrontend } from "../support/servers.mjs";
import { startStubApi } from "../support/stub-api.mjs";
import { syntheticPdf } from "../support/pdf.mjs";

type ParseOutcome = "parsed" | "failed";
type DocStatus = "processing" | ParseOutcome;
type SummaryStatus = "none" | "running" | "succeeded";
type HookResult = undefined | Response;

export interface Sent {
  method: string;
  path: string; // e.g. /api/backend/projects?limit=100
  headers: Record<string, string>;
  body: string | null; // JSON bodies only; uploads record their size
  bytes: number;
  at: number;
  doneAt: number | null;
  faked?: boolean;
}

interface StubDoc {
  pid: string;
  row: Record<string, unknown> & { id: string; status: DocStatus; summarization_status: SummaryStatus };
}
export interface Stub {
  url: string;
  log: { method: string; path: string; headers: Record<string, string>; contentType?: string; bytes?: number; head?: string }[];
  state: { parseMs: number; summarizeMs: number; parseOutcome: ParseOutcome };
  docs: Map<string, StubDoc>;
  /** Drive sources per project (SourceOut rows; tests mutate them to move an import along). */
  sources: Map<string, Record<string, unknown>[]>;
  drive: { identityReason: string | null; email: string };
  /** sources-ux model (tests/support/stub-sources.mjs): seed a source with chosen file states, or remove one as another tab would. */
  model: { seed: (pid: string, spec: Record<string, unknown>) => string; remove: (pid: string, sid: string) => void };
  setTiming: (t: Partial<{ parseMs: number; summarizeMs: number; parseOutcome: ParseOutcome }>) => void;
  close: () => Promise<void>;
}

export interface Env {
  api: Stub;
  base: string;
  sent: Sent[];
  /** Runs before a request is forwarded; may change the stub state (e.g. the 409 document_not_parsed case). */
  hooks: ((s: Sent) => HookResult | Promise<HookResult>)[];
  setLoggedIn: (on: boolean) => void;
  stop: () => Promise<void>;
}

export async function startEnv(fePort: number, apiPort: number): Promise<Env> {
  const key = randomSecret();
  const email = "p16.user@example.test";
  const api: Stub = await startStubApi({ port: apiPort, signingKey: key, parseMs: 1500, summarizeMs: 1500 });
  const fe = await startFrontend({
    port: fePort,
    env: frontendEnv(fePort, {
      APP_ENV: "local",
      AUTH_MODE: "dev",
      API_INTERNAL_URL: api.url,
      SESSION_SECRET: randomSecret(),
      AUTH_DEV_SIGNING_KEY: key,
      LOCAL_TEST_USER_EMAIL: email,
    }),
  });
  const jar = await devLogin(fe.base, email);
  const realFetch = globalThis.fetch;
  const sent: Sent[] = [];
  const hooks: Env["hooks"] = [];
  let loggedIn = true;

  // The page's `window`, as a browser has it: lib/api.ts announces a 401 on it (UNAUTHENTICATED_EVENT; no listener here).
  Object.defineProperty(globalThis, "window", { value: new EventTarget(), configurable: true, writable: true });

  globalThis.fetch = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    let raw: string;
    if (typeof input === "string") raw = input;
    else if (input instanceof URL) raw = input.href;
    else raw = input.url;
    if (!raw.startsWith("/")) return realFetch(input, init); // absolute URLs (test helpers) pass through untouched
    const headers: Record<string, string> = {};
    new Headers(init.headers).forEach((v, k) => (headers[k] = v));
    headers.origin = fe.base; // a browser sends the page origin
    if (loggedIn) headers.cookie = jar.header();
    const body = init.body;
    let size = 0;
    if (body instanceof Blob) size = body.size;
    else if (typeof body === "string") size = body.length;
    const entry: Sent = {
      method: (init.method ?? "GET").toUpperCase(),
      path: raw,
      headers,
      body: typeof body === "string" ? body : null,
      bytes: size,
      at: Date.now(),
      doneAt: null,
    };
    sent.push(entry);
    for (const h of hooks) {
      const fake = await h(entry);
      if (fake instanceof Response) {
        // A fault injected by a test: this answer replaces the request (it never reaches the BFF).
        entry.doneAt = Date.now();
        entry.faked = true;
        return fake;
      }
    }
    try {
      const res = await realFetch(`${fe.base}${raw}`, { ...init, headers, redirect: "manual" });
      const text = await res.text(); // read fully so `doneAt` is the response end
      entry.doneAt = Date.now();
      return new Response(text === "" ? null : text, { status: res.status, headers: res.headers });
    } catch (e) {
      entry.doneAt = Date.now();
      throw e;
    }
  };

  return {
    api,
    base: fe.base,
    sent,
    hooks,
    setLoggedIn: (on) => (loggedIn = on),
    async stop() {
      globalThis.fetch = realFetch;
      Reflect.deleteProperty(globalThis, "window");
      await fe.stop();
      await api.close();
    },
  };
}

export const pdfFile = (name: string, size = 2048) => new File([new Uint8Array(syntheticPdf(size))], name, { type: "application/pdf" });
export const textFile = (name: string) => new File(["plain text, not a PDF"], name, { type: "text/plain" });

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function waitFor(what: string, pred: () => boolean, timeoutMs = 15_000): Promise<void> {
  const end = Date.now() + timeoutMs;
  while (!pred()) {
    if (Date.now() > end) throw new Error(`timed out waiting for: ${what}`);
    await sleep(20);
  }
}

/** Stub API requests (what the BFF forwarded) for a project, by action. */
export function stubCalls(api: Stub, pid: string, action: "parse" | "summarize" | "upload" | "get-doc" | "list-docs") {
  return api.log.filter((e) => {
    const p = e.path.split("?")[0];
    const docs = `/api/v1/projects/${pid}/documents`;
    if (action === "upload") return e.method === "POST" && p === docs;
    if (action === "list-docs") return e.method === "GET" && p === docs;
    if (!p.startsWith(`${docs}/`)) return false;
    const rest = p.slice(docs.length + 1).split("/");
    if (action === "get-doc") return e.method === "GET" && rest.length === 1;
    return e.method === "POST" && rest[1] === action;
  });
}

/** The endpoints the production adapter uses (frontend/README.md, TECH-SPECS §3.3, GDRIVE plan 04 B4), as stub API paths. */
const UUID = "[0-9a-f-]{36}";
export const EXISTING_ENDPOINTS: [string, RegExp][] = [
  ["GET", /^\/api\/v1\/me$/],
  ["GET", /^\/api\/v1\/projects(\?limit=100)?$/],
  ["POST", /^\/api\/v1\/projects$/],
  ["GET", new RegExp(`^/api/v1/projects/${UUID}$`)],
  ["POST", new RegExp(`^/api/v1/projects/${UUID}/documents\\?filename=[^&]+$`)],
  ["GET", /^\/api\/v1\/drive\/identity$/],
  ["GET", new RegExp(`^/api/v1/projects/${UUID}/sources(\\?batches=1)?$`)],
  ["POST", new RegExp(`^/api/v1/projects/${UUID}/sources/gdrive(/test)?$`)],
  // sources-ux (INT-SRC-0002..0004)
  ["GET", new RegExp(`^/api/v1/projects/${UUID}/sources/${UUID}/files\\?limit=200(&cursor=[^&]+)?$`)],
  ["DELETE", new RegExp(`^/api/v1/projects/${UUID}/sources/${UUID}$`)],
  ["POST", new RegExp(`^/api/v1/projects/${UUID}/sources/uploads$`)],
  ["POST", new RegExp(`^/api/v1/projects/${UUID}/sources/${UUID}/files\\?filename=[^&]+$`)],
  ["POST", new RegExp(`^/api/v1/projects/${UUID}/sources/${UUID}/end$`)],
];
export const isExistingEndpoint = (method: string, path: string) => EXISTING_ENDPOINTS.some(([m, re]) => m === method && re.test(path));

/** A JSON answer for an injected fault (status, body as given). */
export const jsonResponse = (status: number, body: string) =>
  new Response(body === "" ? null : body, { status, headers: { "content-type": "application/json" } });

/** Adds a hook for the duration of `fn`. */
export async function withHook<T>(env: Env, hook: Env["hooks"][number], fn: () => Promise<T>): Promise<T> {
  env.hooks.push(hook);
  try {
    return await fn();
  } finally {
    env.hooks.splice(env.hooks.indexOf(hook), 1);
  }
}

/** The `error.code` of a JSON error answer. */
export async function errorCode(res: Response): Promise<string> {
  return ((await res.json()) as { error: { code: string } }).error.code;
}
