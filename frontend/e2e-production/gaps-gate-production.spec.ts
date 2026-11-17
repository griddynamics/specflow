// Gaps Ready gate on the PRODUCTION routes, against the stub API through the real BFF (FE-DEC-02.AC5, FE-BP-03.AC8, FE-DS-08.AC2, FR-GWEB-04; D19, D20).
// No paid model call (R11). Before the gate opens the screen sends no `/api/backend` request beyond the gate's; once it is open the Gaps screen reads
// only the gap routes (the inbox with `held_back=false`, the held-back questions with `held_back=true`), the documents and the plan for its filters,
// all GETs. The "at most one sources GET" bound holds for the production build (no StrictMode double effect).
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import {
  allowConsoleErrors,
  allowedGateCalls,
  calls,
  createProjectApi,
  createViaNew,
  expectGateNetwork,
  expect,
  expectUploadReady,
  expectSourceCard,
  login,
  pickLocalPdf,
  rail,
  seedDocument,
  STUB_TIMING,
  stubTiming,
  test,
} from "./support";

test.setTimeout(120_000);

const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
const gate = (page: Page) => page.getByTestId("gaps-gate");
/** The gap rows: the items of the inbox list (labelled "Gaps"). */
const gapRows = (page: Page) => page.getByRole("list", { name: "Gaps" }).getByRole("listitem");
/**
 * How many scene backdrops the page paints: elements with the design system's `gdg-scene` class. A backdrop is a styling fact with no
 * role, label or text, so it is counted in the document rather than located like page content.
 */
const sceneCount = (page: Page) => page.evaluate(() => document.getElementsByClassName("gdg-scene").length);

test.afterEach(async () => {
  await stubTiming(STUB_TIMING);
});

async function expectGate(page: Page) {
  const g = gate(page);
  await expect(g.getByRole("heading", { name: "No gaps yet" })).toBeVisible();
  await expect(g).toContainText("Gaps appear here after Specflow has processed this project's files.");
  await expect(g.getByRole("img", { includeHidden: true }).first()).toBeVisible(); // the empty state's icon (decorative, aria-hidden)
  await expect(g.getByRole("button", { name: "Open Mission Control" })).toBeVisible();
  await expect(gapRows(page)).toHaveCount(0);
  await expect(page.getByTestId("gap-filters")).toHaveCount(0);
  await expect(page.getByRole("group", { name: "View" })).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
}

// P1: AC1, AC2, AC8, AC11
test(
  "FE-DEC-02.AC5, FE-BP-03.AC8, FE-DS-08.AC2 (P1): a new project shows the Gaps gate on the scene; the only backend calls are the shell's and one GET of the sources status; the button opens Mission Control",
  { tag: "@production" },
  async ({ page, requests }) => {
    await login(page);
    const pid = await createProjectApi(page, unique("Gaps empty"));
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(page.getByTestId("mcs-empty")).toBeVisible();
    const from = requests.length;
    await rail(page).getByRole("button", { name: "Gaps", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps$`));
    await expectGate(page);
    // AC11: the gate wrapper is the one .gdg-scene on the page (Mission Control is unmounted)
    await expect(gate(page)).toHaveClass(/\bgdg-scene\b/);
    await expect.poll(() => sceneCount(page)).toBe(1);
    expect(await gate(page).evaluate((el) => getComputedStyle(el).backgroundImage)).toContain("gd-prism-cubes");
    await expectGateNetwork(page, requests, from, pid);
    await gate(page).getByRole("button", { name: "Open Mission Control" }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/mission-control$`));
    await expect(page.getByTestId("screen-mission-control")).toBeVisible();
  },
);

// P2: AC3 (a phase other than empty and ready)
test(
  "FE-DEC-02.AC5 (P2): a project whose only file is still being parsed (no processed document yet) shows the Gaps gate",
  { tag: "@production" },
  async ({ page, requests }) => {
    await login(page);
    const pid = await createProjectApi(page, unique("Gaps unfinished"));
    await stubTiming({ parseMs: null }); // the stub never finishes the parse
    await seedDocument(page, pid, "uploaded only.pdf");
    await stubTiming(STUB_TIMING);
    await page.goto(`/projects/${pid}/mission-control`);
    await expectSourceCard(page, "upload", { state: "processing" });
    const from = requests.length;
    await rail(page).getByRole("button", { name: "Gaps", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps$`));
    await expectGate(page);
    expect(calls(requests.slice(from)).filter((r) => r.method !== "GET")).toEqual([]);
  },
);

// P3: AC4 (a failed read is "blocked")
test("FE-DEC-02.AC5 (P3): when the sources status read fails (503) the Gaps gate shows", { tag: "@production" }, async ({ page }) => {
  allowConsoleErrors(page, /status of 503/);
  await login(page);
  const pid = await createProjectApi(page, unique("Gaps 503"));
  await page.goto(`/projects/${pid}/mission-control`);
  await expect(page.getByTestId("mcs-empty")).toBeVisible();
  await page.route(new RegExp(`/api/backend/projects/${pid}/sources`), (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "unavailable" }) }),
  );
  await rail(page).getByRole("button", { name: "Gaps", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps$`));
  await expectGate(page);
});

// P4: AC5, AC8, AC12
test(
  "FE-DEC-01.AC1, FE-BP-03.AC8, FE-DS-08.AC3, FR-GWEB-04 (P4): a Ready project shows no gate and no scene but 'No open gaps'; the screen reads only the gap routes (held_back=false and true, the latest gap run), the build state, the documents and the plan, all GETs",
  { tag: "@production" },
  async ({ page, requests }) => {
    await login(page);
    const pid = await createViaNew(page, unique("Gaps ready"));
    await pickLocalPdf(page, "requirements.pdf"); // Local files; the server parses (stub 1.5 s); Ready = one processed document
    await expectUploadReady(page);
    const from = requests.length;
    await rail(page).getByRole("button", { name: "Gaps", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps$`));
    await expect(page.getByTestId("gap-filters")).toBeVisible();
    await expect(gate(page)).toHaveCount(0);
    await expect.poll(() => sceneCount(page)).toBe(0);
    // No gap run has found anything for this project: the inbox is empty and says so; nothing was held back.
    await expect(page.getByTestId("gaps-none").getByRole("heading", { name: "No open gaps" })).toBeVisible();
    await expect(gapRows(page)).toHaveCount(0);
    await expect(page.getByTestId("gap-count")).toHaveText("");
    await expect(page.getByTestId("gaps-held-back")).toHaveCount(0);
    const gapReads = new RegExp(`^/api/backend/projects/${pid}/(?:gaps|documents|plan|gap/runs/latest|graph_build)$`);
    const allowed = allowedGateCalls(pid);
    await expectGateNetwork(page, requests, from, pid, (r) => allowed(r) || (r.method === "GET" && gapReads.test(r.path)));
    const made = calls(requests.slice(from)).map((r) => r.path + r.search);
    expect(made).toContain(`/api/backend/projects/${pid}/gaps?route=ask&held_back=false`);
    expect(made).toContain(`/api/backend/projects/${pid}/gaps?route=ask&held_back=true`);
    expect(made).toContain(`/api/backend/projects/${pid}/gap/runs/latest`); // the gap check line
    await expect(page.getByTestId("gap-check-title")).toHaveText("No gap check yet");
  },
);
