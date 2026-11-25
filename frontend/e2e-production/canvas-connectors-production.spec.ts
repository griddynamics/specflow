// Mission Control V2, the canvas connectors and decisions of session 06b on the PRODUCTION routes in a real browser, against the stub API
// through the real BFF (docs/mission-control/README.md "The flow" scenes 4-8; DECISIONS D2, D5, D9, D10, D19): the Build and Plan decisions,
// the Gaps list, the node dialog, and the GitHub repository, Slack channel and Jira plan connections. Every case seeds its own project.
import type { Page } from "@playwright/test";
import { ADD, bodiesOf, canvas, dialog, done, hub, MOTION, nodes, parsed, SPECS, STATE, unique, wire, YELLOW } from "./canvas-support";
import { FINISHED_RUN, shopGaps } from "./gaps-fixtures";
import {
  allowConsoleErrors,
  buildGraph,
  buildGraphAndPlan,
  expect,
  loginV2,
  seedGaps,
  seedGitHub,
  seedIntegrations,
  seedJira,
  seedMembers,
  seedSlack,
  seedSource,
  stubTiming,
  STUB_TIMING,
  test,
} from "./support";

const ALEX = { user_id: "u-alex", name: "Alex Kim" };
const CHANNEL = { id: "C0NWO", name: "northwind-specs", private: false };
const running = (progress: { step: string; done: number; total: number } | null) => ({
  status: "running",
  finished_at: null,
  error_code: null,
  progress,
});
const jiraTarget = (sync?: Record<string, unknown>) => ({
  jira_project: "NWO",
  epic_key: "NWO-1",
  created_epic_key: null,
  enabled: true,
  acting: "me" as const,
  sync,
});
const CHECK_ACCESS = "Check access";
const ADD_SELECTED = "Add selected";
const button = (from: Pick<Page, "getByRole">, name: string | RegExp) => from.getByRole("button", { name });

test.beforeEach(async () => {
  await seedJira({ pid: "none", mode: "oauth", connection: { status: "active", account_name: "Alex Kim" } });
});

test.afterEach(async () => {
  await stubTiming(STUB_TIMING);
  await seedGitHub({ reset: true });
  await seedIntegrations({ reset: true });
  await seedSlack({ pid: "none", channel: null, available: true, test: "ok" });
  await seedJira({ pid: "none", mode: "token", connection: null });
});

/** A project with sources the build can read: a Drive folder and one parsed document. */
async function readyProject(page: Page, name: string): Promise<{ pid: string; drive: string }> {
  const pid = await canvas(page, unique(name));
  const drive = await seedSource(pid, { kind: "gdrive", name: SPECS, files: done(24) });
  await parsed(page, pid);
  return { pid, drive };
}

/** The graph is built and the gap run found the gaps of the shop fixture. */
async function gapsFound(page: Page, name: string): Promise<string> {
  const { pid } = await readyProject(page, name);
  await buildGraph(page, pid);
  await seedGaps(pid, { gaps: shopGaps(), run: FINISHED_RUN });
  return pid;
}

test.describe("Mission Control V2: the canvas decisions and connections", { tag: "@production" }, () => {
  test("scene 4, building: Build asks for a reasoning level, Cancel sends nothing, High posts level 3; the hub is busy with a turning ring, the wires move, nothing is yellow", async ({
    page,
    requests,
  }) => {
    const { pid, drive } = await readyProject(page, "Scene four");
    await stubTiming({ graphMs: null }); // the build stays running
    await page.goto(`/projects/${pid}`);
    await button(page, "Graph. Build graph").click();
    const build = dialog(page, "Build the graph");
    await expect(build.getByText("from 2 sources and builds one graph")).toBeVisible();
    await expect(build.getByRole("group", { name: "Reasoning level" }).getByRole("button")).toHaveCount(5);
    await expect(button(build, "Medium")).toHaveAttribute("aria-pressed", "true");
    await build.getByRole("button", { name: "Cancel" }).click();
    await expect(build).toBeHidden();
    expect(requests.filter((r) => r.method === "POST" && r.path.endsWith("/graph_build"))).toHaveLength(0);
    await button(page, "Graph. Build graph").click();
    await build.getByRole("button", { name: "High", exact: true }).click();
    await expect(build.getByRole("button", { name: "High", exact: true })).toHaveAttribute("aria-pressed", "true");
    await build.getByRole("button", { name: "Build", exact: true }).click();
    await expect(build).toBeHidden();
    await expect(hub(page, "graph")).toHaveAttribute(STATE, "busy");
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "false");
    await expect(hub(page, "graph").getByRole("group")).toHaveCount(1); // no decision under it while it builds, no button
    await expect(wire(page, drive)).toHaveAttribute(MOTION, "flow");
    const posts = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/graph_build`);
    expect(bodiesOf(posts)).toEqual([{ reasoning_level: 3 }]);
  });

  test("scene 5, gaps found: Plan is the yellow decision, the Gaps hub lists the open gaps read-only, Build plan posts a plan and the wire from Gap moves", async ({
    page,
    requests,
  }) => {
    const pid = await gapsFound(page, "Scene five");
    await seedSlack({ pid, channel: CHANNEL });
    await page.goto(`/projects/${pid}`);
    await expect(hub(page, "plan")).toHaveAttribute(YELLOW, "true");
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "false");
    const channel = nodes(page).filter({ hasText: "#northwind-specs" });
    await expect(channel).toHaveAttribute(STATE, "on");
    await expect(channel).toContainText("found");
    await expect(hub(page, "gap")).toContainText(/\d critical/);

    await button(page, /^Gap$/).click();
    const gaps = dialog(page, /open gaps?$/);
    await expect(gaps.getByText("in Slack, in Jira, or by a coding agent over MCP")).toBeVisible();
    await expect(gaps.getByRole("list", { name: "Gaps" }).getByRole("listitem").first()).toBeVisible();
    await expect(gaps.getByRole("textbox")).toHaveCount(0); // gaps are not answered in the app (D10)
    await gaps.getByRole("button", { name: "Done" }).click();
    await expect(gaps).toBeHidden();

    await stubTiming({ planMs: 600_000 });
    await button(page, "Plan. Build plan").click();
    const plan = dialog(page, "Build the plan");
    await expect(plan.getByText("gaps are still open", { exact: false })).toBeVisible();
    await plan.getByRole("button", { name: "Build plan" }).click();
    await expect(plan).toBeHidden();
    await expect(hub(page, "plan")).toHaveAttribute(STATE, "busy");
    await expect(hub(page, "plan")).toHaveAttribute(YELLOW, "false");
    await expect(wire(page, "gap-plan")).toHaveAttribute(MOTION, "flow");
    const posts = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/plan`);
    expect(bodiesOf(posts)).toEqual([{ replan: false }]);
  });

  test("scene 6, the plan is in Jira: the Jira node syncs with a moving wire, then rests with a solid one", async ({ page }) => {
    const pid = await gapsFound(page, "Scene six");
    await buildGraphAndPlan(page, pid);
    await seedIntegrations({ pid, atlassian: { by: ALEX, health: "active" } });
    await seedJira({ pid, target: jiraTarget(running({ step: "issues", done: 3, total: 12 })) });
    await page.goto(`/projects/${pid}`);
    const jira = nodes(page).filter({ hasText: "Jira · NWO-1" });
    await expect(hub(page, "plan")).toHaveAttribute(STATE, "on");
    await expect(jira).toHaveAttribute(STATE, "busy");
    await expect(jira).toContainText("Syncing…");
    await expect(wire(page, "jira-plan")).toHaveAttribute(MOTION, "flow");
    await seedJira({
      pid,
      target: jiraTarget({ status: "succeeded", finished_at: "2026-11-25T09:00:00Z", error_code: null, progress: null }),
    });
    await expect(jira).toHaveAttribute(STATE, "on", { timeout: 15_000 });
    await expect(jira).toContainText("Synced");
    await expect(wire(page, "jira-plan")).toHaveAttribute(MOTION, "on");
    await expect(hub(page, "plan")).toHaveAttribute(YELLOW, "false");
  });

  test("scene 7, new information: a source added after the plan makes Update graph the decision; once it is built the plan is yellow again as Replan, and Replan posts replan true", async ({
    page,
    requests,
  }) => {
    const pid = await gapsFound(page, "Scene seven");
    await buildGraphAndPlan(page, pid);
    await page.goto(`/projects/${pid}`);
    await expect(hub(page, "plan")).toHaveAttribute(STATE, "on");
    await expect(button(page, /Replan/)).toHaveCount(0);
    await seedSource(pid, { kind: "upload", files: done(2) });
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "true", { timeout: 15_000 });
    await button(page, "Graph. Update graph").click();
    const update = dialog(page, "Update the graph");
    await expect(update.getByText(/Open gaps stay open/)).toBeVisible();
    await update.getByRole("button", { name: "Update", exact: true }).click();
    await expect(hub(page, "plan")).toHaveAttribute(YELLOW, "true", { timeout: 20_000 });
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "false");
    await button(page, "Plan. Replan").click();
    const replan = dialog(page, "Replan");
    await expect(replan.getByText(/plans every area again/)).toBeVisible();
    await replan.getByRole("button", { name: "Replan", exact: true }).click();
    await expect(replan).toBeHidden();
    const posts = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/plan`);
    expect(posts.at(-1)?.postData).toBe(JSON.stringify({ replan: true }));
  });

  test("scene 8, a connection stopped: GitHub needs a reconnect, so both repositories stop with a badge and the dialog offers Reconnect GitHub", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("Scene eight"));
    await seedGitHub({ pid, repositories: [{ full_name: "northwind/ordering" }, { full_name: "northwind/deploy" }] });
    await seedIntegrations({ pid, github: { by: ALEX, health: "reconnect_required" } });
    await page.goto(`/projects/${pid}`);
    const ordering = nodes(page).filter({ hasText: "ordering" });
    for (const name of ["ordering", "deploy"]) {
      const node = nodes(page).filter({ hasText: name });
      await expect(node).toHaveAttribute(STATE, "stopped");
      await expect(node).toContainText("Stopped · reconnect");
    }
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "false");
    await ordering.getByRole("button").click();
    const detail = dialog(page, "ordering");
    await expect(detail.getByText(/no access now/)).toBeVisible();
    await expect(detail.getByText("Connected by Alex Kim")).toBeVisible();
    await expect(detail.getByRole("link", { name: "Reconnect GitHub" })).toHaveAttribute(
      "href",
      new RegExp(`/projects/${pid}/integrations\\?setup=github`),
    );
  });

  test("GitHub: a repository the account can read is picked in the form, becomes a node that clones, and the over-sized one cannot be picked; no GitHub integration says Set up first", async ({
    page,
    requests,
  }) => {
    const pid = await canvas(page, unique("GitHub form"));
    await page.getByRole("button", { name: ADD.source }).click();
    const plus = dialog(page, "Add a source");
    await expect(plus.getByRole("button", { name: /^GitHub repository/ })).toContainText("Set up first");
    await page.keyboard.press("Escape");
    await seedGitHub({ connection: { status: "active", login: "alex-kim" } });
    await seedIntegrations({ pid, github: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}`);
    await page.getByRole("button", { name: ADD.source }).click();
    await plus.getByRole("button", { name: /^GitHub repository/ }).click();
    const form = dialog(page, "Add repositories");
    await expect(form.getByLabel("acme/payments-api")).toBeEnabled();
    await expect(form.getByLabel("acme/monorepo")).toBeDisabled();
    await expect(form.getByText("over 1 GB")).toBeVisible();
    await expect(form.getByRole("button", { name: ADD_SELECTED })).toBeDisabled();
    await form.getByLabel("acme/payments-api").check();
    await form.getByRole("button", { name: ADD_SELECTED }).click();
    await expect(form).toBeHidden();
    const node = nodes(page).filter({ hasText: "payments-api" });
    await expect(node).toHaveAttribute(STATE, "busy");
    await expect(node).toHaveAttribute(STATE, "on", { timeout: 15_000 });
    await expect(node).toContainText("Cloned · GitHub");
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "false"); // the build does not read a clone (D19)
    const posts = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/repositories`);
    expect(bodiesOf(posts)).toEqual([{ github_repo_id: 101 }]);
    await node.getByRole("button").click();
    const detail = dialog(page, "payments-api");
    await expect(detail.getByText(/The build does not read repositories yet\./)).toBeVisible();
  });

  test("GitHub: when the second add fails the first stays added and leaves the form; the second pick stays and the error is said", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("GitHub partial"));
    await seedGitHub({ connection: { status: "active", login: "alex-kim" } });
    await seedIntegrations({ pid, github: { by: ALEX, health: "active" } });
    allowConsoleErrors(page, /Failed to load resource/);
    let adds = 0;
    await page.route(`**/api/backend/projects/${pid}/repositories`, (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      adds += 1;
      if (adds === 1) return route.fallback();
      return route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "github_unreachable", message: "github_unreachable", details: {} } }),
      });
    });
    await page.goto(`/projects/${pid}`);
    await page.getByRole("button", { name: ADD.source }).click();
    await dialog(page, "Add a source")
      .getByRole("button", { name: /^GitHub repository/ })
      .click();
    const form = dialog(page, "Add repositories");
    await form.getByLabel("acme/payments-api").check();
    await form.getByLabel("acme/web-app").check();
    await form.getByRole("button", { name: ADD_SELECTED }).click();
    await expect(form.getByRole("alert")).toBeVisible();
    await expect(form.getByLabel("acme/payments-api")).toHaveCount(0); // it got in: no longer offered
    await expect(form.getByLabel("acme/web-app")).toBeChecked(); // still picked, to try again
    await expect(form.getByRole("button", { name: ADD_SELECTED })).toBeEnabled();
    await form.getByRole("button", { name: "Close" }).click();
    await expect(nodes(page).filter({ hasText: "payments-api" })).toHaveCount(1); // the canvas read again
    await expect(nodes(page).filter({ hasText: "web-app" })).toHaveCount(0);
  });

  test("a member the API refuses sees one read-only line in the GitHub and Jira forms, no picker, no submit, and no Remove on a repository", async ({
    page,
  }) => {
    await loginV2(page);
    const { id: pid } = await seedMembers({ name: unique("Member"), role: "collaborator" });
    await seedGitHub({ connection: { status: "active", login: "sam-ortiz" } });
    await seedGitHub({ pid, repositories: [{ full_name: "northwind/ordering" }] });
    await seedIntegrations({ pid, github: { by: ALEX, health: "active" }, atlassian: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}`);
    await page.getByRole("button", { name: ADD.source }).click();
    await dialog(page, "Add a source")
      .getByRole("button", { name: /^GitHub repository/ })
      .click();
    const github = dialog(page, "Add repositories");
    await expect(github.getByText("Only the project's owner can add repositories.")).toBeVisible();
    await expect(github.getByRole("checkbox")).toHaveCount(0);
    await expect(github.getByRole("button", { name: ADD_SELECTED })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: ADD.destination }).click();
    await dialog(page, "Add a plan destination").getByRole("button", { name: /^Jira/ }).click();
    const jira = dialog(page, "Send the plan to Jira");
    await expect(jira.getByText("Only the project's owner can change where the plan goes.")).toBeVisible();
    await expect(jira.getByLabel("Jira space key")).toHaveCount(0);
    await expect(jira.getByRole("button", { name: CHECK_ACCESS })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await nodes(page).filter({ hasText: "ordering" }).getByRole("button").click();
    const detail = dialog(page, "ordering");
    await expect(detail.getByRole("button", { name: "Done" })).toBeVisible();
    await expect(detail.getByRole("button", { name: "Remove" })).toHaveCount(0);
  });

  test("Slack: the form shows the invite and the connect command, waits, and names the channel once it is bound in Slack; the channel is a node under Gap", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("Slack form"));
    await page.getByRole("button", { name: ADD.channel }).click();
    await dialog(page, "Add a gap channel")
      .getByRole("button", { name: /^Slack channel/ })
      .click();
    const form = dialog(page, "Connect a Slack channel");
    await expect(form.getByText("/invite @SpecFlow", { exact: true })).toBeVisible();
    await expect(form.getByText(`/specflow connect ${pid}`, { exact: true })).toBeVisible();
    await expect(form.getByText("Waiting for the channel.")).toBeVisible();
    await expect(form.getByRole("button", { name: /Test connection/ })).toHaveCount(0);
    await seedSlack({ pid, channel: CHANNEL });
    await expect(form.getByText("#northwind-specs is connected.")).toBeVisible({ timeout: 15_000 });
    await form.getByRole("button", { name: "Done" }).click();
    const node = nodes(page).filter({ hasText: "#northwind-specs" });
    await expect(node).toHaveAttribute(STATE, "wait");
    await expect(node).toContainText("Waits for gaps");
    await expect(wire(page, "slack-C0NWO")).toHaveAttribute(MOTION, "off");
  });

  test("Jira plan: the space and epic keys, Check access, then Add destination saves the target and shows its node; a failed check names the field", async ({
    page,
    requests,
  }) => {
    const pid = await canvas(page, unique("Jira form"));
    await seedIntegrations({ pid, atlassian: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}`);
    await page.getByRole("button", { name: ADD.destination }).click();
    await dialog(page, "Add a plan destination").getByRole("button", { name: /^Jira/ }).click();
    const form = dialog(page, "Send the plan to Jira");
    await expect(form.getByRole("button", { name: CHECK_ACCESS })).toBeDisabled();
    await form.getByLabel("Jira space key").fill("nwo");
    await seedJira({ pid, access: { reason: "jira_not_found", field: "jira_project" } });
    await form.getByRole("button", { name: CHECK_ACCESS }).click();
    await expect(form.getByText(/space/i).first()).toBeVisible();
    await expect(form.getByRole("button", { name: CHECK_ACCESS })).toBeVisible(); // still a check: nothing to add yet
    await seedJira({ pid, access: "ok" });
    await form.getByLabel("Jira space key").fill("nwo");
    await form.getByLabel("Epic key").fill("nwo-1");
    await form.getByRole("button", { name: CHECK_ACCESS }).click();
    await expect(form.getByText(/Jira space/)).toBeVisible();
    await form.getByRole("button", { name: "Add destination" }).click();
    await expect(form).toBeHidden();
    const node = nodes(page).filter({ hasText: "Jira · NWO-1" });
    await expect(node).toHaveAttribute(STATE, "wait");
    await expect(node).toContainText("Waits for the plan");
    const puts = requests.filter((r) => r.method === "PUT" && r.path === `/api/backend/projects/${pid}/jira`);
    expect(bodiesOf(puts)).toEqual([{ jira_project: "NWO", epic_key: "NWO-1", enabled: true }]);
  });

  test("a source's dialog: who added it and its state; Remove asks first, Keep leaves it, Remove deletes it and the node goes", async ({
    page,
    requests,
  }) => {
    const { pid, drive } = await readyProject(page, "Remove");
    await page.goto(`/projects/${pid}`);
    const node = nodes(page).filter({ hasText: SPECS });
    await node.getByRole("button").click();
    const detail = dialog(page, SPECS);
    await expect(detail.getByText(/24 files\. Imported when it was added/)).toBeVisible();
    await expect(detail.getByText(/^Added by /)).toBeVisible();
    await detail.getByRole("button", { name: "Remove" }).click();
    const sure = detail.getByRole("alertdialog", { name: "Remove this source?" });
    await sure.getByRole("button", { name: "Keep" }).click();
    await expect(sure).toBeHidden();
    await expect(node).toHaveCount(1);
    await detail.getByRole("button", { name: "Remove" }).click();
    await sure.getByRole("button", { name: "Remove" }).click();
    await expect(detail).toBeHidden();
    await expect(node).toHaveCount(0);
    expect(requests.filter((r) => r.method === "DELETE" && r.path === `/api/backend/projects/${pid}/sources/${drive}`)).toHaveLength(1);
  });
});
