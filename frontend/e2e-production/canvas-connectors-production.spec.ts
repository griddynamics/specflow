// Mission Control V2, the canvas connectors and decisions of session 06b on the PRODUCTION routes in a real browser, against the stub API
// through the real BFF (docs/mission-control/README.md "The flow" scenes 4-8; DECISIONS D2, D5, D9, D10, D19): the Build and Plan decisions,
// the Gaps list, the node dialog, and the GitHub repository, Slack channel and Jira plan connections. Every case seeds its own project.
import type { Page } from "@playwright/test";
import {
  ADD,
  bodiesOf,
  canvas,
  dialog,
  done,
  FOLDER,
  hub,
  MOTION,
  nodes,
  parsed,
  SPECS,
  STATE,
  unique,
  wire,
  YELLOW,
} from "./canvas-support";
import { FINISHED_RUN, shopGaps } from "./gaps-fixtures";
import {
  allowConsoleErrors,
  buildGraph,
  buildGraphAndPlan,
  expect,
  loginV2,
  type RequestLog,
  seedGaps,
  seedGitHub,
  seedGitHubProjects,
  seedIntegrations,
  seedJira,
  seedJiraGaps,
  seedMembers,
  seedSlack,
  seedSource,
  stubTiming,
  STUB_TIMING,
  test,
} from "./support";

const SITE = "https://northwind.atlassian.test";
const PAGE = `${SITE}/wiki/spaces/ORD/pages/12345/Checkout`;
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
const SPACE_KEY = "Jira space key";
const ADD_SOURCE = "Add a source";
const SET_UP_FIRST = "Set up first";
const CHECK_LINK = "Check link";
const CHECK_EPIC = "Check epic";
const ADD_SELECTED = "Add selected";
const FILES_TO_ADD = "Files to add";
const SELECT_NONE = "Select none";
const SELECT_ALL = "Select all";
const TEST_CONNECTION = "Test connection";
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
  await seedJiraGaps({ pid: "none", available: true });
  await seedGitHubProjects({ available: true, organization: "acme" });
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

  test("scene 5, gaps found: Plan is the yellow decision, the Gaps hub counts the open gaps by category, read-only, Build plan posts a plan and the wire from Gap moves", async ({
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
    await expect(gaps.getByRole("table", { name: "Gaps" }).getByRole("rowheader").first()).toBeVisible(); // counts by category, not a list
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
    const plus = dialog(page, ADD_SOURCE);
    await expect(plus.getByRole("button", { name: /^GitHub repository/ })).toContainText(SET_UP_FIRST);
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
    await dialog(page, ADD_SOURCE)
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
    await dialog(page, ADD_SOURCE)
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
    await expect(jira.getByLabel(SPACE_KEY)).toHaveCount(0);
    await expect(jira.getByRole("button", { name: CHECK_ACCESS })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await nodes(page).filter({ hasText: "ordering" }).getByRole("button").click();
    const detail = dialog(page, "ordering");
    await expect(detail.getByRole("button", { name: "Done" })).toBeVisible();
    await expect(detail.getByRole("button", { name: "Remove" })).toHaveCount(0);
  });

  test("Confluence: no Atlassian on the project says Set up first and comes back with the form open; a link is checked (the closed codes in words), then added as a source that imports its pages", async ({
    page,
    requests,
  }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 4\d\d/); // the refused checks are answers
    const pid = await canvas(page, unique("Confluence form"));
    await page.getByRole("button", { name: ADD.source }).click();
    const plus = dialog(page, ADD_SOURCE);
    await expect(plus.getByRole("button", { name: /^Confluence link/ })).toContainText(SET_UP_FIRST);
    await plus.getByRole("button", { name: /^Confluence link/ }).click();
    await expect(page).toHaveURL(
      new RegExp(`/projects/${pid}/integrations\\?setup=atlassian&next=%2Fprojects%2F${pid}%3Fadd%3Dconfluence$`),
    );
    await seedIntegrations({ pid, atlassian: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}?add=confluence`);
    const form = dialog(page, "Add a Confluence link");
    await expect(form.getByRole("button", { name: CHECK_LINK })).toBeDisabled();
    const link = form.getByLabel("Confluence page or space link");
    for (const [text, words] of [
      ["https://evil.example.com/wiki/spaces/ORD", /another Atlassian site/],
      [`${SITE}/wiki/x/AbCdEf`, /Paste the full address/],
      [`${SITE}/wiki/spaces/ORD/pages/404`, /no page or space at this link/],
      [`${SITE}/wiki/spaces/ORD/pages/403`, /not allowed to read/],
    ] as const) {
      await link.fill(text);
      await form.getByRole("button", { name: CHECK_LINK }).click();
      await expect(form.getByRole("alert")).toContainText(words);
      await expect(form.getByRole("button", { name: CHECK_LINK })).toBeVisible(); // still a check: nothing to add
    }
    await link.fill(PAGE);
    await form.getByRole("button", { name: CHECK_LINK }).click();
    await expect(form.getByText("Page Checkout notes · 4 pages, 3 linked Jira issues.")).toBeVisible();
    await expect(form.getByRole("button", { name: "Add source" })).toBeVisible();
    await form.getByLabel("Follow links to other pages and Jira issues").uncheck(); // a different choice is a new check
    await expect(form.getByRole("button", { name: CHECK_LINK })).toBeVisible();
    await form.getByRole("button", { name: CHECK_LINK }).click();
    await expect(form.getByText("Page Checkout notes · 4 pages.")).toBeVisible();
    await form.getByRole("button", { name: "Add source" }).click();
    await expect(form).toBeHidden();
    const node = nodes(page).filter({ hasText: "Checkout notes" });
    await expect(node).toHaveAttribute(STATE, "busy");
    await expect(node).toHaveAttribute(STATE, "on", { timeout: 15_000 });
    await expect(node).toContainText("4 pages · Confluence");
    const adds = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/sources/confluence`);
    expect(bodiesOf(adds)).toEqual([{ link: PAGE, follow: false }]);
    await node.getByRole("button").click();
    const detail = dialog(page, "Checkout notes");
    await expect(detail.getByText(/^4 pages\. Imported when it was added/)).toBeVisible();
    await expect(detail.getByRole("button", { name: "Remove" })).toBeVisible();
  });

  test("Jira epic: no Atlassian on the project says Set up first and comes back with the form open; a key or link is checked (the closed codes in words), then added as a source that imports its issues", async ({
    page,
    requests,
  }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 4\d\d/); // the refused checks are answers
    const pid = await canvas(page, unique("Jira epic form"));
    await page.getByRole("button", { name: ADD.source }).click();
    const plus = dialog(page, ADD_SOURCE);
    await expect(plus.getByRole("button", { name: /^Jira epic/ })).toContainText(SET_UP_FIRST);
    await plus.getByRole("button", { name: /^Jira epic/ }).click();
    await expect(page).toHaveURL(
      new RegExp(`/projects/${pid}/integrations\\?setup=atlassian&next=%2Fprojects%2F${pid}%3Fadd%3Djira_epic$`),
    );
    await seedIntegrations({ pid, atlassian: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}?add=jira_epic`);
    const form = dialog(page, "Add a Jira epic");
    await expect(form.getByRole("button", { name: CHECK_EPIC })).toBeDisabled();
    const key = form.getByLabel("Epic key or link");
    for (const [text, words] of [
      ["https://evil.example.com/browse/NWO-12", /another Atlassian site/],
      ["not an epic key", /not an epic key/],
      ["NWO-13", /not an epic/],
      ["NWO-404", /no issue with this key/],
      ["NWO-403", /not allowed to read this epic/],
    ] as const) {
      await key.fill(text);
      await form.getByRole("button", { name: CHECK_EPIC }).click();
      await expect(form.getByRole("alert")).toContainText(words);
      await expect(form.getByRole("button", { name: CHECK_EPIC })).toBeVisible(); // still a check: nothing to add
    }
    await key.fill("NWO-250");
    await form.getByRole("button", { name: CHECK_EPIC }).click();
    await expect(form.getByText("NWO-250 · Everything · 250 issues.")).toBeVisible();
    await expect(form.getByText(/the first 200 are read/)).toHaveCount(0); // the cap is said once, in the note of the list
    await expect(form.getByText(/Only the first 200 issues/)).toHaveCount(1);
    await key.fill(`${SITE}/browse/NWO-12`); // a different key is a new check
    await expect(form.getByRole("button", { name: CHECK_EPIC })).toBeVisible();
    await form.getByRole("button", { name: CHECK_EPIC }).click();
    await expect(form.getByText("NWO-12 · Checkout redesign · 18 issues.")).toBeVisible();
    await form.getByRole("button", { name: "Add source" }).click();
    await expect(form).toBeHidden();
    const node = nodes(page).filter({ hasText: "NWO-12" });
    await expect(node).toHaveAttribute(STATE, "busy");
    await expect(node).toHaveAttribute(STATE, "on", { timeout: 15_000 });
    await expect(node).toContainText("18 issues · Jira");
    const adds = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/sources/jira-epic`);
    expect(bodiesOf(adds)).toEqual([{ key_or_link: `${SITE}/browse/NWO-12` }]);
    await node.getByRole("button").click();
    const detail = dialog(page, "NWO-12");
    await expect(detail.getByText(/^18 issues\. Imported when it was added/)).toBeVisible();
    await expect(detail.getByRole("button", { name: "Remove" })).toBeVisible();
  });

  test("Jira epic issues: a check lists every issue checked, Select none and Select all move them together, and Add sends the unchecked keys as exclude", async ({
    page,
    requests,
  }) => {
    const pid = await canvas(page, unique("Jira epic issues"));
    await seedIntegrations({ pid, atlassian: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}?add=jira_epic`);
    const form = dialog(page, "Add a Jira epic");
    await form.getByLabel("Epic key or link").fill("NWO-12");
    await form.getByRole("button", { name: CHECK_EPIC }).click();
    const list = form.getByRole("group", { name: "Issues to add" });
    const boxes = list.getByRole("checkbox");
    await expect(boxes).toHaveCount(18);
    for (const box of await boxes.all()) await expect(box).toBeChecked();
    await expect(list.getByText("18 of 18 issues, and the epic")).toBeVisible();
    await expect(list.getByText("Story · To Do").first()).toBeVisible();
    await expect(list.getByText("NWO-101", { exact: true })).toHaveCSS("font-family", /Fira Code/); // only the key is mono
    await expect(list.getByText("Cart totals").first()).toHaveCSS("font-family", /Fira Sans/);
    await expect(list.getByRole("button", { name: SELECT_ALL })).toBeDisabled();
    await list.getByRole("checkbox", { name: /NWO-102 +Address form/ }).uncheck();
    await expect(list.getByText("17 of 18 issues, and the epic")).toBeVisible();
    await expect(list.getByRole("button", { name: SELECT_ALL })).toBeEnabled();
    await list.getByRole("button", { name: SELECT_NONE }).click();
    await expect(list.getByText("Only the epic")).toBeVisible();
    await expect(list.getByRole("checkbox", { name: /NWO-101/ })).not.toBeChecked();
    await expect(list.getByRole("button", { name: SELECT_NONE })).toBeDisabled();
    await list.getByRole("button", { name: SELECT_ALL }).click();
    await expect(list.getByText("18 of 18 issues, and the epic")).toBeVisible();
    await list.getByRole("checkbox", { name: /NWO-102 +Address form/ }).uncheck();
    await form.getByRole("button", { name: "Add source" }).click();
    await expect(form).toBeHidden();
    const adds = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/sources/jira-epic`);
    expect(bodiesOf(adds)).toEqual([{ key_or_link: "NWO-12", exclude: ["NWO-102"] }]);
    await expect(nodes(page).filter({ hasText: "NWO-12" })).toContainText("17 issues · Jira", { timeout: 15_000 });
  });

  test("Jira epic issues: a different key starts again with every issue checked, and an epic over the cap says only the first 200 are listed", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("Jira epic cap"));
    await seedIntegrations({ pid, atlassian: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}?add=jira_epic`);
    const form = dialog(page, "Add a Jira epic");
    const key = form.getByLabel("Epic key or link");
    await key.fill("NWO-250");
    await form.getByRole("button", { name: CHECK_EPIC }).click();
    const list = form.getByRole("group", { name: "Issues to add" });
    await expect(list.getByRole("checkbox")).toHaveCount(200);
    await expect(list.getByText("Only the first 200 issues are listed and imported.")).toBeVisible();
    await list.getByRole("button", { name: SELECT_NONE }).click();
    await key.fill("NWO-12");
    await form.getByRole("button", { name: CHECK_EPIC }).click();
    await expect(list.getByText("18 of 18 issues, and the epic")).toBeVisible();
    await expect(list.getByText("Only the first 200 issues are listed and imported.")).toBeHidden();
  });

  const DRIVE = "Connect a Google Drive folder";
  const drivePage = async (page: Page, name: string): Promise<string> => {
    const pid = await canvas(page, unique(name));
    await page.goto(`/projects/${pid}?add=gdrive`);
    return pid;
  };
  const driveTest = async (form: ReturnType<typeof dialog>, link: string) => {
    await form.getByLabel("Google Drive folder link").fill(link);
    await form.getByRole("button", { name: TEST_CONNECTION }).click();
  };
  const driveAdds = (requests: RequestLog[], pid: string) =>
    requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/sources/gdrive`);

  test("Drive files: a test lists the files flat and checked, a root file without a path and a nested one with it; unchecking one sends it as exclude", async ({
    page,
    requests,
  }) => {
    const pid = await drivePage(page, "Drive files");
    const form = dialog(page, DRIVE);
    await driveTest(form, FOLDER);
    await expect(form.getByText(/We found 11 files in 3 folders\./)).toBeVisible(); // the summary is as it was
    const list = form.getByRole("group", { name: FILES_TO_ADD });
    const boxes = list.getByRole("checkbox");
    await expect(boxes).toHaveCount(10); // the shortcut is a skipped category: not offered
    for (const box of await boxes.all()) await expect(box).toBeChecked();
    await expect(list.getByText("10 of 10 files")).toBeVisible();
    await expect(list.getByRole("checkbox", { name: "Contract.pdf", exact: true })).toBeChecked(); // a root file: no path after the name
    await expect(list.getByRole("checkbox", { name: "Specs.pdf design", exact: true })).toBeChecked();
    await expect(list.getByRole("checkbox", { name: "Pricing.pdf design/archive", exact: true })).toBeChecked();
    await expect(list.getByRole("checkbox", { name: /Link to Roadmap/ })).toHaveCount(0);
    await expect(list.getByText("design/archive", { exact: true })).toHaveCount(3);
    await expect(list.getByText("design", { exact: true })).toHaveCount(3);
    await expect(list.getByText("design/archive", { exact: true }).first()).toHaveCSS("font-family", /Fira Sans/);
    await expect(list.getByText("Specs.pdf").first()).toHaveCSS("font-size", "14px");
    await expect(list.getByText("design", { exact: true }).first()).toHaveCSS("font-size", "12.5px");
    await expect(list.getByRole("button", { name: SELECT_ALL })).toBeDisabled();
    await list.getByRole("checkbox", { name: "Specs.pdf design", exact: true }).uncheck();
    await expect(list.getByText("9 of 10 files")).toBeVisible();
    await expect(list.getByRole("button", { name: SELECT_ALL })).toBeEnabled();
    await form.getByRole("button", { name: ADD.source }).click();
    await expect(form).toBeHidden();
    expect(bodiesOf(driveAdds(requests, pid))).toEqual([{ link: FOLDER, exclude: ["fake-synthetic-30-f5"] }]);
    await expect(nodes(page).filter({ hasText: "Recorded folder" })).toHaveCount(1);
  });

  test("Drive files: Select none says No files and turns Add off, Select all restores every file, and a different link starts again with all checked", async ({
    page,
    requests,
  }) => {
    const pid = await drivePage(page, "Drive none");
    const form = dialog(page, DRIVE);
    await driveTest(form, FOLDER);
    const list = form.getByRole("group", { name: FILES_TO_ADD });
    await list.getByRole("button", { name: SELECT_NONE }).click();
    await expect(list.getByText("No files")).toBeVisible();
    await expect(list.getByRole("checkbox", { name: "Contract.pdf", exact: true })).not.toBeChecked();
    await expect(list.getByRole("button", { name: SELECT_NONE })).toBeDisabled();
    await expect(form.getByRole("button", { name: ADD.source })).toBeDisabled(); // a folder with no files imports nothing
    await list.getByRole("checkbox", { name: "Contract.pdf", exact: true }).check();
    await expect(list.getByText("1 of 10 files")).toBeVisible();
    await expect(form.getByRole("button", { name: ADD.source })).toBeEnabled();
    await list.getByRole("button", { name: SELECT_NONE }).click();
    await list.getByRole("button", { name: SELECT_ALL }).click();
    await expect(list.getByText("10 of 10 files")).toBeVisible();
    await expect(form.getByRole("button", { name: ADD.source })).toBeEnabled();
    await list.getByRole("button", { name: SELECT_NONE }).click();
    await driveTest(form, "https://drive.google.com/drive/folders/fake-synthetic-23"); // another link: a new test, nothing left out
    await expect(list.getByText("10 of 10 files")).toBeVisible();
    await form.getByRole("button", { name: ADD.source }).click();
    await expect(form).toBeHidden();
    expect(bodiesOf(driveAdds(requests, pid))).toEqual([{ link: "https://drive.google.com/drive/folders/fake-synthetic-23" }]); // no exclude when all
  });

  test("Drive files: a folder with more files than an import takes says so once, quietly", async ({ page }) => {
    await drivePage(page, "Drive capped");
    const form = dialog(page, DRIVE);
    await driveTest(form, "https://drive.google.com/drive/folders/fake-capped-folder");
    const list = form.getByRole("group", { name: FILES_TO_ADD });
    await expect(list.getByRole("checkbox")).toHaveCount(1000);
    await expect(list.getByText("1000 of 1000 files")).toBeVisible();
    await expect(form.getByText(/more files than one import takes/)).toHaveCount(1);
    await driveTest(form, FOLDER);
    await expect(list.getByText("10 of 10 files")).toBeVisible();
    await expect(form.getByText(/more files than one import takes/)).toHaveCount(0);
  });

  test("Slack: the form shows the invite and the connect command, waits, and names the channel once it is bound in Slack, then Test connection posts the welcome message (or says why not) and Add channel closes; the channel is a node under Gap", async ({
    page,
    requests,
  }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 409/); // the refused test is an answer, not a bug
    const pid = await canvas(page, unique("Slack form"));
    await page.getByRole("button", { name: ADD.channel }).click();
    await dialog(page, "Add a gap channel")
      .getByRole("button", { name: /^Slack channel/ })
      .click();
    const form = dialog(page, "Connect a Slack channel");
    await expect(form.getByText("/invite @SpecFlow", { exact: true })).toBeVisible();
    await expect(form.getByText(`/specflow connect ${pid}`, { exact: true })).toBeVisible();
    await expect(form.getByText("Waiting for the channel.")).toBeVisible();
    await expect(form.getByText("Test it: Graphico posts a welcome message in the channel.")).toBeVisible();
    await expect(form.getByRole("button", { name: TEST_CONNECTION })).toBeDisabled(); // nothing to post in before the binding
    await seedSlack({ pid, channel: CHANNEL });
    await expect(form.getByText("#northwind-specs is connected.")).toBeVisible({ timeout: 15_000 });
    await expect(form.getByRole("button", { name: TEST_CONNECTION })).toBeEnabled();
    // The app is not in the channel: the API's word, and Test connection stays to try again.
    await seedSlack({ pid, channel: CHANNEL, test: "slack_not_in_channel" });
    await form.getByRole("button", { name: TEST_CONNECTION }).click();
    await expect(form.getByRole("alert")).toContainText("The app is not in the channel any more");
    await expect(form.getByRole("button", { name: "Add channel" })).toHaveCount(0);
    await seedSlack({ pid, channel: CHANNEL, test: "ok" });
    await form.getByRole("button", { name: TEST_CONNECTION }).click();
    await expect(form.getByText(/^Posted in #northwind-specs: “Welcome to the workspace of project /)).toBeVisible();
    await expect(form.getByText(/If it arrived, add the channel\.$/)).toBeVisible();
    await form.getByRole("button", { name: "Add channel" }).click();
    await expect(form).toBeHidden();
    const tests = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/slack/test`);
    expect(tests).toHaveLength(2);
    expect(tests.every((r) => r.postData === null || r.postData === "")).toBe(true); // no body: the server writes the message
    const node = nodes(page).filter({ hasText: "#northwind-specs" });
    await expect(node).toHaveAttribute(STATE, "wait");
    await expect(node).toContainText("Waits for gaps");
    await expect(wire(page, "slack-C0NWO")).toHaveAttribute(MOTION, "off");
  });

  test("GitHub Projects: no GitHub on the project says Set up first; the repository, the project (a new one named after this project) and Check access are asked, a missing permission says what to approve, Add destination saves it and the node counts the issues made", async ({
    page,
    requests,
  }) => {
    const pid = await gapsFound(page, "GitHub plan form");
    await buildGraphAndPlan(page, pid);
    await seedGitHub({ connection: { status: "active", login: "alex-kim" } });
    await seedGitHub({ pid, repositories: [{ full_name: "northwind/ordering" }] });
    await page.goto(`/projects/${pid}`);
    await page.getByRole("button", { name: ADD.destination }).click();
    const plus = dialog(page, "Add a plan destination");
    await expect(plus.getByRole("button", { name: /^GitHub Projects/ })).toContainText(SET_UP_FIRST);
    await plus.getByRole("button", { name: /^GitHub Projects/ }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/integrations\\?setup=github`));
    await seedIntegrations({ pid, github: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}?add=github_plan`);
    const form = dialog(page, "Send the plan to GitHub Projects");
    await expect(form.getByLabel("Repository")).toHaveValue(/.+/);
    await expect(form.getByLabel("Project").getByRole("option", { name: "Roadmap" })).toBeAttached();
    const creating = form.getByLabel("Project").getByRole("option", { name: /^Create “GitHub plan form / });
    await expect(creating).toBeAttached();
    const name = ((await creating.textContent()) ?? "").slice("Create “".length, -1); // the project's own name
    await seedGitHubProjects({ pid, access: { reason: "github_permissions_missing" } });
    await form.getByRole("button", { name: CHECK_ACCESS }).click();
    await expect(form.getByRole("alert")).toContainText("Approve the new permissions of the Specflow GitHub App");
    await expect(form.getByRole("button", { name: CHECK_ACCESS })).toBeVisible(); // still a check: nothing to add yet
    await seedGitHubProjects({ pid, access: { reason: "github_project_not_found", field: "project" } });
    await form.getByLabel("Project").selectOption({ label: "Roadmap" });
    await form.getByRole("button", { name: CHECK_ACCESS }).click();
    await expect(form.getByRole("alert")).toContainText("no open project with this title");
    await seedGitHubProjects({ pid, access: "ok" });
    await form.getByLabel("Project").selectOption({ label: `Create “${name}”` });
    await form.getByRole("button", { name: CHECK_ACCESS }).click();
    await expect(form.getByText(`Organization acme. A project named “${name}” will be created.`)).toBeVisible();
    await form.getByRole("button", { name: "Add destination" }).click();
    await expect(form).toBeHidden();
    const saves = requests.filter((r) => r.method === "PUT" && r.path === `/api/backend/projects/${pid}/github-projects`);
    const [body] = bodiesOf(saves) as { repository_id: string; new_project_title: string; enabled: boolean }[];
    expect([body.new_project_title, body.enabled]).toEqual([name, true]);
    const node = nodes(page).filter({ hasText: `GitHub · ${name}` });
    await expect(node).toHaveAttribute(STATE, "on");
    await expect(node).toContainText("In sync");
    // A pass that runs moves the wire and is read again until it ends.
    const target = {
      repository_id: body.repository_id,
      repository: "northwind/ordering",
      project_node_id: "PVT_new1",
      project_title: name,
    };
    await seedGitHubProjects({ pid, target: { ...target, sync: { status: "running", counts: null } } });
    await page.reload();
    await expect(node).toHaveAttribute(STATE, "busy");
    await expect(wire(page, "github-plan")).toHaveAttribute(MOTION, "flow");
    await seedGitHubProjects({
      pid,
      target: {
        ...target,
        sync: { status: "succeeded", counts: { created: 6, updated: 0, commented: 0, removed: 0, ranked: 6, unchanged: 0, failed: 0 } },
      },
    });
    await expect(node).toContainText("6 issues · GitHub", { timeout: 15_000 });
    await expect(wire(page, "github-plan")).toHaveAttribute(MOTION, "on");
  });

  test("a finished build queues the gap run by itself, and once it is done the Gap hub is on and Build plan is offered", async ({
    page,
  }) => {
    const { pid } = await readyProject(page, "Gap run after build");
    await buildGraph(page, pid);
    await page.goto(`/projects/${pid}`);
    await expect(hub(page, "plan")).toHaveAttribute(STATE, /wait|ready/);
    await expect(hub(page, "gap")).toHaveAttribute(STATE, "on", { timeout: 15_000 });
    await expect(button(page, "Plan. Build plan")).toBeVisible();
  });

  test("Jira gaps: no Atlassian on the project says Set up first and comes back with the form open; the space is checked, the grouping chosen, Add channel saves it and the node counts what was posted and answered", async ({
    page,
    requests,
  }) => {
    const pid = await gapsFound(page, "Jira gaps form");
    await page.getByRole("button", { name: ADD.channel }).click();
    const plus = dialog(page, "Add a gap channel");
    await expect(plus.getByRole("button", { name: /^Jira sub-tasks/ })).toContainText(SET_UP_FIRST);
    await plus.getByRole("button", { name: /^Jira sub-tasks/ }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/integrations\\?setup=atlassian&next=%2Fprojects%2F${pid}%3Fadd%3Djira_gap$`));
    await seedIntegrations({ pid, atlassian: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}?add=jira_gap`);
    const form = dialog(page, "Send gaps to Jira");
    await expect(form.getByText(/about every 60 seconds/)).toBeVisible();
    await expect(form.getByRole("button", { name: CHECK_ACCESS })).toBeDisabled();
    await form.getByLabel(SPACE_KEY).fill("nwo");
    await seedJiraGaps({ pid, access: { reason: "jira_forbidden", field: "jira_project" } });
    await form.getByRole("button", { name: CHECK_ACCESS }).click();
    await expect(form.getByText("The Specflow account may not create, edit, link or rank issues in this space.")).toBeVisible();
    await expect(form.getByRole("button", { name: CHECK_ACCESS })).toBeVisible(); // still a check: nothing to add yet
    await seedJiraGaps({ pid, access: "ok" });
    await form.getByLabel("Sub-tasks").selectOption("area");
    await form.getByRole("button", { name: CHECK_ACCESS }).click();
    await expect(form.getByText("Jira space Northwind.")).toBeVisible();
    await form.getByLabel(SPACE_KEY).fill("nwo "); // the same key (trimmed): the check still holds
    await expect(form.getByRole("button", { name: "Add channel" })).toBeVisible();
    await form.getByLabel(SPACE_KEY).fill("nwx"); // another key is a new check
    await expect(form.getByRole("button", { name: CHECK_ACCESS })).toBeVisible();
    await form.getByRole("button", { name: CHECK_ACCESS }).click();
    await form.getByRole("button", { name: "Add channel" }).click();
    await expect(form).toBeHidden();
    const saves = requests.filter((r) => r.method === "PUT" && r.path === `/api/backend/projects/${pid}/jira-gaps`);
    expect(bodiesOf(saves)).toEqual([{ jira_project: "NWX", grouping: "area", enabled: true }]);
    const node = nodes(page).filter({ hasText: "Jira · NWX gaps" });
    await expect(node).toHaveAttribute(STATE, "on");
    await expect(node).toContainText("0 posted · 0 answered");
    await seedJiraGaps({
      pid,
      target: { jira_project: "NWX", grouping: "area", enabled: true, story_key: "NWO-5", posted: 7, answered: 2 },
    });
    await expect(node).toContainText("7 posted · 2 answered", { timeout: 15_000 });
    await node.getByRole("button").click();
    await expect(dialog(page, "Jira · NWX gaps").getByText(/Story NWO-5\./)).toBeVisible();
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
    await form.getByLabel(SPACE_KEY).fill("nwo");
    await seedJira({ pid, access: { reason: "jira_not_found", field: "jira_project" } });
    await form.getByRole("button", { name: CHECK_ACCESS }).click();
    await expect(form.getByText(/space/i).first()).toBeVisible();
    await expect(form.getByRole("button", { name: CHECK_ACCESS })).toBeVisible(); // still a check: nothing to add yet
    await seedJira({ pid, access: "ok" });
    await form.getByLabel(SPACE_KEY).fill("nwo");
    await form.getByLabel("Epic key").fill("nwo-1");
    await form.getByRole("button", { name: CHECK_ACCESS }).click();
    await expect(
      form
        .getByRole("status")
        .filter({ hasText: /Jira space/ })
        .last(),
    ).toBeVisible();
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
