// Local files accepts PDF, Word, PowerPoint, Excel, HTML, Markdown and CSV in a real browser, against the stub
// API through the real BFF. One upload per type reaches the API with the canonical Content-Type (whatever type the browser sent);
// a legacy `.doc` is refused in the page with a toast that names the accepted types; a file over its cap shows the cap.
import { randomBytes } from "node:crypto";
import {
  allowConsoleErrors,
  createViaNew,
  expect,
  expectSourceCard,
  login,
  pickLocalFile,
  stubLog,
  syntheticDocument,
  test,
} from "./support";

const MB = 1024 * 1024;
const OOXML = "application/vnd.openxmlformats-officedocument";
// extension, canonical type (the stub wants it exactly), the type a browser sends, the first bytes the API checks.
const TYPES = [
  [".pdf", "application/pdf", "application/pdf", "%PDF-"],
  [".docx", `${OOXML}.wordprocessingml.document`, `${OOXML}.wordprocessingml.document`, "PK\x03\x04"],
  [".pptx", `${OOXML}.presentationml.presentation`, `${OOXML}.presentationml.presentation`, "PK\x03\x04"],
  [".xlsx", `${OOXML}.spreadsheetml.sheet`, `${OOXML}.spreadsheetml.sheet`, "PK\x03\x04"],
  [".html", "text/html", "text/html", "<!doc"],
  [".md", "text/markdown", "text/plain", "# Tit"], // some browsers say text/plain, others ""
  [".csv", "text/csv", "application/vnd.ms-excel", "a,b\n1"], // Windows
] as const;
const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;

test.describe("Local files: accepted types", { tag: "@production" }, () => {
  for (const [extension, canonical, browserType, head] of TYPES) {
    test(`${extension} is uploaded with Content-Type ${canonical}`, async ({ page }) => {
      await login(page);
      const pid = await createViaNew(page, unique(`Upload ${extension.slice(1)}`));
      const name = `sample${extension}`;
      await pickLocalFile(page, name, browserType, syntheticDocument(extension));
      await expectSourceCard(page, "upload");
      await expect(page.getByTestId("toasts").getByText("1 file added")).toBeVisible();
      const sent = (await stubLog()).filter(
        (e) => e.method === "POST" && e.path.includes(`/projects/${pid}/sources/`) && e.path.endsWith(`/files?filename=${name}`),
      );
      expect(sent).toHaveLength(1);
      expect(sent[0].contentType).toBe(canonical);
      expect(sent[0].head?.startsWith(head)).toBe(true);
    });
  }

  test("the picker offers the seven types and the menu says so", async ({ page }) => {
    await login(page);
    await createViaNew(page, unique("Picker"));
    const accept = (await page.getByTestId("mcs-upload-input").getAttribute("accept")) ?? "";
    expect(accept.split(",").filter((x) => x.startsWith("."))).toEqual([".pdf", ".docx", ".pptx", ".xlsx", ".html", ".md", ".csv"]);
    await page.getByTestId("mcs-add-source").click();
    await expect(page.getByRole("menuitem", { name: /Local files/ })).toContainText("Upload PDF, Office, HTML, Markdown or CSV files");
  });

  test("a legacy .doc is refused in the page: nothing is sent, the toast (a live region) names the accepted types", async ({ page }) => {
    await login(page);
    const pid = await createViaNew(page, unique("Legacy"));
    const before = (await stubLog()).length;
    await pickLocalFile(page, "old.doc", "application/msword", Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0, 0, 0, 0]));
    const toast = page.getByTestId("toasts").getByTestId("toast");
    await expect(toast).toContainText("old.doc was not added.");
    await expect(toast).toContainText(
      "PDF, Word, PowerPoint, Excel, HTML, Markdown or CSV files (.pdf, .docx, .pptx, .xlsx, .html, .md, .csv)",
    );
    await expect(page.getByTestId("toasts")).toHaveAttribute("aria-live", "polite");
    expect((await stubLog()).slice(before).filter((e) => e.path.includes(`/projects/${pid}/sources`))).toEqual([]);
    await expect(page.getByTestId("mcs-empty")).toBeVisible();
  });

  test("a file over its cap shows the cap of its type (413 file_too_large, 10 MB for CSV)", async ({ page }) => {
    await login(page);
    allowConsoleErrors(page, /status of 413/); // the browser logs the refused request itself
    await createViaNew(page, unique("Too large"));
    await pickLocalFile(page, "big.csv", "text/csv", Buffer.concat([Buffer.from("a,b\n"), Buffer.alloc(10 * MB, 0x31)]));
    await expect(page.getByTestId("toasts").getByTestId("toast")).toContainText("big.csv is over the 10 MB limit.");
  });
});
