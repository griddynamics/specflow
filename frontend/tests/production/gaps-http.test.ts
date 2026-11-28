// FR-GWEB-04, FR-GWEB-06, FR-GRTE-06: the production gaps port over `/projects/{pid}/gaps` and `/projects/{pid}/gap/runs/latest`
// (reads; the writes moved to the agents and the Slack bot, D10), and the pure logic of the former Gaps screen and the Plan's gap links.
// Layer U: the wire parsers and the words. Layer P: the port against a fake `fetch`.
import { test, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import { createHttpGaps, gapQueryString, toGap } from "@/lib/data/http/gaps";
import { DataError, isDataError } from "@/lib/domain/errors";
import type { Gap } from "@/lib/domain/gaps";
import type { Member } from "@/lib/domain/members";
import {
  DEFAULT_GAP_FILTERS,
  NOTHING_STAGED_TOAST,
  STATUS_LABEL,
  STATUS_OPTIONS,
  actorText,
  answerMetaText,
  answerOutcomeText,
  answeredByText,
  isNothingStaged,
  resolveView,
  resolvedToast,
  stagedChoiceText,
  submitLabel,
  assignedOptions,
  assigneeChoices,
  assigneeText,
  clarificationText,
  confidenceText,
  countText,
  eventText,
  failureOf,
  failureText,
  gapsForSpecIds,
  memberNames,
  openGapsText,
  preselectedOption,
  sectionRuleValue,
  unionGaps,
  withdrawable,
  witnessSection,
  gapQuery,
  headingText,
  inboxOrder,
  patchOf,
  quoteSource,
  removedText,
} from "@/components/shared/gaps/gaps-logic";

const PID = "11111111-1111-4111-8111-111111111111";
const T0 = "2026-01-01T10:00:00Z";
const option = (rank: number, label: string, recommended: boolean) => ({
  gap_id: "GAP::1",
  rank,
  label,
  detail: `${label} in detail`,
  recommended,
  basis: [],
  edits: [{}],
  created_run: null,
});
const wireGap = (over: Record<string, unknown> = {}) => ({
  gap_id: "GAP::1",
  kind: "missing",
  severity: "major",
  status: "open",
  origin: "analysis",
  route: "ask",
  title: "Cancelling a paid order is not specified",
  question: "What happens when a customer cancels an order that is already paid?",
  why: "Checkout and refunds depend on it.",
  detector: "ST-MISSING",
  signature: "sig",
  created_by: "gap-run",
  created_at: T0,
  updated_at: T0,
  blocking: false,
  about: [{ spec_id: "REQ::FR::PLACE_ORDER", label: "Requirement", name: "Place order" }],
  impacts: [{ spec_id: "REQ::FR::REFUND", label: "Requirement", name: "Refund" }],
  witness: [{ fragment_id: "FRAG::orders::3", quote: "A customer may cancel an order.", line_start: 12, line_end: 14 }],
  options: [
    option(2, "Cancel only before shipping", false),
    option(1, "Refund in full and cancel", true),
    option(3, "No cancellation", false),
  ],
  clarification: null,
  history: null,
  reach: { stories: ["STORY::CHECKOUT"], tasks: ["TASK::REFUND"] },
  resolution: null,
  resolved_at: null,
  run_id: "run-1",
  ...over,
});
const invalid = (e: unknown) => isDataError(e) && e.code === "invalid_response";

// ---- Layer U ----

test("FR-GWEB-04: toGap reads a gap with its nodes, quotes, options by rank, the recommended one and the reach", () => {
  const g = toGap(wireGap());
  assert.equal(g.id, "GAP::1");
  assert.deepEqual(
    g.options.map((o) => [o.rank, o.recommended]),
    [
      [1, true],
      [2, false],
      [3, false],
    ],
  );
  assert.equal(g.options[0].edits, 1);
  assert.deepEqual(g.about, [{ specId: "REQ::FR::PLACE_ORDER", label: "Requirement", name: "Place order", aliases: [] }]);
  assert.deepEqual(g.witness[0], {
    fragmentId: "FRAG::orders::3",
    quote: "A customer may cancel an order.",
    lineStart: 12,
    lineEnd: 14,
    documentId: null,
    documentTitle: null,
    documentPath: null,
    headingPath: [],
  });
  assert.deepEqual(g.reach, { stories: ["STORY::CHECKOUT"], tasks: ["TASK::REFUND"] });
});

test("FR-GWEB-04: a list item has no options and no reach; a second recommendation never shows; any other shape is invalid_response", () => {
  const item = toGap(wireGap({ options: undefined, reach: undefined, history: undefined, witness: undefined }));
  assert.deepEqual(item.options, []);
  assert.equal(item.reach, null);
  const two = toGap(wireGap({ options: [option(1, "A", true), option(2, "B", true)] }));
  assert.deepEqual(
    two.options.map((o) => o.recommended),
    [true, false],
  );
  assert.throws(() => toGap(wireGap({ kind: "idea" })), invalid);
  assert.throws(() => toGap(wireGap({ about: [{ spec_id: 1 }] })), invalid);
});

test("FR-GWEB-04, FR-GRTE-06: the inbox query asks for the open gaps of route ask, every one a run found (no held_back); list filters repeat", () => {
  assert.equal(gapQueryString(gapQuery(DEFAULT_GAP_FILTERS)), "status=open&route=ask");
  const f = {
    ...DEFAULT_GAP_FILTERS,
    status: "all" as const,
    kind: "missing" as const,
    severity: "blocker" as const,
  };
  assert.equal(
    gapQueryString(gapQuery({ ...f, story: "STORY::A", assigned: "me" })),
    "status=open&status=answered&status=applying&status=resolved&kind=missing&severity=blocker&route=ask&story_id=STORY%3A%3AA&assigned_to=me",
  );
  assert.equal(gapQueryString(gapQuery({ ...DEFAULT_GAP_FILTERS, assigned: "none" })), "status=open&route=ask&assigned_to=none");
  assert.equal(gapQueryString({ ...gapQuery(DEFAULT_GAP_FILTERS), cursor: "c2" }), "status=open&route=ask&cursor=c2");
  // The Plan's read: no route, the open statuses, up to 200.
  assert.equal(
    gapQueryString({ status: ["open", "answered", "applying"], limit: 200 }),
    "status=open&status=answered&status=applying&limit=200",
  );
});

test("FR-GWEB-04: the Status filter offers Open (the default), Staged, Resolved and All; Staged asks for answered and applying", () => {
  assert.deepEqual(
    STATUS_OPTIONS.map((o) => o.label),
    ["Open", "Staged", "Resolved", "All"],
  );
  assert.equal(DEFAULT_GAP_FILTERS.status, "open");
  const q = (status: (typeof DEFAULT_GAP_FILTERS)["status"]) => gapQueryString(gapQuery({ ...DEFAULT_GAP_FILTERS, status }));
  assert.equal(q("staged"), "status=answered&status=applying&route=ask");
  assert.equal(q("resolved"), "status=resolved&route=ask");
  assert.equal(q("all"), "status=open&status=answered&status=applying&status=resolved&route=ask");
  assert.deepEqual(STATUS_LABEL, { open: "Open", answered: "Staged", applying: "Staged", resolved: "Resolved" });
});

test("FR-GWEB-04: a filter value outside its vocabulary is the default", () => {
  assert.deepEqual(patchOf("kind", "idea"), { kind: "all" });
  assert.deepEqual(patchOf("kind", "assumption"), { kind: "assumption" });
  assert.deepEqual(patchOf("status", "nope"), { status: "open" });
  assert.deepEqual(patchOf("status", "answered"), { status: "open" }); // a stored status is not a filter value
  assert.deepEqual(patchOf("status", "staged"), { status: "staged" });
  assert.deepEqual(patchOf("story", ""), { story: "all" });
  assert.deepEqual(patchOf("assigned", ""), { assigned: "all" });
  assert.deepEqual(patchOf("assigned", "u-2"), { assigned: "u-2" });
});

test("FR-GWEB-04: development gaps that hold tasks come first; the rest keep the server's order", () => {
  const g = (id: string, origin: Gap["origin"], blocking: boolean) => ({ ...toGap(wireGap({ gap_id: id, origin, blocking })) });
  assert.deepEqual(
    inboxOrder([g("a", "analysis", true), g("b", "development", false), g("c", "development", true)]).map((x) => x.id),
    ["c", "a", "b"],
  );
});

test("FR-GWEB-04: the words of counts, quotes, answers and noise sections", () => {
  assert.equal(countText(1, false), "1 gap");
  assert.equal(countText(3, true), "3+ gaps");
  assert.equal(quoteSource({ fragmentId: "F", quote: "q", lineStart: 12, lineEnd: 14 }), "F · lines 12–14");
  assert.equal(quoteSource({ fragmentId: "F", quote: "q", lineStart: 3, lineEnd: 3 }), "F · line 3");
  assert.equal(quoteSource({ fragmentId: "F", quote: "q", lineStart: null, lineEnd: null }), "F");
  const titled = { fragmentId: "F", quote: "q", lineStart: 9, lineEnd: 9, documentTitle: "Orders", documentPath: "o.md" };
  assert.equal(quoteSource({ ...titled, headingPath: ["Orders", "Place order"] }), "Orders › Place order · line 9");
  const c = { mode: "option" as const, status: "pending" as const, optionLabel: "Refund", text: null, answeredBy: "dana", answeredAt: "" };
  assert.equal(clarificationText(c), "Refund");
  assert.equal(clarificationText({ ...c, mode: "own", optionLabel: null, text: "Own words" }), "Own words");
  const s = { lineage: "L", headingPath: ["Notes", "Sync"], relevance: "mixed" as const, by: "model" as const, signals: [], removed: [] };
  assert.equal(headingText(s), "Notes › Sync");
  assert.equal(headingText({ ...s, headingPath: [] }), "Top of the document");
  assert.equal(removedText(1), "Removed 1 node from the checks");
  assert.equal(removedText(0), "Removed no nodes");
});

// ---- Layer P ----

const calls: string[] = [];
const BASE = "/api/backend";
function answer(status: number, body: unknown) {
  calls.length = 0;
  mock.method(globalThis, "fetch", (url: string, init?: RequestInit) => {
    calls.push(`${init?.method ?? "GET"} ${url.slice(BASE.length)}`);
    return Promise.resolve(new Response(JSON.stringify(body), { status }));
  });
}
const apiError = (code: string) => ({ error: { code, message: code, details: {} } });
afterEach(() => {
  mock.restoreAll();
});

test("FR-GWEB-04, FR-GRTE-06: list sends the filters and reads the page and its cursor; the next page sends the cursor", async () => {
  answer(200, { gaps: [wireGap({ options: [], reach: null })], next: "c2" });
  const gaps = createHttpGaps();
  const page = await gaps.list(PID, gapQuery(DEFAULT_GAP_FILTERS));
  assert.equal(page.gaps.length, 1);
  assert.equal(page.next, "c2");
  assert.deepEqual(calls, [`GET /projects/${PID}/gaps?status=open&route=ask`]);
  answer(200, { gaps: [wireGap({ gap_id: "GAP::2", options: [], reach: null })], next: null });
  const last = await gaps.list(PID, { ...gapQuery(DEFAULT_GAP_FILTERS), cursor: "c2" });
  assert.deepEqual(
    last.gaps.map((g) => g.id),
    ["GAP::2"],
  );
  assert.equal(last.next, null);
  assert.deepEqual(calls, [`GET /projects/${PID}/gaps?status=open&route=ask&cursor=c2`]);
  answer(200, { gaps: "none" });
  await assert.rejects(createHttpGaps().list(PID, {}), invalid);
});

test("FR-GWEB-04: a list read when gap runs are not configured rejects as unavailable with gap_unavailable", async () => {
  answer(503, apiError("gap_unavailable"));
  let caught: unknown;
  try {
    await createHttpGaps().list(PID, {});
  } catch (e) {
    caught = e;
  }
  assert.ok(isDataError(caught) && caught.code === "unavailable");
  assert.equal(failureOf(caught), "unavailable");
  assert.equal(failureOf(new Error("boom")), "failed");
});

const member = (userId: string, name: string | null, isYou = false): Member => ({
  userId,
  name,
  email: `${userId}@example.com`,
  role: isYou ? "owner" : "collaborator",
  joinedAt: T0,
  isYou,
});

test("FR-GWEB-06: the Assigned filter's options, the assignee menu's choices, the assignee in words and the history's names", () => {
  const members = [member("u-1", "Dana", true), member("u-2", null)];
  assert.deepEqual(
    assignedOptions(members).map((o) => [o.value, o.label]),
    [
      ["all", "All"],
      ["none", "Unassigned"],
      ["u-1", "Dana (you)"],
      ["u-2", "u-2@example.com"],
    ],
  );
  assert.deepEqual(
    assigneeChoices(members, "u-2").map((o) => o.label),
    ["Unassigned", "Dana (you)", "u-2@example.com"],
  );
  assert.equal(assigneeChoices(members, "u-gone").at(-1)?.label, "A former member");
  assert.equal(assigneeText(null, members), "Unassigned");
  assert.equal(assigneeText("u-1", members), "Dana");
  assert.equal(assigneeText("u-gone", members), "A former member");
  const names = memberNames(members);
  const event = { gapId: "GAP::1", clrId: null, at: T0, actor: "u-1", detail: {} };
  assert.equal(
    eventText({ ...event, event: "assigned", detail: { user_id: "u-2", previous: null } }, names),
    "Assigned to u-2@example.com",
  );
  assert.equal(eventText({ ...event, event: "assigned", detail: { user_id: "u-gone" } }, names), "Assigned to a member");
  assert.equal(eventText({ ...event, event: "unassigned", detail: { previous: "u-2" } }, names), "Unassigned");
  assert.equal(actorText("u-1", names), "Dana");
  assert.equal(actorText("u-9", names), "a person");
  assert.equal(actorText("gap-run", names), "Specflow");
  assert.equal(toGap(wireGap()).assignedTo, null);
  assert.equal(toGap(wireGap({ assigned_to: "u-2" })).assignedTo, "u-2");
  assert.throws(() => toGap(wireGap({ assigned_to: 7 })), invalid);
});

test("FR-GWEB-04: toGap reads updated_at, the witness's document and section, the clarification's id, placement, basis and confidence, and the history in time order", () => {
  const g = toGap(
    wireGap({
      witness: [
        {
          fragment_id: "F",
          quote: "q",
          line_start: 3,
          line_end: 3,
          document_id: "doc:1",
          document_title: "Orders service specification",
          document_path: "Local files/orders-spec.md",
          heading_path: ["Orders", "Cancel"],
        },
      ],
      clarification: {
        clr_id: "CLR::1",
        mode: "ai",
        placement: "amend",
        status: "applied",
        option_label: "Refund",
        text: "Refund in full.",
        answered_by: "ai",
        answered_at: "2026-01-02T10:00:00Z",
        applied_build_id: "gb-2",
        basis: [{ fragment_id: "F", quote: "Refunds are full." }],
        confidence: 0.874,
      },
      history: {
        events: [
          {
            gap_id: "GAP::1",
            clr_id: null,
            at: "2026-01-03T10:00:00Z",
            actor: "reconcile",
            event: "reconciled",
            detail: { outcome: "absorbed" },
          },
          { gap_id: "GAP::1", clr_id: null, at: T0, actor: "gap_run", event: "created", detail: {} },
        ],
        follow_ups: [{ gap_id: "GAP::2", reason: "reverted", direction: "continued_by" }],
      },
    }),
  );
  assert.equal(g.updatedAt, T0);
  assert.equal(quoteSource(g.witness[0]), "Orders service specification › Orders › Cancel · line 3");
  const c = g.clarification;
  const h = g.history;
  assert.ok(c !== null && h !== null && h !== undefined);
  assert.equal(c.clrId, "CLR::1");
  assert.equal(c.placement, "amend");
  assert.equal(confidenceText(c.confidence), "87% confident");
  assert.deepEqual(
    h.events.map((e) => e.event),
    ["created", "reconciled"],
  );
  assert.equal(eventText(h.events[1]), "Checked against a new source revision: found in the source file");
  assert.deepEqual(h.followUps, [{ gapId: "GAP::2", reason: "reverted", direction: "continued_by" }]);
  assert.deepEqual(witnessSection(g), { value: "doc:1 › Orders › Cancel", label: "Orders service specification › Orders › Cancel" });
  assert.throws(() => toGap(wireGap({ history: { events: [{ at: 1 }] } })), invalid);
  assert.throws(
    () => toGap(wireGap({ clarification: { answered_by: "a", answered_at: "t", mode: "option", status: "pending", confidence: "high" } })),
    invalid,
  );
});

test("FR-GWEB-04: the recommended option is preselected only for a major gap; the recorded option wins; unstage and withdraw fit pending, applied and absorbed, never while a rebuild runs", () => {
  const major = toGap(wireGap());
  assert.equal(preselectedOption(major), 1);
  assert.equal(preselectedOption({ ...major, severity: "blocker" }), undefined);
  assert.equal(preselectedOption({ ...major, severity: "minor" }), undefined);
  const c = {
    clrId: "C",
    mode: "option" as const,
    status: "pending" as const,
    optionLabel: "No cancellation",
    text: null,
    answeredBy: "u",
    answeredAt: "",
  };
  assert.equal(preselectedOption({ ...major, clarification: c }), 3);
  const staged = { ...major, status: "answered" as const, clarification: c };
  assert.equal(withdrawable(staged), true);
  assert.equal(withdrawable({ ...staged, status: "applying" }), false); // read-only while its rebuild runs
  assert.equal(withdrawable({ ...staged, status: "resolved", clarification: { ...c, status: "applied" } }), true);
  assert.equal(withdrawable({ ...staged, clarification: { ...c, status: "reverted" } }), false);
  assert.equal(withdrawable({ ...staged, clarification: { ...c, clrId: undefined } }), false);
});

test("FR-GWEB-04: an open gap's button stages the answer; a gap a coding agent waits on is answered and resolved at once; a staged one changes its answer", () => {
  const open = toGap(wireGap());
  assert.equal(submitLabel(open), "Stage answer");
  assert.equal(submitLabel({ ...open, origin: "development", blocking: true }), "Answer and resolve");
  assert.equal(submitLabel({ ...open, origin: "development", blocking: false }), "Stage answer");
  assert.equal(submitLabel({ ...open, origin: "analysis", blocking: true }), "Stage answer");
  assert.equal(submitLabel({ ...open, status: "answered" }), "Change answer");
  const result = { gapId: "GAP::1", clrId: "C", replaced: null, placement: "amend" as const, amendments: [], applyAt: null };
  assert.equal(answerOutcomeText({ ...result, immediate: false }), "Staged: it reaches the spec when someone presses Resolve gaps.");
  assert.equal(
    answerOutcomeText({ ...result, applyAt: T0, immediate: true }),
    "Your answer is being applied now: a coding agent waits on it.",
  );
});

test("FR-GWEB-04: a recorded answer says who staged it and what waits, or who answered it and its status; AI answers are staged by AI", () => {
  const g = toGap(wireGap({ status: "answered" }));
  const c = {
    clrId: "C",
    mode: "option" as const,
    status: "pending" as const,
    optionLabel: "Refund",
    text: null,
    answeredBy: "u-1",
    answeredAt: "",
  };
  const names = { "u-1": "Dana" };
  assert.equal(answerMetaText(g, c, names), "Staged by Dana · waits for Resolve gaps");
  assert.equal(answerMetaText({ ...g, status: "applying" }, c, names), "Staged by Dana · being resolved");
  assert.equal(
    answerMetaText({ ...g, status: "resolved" }, { ...c, status: "applied", appliedBuildId: "gb-9" }, names),
    "Answered by Dana · Applied · applied by build gb-9",
  );
  const ai = { ...c, mode: "ai" as const, answeredBy: "ai", confidence: 0.87 };
  assert.equal(answerMetaText(g, ai, names), "Staged by AI · waits for Resolve gaps · 87% confident");
  assert.equal(answeredByText({ ...c, answeredBy: "u-gone" }, names), "a person");
  const event = { gapId: "GAP::1", clrId: "C", at: T0, actor: "u-1", detail: {} };
  assert.deepEqual(
    ["answered", "replaced", "applying", "resolved"].map((name) => eventText({ ...event, event: name })),
    ["Answer staged", "Staged answer replaced", "Resolve started", "Resolved"],
  );
  assert.equal(stagedChoiceText(c), "Refund");
  assert.equal(stagedChoiceText({ ...c, mode: "own", optionLabel: null, text: "Mine" }), "Own answer");
});

test("FR-GWEB-04: Resolve gaps names the staged count; it is off with a reason when nothing is staged or the read failed, and busy while a rebuild runs", () => {
  assert.deepEqual(resolveView(3, false), {
    label: "Resolve 3 gaps",
    state: "ready",
    hint: "Applies every staged answer of the project in one rebuild.",
  });
  assert.equal(resolveView(1, false).label, "Resolve 1 gap");
  assert.deepEqual(resolveView(0, false), { label: "Resolve gaps", state: "off", hint: "Stage an answer first." });
  assert.equal(resolveView(2, false, true).state, "off");
  assert.match(resolveView(2, false, true).hint, /could not be read/);
  const busy = resolveView(2, true);
  assert.equal(busy.label, "Resolving…");
  assert.equal(busy.state, "busy");
  assert.equal(resolveView(0, true).state, "busy");
  assert.deepEqual(resolvedToast({ gapIds: ["A", "B"], clrIds: ["C1", "C2"], amendments: [], build: "gb-7" }), {
    title: "Resolving 2 gaps",
    message: "One rebuild applies the staged answers. The gaps become Resolved when it ends.",
  });
  assert.equal(resolvedToast({ gapIds: ["A"], clrIds: ["C1"], amendments: [], build: null }).title, "Resolving 1 gap");
  assert.equal(NOTHING_STAGED_TOAST.title, "Nothing to resolve");
  const conflict = new DataError("conflict", false, { status: 409, apiCode: "gap_wrong_state" });
  assert.equal(isNothingStaged(conflict), true);
  assert.equal(isNothingStaged(new DataError("conflict", false, { status: 409, apiCode: "gap_stale" })), false);
});

test("FR-GWEB-04: refusals in words: answer_invalid by field, gap_wrong_state by state, gap_stale, noise_rule_not_found", () => {
  const e = (apiCode: string, more: Record<string, string> = {}) => new DataError("validation", false, { status: 400, apiCode, ...more });
  assert.equal(failureText(e("answer_invalid", { field: "own_text", reason: "length" })), "Your answer must be 1 to 4,000 characters.");
  assert.match(failureText(e("answer_invalid", { field: "placement", reason: "contradiction_amends" })), /contradiction is always fixed/);
  assert.match(failureText(e("answer_invalid", { field: "about", reason: "unknown" })), /spec ids/);
  assert.equal(failureText(e("gap_wrong_state", { state: "resolved" })), "This gap is resolved now, so this action no longer applies.");
  assert.equal(failureText(e("gap_wrong_state", { state: "answered" })), "This gap is staged now, so this action no longer applies.");
  assert.equal(
    failureText(e("gap_wrong_state", { state: "applying" })),
    "This gap is being resolved now, so this action no longer applies.",
  );
  assert.equal(failureText(e("gap_stale")), "This gap changed since you opened it.");
  assert.equal(failureText(e("noise_rule_not_found")), "This rule was already removed.");
});

test("FR-GWEB-04: section rule values and the plan's gap links", () => {
  const CHECKOUT = "FEAT::CHECKOUT";
  assert.equal(sectionRuleValue("upload:roadmap", ["Shop roadmap", "Release plan"]), "upload:roadmap › Shop roadmap › Release plan");
  const a = toGap(wireGap({ gap_id: "A", about: [{ spec_id: CHECKOUT, label: "", name: "" }] }));
  const b = toGap(wireGap({ gap_id: "B", about: [{ spec_id: "REQ::X", label: "", name: "" }] }));
  assert.deepEqual(
    gapsForSpecIds([a, b], [CHECKOUT]).map((g) => g.id),
    ["A"],
  );
  // An ABOUT node also meets a task's implemented id through one of its aliases; missing or null aliases read as none.
  const c = toGap(wireGap({ gap_id: "C", about: [{ spec_id: "FEAT::CHECKOUT_FLOW", label: "", name: "", aliases: [CHECKOUT] }] }));
  const d = toGap(wireGap({ gap_id: "D", about: [{ spec_id: "FEAT::SEARCH", label: "", name: "", aliases: null }] }));
  assert.deepEqual(c.about[0].aliases, [CHECKOUT]);
  assert.deepEqual(d.about[0].aliases, []);
  assert.deepEqual(
    gapsForSpecIds([a, b, c, d], [CHECKOUT]).map((g) => g.id),
    ["A", "C"],
  );
  assert.deepEqual(
    gapsForSpecIds([c, d], ["FEAT::CHECKOUT_FLOW", "FEAT::FIND"]).map((g) => g.id),
    ["C"],
  );
  assert.throws(() => toGap(wireGap({ about: [{ spec_id: "X", label: "", name: "", aliases: [1] }] })), invalid);
  assert.deepEqual(
    unionGaps([[a], [a, b]]).map((g) => g.id),
    ["A", "B"],
  );
  assert.equal(openGapsText(1), "1 open gap");
  assert.equal(openGapsText(2), "2 open gaps");
});
