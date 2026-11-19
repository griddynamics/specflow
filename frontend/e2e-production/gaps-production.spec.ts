// FR-GWEB-04 (read part), FR-GRTE-06: the Gaps screen on the PRODUCTION routes against the stub API through the real BFF, with shop-corpus
// gaps seeded through the stub's control port (no paid model call): the inbox (every open gap a run found, a page at a time with "Show
// more"), "Raise a gap" and "Resolve gaps" in the header row beside the tabs (off with its reason while nothing is staged), the filters
// going to the server (Status: Open, Staged, Resolved, All; the Assigned filter and column, FR-GWEB-06), the
// question card with its recommended option preselected, quotes and ABOUT/IMPACTS chips, the noise report tab, the gap check status line
// and the "not available" state of `503 gap_unavailable`. Opening the screen and a card only reads (GETs); the writes are in
// gaps-write-production.spec.ts.
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import {
  CANCEL_TITLE,
  foundGaps,
  GUEST_TITLE,
  ME_ID,
  NOISE_RUN,
  PAYMENT_PROVIDER_FINDING,
  PAYMENT_PROVIDER_TITLE,
  shopGaps,
} from "./gaps-fixtures";
import {
  allowConsoleErrors,
  buildGraphAndPlan,
  calls,
  createViaNew,
  defined,
  expect,
  expectUploadReady,
  login,
  pickLocalPdf,
  rail,
  seedGaps,
  STUB_TIMING,
  stubTiming,
  test,
} from "./support";

const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;

const ROUNDING_TITLE = "Order totals are rounded differently"; // assigned to the caller in the shop gaps
const DELIVERY_TITLE = '"Next day" delivery has no cut-off time';
const RETURN_TITLE = "The return window is not stated"; // resolved
const screen = (page: Page) => page.getByTestId("screen-gaps");
const inboxRows = (page: Page) => page.getByRole("list", { name: "Gaps" }).getByRole("listitem");
const row = (page: Page, title: string) => inboxRows(page).filter({ hasText: title });
const showMore = (page: Page) => screen(page).getByRole("button", { name: "Show more", exact: true });

async function pick(page: Page, filter: string, choice: string) {
  await page.getByTestId(`select-${filter}`).getByRole("button").click();
  await page.getByRole("menu", { name: filter }).getByRole("menuitemradio", { name: choice, exact: true }).click();
}

test.describe("Gaps on production routes", { tag: "@production" }, () => {
  test.setTimeout(120_000);

  test.afterEach(async () => {
    await stubTiming(STUB_TIMING);
  });

  /** A Ready project (one processed document) with the shop gaps seeded; returns its id. */
  async function shopProject(page: Page, opts: { plan?: boolean } = {}) {
    await login(page);
    const pid = await createViaNew(page, unique("Shop gaps"));
    await pickLocalPdf(page, "orders.pdf");
    await expectUploadReady(page);
    if (opts.plan === true) await buildGraphAndPlan(page, pid);
    await seedGaps(pid, { gaps: shopGaps(), run: NOISE_RUN });
    return { pid };
  }

  test("FR-GWEB-04, FR-GRTE-06: the inbox lists every question the run found, in pages; coding-agent gaps that hold tasks come first", async ({
    page,
    requests,
  }) => {
    const { pid } = await shopProject(page);
    const from = requests.length;
    await rail(page).getByRole("button", { name: "Gaps", exact: true }).click();
    await expect(screen(page)).toBeVisible();
    await expect(inboxRows(page)).toHaveCount(5);
    await expect(inboxRows(page).nth(0)).toContainText("Which payment provider takes the payment?");
    await expect(inboxRows(page).nth(1)).toContainText(ROUNDING_TITLE);
    await expect(row(page, CANCEL_TITLE)).toContainText("Missing");
    await expect(row(page, CANCEL_TITLE)).toContainText("Major");
    await expect(row(page, CANCEL_TITLE)).toContainText("Unassigned");
    await expect(row(page, ROUNDING_TITLE)).toContainText("Blocker");
    await expect(row(page, ROUNDING_TITLE)).toContainText("dev"); // assigned to the caller
    await expect(row(page, GUEST_TITLE)).toContainText("Assumption");
    await expect(inboxRows(page).filter({ hasText: "Prices are shown in euro" })).toHaveCount(0); // routed to AI: not a question for people
    await expect(page.getByTestId("gap-count")).toHaveText("5 gaps");
    await expect(showMore(page)).toHaveCount(0);

    const base = `/api/backend/projects/${pid}/gaps?`;
    const made = calls(requests.slice(from));
    const paths = made.map((r) => r.path + r.search);
    expect(paths).toContain(`${base}status=open&route=ask`);
    expect(paths).toContain(`${base}status=answered&status=applying&limit=200`); // the staged answers
    expect(paths.filter((p) => p.includes("held_back"))).toEqual([]);
    expect(made.filter((r) => r.method !== "GET")).toEqual([]);
    for (const tab of ["inbox", "ai", "noise"]) await expect(page.getByTestId(`gaps-tab-${tab}`)).toBeVisible();
    await expect(page.getByTestId("gaps-tab-documents")).toHaveCount(0);

    // A run that found more than a page: the first 50 rows, "50+ gaps" and "Show more"; the next page brings the rest.
    await seedGaps(pid, { gaps: [...shopGaps(), ...foundGaps(50)] });
    const reread = requests.length;
    await page.reload();
    await expect(inboxRows(page)).toHaveCount(50);
    await expect(page.getByTestId("gap-count")).toHaveText("50+ gaps");
    await expect(row(page, GUEST_TITLE)).toHaveCount(0);
    await showMore(page).click();
    await expect(inboxRows(page)).toHaveCount(55);
    await expect(page.getByTestId("gap-count")).toHaveText("55 gaps");
    await expect(row(page, GUEST_TITLE)).toHaveCount(1);
    await expect(row(page, DELIVERY_TITLE)).toHaveCount(1);
    await expect(showMore(page)).toHaveCount(0);
    const pages = calls(requests.slice(reread)).map((r) => r.path + r.search);
    expect(pages).toContain(`${base}status=open&route=ask`);
    expect(pages).toContain(`${base}status=open&route=ask&cursor=50`);
  });

  test("FR-GWEB-04: 'Raise a gap' and 'Resolve gaps' sit in the header row right of the tabs on every tab; with nothing staged Resolve gaps is off and says why", async ({
    page,
  }) => {
    const { pid } = await shopProject(page);
    await page.goto(`/projects/${pid}/gaps`);
    await expect(inboxRows(page)).toHaveCount(5);
    const head = page.getByTestId("shell-head");
    const tabs = head.getByRole("group", { name: "View" });
    const raise = head.getByTestId("gaps-raise");
    const resolve = head.getByTestId("gaps-resolve");
    for (const tab of ["inbox", "ai", "noise"]) {
      await page.getByTestId(`gaps-tab-${tab}`).click();
      await expect(raise).toBeVisible();
      await expect(resolve).toBeVisible();
      const [t, r, x] = (await Promise.all([tabs.boundingBox(), raise.boundingBox(), resolve.boundingBox()])).map((b) =>
        defined(b, "a laid-out header row"),
      );
      expect(Math.abs(t.y + t.height / 2 - (x.y + x.height / 2)), "on the tabs' line").toBeLessThan(4);
      expect(r.x, "Raise a gap right of the tabs").toBeGreaterThan(t.x + t.width);
      expect(x.x, "Resolve gaps right of Raise a gap").toBeGreaterThan(r.x + r.width);
    }
    await expect(page.getByTestId("gaps-actions")).toHaveCount(1);
    await expect(resolve).toHaveText("Resolve gaps");
    await expect(resolve).toHaveAttribute("aria-disabled", "true");
    await expect(resolve).toHaveAttribute("data-state", "off");
    await expect(resolve).toHaveAccessibleDescription("Stage an answer first.");
    await expect(resolve).toHaveClass(/gdg-btn--primary/);
    await expect(raise).toHaveClass(/gdg-btn--glass/);
    await resolve.focus(); // off but focusable (aria-disabled): Enter opens nothing
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });

  test("FR-GWEB-04, FR-GWEB-06: the filters (status, kind, severity, story, assigned) go to the server and narrow the inbox", async ({
    page,
    requests,
  }) => {
    const { pid } = await shopProject(page, { plan: true });
    await page.goto(`/projects/${pid}/gaps`);
    await expect(inboxRows(page)).toHaveCount(5);
    const filters = page.getByTestId("gap-filters");
    for (const name of ["Status", "Kind", "Severity", "Story", "Assigned"])
      await expect(filters.getByText(name, { exact: true })).toBeVisible();
    for (const name of ["Origin", "Document"]) await expect(filters.getByText(name, { exact: true })).toHaveCount(0);

    await pick(page, "Kind", "Contradiction");
    await expect(inboxRows(page)).toHaveCount(1);
    await expect(inboxRows(page)).toContainText(ROUNDING_TITLE);
    await pick(page, "Kind", "All");
    await pick(page, "Severity", "Minor");
    await expect(inboxRows(page)).toHaveCount(2);
    await expect(inboxRows(page)).toContainText([DELIVERY_TITLE, GUEST_TITLE]);
    await pick(page, "Severity", "All");
    await expect(page.getByTestId("select-Assigned").getByRole("button")).toBeVisible();
    await page.getByTestId("select-Assigned").getByRole("button").click();
    await expect(page.getByRole("menu", { name: "Assigned" }).getByRole("menuitemradio")).toHaveText(["All", "Unassigned", "dev (you)"]);
    await page.keyboard.press("Escape");
    await pick(page, "Assigned", "dev (you)");
    await expect(inboxRows(page)).toHaveCount(1);
    await expect(inboxRows(page)).toContainText(ROUNDING_TITLE);
    await pick(page, "Assigned", "Unassigned");
    await expect(inboxRows(page)).toHaveCount(4);
    await expect(inboxRows(page).filter({ hasText: ROUNDING_TITLE })).toHaveCount(0);
    await pick(page, "Assigned", "All");
    await pick(page, "Story", "Checkout");
    await expect(inboxRows(page)).toHaveCount(1);
    await expect(inboxRows(page)).toContainText(CANCEL_TITLE);
    await pick(page, "Story", "All");
    await page.getByTestId("select-Status").getByRole("button").click();
    await expect(page.getByRole("menu", { name: "Status" }).getByRole("menuitemradio")).toHaveText(["Open", "Staged", "Resolved", "All"]);
    await page.keyboard.press("Escape");
    await pick(page, "Status", "Resolved");
    await expect(inboxRows(page)).toHaveCount(1);
    await expect(inboxRows(page)).toContainText("The return window is not stated");
    await expect(inboxRows(page)).toContainText("Resolved · How many days may a customer return goods?");
    await pick(page, "Status", "Staged");
    await expect(page.getByTestId("gaps-empty")).toBeVisible(); // nothing is staged in the shop gaps
    await pick(page, "Status", "All");
    await expect(inboxRows(page)).toHaveCount(6);
    await pick(page, "Status", "Open");
    await pick(page, "Kind", "Inconsistency");
    await expect(page.getByTestId("gaps-empty").getByRole("heading", { name: "No gaps match these filters" })).toBeVisible();
    await expect(page.getByTestId("gap-count")).toHaveText("");

    const paths = calls(requests).map((r) => r.path + r.search);
    const base = `/api/backend/projects/${pid}/gaps?`;
    expect(paths).toContain(`${base}status=open&kind=contradiction&route=ask`);
    expect(paths).toContain(`${base}status=open&severity=minor&route=ask`);
    expect(paths).toContain(`${base}status=open&route=ask&assigned_to=${ME_ID}`);
    expect(paths).toContain(`${base}status=open&route=ask&assigned_to=none`);
    expect(paths).toContain(`${base}status=open&route=ask&story_id=STORY%3A%3ACHECKOUT`);
    expect(paths).toContain(`${base}status=resolved&route=ask`);
    expect(paths).toContain(`${base}status=answered&status=applying&route=ask`);
    expect(paths).toContain(`${base}status=open&status=answered&status=applying&status=resolved&route=ask`);
  });

  test("FR-GWEB-04: the question card shows the question, why, ABOUT and IMPACTS chips, quotes with their document and section, reach and the options with A recommended and preselected (major); opening it only reads", async ({
    page,
    requests,
  }) => {
    const { pid } = await shopProject(page);
    await page.goto(`/projects/${pid}/gaps`);
    const from = requests.length;
    await row(page, CANCEL_TITLE).getByRole("button").first().click(); // the row's button; the second is the assignee menu
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps\\?gap=GAP%3A%3ACANCEL_PAID$`));
    const card = page.getByRole("dialog", { name: CANCEL_TITLE });
    await expect(card).toBeVisible();
    await expect(card.getByTestId("gap-meta")).toContainText("Major");
    await expect(card.getByTestId("gap-meta")).toContainText("Missing");
    await expect(card.getByTestId("gap-meta")).toContainText("Found by analysis");
    await expect(card.getByRole("heading", { name: "What happens when a customer cancels an order that is already paid?" })).toBeVisible();
    await expect(card.getByTestId("gap-why")).toContainText("Checkout and refunds both depend on it");
    await expect(card.getByRole("list", { name: "About" }).getByRole("listitem")).toHaveText([/REQ::FR::PLACE_ORDER/, /FEAT::CHECKOUT/]);
    await expect(card.getByRole("list", { name: "Impacts" }).getByRole("listitem")).toHaveText([/REQ::FR::REFUND/]);
    await expect(card.getByTestId("gap-quotes").getByRole("listitem")).toHaveCount(2);
    await expect(card.getByTestId("gap-quotes")).toContainText("A customer may cancel an order before it ships.");
    await expect(card.getByTestId("gap-quotes")).toContainText("Orders service specification › Orders › Cancel an order · lines 41–42");
    await expect(card.getByTestId("gap-quotes")).toContainText("FRAG::payments::2 · line 12"); // no document named: the fragment form
    await expect(card.getByTestId("gap-reach")).toContainText("1 story, 2 tasks");

    const radios = card
      .getByRole("radiogroup", { name: "What happens when a customer cancels an order that is already paid?" })
      .getByRole("radio");
    await expect(radios).toHaveCount(4); // three options and "Other"
    await expect(radios.nth(0)).toContainText("Refund in full and cancel");
    await expect(radios.nth(0)).toContainText("Recommended");
    await expect(radios.nth(0)).toHaveAttribute("aria-checked", "true"); // preselected: the gap is major
    await expect(radios.nth(1)).not.toContainText("Recommended");
    await expect(radios.nth(2)).not.toContainText("Recommended");
    await expect(radios.nth(3)).toContainText("Other");
    await expect(card.getByTestId("option-recommended")).toHaveCount(1);
    for (let i = 0; i < 4; i++) await expect(radios.nth(i)).toBeEnabled();
    await expect(card.getByTestId("gap-submit")).toBeEnabled();
    await expect(card.getByTestId("gap-submit")).toHaveText("Stage answer");
    await expect(card.getByTestId("gap-dismiss")).toBeVisible();
    await expect(card.getByRole("button", { name: "Add comment" })).toHaveCount(0);

    await card.getByRole("button", { name: "Close" }).last().click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps$`));
    expect(calls(requests.slice(from)).map((r) => `${r.method} ${r.path}`)).toContain(
      `GET /api/backend/projects/${pid}/gaps/GAP%3A%3ACANCEL_PAID`,
    );
    expect(calls(requests.slice(from)).filter((r) => r.method !== "GET")).toEqual([]);
  });

  test("FR-GWEB-04: a deep link opens the card even when the filters hide its row; an unknown gap opens nothing", async ({ page }) => {
    allowConsoleErrors(page, /status of 404/); // GET /gaps/GAP::NOPE answers 404 gap_not_found
    const { pid } = await shopProject(page);
    await page.goto(`/projects/${pid}/gaps?gap=${encodeURIComponent("GAP::GUEST_CHECKOUT")}`);
    await expect(page.getByRole("dialog", { name: GUEST_TITLE })).toBeVisible();
    await expect(page.getByRole("dialog").getByTestId("gap-no-options")).toContainText("Options are being prepared");
    await expect(page.getByRole("dialog").getByRole("radio", { name: /Other/ })).toBeEnabled(); // an own answer is accepted at once
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.goto(`/projects/${pid}/gaps?gap=${encodeURIComponent("GAP::RETURN_WINDOW")}`);
    await expect(page.getByRole("dialog", { name: RETURN_TITLE })).toBeVisible();
    await expect(row(page, RETURN_TITLE)).toHaveCount(0); // resolved: the Open filter hides its row
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.goto(`/projects/${pid}/gaps?gap=GAP%3A%3ANOPE`);
    await expect(inboxRows(page)).toHaveCount(5);
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps$`));
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });

  test("FR-GWEB-04: the old Decisions URL redirects to the Gaps screen with its query; a migrated finding's old id opens its gap's card", async ({
    page,
  }) => {
    const { pid } = await shopProject(page);
    await page.goto(`/projects/${pid}/decisions`);
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps$`));
    await expect(inboxRows(page)).toHaveCount(5);
    const old = encodeURIComponent(PAYMENT_PROVIDER_FINDING);
    await page.goto(`/projects/${pid}/decisions?gap=${old}`);
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps\\?gap=${old}$`));
    await expect(page.getByRole("dialog", { name: PAYMENT_PROVIDER_TITLE })).toBeVisible();
  });

  test("FR-GWEB-04: the noise report tab lists the sections the latest run judged tracking or mixed, with their signals", async ({
    page,
  }) => {
    const { pid } = await shopProject(page);
    await page.goto(`/projects/${pid}/gaps`);
    await expect(inboxRows(page)).toHaveCount(5);
    await page.getByRole("group", { name: "View" }).getByRole("button", { name: "Noise report", exact: true }).click();
    const report = page.getByRole("list", { name: "Noise report" });
    await expect(report.getByTestId("noise-section")).toHaveCount(2);
    const notes = report.getByTestId("noise-section").filter({ hasText: "Meeting notes › 2026-03-02 sync" });
    await expect(notes).toContainText("Tracking");
    await expect(notes).toContainText("By rules · Removed 2 nodes from the checks");
    await expect(notes.getByRole("list", { name: "Signals" }).getByRole("listitem")).toHaveText(["dated heading", "attendee list"]);
    await expect(report.getByTestId("noise-section").filter({ hasText: "Release plan" })).toContainText("Mixed");
    await expect(page.getByTestId("gap-filters")).toHaveCount(0);
    await expect(notes.getByTestId("noise-mark-not-spec")).toBeVisible();
  });

  test("the gap check line shows a running gap run with its step and progress, then when it finished and what it found", async ({
    page,
  }) => {
    const { pid } = await shopProject(page);
    const result = { ...NOISE_RUN.result, counts: { gaps_new: { total: 2 }, gaps_updated: { total: 9 }, close: { resolved: 1 } } };
    await seedGaps(pid, {
      run: {
        ...NOISE_RUN,
        status: "running",
        step: "investigate",
        steps_done: 5,
        steps_total: 9,
        step_items_done: 7,
        step_items_total: 21,
        result,
      },
    });
    await page.goto(`/projects/${pid}/gaps`);
    const line = page.getByTestId("gap-check");
    await expect(line).toHaveAttribute("data-phase", "running");
    await expect(line.getByTestId("gap-check-title")).toHaveText("Looking for gaps");
    await expect(line.getByTestId("gap-check-detail")).toHaveText("Step 6 of 9: Investigating · 7 of 21 sites");
    await expect(line.getByRole("progressbar", { name: "Gap check progress" })).toHaveAttribute("aria-valuenow", "59");

    // The run ends: the line reads again within seconds, says what the run found, and the inbox reads again.
    await seedGaps(pid, { run: { ...NOISE_RUN, steps_done: 9, steps_total: 9, finished_at: "2026-11-17T12:28:46Z", result } });
    await expect(line).toHaveAttribute("data-phase", "done", { timeout: 10_000 });
    await expect(line.getByTestId("gap-check-title")).toHaveText(/^Gaps checked /);
    await expect(line.getByTestId("gap-check-detail")).toHaveText("2 new gaps, 9 updated, 1 resolved");
    await expect(line.getByRole("progressbar")).toHaveCount(0);
  });

  test("FR-GWEB-04: before any finished gap run the noise report says so; with gap runs not configured (503 gap_unavailable) both tabs say Gaps are not available", async ({
    page,
  }) => {
    allowConsoleErrors(page, /status of 503/);
    await login(page);
    const pid = await createViaNew(page, unique("Gaps idle"));
    await pickLocalPdf(page, "orders.pdf");
    await expectUploadReady(page);
    await page.goto(`/projects/${pid}/gaps`);
    await expect(page.getByTestId("gaps-none")).toBeVisible();
    await expect(page.getByTestId("gap-check-title")).toHaveText("No gap check yet");
    await page.getByRole("group", { name: "View" }).getByRole("button", { name: "Noise report", exact: true }).click();
    await expect(page.getByTestId("noise-none").getByRole("heading", { name: "No gap run has finished yet" })).toBeVisible();

    await stubTiming({ gapFail: "gap_unavailable" });
    await page.goto(`/projects/${pid}/gaps`);
    await expect(page.getByTestId("gaps-unavailable").getByRole("heading", { name: "Gaps are not available" })).toBeVisible();
    await expect(page.getByTestId("gap-check")).toHaveCount(0);
    await page.getByRole("group", { name: "View" }).getByRole("button", { name: "Noise report", exact: true }).click();
    await expect(page.getByTestId("gaps-unavailable")).toBeVisible();
  });
});
