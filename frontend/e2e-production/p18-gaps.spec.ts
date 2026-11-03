// P18 T11 (FE-BP-02.AC4, AC6): browser-level cases the P17 spec did not cover, on the production routes against the stub API
// (scripts/serve-production.mjs; no backend, no model call).
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import {
  allowConsoleErrors,
  bff,
  confirmDialog,
  createProjectApi,
  type DocumentStatus,
  expect,
  expectSourceCard,
  kbBuild,
  login,
  openCreateProject,
  readyProject,
  seedDocument,
  seedSource,
  settled,
  startBuild,
  STUB_TIMING,
  stubLog,
  stubTiming,
  test,
} from "./support";

const NL = "\n";
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

// ---- plans/connect-build-kg-button: a failed knowledge build (CTORNDGAIN-2244) ----

const errorDialog = (page: Page) => page.getByTestId("kb-error-dialog");
const expectState = (page: Page, state: "failed" | "running") => expect(kbBuild(page)).toHaveAttribute("data-state", state);
const errorText = (page: Page) => page.getByTestId("kb-error-text");
const copyButton = (page: Page) => page.getByTestId("kb-error-copy");
const retryButton = (page: Page) => page.getByTestId("kb-error-retry");
/** Line endings and outer whitespace differ by engine; the report text is compared normalized. */
const norm = (t: string) => t.replace(/\r\n/g, NL).trim();
const BUILD_PROJECT = "Build error";
const COPY_LABEL = "Copy details";
const RETRY_LABEL = "Try again";
const builtToast = (page: Page) => page.getByTestId("toast");

/** A start the API refuses (503): the red Error button, no toast. Console shows the browser's own line for the 503. */
async function failedStart(page: Page): Promise<string> {
  allowConsoleErrors(page, /status of 503/);
  const pid = await readyProject(page, BUILD_PROJECT);
  await stubTiming({ graphStartFail: "graph_build_unavailable" });
  await startBuild(page);
  await expectState(page, "failed");
  return pid;
}

test.describe("Knowledge build errors", { tag: "@production" }, () => {
  test("AC-U12, AC-U1e, AC-U9: a refused start (503) turns the button into a red Error with an icon, shows no toast, and opens the error modal with ids and codes only; the page has no query", async ({
    page,
  }) => {
    const pid = await failedStart(page);
    await expect(kbBuild(page)).toContainText("Error");
    await expect(kbBuild(page)).toHaveClass(/mcs-btn--error/);
    await expect(kbBuild(page)).not.toHaveAttribute("aria-disabled", /.*/); // not disabled: it opens the details
    await expect(kbBuild(page)).toHaveAttribute("data-mode", "first"); // Try again would start the first build
    expect(await kbBuild(page).evaluate((el) => el.querySelectorAll("svg").length), "the icon: not colour alone").toBe(1);
    await expect(page.getByTestId("kb-detail")).toHaveText("The build failed (graph_build_unavailable). Open for details.");
    await settled(page);
    await expect(builtToast(page)).toHaveCount(0);
    await expect(page.getByTestId("kb-menu")).toHaveCount(0);
    await kbBuild(page).click();
    const modal = page.getByRole("dialog");
    await expect(modal).toContainText("Knowledge build failed");
    await expect(modal).toContainText("Copy the details and send them to the Specflow team.");
    await expect(page.getByTestId("kb-error-close")).toHaveText("Close");
    await expect(copyButton(page)).toHaveText(COPY_LABEL);
    await expect(retryButton(page)).toHaveText(RETRY_LABEL);
    const text = (await errorText(page).innerText()).split(NL);
    const origin = new URL(page.url()).origin;
    expect(text).toHaveLength(8);
    expect(text.slice(0, 5)).toEqual([
      "Specflow knowledge build failed",
      `Project: ${pid}`,
      "Build: —",
      "Mode: —",
      "Error code: graph_build_unavailable",
    ]);
    expect(text[5]).toMatch(/^Started \(UTC\): \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
    expect(text[6]).toBe("Finished (UTC): —");
    expect(text[7]).toBe(`Page: ${origin}/projects/${pid}/mission-control`);
    expect(text.join(NL)).not.toMatch(/NaN|\?|#/);
  });

  test("AC-U12: a build that ends failed (GET state) is the same Error, with the server's error code; no toast", async ({ page }) => {
    await readyProject(page, BUILD_PROJECT);
    await stubTiming({ graphMs: 0, graphOutcome: "failed" });
    await startBuild(page);
    await expect(kbBuild(page)).toHaveAttribute("data-state", "failed", { timeout: 20_000 });
    await expect(page.getByTestId("kb-detail")).toHaveText("The build failed (stub_failure). Open for details.");
    await settled(page);
    await expect(builtToast(page)).toHaveCount(0);
    await kbBuild(page).click();
    const text = await errorText(page).innerText();
    expect(text).toMatch(/^Build: gb-\d+$/m);
    expect(text).toMatch(/^Mode: first$/m);
    expect(text).toMatch(/^Error code: stub_failure$/m);
    expect(text).toMatch(/^Finished \(UTC\): \d{4}-/m);
    // A later successful build clears the Error.
    await retryButton(page).click();
    await expect(confirmDialog(page)).toBeVisible();
    await stubTiming({ graphMs: 0, graphOutcome: "succeeded" });
    await page.getByTestId("kb-build-dialog-confirm").click();
    await expect(kbBuild(page)).toContainText("Update knowledge", { timeout: 20_000 });
    await expect(errorDialog(page)).toHaveCount(0);
  });

  test("AC-U14: Copy details writes the report to the clipboard and reads Copied", async ({ page, context, baseURL }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: new URL(baseURL ?? "http://127.0.0.1:3260").origin });
    await failedStart(page);
    await kbBuild(page).click();
    await copyButton(page).click();
    await expect(copyButton(page)).toHaveText("Copied");
    await expect(page.getByTestId("kb-error-copy-status")).toHaveText("Copied");
    expect(norm(await page.evaluate(() => navigator.clipboard.readText()))).toBe(norm(await errorText(page).innerText()));
    // The label goes back by itself.
    await expect(copyButton(page)).toHaveText("Copy details", { timeout: 10_000 });
  });

  test("AC-U14: a refused clipboard selects the report text and shows the hint; no toast", async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: () => Promise.reject(new DOMException("denied", "NotAllowedError")) },
      });
    });
    await failedStart(page);
    await kbBuild(page).click();
    await copyButton(page).click();
    await expect(copyButton(page)).toHaveText(COPY_LABEL);
    await expect(page.getByText("Press Ctrl+C or Cmd+C to copy")).toBeVisible();
    const selected = await page.evaluate(() => window.getSelection()?.toString() ?? "");
    expect(norm(selected)).toBe(norm(await errorText(page).innerText()));
    await settled(page);
    await expect(builtToast(page)).toHaveCount(0);
  });

  test("AC-U15, AC-U16: Try again opens the confirm of the same mode; Cancel returns the focus to the Build button; Close does too (all by keyboard)", async ({
    page,
    requests,
  }) => {
    const pid = await failedStart(page);
    const posts = () => requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/graph_build`).length;
    expect(posts()).toBe(1);
    // Close.
    await kbBuild(page).focus();
    await page.keyboard.press("Enter");
    await expect(errorDialog(page)).toBeVisible();
    await page.getByTestId("kb-error-close").focus();
    await page.keyboard.press("Enter");
    await expect(errorDialog(page)).toHaveCount(0);
    await expect(kbBuild(page)).toBeFocused();
    // Try again -> confirm -> Cancel.
    await page.keyboard.press("Enter");
    await expect(errorDialog(page)).toBeVisible();
    await retryButton(page).focus();
    await page.keyboard.press("Enter");
    await expect(errorDialog(page)).toHaveCount(0);
    const confirm = confirmDialog(page);
    await expect(confirm).toBeVisible();
    await expect(page.getByRole("dialog")).toContainText("Build knowledge?"); // the first build: the failed one never succeeded
    await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).focus();
    await page.keyboard.press("Enter");
    await expect(confirm).toHaveCount(0);
    await expect(kbBuild(page)).toBeFocused();
    expect(posts(), "Cancel sends nothing").toBe(1);
    await expectState(page, "failed"); // the Error stays until a build starts
  });

  test("AC-U15, AC-U4: while files are still being imported the Error stays but Try again is blocked with the reason; a click starts nothing", async ({
    page,
    requests,
  }) => {
    const pid = await failedStart(page);
    await kbBuild(page).click();
    await expect(retryButton(page)).not.toHaveAttribute("aria-disabled", /.*/);
    // Another tab adds a source that still waits for its files: the next poll (at most 5 s) locks the build.
    await seedSource(pid, { kind: "upload", name: "More files", files: [{ name: "b.pdf", state: "waiting" }] });
    const retry = retryButton(page);
    await expect(retry).toHaveAttribute("aria-disabled", "true", { timeout: 20_000 });
    await expect(page.getByTestId("kb-error-blocked")).toContainText("Build knowledge starts after all files are imported.");
    await expectState(page, "failed"); // still red
    const posts = () => requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/graph_build`).length;
    const before = posts();
    await retry.focus(); // aria-disabled: press Enter instead of a click
    await page.keyboard.press("Enter");
    await settled(page);
    await expect(confirmDialog(page)).toHaveCount(0);
    expect(posts()).toBe(before);
  });

  test("AC-U12: the error modal closes by itself when the failure clears (a build started elsewhere is read)", async ({ page }) => {
    const pid = await failedStart(page);
    await kbBuild(page).click();
    await expect(errorDialog(page)).toBeVisible();
    // Another tab starts a build (the API accepts it now): the next poll reads it running and the failure is gone.
    await stubTiming({ graphStartFail: null, graphMs: null });
    const start = await bff(page, "POST", `/projects/${pid}/graph_build`, { reasoning_level: 3 });
    expect(start.status).toBe(202);
    await expect(errorDialog(page)).toHaveCount(0, { timeout: 20_000 });
    await expect(kbBuild(page)).toContainText("Building…");
    await expect(kbBuild(page)).toBeFocused(); // Modal returns the focus to the opener
  });

  test("AC-D5: a build refused by the API for lack of documents (409) shows the Error with its code", async ({ page }) => {
    await login(page);
    allowConsoleErrors(page, /status of 409/);
    const pid = await createProjectApi(page, unique("409"));
    // A parsed upload makes the button active; the project is then given no processed document from the API's point of view.
    await stubTiming({ parseMs: 0 });
    const id = await seedDocument(page, pid, "x.pdf");
    await expect
      .poll(async () => ((await bff(page, "GET", `/projects/${pid}/documents/${id}`)).json as { status: DocumentStatus }).status)
      .toBe("parsed");
    await page.route(`**/api/backend/projects/${pid}/graph_build`, (route) =>
      route.request().method() !== "POST"
        ? route.fallback()
        : route.fulfill({
            status: 409,
            contentType: "application/json",
            body: JSON.stringify({ error: { code: "no_processed_documents", message: "none", details: {} } }),
          }),
    );
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(kbBuild(page)).toHaveAttribute("data-mode", "first");
    await startBuild(page);
    await expectState(page, "failed");
    await expect(page.getByTestId("kb-detail")).toHaveText("The build failed (no_processed_documents). Open for details.");
    await settled(page);
    await expect(builtToast(page)).toHaveCount(0);
    // The error modal names the API's code; the stubbed project still has a processed document, so Try again is enabled.
    await kbBuild(page).click();
    await expect(errorText(page)).toContainText("Error code: no_processed_documents");
    await expect(retryButton(page)).toBeEnabled();
    await expect(retryButton(page)).not.toHaveAttribute("aria-disabled", /.*/);
  });
});
