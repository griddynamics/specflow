// P17 (FE-BP-03.AC1-AC10, FE-BP-08, FE-BP-09, FE-DS-07, FE-DOD-03): the new screens on the PRODUCTION routes in a real browser,
// against the stub API through the real BFF (scripts/serve-production.mjs; no backend, no model call). Every case signs in with the
// dev form and makes its own projects, so the shared stub never decides an outcome; the specs run one at a time (workers 1).
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import { networkQuiet } from "../e2e/support";
import {
  allowConsoleErrors,
  bff,
  createProjectApi,
  createViaNew,
  expect,
  expectSourceCard,
  expectUploadReady,
  login,
  pickLocalPdf,
  projectSwitcher,
  rail,
  saveDriveApi,
  seedDocument,
  setTheme,
  STUB_TIMING,
  stubLog,
  stubTiming,
  test,
  topbarProjectName,
  defined,
} from "./support";

test.describe("P17 screens on production routes", { tag: "@production" }, () => {
  const MC = /\/projects\/([0-9a-f-]{36})\/mission-control$/;
  const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
  const backendCalls = (requests: { path: string }[]) => requests.filter((r) => r.path.startsWith("/api/backend/"));

  test.afterEach(async () => {
    await stubTiming(STUB_TIMING); // a case that changed the stub timing never leaks it into the next one
  });

  test("FE-BP-03.AC5, AC1 (O12, G01-14): dev sign-in on /login lands on `/`, which opens the newest project's Mission Control", async ({
    page,
  }) => {
    await login(page);
    await expect(page).not.toHaveURL(/\/login/);
    await createProjectApi(page, unique("Older"));
    const newest = await createProjectApi(page, unique("Newest"));
    await page.goto("/");
    await expect(page).toHaveURL(new RegExp(`/projects/${newest}/mission-control$`));
    await expect(page.getByTestId("screen-mission-control")).toHaveAttribute("data-phase", "no_sources");
    await expect(topbarProjectName(page)).toHaveText(/^Newest /);
  });

  test("FE-BP-03.AC2 (O12): with no project `/` shows the empty state; New creates through POST /projects with the name only and opens the Empty Mission Control", async ({
    page,
    requests,
  }) => {
    await login(page);
    // The stub is shared by the run, so "no project yet" is the list endpoint answering an empty page for this one load.
    await page.route("**/api/backend/projects?limit=100", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [], next_cursor: null }) }),
    );
    await page.goto("/");
    await expect(page.getByTestId("home-empty")).toBeVisible();
    await expect(page.getByTestId("home-empty")).toContainText("No projects yet");
    await page.unroute("**/api/backend/projects?limit=100");
    await page.getByTestId("home-new").click();
    const modal = page.getByTestId("create-project-modal");
    await expect(modal).toBeVisible();
    const name = unique("Created in the browser");
    await page.getByLabel("Name").fill(name);
    // Owner decision 2026-10-15: the form asks only for the name and the contributors; contributors stay UI-only (A9).
    await expect(modal.getByRole("textbox")).toHaveCount(1); // Name; no link fields, no access-check field, no tracker switches
    await expect(modal.getByRole("combobox")).toHaveCount(1); // Contributors
    await expect(modal.getByRole("switch")).toHaveCount(0);
    await page.getByTestId("cp-create").click();
    await expect(page).toHaveURL(MC);
    await expect(page.getByTestId("mcs-empty")).toBeVisible();
    const posts = requests.filter((r) => r.method === "POST" && r.path === "/api/backend/projects");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0].postData ?? "{}")).toEqual({ name });
    await expect(topbarProjectName(page)).toHaveText(name);
  });

  test("FE-BP-08, K4, K5, Q12: create -> Local files -> the card collects, the server parses, Ready; the upload is the raw PDF; GitHub is not offered; Build knowledge says not available yet", async ({
    page,
    requests,
  }) => {
    await login(page);
    const pid = await createViaNew(page, unique("Pipeline"));
    const pdf = await pickLocalPdf(page, "requirements.pdf", 8192);
    // The Local files card (K4), collecting while the stub parses (1.5 s), then collected (the status is read every 3 s).
    await expectSourceCard(page, "upload");
    await expectUploadReady(page);
    await expect(page.getByTestId("kb-build")).toHaveAttribute("data-mode", "first");
    await page.getByTestId("kb-build").click();
    await expect(page.getByText("Building knowledge is not available yet.")).toBeVisible(); // Q12
    await expect(page.getByText(/\bdemo\b/i)).toHaveCount(0);
    await page.getByTestId("mcs-add-source").click();
    await expect(page.getByRole("menuitem", { name: /Google Drive/ })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: /Local files/ })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: /GitHub/ })).toHaveCount(0); // K3
    await page.keyboard.press("Escape");
    // INT-SRC-0004: one batch: start, the raw PDF, end. Every call carries the CSRF header.
    const sources = `/api/backend/projects/${pid}/sources`;
    const posts = requests.filter((r) => r.method === "POST" && r.path.startsWith(`${sources}/`));
    expect(posts.map((r) => r.path.replace(/\/sources\/[0-9a-f-]{36}/, "/sources/:sid"))).toEqual([
      `${sources}/uploads`,
      `${sources}/:sid/files`,
      `${sources}/:sid/end`,
    ]);
    expect(JSON.parse(posts[0].postData ?? "{}")).toEqual({ expected_files: 1 });
    expect(posts.every((r) => r.headers["x-requested-with"] === "specflow")).toBe(true);
    const up = posts[1];
    expect(up.search).toBe("?filename=requirements.pdf");
    expect(up.headers["content-type"]).toBe("application/pdf");
    expect(
      requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/documents`),
      "not the legacy upload",
    ).toHaveLength(0);
    const log = await stubLog();
    const stored = log.filter(
      (e) =>
        e.method === "POST" &&
        /^\/api\/v1\/projects\/[0-9a-f-]+\/sources\/[0-9a-f-]{36}\/files\?filename=requirements\.pdf$/.test(e.path) &&
        e.path.includes(pid),
    );
    expect(stored).toHaveLength(1);
    expect(stored[0]).toMatchObject({ contentType: "application/pdf", bytes: pdf.length, head: "%PDF-" });
    expect(
      log.filter((e) => e.path.startsWith(`/api/v1/projects/${pid}/documents/`) && /\/(?:parse|summarize)$/.test(e.path)),
      "no browser-driven parse or summarize (K5)",
    ).toEqual([]);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth),
      "no horizontal scroll",
    ).toBe(true);
  });

  test("FE-BP-03 (switcher, FE-SHELL-02): the switcher lists the real projects newest first and opens the picked project's Mission Control", async ({
    page,
  }) => {
    await login(page);
    const nameA = unique("Switch A");
    const nameB = unique("Switch B");
    const a = await createProjectApi(page, nameA);
    const b = await createProjectApi(page, nameB);
    await page.goto(`/projects/${a}/mission-control`);
    await expect(topbarProjectName(page)).toHaveText(nameA);
    await projectSwitcher(page).click();
    const items = page.getByRole("menu", { name: "Switch project" }).getByRole("menuitemradio");
    const list = ((await bff(page, "GET", "/projects?limit=100")).json as { items: { id: string; name: string }[] }).items;
    const names = list.map((p) => p.name);
    await expect(items, "the switcher shows the API list, in its order").toHaveText(names);
    expect(names.indexOf(defined(list.find((p) => p.id === b)).name)).toBeLessThan(
      names.indexOf(defined(list.find((p) => p.id === a)).name),
    );
    await expect(page.getByRole("menuitemradio", { name: nameA, exact: true })).toHaveAttribute("aria-checked", "true");
    await page.getByRole("menuitemradio", { name: nameB, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${b}/mission-control$`));
    await expect(topbarProjectName(page)).toHaveText(nameB);
  });

  test("FE-BP-03.AC3, AC4 (O12): the avatar menu shows name and email from GET /me and Sign out, no Reset demo; Sign out ends the session", async ({
    page,
    requests,
  }) => {
    await login(page);
    const pid = await createProjectApi(page, unique("Menu"));
    const from = requests.length;
    await page.goto(`/projects/${pid}/mission-control`);
    await page.getByRole("button", { name: "Account: dev" }).click();
    const menu = page.getByRole("menu", { name: "Account" });
    await expect(menu.getByText("dev", { exact: true }), "the header's name").toBeVisible();
    await expect(menu.getByText("dev@example.test", { exact: true }), "the header's email").toBeVisible();
    await expect(menu.getByRole("menuitem")).toHaveText(["Sign out"]);
    await expect(menu.getByRole("menuitem", { name: "Reset demo" })).toHaveCount(0);
    expect(
      requests.slice(from).filter((r) => r.path === "/api/backend/me"),
      "one /me read per page load",
    ).toHaveLength(1);
    await networkQuiet(page); // no BFF response in flight that could write the session cookie again (GAPS G-P17-4)
    await menu.getByRole("menuitem", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login\?signedout=1$/);
    await expect(page.getByTestId("login-signed-out")).toHaveCount(0); // D21: no signed-out note
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Specflow");
    await expect(page.getByTestId("login-dev-submit")).toBeVisible();
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(page).toHaveURL(/\/login$/); // FE-BP-03.AC6: the session is gone
  });

  test("K5, FR-GDUI-0021: a project with a file still being parsed shows the Local files card collecting, Build knowledge locked, and sends no parse or summarize", async ({
    page,
    requests,
  }) => {
    await login(page);
    const pid = await createProjectApi(page, unique("Unfinished"));
    await stubTiming({ parseMs: null }); // the stub never finishes the parse
    await seedDocument(page, pid, "uploaded only.pdf");
    await stubTiming(STUB_TIMING);
    const work = async () =>
      (await stubLog()).filter(
        (e) => e.method === "POST" && e.path.startsWith(`/api/v1/projects/${pid}/documents/`) && /\/(?:parse|summarize)$/.test(e.path),
      ).length;
    await page.goto(`/projects/${pid}/mission-control`);
    await expectSourceCard(page, "upload", { state: "processing" });
    await expect(page.getByTestId("kb-build")).toHaveAttribute("aria-disabled", "true");
    await expect(page.getByTestId("kb-detail")).toContainText("Build knowledge starts after all files are imported.");
    // More than one poll interval: the page reads the sources status again (its poll), and the network goes quiet after that answer.
    const statusReads = () => requests.filter((r) => r.method === "GET" && r.path === `/api/backend/projects/${pid}/sources`).length;
    const readsSoFar = statusReads();
    await expect.poll(statusReads, { message: "the page polled the sources status again" }).toBeGreaterThan(readsSoFar);
    await networkQuiet(page);
    expect(await work(), "no parse or summarize request, ever").toBe(0);
  });

  // Decisions is covered by decisions-gate-production.spec.ts (D19): a not-Ready project shows the Ready gate there, a Ready one "No decisions yet".
  test("FE-BP-03.AC8, SPL-DEMO-02: SpecBot says it is not available yet and Connections shows no connectors; neither sends a /api/backend request for content", async ({
    page,
    requests,
  }) => {
    await login(page);
    const pid = await createProjectApi(page, unique("Content"));
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(page.getByTestId("screen-mission-control")).toBeVisible();
    // Connections is hidden from the sidebar (owner, 2026-10-15); its page is checked by URL below. SpecBot is opened from the sidebar.
    const fromSpecBot = requests.length;
    await rail(page).getByRole("button", { name: "SpecBot", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/specbot$`));
    await expect(page.getByTestId("screen-specbot")).toBeVisible();
    // The greeting names the signed-in user (GET /me: "dev"); the sub-line says SpecBot is not available yet; no suggestions.
    await expect(page.getByTestId("specbot-greeting")).toHaveText("Hi dev");
    await expect(page.getByText("SpecBot is not available yet.")).toBeVisible();
    // Only the shell's own reads (the project gate on a project route): nothing for the screen's content.
    for (const p of backendCalls(requests.slice(fromSpecBot)).map((r) => r.path))
      expect(p, `SpecBot: ${p}`).toMatch(new RegExp(`^/api/backend/projects(/${pid})?$`));
    await expect(rail(page).getByRole("button", { name: "Connections", exact: true })).toHaveCount(0);
    const from = requests.length;
    await page.goto("/connections");
    await expect(page.getByTestId("screen-connections")).toHaveAttribute("data-state", "ready");
    await expect(page.getByTestId("con-card")).toHaveCount(0);
    await expect(page.getByTestId("con-empty")).toBeVisible();
    // A full load: only the shell's own reads (/me and the project list); nothing for the connectors.
    for (const p of backendCalls(requests.slice(from)).map((r) => r.path))
      expect(p, `Connections: ${p}`).toMatch(/^\/api\/backend\/(projects|me)$/);
  });

  test("FE-BP-03.AC10, AC7 (PA27, §1.8): an unknown id (404) and a non-UUID id (stub 404, real API 422) and the old hello routes lead back to Mission Control", async ({
    page,
  }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 404/);
    await login(page);
    const newest = await createProjectApi(page, unique("Target"));
    for (const bad of ["00000000-0000-0000-0000-000000000000", "not-a-uuid"]) {
      await page.goto(`/projects/${bad}/specbot`);
      await expect(page).toHaveURL(new RegExp(`/projects/${newest}/mission-control$`)); // -> `/` -> the newest project
    }
    for (const old of [`/projects/${newest}`, `/projects/${newest}/documents/00000000-0000-0000-0000-000000000001`]) {
      await page.goto(old);
      await expect(page).toHaveURL(new RegExp(`/projects/${newest}/mission-control$`));
    }
  });

  const signedOut = async (page: Page) => {
    const res = await page.request.get("/api/auth/session");
    const body = (await res.json()) as { user?: unknown } | null;
    return body?.user === undefined || body.user === null;
  };

  test("FE-BP-02, TECH-SPECS §1.7, review M1: concurrent 401s from the BFF sign the user out once and land on /login without a loop", async ({
    page,
    requests,
  }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 401/);
    await login(page);
    const pid = await createProjectApi(page, unique("Unauthorised"));
    // The API rejects the session: every BFF call (/me, the list, the project gate, the snapshot) answers 401 at about the same time.
    await page.route("**/api/backend/**", (route) =>
      route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "unauthenticated", message: "unauthenticated", details: {} } }),
      }),
    );
    const loginLoads: string[] = [];
    page.on("request", (r) => {
      if (r.resourceType() === "document" && new URL(r.url()).pathname === "/login") loginLoads.push(r.url());
    });
    const from = requests.length;
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(page).toHaveURL(/\/login\?signedout=1$/, { timeout: 15_000 });
    await networkQuiet(page, 2_500); // no request for 2.5 s: a loop would navigate again in this time
    const after = requests.slice(from);
    expect(
      after.filter((r) => r.path === "/api/backend/me").length + after.filter((r) => r.path.startsWith("/api/backend/projects")).length,
      "several 401s",
    ).toBeGreaterThan(1);
    const signouts = after.filter((r) => r.method === "POST" && r.path === "/api/auth/signout").length;
    expect(signouts, "one sign-out routine (at most 3 attempts)").toBeGreaterThanOrEqual(1);
    expect(signouts).toBeLessThanOrEqual(3);
    expect(loginLoads, "one landing on /login, no loop").toHaveLength(1);
    await page.unroute("**/api/backend/**");
    expect(await signedOut(page), "the session is gone").toBe(true);
    await page.goto("/");
    await expect(page).toHaveURL(/\/login$/); // signed out, not only redirected
  });

  test("review M1, GAPS G-P17-4: a poll answer that lands AFTER the sign-out request does not bring the session back", async ({
    page,
    requests,
  }) => {
    await login(page);
    const pid = await createProjectApi(page, unique("Late poll"));
    await saveDriveApi(page, pid); // the source stays in `listing` on the stub: Mission Control reads the status every 3 s
    // Hold the next poll of the sources status until the browser has sent /api/auth/signout, then let it reach the BFF: its response
    // (Auth.js `auth()` wrapper) arrives after the sign-out with a fresh session cookie.
    let armed = false;
    let held = false;
    let signoutSeen!: () => void;
    const signout = new Promise<void>((r) => (signoutSeen = r));
    page.on("request", (r) => {
      if (r.method() === "POST" && new URL(r.url()).pathname === "/api/auth/signout") signoutSeen();
    });
    await page.route(
      (url) => url.pathname === `/api/backend/projects/${pid}/sources`,
      async (route) => {
        if (route.request().method() !== "GET" || !armed || held) return route.continue();
        held = true;
        await signout;
        await route.continue();
      },
    );
    await page.goto(`/projects/${pid}/mission-control`);
    await expectSourceCard(page, "gdrive");
    armed = true;
    await expect.poll(() => held, { timeout: 10_000 }).toBe(true); // a poll is now waiting in the browser
    await page.getByRole("button", { name: "Account: dev" }).click();
    await page.getByRole("menuitem", { name: "Sign out" }).click();
    await expect(page.getByTestId("signing-out")).toBeVisible();
    await expect(page).toHaveURL(/\/login\?signedout=1$/, { timeout: 15_000 });
    await expect(page.getByTestId("login-page")).toBeVisible();
    await expect(page.getByTestId("login-still-signed-in")).toHaveCount(0); // the held poll did not keep the session (the signedOut(page) check below)
    const signouts = requests.filter((r) => r.method === "POST" && r.path === "/api/auth/signout").length;
    expect(signouts).toBeGreaterThanOrEqual(1);
    expect(signouts).toBeLessThanOrEqual(3);
    expect(
      requests.filter((r) => r.path === `/api/backend/projects/${pid}/sources` && r.method === "GET").length,
      "the held poll was sent",
    ).toBeGreaterThan(0);
    test.info().annotations.push({ type: "sign-out attempts", description: String(signouts) }); // 2 = the late answer restored the session once and the routine ended it again
    expect(await signedOut(page), "the late answer did not restore the session").toBe(true);
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(page).toHaveURL(/\/login$/);
  });

  test("review m3: /login shows one fixed message for a failed sign-in and never echoes the error parameter", async ({ page }) => {
    await page.goto("/login?error=%3Cb%3EX-INJECTED%3C%2Fb%3E");
    await expect(page.getByTestId("login-error")).toHaveText("Sign-in did not work. Try again.");
    await expect(page.getByTestId("login-page").getByRole("alert")).toHaveCount(1);
    // The rendered page never shows the value (Next.js keeps the URL in its own router payload; that is not page content).
    expect(await page.getByRole("document").innerText()).not.toContain("X-INJECTED"); // the rendered text of <html>, i.e. of the body
    expect(await page.getByTestId("login-page").innerHTML()).not.toContain("X-INJECTED");
    await page.goto("/login?error=CredentialsSignin");
    await expect(page.getByTestId("login-error")).toBeVisible();
    await page.goto("/login");
    await expect(page.getByTestId("login-error")).toHaveCount(0);
  });

  test("review M1: /login?signedout=1 with a session that is still valid shows a note instead of bouncing back to `/`", async ({
    page,
  }) => {
    await login(page);
    await page.goto("/login?signedout=1");
    await expect(page).toHaveURL(/\/login\?signedout=1$/);
    await expect(page.getByTestId("login-still-signed-in")).toContainText("You are still signed in");
    await page.goto("/login");
    await expect(page).not.toHaveURL(/\/login/); // without the flag a signed-in user still goes to `/`
  });

  test.describe("FE-DS-07 on production routes", () => {
    test.use({ colorScheme: "dark" });

    test("FE-DS-07.AC1: with the OS in dark mode and no cookie, <html> is light and the page ground is the light token; /login too", async ({
      page,
    }) => {
      const ground = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      await page.goto("/login");
      await expect(page.getByRole("document")).toHaveAttribute("data-theme", "light");
      const loginGround = await ground();
      await login(page);
      const pid = await createProjectApi(page, unique("Light"));
      await page.goto(`/projects/${pid}/mission-control`);
      await expect(page.getByRole("document")).toHaveAttribute("data-theme", "light");
      expect(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("light");
      expect(await ground()).toBe(loginGround);
      expect(await ground()).not.toBe("rgb(18, 20, 23)");
    });

    test("FE-DS-07.AC2: the specflow-theme cookie makes <html> and the page ground dark", async ({ page }) => {
      await setTheme(page, "dark");
      await login(page);
      const pid = await createProjectApi(page, unique("Dark"));
      await page.goto(`/projects/${pid}/mission-control`);
      await expect(page.getByRole("document")).toHaveAttribute("data-theme", "dark");
      expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe("rgb(18, 20, 23)");
      expect(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("dark");
    });
  });
});
