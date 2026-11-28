// Local files accepts PDF, Word, PowerPoint, Excel, HTML, Markdown, CSV and draw.io diagrams in a real browser, against the stub
// API through the real BFF. The V2 Upload form (the plus dialog's Upload card, `?add=upload`) picks the files: one upload per type
// reaches the API with the canonical Content-Type (whatever type the browser sent), a legacy `.doc` is refused in the page with a
// toast that names the accepted types, and a file over its cap shows the cap. The browser only ever sends the raw file (K5).
import { ADD, canvas, dialog, nodes, unique } from "./canvas-support";
import { allowConsoleErrors, expect, stubLog, syntheticDocument, test } from "./support";

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

/** The Upload form of the plus dialog. */
async function openUpload(page: Parameters<typeof canvas>[0]): Promise<ReturnType<typeof dialog>> {
  await page.getByRole("button", { name: ADD.source }).click();
  await dialog(page, "Add a source")
    .getByRole("button", { name: /^Upload/ })
    .click();
  const form = dialog(page, "Add files");
  await expect(form).toBeVisible();
  return form;
}

test.describe("Local files: accepted types, on the V2 Upload form", { tag: "@production" }, () => {
  for (const [extension, canonical, browserType, head] of TYPES) {
    test(`${extension} is uploaded with Content-Type ${canonical}`, async ({ page, requests }) => {
      const pid = await canvas(page, unique(`Upload ${extension.slice(1)}`));
      const form = await openUpload(page);
      const name = `sample${extension}`;
      await form.getByLabel("Choose files").setInputFiles({ name, mimeType: browserType, buffer: syntheticDocument(extension) });
      await expect(form).toBeHidden(); // the pick itself closes the form: the batch is on its way
      await expect(page.getByTestId("toasts").getByText("1 file added")).toBeVisible();
      const sent = (await stubLog()).filter(
        (e) => e.method === "POST" && e.path.includes(`/projects/${pid}/sources/`) && e.path.endsWith(`/files?filename=${name}`),
      );
      expect(sent).toHaveLength(1);
      expect(sent[0].contentType).toBe(canonical);
      expect(sent[0].head?.startsWith(head)).toBe(true);
      if (extension === ".pdf") {
        // K5: the browser sends the raw file and never drives the parse (the server does that on its own).
        expect(
          requests.filter((r) => r.method === "POST" && r.path.startsWith(`/api/backend/projects/${pid}/documents`)),
          "not the legacy upload",
        ).toEqual([]);
        expect(
          (await stubLog()).filter((e) => e.method === "POST" && e.path.includes(`/api/v1/projects/${pid}/documents/`) && /\/(?:parse|summarize)$/.test(e.path)),
        ).toEqual([]);
      }
    });
  }

  test("the picker offers the ten types and the plus dialog's Upload card says what it is for", async ({ page }) => {
    await canvas(page, unique("Picker"));
    await page.getByRole("button", { name: ADD.source }).click();
    const plus = dialog(page, "Add a source");
    await expect(plus.getByRole("button", { name: /^Upload/ })).toBeEnabled();
    await expect(plus.getByRole("button", { name: /^Upload/ })).toContainText("Files from your computer");
    await plus.getByRole("button", { name: /^Upload/ }).click();
    const form = dialog(page, "Add files");
    const accept = (await form.getByLabel("Choose files").getAttribute("accept")) ?? "";
    expect(accept.split(",").filter((x) => x.startsWith("."))).toEqual([
      ".pdf",
      ".docx",
      ".pptx",
      ".xlsx",
      ".html",
      ".md",
      ".csv",
      ".drawio",
      ".drawio.svg",
      ".drawio.png",
    ]);
  });

  test("a legacy .doc is refused in the page: nothing is sent, the toast (a live region) names the accepted types", async ({ page }) => {
    const pid = await canvas(page, unique("Legacy"));
    const form = await openUpload(page);
    const before = (await stubLog()).length;
    await form.getByLabel("Choose files").setInputFiles({ name: "old.doc", mimeType: "application/msword", buffer: syntheticDocument(".docx") });
    await expect(form).toBeHidden();
    const toast = page.getByTestId("toasts").getByTestId("toast");
    await expect(toast).toContainText("old.doc was not added.");
    await expect(toast).toContainText(
      "PDF, Word, PowerPoint, Excel, HTML, Markdown, CSV, Diagram, Diagram SVG or Diagram PNG files (.pdf, .docx, .pptx, .xlsx, .html, .md, .csv, .drawio, .drawio.svg, .drawio.png)",
    );
    await expect(page.getByTestId("toasts")).toHaveAttribute("aria-live", "polite");
    expect((await stubLog()).slice(before).filter((e) => e.path.includes(`/projects/${pid}/sources`))).toEqual([]);
    await expect(nodes(page)).toHaveCount(0); // no batch was made
  });

  test("a file over its cap shows the cap of its type (413 file_too_large, 10 MB for CSV)", async ({ page }) => {
    allowConsoleErrors(page, /status of 413/); // the browser logs the refused request itself
    await canvas(page, unique("Too large"));
    const form = await openUpload(page);
    await form
      .getByLabel("Choose files")
      .setInputFiles({ name: "big.csv", mimeType: "text/csv", buffer: Buffer.concat([Buffer.from("a,b\n"), Buffer.alloc(10 * MB, 0x31)]) });
    await expect(page.getByTestId("toasts").getByTestId("toast")).toContainText("big.csv is over the 10 MB limit.");
  });
});
