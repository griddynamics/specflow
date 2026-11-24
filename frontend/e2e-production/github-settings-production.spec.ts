// GitHub integration (docs/github/INDEX.md): the Settings tab GitHub on the PRODUCTION routes in a real browser, against
// the stub API (tests/support/stub-github.mjs) through the real BFF. GitHub's own pages are intercepted and never reached: the authorize
// page answers with the redirect to the callback route, the app's install page with the redirect to the setup route. A case seeds its own
// project through the control port and puts the global GitHub knobs back after it.
import { randomBytes } from "node:crypto";
import type { Locator, Page } from "@playwright/test";
import { allowOrigin, expect, login, seedGitHub, seedMembers, stubLog, test } from "./support";

const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
const bodyOf = (r: { postData: string | null }): unknown => JSON.parse(r.postData ?? "{}");
const TAB = (pid: string) => `/projects/${pid}/settings?tab=github`;
const ACTIVE = { status: "active", login: "octo-dev" } as const;
const TOAST = "toast";
const ACCOUNT_ROW = "github-row";
const STATE = "data-state";
const TAB_URL = (pid: string) => new RegExp(`/projects/${pid}/settings\\?tab=github$`);
/** The picker row of `fullName` (its `data-blocked` says whether it can be added). */
const pickRow = (scope: Locator, fullName: string): Locator => scope.getByTestId("github-pick").filter({ hasText: fullName });

/** Answers every github.com request of `page` with a redirect to `to(url)` (the page GitHub would send the browser to). */
async function interceptGitHub(page: Page, to: (url: URL) => string): Promise<URL[]> {
  const seen: URL[] = [];
  allowOrigin(page, "https://github.com");
  await page.route(
    (url) => url.origin === "https://github.com",
    async (route) => {
      const url = new URL(route.request().url());
      seen.push(url);
      await route.fulfill({ status: 302, headers: { location: to(url) } });
    },
  );
  return seen;
}

test.describe("Settings, tab GitHub, on production routes", { tag: "@production" }, () => {
  test.afterEach(async () => {
    await seedGitHub({ reset: true });
  });

  test("the GitHub tab sits in the shell header; Connect goes to GitHub, the callback connects, toasts once and drops ?github=", async ({
    page,
    requests,
    baseURL,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("GitHub connect") });
    const seen = await interceptGitHub(
      page,
      (url) => `${baseURL ?? ""}/api/integrations/github/callback?code=stub-code&state=${url.searchParams.get("state") ?? ""}`,
    );
    const logged = (await stubLog()).length;
    await page.goto(`/projects/${id}/settings`);
    await page.getByTestId("shell-head").getByRole("button", { name: "GitHub" }).click();
    await expect(page).toHaveURL(TAB_URL(id));
    const row = page.getByTestId(ACCOUNT_ROW);
    await expect(row).toHaveAttribute(STATE, "none");
    await expect(page.getByTestId("github-add")).toBeDisabled();
    await expect(page.getByTestId("github-gate")).toHaveText("Connect GitHub to add repositories.");
    await row.getByRole("button", { name: "Connect GitHub" }).click();

    await expect(page.getByTestId(TOAST).filter({ hasText: "GitHub connected" })).toHaveCount(1);
    expect(seen).toHaveLength(1);
    expect(seen[0].pathname).toBe("/login/oauth/authorize");
    const authorize = requests.find((r) => r.method === "POST" && r.path === "/api/backend/integrations/github/authorize");
    expect(bodyOf(authorize ?? { postData: null })).toEqual({ return_to: TAB(id) });
    await expect(page).toHaveURL(TAB_URL(id));
    await expect(row).toHaveAttribute(STATE, "connected");
    await expect(row).toContainText("Connected to GitHub as octo-dev");
    await expect(page.getByTestId("github-add")).toBeEnabled();
    const log = await stubLog();
    expect(log.slice(logged).filter((e) => e.method === "POST" && e.path === "/api/v1/integrations/github/callback")).toHaveLength(1);
  });

  test("Choose repositories goes to the app's install page; the setup return sends only the state and toasts once", async ({
    page,
    requests,
    baseURL,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("GitHub install") });
    await seedGitHub({ connection: ACTIVE });
    const seen = await interceptGitHub(
      page,
      (url) =>
        `${baseURL ?? ""}/api/integrations/github/setup?installation_id=4242&setup_action=install&state=${url.searchParams.get("state") ?? ""}`,
    );
    const logged = (await stubLog()).length;
    await page.goto(TAB(id));
    await page.getByTestId("github-choose").click();
    await expect(page.getByTestId(TOAST).filter({ hasText: "GitHub repositories updated" })).toHaveCount(1);
    expect(seen[0].pathname).toMatch(/^\/apps\/[^/]+\/installations\/new$/);
    const install = requests.find((r) => r.method === "POST" && r.path === "/api/backend/integrations/github/install");
    expect(bodyOf(install ?? { postData: null })).toEqual({ return_to: TAB(id) });
    await expect(page).toHaveURL(TAB_URL(id));
    const setup = (await stubLog()).slice(logged).filter((e) => e.path === "/api/v1/integrations/github/setup");
    expect(setup).toHaveLength(1);
    expect(setup[0].path).not.toContain("4242");
  });

  test("an owner adds a repository from the picker; it clones to Cloned with its facts; Clone again and Remove ask the API", async ({
    page,
    requests,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("GitHub add") });
    await seedGitHub({ connection: ACTIVE, cloneMs: 1500 });
    await page.goto(TAB(id));
    await expect(page.getByTestId("github-repos")).toContainText("No repositories yet");
    await page.getByTestId("github-add").click();
    const dialog = page.getByRole("dialog", { name: "Add a repository" });
    await expect(dialog.getByTestId("github-pick")).toHaveCount(3);
    await expect(pickRow(dialog, "acme/monorepo")).toHaveAttribute("data-blocked", "too_large");
    await dialog.getByRole("searchbox", { name: "Search repositories" }).fill("payments");
    await expect(dialog.getByTestId("github-pick")).toHaveCount(1);
    await dialog.getByRole("button", { name: "Add acme/payments-api" }).click();
    await expect(page.getByTestId(TOAST).filter({ hasText: "Repository added." })).toHaveCount(1);
    await expect(pickRow(dialog, "acme/payments-api")).toHaveAttribute("data-blocked", "added");
    const add = requests.find((r) => r.method === "POST" && r.path === `/api/backend/projects/${id}/repositories`);
    expect(bodyOf(add ?? { postData: null })).toEqual({ github_repo_id: 101 });
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);

    const repo = page.getByTestId("github-repo");
    await expect(repo).toHaveCount(1);
    await expect(repo).toHaveAttribute(STATE, "cloned", { timeout: 10_000 }); // the list polls while the check runs
    await expect(repo.getByTestId("github-repo-facts")).toContainText("3f9c2e1 · 1,204 files · 3.2 MB");
    await expect(repo.getByRole("link", { name: "acme/payments-api" })).toHaveAttribute("href", "https://github.com/acme/payments-api");

    await repo.getByRole("button", { name: "Clone again acme/payments-api" }).click();
    await expect(page.getByTestId(TOAST).filter({ hasText: "Clone started" })).toHaveCount(1);
    expect(requests.filter((r) => r.method === "POST" && r.path.endsWith("/clone"))).toHaveLength(1);
    await expect(repo).toHaveAttribute(STATE, "cloned", { timeout: 10_000 });

    await repo.getByTestId("github-remove").click();
    const confirm = page.getByRole("dialog", { name: "Remove this repository?" });
    await confirm.getByRole("button", { name: "Keep" }).click();
    expect(requests.filter((r) => r.method === "DELETE")).toHaveLength(0);
    await repo.getByTestId("github-remove").click();
    await page.getByTestId("github-remove-confirm").click();
    await expect(page.getByTestId(TOAST).filter({ hasText: "Repository removed" })).toHaveCount(1);
    await expect(page.getByTestId("github-repo")).toHaveCount(0);
    expect(requests.filter((r) => r.method === "DELETE" && r.path.startsWith(`/api/backend/projects/${id}/repositories/`))).toHaveLength(1);
  });

  test("a failed clone check shows its reason; a member sees the list read-only with no connection row", async ({ page }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("GitHub member"), role: "collaborator" });
    await seedGitHub({
      pid: id,
      repositories: [{ full_name: "acme/web-app", clone_state: "failed", failure_code: "github_repo_not_accessible", added_by_me: false }],
    });
    await page.goto(TAB(id));
    const repo = page.getByTestId("github-repo");
    await expect(repo).toHaveAttribute(STATE, "failed");
    await expect(repo.getByTestId("github-repo-reason")).toContainText("GitHub refused access.");
    await expect(page.getByTestId(ACCOUNT_ROW)).toHaveCount(0);
    await expect(page.getByTestId("github-add")).toHaveCount(0);
    await expect(repo.getByTestId("github-clone")).toHaveCount(0);
    await expect(repo.getByTestId("github-remove")).toHaveCount(0);
  });

  test("Disconnect asks first, then forgets the connection", async ({ page, requests }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("GitHub disconnect") });
    await seedGitHub({ connection: ACTIVE });
    await page.goto(TAB(id));
    await page.getByTestId("github-disconnect").click();
    await page.getByRole("dialog", { name: "Disconnect GitHub?" }).getByRole("button", { name: "Keep" }).click();
    expect(requests.filter((r) => r.method === "DELETE")).toHaveLength(0);
    await page.getByTestId("github-disconnect").click();
    await page.getByTestId("github-disconnect-confirm").click();
    await expect(page.getByText("GitHub disconnected")).toBeVisible();
    await expect(page.getByTestId(ACCOUNT_ROW)).toHaveAttribute(STATE, "none");
    expect(requests.filter((r) => r.method === "DELETE" && r.path === "/api/backend/integrations/github")).toHaveLength(1);
  });

  test("GitHub not configured: one note, no connection row", async ({ page }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("GitHub off") });
    await seedGitHub({ enabled: false });
    await page.goto(TAB(id));
    await expect(page.getByTestId("github-unavailable")).toContainText("GitHub is not set up on this Specflow server");
    await expect(page.getByTestId(ACCOUNT_ROW)).toHaveCount(0);
  });

  test("a refused consent and a refused callback land back on the tab with their toast; a bad return_to never leaves the site", async ({
    page,
    baseURL,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("GitHub refused") });
    await seedGitHub({ callbackError: "github_code_invalid" });
    const stateFor = (r: string, p: string) =>
      `n.${Buffer.from(JSON.stringify({ u: "u1", r, p, e: 1 })).toString("base64url")}.stub-signature`;
    await page.goto(`/api/integrations/github/callback?error=access_denied&state=${stateFor(TAB(id), "authorize")}`);
    await expect(page.getByText("GitHub access was not granted.")).toBeVisible();
    await expect(page).toHaveURL(TAB_URL(id));
    await page.goto(`/api/integrations/github/callback?code=c&state=${stateFor(TAB(id), "authorize")}`);
    await expect(page.getByText("GitHub did not accept the sign-in. Try again.")).toBeVisible();
    await expect(page.getByTestId(ACCOUNT_ROW)).toHaveAttribute(STATE, "none");

    const origin = new URL(baseURL ?? "").origin;
    for (const r of ["/.//evil.example", "/%2e%2e//evil.example"]) {
      for (const path of [
        `/api/integrations/github/callback?error=x&state=${stateFor(r, "authorize")}`,
        `/api/integrations/github/setup?state=${stateFor(r, "install")}`,
      ]) {
        const res = await page.request.get(path, { maxRedirects: 0 });
        expect(res.status()).toBe(302);
        const location = new URL(res.headers().location);
        expect(location.origin, path).toBe(origin);
        expect(location.pathname).toBe("/");
      }
    }
  });

  test("the return routes without a session go to sign-in and never call the API", async ({ playwright, baseURL }) => {
    const anonymous = await playwright.request.newContext({ baseURL });
    const before = (await stubLog()).length;
    for (const path of ["/api/integrations/github/callback?code=c&state=a.b.c", "/api/integrations/github/setup?state=a.b.c"]) {
      const res = await anonymous.get(path, { maxRedirects: 0 });
      expect(res.status()).toBe(302);
      expect(new URL(res.headers().location).pathname).toBe("/login");
    }
    expect((await stubLog()).slice(before).filter((e) => e.path.includes("github"))).toHaveLength(0);
    await anonymous.dispose();
  });
});
