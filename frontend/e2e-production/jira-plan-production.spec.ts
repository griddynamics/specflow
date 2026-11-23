// The Jira sync line of the ready Plan (docs/jira-sync/ui.md#plan-line) on the PRODUCTION routes, against the stub API through the real BFF.
// No Jira and no paid model call: the stub holds the Jira settings of the project and a case seeds the sync status (with `progress`) through
// the control port; the line polls the settings every 5 s, so a reseed shows within one poll.
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import {
  buildGraphAndPlan,
  createViaNew,
  expect,
  expectUploadReady,
  login,
  pickLocalPdf,
  rail,
  seedJira,
  seedMembers,
  test,
} from "./support";

const unique = () => `Jira plan ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
const line = (page: Page) => page.getByTestId("plan-jira");
const target = (enabled: boolean, sync?: Record<string, unknown>, createdEpicKey: string | null = null) => ({
  jira_project: "ABC",
  epic_key: null,
  created_epic_key: createdEpicKey,
  enabled,
  ...(sync === undefined ? {} : { sync }),
});
const running = (progress: Record<string, unknown> | null) => ({
  status: "running",
  finished_at: null,
  error_code: null,
  progress,
});

test.describe("Plan Jira sync line on production routes", { tag: "@production" }, () => {
  test.setTimeout(120_000);

  test.afterEach(async () => {
    await seedJira({ pid: "none", available: true }); // the Jira switch is global in the stub: put it back
  });

  /** A project at Plan Ready; the Jira settings are seeded before the plan screen opens. */
  async function readyPlan(page: Page, jira: Parameters<typeof seedJira>[0] | null): Promise<string> {
    await login(page);
    const pid = await createViaNew(page, unique());
    await pickLocalPdf(page, "requirements.pdf");
    await expectUploadReady(page);
    await buildGraphAndPlan(page, pid);
    if (jira !== null) await seedJira({ ...jira, pid });
    await rail(page).getByRole("button", { name: "Plan", exact: true }).click();
    await expect(page.getByTestId("screen-plan")).toHaveAttribute("data-stage", "ready", { timeout: 15_000 });
    return pid;
  }

  test("a running sync shows the step, the count and a progress bar; the line turns to 'up to date' with the Epic link when it ends", async ({
    page,
  }) => {
    const pid = await readyPlan(page, { pid: "", target: target(true, running({ step: "issues", done: 23, total: 60 })) });
    await expect(line(page)).toHaveAttribute("data-status", "running");
    await expect(line(page)).toContainText("Creating and updating issues");
    await expect(line(page)).toContainText("23 of 60");
    const bar = line(page).getByRole("progressbar");
    await expect(bar).toHaveAttribute("aria-valuenow", "23");
    await expect(bar).toHaveAttribute("aria-valuemax", "60");

    await seedJira({ pid, target: target(true, running({ step: "links", done: 4, total: 10 })) });
    await expect(line(page)).toContainText("Adding links", { timeout: 12_000 });
    await expect(line(page)).toContainText("4 of 10");

    await seedJira({
      pid,
      target: target(true, { status: "succeeded", finished_at: "2026-02-01T10:00:00Z", error_code: null, progress: null }, "ABC-9"),
    });
    await expect(line(page)).toContainText("Jira is up to date", { timeout: 12_000 });
    await expect(line(page).getByRole("progressbar")).toHaveCount(0);
    await expect(line(page).getByRole("link", { name: "Epic ABC-9" })).toHaveAttribute(
      "href",
      "https://example.atlassian.net/browse/ABC-9",
    );
  });

  test("a queued sync (running, no progress yet) says so; a failed one links to the Jira tab of Settings", async ({ page }) => {
    const pid = await readyPlan(page, { pid: "", target: target(true, running(null)) });
    await expect(line(page)).toContainText("Jira sync queued");
    await expect(line(page).getByRole("progressbar")).toHaveCount(0);

    await seedJira({
      pid,
      target: target(true, { status: "failed", finished_at: "2026-02-01T10:00:00Z", error_code: "jira_unreachable", progress: null }),
    });
    await expect(line(page)).toContainText("Jira sync failed.", { timeout: 12_000 });
    await line(page).getByRole("link", { name: "Open Jira settings" }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/settings\\?tab=jira$`));
  });

  test("the line is hidden when the sync is off, there is no target, or Jira is not connected; the plan stays usable", async ({ page }) => {
    const pid = await readyPlan(page, { pid: "", target: target(false, running({ step: "issues", done: 1, total: 2 })) });
    await expect(page.getByTestId("plan-ready")).toBeVisible();
    await expect(line(page)).toHaveCount(0);
    await seedJira({ pid, target: null });
    await seedJira({ pid, available: false });
    await page.reload();
    await expect(page.getByTestId("plan-ready")).toBeVisible();
    await expect(line(page)).toHaveCount(0);
  });

  test("Settings, tab Jira: a running sync shows the step, the count and a progress bar next to the status word", async ({ page }) => {
    await login(page);
    const { id } = await seedMembers({ name: unique(), members: [] });
    await seedJira({ pid: id, target: target(true, running({ step: "order", done: 5, total: 12 })) });
    await page.goto(`/projects/${id}/settings?tab=jira`);
    const status = page.getByTestId("jira-status");
    await expect(status).toContainText("Syncing");
    await expect(status).toContainText("Ordering issues");
    await expect(status).toContainText("5 of 12");
    await expect(status.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "5");
  });
});
