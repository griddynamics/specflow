// P26 Plan page on the PRODUCTION routes, against the stub API through the real BFF (FE-PLAN-09 and FE-PLAN-11, AUTO-23, P19-02/16/17).
// No paid model call (R11): the stub answers upload, parse, summarize, the graph build and the plan runs with adjustable timings. After the
// gate, Stage 0 reads the plan state and the preview once and then polls nothing; "Create PLAN" without a built graph shows the API's
// refusal; with one, the run goes through its steps to Stage 2. `p17-production.spec.ts` is not touched: its per-section regex admits no
// document read, so the gate's one GET is allowed here, by an explicit project-scoped regex (not a looser shell rule).
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import { networkQuiet } from "../e2e/support";
import {
  allowConsoleErrors,
  allowedGateCalls,
  bff,
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
    await expect(screen(page).getByTestId("plan-preview")).toHaveAttribute("data-loaded", ""); // the preview has answered
    return requests.length;
  }

  /** Builds the project's knowledge graph through the BFF (the stub resolves a build when it is read after `graphMs`). */
  async function buildGraph(page: Page, pid: string): Promise<void> {
    expect((await bff(page, "POST", `/projects/${pid}/graph_build`, { reasoning_level: 3 })).status).toBe(202);
    await expect
      .poll(
        async () =>
          ((await bff(page, "GET", `/projects/${pid}/graph_build`)).json as { status: "idle" | "running" | "succeeded" | "failed" }).status,
        { timeout: 10_000 },
      )
      .toBe("succeeded");
  }

  const planReads = (pid: string) => [`GET /api/backend/projects/${pid}/plan`, `GET /api/backend/projects/${pid}/plan/preview`];

  // (b) FE-PLAN-11.AC4
  test("FE-PLAN-11.AC4 (b): a Ready project without a graph shows Stage 0 with no features and 'Create PLAN'; it reads the plan once and polls nothing", async ({
    page,
    requests,
  }) => {
    const pid = await readyProject(page);
    const from = requests.length;
    const afterGate = await openPlan(page, requests);
    const made = calls(requests.slice(from, afterGate));
    const allowed = allowedGateCalls(pid);
    const plan = new Set(planReads(pid));
    expect(
      made.filter((r) => !allowed(r) && !plan.has(`${r.method} ${r.path}`)).map((r) => `${r.method} ${r.path}`),
      "backend calls while the gate and Stage 0 answer",
    ).toEqual([]);
    await expect(create(page)).toBeVisible();
    await expect(screen(page).getByRole("heading", { name: "No features to plan yet" })).toBeVisible();
    await expect(screen(page).getByRole("button", { name: "Open Mission Control" })).toBeVisible();
    await networkQuiet(page, 2_500); // no request for longer than a poll interval
    expect(
      calls(requests.slice(afterGate)).map((r) => `${r.method} ${r.path}`),
      "no /api/backend request once Stage 0 shows (FE-PLAN-11.AC4)",
    ).toEqual([]);
  });

  // (c) "Create PLAN" before the graph is built: the API refuses, the page says why and stays in Stage 0
  test("FE-PLAN-03 (c): 'Create PLAN' without a built graph shows the refusal once per press and stays in Stage 0; a reload shows Stage 0; production has no 'Reset demo'", async ({
    page,
    requests,
  }) => {
    const pid = await readyProject(page);
    const afterGate = await openPlan(page, requests);
    allowConsoleErrors(page, /status of 409/); // the browser logs the API's refusal
    await create(page).click();
    await expect(page.getByTestId("toast")).toHaveCount(1);
    await expect(page.getByTestId("toast")).toContainText("Build the knowledge graph before creating the plan.");
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "idle");
    await networkQuiet(page, 2_500);
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "idle");
    expect(
      calls(requests.slice(afterGate)).map((r) => `${r.method} ${r.path}`),
      "one POST and nothing after it",
    ).toEqual([`POST /api/backend/projects/${pid}/plan`]);
    await page.reload();
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "idle");
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/plan$`));
    await page.getByRole("button", { name: /^Account:/ }).click();
    await expect(page.getByRole("menuitem", { name: "Reset demo" })).toHaveCount(0);
    await page.keyboard.press("Escape");
  });

  // (d) The whole flow on a built graph: the features and the estimate, a run through its steps, the plan, a task, Recreate
  test("FE-PLAN-02..07, FE-PLAN-13 (d): with a built graph Stage 0 lists the features; 'Create PLAN' runs to Stage 2; tasks show by area and story with the run quality; Edit is disabled; 'Recreate Plan' replans", async ({
    page,
    requests,
  }) => {
    const pid = await readyProject(page);
    await buildGraph(page, pid);
    await openPlan(page, requests);
    await expect(page.getByTestId("plan-summary")).toHaveText("2 features to plan. Estimated 12 model calls, about 48,000 input tokens.");
    await expect(screen(page).getByRole("rowheader")).toHaveText(["Checkout", "Search"]);

    await create(page).click();
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "creating");
    await expect(page.getByTestId("plan-card")).toContainText("Reading the knowledge graph");
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "ready", { timeout: 15_000 });

    const quality = page.getByTestId("plan-quality");
    await expect(quality).toContainText("1 task was written from the specification by rules");
    await expect(quality).toContainText("1 blocking issue and 0 advisory issues");
    await expect(page.getByTestId("plan-area-name")).toHaveText(["Checkout", "Project-wide work"]);
    const checkout = page.getByTestId("plan-area").first();
    await expect(checkout.getByTestId("plan-area-count")).toHaveText("1 of 4 done");
    await expect(checkout.getByTestId("plan-area-review")).toContainText("No task covers refunds.");
    await expect(checkout.getByTestId("plan-not-buildable")).toContainText("Not code work (1)");
    await expect(checkout.getByTestId("plan-story-name")).toHaveText(["Checkout", "Track the order"]);
    const pay = page.getByTestId("plan-task").filter({ hasText: "Take the payment" }); // the first story of the first area is open
    await expect(pay).toContainText("Pending");
    await expect(pay.getByTestId("plan-task-phase")).toHaveText("Logic");
    await expect(pay.getByTestId("plan-task-rules")).toHaveCount(0);
    const project = page.getByTestId("plan-area").nth(1);
    await project.getByTestId("plan-area-head").click();
    await project.getByTestId("plan-story-head").click();
    await expect(project.getByTestId("plan-task").getByTestId("plan-task-rules")).toHaveText("Rule-made");
    await pay.getByTestId("plan-task-head").click();
    const body = page.getByTestId("plan-task-body");
    await expect(body.getByRole("heading", { name: "Implements" })).toBeVisible();
    await expect(body).toContainText("Define the order API"); // Depends on: the prerequisite by title
    await body.getByText("Take the payment.").selectText();
    await page.keyboard.press("Shift"); // the toolbar follows the selection on keyup (use-text-selection)
    await expect(page.getByRole("toolbar", { name: "Selection actions" }).getByRole("button", { name: "Edit" })).toBeDisabled();

    const before = requests.length;
    await page.getByRole("button", { name: "Recreate Plan", exact: true }).click();
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "creating");
    const post = requests.slice(before).find((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/plan`);
    expect(post?.postData).toBe(JSON.stringify({ replan: true }));
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "ready", { timeout: 15_000 });
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

  // MCP-M10 U5: the switch is on the idle screen; Develop mounts the per-project tab; "Create PLAN" is still there and the stage stays idle
  test("MCP-M10 U5: Stage 0 shows the Plan | Develop switch; Develop shows the project tab, Plan the features; the URL carries the view", async ({
    page,
    requests,
  }) => {
    const pid = await readyProject(page);
    await openPlan(page, requests);
    const switchGroup = page.getByRole("group", { name: "Plan view" });
    await expect(switchGroup.getByRole("button", { name: "Plan", exact: true })).toHaveAttribute("aria-pressed", "true");
    await switchGroup.getByRole("button", { name: "Develop", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/plan\\?view=develop$`));
    await expect(page.getByTestId("plan-develop")).toBeVisible();
    await expect(screen(page)).toHaveAttribute(DATA_STAGE, "idle");
    await page.reload();
    await expect(page.getByTestId("plan-develop")).toBeVisible();
    await switchGroup.getByRole("button", { name: "Plan", exact: true }).click();
    await expect(screen(page).getByRole("heading", { name: "No features to plan yet" })).toBeVisible();
    await expect(page.getByTestId("plan-develop")).toHaveCount(0);
  });
});
