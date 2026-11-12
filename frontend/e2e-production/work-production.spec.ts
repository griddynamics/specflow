// MCP-TOOLING session 10: Plan › Work on the PRODUCTION routes against the stub API through the real BFF: the findings list (empty, blocking
// first, the node filter, links), resolve and dismiss with a note, the failure and the "already answered" cases, and the claims board with the
// forced release and its confirmation. No paid model call: the stub seeds findings and claims through its control port.
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import {
  allowConsoleErrors,
  bff,
  createViaNew,
  expect,
  expectUploadReady,
  login,
  pickLocalPdf,
  rail,
  seedWork,
  STUB_TIMING,
  stubTiming,
  test,
} from "./support";

const person = (name: string, you = false, agentLabel: string | null = null) => ({ name, you, agent_label: agentLabel });
const finding = (id: string, over: Record<string, unknown> = {}) => ({
  finding_id: id,
  kind: "question",
  text: "Which currencies does the API accept?",
  status: "open",
  blocking: true,
  about: ["FEAT::API"],
  refers_to: [
    { type: "url", value: "https://example.com/spec" },
    { type: "commit", value: "e44f0c1" },
  ],
  story_id: "STORY::A::STUCK",
  story_title: "Checkout",
  author: person("Alice", true),
  created_at: "2026-01-01T10:00:00Z",
  resolved_by: null,
  resolved_at: null,
  resolution_note: null,
  orphaned: false,
  ...over,
});
const note = finding("FND::2", {
  kind: "note",
  text: "The API is slow under load.",
  blocking: false,
  story_id: null,
  about: ["FEAT::UI"],
  refers_to: [],
  author: person("Ben"),
  created_at: "2026-01-02T10:00:00Z",
});
const storyClaim = {
  kind: "story",
  story_id: "STORY::A::API",
  title: "Order API",
  task_id: null,
  holder: person("Ben", false, "claude-code"),
  claimed_at: "2026-01-01T09:00:00Z",
  branch: "fix/order-api",
  tasks: 2,
};
const taskClaim = {
  kind: "task",
  story_id: "STORY::A::UI",
  title: "Build the order screen",
  task_id: "TASK::A::UI_SCREEN",
  holder: person("Alice", true),
  claimed_at: "2026-01-02T09:00:00Z",
  branch: null,
  tasks: 1,
};

test.describe("Plan > Work on production routes", { tag: "@production" }, () => {
  test.setTimeout(120_000);

  test.afterEach(async () => {
    await stubTiming(STUB_TIMING);
  });

  const unique = () => `Work ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
  const CONFIRM = "finding-confirm";
  const findings = (page: Page) => page.getByTestId("work-findings");
  const filter = (page: Page, name: "Open" | "Resolved" | "Dismissed") =>
    page.getByRole("group", { name: "Finding status" }).getByRole("button", { name, exact: true });
  const claims = (page: Page) => page.getByTestId("work-claims");

  /** A project with a processed document, a built graph and a plan (stub API, no paid call); the page sits on Plan › Work. */
  async function workScreen(
    page: Page,
    seed: { findings?: Record<string, unknown>[]; claims?: Record<string, unknown>[] },
  ): Promise<string> {
    await login(page);
    const pid = await createViaNew(page, unique());
    await pickLocalPdf(page, "requirements.pdf");
    await expectUploadReady(page);
    expect((await bff(page, "POST", `/projects/${pid}/graph_build`, { reasoning_level: 3 })).status).toBe(202);
    await expect
      .poll(
        async () =>
          ((await bff(page, "GET", `/projects/${pid}/graph_build`)).json as { status: "idle" | "running" | "succeeded" | "failed" }).status,
        { timeout: 10_000 },
      )
      .toBe("succeeded");
    expect((await bff(page, "POST", `/projects/${pid}/plan`, { replan: false })).status).toBe(202);
    await expect
      .poll(async () => ((await bff(page, "GET", `/projects/${pid}/plan`)).json as { has_plan: boolean }).has_plan, { timeout: 15_000 })
      .toBe(true);
    await seedWork(pid, seed);
    await rail(page).getByRole("button", { name: "Plan", exact: true }).click();
    await expect(page.getByTestId("screen-plan")).toHaveAttribute("data-stage", "ready", { timeout: 15_000 });
    await page.getByRole("group", { name: "Plan view" }).getByRole("button", { name: "Work", exact: true }).click();
    await expect(page.getByTestId("plan-work")).toBeVisible();
    return pid;
  }

  test("an empty project shows both empty states", async ({ page }) => {
    await workScreen(page, {});
    await expect(findings(page).getByRole("heading", { name: "No open findings" })).toBeVisible();
    await expect(claims(page).getByRole("heading", { name: "Nobody is working on a story" })).toBeVisible();
    await expect(page.getByTestId("finding")).toHaveCount(0);
    await expect(page.getByTestId("claim")).toHaveCount(0);
  });

  test("the list puts the blocking finding first, shows kind, links and spec nodes, and filters by node and status", async ({ page }) => {
    await workScreen(page, { findings: [note, finding("FND::1")] });
    const rows = page.getByTestId("finding");
    await expect(rows).toHaveCount(2);
    await expect(rows.first()).toHaveAttribute("data-finding", "FND::1");
    await expect(rows.first()).toContainText("Blocking");
    await expect(rows.first()).toContainText("Question");
    await expect(rows.first()).toContainText("Blocking · Checkout");
    await expect(rows.nth(1)).not.toContainText("Blocking");
    const link = rows.first().getByTestId("finding-ref");
    await expect(link).toHaveAttribute("href", "https://example.com/spec");
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(rows.first()).toContainText("e44f0c1");
    await rows.first().getByTestId("finding-node").click();
    await expect(page.getByTestId("work-node-filter")).toContainText("FEAT::API");
    await expect(rows).toHaveCount(1);
    await page.getByRole("button", { name: "Show all" }).click();
    await expect(rows).toHaveCount(2);
    await filter(page, "Resolved").click();
    await expect(findings(page).getByRole("heading", { name: "No resolved findings" })).toBeVisible();
  });

  test("resolving a blocking finding with a note closes it and it shows under Resolved with who answered and the note", async ({
    page,
  }) => {
    await workScreen(page, { findings: [finding("FND::1"), note] });
    await page.getByTestId("finding").first().getByTestId("finding-resolve").click();
    const dialog = page.getByRole("dialog", { name: "Resolve finding" });
    await expect(dialog).toContainText("Which currencies does the API accept?");
    await expect(dialog).toContainText("Checkout");
    await dialog.getByLabel("Note (optional)").fill("Euro and dollar only.");
    await dialog.getByTestId(CONFIRM).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByTestId("finding")).toHaveCount(1);
    await expect(page.getByTestId("finding")).toHaveAttribute("data-finding", "FND::2");
    await filter(page, "Resolved").click();
    const closed = page.getByTestId("finding").first();
    await expect(closed).toHaveAttribute("data-status", "resolved");
    await expect(closed.getByTestId("finding-closed")).toContainText("Resolved by Alice (you)");
    await expect(closed.getByTestId("finding-closed")).toContainText("Euro and dollar only.");
    await expect(closed.getByTestId("finding-resolve")).toHaveCount(0);
  });

  test("dismissing a plain finding without a note works, and Esc closes the dialog without writing", async ({ page, requests }) => {
    await workScreen(page, { findings: [note] });
    const row = page.getByTestId("finding");
    await row.getByTestId("finding-dismiss").click();
    await expect(page.getByRole("dialog", { name: "Dismiss finding" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(requests.filter((r) => r.method === "POST" && r.path.includes("/findings/"))).toHaveLength(0);
    await row.getByTestId("finding-dismiss").click();
    await page.getByRole("dialog").getByTestId(CONFIRM).click();
    await expect(findings(page).getByRole("heading", { name: "No open findings" })).toBeVisible();
    const post = requests.find((r) => r.method === "POST" && r.path.endsWith("/findings/FND%3A%3A2/dismiss"));
    expect(post?.postData).toBe(JSON.stringify({ note: null }));
  });

  test("a failed save keeps the dialog and the note, says so, and a retry works", async ({ page }) => {
    allowConsoleErrors(page, /status of 503/); // the browser logs the expected refusal
    await workScreen(page, { findings: [finding("FND::1")] });
    await page.getByTestId("finding-resolve").click();
    const dialog = page.getByRole("dialog", { name: "Resolve finding" });
    await dialog.getByLabel("Note (optional)").fill("Euro only.");
    await stubTiming({ workFail: "graph_unavailable" });
    await dialog.getByTestId(CONFIRM).click();
    await expect(dialog.getByRole("alert")).toContainText("could not be saved");
    await expect(dialog.getByLabel("Note (optional)")).toHaveValue("Euro only.");
    await expect(page.getByTestId("finding")).toHaveCount(1);
    await dialog.getByTestId(CONFIRM).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(findings(page).getByRole("heading", { name: "No open findings" })).toBeVisible();
  });

  test("a finding someone else already answered closes the dialog and the list is up to date", async ({ page }) => {
    allowConsoleErrors(page, /status of 409/);
    const pid = await workScreen(page, { findings: [finding("FND::1"), note] });
    await page.getByTestId("finding").first().getByTestId("finding-dismiss").click();
    expect((await bff(page, "POST", `/projects/${pid}/findings/FND%3A%3A1/resolve`, {})).status).toBe(200);
    await page.getByRole("dialog").getByTestId(CONFIRM).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByTestId("finding")).toHaveCount(1);
    await expect(page.getByTestId("finding")).toHaveAttribute("data-finding", "FND::2");
  });

  test("a list that cannot be read shows the failure with 'Try again'", async ({ page }) => {
    allowConsoleErrors(page, /status of 503/);
    await workScreen(page, { findings: [note], claims: [storyClaim] });
    await stubTiming({ workReadFail: "graph_unavailable" });
    await filter(page, "Dismissed").click();
    await expect(findings(page).getByRole("heading", { name: "Findings could not be loaded" })).toBeVisible();
    await stubTiming({ workReadFail: null });
    await findings(page).getByRole("button", { name: "Try again" }).click();
    await expect(findings(page).getByRole("heading", { name: "No dismissed findings" })).toBeVisible();
  });

  test("the claims board lists story and task claims oldest first; a story claim is freed after a confirmation, a task claim has no release", async ({
    page,
  }) => {
    await workScreen(page, { claims: [storyClaim, taskClaim] });
    const rows = page.getByTestId("claim");
    await expect(rows).toHaveCount(2);
    const story = rows.first();
    await expect(story.getByTestId("claim-holder")).toHaveText("Ben");
    await expect(story).toContainText("claude-code");
    await expect(story).toContainText("Order API");
    await expect(story.getByTestId("claim-branch")).toHaveText("fix/order-api");
    await expect(story.getByTestId("claim-since")).not.toBeEmpty();
    const task = rows.nth(1);
    await expect(task.getByTestId("claim-holder")).toHaveText("Alice (you)");
    await expect(task.getByTestId("claim-release")).toHaveCount(0);
    await expect(task.getByTestId("claim-task-note")).toContainText("released by its agent");
    await expect(story).toHaveAttribute("data-stale", "");

    await story.getByTestId("claim-release").click();
    const dialog = page.getByRole("dialog", { name: "Release this claim?" });
    await expect(dialog).toContainText("Ben holds this story.");
    await expect(dialog).toContainText("Releasing returns 2 tasks to pending.");
    await expect(dialog).toContainText("The agent must stop working on this story");
    await dialog.getByRole("button", { name: "Keep claim" }).click();
    await expect(rows).toHaveCount(2);

    await story.getByTestId("claim-release").click();
    await page.getByRole("dialog").getByTestId("release-confirm").click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toHaveAttribute("data-kind", "task");
  });

  test("a release that fails keeps the claim and says so", async ({ page }) => {
    allowConsoleErrors(page, /status of 503/);
    await workScreen(page, { claims: [storyClaim] });
    await page.getByTestId("claim-release").click();
    await stubTiming({ workFail: "graph_unavailable" });
    await page.getByTestId("release-confirm").click();
    await expect(page.getByTestId("release-dialog").getByRole("alert")).toContainText("could not be released");
    await page.getByRole("button", { name: "Keep claim" }).click();
    await expect(page.getByTestId("claim")).toHaveCount(1);
  });
});
