// COLLABORATORS: the project Settings screen (tab General) on the PRODUCTION routes in a real browser, against the stub API through
// the real BFF. The stub keeps the people of each project (tests/support/stub-members.mjs); a case seeds its own project through the
// control port, so the shared stub never decides an outcome. Proves FR-PRJ-UI-01..08 (docs/projects/requirements.md) and, for the tab
// Jira, FR-JIRA-UI-01, UI-03, UI-05 (docs/jira-sync/requirements.md), and the Atlassian connection of the oauth mode
// (docs/jira-sync/oauth.md#jira-tab): connect through the consent (Atlassian's page is intercepted, never reached), the callback route,
// its toast and disconnect. The tab Slack (docs/slack/gap-answering.md "The channel flow") shows only the project's own channel and
// the command that connects one from inside Slack; its API answer is given here (`page.route`), as the stub has no Slack.
import { randomBytes } from "node:crypto";
import type { Locator, Page } from "@playwright/test";
import {
  allowConsoleErrors,
  allowOrigin,
  expect,
  login,
  projectSwitcher,
  rail,
  resetPeopleState,
  seedJira,
  seedMembers,
  stubLog,
  test,
  topbarProjectName,
} from "./support";

const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
const ROW_X = "settings-row-x";
const ADD_ROW = "settings-add";
const bodyOf = (r: { postData: string | null }): unknown => JSON.parse(r.postData ?? "{}");
const SETTINGS = (pid: string) => new RegExp(`/projects/${pid}/settings$`);
const JIRA_PATH = (pid: string) => `/api/backend/projects/${pid}/jira`;
const projectField = (page: Page): Locator => page.getByRole("textbox", { name: "Jira space key" });
const epicField = (page: Page): Locator => page.getByRole("textbox", { name: "Epic key" });
const syncSwitch = (page: Page): Locator => page.getByRole("switch", { name: "Sync to Jira" });

test.describe("Project Settings on production routes", { tag: "@production" }, () => {
  test.afterEach(async () => {
    await resetPeopleState();
    // The Jira switch and the Atlassian knobs are global in the stub: put them back.
    await seedJira({ pid: "none", available: true, mode: "token", connection: null, callbackError: null });
  });

  test("FR-PRJ-UI-01, UI-02: the sidebar opens Settings; the General tab sits in the shell header row, not in the body", async ({
    page,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Tab"), members: [{ email: "alice.anders@example.test", name: "Alice Anders" }] });
    await page.goto(`/projects/${id}/mission-control`);
    await rail(page).getByRole("button", { name: "Settings" }).click();
    await expect(page).toHaveURL(SETTINGS(id));
    const screen = page.getByTestId("screen-settings");
    await expect(screen).toBeVisible();
    const tab = page.getByTestId("shell-head").getByRole("button", { name: "General" });
    await expect(tab).toBeVisible();
    await expect(tab).toHaveAttribute("aria-pressed", "true");
    await expect(screen.getByText("General", { exact: true })).toHaveCount(0); // the body has no tab row of its own
    await expect(screen.getByRole("heading", { level: 1 })).toHaveCount(0);
  });

  test("FR-PRJ-UI-03, UI-04, UI-05, UI-06: owner adds a known person and a new email, sees the count, removes and cancels with a confirm", async ({
    page,
    requests,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Owner"), members: [{ email: "bob.brown@example.test", name: "Bob Brown" }] });
    await page.goto(`/projects/${id}/settings`);
    const rows = page.getByTestId("settings-row");
    await expect(page.getByTestId("settings-count")).toHaveText("2 of 50");
    await expect(rows.first()).toContainText("Owner");
    await expect(rows.first().getByTestId(ROW_X)).toHaveCount(0); // the owner row has no x (UI-03)

    const add = page.getByTestId(ADD_ROW).getByRole("combobox");
    await add.fill("alan.turner@example.test"); // known: becomes a member
    await page.getByTestId("settings-add-button").click();
    await expect(rows.filter({ hasText: "Alan Turner" })).toHaveCount(1);
    await add.fill("brand.new@example.test"); // never signed in: an invite
    await page.getByTestId("settings-add-button").click();
    const invite = rows.filter({ hasText: "brand.new@example.test" });
    await expect(invite).toContainText("Invited");
    await expect(page.getByTestId("settings-count")).toHaveText("4 of 50");
    const posts = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${id}/members`);
    expect(posts.map(bodyOf)).toEqual([{ email: "alan.turner@example.test" }, { email: "brand.new@example.test" }]);
    expect(requests.some((r) => r.path.includes("@") || r.search.includes("@"))).toBe(false); // NFR-PRJ-02: no email in a URL

    // Remove asks first: Cancel sends nothing, the confirm removes.
    const deletes = () => requests.filter((r) => r.method === "DELETE");
    await rows.filter({ hasText: "Bob Brown" }).getByTestId(ROW_X).click();
    await expect(page.getByTestId("settings-dialog")).toContainText("Bob Brown");
    await page.getByTestId("settings-confirm-cancel").click();
    await expect(page.getByTestId("settings-dialog")).toHaveCount(0);
    expect(deletes()).toHaveLength(0);
    await rows.filter({ hasText: "Bob Brown" }).getByTestId(ROW_X).click();
    await page.getByTestId("settings-confirm").click();
    await expect(rows.filter({ hasText: "Bob Brown" })).toHaveCount(0);
    expect(deletes()).toHaveLength(1);
    // Cancel invite, same pattern.
    await invite.getByTestId(ROW_X).click();
    await page.getByTestId("settings-confirm").click();
    await expect(invite).toHaveCount(0);
    await expect(page.getByTestId("settings-count")).toHaveText("2 of 50");
  });

  test("FR-PRJ-UI-03, SUG: typing 3 characters lists suggestions; the keyboard picks one; Esc closes only the list; the request body holds the text and project, no URL an email", async ({
    page,
    requests,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Suggest"), members: [{ email: "bob.brown@example.test", name: "Bob Brown" }] });
    await page.goto(`/projects/${id}/settings`);
    const add = page.getByTestId(ADD_ROW).getByRole("combobox");
    const list = page.getByRole("listbox", { name: "Suggested people" });
    const suggestions = () => requests.filter((r) => r.method === "POST" && r.path === "/api/backend/users/suggestions");
    await add.click();
    await add.pressSequentially("al");
    await expect(list).toHaveCount(0); // under 3 characters: no list, no request
    expect(suggestions()).toHaveLength(0);
    await add.pressSequentially("a");
    await expect(list).toBeVisible();
    await expect(list.getByRole("option")).toContainText(["Alan Turner"]);
    // Esc closes only the list: the screen and the typed text stay.
    await add.press("Escape");
    await expect(list).toHaveCount(0);
    await expect(page.getByTestId("screen-settings")).toBeVisible();
    await expect(add).toHaveValue("ala");
    // The keyboard opens it again and picks: the field then holds the address.
    await add.press("ArrowDown");
    await expect(list).toBeVisible();
    await add.press("Enter");
    await expect(list).toHaveCount(0);
    await expect(add).toHaveValue("alan.turner@example.test");
    expect(suggestions().map(bodyOf)).toEqual([{ q: "ala", project_id: id }]);
    expect(requests.some((r) => r.path.includes("@") || r.search.includes("@"))).toBe(false); // NFR-PRJ-02
  });

  test("FR-PRJ-UI-10: an email the sign-in refuses and an already added one show their text under the field", async ({ page }) => {
    allowConsoleErrors(page, /status of (409|422)/);
    await login(page);
    const { id } = await seedMembers({ name: unique("Errors"), invites: ["waiting@example.test"] });
    await page.goto(`/projects/${id}/settings`);
    const add = page.getByTestId(ADD_ROW).getByRole("combobox");
    await add.fill("someone@refused.test");
    await page.getByTestId("settings-add-button").click();
    await expect(page.getByTestId(ADD_ROW)).toContainText("This email domain cannot sign in to SpecFlow.");
    await add.fill("waiting@example.test");
    await page.getByTestId("settings-add-button").click();
    await expect(page.getByTestId(ADD_ROW)).toContainText("Already invited.");
    await expect(page.getByTestId("settings-count")).toHaveText("2 of 50");
  });

  test("FR-PRJ-UI-07: renaming the project updates the project switcher without a reload", async ({ page, requests }) => {
    await login(page);
    const oldName = unique("Before rename");
    const { id } = await seedMembers({ name: oldName });
    await page.goto(`/projects/${id}/settings`);
    await expect(topbarProjectName(page)).toHaveText(oldName);
    const newName = unique("After rename");
    const pageLoads = () => requests.filter((r) => r.path === `/projects/${id}/settings` && r.method === "GET");
    const loads = pageLoads().length;
    await page.getByTestId("settings-name").getByRole("textbox").fill(newName);
    await page.getByTestId("settings-save").click();
    await expect(topbarProjectName(page)).toHaveText(newName);
    await projectSwitcher(page).click();
    const items = page.getByRole("menu", { name: "Switch project" }).getByRole("menuitemradio");
    await expect(items.filter({ hasText: newName })).toHaveCount(1);
    await expect(items.filter({ hasText: oldName })).toHaveCount(0);
    expect(pageLoads()).toHaveLength(loads); // no page reload
    expect(requests.filter((r) => r.method === "PATCH").map(bodyOf)).toEqual([{ name: newName }]);
  });

  test("FR-PRJ-UI-08, D29: a collaborator sees the list without name block, add row or x; Leave redirects to `/` and the project is gone from the switcher", async ({
    page,
    requests,
  }) => {
    await login(page);
    const name = unique("Left behind");
    const { id } = await seedMembers({
      name,
      role: "collaborator",
      members: [{ email: "dev@example.test", name: "dev" }],
    });
    // A newer project, so `/` has somewhere else to land.
    const other = await seedMembers({ name: unique("Stay here") });
    await page.goto(`/projects/${id}/settings`);
    await expect(page.getByTestId("screen-settings")).toBeVisible();
    await expect(page.getByTestId("settings-name")).toHaveCount(0);
    await expect(page.getByTestId(ADD_ROW)).toHaveCount(0);
    await expect(page.getByTestId(ROW_X)).toHaveCount(0);
    await expect(page.getByTestId("screen-settings").getByRole("textbox")).toHaveCount(0); // no name field, no email field (UI-04)
    await expect(page.getByTestId("settings-row").first()).toContainText("Owner");
    await projectSwitcher(page).click();
    await expect(page.getByRole("menuitemradio", { name, exact: true })).toHaveCount(1);
    await page.keyboard.press("Escape");

    await page.getByTestId("settings-leave").click();
    await expect(page.getByTestId("settings-dialog")).toBeVisible();
    await page.getByTestId("settings-confirm").click();
    await expect(page).not.toHaveURL(SETTINGS(id));
    await expect(page.getByTestId("app-shell")).toBeVisible();
    await expect(page.getByTestId("screen-mission-control").or(page.getByTestId("home-empty"))).toBeVisible();
    expect(requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${id}/leave`)).toHaveLength(1);
    await projectSwitcher(page).click();
    const menu = page.getByRole("menu", { name: "Switch project" });
    await expect(menu.getByRole("menuitemradio", { name, exact: true })).toHaveCount(0);
    await expect(menu.getByRole("menuitemradio", { name: other.name, exact: true })).toHaveCount(1);
  });

  test("FR-JIRA-UI-01: the shell header offers General and Jira; ?tab=jira selects Jira", async ({ page }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Jira tab") });
    await seedJira({ pid: id, target: null });
    await page.goto(`/projects/${id}/settings?tab=jira`);
    const head = page.getByTestId("shell-head");
    await expect(head.getByRole("button", { name: "General" })).toBeVisible();
    await expect(head.getByRole("button", { name: "Jira" })).toHaveAttribute("aria-pressed", "true");
    await expect(head.getByRole("button", { name: "General" })).toHaveAttribute("aria-pressed", "false");
    await expect(page.getByTestId("jira-form")).toBeVisible();
    await head.getByRole("button", { name: "General" }).click();
    await expect(page.getByTestId("settings-name")).toBeVisible();
    await expect(page.getByTestId("jira-form")).toHaveCount(0);
    await page.goto(`/projects/${id}/settings?tab=nonsense`); // an unknown tab still opens General
    await expect(head.getByRole("button", { name: "General" })).toHaveAttribute("aria-pressed", "true");
  });

  test("FR-JIRA-UI-02, UI-03: the owner types the keys, checks access, saves, then the switch turns on and sends one PUT with enabled true", async ({
    page,
    requests,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Jira owner") });
    await seedJira({ pid: id, target: null, access: "ok" });
    await page.goto(`/projects/${id}/settings?tab=jira`);
    const primary = page.getByTestId("jira-primary");
    const sync = syncSwitch(page);
    const puts = () => requests.filter((r) => r.method === "PUT" && r.path === JIRA_PATH(id));
    await expect(primary).toHaveText("Check access");
    await expect(sync).toBeDisabled(); // no saved target yet
    await projectField(page).fill("abc");
    await epicField(page).fill("ABC-12");
    await primary.click();
    await expect(page.getByTestId("jira-check")).toContainText("Jira space");
    await expect(primary).toHaveText("Save");
    expect(puts()).toHaveLength(0); // the check saves nothing
    await primary.click();
    await expect(page.getByText("Jira target saved")).toBeVisible();
    await expect(primary).toBeDisabled(); // the typed keys are the saved target
    await expect(sync).toBeEnabled();
    await expect(sync).not.toBeChecked();
    expect(puts().map(bodyOf)).toEqual([{ jira_project: "ABC", epic_key: "ABC-12", enabled: false }]);

    await sync.click();
    await expect(sync).toBeChecked();
    expect(puts().map(bodyOf)).toEqual([
      { jira_project: "ABC", epic_key: "ABC-12", enabled: false },
      { jira_project: "ABC", epic_key: "ABC-12", enabled: true },
    ]);
    const log = await stubLog();
    expect(log.filter((e) => e.method === "PUT" && e.path === `/api/v1/projects/${id}/jira`)).toHaveLength(2); // the stub saw the same two
    expect(log.filter((e) => e.method === "POST" && e.path === `/api/v1/projects/${id}/jira/access-check`)).toHaveLength(1);
    // Editing a key brings the check back.
    await projectField(page).fill("XYZ");
    await expect(primary).toHaveText("Check access");
    await expect(primary).toBeEnabled();
  });

  test("FR-JIRA-UI-02: a failed check shows its reason under the field it names and keeps the button on Check access", async ({ page }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Jira failed") });
    await seedJira({ pid: id, target: null, access: { reason: "jira_not_found", field: "jira_project" } });
    await page.goto(`/projects/${id}/settings?tab=jira`);
    await projectField(page).fill("NOPE");
    await page.getByTestId("jira-primary").click();
    await expect(page.getByText("No Jira space with this key, or the account cannot see it.")).toBeVisible();
    await expect(page.getByTestId("jira-primary")).toHaveText("Check access");
  });

  test("FR-JIRA-UI-05: a collaborator sees the saved target with every control disabled and no primary button", async ({
    page,
    requests,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Jira collaborator"), role: "collaborator" });
    await seedJira({ pid: id, target: { jira_project: "ABC", epic_key: null, enabled: true } });
    await page.goto(`/projects/${id}/settings?tab=jira`);
    await expect(page.getByTestId("jira-form")).toBeVisible();
    await expect(projectField(page)).toHaveValue("ABC");
    await expect(projectField(page)).toBeDisabled();
    await expect(epicField(page)).toBeDisabled();
    await expect(syncSwitch(page)).toBeDisabled();
    await expect(syncSwitch(page)).toBeChecked();
    await expect(page.getByTestId("jira-primary")).toHaveCount(0);
    await expect(page.getByTestId("jira-unavailable")).toHaveCount(0);
    expect(requests.filter((r) => r.method === "PUT" || r.method === "POST").filter((r) => r.path === JIRA_PATH(id))).toHaveLength(0);
  });

  test("FR-JIRA-UI-05: when the server has no Atlassian account the notice shows and the form is disabled", async ({ page }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Jira unavailable") });
    await seedJira({ pid: id, available: false, target: null });
    await page.goto(`/projects/${id}/settings?tab=jira`);
    await expect(page.getByTestId("jira-unavailable")).toContainText("Jira is not connected to this Specflow server.");
    await expect(projectField(page)).toBeDisabled();
    await expect(epicField(page)).toBeDisabled();
    await expect(syncSwitch(page)).toBeDisabled();
  });

  test("Atlassian: Connect goes to Atlassian's consent; the callback connects, toasts once and drops ?atlassian= without a reload", async ({
    page,
    requests,
    baseURL,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Atlassian connect") });
    await seedJira({ pid: id, target: null, mode: "oauth", connection: null });
    const back = `/projects/${id}/settings?tab=jira`;
    // Atlassian's consent page never loads: the request is answered here with the redirect Atlassian sends after "Accept".
    const consent: URL[] = [];
    allowOrigin(page, "https://auth.atlassian.com");
    await page.route(
      (url) => url.origin === "https://auth.atlassian.com",
      async (route) => {
        const url = new URL(route.request().url());
        consent.push(url);
        const state = url.searchParams.get("state") ?? "";
        await route.fulfill({
          status: 302,
          headers: { location: `${baseURL ?? ""}/api/integrations/atlassian/callback?code=stub-code&state=${state}` },
        });
      },
    );
    const logged = (await stubLog()).length; // the stub's log is shared by every case
    let loads = 0;
    page.on("load", () => {
      loads += 1;
    });
    await page.goto(back);
    const row = page.getByTestId("atlassian-row");
    await expect(row).toHaveAttribute("data-state", "none");
    await expect(page.getByRole("button", { name: "Check access" })).toBeDisabled();
    await expect(page.getByTestId("jira-gate")).toHaveText("Connect Atlassian to check access and save.");
    await row.getByRole("button", { name: "Connect Atlassian" }).click();

    await expect(page.getByText("Atlassian connected")).toBeVisible();
    expect(consent).toHaveLength(1);
    expect(consent[0].pathname).toBe("/authorize");
    const authorize = requests.find((r) => r.method === "POST" && r.path === "/api/backend/integrations/atlassian/authorize");
    expect(bodyOf(authorize ?? { postData: null })).toEqual({ return_to: back });
    await expect(page).toHaveURL(new RegExp(`/projects/${id}/settings\\?tab=jira$`)); // ?atlassian= is gone
    await expect(row).toHaveAttribute("data-state", "connected");
    await expect(row).toContainText("Connected to Atlassian as Ann Lee");
    await expect(page.getByTestId("jira-gate")).toHaveCount(0);
    expect(loads).toBe(2); // the tab, then the page the callback returned to: dropping ?atlassian= did not load it again
    await expect(page.getByTestId("toast").filter({ hasText: "Atlassian connected" })).toHaveCount(1);
    const log = await stubLog();
    expect(log.slice(logged).filter((e) => e.method === "POST" && e.path === "/api/v1/integrations/atlassian/callback")).toHaveLength(1);
  });

  test("Atlassian: a refused consent and a refused callback land back on the tab with their toast", async ({ page }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Atlassian refused") });
    await seedJira({ pid: id, target: null, mode: "oauth", connection: null, callbackError: "atlassian_site_not_granted" });
    const payload = JSON.stringify({ u: "u1", r: `/projects/${id}/settings?tab=jira`, e: 1 });
    const state = `n.${Buffer.from(payload).toString("base64url")}.stub-signature`;
    await page.goto(`/api/integrations/atlassian/callback?error=access_denied&state=${state}`);
    await expect(page.getByText("Atlassian access was not granted.")).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/projects/${id}/settings\\?tab=jira$`));
    await page.goto(`/api/integrations/atlassian/callback?code=c&state=${state}`);
    await expect(page.getByText("Your Atlassian account has no access to this Jira site.")).toBeVisible();
    await expect(page.getByTestId("atlassian-row")).toHaveAttribute("data-state", "none");
  });

  test("Atlassian: a return_to with dot segments never redirects off the site, after a refusal or a connect", async ({ page, baseURL }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Atlassian redirect") });
    await seedJira({ pid: id, target: null, mode: "oauth", connection: null, callbackError: null });
    const origin = new URL(baseURL ?? "").origin;
    for (const r of ["/.//evil.example", "/..//evil.example", "/%2e%2e//evil.example"]) {
      const state = `n.${Buffer.from(JSON.stringify({ u: "u1", r, e: 1 })).toString("base64url")}.stub-signature`;
      for (const query of [`error=access_denied&state=${state}`, `code=c&state=${state}`]) {
        const res = await page.request.get(`/api/integrations/atlassian/callback?${query}`, { maxRedirects: 0 });
        expect(res.status()).toBe(302);
        const location = new URL(res.headers().location);
        expect(location.origin, `${r} ${query.slice(0, 5)}`).toBe(origin);
        expect(location.pathname).toBe("/");
      }
    }
  });

  test("Atlassian: Disconnect asks first, then forgets the connection; the acting line warns to save the target again", async ({
    page,
    requests,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Atlassian disconnect") });
    await seedJira({
      pid: id,
      mode: "oauth",
      connection: { status: "active", account_name: "Ann Lee" },
      target: { jira_project: "ABC", epic_key: null, enabled: true, acting: "me" },
    });
    await page.goto(`/projects/${id}/settings?tab=jira`);
    await expect(page.getByTestId("jira-acting")).toHaveText("Syncs to Jira as you");
    await page.getByTestId("atlassian-disconnect").click();
    const dialog = page.getByRole("dialog", { name: "Disconnect Atlassian?" });
    await expect(dialog.getByRole("link", { name: "Connected apps" })).toHaveAttribute(
      "href",
      "https://id.atlassian.com/manage-profile/apps",
    );
    await dialog.getByRole("button", { name: "Keep" }).click();
    expect(requests.filter((r) => r.method === "DELETE")).toHaveLength(0);
    await page.getByTestId("atlassian-disconnect").click();
    await page.getByTestId("atlassian-disconnect-confirm").click();
    await expect(page.getByText("Atlassian disconnected")).toBeVisible();
    await expect(page.getByTestId("atlassian-row")).toHaveAttribute("data-state", "none");
    await expect(page.getByTestId("jira-acting")).toContainText("Syncs to Jira as nobody yet");
    await expect(page.getByTestId("jira-acting")).toContainText("Save the target to sync as you.");
    await expect(syncSwitch(page)).toBeDisabled();
    expect(requests.filter((r) => r.method === "DELETE" && r.path === "/api/backend/integrations/atlassian")).toHaveLength(1);
  });

  test("Atlassian: the callback without a session goes to sign-in and never calls the API", async ({ playwright, baseURL }) => {
    const anonymous = await playwright.request.newContext({ baseURL });
    const before = (await stubLog()).length;
    const res = await anonymous.get("/api/integrations/atlassian/callback?code=c&state=a.b.c", { maxRedirects: 0 });
    expect(res.status()).toBe(302);
    expect(new URL(res.headers().location).pathname).toBe("/login");
    expect((await stubLog()).slice(before).filter((e) => e.path.includes("atlassian"))).toHaveLength(0);
    await anonymous.dispose();
  });
});

const SLACK_PATH = (pid: string) => `/api/backend/projects/${pid}/slack`;
const SHOP_GAPS = { id: "C1", name: "shop-gaps", private: true };

/** Answers `GET /projects/{pid}/slack` with `settings` and a `DELETE` with 204; returns the methods the tab sent. */
async function stubSlack(page: Page, pid: string, settings: Record<string, unknown>): Promise<string[]> {
  const sent: string[] = [];
  await page.route(
    (url) => url.pathname === SLACK_PATH(pid),
    async (route) => {
      sent.push(route.request().method());
      if (route.request().method() === "DELETE") await route.fulfill({ status: 204 });
      else await route.fulfill({ json: settings });
    },
  );
  return sent;
}

test.describe("Project Settings, tab Slack, on production routes", { tag: "@production" }, () => {
  test.afterEach(async () => {
    await resetPeopleState();
  });

  test("Slack: the owner of a project with no channel gets the connect command with its id, and no channel list", async ({ page }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Slack connect") });
    await stubSlack(page, id, { available: true, channel: null, editable: true });
    await page.goto(`/projects/${id}/settings?tab=slack`);

    const tab = page.getByTestId("slack-tab");
    await expect(tab.getByText(`/specflow connect ${id}`, { exact: true })).toBeVisible();
    await expect(tab.getByRole("button", { name: "Copy" })).toBeVisible();
    await expect(tab).toContainText("/invite @SpecFlow");
    await expect(page.getByRole("combobox")).toHaveCount(0);
  });

  test("Slack: the owner sees the connected channel and disconnects it; the command shows again", async ({ page }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Slack bound") });
    const sent = await stubSlack(page, id, { available: true, channel: SHOP_GAPS, editable: true });
    await page.goto(`/projects/${id}/settings?tab=slack`);

    await expect(page.getByTestId("slack-bound")).toHaveText("🔒 shop-gaps");
    await page.getByTestId("slack-unbind").click();

    await expect(page.getByText("The channel is disconnected; no more questions are posted there.")).toBeVisible();
    await expect(page.getByTestId("slack-tab").getByText(`/specflow connect ${id}`, { exact: true })).toBeVisible();
    expect(sent).toEqual(["GET", "DELETE"]);
  });

  test("Slack: a collaborator sees the channel or that there is none, with no command and no Disconnect", async ({ page }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Slack collaborator") });
    await stubSlack(page, id, { available: true, channel: null, editable: false });
    await page.goto(`/projects/${id}/settings?tab=slack`);

    await expect(page.getByTestId("slack-bound")).toHaveText("No Slack channel yet. The project's owner connects one from Slack.");
    await expect(page.getByTestId("slack-tab").getByText("/specflow connect")).toHaveCount(0);
    await expect(page.getByTestId("slack-unbind")).toHaveCount(0);
  });
});
