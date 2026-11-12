// P17 (FE-BP-03.AC9, FE-DOD-03, TECH-SPECS §10.4): the `test` every production-route spec uses. The app runs against the stub API
// (scripts/serve-production.mjs), never the real backend. It fails a case on a console error or page error (a case may allow a
// named one, e.g. the browser's "Failed to load resource" line for an expected 404), on any origin other than the app, the stub
// issuer the sign-in goes through and the two Google Fonts hosts, and on any app `/api/` path outside `/api/backend/` (the BFF) and `/api/auth/` (sign-in, sign-out).
import { randomBytes } from "node:crypto";
import { request, type Locator, type Page } from "@playwright/test";
import { test as base, expect, networkQuiet } from "../e2e/support";

export const ALLOWED_EXTRA_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com"];
export const CONTROL = process.env.E2E_PRODUCTION_CONTROL_URL ?? "http://127.0.0.1:3262";
/** The stub issuer (scripts/serve-production.mjs): the one extra origin a page may visit, during sign-in and sign-out. */
export const ISSUER_ORIGIN = process.env.E2E_PRODUCTION_ISSUER_URL ?? "http://127.0.0.1:3263";

export interface RequestLog {
  method: string;
  path: string;
  search: string;
  origin: string;
  postData: string | null;
  headers: Record<string, string>;
}
export interface StubEntry {
  method: string;
  path: string;
  contentType?: string;
  bytes?: number;
  head?: string;
}

// The allow-list lives per page so a case can extend it after the guard is installed (see allowConsoleErrors).
const allowConsoleList = new WeakMap<Page, RegExp[]>();

export const test = base.extend<{ requests: RequestLog[]; guards: undefined }>({
  requests: async ({}, use) => {
    await use([]);
  },
  guards: [
    async ({ page, requests, baseURL }, use) => {
      const problems: string[] = [];
      const appOrigin = new URL(baseURL ?? "http://127.0.0.1:3260").origin;
      page.on("console", (m) => {
        if (m.type() !== "error") return;
        if (!(allowConsoleList.get(page) ?? []).some((re) => re.test(m.text()))) problems.push(`console error: ${m.text()}`);
      });
      page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
      page.on("request", (r) => {
        const u = new URL(r.url());
        if (u.protocol === "data:" || u.protocol === "blob:") return;
        requests.push({
          method: r.method(),
          path: u.pathname,
          search: u.search,
          origin: u.origin,
          postData: r.postData(),
          headers: r.headers(),
        });
        if (u.origin !== appOrigin && u.origin !== ISSUER_ORIGIN && !ALLOWED_EXTRA_HOSTS.includes(u.hostname))
          problems.push(`foreign origin: ${u.origin}`);
        if (u.origin === appOrigin && u.pathname.startsWith("/api/") && !/^\/api\/(?:backend|auth)\//.test(u.pathname))
          problems.push(`API path: ${u.pathname}`);
      });
      await use(undefined);
      expect(problems, "console, origin and API-path guards").toEqual([]);
    },
    { auto: true },
  ],
});

/** Accepts console errors matching `re` on this page (for example the browser line of an expected 404 or 401 response). */
export function allowConsoleErrors(page: Page, re: RegExp): void {
  const list = allowConsoleList.get(page) ?? [];
  list.push(re);
  allowConsoleList.set(page, list);
}

export { expect };

/** Sets the theme cookie (TECH-SPECS §7). Call BEFORE page.goto. */
export async function setTheme(page: Page, theme: "light" | "dark"): Promise<void> {
  const baseURL = test.info().project.use.baseURL ?? "http://127.0.0.1:3260";
  await page.context().addCookies([{ name: "specflow-theme", value: theme, url: baseURL }]);
}

/** FE-BP-03.AC5: the OIDC sign-in button on /login; the stub issuer signs the default user in and `/` follows. */
export async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByTestId("login-oidc").click();
  await expect(page.getByTestId("app-shell")).toBeVisible();
  // `/` settles on the newest project's Mission Control, or the empty state when there is no project (O12).
  await expect(page.getByTestId("screen-mission-control").or(page.getByTestId("home-empty"))).toBeVisible();
}

/** The top bar's current project name (inside the project switcher's trigger). */
export const topbarProjectName = (page: Page): Locator => page.getByTestId("topbar-project-name");

/** The top bar's project switcher: the button that shows the current project name and opens the "Switch project" menu. */
export const projectSwitcher = (page: Page): Locator => page.getByRole("button").filter({ has: topbarProjectName(page) });

/** The app shell's sidebar (the Sidebar landmark, labelled "Primary"). */
export const rail = (page: Page): Locator => page.getByRole("complementary", { name: "Primary" });

/** Owner decision 2026-10-15: "Create project" is the last item of the top bar's project dropdown (no separate New button). */
export async function openCreateProject(page: Page): Promise<void> {
  await projectSwitcher(page).click();
  await page.getByRole("menuitem", { name: "Create project" }).click();
}

/**
 * Creates a project through the top bar's Create project modal (name only, A9) and returns its id. Waits for a NEW Mission Control
 * URL: the page usually sits on another project's Mission Control already, so "any Mission Control URL" would match too early.
 */
export async function createViaNew(page: Page, name: string): Promise<string> {
  const before = page.url();
  await openCreateProject(page);
  await page.getByLabel("Name").fill(name);
  await page.getByTestId("cp-create").click();
  await page.waitForURL((u) => /^\/projects\/[0-9a-f-]{36}\/mission-control$/.test(u.pathname) && u.href !== before);
  await expect(topbarProjectName(page)).toHaveText(name);
  await expect(page.getByTestId("mcs-empty")).toBeVisible();
  return defined(/\/projects\/([0-9a-f-]{36})\//.exec(new URL(page.url()).pathname + "/"))[1];
}

/** Calls the BFF the way the browser does (the page's session cookie, Origin, X-Requested-With). The caller names the body's shape. */
export async function bff(
  page: Page,
  method: "GET" | "POST",
  path: string,
  body?: unknown,
  contentType = "application/json",
): Promise<{ status: number; json: unknown }> {
  const baseURL = test.info().project.use.baseURL ?? "http://127.0.0.1:3260";
  let data: string | Buffer | undefined;
  if (body !== undefined) data = contentType === "application/json" ? JSON.stringify(body) : (body as Buffer);
  const res = await page.request.fetch(`${baseURL}/api/backend${path}`, {
    method,
    headers: { origin: new URL(baseURL).origin, "x-requested-with": "specflow", "content-type": contentType },
    data,
  });
  return { status: res.status(), json: (await res.json()) as unknown };
}

export const createProjectApi = async (page: Page, name: string): Promise<string> =>
  ((await bff(page, "POST", "/projects", { name })).json as { id: string }).id;

/** Uploads a document through the BFF, like a page left before the run finished (the server parses it, K5). */
export async function seedDocument(page: Page, pid: string, filename: string): Promise<string> {
  const up = await bff(
    page,
    "POST",
    `/projects/${pid}/documents?filename=${encodeURIComponent(filename)}`,
    syntheticPdf(),
    "application/pdf",
  );
  return (up.json as { document: { id: string } }).document.id;
}

/** Local files: picks a PDF in the sources screen's hidden file input (the Add source menu opens the same picker). */
export async function pickLocalPdf(page: Page, name: string, size = 4096): Promise<Buffer> {
  const buffer = syntheticPdf(size);
  await page.getByTestId("mcs-upload-input").setInputFiles({ name, mimeType: "application/pdf", buffer });
  return buffer;
}

/** Local files: picks any file in the hidden input; `mimeType` is what the browser would say (it may be empty or odd). */
export async function pickLocalFile(page: Page, name: string, mimeType: string, buffer: Buffer): Promise<void> {
  await page.getByTestId("mcs-upload-input").setInputFiles({ name, mimeType, buffer });
}

/** A tiny synthetic file of an accepted type: the API's first-byte check, then filler; never a real document. */
export function syntheticDocument(extension: string, size = 512): Buffer {
  const zip = Buffer.from("PK\x03\x04", "latin1");
  const text: Record<string, string> = {
    ".html": "<!doctype html><title>t</title><p>x</p>\n",
    ".md": "# Title\n\ntext\n",
    ".csv": "a,b\n1,2\n",
  };
  if (extension === ".pdf") return syntheticPdf(size);
  if (extension in text) return Buffer.concat([Buffer.from(text[extension]), Buffer.from(" ".repeat(size))]);
  return Buffer.concat([zip, randomBytes(size)]);
}

/** Saves a Drive folder through the BFF (the stub's reserved ids; the source stays in `listing`). */
export const saveDriveApi = (page: Page, pid: string, folderId = "fake-synthetic-30") =>
  bff(page, "POST", `/projects/${pid}/sources/gdrive`, { link: `https://drive.google.com/drive/folders/${folderId}` });

type SourceCardKind = "upload" | "gdrive";
type SourceCardState = "processing" | "done" | "done_with_problems" | "failed";

/**
 * A source card found by the kind it shows: a Local files card is named "Local files" (its open button), a Drive card's second line
 * reads "Google Drive" (FR-SRC-0001).
 */
function sourceCard(page: Page, kind: SourceCardKind): Locator {
  const cards = page.getByTestId("source-card");
  return kind === "upload"
    ? cards.filter({ has: page.getByRole("button", { name: "Local files", exact: true }) })
    : cards.filter({ has: page.getByText("Google Drive", { exact: true }) });
}

/** The source card whose open button (the card's name) reads `name`; source names are unique within a fresh project. */
export const namedSourceCard = (page: Page, name: string): Locator =>
  page.getByTestId("source-card").filter({ has: page.getByRole("button", { name, exact: true }) });

/** Waits until the project's card of `kind` is visible, carries that kind and, when given, is in `state`. Returns the card. */
export async function expectSourceCard(
  page: Page,
  kind: SourceCardKind,
  { state, timeout }: { state?: SourceCardState; timeout?: number } = {},
): Promise<Locator> {
  const card = sourceCard(page, kind);
  if (state !== undefined) await expect(card).toHaveAttribute("data-state", state, { timeout });
  await expect(card).toHaveAttribute("data-kind", kind, { timeout });
  await expect(card).toBeVisible({ timeout });
  return card;
}

/** Waits until the project's Local files card is Done and the project is Ready (>= 1 processed document, stub parse 1.5 s). */
export async function expectUploadReady(page: Page): Promise<void> {
  await expectSourceCard(page, "upload", { state: "done", timeout: 30_000 });
}

/** A tiny synthetic "PDF" (magic bytes + filler): enough for the stub API, never a real document. */
export const syntheticPdf = (size = 2048): Buffer =>
  Buffer.concat([Buffer.from("%PDF-1.7\n"), Buffer.alloc(size, 0x41), Buffer.from(`\n%${randomBytes(8).toString("hex")}\n%%EOF\n`)]);

/** One call to the serve script's test control port (the runner's request client, never the app's `fetch`); the JSON answer. */
async function control(method: "GET" | "POST", path: string, data?: Record<string, unknown>): Promise<unknown> {
  const client = await request.newContext({ baseURL: CONTROL });
  try {
    const res = method === "GET" ? await client.get(path) : await client.post(path, { data });
    return (await res.json()) as unknown;
  } finally {
    await client.dispose();
  }
}

/** The stub API's request log (method, path, content type, bytes, first bytes; no headers), from the serve script's control port. */
export async function stubLog(): Promise<StubEntry[]> {
  return (await control("GET", "/log")) as StubEntry[];
}

/** Stub knobs (parseMs, summarizeMs, parseOutcome, graphMs, graphOutcome, graphStartFail, planMs, workFail, workReadFail); a null time = never finishes. */
export async function stubTiming(t: Record<string, number | string | null>): Promise<void> {
  await control("POST", "/timing", t);
}

export const STUB_TIMING = {
  parseMs: 1500,
  summarizeMs: 1500,
  parseOutcome: "parsed",
  graphMs: 1500,
  graphOutcome: "succeeded",
  graphStartFail: null,
  planMs: 2000,
  workFail: null,
  workReadFail: null,
};

/** Seeds a source with chosen file states in the stub (the case control port; see tests/support/stub-sources.mjs `seed`). Returns its id. */
export async function seedSource(pid: string, source: Record<string, unknown>): Promise<string> {
  return ((await control("POST", "/seed-source", { pid, source })) as { id: string }).id;
}

/** Seeds the findings and claims of a project in the stub (rows as the API answers them; see tests/support/stub-api.mjs `workRoute`). */
export async function seedWork(
  pid: string,
  work: { findings?: Record<string, unknown>[]; claims?: Record<string, unknown>[] },
): Promise<void> {
  await control("POST", "/seed-work", { pid, work });
}

/** Removes a source in the stub as if another tab had (the details modal then finds it gone). */
export async function removeSourceElsewhere(pid: string, sid: string): Promise<void> {
  await control("POST", "/remove-source", { pid, sid });
}

/** What the stub issuer saw of the sign-out (the serve script's `GET /oidc`; never a token). */
export interface OidcObservations {
  endSession: boolean;
  endSessionCalls: number;
  endSessionRequests: {
    hasIdTokenHint: boolean;
    idTokenHintValid: boolean;
    postLogoutRedirectUri: string;
    clientId?: string;
    ok: boolean;
  }[];
  signIns: number;
  refreshCount: number;
}

export async function oidcObservations(): Promise<OidcObservations> {
  return (await control("GET", "/oidc")) as OidcObservations;
}

/** Zeroes the issuer's end-session counter and its other observations (call before the action under test). */
export async function resetOidc(): Promise<void> {
  await control("POST", "/oidc/reset");
}

/** Switches the issuer's `end_session_endpoint` on or off (off = a provider without one, as Google). */
export async function setEndSession(enabled: boolean): Promise<void> {
  await control("POST", "/oidc/end-session", { enabled });
}

/** Returns `value` once it is known to be present; fails the scenario with `what` otherwise. */
export function defined<T>(value: T | null | undefined, what = "value"): T {
  if (value === null || value === undefined) throw new Error(`expected ${what} to be present`);
  return value;
}

/** The requests that went to the backend through the BFF. */
export const calls = <T extends { path: string }>(requests: T[]): T[] => requests.filter((r) => r.path.startsWith("/api/backend/"));

/** The explicit allow-list while a Ready gate answers: the shell's project reads and ONE project-scoped GET of the sources status. */
export function allowedGateCalls(pid: string): (r: { method: string; path: string }) => boolean {
  const shell = new RegExp(`^/api/backend/projects(/${pid})?$`);
  const docs = new RegExp(`^/api/backend/projects/${pid}/sources$`);
  return (r) => r.method === "GET" && (shell.test(r.path) || docs.test(r.path));
}

/** After a gate screen: nothing keeps polling, the backend calls since `from` stay inside the allow-list, no write, no parse or summarize. */
export async function expectGateNetwork(page: Page, requests: RequestLog[], from: number, pid: string): Promise<void> {
  await networkQuiet(page, 2_500); // no request for longer than a poll interval: nothing keeps polling
  const made = calls(requests.slice(from));
  const allowed = allowedGateCalls(pid);
  expect(
    made.filter((r) => !allowed(r)).map((r) => `${r.method} ${r.path}`),
    "backend calls outside the allow-list",
  ).toEqual([]);
  expect(made.filter((r) => r.method !== "GET")).toEqual([]);
  expect(made.filter((r) => /\/(?:parse|summarize)$/.test(r.path))).toEqual([]);
  expect(
    made.filter((r) => new RegExp(`^/api/backend/projects/${pid}/sources$`).test(r.path)).length,
    "one GET of the sources status (the gate)",
  ).toBeLessThanOrEqual(1);
}

// ---- the Build knowledge button (plans/connect-build-kg-button): helpers p17 and p18 share ----

export type DocumentStatus = "uploaded" | "processing" | "parsed" | "failed" | "ingested" | "partially_ingested"; // backend DOCUMENT_STATUSES

export const kbBuild = (page: Page): Locator => page.getByTestId("kb-build");
export const confirmDialog = (page: Page): Locator => page.getByTestId("kb-build-dialog");

/** A project with one parsed document (the stub parses at once), on its Mission Control with the first build offered. */
export async function readyProject(page: Page, name: string): Promise<string> {
  await login(page);
  await stubTiming({ parseMs: 0 });
  const pid = await createProjectApi(page, `${name} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`);
  const id = await seedDocument(page, pid, "spec.pdf");
  await expect
    .poll(async () => ((await bff(page, "GET", `/projects/${pid}/documents/${id}`)).json as { status: DocumentStatus }).status)
    .toBe("parsed");
  await page.goto(`/projects/${pid}/mission-control`);
  await expect(kbBuild(page)).toHaveAttribute("data-mode", "first");
  return pid;
}

/** Opens the confirm the keyboard way: focus the button, press Enter. */
export async function openConfirm(page: Page): Promise<void> {
  await kbBuild(page).focus();
  await page.keyboard.press("Enter");
  await expect(confirmDialog(page)).toBeVisible();
}

/** Build -> Confirm, driven by the keyboard (focus the button, Enter; focus Confirm, Enter). */
export async function startBuild(page: Page): Promise<void> {
  await openConfirm(page);
  await page.getByTestId("kb-build-dialog-confirm").focus();
  await page.keyboard.press("Enter");
}

/**
 * Lets the page render twice (two animation frames, no timer). Call it after the event a negative check is about, so a toast or a
 * dialog that would appear one render later is already there when `toHaveCount(0)` runs.
 */
export async function settled(page: Page): Promise<void> {
  const nextFrame = () => page.evaluate(() => new Promise<number>((done) => requestAnimationFrame(done)));
  await nextFrame();
  await nextFrame();
}
