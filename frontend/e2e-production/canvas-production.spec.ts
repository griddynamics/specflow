// Mission Control V2, the canvas of a project on the PRODUCTION routes in a real browser, against the stub API through the real BFF
// (docs/mission-control/README.md "The canvas", "The flow" scenes 1-3, plus the plus dialog, Upload, Google Drive and MCP; DECISIONS D5, D10).
// Scene 1 nothing connected, scene 2 importing, scene 3 ready to build. Every case seeds its own project through the control port.
import { ADD, canvas, dialog, done, FOLDER, hub, MOTION, nodes, parsed, SPECS, STATE, unique, wire, YELLOW } from "./canvas-support";
import { FINISHED_RUN, shopGaps } from "./gaps-fixtures";
import { buildGraphAndPlan, expect, seedGaps, seedSource, syntheticPdf, test, v2Header } from "./support";

test.describe("Mission Control V2: the canvas", { tag: "@production" }, () => {
  test("scene 1, nothing connected: three zones on one line, three node-sized plus tiles, grey hubs, dashed wires, no yellow, no heading", async ({
    page,
  }) => {
    await canvas(page, unique("Scene one"));
    for (const zone of ["01 · Graph", "02 · Gap", "03 · Plan"]) await expect(page.getByRole("region", { name: zone })).toBeVisible();
    for (const label of [ADD.source, ADD.channel, ADD.destination]) await expect(page.getByRole("button", { name: label })).toBeVisible();
    for (const id of ["graph", "gap", "plan"] as const) {
      await expect(hub(page, id)).toHaveAttribute(STATE, "off");
      await expect(hub(page, id)).toHaveAttribute(YELLOW, "false");
    }
    // One line: the three hubs are on the same horizontal axis.
    const hubs = await Promise.all((["graph", "gap", "plan"] as const).map((id) => hub(page, id).boundingBox()));
    const axes = hubs.map((b) => (b === null ? Number.NaN : b.y + b.height / 2));
    expect(Math.max(...axes) - Math.min(...axes)).toBeLessThanOrEqual(1);
    await expect(nodes(page)).toHaveCount(0);
    for (const key of ["graph-gap", "gap-plan"]) await expect(wire(page, key)).toHaveAttribute(MOTION, "off");
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(0);
    await expect(page.getByRole("heading", { level: 2 })).toHaveCount(0); // a dialog's title only shows while it is open
    // The three plus tiles are node-sized, and Graph's hub is the anchor its wires start from.
    for (const label of [ADD.source, ADD.channel, ADD.destination]) {
      await expect(page.getByRole("button", { name: label })).toHaveAttribute("data-big", "true");
    }
    await expect(hub(page, "graph")).toHaveAttribute("data-anchor", "graph");
  });

  test("scene 2, importing: a node per source with its progress, the wires into the Graph hub move, the hub waits; the reduced-motion setting stops them", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("Scene two"));
    const drive = await seedSource(pid, {
      kind: "gdrive",
      name: SPECS,
      files: [...done(6), ...Array.from({ length: 18 }, (_, i) => ({ name: `wait-${String(i)}.pdf`, state: "processing" }))],
    });
    const upload = await seedSource(pid, {
      kind: "upload",
      files: [...done(4), { name: "late.pdf", state: "waiting" }, { name: "later.pdf", state: "waiting" }],
    });
    await page.goto(`/projects/${pid}`);
    await expect(nodes(page)).toHaveCount(2);
    const specs = nodes(page).filter({ hasText: SPECS });
    await expect(specs).toHaveAttribute(STATE, "busy");
    await expect(specs).toContainText("6 of 24 files");
    await expect(nodes(page).filter({ hasText: /Upload \d/ })).toContainText("4 of 6 files");
    await expect(hub(page, "graph")).toHaveAttribute(STATE, "wait");
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "false");
    for (const sid of [drive, upload]) {
      await expect(wire(page, sid)).toHaveAttribute(MOTION, "flow");
      await expect(wire(page, sid)).toHaveCSS("animation-name", "gx-flow");
    }
    await page.emulateMedia({ reducedMotion: "reduce" });
    await expect(wire(page, drive)).toHaveCSS("animation-name", "none");
    await expect(wire(page, drive)).toHaveCSS("stroke-dasharray", "none");
    await expect(page.getByRole("button", { name: ADD.source })).toBeVisible();
  });

  test("scene 3, ready to build: the Graph hub is the one yellow decision; the sources are ink with a check and solid wires", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("Scene three"));
    const drive = await seedSource(pid, { kind: "gdrive", name: SPECS, files: done(24) });
    await seedSource(pid, { kind: "upload", files: done(6) });
    await parsed(page, pid);
    await page.goto(`/projects/${pid}`);
    await expect(hub(page, "graph")).toHaveAttribute(STATE, "ready");
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "true");
    await expect(hub(page, "graph")).toContainText("Build graph");
    for (const id of ["gap", "plan"] as const) {
      await expect(hub(page, id)).toHaveAttribute(STATE, "off");
      await expect(hub(page, id)).toHaveAttribute(YELLOW, "false");
    }
    await expect(nodes(page).filter({ hasText: SPECS })).toContainText("24 files · Google Drive");
    await expect(wire(page, drive)).toHaveAttribute(MOTION, "on");
    await expect(page.getByText("Update graph")).toHaveCount(0);
    await expect(page.getByTestId("hub-plan")).not.toContainText("Build plan");
    await expect(v2Header(page).getByRole("link", { name: "Mission Control" })).toHaveAttribute("aria-current", "page");
  });

  test("four sources do not push the plus off the screen: its label stays fully visible", async ({ page }) => {
    const pid = await canvas(page, unique("Four sources"));
    for (let i = 0; i < 4; i += 1) await seedSource(pid, { kind: "upload", files: done(1) });
    await page.goto(`/projects/${pid}`);
    await expect(nodes(page)).toHaveCount(4);
    await expect(page.getByText(ADD.source, { exact: true })).toBeInViewport({ ratio: 1 });
    await expect(nodes(page).last()).toBeInViewport({ ratio: 1 });
  });

  test("the plus dialog lists its stage first, the others dashed; a connection that needs an app says Set up first; MCP is not listed", async ({
    page,
  }) => {
    await canvas(page, unique("Plus"));
    await page.getByRole("button", { name: ADD.source }).click();
    const plus = dialog(page, "Add a source");
    await expect(plus.getByRole("button", { name: /^Upload/ })).toBeEnabled();
    await expect(plus.getByRole("button", { name: /^Google Drive/ })).toBeEnabled();
    await expect(plus.getByRole("button", { name: /^GitHub repository/ })).toContainText("Set up first"); // GitHub is not connected to this project
    await expect(plus.getByRole("button", { name: /MCP/ })).toHaveCount(0);
    await expect(plus.getByRole("link", { name: "Integrations" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(plus).toBeHidden();
    await page.getByRole("button", { name: ADD.channel }).click();
    const gap = dialog(page, "Add a gap channel");
    await expect(gap.getByRole("button", { name: /^Slack channel/ })).toBeEnabled();
    await gap.getByText("Connections for the other stages").click();
    await expect(gap.getByRole("button", { name: /^Upload/ })).toHaveAttribute("aria-disabled", "true");
    await expect(gap.getByText("For Graph")).toHaveCount(3);
    await expect(gap.getByText("For Plan")).toHaveCount(1);
  });

  test("Upload: pick a file in the form, the dialog closes, the batch is a node that finishes importing", async ({ page, requests }) => {
    const pid = await canvas(page, unique("Upload"));
    await page.getByRole("button", { name: ADD.source }).click();
    await dialog(page, "Add a source")
      .getByRole("button", { name: /^Upload/ })
      .click();
    const form = dialog(page, "Add files");
    await expect(form.getByText("Drop files here")).toBeVisible();
    await form.getByLabel("Choose files").setInputFiles({ name: "spec.pdf", mimeType: "application/pdf", buffer: syntheticPdf() });
    await expect(form).toBeHidden();
    const node = nodes(page).filter({ hasText: /Upload \d/ });
    await expect(node).toHaveCount(1);
    await expect(node).toHaveAttribute(STATE, "on", { timeout: 30_000 });
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "true");
    expect(requests.filter((r) => r.method === "POST" && r.path.startsWith(`/api/backend/projects/${pid}/sources`)).length).toBeGreaterThan(
      0,
    );
  });

  test("Google Drive: the account to share with, a link, Test connection, then Add source: a node that lists its files; a bad link says so and sends nothing", async ({
    page,
    requests,
  }) => {
    const pid = await canvas(page, unique("Drive"));
    await page.getByRole("button", { name: ADD.source }).click();
    await dialog(page, "Add a source")
      .getByRole("button", { name: /^Google Drive/ })
      .click();
    const form = dialog(page, "Connect a Google Drive folder");
    await expect(form.getByText("Share the folder with this account as a Viewer:")).toBeVisible();
    const link = form.getByLabel("Google Drive folder link");
    await link.fill("not a link");
    await form.getByRole("button", { name: "Test connection" }).click();
    await expect(form.getByText("This is not a Google Drive folder link.")).toBeVisible();
    expect(requests.some((r) => r.path.endsWith("/sources/gdrive/test"))).toBe(false);
    await link.fill(FOLDER);
    await form.getByRole("button", { name: "Test connection" }).click();
    await expect(form.getByText(/We found 11 files in 3 folders\./)).toBeVisible();
    await form.getByRole("button", { name: ADD.source }).click();
    await expect(form).toBeHidden();
    const node = nodes(page).filter({ hasText: "Recorded folder" });
    await expect(node).toHaveAttribute(STATE, "busy");
    await expect(node).toContainText("Listing files…");
    await expect(hub(page, "graph")).toHaveAttribute(STATE, "wait");
    expect(requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/sources/gdrive`)).toHaveLength(1);
  });

  test("back from Integrations: ?add= opens that connection's form once and leaves the address clean", async ({ page }) => {
    const pid = await canvas(page, unique("Round trip"));
    await page.goto(`/projects/${pid}?add=gdrive`);
    await expect(dialog(page, "Connect a Google Drive folder")).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}$`));
    await page.goto(`/projects/${pid}?add=nothing`);
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}$`));
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });

  test("gaps found, then the plan: critical count on Gaps, 'Build plan' as the yellow decision, MCP joins Gap and then Plan by itself", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("Gaps and plan"));
    await seedSource(pid, { kind: "gdrive", name: SPECS, files: done(8) });
    await parsed(page, pid);
    await seedGaps(pid, { gaps: shopGaps(), run: FINISHED_RUN });
    await page.goto(`/projects/${pid}`);
    await expect(hub(page, "graph")).toHaveAttribute(YELLOW, "true");
    await buildGraphAndPlan(page, pid); // the stub builds the graph, then the plan, without a model
    await page.goto(`/projects/${pid}`);
    await expect(hub(page, "plan")).toHaveAttribute(STATE, "on");
    await expect(hub(page, "gap")).toHaveAttribute(STATE, "on");
    await expect(hub(page, "gap")).toContainText(/\d critical/);
    const mcp = nodes(page).filter({ hasText: "MCP" });
    await expect(mcp).toHaveCount(2);
    await expect(mcp.first()).toContainText("Automatic");
    await expect(wire(page, "gap-plan")).toHaveAttribute(MOTION, "on");
    await mcp.first().getByRole("button").click();
    const agent = dialog(page, "Connect a coding agent over MCP");
    await expect(
      agent.getByText(/claude mcp add --transport http .*--client-id specflow-test .* https:\/\/mcp\.example\.com\/mcp/),
    ).toBeVisible();
    await expect(agent.getByText("answer_gap", { exact: true })).toBeVisible();
    await expect(agent.getByText("claim_story", { exact: true })).toHaveCount(0);
    await agent.getByRole("button", { name: "Done" }).click();
    await expect(agent).toBeHidden();
  });
});
