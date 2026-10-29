// P18 T11 (FE-BP-02.AC4, AC6): browser-level cases the P17 spec did not cover, on the production routes against the stub API
// (scripts/serve-production.mjs; no backend, no model call).
import { randomBytes } from "node:crypto";
import {
  allowConsoleErrors,
  bff,
  createProjectApi,
  expect,
  expectSourceCard,
  login,
  openCreateProject,
  seedDocument,
  STUB_TIMING,
  stubLog,
  stubTiming,
  test,
} from "./support";

type DocumentStatus = "uploaded" | "processing" | "parsed" | "failed" | "ingested" | "partially_ingested"; // backend DOCUMENT_STATUSES

const unique = (p: string) => `${p} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;

test.afterEach(async () => {
  await stubTiming(STUB_TIMING);
});

test(
  "FE-BP-02.AC4: a 422 on create shows an error on the Name field and the modal stays open",
  { tag: "@production" },
  async ({ page, requests }) => {
    await login(page);
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 422/);
    await page.route("**/api/backend/projects", (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      return route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "validation_failed", message: "validation_failed", details: {} } }),
      });
    });
    const urlBefore = page.url();
    await openCreateProject(page);
    await page.getByLabel("Name").fill(unique("Rejected by the API"));
    const before = requests.filter((r) => r.method === "POST" && r.path === "/api/backend/projects").length;
    await page.getByTestId("cp-create").click();
    await expect.poll(() => requests.filter((r) => r.method === "POST" && r.path === "/api/backend/projects").length).toBe(before + 1);
    await expect(page.getByTestId("create-project-modal")).toBeVisible();
    const name = page.getByLabel("Name");
    await expect(name).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByTestId("create-project-modal")).toContainText("Enter a valid project name.");
    await expect(page.getByTestId("cp-error")).toHaveCount(0); // the generic alert is for non-validation failures
    expect(page.url(), "no navigation to a new project").toBe(urlBefore);
  },
);

test(
  "FE-BP-02.AC6, K5: a project whose file is already parsed opens with the Local files card done, Build knowledge unlocked and no parse or summarize request",
  { tag: "@production" },
  async ({ page }) => {
    await login(page);
    const pid = await createProjectApi(page, unique("Already parsed"));
    const id = await seedDocument(page, pid, "done.pdf");
    await expect
      .poll(async () => ((await bff(page, "GET", `/projects/${pid}/documents/${id}`)).json as { status: DocumentStatus }).status)
      .toBe("parsed");
    const work = async () =>
      (await stubLog()).filter((e) => e.method === "POST" && /\/(?:parse|summarize)$/.test(e.path) && e.path.includes(`/projects/${pid}/`))
        .length;
    await page.goto(`/projects/${pid}/mission-control`);
    await expectSourceCard(page, "upload", { state: "done" });
    await expect(page.getByTestId("kb-build")).toHaveAttribute("data-mode", "first");
    expect(await work(), "opening a finished project starts no work").toBe(0);
  },
);
