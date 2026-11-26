// Mission Control V2 on the PRODUCTION routes in a real browser, against the stub API through the real BFF (docs/mission-control/README.md,
// DECISIONS D1-D6, D11, D12): the header with the project menu, the first-run window, the Members dialog, and the Integrations page with a
// card per app. The apps' own pages (GitHub, Atlassian) are intercepted and never reached: they answer with the redirect to the callback
// route. A case seeds its own project through the control port and puts the global knobs back after it.
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import { networkQuiet } from "../e2e/support";
import {
  allowConsoleErrors,
  allowOrigin,
  createProjectApi,
  expect,
  loginV2,
  setEndSession,
  seedGitHub,
  seedIntegrations,
  seedJira,
  seedMembers,
  stubLog,
  test,
  v2Header,
} from "./support";

const bodyOf = (r: { postData: string | null }): unknown => JSON.parse(r.postData ?? "{}");
const ATLASSIAN = "Atlassian";
const STATE = "data-state";
const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
const CANVAS = (pid: string) => new RegExp(`/projects/${pid}$`);
const INTEGRATIONS = (pid: string) => `/projects/${pid}/integrations`;
const ALEX = { user_id: "someone", name: "Sam Ortiz" };
const card = (page: Page, name: string) => page.getByRole("listitem").filter({ has: page.getByRole("heading", { name, exact: true }) });
const dialog = (page: Page, name: string) => page.getByRole("dialog", { name });
const projectsButton = (page: Page) => page.getByRole("button", { name: "Projects" });

test.beforeEach(async () => {
  await seedJira({ pid: "none", mode: "oauth", connection: null }); // Atlassian runs in the oauth mode on the server V2 targets
});

test.afterEach(async () => {
  await seedGitHub({ reset: true });
  await seedIntegrations({ reset: true });
  await seedJira({ pid: "none", mode: "token", connection: null });
});

test.describe("Mission Control V2: header, Members, Integrations", { tag: "@production" }, () => {
  test.afterEach(async () => {
    await setEndSession(true); // one case switches the issuer's end-session endpoint off
  });

  test("login, first run, create a project, Members, Integrations: the whole way through", async ({ page, requests }) => {
    await loginV2(page);
    // The stub is shared by the run, so "no project yet" is the list endpoint answering an empty page for this one load.
    await page.route("**/api/backend/projects?limit=100", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [], next_cursor: null }) }),
    );
    await page.goto("/");
    const first = dialog(page, "Create your first project");
    await expect(first).toBeVisible();
    // First run: the brand, the theme switch and the account only (AC36): no project menu, none of the links.
    await expect(projectsButton(page)).toHaveCount(0);
    await expect(v2Header(page).getByRole("link")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Switch to dark mode" }).or(page.getByRole("button", { name: "Switch to light mode" })),
    ).toBeVisible();
    await page.unroute("**/api/backend/projects?limit=100");
    const name = unique("Walkthrough");
    await first.getByLabel("Project name").fill(name);
    await first.getByRole("button", { name: "Create project" }).click();
    await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}$/);
    const pid = page.url().split("/").at(-1) ?? "";
    await expect(projectsButton(page)).toHaveText(name);
    await expect(projectsButton(page)).toHaveAccessibleName(`Projects: ${name}`); // the name carries the visible text
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(0); // no page title in the body

    // Members: the owner adds a known person (a member), removes them after a confirmation, then renames the project.
    await v2Header(page).getByRole("button", { name: "Members" }).click();
    const members = dialog(page, "Members");
    await expect(members).toBeVisible();
    await expect(members.getByText("1 of 50")).toBeVisible();
    await expect(members.getByText("Owner", { exact: true })).toBeVisible();
    // Escape in the email field closes the suggestions and leaves the dialog open (the native dialog closes on Escape's default action).
    await members.getByLabel("Email address").fill("ali");
    const suggestions = members.getByRole("list", { name: "Suggested people" });
    await expect(suggestions).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(suggestions).toBeHidden();
    await expect(members).toBeVisible();
    await expect(members.getByLabel("Email address")).toBeFocused();
    await members.getByLabel("Email address").fill("alice.anders@example.test");
    await members.getByRole("button", { name: "Add", exact: true }).click();
    await expect(page.getByText("Collaborator added")).toBeVisible();
    await expect(members.getByText("Alice Anders")).toBeVisible();
    await members.getByRole("button", { name: "Remove Alice Anders" }).click();
    const sure = members.getByRole("alertdialog", { name: "Remove this collaborator?" });
    await expect(sure).toBeVisible();
    await sure.getByRole("button", { name: "Keep" }).click();
    await expect(members.getByText("Alice Anders")).toBeVisible();
    await members.getByRole("button", { name: "Remove Alice Anders" }).click();
    await sure.getByRole("button", { name: "Remove", exact: true }).click();
    await expect(members.getByText("Alice Anders")).toHaveCount(0);
    const renamed = unique("Renamed");
    await members.getByLabel("Project name").fill(renamed);
    await members.getByRole("button", { name: "Save" }).click();
    await expect(projectsButton(page)).toHaveText(renamed);
    await members.getByRole("button", { name: "Done" }).click();
    await expect(members).toBeHidden();
    expect(requests.filter((r) => r.method === "PATCH").map(bodyOf)).toEqual([{ name: renamed }]);

    // Integrations: a card per app, none connected, the built-in ones under them.
    await v2Header(page).getByRole("link", { name: "Integrations" }).click();
    await expect(page).toHaveURL(INTEGRATIONS(pid));
    await expect(page.getByRole("list", { name: "Integrations" }).getByRole("listitem")).toHaveCount(5); // the four apps and MCP
    for (const app of [ATLASSIAN, "GitHub", "Slack", "Notion"]) {
      await expect(card(page, app)).toHaveAttribute(STATE, "off");
      await expect(card(page, app).getByRole("button", { name: "Set up" })).toBeVisible();
    }
    await expect(page.getByText("Upload and Google Drive")).toBeVisible();
    await expect(page.getByText("Added to the canvas by itself.")).toHaveCount(0); // MCP is a card now, not a built-in line
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(0);
    await v2Header(page).getByRole("link", { name: "Mission Control" }).click();
    await expect(page).toHaveURL(CANVAS(pid));
  });

  test("the project menu lists the projects with the current one checked; picking one opens its canvas; Create new project opens a closable dialog", async ({
    page,
  }) => {
    await loginV2(page);
    const a = await seedMembers({ name: unique("Menu A") });
    const b = await seedMembers({ name: unique("Menu B") });
    await page.goto(`/projects/${a.id}`);
    await projectsButton(page).click();
    const menu = page.getByRole("menu", { name: "Projects" });
    await expect(menu.getByRole("menuitemradio", { name: a.name, exact: true })).toHaveAttribute("aria-checked", "true");
    await expect(menu.getByRole("menuitemradio", { name: b.name, exact: true })).toHaveAttribute("aria-checked", "false");
    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
    // Keyboard: Home and End go to the first and last item; Tab out of the menu closes it.
    await projectsButton(page).click();
    await page.keyboard.press("End");
    await expect(menu.getByRole("menuitem", { name: "+ Create new project" })).toBeFocused();
    await page.keyboard.press("Home");
    await expect(menu.getByRole("menuitemradio").first()).toBeFocused();
    await page.keyboard.press("End");
    await page.keyboard.press("Tab");
    await expect(menu).toHaveCount(0);
    await projectsButton(page).click();
    // Safari and Firefox on macOS do not focus a button on click: the list is blurred with no related target before the click lands.
    await page.evaluate(() => {
      for (const item of document.querySelectorAll('[role="menuitemradio"]'))
        item.addEventListener("mousedown", () => {
          if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        });
    });
    await menu.getByRole("menuitemradio", { name: b.name, exact: true }).click();
    await expect(page).toHaveURL(CANVAS(b.id));
    await expect(projectsButton(page)).toHaveText(b.name);

    await projectsButton(page).click();
    await page.getByRole("menuitem", { name: "+ Create new project" }).click();
    const create = dialog(page, "Create a project");
    await expect(create).toBeVisible();
    await create.getByRole("button", { name: "Cancel" }).click();
    await expect(create).toBeHidden();
    await projectsButton(page).click();
    await page.getByRole("menuitem", { name: "+ Create new project" }).click();
    const made = unique("Via the menu");
    await create.getByLabel("Project name").fill(made);
    await create.getByRole("button", { name: "Create project" }).click();
    await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}$/);
    await expect(projectsButton(page)).toHaveText(made);
  });

  test("the Slack link landing `/?slack=linked` toasts once and opens the newest project's canvas", async ({ page }) => {
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Slack link") });
    await page.goto("/?slack=linked");
    await expect(page.getByText("Slack linked to your account.")).toBeVisible();
    await expect(page).toHaveURL(CANVAS(id));
    await page.goto("/?slack=slack_link_invalid");
    await expect(page.getByText("The Slack link expired.")).toBeVisible();
  });

  test("the theme switch sets the theme on the page and in the cookie; Sign out ends the session", async ({ page, context }) => {
    await setEndSession(false); // the Google case: the card says "You are signed out." and waits (the provider's logout: logout-production)
    await loginV2(page);
    await page.goto("/");
    const theme = () => page.evaluate(() => document.documentElement.dataset.theme);
    await expect.poll(theme).toBe("light");
    await v2Header(page).getByRole("button", { name: "Switch to dark mode" }).click();
    await expect.poll(theme).toBe("dark");
    expect((await context.cookies()).find((c) => c.name === "specflow-theme")?.value).toBe("dark");
    await page.reload();
    await expect.poll(theme).toBe("dark");
    await v2Header(page).getByRole("button", { name: "Switch to light mode" }).click();
    await expect.poll(theme).toBe("light");
    await networkQuiet(page); // a response in flight after the sign-out could bring the session back
    await v2Header(page).getByRole("button", { name: "Account" }).click();
    await page.getByRole("menuitem", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login\?signedout=1$/, { timeout: 15_000 });
    await expect(page.getByTestId("login-signed-out")).toBeVisible();
  });

  test("an unknown project is a 404 page inside the shell; a collaborator sees Members read-only and can leave", async ({ page }) => {
    allowConsoleErrors(page, /status of 404/);
    await loginV2(page);
    const res = await page.goto("/projects/00000000-0000-4000-8000-000000000000");
    expect(res?.status()).toBe(404);
    await expect(page.getByText("This project does not exist, or you are not a member of it.")).toBeVisible();

    const name = unique("Collaborating");
    const { id } = await seedMembers({ name, role: "collaborator", members: [{ email: "dev@example.test", name: "dev" }] });
    const other = await seedMembers({ name: unique("Stay") }); // the newest project: where `/` opens after leaving
    await page.goto(`/projects/${id}`);
    await v2Header(page).getByRole("button", { name: "Members" }).click();
    const members = dialog(page, "Members");
    await expect(members.getByText("Olivia Owner")).toBeVisible();
    await expect(members.getByRole("textbox")).toHaveCount(0);
    await expect(members.getByRole("button", { name: /^Remove / })).toHaveCount(0);
    await members.getByRole("button", { name: "Leave project" }).click();
    await members.getByRole("alertdialog", { name: "Leave this project?" }).getByRole("button", { name: "Leave project" }).click();
    await expect(page).toHaveURL(CANVAS(other.id));
    await projectsButton(page).click();
    await expect(page.getByRole("menuitemradio", { name, exact: true })).toHaveCount(0);
  });

  test("cards show the API's health word and who connected it; the owner disconnects another member's binding, a collaborator cannot", async ({
    page,
  }) => {
    await loginV2(page);
    const owned = await seedMembers({ name: unique("Owned") });
    const shared = await seedMembers({ name: unique("Shared"), role: "collaborator" });
    await seedIntegrations({
      pid: owned.id,
      atlassian: { by: ALEX, health: "active", workspace: { id: "c", name: "northwind.atlassian.test" } },
      github: { by: ALEX, health: "reconnect_required" },
      slack: null,
    });
    await seedIntegrations({ pid: shared.id, atlassian: { by: ALEX, health: "active" }, github: { by: null, health: "none" } });

    await page.goto(INTEGRATIONS(owned.id));
    await expect(card(page, ATLASSIAN)).toHaveAttribute(STATE, "on");
    await expect(card(page, ATLASSIAN)).toContainText("Active");
    await expect(card(page, ATLASSIAN)).toContainText("Connected by Sam Ortiz");
    await expect(card(page, "GitHub")).toHaveAttribute(STATE, "stopped");
    await expect(card(page, "GitHub")).toContainText("Reconnect required");
    await expect(card(page, "Slack")).toHaveAttribute(STATE, "off");
    await card(page, ATLASSIAN).getByRole("button", { name: "Manage" }).click();
    const set = dialog(page, ATLASSIAN);
    await expect(set).toContainText("northwind.atlassian.test · Connected by Sam Ortiz");
    await set.getByRole("button", { name: "Disconnect" }).click();
    await expect(set.getByRole("alertdialog", { name: "Disconnect Atlassian?" })).toBeVisible();
    await set.getByRole("alertdialog").getByRole("button", { name: "Disconnect" }).click();
    await expect(page.getByText("Disconnected")).toBeVisible();
    await expect(card(page, ATLASSIAN)).toHaveAttribute(STATE, "off");

    await page.goto(INTEGRATIONS(shared.id));
    await expect(card(page, ATLASSIAN)).toContainText("Connected by Sam Ortiz");
    await card(page, ATLASSIAN).getByRole("button", { name: "Manage" }).click();
    await expect(dialog(page, ATLASSIAN).getByRole("button", { name: "Disconnect" })).toHaveCount(0);
    await dialog(page, ATLASSIAN).getByRole("button", { name: "Done" }).click();
    await card(page, "GitHub").getByRole("button", { name: "Manage" }).click(); // its member left: stopped, nobody named
    await expect(card(page, "GitHub")).toContainText("Stopped");
    await expect(
      dialog(page, "GitHub")
        .getByRole("button", { name: "Connect GitHub" })
        .or(dialog(page, "GitHub").getByRole("button", { name: "Reconnect GitHub" })),
    ).toBeVisible();
  });

  test("GitHub: Connect goes to GitHub, the return binds the account to the project and the picker adds and removes repositories", async ({
    page,
    requests,
    baseURL,
  }) => {
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("GitHub V2") });
    await seedGitHub({ reset: true, cloneMs: null });
    allowOrigin(page, "https://github.com");
    await page.route(
      (url) => url.origin === "https://github.com",
      async (route) => {
        const state = new URL(route.request().url()).searchParams.get("state") ?? "";
        await route.fulfill({
          status: 302,
          headers: { location: `${baseURL ?? ""}/api/integrations/github/callback?code=stub-code&state=${state}` },
        });
      },
    );
    await page.goto(INTEGRATIONS(id));
    await card(page, "GitHub").getByRole("button", { name: "Set up" }).click();
    const set = dialog(page, "GitHub");
    await set.getByRole("button", { name: "Connect GitHub" }).click();
    // Back from GitHub: the account is bound to the project and the dialog opens on the repositories.
    await expect(page.getByText("Connected", { exact: true })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${INTEGRATIONS(id)}\\?setup=github$`));
    await expect(card(page, "GitHub")).toHaveAttribute(STATE, "on");
    await expect(card(page, "GitHub")).toContainText("Connected by dev");
    const repos = dialog(page, "GitHub").getByRole("group", { name: "Repositories" });
    await expect(repos.getByRole("checkbox", { name: "acme/payments-api" })).not.toBeChecked();
    await expect(repos.getByRole("checkbox", { name: /acme\/monorepo/ })).toBeDisabled();
    await repos.getByRole("checkbox", { name: "acme/payments-api" }).click(); // checked once the request is answered
    await expect(repos.getByRole("checkbox", { name: /acme\/payments-api/ })).toBeChecked();
    expect(requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${id}/repositories`).map(bodyOf)).toEqual([
      { github_repo_id: 101 },
    ]);
    await repos.getByRole("checkbox", { name: /acme\/payments-api/ }).click();
    await expect(repos.getByRole("checkbox", { name: "acme/payments-api" })).not.toBeChecked();
    expect(requests.filter((r) => r.method === "DELETE" && r.path.startsWith(`/api/backend/projects/${id}/repositories/`))).toHaveLength(1);
  });

  test("Atlassian: with `next`, the return from the consent binds the site and sends the browser back to the canvas form that asked", async ({
    page,
    baseURL,
  }) => {
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Atlassian V2") });
    await seedJira({ pid: id, target: null, mode: "oauth", connection: null });
    allowOrigin(page, "https://auth.atlassian.com");
    await page.route(
      (url) => url.origin === "https://auth.atlassian.com",
      async (route) => {
        const state = new URL(route.request().url()).searchParams.get("state") ?? "";
        await route.fulfill({
          status: 302,
          headers: { location: `${baseURL ?? ""}/api/integrations/atlassian/callback?code=stub-code&state=${state}` },
        });
      },
    );
    const logged = (await stubLog()).length;
    const back = encodeURIComponent(`/projects/${id}`);
    await page.goto(`${INTEGRATIONS(id)}?setup=atlassian&next=${back}`);
    await dialog(page, ATLASSIAN).getByRole("button", { name: "Connect with Atlassian" }).click();
    await expect(page).toHaveURL(CANVAS(id)); // `next`: the canvas form that asked
    const put = (await stubLog())
      .slice(logged)
      .filter((e) => e.method === "PUT" && e.path === `/api/v1/projects/${id}/integrations/atlassian`);
    expect(put).toHaveLength(1);
    await page.goto(INTEGRATIONS(id));
    await expect(card(page, ATLASSIAN)).toHaveAttribute(STATE, "on");
    await expect(card(page, ATLASSIAN)).toContainText("Connected by dev");
  });

  test("a refused return says why in a toast and drops the code from the URL", async ({ page }) => {
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Refused") });
    await page.goto(`${INTEGRATIONS(id)}?github=github_consent_denied`);
    await expect(page.getByText("GitHub access was not granted.")).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${INTEGRATIONS(id)}\\?setup=github$`));
    await page.goto(`${INTEGRATIONS(id)}?atlassian=atlassian_site_not_granted`);
    await expect(page.getByText("Your Atlassian account has no access to this Jira site.")).toBeVisible();
    await expect(card(page, ATLASSIAN)).toHaveAttribute(STATE, "off");
  });

  test("Slack: the command waits for the channel; once bound, Test connection posts the welcome message; a member disconnects it", async ({
    page,
  }) => {
    allowConsoleErrors(page, /status of 409/); // the message the stub refuses
    await loginV2(page);
    const { id } = await seedMembers({
      name: unique("Slack V2"),
      role: "collaborator",
      members: [{ email: "dev@example.test", name: "dev" }],
    });
    await seedIntegrations({ pid: id, slack: null });
    await page.goto(INTEGRATIONS(id));
    await card(page, "Slack").getByRole("button", { name: "Set up" }).click();
    const set = dialog(page, "Slack");
    await expect(set.getByText(`/specflow connect ${id}`)).toBeVisible();
    await expect(set.getByText("Waiting for the channel.")).toBeVisible();
    await expect(set.getByRole("button", { name: "Test connection" })).toHaveCount(0);
    // The person runs the command in Slack: the channel is bound and the dialog notices on its next look.
    await seedIntegrations({ pid: id, slack: { channel: { id: "C1", name: "northwind-gaps", private: false } } });
    await expect(set.getByRole("button", { name: "Test connection" })).toBeVisible({ timeout: 10_000 });
    await expect(card(page, "Slack")).toHaveAttribute(STATE, "on");
    await set.getByRole("button", { name: "Test connection" }).click();
    await expect(set.getByText("Welcome message posted in #northwind-gaps.")).toBeVisible();
    await seedIntegrations({ slackTest: "slack_not_in_channel" });
    await set.getByRole("button", { name: "Test connection" }).click();
    await expect(set.getByRole("alert")).toContainText("The app is not in the channel any more.");
    // Any member may remove the channel (D6), not only the one who connected it.
    await set.getByRole("button", { name: "Disconnect" }).click();
    await set.getByRole("alertdialog").getByRole("button", { name: "Disconnect" }).click();
    await expect(card(page, "Slack")).toHaveAttribute(STATE, "off");
  });

  test("an app the server does not run has a disabled card", async ({ page }) => {
    await loginV2(page);
    const pid = await createProjectApi(page, unique("Unavailable"));
    await seedIntegrations({ pid, slackAvailable: false });
    await page.goto(INTEGRATIONS(pid));
    await expect(card(page, "Slack")).toHaveAttribute(STATE, "unavailable");
    await expect(card(page, "Slack").getByRole("button", { name: "Unavailable" })).toBeDisabled();
    await expect(card(page, "Slack")).toContainText("Not on this server");
  });
});
