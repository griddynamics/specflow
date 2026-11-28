// FR-GDUI-0002..0021 on the PRODUCTION routes (GDRIVE plan 07), against the stub API through the real BFF (scripts/serve-production.mjs;
// no Google, no model call). The V2 `?add=gdrive` deep link opens the same Drive form the plus dialog does: the file checklist of a tested
// folder (the skipped categories are not listed), the files left out go to the save as `exclude`, and the saved folder is the canvas node
// that keeps the Graph hub waiting. A reason in the test envelope shows its message, never the server text (INT-GDUI-0002).
import { bodiesOf, canvas, dialog, FOLDER, hub, nodes, STATE, unique } from "./canvas-support";
import { expect, test } from "./support";

const folder = (id: string) => `https://drive.google.com/drive/folders/${id}`;

test.describe("Google Drive on the V2 canvas", { tag: "@production" }, () => {
  test("FR-GDUI-0009, FR-GDUI-0017, FR-GDUI-0018: ?add=gdrive opens the form; the checklist starts checked, the files left out are excluded, and the folder becomes its node", async ({
    page,
    requests,
  }) => {
    const pid = await canvas(page, unique("Drive"));
    await page.goto(`/projects/${pid}?add=gdrive`);
    const form = dialog(page, "Connect a Google Drive folder");
    await expect(form).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}$`)); // the deep link leaves the address clean

    await form.getByLabel("Google Drive folder link").fill(FOLDER);
    await form.getByRole("button", { name: "Test connection" }).click();
    await expect(form.getByText(/Recorded folder\. We found 11 files in 3 folders\./)).toBeVisible();
    const list = form.getByRole("group", { name: "Files to add" });
    await expect(list.getByRole("checkbox")).toHaveCount(10); // the shortcut is not offered
    await expect(list.getByRole("checkbox", { checked: true })).toHaveCount(10);
    await expect(form.getByText("10 of 10 files")).toBeVisible();

    await form.getByRole("button", { name: "Select none" }).click();
    await expect(form.getByText("No files")).toBeVisible();
    await expect(form.getByRole("button", { name: "Add source" })).toBeDisabled();
    await form.getByRole("button", { name: "Select all" }).click();
    await list.getByRole("checkbox", { name: /Contract\.pdf/ }).uncheck();
    await list.getByRole("checkbox", { name: /Letter\.docx/ }).uncheck();
    await expect(form.getByText("8 of 10 files")).toBeVisible();

    await form.getByRole("button", { name: "Add source" }).click();
    await expect(form).toBeHidden();
    const node = nodes(page).filter({ hasText: "Recorded folder" });
    await expect(node).toHaveAttribute(STATE, "busy");
    await expect(node).toContainText("Listing files…");
    await expect(hub(page, "graph")).toHaveAttribute(STATE, "wait"); // the build waits for the import

    const posts = requests.filter((r) => r.method === "POST" && r.path.startsWith(`/api/backend/projects/${pid}/sources/gdrive`));
    expect(posts.map((r) => r.path)).toEqual([
      `/api/backend/projects/${pid}/sources/gdrive/test`,
      `/api/backend/projects/${pid}/sources/gdrive`,
    ]);
    expect(posts.every((r) => r.headers["x-requested-with"] === "specflow" && !r.search)).toBe(true);
    expect(bodiesOf(posts)).toEqual([
      { link: FOLDER }, // Test connection sends the link alone
      { link: FOLDER, exclude: ["fake-synthetic-30-f3", "fake-synthetic-30-f10"] }, // the save names the files left out
    ]);
  });

  test("FR-GDUI-0012, INT-GDUI-0002: a Drive reason in the envelope shows its message in the form; the server text never does", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("Drive reasons"));
    await page.goto(`/projects/${pid}?add=gdrive`);
    const form = dialog(page, "Connect a Google Drive folder");
    const failure = form.getByRole("alert");
    const messages: string[] = [];
    for (const id of ["fake-reason-no_access", "fake-reason-drive_unavailable", "fake-reason-not_a_folder"]) {
      await form.getByLabel("Google Drive folder link").fill(folder(id));
      await form.getByRole("button", { name: "Test connection" }).click();
      await expect(failure).toBeVisible();
      const text = await failure.innerText();
      expect(text).not.toMatch(/SERVER TEXT|fake-reason|http_/i);
      expect(messages, "each reason has its own message").not.toContain(text);
      messages.push(text);
    }
  });
});
