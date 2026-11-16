// FR-GWEB-04 (read part), FR-GRTE-06: the Gaps screen on the PRODUCTION routes against the stub API through the real BFF, with shop-corpus
// gaps seeded through the stub's control port (no paid model call): the inbox (held_back=false) and "More found in this run" (held_back=true),
// the filters going to the server, the question card with its recommended option, quotes and ABOUT/IMPACTS chips, the noise report tab, the
// "not available" state of `503 gap_unavailable`, and no answering control anywhere (the read-only screen sends GETs only).
import { randomBytes } from "node:crypto";
import type { Locator, Page } from "@playwright/test";
import {
  allowConsoleErrors,
  bff,
  buildGraphAndPlan,
  calls,
  createViaNew,
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

const REQUIREMENT = "Requirement";
const CANCEL_TITLE = "Cancelling a paid order is not specified";
const node = (specId: string, label: string, name: string) => ({ spec_id: specId, label, name });
const option = (gapId: string, rank: number, label: string, detail: string, recommended = false) => ({
  gap_id: gapId,
  rank,
  label,
  detail,
  recommended,
  basis: [],
  edits: [{ target: { kind: "section" }, text: detail }],
  created_run: "run-7",
});
const gap = (id: string, over: Record<string, unknown>) => ({
  gap_id: id,
  kind: "missing",
  severity: "major",
  status: "open",
  origin: "analysis",
  route: "ask",
  title: "",
  question: "",
  why: "",
  detector: "ST-MISSING",
  signature: `sig-${id}`,
  created_by: "gap-run",
  created_at: "2026-11-10T10:00:00Z",
  updated_at: "2026-11-10T10:00:00Z",
  blocking: false,
  held_back: false,
  about: [],
  impacts: [],
  witness: [],
  options: [],
  clarification: null,
  history: { events: [], follow_ups: [] },
  reach: { stories: [], tasks: [] },
  resolution: null,
  resolved_at: null,
  run_id: "run-7",
  ...over,
});

/** The shop corpus: the cancel-paid-order gap (3 options, A recommended), a blocker contradiction, a person's minor gap, a coding agent's
 *  blocking question, an AI-routed gap (not in the inbox) and one question the run held back (FR-GRTE-06). */
function shopGaps(documentId: string) {
  return [
    gap("GAP::CANCEL_PAID", {
      title: CANCEL_TITLE,
      question: "What happens when a customer cancels an order that is already paid?",
      why: "Checkout and refunds both depend on it: without an answer the refund flow cannot be built.",
      about: [node("REQ::FR::PLACE_ORDER", REQUIREMENT, "Place order"), node("FEAT::CHECKOUT", "Feature", "Checkout")],
      impacts: [node("REQ::FR::REFUND", REQUIREMENT, "Refund a payment")],
      witness: [
        { fragment_id: "FRAG::orders::4", quote: "A customer may cancel an order before it ships.", line_start: 41, line_end: 42 },
        { fragment_id: "FRAG::payments::2", quote: "Payment is captured when the order is placed.", line_start: 12, line_end: 12 },
      ],
      options: [
        option("GAP::CANCEL_PAID", 1, "Refund in full and cancel", "The payment is refunded and the order is cancelled.", true),
        option("GAP::CANCEL_PAID", 2, "Cancel only before shipping", "After shipping the customer must return the goods."),
        option("GAP::CANCEL_PAID", 3, "No cancellation after payment", "A paid order can no longer be cancelled."),
      ],
      reach: { stories: ["STORY::CHECKOUT"], tasks: ["TASK::PAY", "TASK::ORDER_API"] },
      stories: ["STORY::CHECKOUT"],
      documents: [documentId],
    }),
    gap("GAP::TOTAL_ROUNDING", {
      kind: "contradiction",
      severity: "blocker",
      title: "Order totals are rounded differently",
      question: "Are order totals rounded per line or on the total?",
      created_at: "2026-11-09T10:00:00Z",
      about: [node("REQ::FR::ORDER_TOTAL", REQUIREMENT, "Order total")],
    }),
    gap("GAP::DELIVERY_WINDOW", {
      kind: "ambiguous",
      severity: "minor",
      origin: "person",
      title: '"Next day" delivery has no cut-off time',
      question: "Until what time does an order still ship the next day?",
      about: [node("REQ::FR::DELIVERY", REQUIREMENT, "Delivery")],
    }),
    gap("GAP::PAYMENT_PROVIDER", {
      kind: "open_question",
      severity: "major",
      origin: "development",
      blocking: true,
      title: "Which payment provider takes the payment?",
      question: "Which payment provider should the order API call?",
      created_at: "2026-11-01T10:00:00Z",
      about: [node("REQ::FR::PAY", REQUIREMENT, "Take the payment")],
    }),
    gap("GAP::AI_CURRENCY", {
      route: "ai",
      status: "answered",
      severity: "minor",
      title: "Prices are shown in euro",
      question: "Which currency are prices shown in?",
    }),
    gap("GAP::GUEST_CHECKOUT", {
      kind: "assumption",
      severity: "minor",
      held_back: true,
      title: "Guest checkout is assumed",
      question: "May a customer order without an account?",
    }),
  ];
}

const NOISE_RUN = {
  status: "completed",
  run_id: "run-7",
  build_id: "gb-1",
  mode: "full",
  result: {
    run_id: "run-7",
    mode: "full",
    build_id: "gb-1",
    status: "completed",
    counts: {},
    cost: {},
    noise: {
      sections: [
        {
          lineage: "LIN::meeting-notes",
          heading_path: ["Meeting notes", "2026-03-02 sync"],
          relevance: "tracking",
          by: "rules",
          signals: ["dated heading", "attendee list"],
          removed: ["REQ::FR::SYNC_1", "REQ::FR::SYNC_2"],
        },
        {
          lineage: "LIN::release",
          heading_path: ["Release plan"],
          relevance: "mixed",
          by: "model",
          signals: ["status words"],
          removed: [],
        },
      ],
    },
    deferred_sites: [],
    held_tasks: [],
    warnings: [],
  },
};

const screen = (page: Page) => page.getByTestId("screen-gaps");
const inboxRows = (page: Page) => page.getByRole("list", { name: "Gaps" }).getByRole("listitem");
const row = (page: Page, title: string) => inboxRows(page).filter({ hasText: title });

async function pick(page: Page, filter: string, choice: string) {
  await page.getByTestId(`select-${filter}`).getByRole("button").click();
  await page.getByRole("menu", { name: filter }).getByRole("menuitemradio", { name: choice, exact: true }).click();
}

/** No control that would write: answering, own text, preview, placement, dismissal, revert, noise marking, runs or apply. */
async function expectNoWriteControls(scope: Locator) {
  for (const name of [
    /submit/i,
    /^other\b/i,
    /not a gap/i,
    /dismiss/i,
    /revert/i,
    /mark as not spec/i,
    /^apply/i,
    /preview/i,
    /^(start|run)\b/i,
    /^answer\b/i,
  ]) {
    await expect(scope.getByRole("button", { name })).toHaveCount(0);
  }
  await expect(scope.getByRole("textbox")).toHaveCount(0);
}

test.describe("Gaps on production routes", { tag: "@production" }, () => {
  test.setTimeout(120_000);

  test.afterEach(async () => {
    await stubTiming(STUB_TIMING);
  });

  /** A Ready project (one processed document) with the shop gaps seeded; returns its id and the document's. */
  async function shopProject(page: Page, opts: { plan?: boolean } = {}) {
    await login(page);
    const pid = await createViaNew(page, unique("Shop gaps"));
    await pickLocalPdf(page, "orders.pdf");
    await expectUploadReady(page);
    const docs = (await bff(page, "GET", `/projects/${pid}/documents`)).json as { items: { id: string }[] };
    const documentId = docs.items[0].id;
    if (opts.plan === true) await buildGraphAndPlan(page, pid);
    await seedGaps(pid, { gaps: shopGaps(documentId), run: NOISE_RUN });
    return { pid, documentId };
  }

  test("FR-GWEB-04, FR-GRTE-06: the inbox lists the asked gaps (held_back=false), coding-agent gaps that hold tasks first; the held-back question is under 'More found in this run'", async ({
    page,
    requests,
  }) => {
    const { pid } = await shopProject(page);
    const from = requests.length;
    await rail(page).getByRole("button", { name: "Gaps", exact: true }).click();
    await expect(screen(page)).toBeVisible();
    await expect(inboxRows(page)).toHaveCount(4);
    await expect(inboxRows(page).nth(0)).toContainText("Which payment provider takes the payment?");
    await expect(inboxRows(page).nth(1)).toContainText("Order totals are rounded differently");
    await expect(row(page, CANCEL_TITLE)).toContainText("Missing");
    await expect(row(page, CANCEL_TITLE)).toContainText("Major");
    await expect(row(page, CANCEL_TITLE)).toContainText("Found by analysis");
    await expect(row(page, "Order totals are rounded differently")).toContainText("Blocker");
    await expect(inboxRows(page).filter({ hasText: "Prices are shown in euro" })).toHaveCount(0); // routed to AI: not a question for people
    await expect(inboxRows(page).filter({ hasText: "Guest checkout is assumed" })).toHaveCount(0); // held back until the next run
    await expect(page.getByTestId("gap-count")).toHaveText("4 gaps");

    const held = page.getByTestId("gaps-held-back");
    await expect(held.getByRole("heading", { name: "More found in this run" })).toBeVisible();
    await expect(held.getByRole("listitem")).toHaveCount(0);
    await held.getByRole("button", { name: "Show 1 gap" }).click();
    await expect(held.getByRole("list", { name: "More found in this run" }).getByRole("listitem")).toHaveText(/Guest checkout is assumed/);

    const made = calls(requests.slice(from));
    expect(made.map((r) => r.path + r.search)).toContain(`/api/backend/projects/${pid}/gaps?route=ask&held_back=false`);
    expect(made.map((r) => r.path + r.search)).toContain(`/api/backend/projects/${pid}/gaps?route=ask&held_back=true`);
    expect(made.filter((r) => r.method !== "GET")).toEqual([]);
    await expectNoWriteControls(screen(page));
  });

  test("FR-GWEB-04: the filters (status, kind, severity, origin, story, document) go to the server and narrow the inbox", async ({
    page,
    requests,
  }) => {
    const { pid, documentId } = await shopProject(page, { plan: true });
    await page.goto(`/projects/${pid}/gaps`);
    await expect(inboxRows(page)).toHaveCount(4);
    const filters = page.getByTestId("gap-filters");
    for (const name of ["Status", "Kind", "Severity", "Origin", "Story", "Document"])
      await expect(filters.getByText(name, { exact: true })).toBeVisible();

    await pick(page, "Kind", "Contradiction");
    await expect(inboxRows(page)).toHaveCount(1);
    await expect(inboxRows(page)).toContainText("Order totals are rounded differently");
    await pick(page, "Kind", "All");
    await pick(page, "Severity", "Minor");
    await expect(inboxRows(page)).toHaveCount(1);
    await expect(inboxRows(page)).toContainText('"Next day" delivery has no cut-off time');
    await pick(page, "Severity", "All");
    await pick(page, "Origin", "Coding agent");
    await expect(inboxRows(page)).toHaveCount(1);
    await expect(inboxRows(page)).toContainText("Which payment provider takes the payment?");
    await pick(page, "Origin", "All");
    await pick(page, "Story", "Checkout");
    await expect(inboxRows(page)).toHaveCount(1);
    await expect(inboxRows(page)).toContainText(CANCEL_TITLE);
    await pick(page, "Story", "All");
    await pick(page, "Document", "orders.pdf");
    await expect(inboxRows(page)).toHaveCount(1);
    await pick(page, "Document", "All");
    await pick(page, "Status", "Resolved");
    await expect(page.getByTestId("gaps-empty").getByRole("heading", { name: "No gaps match these filters" })).toBeVisible();
    await expect(page.getByTestId("gap-count")).toHaveText("");

    const paths = calls(requests).map((r) => r.path + r.search);
    const base = `/api/backend/projects/${pid}/gaps?`;
    expect(paths).toContain(`${base}kind=contradiction&route=ask&held_back=false`);
    expect(paths).toContain(`${base}severity=minor&route=ask&held_back=false`);
    expect(paths).toContain(`${base}origin=development&route=ask&held_back=false`);
    expect(paths).toContain(`${base}route=ask&story_id=STORY%3A%3ACHECKOUT&held_back=false`);
    expect(paths).toContain(`${base}route=ask&document=${documentId}&held_back=false`);
    expect(paths).toContain(`${base}status=resolved&route=ask&held_back=false`);
  });

  test("FR-GWEB-04: the question card shows the question, why, ABOUT and IMPACTS chips, quotes, reach and the options with A recommended; no answering control", async ({
    page,
    requests,
  }) => {
    const { pid } = await shopProject(page);
    await page.goto(`/projects/${pid}/gaps`);
    const from = requests.length;
    await row(page, CANCEL_TITLE).getByRole("button").click();
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
    await expect(card.getByTestId("gap-quotes")).toContainText("FRAG::orders::4 · lines 41–42");
    await expect(card.getByTestId("gap-reach")).toContainText("1 story, 2 tasks");

    const radios = card
      .getByRole("radiogroup", { name: "What happens when a customer cancels an order that is already paid?" })
      .getByRole("radio");
    await expect(radios).toHaveCount(3);
    await expect(radios.nth(0)).toContainText("Refund in full and cancel");
    await expect(radios.nth(0)).toContainText("Recommended");
    await expect(radios.nth(1)).not.toContainText("Recommended");
    await expect(radios.nth(2)).not.toContainText("Recommended");
    await expect(card.getByTestId("option-recommended")).toHaveCount(1);
    for (let i = 0; i < 3; i++) await expect(radios.nth(i)).toBeDisabled();
    await expectNoWriteControls(card);
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
    await expect(page.getByRole("dialog", { name: "Guest checkout is assumed" })).toBeVisible();
    await expect(
      page.getByRole("dialog").getByTestId("gap-no-options").getByRole("heading", { name: "Options are being prepared" }),
    ).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.goto(`/projects/${pid}/gaps?gap=GAP%3A%3ANOPE`);
    await expect(inboxRows(page)).toHaveCount(4);
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps$`));
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });

  test("FR-GWEB-04: the noise report tab lists the sections the latest run judged tracking or mixed, with their signals", async ({
    page,
  }) => {
    const { pid } = await shopProject(page);
    await page.goto(`/projects/${pid}/gaps`);
    await expect(inboxRows(page)).toHaveCount(4);
    await page.getByRole("group", { name: "View" }).getByRole("button", { name: "Noise report", exact: true }).click();
    const report = page.getByRole("list", { name: "Noise report" });
    await expect(report.getByTestId("noise-section")).toHaveCount(2);
    const notes = report.getByTestId("noise-section").filter({ hasText: "Meeting notes › 2026-03-02 sync" });
    await expect(notes).toContainText("Tracking");
    await expect(notes).toContainText("By rules · Removed 2 nodes from the checks");
    await expect(notes.getByRole("list", { name: "Signals" }).getByRole("listitem")).toHaveText(["dated heading", "attendee list"]);
    await expect(report.getByTestId("noise-section").filter({ hasText: "Release plan" })).toContainText("Mixed");
    await expect(page.getByTestId("gap-filters")).toHaveCount(0);
    await expectNoWriteControls(screen(page));
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
    await page.getByRole("group", { name: "View" }).getByRole("button", { name: "Noise report", exact: true }).click();
    await expect(page.getByTestId("noise-none").getByRole("heading", { name: "No gap run has finished yet" })).toBeVisible();

    await stubTiming({ gapFail: "gap_unavailable" });
    await page.goto(`/projects/${pid}/gaps`);
    await expect(page.getByTestId("gaps-unavailable").getByRole("heading", { name: "Gaps are not available" })).toBeVisible();
    await page.getByRole("group", { name: "View" }).getByRole("button", { name: "Noise report", exact: true }).click();
    await expect(page.getByTestId("gaps-unavailable")).toBeVisible();
  });
});
