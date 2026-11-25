// P17 (FE-BP-03.AC9, FE-DOD-03, TECH-SPECS §10.4): the `test` every production-route spec uses. The app runs against the stub API
// (scripts/serve-production.mjs), never the real backend. It fails a case on a console error or page error (a case may allow a
// named one, e.g. the browser's "Failed to load resource" line for an expected 404), on any origin other than the app, the stub
// issuer the sign-in goes through and the two Google Fonts hosts (a case may allow one more, e.g. Atlassian's consent page it intercepts),
// and on any app `/api/` path outside `/api/backend/` (the BFF), `/api/auth/` (sign-in, sign-out) and the Atlassian consent callback.
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
const allowOriginList = new WeakMap<Page, string[]>();
/** The OAuth return routes a browser may open on the app (lib/oauth-route.ts): GitHub's and Atlassian's redirects land there. */
const OAUTH_RETURN_ROUTES = ["/api/integrations/atlassian/callback", "/api/integrations/github/callback", "/api/integrations/github/setup"];

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
        // The font hosts of the Graphico screens (V2) are not the app's traffic: their URLs carry an `@` by design (`wght@400`), which
        // the specs that check "no email in a URL" must not mistake for data. They stay allowed origins; they are just not logged here.
        if (ALLOWED_EXTRA_HOSTS.includes(u.hostname)) return;
        requests.push({
          method: r.method(),
          path: u.pathname,
          search: u.search,
          origin: u.origin,
          postData: r.postData(),
          headers: r.headers(),
        });
        const extra = allowOriginList.get(page) ?? [];
        if (u.origin !== appOrigin && u.origin !== ISSUER_ORIGIN && !ALLOWED_EXTRA_HOSTS.includes(u.hostname) && !extra.includes(u.origin))
          problems.push(`foreign origin: ${u.origin}`);
        if (
          u.origin === appOrigin &&
          u.pathname.startsWith("/api/") &&
          !/^\/api\/(?:backend|auth)\//.test(u.pathname) &&
          !OAUTH_RETURN_ROUTES.includes(u.pathname)
        )
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

/** Accepts requests to `origin` on this page (a case that intercepts it with `page.route`, so nothing leaves the machine). */
export function allowOrigin(page: Page, origin: string): void {
  allowOriginList.set(page, [...(allowOriginList.get(page) ?? []), origin]);
}

export { expect };

/** Sets the theme cookie (TECH-SPECS §7). Call BEFORE page.goto. */
export async function setTheme(page: Page, theme: "light" | "dark"): Promise<void> {
  const baseURL = test.info().project.use.baseURL ?? "http://127.0.0.1:3260";
  await page.context().addCookies([{ name: "specflow-theme", value: theme, url: baseURL }]);
}

/** The V2 header's navigation (every V2 screen has it; the V1 shell does not). */
export const v2Header = (page: Page): Locator => page.getByRole("navigation", { name: "App" });

/** FE-BP-03.AC5: /login starts the OIDC sign-in by itself; the stub issuer signs the default user in and `/` (the V2 screens) follows. */
export async function loginV2(page: Page): Promise<void> {
  await page.goto("/login");
  await expect(v2Header(page)).toBeVisible();
}

/**
 * Signs in and opens a project's V1 Mission Control, for the specs of the V1 screens (they stay at their own URLs until the clean-up).
 * `/` is V2 now, so the way in is the one `/` used to take: the newest project of the list, or a new one when the list is empty.
 */
export async function login(page: Page): Promise<void> {
  await loginV2(page);
  const listed = (await bff(page, "GET", "/projects?limit=100")).json as { items: { id: string }[] };
  const pid = listed.items.at(0)?.id ?? (await createProjectApi(page, `First project ${Date.now().toString(36)}`));
  await page.goto(`/projects/${pid}/mission-control`);
  await expect(page.getByTestId("app-shell")).toBeVisible();
  await expect(page.getByTestId("screen-mission-control")).toBeVisible();
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

/** Stub knobs (parseMs, summarizeMs, parseOutcome, graphMs, graphOutcome, graphStartFail, planMs, workFail, workReadFail, gapFail, sourcesFail); a null time = never finishes. */
export async function stubTiming(t: Record<string, number | string | null>): Promise<void> {
  await control("POST", "/timing", t);
}

export const STUB_TIMING = {
  parseMs: 1500,
  summarizeMs: 1500,
  parseOutcome: "parsed",
  graphMs: 1500,
  gapMs: 1000,
  graphOutcome: "succeeded",
  graphStartFail: null,
  planMs: 2000,
  workFail: null,
  workReadFail: null,
  gapFail: null,
  sourcesFail: null,
};

/** The V2 Integrations stub (`POST /seed-integrations`, tests/support/stub-integrations.mjs `seed`): the Slack knobs and, with `pid`, a project's bindings. */
export interface SeedIntegrations extends Record<string, unknown> {
  /** Put the Slack knobs back first. */
  reset?: boolean;
  pid?: string;
  slackAvailable?: boolean;
  /** "ok", or the API code the next Test connection answers with. */
  slackTest?: string;
  atlassian?: { by?: { user_id: string; name: string } | null; workspace?: { id: string; name: string }; health?: string } | null;
  github?: { by?: { user_id: string; name: string } | null; workspace?: { id: string; name: string }; health?: string } | null;
  slack?: { channel?: { id: string; name: string; private: boolean }; by?: { user_id: string; name: string } | null } | null;
}
export async function seedIntegrations(spec: SeedIntegrations): Promise<void> {
  await control("POST", "/seed-integrations", spec);
}

/** The Slack channel of a project in the stub (`POST /seed-slack`): `channel: null` = none bound yet; `available: false` = Slack is off on the server. */
export interface SeedSlack extends Record<string, unknown> {
  pid: string;
  channel: { id: string; name: string; private: boolean } | null;
  by?: { user_id: string; name: string } | null;
  available?: boolean;
  /** "ok", or the API code the next Test connection answers with. */
  test?: string;
}
export async function seedSlack(spec: SeedSlack): Promise<void> {
  await control("POST", "/seed-slack", spec);
}

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

/** Seeds the gaps (full `GapView` rows, plus stub-only `stories` and `documents` id lists), the latest gap run, the noise rules and the
 *  corpus documents (`{document_id, lineage, path, title, markdown}`, the Documents tab) of a project in the stub. */
export async function seedGaps(
  pid: string,
  seed: {
    gaps?: Record<string, unknown>[];
    run?: Record<string, unknown>;
    rules?: Record<string, unknown>[];
    corpus?: Record<string, unknown>[];
  },
): Promise<void> {
  await control("POST", "/seed-gaps", { pid, ...seed });
}

/** A running gap run records one more site (stub): its gaps join the project's (same id replaces) and the run counts the site. */
export async function recordGaps(pid: string, gaps: Record<string, unknown>[]): Promise<void> {
  await control("POST", "/record-gaps", { pid, gaps });
}

/** The update build of the applied answers finished (stub): every applying gap of the project resolves with its answer applied. */
export async function settleGaps(pid: string): Promise<void> {
  await control("POST", "/settle-gaps", { pid });
}

/** The stub's collaborator knobs; `resetPeopleState` puts them back so one case never decides another. */
export const PEOPLE_STATE = { allowedDomains: ["example.test"], claimsReleased: 0, claimsFailed: false };
export interface SeedMembers extends Record<string, unknown> {
  name: string;
  role?: "owner" | "collaborator";
  members?: { email: string; name?: string | null; role?: string }[];
  invites?: string[];
  state?: Partial<typeof PEOPLE_STATE>;
}
export const resetPeopleState = (): Promise<void> => seedMembersState(PEOPLE_STATE);
const seedMembersState = async (state: Partial<typeof PEOPLE_STATE>): Promise<void> => {
  await control("POST", "/seed-members", { state });
};

/** Seeds a project in the stub where the caller is owner or collaborator, with chosen members and invites (stub-members.mjs `seed`). */
export async function seedMembers(spec: SeedMembers): Promise<{ id: string; name: string }> {
  return (await control("POST", "/seed-members", spec)) as { id: string; name: string };
}

/** The Jira settings of one seeded project in the stub (`POST /seed-jira`, tests/support/stub-jira.mjs `seed`). `available: false` = no Atlassian account; `target: null` removes the target; `access` scripts the next access checks. */
export interface SeedJira extends Record<string, unknown> {
  pid: string;
  available?: boolean;
  target?: {
    jira_project: string;
    epic_key: string | null;
    enabled: boolean;
    created_epic_key?: string | null;
    sync?: Record<string, unknown>;
    acting?: "me" | { connected_by_name: string | null; connected_by_me: boolean; status: "active" | "expired" | "missing" };
  } | null;
  access?: "ok" | { reason: string; field?: string | null };
  /** Global Atlassian knobs (docs/jira-sync/oauth.md): the server mode, the caller's own connection and the next callback refusal. */
  mode?: "token" | "oauth" | null;
  connection?: { status: "active" | "expired"; account_name: string } | null;
  callbackError?: string | null;
}
export async function seedJira(spec: SeedJira): Promise<void> {
  await control("POST", "/seed-jira", spec);
}

/** The Jira gap channel of one seeded project (`POST /seed-jira-gaps`, tests/support/stub-jira-gaps.mjs `seed`). `target: null` removes it; `access` scripts the next checks. */
export interface SeedJiraGaps extends Record<string, unknown> {
  pid: string;
  available?: boolean;
  target?: {
    jira_project: string;
    grouping: "one" | "area";
    enabled: boolean;
    story_key?: string | null;
    posted?: number;
    answered?: number;
    error_code?: string | null;
  } | null;
  access?: "ok" | { reason: string; field?: string | null };
}
export async function seedJiraGaps(spec: SeedJiraGaps): Promise<void> {
  await control("POST", "/seed-jira-gaps", spec);
}

/** The GitHub Projects destination of one seeded project (`POST /seed-github-projects`, tests/support/stub-github-projects.mjs `seed`). */
export interface SeedGitHubProjects extends Record<string, unknown> {
  pid?: string;
  available?: boolean;
  /** null = the repository belongs to a user. */
  organization?: string | null;
  target?: {
    repository_id: string;
    repository: string;
    project_node_id: string;
    project_title: string;
    enabled?: boolean;
    sync?: Record<string, unknown>;
  } | null;
  access?: "ok" | { reason: string; field?: string | null };
}
export async function seedGitHubProjects(spec: SeedGitHubProjects): Promise<void> {
  await control("POST", "/seed-github-projects", spec);
}

/** The GitHub stub (`POST /seed-github`, tests/support/stub-github.mjs `seed`): global knobs and, with `pid`, a project's repositories. */
export interface SeedGitHub extends Record<string, unknown> {
  /** Put every global knob back first. */
  reset?: boolean;
  pid?: string;
  repositories?: Record<string, unknown>[];
  enabled?: boolean;
  connection?: { status: "active" | "expired"; login: string } | null;
  callbackError?: string | null;
  setupError?: string | null;
  available?: Record<string, unknown>[];
  truncated?: boolean;
  availableError?: string | null;
  /** null keeps a clone check running. */
  cloneMs?: number | null;
  cloneOutcome?: string;
}
export async function seedGitHub(spec: SeedGitHub): Promise<void> {
  await control("POST", "/seed-github", spec);
}

/** Builds the knowledge graph of a Ready project through the BFF and waits for it (the stub runs it without a model). */
export async function buildGraph(page: Page, pid: string): Promise<void> {
  expect((await bff(page, "POST", `/projects/${pid}/graph_build`, { reasoning_level: 3 })).status).toBe(202);
  await expect
    .poll(
      async () =>
        ((await bff(page, "GET", `/projects/${pid}/graph_build`)).json as { status: "idle" | "running" | "succeeded" | "failed" }).status,
      { timeout: 10_000 },
    )
    .toBe("succeeded");
}

/** Builds the knowledge graph of a Ready project and then its plan through the BFF, waiting for each (the stub runs both without a model). */
export async function buildGraphAndPlan(page: Page, pid: string): Promise<void> {
  await buildGraph(page, pid);
  expect((await bff(page, "POST", `/projects/${pid}/plan`, { replan: false })).status).toBe(202);
  await expect
    .poll(async () => ((await bff(page, "GET", `/projects/${pid}/plan`)).json as { has_plan: boolean }).has_plan, { timeout: 15_000 })
    .toBe(true);
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

/** After a gate screen: nothing keeps polling, the backend calls since `from` stay inside the allow-list (the gate's by default), no write, no parse or summarize. */
export async function expectGateNetwork(
  page: Page,
  requests: RequestLog[],
  from: number,
  pid: string,
  allowed: (r: { method: string; path: string }) => boolean = allowedGateCalls(pid),
): Promise<void> {
  await networkQuiet(page, 2_500); // no request for longer than a poll interval: nothing keeps polling
  const made = calls(requests.slice(from));
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
