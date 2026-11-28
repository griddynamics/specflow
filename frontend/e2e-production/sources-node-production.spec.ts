// sources-ux session 07 (INT-SRC-0001..0003, FR-SRC-0012, 0018, 0019, 0025, NFR-SEC-0002.AC2) on the PRODUCTION routes, against the stub
// API through the real BFF (scripts/serve-production.mjs; no backend): the source lifecycle in the V2 node dialog. The flat file list,
// the tags and reasons and the rename live in feedback-1-production.spec.ts; here a source is removed after a confirmation (V2 asks with
// Keep and Remove, not a typed name), the DELETE goes through the BFF with the CSRF header, an old API's plain not_found is not a removal,
// a source removed elsewhere closes the open dialog without a toast, and a source added elsewhere shows within 10 s while nothing runs.
import { canvas, dialog, nodes, STATE, unique } from "./canvas-support";
import { allowConsoleErrors, expect, removeSourceElsewhere, seedSource, test } from "./support";

const MIXED = [
  { name: "plan.pdf", state: "done" },
  { name: "slides.pdf", state: "done" },
  { name: "scan.docx", state: "not_read", reason: "not_readable", category: "word" },
  { name: "broken.pdf", state: "failed", reason: "read_failed" },
];

test.describe("Sources on the V2 canvas node dialog", { tag: "@production" }, () => {
  test("a source is removed from its node after a confirmation; the DELETE goes through the BFF with the CSRF header", async ({
    page,
    requests,
  }) => {
    const pid = await canvas(page, unique("Remove"));
    const sid = await seedSource(pid, { kind: "gdrive", name: "Mixed folder", files: MIXED });
    const node = nodes(page).filter({ hasText: "Mixed folder" });
    await expect(node).toHaveAttribute(STATE, "on"); // done with problems: imported, and the counts line says the rest
    await expect(node).toContainText("2 of 4 files processed");
    await node.getByRole("button").click();
    const d = dialog(page, "Mixed folder");
    await expect(d.getByRole("list", { name: "Files in this source" })).toBeVisible();
    expect(
      requests.some((r) => r.method === "GET" && r.path === `/api/backend/projects/${pid}/sources/${sid}/files` && r.search === "?limit=200"),
    ).toBe(true);
    await d.getByRole("button", { name: "Remove" }).click();
    const sure = page.getByRole("alertdialog", { name: "Remove this source?" });
    await expect(sure).toContainText("Its files leave the project. What the graph already learned from them stays until the next build.");
    await sure.getByRole("button", { name: "Keep" }).click(); // Keep asks nothing of the API
    expect(requests.filter((r) => r.method === "DELETE")).toHaveLength(0);
    await d.getByRole("button", { name: "Remove" }).click();
    await page.getByRole("alertdialog", { name: "Remove this source?" }).getByRole("button", { name: "Remove" }).click();
    await expect(d).toBeHidden();
    await expect(node).toHaveCount(0);
    const del = requests.filter((r) => r.method === "DELETE");
    expect(del.map((r) => r.path)).toEqual([`/api/backend/projects/${pid}/sources/${sid}`]);
    expect(del[0].headers["x-requested-with"]).toBe("specflow");
  });

  test("an old API's plain not_found is not a removal: the dialog stays open with the failure text and the node stays", async ({ page }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 404/);
    const pid = await canvas(page, unique("Old API"));
    const sid = await seedSource(pid, { kind: "gdrive", name: "Kept folder", files: MIXED });
    await page.route(`**/api/backend/projects/${pid}/sources/${sid}`, (route) => {
      if (route.request().method() !== "DELETE") return route.fallback();
      return route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "not_found", message: "not_found", details: {} } }),
      });
    });
    const node = nodes(page).filter({ hasText: "Kept folder" });
    await node.getByRole("button").click();
    const d = dialog(page, "Kept folder");
    await d.getByRole("button", { name: "Remove" }).click();
    await page.getByRole("alertdialog", { name: "Remove this source?" }).getByRole("button", { name: "Remove" }).click();
    await expect(d.getByRole("alert")).toContainText("It could not be removed. Try again.");
    await expect(d).toBeVisible(); // the confirmation is gone, the dialog and its source stay
    await d.getByRole("button", { name: "Done" }).click();
    await expect(node).toHaveCount(1);
  });

  test("a source removed in another tab closes the open dialog within one refresh, and no toast follows", async ({ page }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 404/); // a refresh in flight is answered after the source is gone
    const pid = await canvas(page, unique("Gone"));
    const sid = await seedSource(pid, { kind: "gdrive", name: "Vanishing folder", files: MIXED });
    const node = nodes(page).filter({ hasText: "Vanishing folder" });
    await node.getByRole("button").click();
    await expect(dialog(page, "Vanishing folder")).toBeVisible();
    await removeSourceElsewhere(pid, sid);
    await expect(dialog(page, "Vanishing folder")).toHaveCount(0, { timeout: 15_000 }); // the canvas reads again within 5 s
    await expect(page.getByTestId("toast")).toHaveCount(0); // V2 says nothing: the dialog simply follows the canvas
    await expect(node).toHaveCount(0, { timeout: 15_000 });
  });

  test("a source added elsewhere shows within 10 s while nothing runs (the idle poll)", async ({ page }) => {
    const pid = await canvas(page, unique("Idle poll"));
    const sid = await seedSource(pid, { kind: "upload", name: "Late files", files: [{ name: "late.pdf", state: "done" }] });
    const node = nodes(page).filter({ hasText: "Late files" });
    await expect(node).toBeVisible({ timeout: 10_000 });
    await expect(node).toHaveAttribute("data-anchor", sid);
    await expect(node).toContainText("1 file");
  });
});
