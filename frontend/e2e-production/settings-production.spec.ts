// The V2 Members dialog (D11) on the PRODUCTION routes in a real browser, against the stub API (tests/support/stub-members.mjs) through
// the real BFF: the General-tab coverage the walkthrough in v2-production.spec.ts does not already drive. A new email becomes an Invited
// row and its invite asks before it is cancelled; the suggestion list starts at three characters, the keyboard picks, and the request body
// holds the text and the project, never an email in a URL (NFR-PRJ-02); an address the sign-in refuses and one already invited say so
// under the field. The rename, the known-person add and the member removal are in v2-production's walkthrough, and the collaborator's
// read-only list and Leave there too; the Jira, Atlassian and Slack tabs have no V2 Settings equivalent (their Integrations-page coverage
// lives in v2-production and canvas-connectors).
import type { Locator, Page } from "@playwright/test";
import { canvas, dialog, unique } from "./canvas-support";
import { allowConsoleErrors, expect, loginV2, seedMembers, test, v2Header } from "./support";

const bodyOf = (r: { postData: string | null }): unknown => JSON.parse(r.postData ?? "{}");

/** The Members dialog of the header, opened over the canvas. */
async function openMembers(page: Page): Promise<Locator> {
  await v2Header(page).getByRole("button", { name: "Members" }).click();
  const members = dialog(page, "Members");
  await expect(members).toBeVisible();
  return members;
}

test.describe("The Members dialog on production routes", { tag: "@production" }, () => {
  test("a new email is an Invited row, the count follows, and the invite asks before it is cancelled; no email travels in a URL", async ({
    page,
    requests,
  }) => {
    const pid = await canvas(page, unique("Invite"));
    const members = await openMembers(page);
    await expect(members.getByText("1 of 50", { exact: true })).toBeVisible();

    const add = members.getByLabel("Email address");
    await add.fill("brand.new@example.test"); // never signed in: an invite
    await members.getByRole("button", { name: "Add", exact: true }).click();
    await expect(page.getByText("Invite saved")).toBeVisible();
    const invite = members.getByRole("listitem").filter({ hasText: "brand.new@example.test" });
    await expect(invite).toContainText("Invited");
    await expect(invite).toContainText("Not signed in yet");
    await expect(members.getByText("2 of 50", { exact: true })).toBeVisible();
    expect(requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/members`).map(bodyOf)).toEqual([
      { email: "brand.new@example.test" },
    ]);
    expect(requests.some((r) => r.path.includes("@") || r.search.includes("@"))).toBe(false); // NFR-PRJ-02

    // Cancel invite asks first: Keep sends nothing, the confirm cancels.
    await invite.getByRole("button", { name: "Cancel invite brand.new@example.test" }).click();
    const sure = members.getByRole("alertdialog", { name: "Cancel this invite?" });
    await expect(sure).toContainText("brand.new@example.test will not get access when they sign in.");
    await sure.getByRole("button", { name: "Keep" }).click();
    expect(requests.filter((r) => r.method === "DELETE")).toHaveLength(0);
    await invite.getByRole("button", { name: "Cancel invite brand.new@example.test" }).click();
    await sure.getByRole("button", { name: "Cancel invite", exact: true }).click();
    await expect(page.getByText("Invite cancelled")).toBeVisible();
    await expect(invite).toHaveCount(0);
    await expect(members.getByText("1 of 50", { exact: true })).toBeVisible();
    expect(requests.filter((r) => r.method === "DELETE" && r.path.startsWith(`/api/backend/projects/${pid}/invites/`))).toHaveLength(1);
  });

  test("three characters list the suggestions; the keyboard picks one; the request body holds the text and the project, and a refused domain or an invited address says so under the field", async ({
    page,
    requests,
  }) => {
    allowConsoleErrors(page, /status of (409|422)/); // the refused adds are answers
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Errors"), invites: ["waiting@example.test"] });
    await page.goto(`/projects/${id}`);
    const members = await openMembers(page);
    await expect(members.getByText("2 of 50", { exact: true })).toBeVisible();

    const add = members.getByLabel("Email address");
    const list = members.getByRole("list", { name: "Suggested people" });
    const suggestions = () => requests.filter((r) => r.method === "POST" && r.path === "/api/backend/users/suggestions");
    await add.click();
    await add.pressSequentially("al");
    await expect(list).toHaveCount(0); // under three characters: no list, no request
    expect(suggestions()).toHaveLength(0);
    await add.pressSequentially("a");
    await expect(list).toBeVisible();
    await expect(list.getByRole("button")).toContainText("Alan Turner");
    await add.press("ArrowDown");
    await add.press("Enter"); // the keyboard picks: the field then holds the address
    await expect(list).toHaveCount(0);
    await expect(add).toHaveValue("alan.turner@example.test");
    expect(suggestions().map(bodyOf)).toEqual([{ q: "ala", project_id: id }]);
    expect(requests.some((r) => r.path.includes("@") || r.search.includes("@"))).toBe(false); // NFR-PRJ-02

    await add.fill("someone@refused.test");
    await members.getByRole("button", { name: "Add", exact: true }).click();
    await expect(members.getByText("This email domain cannot sign in to SpecFlow.")).toBeVisible();
    await add.fill("waiting@example.test");
    await members.getByRole("button", { name: "Add", exact: true }).click();
    await expect(members.getByText("Already invited.")).toBeVisible();
    await expect(members.getByText("2 of 50", { exact: true })).toBeVisible(); // nothing was added
  });
});
