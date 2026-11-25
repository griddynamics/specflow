// The canvas fixes of the independent review (13), on the PRODUCTION routes against the stub API through the real BFF: a status read that
// fails is said with Try again, a build start the API refuses stays in the dialog, the focus follows the body of the dialog, and a hub's status is
// in the page for screen readers.
import { canvas, dialog, done, hub, parsed, SPECS, STATE, unique } from "./canvas-support";
import { allowConsoleErrors, expect, seedSource, stubTiming, STUB_TIMING, test } from "./support";

test.afterEach(async () => {
  await stubTiming(STUB_TIMING);
});

test.describe("Mission Control V2: review fixes of the canvas", { tag: "@production" }, () => {
  test("a status read that fails shows a notice with Try again over the canvas; once it can be read the notice goes and the nodes show", async ({
    page,
  }) => {
    allowConsoleErrors(page, /status of 503/); // the refused read is the case
    const pid = await canvas(page, unique("Status failed"));
    await seedSource(pid, { kind: "gdrive", name: SPECS, files: done(3) });
    await stubTiming({ sourcesFail: "database_unavailable" });
    await page.reload();
    const alert = page.getByRole("alert").filter({ hasText: "The project could not be read" });
    await expect(alert).toBeVisible();
    await stubTiming({ sourcesFail: null });
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(alert).toBeHidden();
    await expect(page.getByTestId("canvas-node").filter({ hasText: SPECS })).toBeVisible();
  });

  test("a build the API refuses to start stays in its dialog with the reason, and the hub does not look busy; a later start works", async ({
    page,
  }) => {
    allowConsoleErrors(page, /status of 503/); // the refused start is the case
    const pid = await canvas(page, unique("Build refused"));
    await seedSource(pid, { kind: "gdrive", name: SPECS, files: done(3) });
    await parsed(page, pid);
    await page.reload();
    await stubTiming({ graphStartFail: "workflow_start_failed" });
    await page.getByRole("button", { name: "Graph. Build graph" }).click();
    const build = dialog(page, "Build the graph");
    await build.getByRole("button", { name: "Build", exact: true }).click();
    await expect(build.getByRole("alert").filter({ hasText: "The build could not be started" })).toBeVisible();
    await expect(hub(page, "graph")).not.toHaveAttribute(STATE, "busy");
    await stubTiming({ graphStartFail: null });
    await build.getByRole("button", { name: "Build", exact: true }).click();
    await expect(build).toBeHidden();
    await expect(hub(page, "graph")).toHaveAttribute(STATE, "busy");
  });

  test("the focus moves to the heading of the new body when the dialog goes from the list to a form and back", async ({ page }) => {
    await canvas(page, unique("Dialog focus"));
    await page.getByRole("button", { name: "Add source" }).click();
    const list = dialog(page, "Add a source");
    await list.getByRole("button", { name: /^Upload/ }).click();
    const form = dialog(page, "Add files");
    await expect(form.getByRole("heading", { name: "Add files" })).toBeFocused();
    await form.getByRole("button", { name: "Back" }).click();
    await expect(dialog(page, "Add a source").getByRole("heading", { name: "Add a source" })).toBeFocused();
  });

  test("a hub's status is in the page for screen readers, with no visible name or count under the hub", async ({ page }) => {
    await canvas(page, unique("Hub status"));
    const status = page.getByTestId("hub-status-graph");
    await expect(status).toHaveText(/.+/);
    await expect(status).not.toBeInViewport(); // hidden visually: one pixel, clipped
    expect(await status.evaluate((el) => el.getBoundingClientRect().width)).toBeLessThanOrEqual(1);
  });
});
