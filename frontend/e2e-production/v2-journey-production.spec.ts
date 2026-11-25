// Mission Control V2, the whole journey on the PRODUCTION routes against the stub API through the real BFF (docs/mission-control/ui.md): one node of
// each registered kind, then the graph build, the gaps (the stub runs the gap run after the build; a critical gap is seeded), the plan, a node that
// stops when GitHub needs a reconnect, and the reconnect. Every form is the real one; only the apps' answers are the stub's.
import type { Locator, Page } from "@playwright/test";
import { canvas, dialog, done, hub, MOTION, nodes, parsed, SPECS, STATE, unique, wire, YELLOW } from "./canvas-support";
import { FINISHED_RUN, shopGaps } from "./gaps-fixtures";
import { expect, seedGaps, seedGitHub, seedIntegrations, seedJira, seedJiraGaps, seedSlack, seedSource, test } from "./support";

const ALEX = { user_id: "u-alex", name: "Alex Kim" };
const CHANNEL = { id: "C0NWO", name: "northwind-specs", private: false };
const SITE = "https://northwind.atlassian.test";
const PLUS = { graph: "Add source", gap: "Add channel", plan: "Add destination" } as const;
const CHECK = "Check access";

/** The plus of a stage, the connector's card, and the form it opens. */
async function openForm(page: Page, stage: keyof typeof PLUS, card: RegExp, title: string): Promise<Locator> {
  await page.getByRole("button", { name: PLUS[stage] }).click();
  await page.getByRole("dialog").getByRole("button", { name: card }).click();
  return dialog(page, title);
}

/** A link source: type the key or link, Check, then Add source. */
async function addLinkSource(page: Page, card: RegExp, title: string, field: string, value: string, check: string): Promise<void> {
  const form = await openForm(page, "graph", card, title);
  await form.getByLabel(field).fill(value);
  await form.getByRole("button", { name: check }).click();
  await form.getByRole("button", { name: "Add source" }).click();
  await expect(form).toBeHidden();
}

/** A destination or channel with a space key: type it, Check access, then the add button. */
async function addKeyed(page: Page, stage: "gap" | "plan", card: RegExp, title: string, add: string): Promise<void> {
  const form = await openForm(page, stage, card, title);
  await form.getByLabel("Jira space key").fill("nwo");
  await form.getByRole("button", { name: CHECK }).click();
  await form.getByRole("button", { name: add }).click();
  await expect(form).toBeHidden();
}

test.beforeEach(async () => {
  await seedJira({ pid: "none", mode: "oauth", connection: { status: "active", account_name: "Alex Kim" } });
  await seedGitHub({ connection: { status: "active", login: "alex-kim" } });
});

test.afterEach(async () => {
  await seedGitHub({ reset: true });
  await seedIntegrations({ reset: true });
  await seedSlack({ pid: "none", channel: null, available: true, test: "ok" });
  await seedJira({ pid: "none", mode: "token", connection: null });
  await seedJiraGaps({ pid: "none", available: true });
});

test.describe("Mission Control V2: the journey", { tag: "@production" }, () => {
  test("a node of every kind, the build, the gaps, the plan, a node that stops and the reconnect", async ({ page }) => {
    test.setTimeout(240_000);
    const pid = await canvas(page, unique("Journey"));
    await seedIntegrations({ pid, atlassian: { by: ALEX, health: "active" }, github: { by: ALEX, health: "active" } });
    await page.goto(`/projects/${pid}`);

    // 01 Graph: a node of each source kind.
    await seedSource(pid, { kind: "upload", files: done(2) });
    await seedSource(pid, { kind: "gdrive", name: SPECS, files: done(24) });
    await parsed(page, pid);
    await page.reload();
    const repos = await openForm(page, "graph", /^GitHub repository/, "Add repositories");
    await repos.getByLabel("acme/payments-api").check();
    await repos.getByRole("button", { name: "Add selected" }).click();
    await expect(repos).toBeHidden();
    await addLinkSource(
      page,
      /^Confluence link/,
      "Add a Confluence link",
      "Confluence page or space link",
      `${SITE}/wiki/spaces/ORD/pages/12345/Checkout`,
      "Check link",
    );
    await addLinkSource(page, /^Jira epic/, "Add a Jira epic", "Epic key or link", "NWO-12", "Check epic");
    await expect(
      nodes(page)
        .filter({ hasText: /^Upload / })
        .first(),
    ).toBeVisible(); // the seeded batch and the parsed document
    for (const name of [SPECS, "payments-api", "Checkout notes", "NWO-12"])
      await expect(nodes(page).filter({ hasText: name })).toHaveCount(1);
    await expect(nodes(page).filter({ hasText: "NWO-12" })).toHaveAttribute(STATE, "on", { timeout: 15_000 });

    // 02 Gap: the Slack channel is bound from inside Slack; the Jira sub-tasks are added here.
    await seedSlack({ pid, channel: CHANNEL });
    await addKeyed(page, "gap", /^Jira sub-tasks/, "Send gaps to Jira", "Add channel");
    await expect(nodes(page).filter({ hasText: "#northwind-specs" })).toHaveCount(1);
    await expect(nodes(page).filter({ hasText: "Jira · NWO gaps" })).toHaveAttribute(STATE, "wait");

    // The build: Graph is the one open decision.
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "true");
    await page.getByRole("button", { name: "Graph. Build graph" }).click();
    await dialog(page, "Build the graph").getByRole("button", { name: "Build", exact: true }).click();
    await expect(hub(page, "graph")).toHaveAttribute(STATE, "busy");
    await expect(hub(page, "graph")).toHaveAttribute(STATE, "on", { timeout: 20_000 });

    // The gap run follows the build by itself; a critical gap is then seeded.
    await expect(hub(page, "gap")).toHaveAttribute(STATE, "on", { timeout: 20_000 });
    await seedGaps(pid, { gaps: shopGaps(), run: FINISHED_RUN });
    await page.reload(); // an idle gap run is read every 30 s; a person who answered elsewhere sees it on the next read
    await expect(hub(page, "gap")).toContainText(/\d critical/, { timeout: 15_000 });
    await expect(nodes(page).filter({ hasText: "#northwind-specs" })).toContainText("found");
    await expect(nodes(page).filter({ hasText: "Jira · NWO gaps" })).toContainText("0 posted");

    // 03 Plan: Build plan is the open decision; the destinations follow.
    await expect(hub(page, "plan")).toHaveAttribute(YELLOW, "true");
    await page.getByRole("button", { name: "Plan. Build plan" }).click();
    await dialog(page, "Build the plan").getByRole("button", { name: "Build plan" }).click();
    await expect(hub(page, "plan")).toHaveAttribute(STATE, "on", { timeout: 20_000 });
    await addKeyed(page, "plan", /^Jira Stories/, "Send the plan to Jira", "Add destination");
    const target = await openForm(page, "plan", /^GitHub Projects/, "Send the plan to GitHub Projects");
    await target.getByRole("button", { name: CHECK }).click();
    await target.getByRole("button", { name: "Add destination" }).click();
    await expect(target).toBeHidden();
    await expect(
      nodes(page)
        .filter({ hasText: /^Jira · NWO/ })
        .first(),
    ).toBeVisible();
    const planNode = nodes(page).filter({ hasText: "GitHub · " });
    await expect(planNode).toHaveCount(1);
    await expect(nodes(page).filter({ hasText: "Automatic" })).toHaveCount(2); // MCP joins Gap and Plan by itself

    // A connection stops: GitHub needs a reconnect, so both its nodes stop; the dialog offers Reconnect GitHub.
    await seedIntegrations({ pid, github: { by: ALEX, health: "reconnect_required" } });
    await page.reload();
    const repoNode = nodes(page).filter({ hasText: "payments-api" });
    await expect(repoNode).toHaveAttribute(STATE, "stopped");
    await expect(planNode).toHaveAttribute(STATE, "stopped");
    await expect(wire(page, "github-plan")).toHaveAttribute(MOTION, "off");
    await planNode.getByRole("button").click();
    await expect(dialog(page, /^GitHub · /).getByRole("link", { name: "Reconnect GitHub" })).toHaveAttribute(
      "href",
      new RegExp(`/projects/${pid}/integrations\\?setup=github`),
    );
    await page.keyboard.press("Escape");

    // The member reconnects: the nodes work again.
    await seedIntegrations({ pid, github: { by: ALEX, health: "active" } });
    await page.reload();
    await expect(repoNode).not.toHaveAttribute(STATE, "stopped");
    await expect(planNode).not.toHaveAttribute(STATE, "stopped");
    await expect(hub(page, "plan")).toHaveAttribute(STATE, "on");
  });
});
