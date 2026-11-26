// Mission Control V2, the owner's first round of feedback, on the PRODUCTION routes in a real browser against the stub API through the real
// BFF: the copy icon in the code boxes, "Show me how" in the setup forms, MCP as a card of Integrations (and the `?setup=mcp` deep link),
// a name for an upload batch, the files inside a source node, and renaming an upload. The canvas MCP dialog is in canvas-production.spec.ts.
import type { Page } from "@playwright/test";
import { ADD, canvas, dialog, done, nodes, STATE, unique } from "./canvas-support";
import { allowConsoleErrors, expect, seedGitHub, seedIntegrations, seedJira, seedSlack, seedSource, syntheticPdf, test } from "./support";

const card = (page: Page, name: string) => page.getByRole("listitem").filter({ has: page.getByRole("heading", { name, exact: true }) });
const integrations = (pid: string) => `/projects/${pid}/integrations`;
const MANY = "Many files";
const BOARD = "Board papers";
const FILES_LIST = "Files in this source";
const CANVAS = (pid: string) => new RegExp(`/projects/${pid}$`);
const bodyOf = (r: { postData: string | null }): unknown => JSON.parse(r.postData ?? "{}");
const CONFLICT = JSON.stringify({ error: { code: "source_not_renamable", message: "SERVER TEXT", details: {} } });
const GONE = JSON.stringify({ error: { code: "source_not_found", message: "SERVER TEXT", details: {} } });

test.beforeEach(async () => {
  await seedJira({ pid: "none", mode: "oauth", connection: null }); // Atlassian runs in the oauth mode on the server V2 targets
});

test.afterEach(async () => {
  await seedGitHub({ reset: true });
  await seedJira({ pid: "none", mode: "token", connection: null });
  await seedIntegrations({ reset: true });
  await seedSlack({ pid: "none", channel: null, available: true, test: "ok" });
});

test.describe("Mission Control V2: owner feedback, round one", { tag: "@production" }, () => {
  test("MCP is a card on Integrations with Connect; it opens the connect steps, and Built in keeps only Upload", async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const pid = await canvas(page, unique("MCP card"));
    await page.goto(integrations(pid));
    await expect(page.getByRole("list", { name: "Integrations" }).getByRole("listitem")).toHaveCount(5);
    const mcp = card(page, "MCP");
    await expect(mcp).toContainText("Coding agents");
    await expect(mcp.getByRole("button", { name: "Connect" })).toBeVisible();
    await expect(page.getByText("Upload and Google Drive")).toBeVisible();
    await expect(page.getByText("Added to the canvas by itself.")).toHaveCount(0);
    await mcp.getByRole("button", { name: "Connect" }).click();
    const d = dialog(page, "MCP");
    await expect(d.getByText("Add the server in Claude Code:")).toBeVisible();
    await expect(
      d.getByText(/claude mcp add --transport http .*--client-id specflow-test .* https:\/\/mcp\.example\.com\/mcp/),
    ).toBeVisible();
    await expect(d.getByText("/mcp", { exact: true })).toBeVisible();
    await expect(d.getByText(`/mcp__specflow__select_project ${pid}`)).toBeVisible();
    await expect(d.getByText("Cursor, Codex and other clients: add the same address as a remote MCP server.")).toBeVisible();
    // The copy icon: an icon in the box with the name, a check and "Copied" for about two seconds, the text on the clipboard.
    const copy = d.getByRole("button", { name: "Copy sign-in command" });
    await expect(copy).toHaveText(""); // an icon, not the word
    await copy.click();
    const copied = d.getByRole("button", { name: "Copied" });
    await expect(copied).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("/mcp");
    await expect(d.getByRole("status").filter({ hasText: "Copied" })).toHaveCount(1); // the polite live region
    await expect(d.getByRole("button", { name: "Copy sign-in command" })).toBeVisible({ timeout: 6_000 });
    await d.getByRole("button", { name: "Done" }).click();
    await expect(d).toBeHidden();
    await expect(page).toHaveURL(new RegExp(`${integrations(pid)}$`));
  });

  test("?setup=mcp opens the MCP dialog like the other cards, and Done follows next back to the canvas", async ({ page }) => {
    const pid = await canvas(page, unique("MCP deep link"));
    const next = encodeURIComponent(`/projects/${pid}`);
    await page.goto(`${integrations(pid)}?setup=mcp&next=${next}`);
    const d = dialog(page, "MCP");
    await expect(d).toBeVisible();
    await expect(d.getByText("Bind the repository to this project:")).toBeVisible();
    await d.getByRole("button", { name: "Done" }).click();
    await expect(page).toHaveURL(CANVAS(pid));
  });

  test("Show me how: closed at first and opening inline in the Slack form and on the Integrations set-ups of Slack, Atlassian and GitHub", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("Show me how"));
    await page.getByRole("button", { name: ADD.channel }).click();
    await dialog(page, "Add a gap channel")
      .getByRole("button", { name: /^Slack channel/ })
      .click();
    const form = dialog(page, "Connect a Slack channel");
    const step = form.getByText("Type /invite @SpecFlow and press Enter.");
    await expect(step).toBeHidden();
    await form.getByText("Show me how").click();
    await expect(step).toBeVisible();
    await expect(
      form.getByText("Use a channel of your workspace: not a direct message, and not one shared with another workspace."),
    ).toBeVisible();
    await page.keyboard.press("Escape");

    await page.goto(integrations(pid));
    const opens: [string, string, string][] = [
      ["Slack", "Slack", "In the same channel, run the command below."],
      ["Atlassian", "Atlassian", "Sign in to Atlassian and approve the access Graphico asks for."],
      ["GitHub", "GitHub", "Press Connect GitHub and approve the app with your own GitHub account."],
    ];
    for (const [name, title, text] of opens) {
      await card(page, name).getByRole("button", { name: "Set up" }).click();
      const d = dialog(page, title);
      await expect(d.getByText(text)).toBeHidden();
      await d.getByText("Show me how").click();
      await expect(d.getByText(text)).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(d).toBeHidden();
    }
  });

  test("Show me how in the Drive form: closed, then the four sharing steps from the docs", async ({ page }) => {
    await canvas(page, unique("Drive how"));
    await page.getByRole("button", { name: ADD.source }).click();
    await dialog(page, "Add a source")
      .getByRole("button", { name: /^Google Drive/ })
      .click();
    const form = dialog(page, "Connect a Google Drive folder");
    const first = form.getByText("In Google Drive, open the menu of the folder and choose Share → Share.");
    await expect(first).toBeHidden();
    await form.getByText("Show me how").click();
    await expect(first).toBeVisible();
    await expect(form.getByText(/Click Share anyway\./)).toBeVisible();
  });

  test("Upload with a name: the placeholder is the default node name; the name is sent trimmed, and left blank the default stays", async ({
    page,
    requests,
  }) => {
    const pid = await canvas(page, unique("Named upload"));
    const open = async () => {
      await page.getByRole("button", { name: ADD.source }).click();
      await dialog(page, "Add a source")
        .getByRole("button", { name: /^Upload/ })
        .click();
      return dialog(page, "Add files");
    };
    const form = await open();
    const name = form.getByLabel("Name (optional)");
    await expect(name).toHaveAttribute("placeholder", /^Upload \d\d:\d\d$/);
    await name.fill("  Board papers ");
    await form.getByLabel("Choose files").setInputFiles({ name: "board.pdf", mimeType: "application/pdf", buffer: syntheticPdf() });
    await expect(form).toBeHidden();
    const named = nodes(page).filter({ hasText: BOARD });
    await expect(named).toHaveCount(1);
    await expect(named).toHaveAttribute(STATE, "on", { timeout: 30_000 });

    const again = await open();
    await again.getByLabel("Choose files").setInputFiles({ name: "other.pdf", mimeType: "application/pdf", buffer: syntheticPdf(2100) });
    await expect(again).toBeHidden();
    await expect(nodes(page).filter({ hasText: /^Upload \d\d:\d\d/ })).toHaveCount(1);
    const starts = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/sources/uploads`);
    expect(starts.map(bodyOf)).toEqual([{ expected_files: 1, name: BOARD }, { expected_files: 1 }]);
  });

  test("a source node lists the files inside: kind always, a state and the V1 reason only when it is not done; Show more pages", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("Node files"));
    await seedSource(pid, {
      kind: "gdrive",
      name: "Specs folder",
      files: [
        { name: "overview.pdf", state: "done" },
        { name: "photo.png", state: "skipped", category: "images" },
        { name: "broken.pdf", state: "failed", reason: "read_failed" },
        { name: "notes.md", state: "done", category: "markdown" },
      ],
    });
    await seedSource(pid, { kind: "upload", name: MANY, files: done(230) });
    await page.goto(`/projects/${pid}`);
    await nodes(page).filter({ hasText: "Specs folder" }).getByRole("button").click();
    const d = dialog(page, "Specs folder");
    await expect(d.getByText("Files", { exact: true })).toBeVisible();
    const rows = d.getByRole("list", { name: FILES_LIST }).getByRole("listitem");
    await expect(rows).toHaveCount(4);
    const row = (name: string) => rows.filter({ hasText: name });
    await expect(row("overview.pdf")).toContainText("PDF");
    await expect(row("overview.pdf")).not.toContainText(/Done|Skipped|Failed|Processing|Waiting/); // done: no state
    await expect(row("notes.md")).toContainText("Markdown");
    await expect(row("photo.png")).toContainText("Images");
    await expect(row("photo.png").getByText("Skipped", { exact: true })).toBeVisible();
    await expect(row("photo.png")).toContainText("Skipped: Specflow does not import Images.");
    await expect(row("broken.pdf").getByText("Failed", { exact: true })).toBeVisible();
    await expect(row("broken.pdf")).toContainText("Specflow could not read this file.");
    await expect(d.getByRole("button", { name: "Rename" })).toHaveCount(0); // only an upload batch is renamed
    await expect(d.getByRole("button", { name: "Show more" })).toHaveCount(0);
    await d.getByRole("button", { name: "Done" }).click();

    await nodes(page).filter({ hasText: MANY }).getByRole("button").click();
    const many = dialog(page, MANY);
    const all = many.getByRole("list", { name: FILES_LIST }).getByRole("listitem");
    await expect(all).toHaveCount(200);
    await many.getByRole("button", { name: "Show more" }).click();
    await expect(all).toHaveCount(230);
    await expect(many.getByRole("button", { name: "Show more" })).toHaveCount(0);
  });

  test("Rename an upload: Save renames the node; a refused name, a source that is not an upload and a source that is gone are said in words", async ({
    page,
    requests,
  }) => {
    allowConsoleErrors(page, /status of (404|409|422)/); // the browser's line for each refused answer
    const pid = await canvas(page, unique("Rename"));
    await seedSource(pid, { kind: "upload", files: done(2) });
    await page.goto(`/projects/${pid}`);
    await nodes(page)
      .filter({ hasText: /^Upload \d/ })
      .getByRole("button")
      .click();
    const d = page.getByRole("dialog");
    await expect(d.getByRole("list", { name: FILES_LIST }).getByRole("listitem")).toHaveCount(2);
    await d.getByRole("button", { name: "Rename" }).click();
    const field = d.getByLabel("Name", { exact: true });
    await expect(field).toHaveValue(/^Upload \d/);
    await expect(d.getByRole("button", { name: "Rename" })).toHaveCount(0);
    // Cancel closes the editor and changes nothing.
    await d.getByRole("button", { name: "Cancel" }).click();
    await expect(field).toHaveCount(0);
    await d.getByRole("button", { name: "Rename" }).click();
    // A blank name cannot be saved; a name that is too long is refused by the server and said in words.
    await field.fill("   ");
    await expect(d.getByRole("button", { name: "Save" })).toBeDisabled();
    await field.fill("x".repeat(81));
    await d.getByRole("button", { name: "Save" }).click();
    await expect(d.getByRole("alert")).toHaveText("The name needs 1 to 80 characters, and no control characters.");
    // Not an upload (409) and gone (404): faked answers, the real UI only offers Rename on an upload.
    let answer: { status: number; body: string } | null = { status: 409, body: CONFLICT };
    await page.route("**/api/backend/projects/*/sources/*", (route) => {
      if (route.request().method() !== "PATCH" || answer === null) return route.fallback();
      return route.fulfill({ status: answer.status, contentType: "application/json", body: answer.body });
    });
    await field.fill(BOARD);
    await d.getByRole("button", { name: "Save" }).click();
    await expect(d.getByRole("alert")).toHaveText("Only an upload can be renamed.");
    answer = { status: 404, body: GONE };
    await d.getByRole("button", { name: "Save" }).click();
    await expect(d.getByRole("alert")).toHaveText("This source is gone. Close this and look at the canvas again.");
    answer = null;
    await d.getByRole("button", { name: "Save" }).click();
    await expect(page.getByRole("dialog", { name: BOARD })).toBeVisible(); // the canvas was read again: the dialog carries the new name
    await expect(nodes(page).filter({ hasText: BOARD })).toHaveCount(1);
    await expect(d.getByLabel("Name", { exact: true })).toHaveCount(0);
    const patches = requests.filter((r) => r.method === "PATCH" && /\/sources\/[0-9a-f-]{36}$/.test(r.path));
    expect(patches.map(bodyOf).at(-1)).toEqual({ name: BOARD });
  });
});
