// MCP-M10 Guides on the PRODUCTION routes (FR-MCP-UI-01 to 06 and 09), against the stub API through the real BFF. Dev sign-in mode
// shows the LOCAL variant (`specflow-local`) with the address from the server env of scripts/serve-production.mjs. The oidc variant
// is rendered by the production render suite instead.
import { readFileSync } from "node:fs";
import path from "node:path";
import type { Locator, Page } from "@playwright/test";
import { allowConsoleErrors, createProjectApi, expect, login, rail, seedDocument, test } from "./support";

const MCP_URL = "https://mcp.example.com/mcp";
const MCP_GUIDE = "/guides/mcp-server";
const TOPICS = [
  { slug: "specflow-101", title: "SpecFlow 101" },
  { slug: "best-practices", title: "Best practices" },
  { slug: "mcp-server", title: "MCP server" },
] as const;
const manifest = JSON.parse(readFileSync(path.resolve(__dirname, "../lib/guides/mcp-tools.json"), "utf8")) as {
  tools: { name: string }[];
  prompts: { name: string }[];
};

const blocks = (page: Page): Locator => page.getByTestId("code-block");
/** The copy block whose text contains `part`: its `<pre>` and its COPY button (one button per block, in page order). */
async function block(page: Page, part: string) {
  const texts = await blocks(page).allTextContents();
  const i = texts.findIndex((t) => t.includes(part));
  expect(i, `a copy block with "${part}"`).toBeGreaterThanOrEqual(0);
  return { pre: blocks(page).nth(i), copy: page.getByRole("button", { name: /^(Copy|Copied)$/ }).nth(i) };
}
const noHorizontalScroll = async (page: Page) => {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
};

test.describe("Guides on production routes", { tag: "@production" }, () => {
  test.setTimeout(120_000);

  // FR-MCP-UI-01
  test("FR-MCP-UI-01: Guides is in the sidebar on the home page and inside a project; the three topics open; an unknown topic answers 404", async ({
    page,
  }) => {
    await login(page);
    await page.goto("/");
    await rail(page).getByRole("button", { name: "Guides", exact: true }).click();
    await expect(page).toHaveURL(/\/guides$/);

    const pid = await createProjectApi(page, `Guides ${Date.now().toString(36)}`);
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(page.getByTestId("screen-mission-control")).toBeVisible();
    await rail(page).getByRole("button", { name: "Guides", exact: true }).click();
    await expect(page).toHaveURL(/\/guides$/);

    for (const t of TOPICS) {
      const res = await page.goto(`/guides/${t.slug}`);
      expect(res?.status(), t.slug).toBe(200);
      await expect(page.getByRole("navigation", { name: "Guides path" })).toContainText(t.title);
      await expect(rail(page).getByRole("button", { name: "Guides", exact: true })).toHaveAttribute("aria-current", "page");
    }
    allowConsoleErrors(page, /Failed to load resource.*404/); // the browser logs the expected 404
    const missing = await page.goto("/guides/nope");
    expect(missing?.status()).toBe(404);
  });

  // FR-MCP-UI-02
  test("FR-MCP-UI-02: the hub shows three cards with OPEN links; each topic's breadcrumb returns to /guides", async ({ page }) => {
    await login(page);
    await page.goto("/guides");
    for (const t of TOPICS) await expect(page.getByRole("link", { name: `Open ${t.title}` })).toHaveAttribute("href", `/guides/${t.slug}`);
    await expect(page.getByTestId("card")).toHaveCount(3);
    for (const t of TOPICS) {
      await page.getByRole("link", { name: `Open ${t.title}` }).click();
      await expect(page).toHaveURL(new RegExp(`/guides/${t.slug}$`));
      await page.getByRole("navigation", { name: "Guides path" }).getByRole("link", { name: "Guides" }).click();
      await expect(page).toHaveURL(/\/guides$/);
    }
  });

  // FR-MCP-UI-03
  test("FR-MCP-UI-03: the step-1 command holds the configured address", async ({ page }) => {
    await login(page);
    await page.goto(MCP_GUIDE);
    const step1 = blocks(page).first();
    await expect(step1).toContainText("claude mcp add-json --scope local specflow-local");
    await expect(step1).toContainText(`"url":"${MCP_URL}"`);
    // In dev mode the client id (`MCP_CLIENT_ID=specflow-test`) is not part of any snippet: the local variant has no OAuth client. The
    // production variant, which shows it in the step-1 command and the team .mcp.json, is covered by the render tests.
    await expect(page.getByTestId("guide-mcp")).not.toContainText("specflow-test");
  });

  // FR-MCP-UI-04
  test("FR-MCP-UI-04: COPY puts exactly the snippet on the clipboard and shows Copied", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await login(page);
    await page.goto(MCP_GUIDE);
    for (const part of ["claude mcp add-json --scope local", "Connect this repository to the SpecFlow project"]) {
      const { pre, copy } = await block(page, part);
      const shown = (await pre.textContent()) ?? "";
      expect(shown.length).toBeGreaterThan(0);
      await copy.click();
      await expect(copy).toHaveText("Copied");
      expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(shown);
    }
  });

  // FR-MCP-UI-05
  test("FR-MCP-UI-05: Claude Code shows the steps; Codex and Cursor show 'Setup is in progress'; ?agent= deep-links", async ({ page }) => {
    await login(page);
    await page.goto(MCP_GUIDE);
    await expect(page.getByTestId("mcp-steps")).toBeVisible();
    await expect(page.getByTestId("mcp-in-progress")).toHaveCount(0);
    for (const agent of ["Codex", "Cursor"]) {
      await page.getByRole("button", { name: agent, exact: true }).click();
      await expect(page.getByTestId("mcp-in-progress")).toContainText("setup is in progress");
      await expect(page.getByTestId("mcp-steps")).toHaveCount(0);
    }
    await page.getByRole("button", { name: "Claude Code", exact: true }).click();
    await expect(page.getByTestId("mcp-steps")).toBeVisible();
    await page.goto("/guides/mcp-server?agent=codex");
    await expect(page.getByTestId("mcp-in-progress")).toContainText("Codex setup is in progress");
    await expect(page.getByRole("button", { name: "Codex", exact: true })).toHaveAttribute("aria-pressed", "true");
  });

  // FR-MCP-UI-06
  test("FR-MCP-UI-06: the reference table lists every tool and prompt of the manifest; the prompt is the specflow-local command", async ({
    page,
  }) => {
    await login(page);
    await page.goto(MCP_GUIDE);
    const table = page.getByRole("table");
    for (const t of manifest.tools) await expect(table.getByRole("cell", { name: t.name, exact: true })).toBeVisible();
    for (const p of manifest.prompts)
      await expect(table.getByRole("cell", { name: `/mcp__specflow-local__${p.name}`, exact: true })).toBeVisible();
  });

  // FR-MCP-UI-09 (Guides pages; Plan › Develop is covered with FR-MCP-UI-07)
  test("FR-MCP-UI-09: at 390x844 the hub and the three topics have no horizontal page scroll", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);
    for (const url of ["/guides", ...TOPICS.map((t) => `/guides/${t.slug}`)]) {
      await page.goto(url);
      await expect(page.getByRole("navigation", { name: "Guides path" })).toBeVisible();
      await noHorizontalScroll(page);
    }
  });

  test("FR-MCP-UI-09: Tab reaches the hub's OPEN links and the SpecFlow 101 step buttons with a visible focus", async ({ page }) => {
    await login(page);
    await page.goto("/guides");
    await expect(page.getByTestId("guides-hub")).toBeVisible();
    for (const t of TOPICS) await reachByTab(page, page.getByRole("link", { name: `Open ${t.title}` }));
    await page.goto("/guides/specflow-101");
    const steps = page.getByTestId("guide-flow").getByRole("button");
    expect(await steps.count()).toBeGreaterThan(0);
    for (let i = 0; i < (await steps.count()); i++) await reachByTab(page, steps.nth(i));
  });
});

const NAME = 'Checkout "v2"';

/** A project whose plan screen is past the gate (one processed document, stub parse 1.5 s), opened on Plan › Develop through the switch. */
async function openDevelop(page: Page): Promise<string> {
  await login(page);
  const pid = await createProjectApi(page, NAME);
  await seedDocument(page, pid, "requirements.pdf");
  await expect(async () => {
    await page.goto(`/projects/${pid}/plan`);
    await expect(page.getByTestId("screen-plan")).toHaveAttribute("data-stage", "idle", { timeout: 3_000 });
  }).toPass({ timeout: 40_000 });
  await page.getByRole("group", { name: "Plan view" }).getByRole("button", { name: "Develop", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${pid}/plan\\?view=develop$`));
  await expect(page.getByTestId("plan-develop")).toBeVisible();
  return pid;
}

test.describe("Plan › Develop on production routes (idle stage, U5)", { tag: "@production" }, () => {
  test.setTimeout(120_000);

  // FR-MCP-UI-07
  test("FR-MCP-UI-07: Develop shows the bind command, the binding JSON of this project and the Guides link, and no mock text", async ({
    page,
  }) => {
    const pid = await openDevelop(page);
    const develop = page.getByTestId("plan-develop");
    await expect(develop.getByTestId("code-block").first()).toHaveText(`/mcp__specflow-local__select_project ${pid}`);
    await page.getByText("Or save the binding file yourself").click();
    const texts = await develop.getByTestId("code-block").allTextContents();
    const json = texts.find((t) => t.trimStart().startsWith("{"));
    expect(JSON.parse(json ?? "null")).toEqual({ version: 1, server: MCP_URL, project_id: pid, project_name: NAME });
    await expect(develop.getByRole("link", { name: /guide/i })).toHaveAttribute("href", MCP_GUIDE);
    const shown = (await develop.textContent()) ?? "";
    for (const mock of ["illustrative", "Codex", "Cursor"]) expect(shown.toLowerCase(), mock).not.toContain(mock.toLowerCase());
    await expect(develop.getByText(/^(Teach|Run|Connect your agent)$/)).toHaveCount(0); // the old mock tab's headings
    // the idle Plan view keeps its own content
    await page.getByRole("group", { name: "Plan view" }).getByRole("button", { name: "Plan", exact: true }).click();
    await expect(page.getByTestId("plan-develop")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Create PLAN", exact: true })).toBeVisible();
  });

  // FR-MCP-UI-04
  test("FR-MCP-UI-04: COPY on the bind command and on the binding JSON puts exactly the shown text on the clipboard", async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await openDevelop(page);
    await page.getByText("Or save the binding file yourself").click();
    for (const part of ["/mcp__specflow-local__select_project", '"project_name"']) {
      const { pre, copy } = await block(page, part);
      const shown = (await pre.textContent()) ?? "";
      expect(shown.length).toBeGreaterThan(0);
      await copy.click();
      await expect(copy).toHaveText("Copied");
      expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(shown);
    }
  });

  // FR-MCP-UI-09
  test("FR-MCP-UI-09: at 390x844 Plan › Develop has no horizontal page scroll", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openDevelop(page);
    await page.getByText("Or save the binding file yourself").click();
    await expect(page.getByText('"project_name"')).toBeVisible();
    await noHorizontalScroll(page);
  });
});

/** Presses Tab (from where the last call stopped) until `target` has focus (at most 60 presses), then checks the focus ring is drawn (`:focus-visible`, a non-empty outline or shadow). */
async function reachByTab(page: Page, target: Locator): Promise<void> {
  for (let i = 0; i < 60; i++) {
    await page.keyboard.press("Tab");
    if (await target.evaluate((el) => el === document.activeElement)) {
      const visible = await target.evaluate((el) => {
        const s = getComputedStyle(el);
        return el.matches(":focus-visible") && (s.outlineStyle !== "none" || s.boxShadow !== "none");
      });
      expect(visible, "visible focus").toBe(true);
      return;
    }
  }
  throw new Error("Tab never reached the target");
}
