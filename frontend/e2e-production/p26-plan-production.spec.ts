// P26 Plan page on the PRODUCTION routes, against the stub API through the real BFF (FE-PLAN-09 and FE-PLAN-11, AUTO-23, P19-02/16/17).
// No paid model call (R11): the stub answers upload, parse and summarize with adjustable timings. The Plan content is not served yet
// (SPL-DEMO-02: the `plan` port answers an empty table and idle), so after the gate has read the project's documents the Plan page sends
// no `/api/backend` request at all, and "Create PLAN" shows one neutral toast and stays idle. `p17-production.spec.ts` is not touched: its
// per-section regex admits no document read, so the gate's one GET is allowed here, by an explicit project-scoped regex (not a looser shell rule).
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import { networkQuiet } from "../e2e/support";
import {
  allowedGateCalls,
  calls,
  createProjectApi,
  expectGateNetwork,
  createViaNew,
  expect,
  expectUploadReady,
  login,
  pickLocalPdf,
  rail,
  STUB_TIMING,
  stubTiming,
  test,
} from "./support";

test.describe("P26 Plan page on production routes", { tag: "@production" }, () => {
  const DATA_STAGE = "data-stage";

  test.setTimeout(120_000);

  const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;

  const screen = (page: Page) => page.getByTestId("screen-plan");
  const create = (page: Page) => page.getByRole("button", { name: "Create PLAN", exact: true });

  test.afterEach(async () => {
    await stubTiming(STUB_TIMING);
  });

  // (a) FE-PLAN-10, FE-PLAN-11.AC3
  test("FE-PLAN-11.AC3, FE-PLAN-10 (a): a project with no processed document shows 'No plan yet'; the only backend calls are the shell's and one GET of the sources status; no parse or summarize", async ({
    page,
    requests,
  }) => {
    await login(page);
    const pid = await createProjectApi(page, unique("Plan empty"));
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(page.getByTestId("mcs-empty")).toBeVisible();
    const from = requests.length;
    await rail(page).getByRole("button", { name: "Plan", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/plan$`));
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "empty");
    await expect(page.getByTestId("plan-gate").getByRole("heading", { name: "No plan yet" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Create PLAN", exact: true })).toHaveCount(0);
    await expect(page.getByRole("table")).toHaveCount(0);
    await expectGateNetwork(page, requests, from, pid);
  });

  /** A project that reached Ready through the upload flow of p17-production.spec.ts (stub API, no paid call); the page sits on its Mission Control. */
  async function readyProject(page: Page): Promise<string> {
    await login(page);
    const pid = await createViaNew(page, unique("Plan ready"));
    await pickLocalPdf(page, "requirements.pdf"); // Local files; the server parses (stub 1.5 s); Ready = one processed document
    await expectUploadReady(page);
    return pid;
  }

  /** From Mission Control by the sidebar to the Plan gate answered (Stage 0). Returns the request count at that moment. */
  async function openPlan(page: Page, requests: unknown[]): Promise<number> {
    await rail(page).getByRole("button", { name: "Plan", exact: true }).click();
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "idle");
    await expect(screen(page).getByRole("table")).toBeVisible();
    return requests.length;
  }

  // (b) FE-PLAN-11.AC4, SPL-DEMO-02
  test("FE-PLAN-11.AC4, SPL-DEMO-02 (b): a Ready project shows Stage 0 with an empty table and 'Create PLAN'; after the gate no /api/backend request", async ({
    page,
    requests,
  }) => {
    const pid = await readyProject(page);
    const from = requests.length;
    const afterGate = await openPlan(page, requests);
    const made = calls(requests.slice(from, afterGate));
    const allowed = allowedGateCalls(pid);
    expect(
      made.filter((r) => !allowed(r)).map((r) => `${r.method} ${r.path}`),
      "backend calls while the gate answers",
    ).toEqual([]);
    await expect(create(page)).toBeVisible();
    await expect(
      screen(page)
        .getByRole("row")
        .filter({ has: page.getByRole("rowheader") }),
    ).toHaveCount(1); // the Total row only
    await expect(page.getByTestId("plan-total-row").getByRole("cell")).toHaveText(["0", "0", "0", "0"]);
    await networkQuiet(page, 2_500); // no request for longer than a poll interval
    expect(
      calls(requests.slice(afterGate)).map((r) => `${r.method} ${r.path}`),
      "no /api/backend request after the gate (FE-PLAN-11.AC4)",
    ).toEqual([]);
  });

  // (c) SPL-DEMO-02: "Create PLAN" has nothing to create; it must not loop
  test("SPL-DEMO-02 (c): 'Create PLAN' shows one neutral not-available toast per press, stays in Stage 0 and sends no request; a reload shows Stage 0; production has no 'Reset demo'", async ({
    page,
    requests,
  }) => {
    const pid = await readyProject(page);
    const afterGate = await openPlan(page, requests);
    await create(page).click();
    await expect(page.getByTestId("toast")).toHaveCount(1);
    await expect(page.getByTestId("toast")).toContainText("Plan creation is not available yet.");
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "idle");
    await networkQuiet(page, 2_500);
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "idle");
    expect(
      calls(requests.slice(afterGate)).map((r) => `${r.method} ${r.path}`),
      "no /api/backend request after the press",
    ).toEqual([]);
    await page.reload();
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "idle");
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/plan$`));
    await page.getByRole("button", { name: /^Account:/ }).click();
    await expect(page.getByRole("menuitem", { name: "Reset demo" })).toHaveCount(0);
    await page.keyboard.press("Escape");
  });

  test("FE-PLAN-09.AC1 (c): leaving Plan for Decisions and coming back shows Stage 0 again", async ({ page, requests }) => {
    await readyProject(page);
    await openPlan(page, requests);
    await rail(page).getByRole("button", { name: "Decisions", exact: true }).click();
    await expect(page.getByTestId("screen-decisions")).toBeVisible();
    await rail(page).getByRole("button", { name: "Plan", exact: true }).click();
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "idle");
    await expect(create(page)).toBeVisible();
  });
});
