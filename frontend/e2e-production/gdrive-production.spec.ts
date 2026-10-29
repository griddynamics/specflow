// FR-GDUI-0002..0021 on the PRODUCTION routes (GDRIVE plan 07), against the stub API through the real BFF (scripts/serve-production.mjs;
// no Google, no model call). The modal, the card and the lock are the same components as in the UI; here: the real adapter end to end (Add source -> Test connection -> Save -> card), the reasons by the envelope code, the lock,
// and the Drive source reaching the project's status. The stub keeps a saved source in `listing`.
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import { createViaNew, expect, expectSourceCard, login, test } from "./support";

const unique = (p: string) => `${p} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
const folder = (id: string) => `https://drive.google.com/drive/folders/${id}`;

async function openDialog(page: Page) {
  await page.getByTestId("mcs-add-source").click();
  await page.getByRole("menuitem", { name: /Google Drive/ }).click();
  const dialog = page.getByRole("dialog", { name: "Connect a Google Drive folder" });
  await expect(dialog).toBeVisible();
  return dialog;
}

test(
  "FR-GDUI-0009, FR-GDUI-0017, FR-GDUI-0018, FR-GDUI-0021: Add source -> Google Drive -> Test -> Save: the card lists files, Build knowledge is locked with its reason",
  { tag: "@production" },
  async ({ page, requests }) => {
    await login(page);
    const pid = await createViaNew(page, unique("Drive"));
    const dialog = await openDialog(page);
    await dialog.getByTestId("drive-link").fill(folder("fake-synthetic-30"));
    await dialog.getByTestId("drive-primary").click();
    await expect(dialog.getByTestId("drive-summary")).toContainText("We found 11 files in 3 folders.");
    await dialog.getByTestId("drive-primary").click(); // Save
    await expect(dialog).toHaveCount(0);
    const card = await expectSourceCard(page, "gdrive");
    await expect(card).toContainText("Recorded folder");
    await expect(card.getByTestId("source-counts")).toContainText("Listing files");
    await expect(page.getByTestId("kb-build")).toHaveAttribute("aria-disabled", "true");
    await expect(page.getByTestId("kb-detail")).toContainText("Build knowledge starts after all files are imported. Listing files");
    // The calls: identity, test and save, each once, as POSTs with the CSRF header and the link only in the body.
    const mine = requests.filter(
      (r) => r.path.startsWith(`/api/backend/projects/${pid}/sources`) || r.path === "/api/backend/drive/identity",
    );
    expect(mine.filter((r) => r.method === "POST").map((r) => r.path)).toEqual([
      `/api/backend/projects/${pid}/sources/gdrive/test`,
      `/api/backend/projects/${pid}/sources/gdrive`,
    ]);
    expect(mine.filter((r) => r.method === "POST").every((r) => r.headers["x-requested-with"] === "specflow" && !r.search)).toBe(true);
  },
);

test(
  "FR-GDUI-0012, INT-GDUI-0002: a Drive reason in the envelope shows its message in the modal; the server text never does",
  { tag: "@production" },
  async ({ page }) => {
    await login(page);
    await createViaNew(page, unique("Drive reasons"));
    const dialog = await openDialog(page);
    const failure = dialog.getByTestId("drive-failure");
    const messages: string[] = [];
    for (const id of ["fake-reason-no_access", "fake-reason-drive_unavailable", "fake-reason-not_a_folder"]) {
      await dialog.getByTestId("drive-link").fill(folder(id));
      await dialog.getByTestId("drive-primary").click();
      await expect(failure).toBeVisible();
      const text = await failure.innerText();
      expect(text).not.toMatch(/SERVER TEXT|fake-reason|http_/i);
      expect(messages, "each reason has its own message").not.toContain(text);
      messages.push(text);
    }
  },
);
