// COLLABORATORS: the project Settings screen (tab General) on the PRODUCTION routes in a real browser, against the stub API through
// the real BFF. The stub keeps the people of each project (tests/support/stub-members.mjs); a case seeds its own project through the
// control port, so the shared stub never decides an outcome. Proves FR-PRJ-UI-01..08 (docs/projects/requirements.md).
import { randomBytes } from "node:crypto";
import {
  allowConsoleErrors,
  expect,
  login,
  projectSwitcher,
  rail,
  resetPeopleState,
  seedMembers,
  test,
  topbarProjectName,
} from "./support";

const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
const ROW_X = "settings-row-x";
const ADD_ROW = "settings-add";
const bodyOf = (r: { postData: string | null }): unknown => JSON.parse(r.postData ?? "{}");
const SETTINGS = (pid: string) => new RegExp(`/projects/${pid}/settings$`);

test.describe("Project Settings on production routes", { tag: "@production" }, () => {
  test.afterEach(async () => {
    await resetPeopleState();
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

  test("FR-PRJ-UI-03, UI-04, UI-05, UI-06: admin adds a known person and a new email, sees the count, removes and cancels with a confirm", async ({
    page,
    requests,
  }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique("Admin"), members: [{ email: "bob.brown@example.test", name: "Bob Brown" }] });
    await page.goto(`/projects/${id}/settings`);
    const rows = page.getByTestId("settings-row");
    await expect(page.getByTestId("settings-count")).toHaveText("2 of 50");
    await expect(rows.first()).toContainText("Admin");
    await expect(rows.first().getByTestId(ROW_X)).toHaveCount(0); // the admin row has no x (UI-03)

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
    await expect(page.getByTestId("settings-row").first()).toContainText("Admin");
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
});
