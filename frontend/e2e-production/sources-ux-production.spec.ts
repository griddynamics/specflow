// sources-ux session 07 (INT-SRC-0001..0003, FR-SRC-0007, 0012, 0018, 0019, 0025, NFR-SEC-0002.AC2) on the PRODUCTION routes, against the stub
// API through the real BFF (scripts/serve-production.mjs; no backend). The stub's control port seeds sources with chosen file states.
// Here: the real adapter end to end.
import { randomBytes } from "node:crypto";
import { allowConsoleErrors, createViaNew, expect, login, namedSourceCard, removeSourceElsewhere, seedSource, test } from "./support";

const unique = (p: string) => `${p} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
const MIXED = [
  { name: "plan.pdf", state: "done" },
  { name: "slides.pdf", state: "done" },
  { name: "scan.docx", state: "not_read", reason: "not_readable", category: "word" },
  { name: "broken.pdf", state: "failed", reason: "read_failed" },
];

test(
  "INT-SRC-0001..0003: a seeded Drive source opens in the details modal with its files grouped; removal needs the typed name, goes through the BFF as a DELETE with the CSRF header, and the card goes",
  { tag: "@production" },
  async ({ page, requests }) => {
    await login(page);
    const pid = await createViaNew(page, unique("Remove"));
    const sid = await seedSource(pid, { kind: "gdrive", name: "Mixed folder", files: MIXED });
    await page.goto(`/projects/${pid}/mission-control`);
    const card = namedSourceCard(page, "Mixed folder");
    await expect(card).toHaveAttribute("data-id", sid);
    await expect(card).toHaveAttribute("data-state", "done_with_problems");
    await card.getByTestId("source-open").click();
    await expect(page.getByTestId("sd-modal")).toBeVisible();
    await expect(page.getByTestId("sd-file")).toHaveCount(2); // the problem files; the Done group is folded
    await expect(
      page
        .getByTestId("sd-group")
        .filter({ has: page.getByRole("button", { name: /^Needs attention · / }) })
        .getByTestId("sd-file"),
    ).toHaveCount(2);
    expect(
      requests.some(
        (r) => r.method === "GET" && r.path === `/api/backend/projects/${pid}/sources/${sid}/files` && r.search === "?limit=200",
      ),
    ).toBe(true);
    await page.getByTestId("sd-remove").click();
    await page.getByTestId("mcs-remove-name").fill("Mixed folder");
    await page.getByTestId("mcs-remove-dialog-confirm").click();
    await expect(card).toHaveCount(0);
    await expect(page.getByTestId("mcs-empty")).toBeVisible();
    const del = requests.filter((r) => r.method === "DELETE");
    expect(del.map((r) => r.path)).toEqual([`/api/backend/projects/${pid}/sources/${sid}`]);
    expect(del[0].headers["x-requested-with"]).toBe("specflow");
  },
);

test(
  "FR-SRC-0019.AC2, SPECS §9: an old API's plain not_found is not a removal: the dialog stays open with the failure text and the card stays",
  { tag: "@production" },
  async ({ page }) => {
    await login(page);
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 404/);
    const pid = await createViaNew(page, unique("Old API"));
    const sid = await seedSource(pid, { kind: "gdrive", name: "Kept folder", files: MIXED });
    await page.goto(`/projects/${pid}/mission-control`);
    await page.route(`**/api/backend/projects/${pid}/sources/${sid}`, (route) => {
      if (route.request().method() !== "DELETE") return route.fallback();
      return route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "not_found", message: "not_found", details: {} } }),
      });
    });
    const card = namedSourceCard(page, "Kept folder");
    await expect(card).toHaveAttribute("data-id", sid);
    await card.getByTestId("source-open").click();
    await page.getByTestId("sd-remove").click();
    await page.getByTestId("mcs-remove-name").fill("Kept folder");
    await page.getByTestId("mcs-remove-dialog-confirm").click();
    await expect(page.getByTestId("mcs-remove-error")).toContainText("Specflow could not remove this source. Try again.");
    await expect(page.getByTestId("mcs-remove-dialog")).toBeVisible();
    await page.getByTestId("mcs-remove-cancel").click();
    await page.keyboard.press("Escape");
    await expect(card).toHaveCount(1);
  },
);

test(
  "FR-SRC-0025.AC3: a source removed in another tab closes the open details within one refresh and shows the 'was removed' note",
  { tag: "@production" },
  async ({ page }) => {
    await login(page);
    const pid = await createViaNew(page, unique("Gone"));
    const sid = await seedSource(pid, { kind: "gdrive", name: "Vanishing folder", files: MIXED });
    await page.goto(`/projects/${pid}/mission-control`);
    const card = namedSourceCard(page, "Vanishing folder");
    await expect(card).toHaveAttribute("data-id", sid);
    await card.getByTestId("source-open").click();
    await expect(page.getByTestId("sd-modal")).toBeVisible();
    await removeSourceElsewhere(pid, sid);
    await expect(page.getByTestId("sd-modal")).toHaveCount(0, { timeout: 15_000 }); // the modal re-reads every 5 s
    await expect(page.getByTestId("toast").filter({ hasText: "Vanishing folder was removed" })).toBeVisible();
    await expect(card).toHaveCount(0, { timeout: 15_000 });
  },
);

test(
  "FR-SRC-0012 [R13]: a source added elsewhere shows within 10 s while nothing runs (the idle poll)",
  { tag: "@production" },
  async ({ page }) => {
    await login(page);
    const pid = await createViaNew(page, unique("Idle poll"));
    await expect(page.getByTestId("mcs-empty")).toBeVisible();
    const sid = await seedSource(pid, { kind: "upload", files: [{ name: "late.pdf", state: "done" }] });
    const card = namedSourceCard(page, "Local files");
    await expect(card).toBeVisible({ timeout: 10_000 });
    await expect(card).toHaveAttribute("data-id", sid);
  },
);
