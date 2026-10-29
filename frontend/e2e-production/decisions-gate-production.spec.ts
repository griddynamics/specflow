// Decisions Ready gate on the PRODUCTION routes, against the stub API through the real BFF (FE-DEC-02.AC5, FE-BP-03.AC8, FE-DS-08.AC2; D19, D20).
// No paid model call (R11). The Decisions port answers an empty list (SPL-DEMO-02), so after the gate has read the project's documents the screen sends no
// `/api/backend` request. The "at most one documents GET" bound holds for the production build (no StrictMode double effect).
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import {
  allowConsoleErrors,
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
const gate = (page: Page) => page.getByTestId("decisions-gate");
/** The decision rows: the items of the DecisionList (its list is labelled "Decisions"). */
const decisionRows = (page: Page) => page.getByRole("list", { name: "Decisions" }).getByRole("listitem");
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
  await expect(g.getByRole("heading", { name: "No decisions yet" })).toBeVisible();
  await expect(g).toContainText("Decisions appear here after Specflow has processed this project's files.");
  await expect(g.getByRole("img", { includeHidden: true }).first()).toBeVisible(); // the empty state's icon (decorative, aria-hidden)
  await expect(g.getByRole("button", { name: "Open Mission Control" })).toBeVisible();
  await expect(decisionRows(page)).toHaveCount(0);
  await expect(page.getByTestId("decision-filters")).toHaveCount(0);
  await expect(page.getByRole("group", { name: "Severity" })).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
}

// P1: AC1, AC2, AC8, AC11
test(
  "FE-DEC-02.AC5, FE-BP-03.AC8, FE-DS-08.AC2 (P1): a new project shows the Decisions gate on the scene; the only backend calls are the shell's and one GET of the sources status; the button opens Mission Control",
  { tag: "@production" },
  async ({ page, requests }) => {
    await login(page);
    const pid = await createProjectApi(page, unique("Decisions empty"));
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(page.getByTestId("mcs-empty")).toBeVisible();
    const from = requests.length;
    await rail(page).getByRole("button", { name: "Decisions", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/decisions$`));
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
  "FE-DEC-02.AC5 (P2): a project whose only file is still being parsed (no processed document yet) shows the Decisions gate",
  { tag: "@production" },
  async ({ page, requests }) => {
    await login(page);
    const pid = await createProjectApi(page, unique("Decisions unfinished"));
    await stubTiming({ parseMs: null }); // the stub never finishes the parse
    await seedDocument(page, pid, "uploaded only.pdf");
    await stubTiming(STUB_TIMING);
    await page.goto(`/projects/${pid}/mission-control`);
    await expectSourceCard(page, "upload", { state: "processing" });
    const from = requests.length;
    await rail(page).getByRole("button", { name: "Decisions", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/decisions$`));
    await expectGate(page);
    expect(calls(requests.slice(from)).filter((r) => r.method !== "GET")).toEqual([]);
  },
);

// P3: AC4 (a failed read is "blocked")
test("FE-DEC-02.AC5 (P3): when the sources status read fails (503) the Decisions gate shows", { tag: "@production" }, async ({ page }) => {
  allowConsoleErrors(page, /status of 503/);
  await login(page);
  const pid = await createProjectApi(page, unique("Decisions 503"));
  await page.goto(`/projects/${pid}/mission-control`);
  await expect(page.getByTestId("mcs-empty")).toBeVisible();
  await page.route(new RegExp(`/api/backend/projects/${pid}/sources`), (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "unavailable" }) }),
  );
  await rail(page).getByRole("button", { name: "Decisions", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${pid}/decisions$`));
  await expectGate(page);
});

// P4: AC5, AC8, AC12
test(
  "FE-DEC-01.AC1, FE-BP-03.AC8, FE-DS-08.AC3, SPL-DEMO-02 (P4): a Ready project shows no gate and no scene, but 'No decisions yet' with no rows, and no /api/backend request for decisions",
  { tag: "@production" },
  async ({ page, requests }) => {
    await login(page);
    const pid = await createViaNew(page, unique("Decisions ready"));
    await pickLocalPdf(page, "requirements.pdf"); // Local files; the server parses (stub 1.5 s); Ready = one processed document
    await expectUploadReady(page);
    const from = requests.length;
    await rail(page).getByRole("button", { name: "Decisions", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/decisions$`));
    await expect(page.getByTestId("decision-filters")).toBeVisible();
    await expect(gate(page)).toHaveCount(0);
    await expect.poll(() => sceneCount(page)).toBe(0);
    // The decisions are not served yet (SPL-DEMO-02): the list is empty and the screen says so.
    await expect(page.getByTestId("decisions-none").getByRole("heading", { name: "No decisions yet" })).toBeVisible();
    await expect(decisionRows(page)).toHaveCount(0);
    await expect(page.getByTestId("decision-count")).toHaveText("");
    await expectGateNetwork(page, requests, from, pid);
  },
);
