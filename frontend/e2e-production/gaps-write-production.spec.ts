// FR-GWEB-04, FR-GWEB-06: the interactive Gaps screen on the PRODUCTION routes against the stub API through the real BFF, with the shop-corpus
// gaps seeded through the stub's control port (no paid model call). The question card answers (keyboard radios, the recommended option
// preselected for a major gap, an own answer with its edits preview and a placement switch, Submit with the gap's updated_at, a stale
// answer and Reload, refusals in words), "Not a gap" with a reason (and the "not spec" section rule), "Apply now", the "Answered by AI"
// review list with Revert and the follow-up link, the history, "Raise a gap", the noise rules ("Mark as not spec", add, remove), assigning
// a gap to a member from the menu in its row (the Assigned column and filter) and the Plan's links to the gap cards.
import { randomBytes } from "node:crypto";
import type { Page, Request } from "@playwright/test";
import { colourViolations, tabWalk } from "./audits";
import { CANCEL_TITLE, gap, node, NOISE_RUN, shopGaps } from "./gaps-fixtures";
import {
  allowConsoleErrors,
  bff,
  buildGraphAndPlan,
  createViaNew,
  expect,
  expectUploadReady,
  login,
  pickLocalPdf,
  seedGaps,
  setTheme,
  settleGaps,
  STUB_TIMING,
  stubTiming,
  test,
} from "./support";

const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
const inboxRows = (page: Page) => page.getByRole("list", { name: "Gaps" }).getByRole("listitem");
const card = (page: Page, title: string) => page.getByRole("dialog", { name: title });
const cardLink = (pid: string, gapId: string) => `/projects/${pid}/gaps?gap=${encodeURIComponent(gapId)}`;
const CANCEL_ID = "GAP::CANCEL_PAID";
const RETURN_TITLE = "The return window is not stated";
const CHECKED = "aria-checked";
const SUBMIT = "gap-submit";
const OWN = "Refund the payment in full, then cancel the order.";

/** A Ready project (one processed document) with the shop gaps and the noise run seeded; returns its id. */
async function shopProject(page: Page, opts: { plan?: boolean } = {}): Promise<string> {
  await login(page);
  const pid = await createViaNew(page, unique("Shop gaps"));
  await pickLocalPdf(page, "orders.pdf");
  await expectUploadReady(page);
  if (opts.plan === true) await buildGraphAndPlan(page, pid);
  await seedGaps(pid, { gaps: shopGaps(), run: NOISE_RUN });
  return pid;
}

async function pick(page: Page, scope: Page | ReturnType<Page["getByRole"]>, select: string, choice: string) {
  await scope.getByTestId(`select-${select}`).getByRole("button").click();
  await page.getByRole("menu", { name: select }).getByRole("menuitemradio", { name: choice, exact: true }).click();
}

const body = (r: Request): Record<string, unknown> => JSON.parse(r.postData() ?? "{}") as Record<string, unknown>;
const isPost = (pid: string, tail: string) => (r: Request) =>
  r.method() === "POST" && r.url().endsWith(`/api/backend/projects/${pid}${tail}`);
const CANCEL = "/gaps/GAP%3A%3ACANCEL_PAID";
const isPut = (pid: string, tail: string) => (r: Request) =>
  r.method() === "PUT" && r.url().endsWith(`/api/backend/projects/${pid}${tail}`);
const ASSIGNEE = "gap-assignee";

test.describe("Gaps: answering and the other writes on production routes", { tag: "@production" }, () => {
  test.setTimeout(120_000);

  test.afterEach(async () => {
    await stubTiming(STUB_TIMING);
  });

  test("FR-GWEB-04: the options are a keyboard radio group; an own answer gets an edits preview, the placement switch asks again, Submit sends the edits and updated_at and says when it applies; Apply now applies it", async ({
    page,
  }) => {
    allowConsoleErrors(page, /status of 409/); // the second "Apply now": nothing waits
    const pid = await shopProject(page);
    await page.goto(cardLink(pid, CANCEL_ID));
    const c = card(page, CANCEL_TITLE);
    const radios = c.getByRole("radiogroup", { name: /cancels an order that is already paid/ }).getByRole("radio");
    await expect(radios.nth(0)).toHaveAttribute(CHECKED, "true");
    await radios.nth(0).focus();
    await page.keyboard.press("ArrowDown");
    await expect(radios.nth(1)).toHaveAttribute(CHECKED, "true");
    await expect(radios.nth(1)).toBeFocused();
    await page.keyboard.press("ArrowUp");
    await expect(radios.nth(0)).toHaveAttribute(CHECKED, "true");

    await radios.nth(3).click(); // Other
    await c.getByTestId("option-other-text").fill(OWN);
    await expect(c.getByTestId(SUBMIT)).toBeDisabled(); // an own answer is previewed first
    const firstPreview = page.waitForRequest(isPost(pid, `${CANCEL}/edits_preview`));
    await c.getByTestId("gap-preview-button").click();
    expect(body(await firstPreview)).toEqual({ own_text: OWN });
    const preview = c.getByTestId("gap-preview");
    await expect(preview).toHaveAttribute("data-placement", "amend");
    await expect(preview.getByTestId("gap-preview-before")).toHaveText("A customer may cancel an order before it ships.");
    await expect(preview.getByTestId("gap-preview-after")).toContainText(OWN);
    const placement = c.getByRole("radiogroup", { name: "Where the answer goes" });
    await expect(placement.getByRole("radio", { name: /Amend the source section/ })).toHaveAttribute(CHECKED, "true");
    const switched = page.waitForRequest(isPost(pid, `${CANCEL}/edits_preview`));
    await placement.getByRole("radio", { name: /Clarifications document/ }).click();
    expect(body(await switched)).toEqual({ own_text: OWN, placement: "document" });
    await expect(preview).toHaveAttribute("data-placement", "document");

    const read = (await bff(page, "GET", `/projects/${pid}${CANCEL}`)).json as { updated_at: string };
    const answer = page.waitForRequest(isPost(pid, `${CANCEL}/answer`));
    await c.getByTestId(SUBMIT).click();
    const sent = body(await answer);
    expect(sent).toMatchObject({ own_text: OWN, placement: "document", updated_at: read.updated_at });
    expect(sent.edits).toEqual([{ placement: "document", target: { heading: CANCEL_TITLE }, operation: "append_section", text: OWN }]);
    await expect(c.getByTestId("gap-answer-result")).toContainText("Your answer is recorded. It will be applied at");
    await expect(c.getByTestId("gap-status")).toHaveText("Answered");
    await expect(c.getByTestId("gap-answer")).toContainText(OWN);
    await c.getByRole("button", { name: "Close" }).last().click();

    await expect(inboxRows(page).filter({ hasText: CANCEL_TITLE })).toContainText("Answered");
    await page.getByTestId("gaps-apply-now").click();
    await expect(page.getByTestId("gaps-apply-result")).toHaveText(/^1 answer is being applied \(build gb-apply-\d+\)\./);
    await page.getByTestId("gaps-apply-now").click();
    await expect(page.getByTestId("gaps-apply-result")).toHaveText("No answers are waiting to be applied.");

    await settleGaps(pid);
    await page.goto(cardLink(pid, CANCEL_ID));
    await expect(c.getByTestId("gap-status")).toHaveText("Resolved");
    await expect(c.getByTestId("gap-answer")).toContainText("Applied");
    await expect(c.getByTestId(SUBMIT)).toHaveCount(0);
    await expect(c.getByTestId("gap-withdraw")).toBeVisible();
  });

  test("FR-GWEB-04: an answer to a gap that changed since it was read says so; Reload re-reads it and keeps the choice; the second Submit replaces the answer", async ({
    page,
  }) => {
    allowConsoleErrors(page, /status of 409/);
    const pid = await shopProject(page);
    await page.goto(cardLink(pid, CANCEL_ID));
    const c = card(page, CANCEL_TITLE);
    await expect(c.getByRole("radio", { name: /Refund in full and cancel/ })).toHaveAttribute(CHECKED, "true");
    // Another person answers in another tab.
    const read = (await bff(page, "GET", `/projects/${pid}${CANCEL}`)).json as { updated_at: string };
    expect((await bff(page, "POST", `/projects/${pid}${CANCEL}/answer`, { option_rank: 2, updated_at: read.updated_at })).status).toBe(200);

    await c.getByTestId(SUBMIT).click();
    await expect(c.getByTestId("gap-stale")).toContainText("This gap changed since you opened it.");
    await c.getByTestId("gap-stale-reload").click();
    await expect(c.getByTestId("gap-stale")).toHaveCount(0);
    await expect(c.getByTestId("gap-status")).toHaveText("Answered");
    await expect(c.getByTestId("gap-answer")).toContainText("Cancel only before shipping");
    await expect(c.getByRole("radio", { name: /Refund in full and cancel/ })).toHaveAttribute(CHECKED, "true"); // kept
    await c.getByTestId(SUBMIT).click();
    await expect(c.getByTestId("gap-answer-result")).toContainText("Your answer is recorded");
    await expect(c.getByTestId("gap-answer")).toContainText("Refund in full and cancel");
  });

  test("FR-GWEB-04: refusals are shown in words (answer_invalid by field, gap_wrong_state); a contradiction is always amended in its source", async ({
    page,
  }) => {
    allowConsoleErrors(page, /status of (400|409)/);
    const pid = await shopProject(page);
    await page.goto(cardLink(pid, CANCEL_ID));
    const c = card(page, CANCEL_TITLE);
    const envelope = (code: string, details: Record<string, unknown>) => JSON.stringify({ error: { code, message: code, details } });
    await page.route(`**/api/backend/projects/${pid}${CANCEL}/answer`, (route) =>
      route.fulfill({
        status: 400,
        contentType: "application/json",
        body: envelope("answer_invalid", { field: "edits", reason: "edit_anchor" }),
      }),
    );
    await c.getByTestId(SUBMIT).click();
    await expect(c.getByTestId("gap-answer-error")).toHaveText(
      "The proposed edits no longer fit the source. Preview them again, or choose the clarifications document.",
    );
    await page.unroute(`**/api/backend/projects/${pid}${CANCEL}/answer`);
    await page.route(`**/api/backend/projects/${pid}${CANCEL}/answer`, (route) =>
      route.fulfill({
        status: 409,
        contentType: "application/json",
        body: envelope("gap_wrong_state", { status: "resolved", allowed: ["open", "answered"] }),
      }),
    );
    await c.getByTestId(SUBMIT).click();
    await expect(c.getByTestId("gap-answer-error")).toHaveText("This gap is resolved now, so this action no longer applies.");
    await page.unroute(`**/api/backend/projects/${pid}${CANCEL}/answer`);

    await page.goto(cardLink(pid, "GAP::TOTAL_ROUNDING"));
    const t = card(page, "Order totals are rounded differently");
    await t.getByRole("radio", { name: /^B?\s*Other/ }).click();
    await t.getByTestId("option-other-text").fill("Round on the total only.");
    await t.getByTestId("gap-preview-button").click();
    await expect(t.getByTestId("gap-preview")).toBeVisible();
    await expect(t.getByTestId("gap-placement-fixed")).toContainText("a contradiction is always fixed in its source section");
    await expect(t.getByRole("radiogroup", { name: "Where the answer goes" })).toHaveCount(0);
  });

  test("FR-GWEB-04: 'Not a gap' with the reason 'Not spec' dismisses the gap and also marks its witness section as not spec", async ({
    page,
  }) => {
    const pid = await shopProject(page);
    await page.goto(`/projects/${pid}/gaps`);
    await expect(inboxRows(page)).toHaveCount(4);
    await inboxRows(page).filter({ hasText: CANCEL_TITLE }).getByRole("button").first().click();
    const c = card(page, CANCEL_TITLE);
    await c.getByTestId("gap-dismiss").click();
    const d = page.getByRole("dialog", { name: "Not a gap" });
    await expect(d.getByTestId("gap-dismiss-confirm")).toBeDisabled();
    await d.getByRole("radio", { name: "Not spec" }).click();
    const offer = d.getByTestId("gap-dismiss-noise-rule");
    await expect(offer).toContainText("Also mark the section Orders service specification › Orders › Cancel an order as not spec");
    await expect(offer.getByRole("radio", { name: "Yes, mark it" })).toHaveAttribute(CHECKED, "true");
    const dismissed = page.waitForRequest(isPost(pid, `${CANCEL}/dismiss`));
    const rule = page.waitForRequest(isPost(pid, "/gaps/noise_rules"));
    await d.getByTestId("gap-dismiss-confirm").click();
    expect(body(await dismissed)).toEqual({ reason: "not spec" });
    expect(body(await rule)).toEqual({ scope: "section", value: "doc:ORDERS › Orders › Cancel an order", reason: "not spec" });
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(inboxRows(page)).toHaveCount(3);
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps$`));
    await page.getByTestId("gaps-tab-noise").click();
    await expect(page.getByTestId("noise-rule").filter({ hasText: "doc:ORDERS › Orders › Cancel an order" })).toHaveCount(1);
  });

  test("FR-GWEB-04: the 'Answered by AI' tab lists the answer with its basis, confidence and build; Revert opens a follow-up gap, linked from the row and in the card's history", async ({
    page,
  }) => {
    const pid = await shopProject(page);
    await page.goto(`/projects/${pid}/gaps`);
    await page.getByTestId("gaps-tab-ai").click();
    const row = page.getByTestId("ai-answer-row").filter({ hasText: "Prices are shown in euro" });
    await expect(row.getByTestId("ai-answer-text")).toHaveText("Euro everywhere");
    await expect(row.getByTestId("ai-answer-meta")).toContainText("87% confident · Applied by build gb-1");
    await expect(row.getByTestId("ai-answer-basis")).toContainText("All amounts are charged in EUR.");
    await row.getByTestId("ai-revert").click();
    const d = page.getByRole("dialog", { name: "Revert AI answer" });
    await expect(d.getByTestId("revert-confirm")).toBeDisabled();
    await d.getByTestId("revert-reason").fill("Prices are shown in the customer's currency.");
    await d.getByTestId("revert-confirm").click();
    await expect(page.getByTestId("ai-revert-result")).toContainText("A follow-up gap is open for a person");
    await expect(row).toContainText("Reverted");
    await expect(row.getByTestId("ai-revert")).toHaveCount(0);
    const follow = row.getByTestId("ai-follow-up-link");
    await expect(follow).toHaveText(/^GAP::AI_CURRENCY-R\d+$/);
    const followId = (await follow.textContent()) ?? "";
    await expect(follow).toHaveAttribute("href", cardLink(pid, followId));
    await follow.click();
    const c = card(page, "Prices are shown in euro");
    await expect(c).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`\\?gap=${encodeURIComponent(followId)}$`));
    await expect(c.getByTestId("gap-follow-ups")).toContainText("Follows up GAP::AI_CURRENCY: the answer was withdrawn");
    await expect(c.getByTestId("gap-answer-form")).toBeVisible(); // a person answers it now
    await expect(c.getByRole("radio", { name: /Other/ })).toBeEnabled();
  });

  test("FR-GWEB-04: the card's History lists the events in time order in words, with the reconcile outcome", async ({ page }) => {
    const pid = await shopProject(page);
    await page.goto(cardLink(pid, "GAP::RETURN_WINDOW"));
    const c = card(page, RETURN_TITLE);
    const events = c.getByTestId("gap-history-event");
    await expect(c.getByTestId("gap-history")).toBeHidden();
    await c.getByTestId("gap-history-toggle").click();
    await expect(c.getByTestId("gap-history-toggle")).toHaveAttribute("aria-expanded", "true");
    await expect(events).toHaveCount(3);
    await expect(events.nth(0)).toContainText("Gap found");
    await expect(events.nth(0)).toContainText("Specflow");
    await expect(events.nth(1)).toContainText("Answered");
    await expect(events.nth(1)).toContainText("a person");
    await expect(events.nth(2)).toContainText("Checked against a new source revision: moved to a changed line");
    await expect(c.getByTestId("gap-history")).not.toContainText("{");
  });

  test("FR-GWEB-04: 'Raise a gap' checks its fields, shows a refused field in words and opens the new gap's card", async ({ page }) => {
    allowConsoleErrors(page, /status of 400/);
    const pid = await shopProject(page);
    await page.goto(`/projects/${pid}/gaps`);
    await expect(inboxRows(page)).toHaveCount(4);
    await page.getByTestId("gaps-raise").click();
    const d = page.getByRole("dialog", { name: "Raise a gap" });
    await d.getByTestId("raise-question").fill("Refunds?");
    await d.getByTestId("raise-submit").click();
    await expect(d.getByText("The question must be 10 to 1,000 characters.")).toBeVisible();
    await d.getByTestId("raise-question").fill("Who may cancel an order after it has shipped?");
    await d.getByTestId("raise-about").fill("UNKNOWN::X");
    await d.getByTestId("raise-submit").click();
    await expect(d.getByText("Name 1 to 20 spec ids that exist in the graph.")).toBeVisible();
    await expect(d.getByTestId("raise-about")).toHaveAttribute("aria-invalid", "true");
    await d.getByTestId("raise-about").fill("REQ::FR::PLACE_ORDER, FEAT::CHECKOUT");
    await d.getByTestId("select-Kind of gap").getByRole("button").click();
    await page.getByRole("menu", { name: "Kind of gap" }).getByRole("menuitemradio", { name: "Open question" }).click();
    const raised = page.waitForRequest(isPost(pid, "/gaps"));
    await d.getByTestId("raise-submit").click();
    expect(body(await raised)).toEqual({
      question: "Who may cancel an order after it has shipped?",
      kind: "open_question",
      about: ["REQ::FR::PLACE_ORDER", "FEAT::CHECKOUT"],
    });
    const c = card(page, "Who may cancel an order after it has shipped?");
    await expect(c).toBeVisible();
    await expect(c.getByTestId("gap-meta")).toContainText("Raised by a person");
    await expect(page).toHaveURL(/\?gap=GAP%3A%3APERSON\d+$/);
    await page.keyboard.press("Escape");
    await expect(inboxRows(page)).toHaveCount(5);
  });

  test("FR-GWEB-04: the noise report marks a section as not spec and lists, adds and removes noise rules", async ({ page }) => {
    const pid = await shopProject(page);
    await page.goto(`/projects/${pid}/gaps`);
    await page.getByTestId("gaps-tab-noise").click();
    const notes = page.getByTestId("noise-section").filter({ hasText: "Meeting notes › 2026-03-02 sync" });
    await expect(page.getByTestId("noise-rules-empty")).toBeVisible();
    await notes.getByTestId("noise-mark-not-spec").click();
    await expect(notes.getByTestId("noise-marked")).toHaveText("Marked as not spec");
    const rules = page.getByTestId("noise-rule");
    await expect(rules).toHaveCount(1);
    await expect(rules).toContainText("LIN::meeting-notes › Meeting notes › 2026-03-02 sync");

    const form = page.getByTestId("noise-rule-add-form");
    await form.getByRole("radio", { name: "Pattern" }).click();
    await form.getByTestId("noise-rule-value").fill("^Sprint \\d+");
    await expect(form.getByTestId("noise-rule-add")).toBeDisabled(); // a reason is required
    await form.getByTestId("noise-rule-reason").fill("Sprint notes are tracking");
    await form.getByTestId("noise-rule-add").click();
    await expect(rules).toHaveCount(2);
    await expect(rules.nth(1)).toContainText("Pattern");
    await expect(rules.nth(1)).toContainText("Sprint notes are tracking");
    await expect(form.getByTestId("noise-rule-value")).toHaveValue("");

    await rules.filter({ hasText: "LIN::meeting-notes" }).getByTestId("noise-rule-remove").click();
    await expect(rules).toHaveCount(1);
    await expect(notes.getByTestId("noise-mark-not-spec")).toBeVisible();
  });

  test("FR-GWEB-06: a member assigns a gap in its row from a menu of Unassigned and the members, never on its card; the history, the Assigned column and the Assigned filter show it", async ({
    page,
  }) => {
    const pid = await shopProject(page);
    const added = await bff(page, "POST", `/projects/${pid}/members`, { email: "bob.brown@example.test" });
    expect(added.status).toBe(201);
    const bob = (added.json as { member: { user_id: string } }).member.user_id;
    const toAssignee = isPut(pid, `${CANCEL}/assignee`);

    await page.goto(`/projects/${pid}/gaps`);
    const cancel = inboxRows(page).filter({ hasText: CANCEL_TITLE });
    await expect(cancel.getByTestId(ASSIGNEE)).toContainText("Unassigned");
    await cancel.getByTestId(ASSIGNEE).click();
    const menu = page.getByRole("menu", { name: "Assign to" });
    const items = menu.getByRole("menuitemradio");
    await expect(items).toHaveCount(3);
    await expect(items.nth(0)).toHaveText("Unassigned");
    await expect(items.nth(0)).toHaveAttribute(CHECKED, "true");
    await expect(items.nth(1)).toContainText("dev (you)");
    await expect(items.nth(2)).toContainText("Bob Brown");
    const assigned = page.waitForRequest(toAssignee);
    await items.nth(2).click();
    expect(body(await assigned)).toEqual({ user_id: bob });
    await expect(page.getByRole("dialog")).toHaveCount(0); // assigning in the row never opens the card
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps$`));
    await expect(cancel.getByTestId(ASSIGNEE)).toContainText("Bob Brown");

    await cancel.getByRole("button").first().click();
    const c = card(page, CANCEL_TITLE);
    await expect(c).toBeVisible();
    await expect(c.getByTestId(ASSIGNEE)).toHaveCount(0); // the card has no assignee control
    await c.getByTestId("gap-history-toggle").click();
    await expect(c.getByTestId("gap-history-event").filter({ hasText: "Assigned to Bob Brown" })).toContainText("dev");
    await c.getByRole("button", { name: "Close" }).last().click();

    await pick(page, page, "Assigned", "Bob Brown");
    await expect(inboxRows(page)).toHaveCount(1);
    await expect(inboxRows(page)).toContainText(CANCEL_TITLE);
    await pick(page, page, "Assigned", "Unassigned");
    await expect(inboxRows(page).filter({ hasText: CANCEL_TITLE })).toHaveCount(0);
    await pick(page, page, "Assigned", "All");

    const cleared = page.waitForRequest(toAssignee);
    await cancel.getByTestId(ASSIGNEE).click();
    await page.getByRole("menu", { name: "Assign to" }).getByRole("menuitemradio").nth(0).click();
    expect(body(await cleared)).toEqual({ user_id: null });
    await expect(cancel.getByTestId(ASSIGNEE)).toContainText("Unassigned");

    // The last row's menu reaches past the list's edge and over no other row: its last choice is the element under the pointer.
    await inboxRows(page).last().getByTestId(ASSIGNEE).click();
    await page.getByRole("menu", { name: "Assign to" }).getByRole("menuitemradio").last().click({ trial: true });
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu", { name: "Assign to" })).toHaveCount(0);
  });

  test("FR-GWEB-04: the Plan's stories and tasks show their open gaps and link each to its card", async ({ page }) => {
    const pid = await shopProject(page, { plan: true });
    await page.goto(`/projects/${pid}/plan`);
    await expect(page.getByTestId("screen-plan")).toHaveAttribute("data-stage", "ready", { timeout: 15_000 });
    const story = page
      .getByTestId("plan-story")
      .filter({ has: page.getByTestId("plan-story-name").getByText("Checkout", { exact: true }) });
    await expect(story.getByTestId("plan-story-gaps")).toHaveText("1 open gap");
    const pay = page.getByTestId("plan-task").filter({ hasText: "Take the payment" });
    await expect(pay.getByTestId("plan-task-gaps")).toHaveText("1 open gap");
    await pay.getByTestId("plan-task-head").click();
    const link = pay.getByTestId("plan-task-gap-link");
    await expect(link).toHaveText(CANCEL_TITLE);
    await expect(link).toHaveAttribute("href", cardLink(pid, CANCEL_ID));
    await link.click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/gaps\\?gap=GAP%3A%3ACANCEL_PAID$`));
    await expect(card(page, CANCEL_TITLE)).toBeVisible();
  });

  test("FR-GWEB-04: a task's gap is found through an alias of its ABOUT node; a node without a matching id or alias links nothing", async ({
    page,
  }) => {
    const pid = await shopProject(page, { plan: true });
    const ALIAS_TITLE = "The checkout flow has no timeout";
    await seedGaps(pid, {
      gaps: [
        gap("GAP::ALIAS", {
          title: ALIAS_TITLE,
          question: "How long may a checkout stay open?",
          about: [node("FEAT::CHECKOUT_FLOW", "Feature", "Checkout flow", ["FEAT::CHECKOUT"])],
        }),
        gap("GAP::ELSEWHERE", {
          title: "Search has no ranking",
          question: "How are search results ranked?",
          about: [node("FEAT::SEARCH", "Feature", "Search", ["FEAT::FIND"])],
        }),
      ],
    });
    await page.goto(`/projects/${pid}/plan`);
    await expect(page.getByTestId("screen-plan")).toHaveAttribute("data-stage", "ready", { timeout: 15_000 });
    const pay = page.getByTestId("plan-task").filter({ hasText: "Take the payment" });
    await expect(pay.getByTestId("plan-task-gaps")).toHaveText("1 open gap");
    await pay.getByTestId("plan-task-head").click();
    const link = pay.getByTestId("plan-task-gap-link");
    await expect(link).toHaveText(ALIAS_TITLE);
    await expect(link).toHaveAttribute("href", cardLink(pid, "GAP::ALIAS"));
  });

  test("FR-GWEB-04: a failed gap read leaves the Plan as it is (no gap tags, no error)", async ({ page }) => {
    allowConsoleErrors(page, /status of 503/);
    const pid = await shopProject(page, { plan: true });
    await stubTiming({ gapFail: "gap_unavailable" });
    await page.goto(`/projects/${pid}/plan`);
    await expect(page.getByTestId("screen-plan")).toHaveAttribute("data-stage", "ready", { timeout: 15_000 });
    await expect(page.getByTestId("plan-task").first()).toBeVisible();
    await expect(page.getByTestId("plan-story-gaps")).toHaveCount(0);
    await expect(page.getByTestId("plan-task-gaps")).toHaveCount(0);
  });

  for (const theme of ["light", "dark"] as const) {
    test(`FR-GWEB-04, FE-DS-01.AC2, FE-DOD-05.AC1 [answer card, ${theme}]: token colours only and every Tab stop shows the 2px focus ring`, async ({
      page,
    }) => {
      await setTheme(page, theme);
      const pid = await shopProject(page);
      await page.goto(cardLink(pid, CANCEL_ID));
      const c = card(page, CANCEL_TITLE);
      await c.getByRole("radio", { name: /Other/ }).click();
      await c.getByTestId("option-other-text").fill(OWN);
      await c.getByTestId("gap-preview-button").click();
      await expect(c.getByTestId("gap-preview")).toBeVisible();
      await page.mouse.move(0, 0); // no hover transition mid-way while the colours are read
      await expect.poll(async () => (await colourViolations(page)).bad, { message: "colours that are not a token value" }).toEqual([]);
      const stops = await tabWalk(page);
      expect(stops.length).toBeGreaterThanOrEqual(5);
      expect(stops.filter((s) => s.ring === "none").map((s) => `${s.id} "${s.label}"`)).toEqual([]);
      expect(stops.filter((s) => !s.visible).map((s) => `${s.id} "${s.label}"`)).toEqual([]);
    });
  }
});
