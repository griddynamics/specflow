// Shared helpers for the browser-level specs. Secrets are read from the env FILE here, kept in
// memory and sent only as HTTP basic auth to the local Langfuse; nothing is printed.
import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { request, test as base, expect, type BrowserContext, type FileChooser, type Page, type Request } from "@playwright/test";

export { expect };

// "The network is quiet": no request started, finished or failed for `quietMs` and none is in flight. This is what
// the runner's discouraged `waitForLoadState("networkidle")` waited for (500 ms without a connection), as an explicit,
// polled condition that a spec can read.
const QUIET_MS = 500;

export async function networkQuiet(page: Page, quietMs = QUIET_MS): Promise<void> {
  const inFlight = new Set<Request>();
  let lastActivity = Date.now();
  const started = (r: Request) => {
    inFlight.add(r);
    lastActivity = Date.now();
  };
  const ended = (r: Request) => {
    inFlight.delete(r);
    lastActivity = Date.now();
  };
  page.on("request", started);
  page.on("requestfinished", ended);
  page.on("requestfailed", ended);
  try {
    await expect.poll(() => inFlight.size === 0 && Date.now() - lastActivity >= quietMs, { message: "the network went quiet" }).toBe(true);
  } finally {
    page.off("request", started);
    page.off("requestfinished", ended);
    page.off("requestfailed", ended);
  }
}

const ROOT = path.resolve(__dirname, "../..");
export const FIXTURE_PDF = path.join(ROOT, "backend/tests/data/specs/extraction.pdf");
const OUT = path.resolve(__dirname, "..", process.env.E2E_OUT ?? "logs/e2e");
export const SHOTS = path.join(OUT, "shots");
const EVIDENCE = path.join(OUT, "evidence.jsonl");
const ENV_FILE = process.env.E2E_ENV_FILE ?? path.join(ROOT, ".env.local");

/** One JSON line per fact (ids, counts, names only). */
export function evidence(entry: Record<string, unknown>): void {
  fs.mkdirSync(OUT, { recursive: true });
  fs.appendFileSync(EVIDENCE, JSON.stringify({ at: new Date().toISOString(), ...entry }) + "\n");
}

export async function shot(page: Page, name: string): Promise<void> {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
}

/** Reads only the requested keys from the env file (simple KEY=VALUE lines). */
function readEnvFile(keys: string[]): Record<string, string> {
  const wanted = new Set(keys);
  const found: Record<string, string> = {};
  for (const line of fs.readFileSync(ENV_FILE, "utf8").split("\n")) {
    const m = /^([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (m && wanted.has(m[1])) found[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
  return found;
}

export const uniqueName = (prefix: string) => `${prefix}-${Date.now().toString(36)}-${randomBytes(3).toString("hex")}`;

// ---------------------------------------------------------------- storage (AC-4.7, D6)
export interface ObjectCounts {
  project_id: string;
  before: number;
  deleted: number;
  after: number;
}

// `uv` is found on PATH unless E2E_UV names its location.
const UV_COMMAND = process.env.E2E_UV ?? "uv";

/**
 * Environment of the cleanup child: the local cloud profile (S3 on RustFS) with the host endpoint and
 * the RustFS keys from the env file. Variables already set in the shell win, as in the Makefile.
 */
function storageEnv(): NodeJS.ProcessEnv {
  const keys = readEnvFile(["RUSTFS_ACCESS_KEY", "RUSTFS_SECRET_KEY", "RUSTFS_HOST_PORT"]);
  const port = process.env.RUSTFS_HOST_PORT ?? ("RUSTFS_HOST_PORT" in keys ? keys.RUSTFS_HOST_PORT : "19000");
  return {
    ...process.env,
    SPECFLOW_CONFIG_FILE: process.env.SPECFLOW_CONFIG_FILE ?? path.join(ROOT, "config", "cloud.local.yaml"),
    AWS_ENDPOINT_URL_S3: process.env.AWS_ENDPOINT_URL_S3 ?? `http://localhost:${port}`,
    AWS_ACCESS_KEY_ID: process.env.AWS_ACCESS_KEY_ID ?? process.env.RUSTFS_ACCESS_KEY ?? keys.RUSTFS_ACCESS_KEY,
    AWS_SECRET_ACCESS_KEY: process.env.AWS_SECRET_ACCESS_KEY ?? process.env.RUSTFS_SECRET_KEY ?? keys.RUSTFS_SECRET_KEY,
  };
}

/** Counts or deletes the objects under `{STORAGE_PREFIX}projects/{pid}/` through the backend script. */
export function storageCleanup(projectId: string, countOnly: boolean): ObjectCounts {
  const args = ["--directory", path.join(ROOT, "backend"), "run", "python", "examples/storage_cleanup.py", "--project-id", projectId];
  if (countOnly) args.push("--count-only");
  const out = execFileSync(UV_COMMAND, args, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], env: storageEnv() });
  return JSON.parse(out.trim().split("\n").pop() ?? "{}") as ObjectCounts;
}

interface Fixtures {
  /** Registers a project id; its objects are deleted (and counted) when the case ends, pass or fail. */
  track: (projectId: string) => void;
}

export const test = base.extend<Fixtures>({
  track: async ({}, provide, testInfo) => {
    const ids: string[] = [];
    await provide((id) => {
      ids.push(id);
      evidence({ test: testInfo.title, event: "project.created", project_id: id });
    });
    for (const id of ids) {
      const counts = storageCleanup(id, false);
      evidence({ test: testInfo.title, event: "storage.cleanup", ...counts });
      testInfo.annotations.push({
        type: "objects",
        description: `${id}: before=${String(counts.before)} deleted=${String(counts.deleted)} after=${String(counts.after)}`,
      });
      expect(counts.after, "objects left after cleanup").toBe(0);
    }
  },
});

// ---------------------------------------------------------------- UI helpers (P17: the new production screens)
/** Dev sign-in on the design-system /login; lands on `/` (the newest project's Mission Control, or the empty state). */
export async function devLogin(page: Page): Promise<void> {
  await page.goto("/login");
  // The live specs need a stack started with AUTH_MODE=dev (APP_ENV=local): in OIDC mode /login has only "login-oidc" (AC-6.5).
  const submit = page.getByTestId("login-dev-submit");
  await expect(submit, "the stack at E2E_BASE_URL runs AUTH_MODE=dev (the dev sign-in form is shown)").toBeVisible();
  await submit.click(); // the form is pre-filled with the dev user
  await expect(page.getByTestId("app-shell")).toBeVisible();
  // `/` settles on the newest project's Mission Control, or the empty state when there is no project (O12).
  await expect(page.getByTestId("screen-mission-control").or(page.getByTestId("home-empty"))).toBeVisible();
}

/** FE-BP-03.AC4: "Sign out" from the avatar menu ends the session and shows /login. */
export async function signOut(page: Page): Promise<void> {
  await networkQuiet(page); // a BFF response in flight after the sign-out could write the session cookie again (GAPS G-P17-4)
  await page.getByRole("button", { name: /^Account/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
}

/** Creates a project through the top bar's "New" modal (name only, A9) and returns its id from the Mission Control URL. */
export async function createProject(page: Page, name: string): Promise<string> {
  const before = page.url(); // usually another project's Mission Control already: wait for a NEW one
  await page.getByTestId("topbar-new").click();
  await page.getByLabel("Name").fill(name);
  await page.getByTestId("cp-create").click();
  await page.waitForURL((u) => /^\/projects\/[0-9a-f-]{36}\/mission-control$/.test(u.pathname) && u.href !== before);
  await expect(page.getByTestId("topbar-project-name")).toHaveText(name);
  await expect(page.getByTestId("mc-empty")).toBeVisible();
  return defined(/\/projects\/([0-9a-f-]{36})\//.exec(page.url()))[1];
}

/**
 * Picks files in the Mission Control Empty state and starts the run ("Create project" on the Files selected card). The files
 * go in through the picker that the Empty state's "Upload files" button opens, as a user would choose them.
 */
export async function uploadInMissionControl(page: Page, files: Parameters<FileChooser["setFiles"]>[0]): Promise<void> {
  const chooser = page.waitForEvent("filechooser");
  await page.getByTestId("mc-empty").getByRole("button", { name: "Upload files" }).click();
  await (await chooser).setFiles(files);
  await page.getByRole("button", { name: "Create project" }).click();
}

/**
 * Waits until Mission Control is Ready, failing fast when a row or the graph step fails (no retry: every Retry of a summary
 * spends model calls). A stuck row (10 min) keeps waiting: the adapter still polls it at the back-off rate.
 */
export async function waitForReady(page: Page, timeout: number): Promise<void> {
  const screen = page.getByTestId("screen-mission-control");
  await expect
    .poll(
      async () => {
        if ((await screen.getAttribute("data-phase")) === "ready") return "ready";
        const failed = await page
          .getByTestId("mc-file-row")
          .or(page.getByTestId("mc-graph"))
          .evaluateAll((items) => items.filter((item) => item.getAttribute("data-state") === "failed").length);
        return failed > 0 ? "failed" : "waiting";
      },
      { timeout, intervals: [2_000, 5_000] },
    )
    .toMatch(/^(ready|failed)$/);
  await expect(screen, "Mission Control ends Ready (a row or the graph step failed otherwise)").toHaveAttribute("data-phase", "ready");
}

/** Calls the BFF the way the browser would (session cookie, Origin, X-Requested-With). */
export async function bffHeaders(context: BrowserContext, baseURL: string): Promise<Record<string, string>> {
  const cookies = await context.cookies(baseURL);
  return {
    cookie: cookies.map((c) => `${c.name}=${c.value}`).join("; "),
    origin: new URL(baseURL).origin,
    "x-requested-with": "specflow",
  };
}

// ---------------------------------------------------------------- 200 MiB fixture (AC-12.4)
/** Writes a synthetic file (`%PDF-` header + random bytes) of `bytes` to the temp dir; returns path + remover. */
export function syntheticPdf(bytes: number): Promise<{ file: string; remove: () => void }> {
  const file = path.join(os.tmpdir(), `specflow-e2e-${randomBytes(6).toString("hex")}.pdf`);
  const fd = fs.openSync(file, "w");
  try {
    fs.writeSync(fd, "%PDF-1.7\n");
    let left = bytes - 9;
    const chunk = 4 * 1024 * 1024;
    while (left > 0) {
      const n = Math.min(chunk, left);
      fs.writeSync(fd, randomBytes(n));
      left -= n;
    }
  } finally {
    fs.closeSync(fd);
  }
  return Promise.resolve({
    file,
    remove: () => {
      fs.rmSync(file, { force: true });
    },
  });
}

/** Streams a file as a fetch request body (1 MiB chunks), so a 200 MiB upload is never held in memory. */
export function fileBodyStream(file: string): ReadableStream<Uint8Array> {
  const handle = fs.promises.open(file, "r");
  const chunk = 1024 * 1024;
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      const open = await handle;
      const buf = Buffer.alloc(chunk);
      const { bytesRead } = await open.read(buf, 0, chunk, null);
      if (bytesRead === 0) {
        await open.close();
        controller.close();
        return;
      }
      controller.enqueue(buf.subarray(0, bytesRead));
    },
  });
}

// ---------------------------------------------------------------- Langfuse (AC-11.3, exact v2 queries)
type Row = Record<string, unknown> & {
  name?: string;
  type?: string;
  sessionId?: string;
  traceId?: string;
  usageDetails?: { input?: number };
};

export interface TraceFacts {
  traceId: string;
  agentIsRoot: boolean;
  generations: number;
  toolNames: string[];
  agentNames: string[];
  secretInIo: boolean;
  rows: number;
}

export class Langfuse {
  private readonly base: string;
  private readonly auth: string;
  private readonly secrets: string[];

  constructor() {
    const keys = [
      "LANGFUSE_BASE_URL",
      "LANGFUSE_PUBLIC_KEY",
      "LANGFUSE_SECRET_KEY",
      "LLM_GATEWAY_API_KEY",
      "AUTH_DEV_SIGNING_KEY",
      "SESSION_SECRET",
      "DATABASE_PASSWORD",
      "NEO4J_PASSWORD",
    ];
    const env = readEnvFile(keys);
    this.base = env.LANGFUSE_BASE_URL.replace(/\/$/, "");
    this.auth = "Basic " + Buffer.from(`${env.LANGFUSE_PUBLIC_KEY}:${env.LANGFUSE_SECRET_KEY}`).toString("base64");
    this.secrets = keys
      .slice(1)
      .map((k) => env[k])
      .filter((v): v is string => !!v && v.length >= 8);
  }

  private async get(params: Record<string, string>): Promise<{ data: Row[] }> {
    const client = await request.newContext();
    try {
      const res = await client.get(`${this.base}/api/public/v2/observations`, { params, headers: { authorization: this.auth } });
      expect(res.status(), "Langfuse v2 observations status").toBe(200);
      return (await res.json()) as { data: Row[] };
    } finally {
      await client.dispose();
    }
  }

  /**
   * The AGENT `summarize-document` of this workflow within 60 s: first with the exact AC-11.3 root
   * query; if that finds nothing, with the same query without `isRootObservation`, because in the
   * live stack the API's own HTTP span is the trace root and the agent is its descendant.
   */
  async traceFor(workflowId: string, startedAt: Date): Promise<TraceFacts> {
    const since = new Date(startedAt.getTime() - 5_000).toISOString();
    const startedPolling = Date.now();
    const result: { row?: Row } = {};
    let agentIsRoot = true;
    await expect
      .poll(
        async () => {
          const base = { fromStartTime: since, fields: "core,basic,trace_context", limit: "1000" };
          const isAgent = (r: Row) => r.sessionId === workflowId && r.name === "summarize-document" && r.type === "AGENT";
          result.row = (await this.get({ isRootObservation: "true", ...base })).data.find(isAgent);
          if (result.row === undefined && Date.now() - startedPolling > 6_000) {
            result.row = (await this.get(base)).data.find(isAgent);
            agentIsRoot = false;
          }
          return result.row !== undefined;
        },
        { message: "no summarize-document observation in Langfuse within 60 s", timeout: 60_000, intervals: [3_000] },
      )
      .toBe(true);
    const root = result.row;
    if (root === undefined) throw new Error("no summarize-document observation in Langfuse");
    const traceId = String(root.traceId);
    const rows = (await this.get({ traceId, fromStartTime: since, fields: "core,basic,model,usage", limit: "200" })).data;
    const withIo = JSON.stringify(await this.get({ traceId, fromStartTime: since, fields: "core,basic,model,usage,io", limit: "200" }));
    return {
      traceId,
      agentIsRoot,
      rows: rows.length,
      generations: rows.filter((r) => r.type === "GENERATION" && (r.usageDetails?.input ?? 0) > 0).length,
      toolNames: [...new Set(rows.filter((r) => r.type === "TOOL").map((r) => String(r.name)))].toSorted((a, b) => a.localeCompare(b)),
      agentNames: [...new Set(rows.filter((r) => r.type === "AGENT").map((r) => String(r.name)))].toSorted((a, b) => a.localeCompare(b)),
      secretInIo: this.secrets.some((s) => withIo.includes(s)),
    };
  }
}

/** Returns `value` once it is known to be present; fails the scenario with `what` otherwise. */
export function defined<T>(value: T | null | undefined, what = "value"): T {
  if (value === null || value === undefined) throw new Error(`expected ${what} to be present`);
  return value;
}
