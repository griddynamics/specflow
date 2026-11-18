// FR-GWEB-04: the option cards' "Recommended" mark, the gap question card and "Resolve gaps", rendered on the server. OptionCards and
// QuestionBlock without `recommended` render exactly as before (no mark); the card shows the chips, the question with the recommended
// option, why, the ABOUT and IMPACTS nodes, the quotes and the reach. Without a card control (read-only use) it has no answering control;
// with one, an open gap has the options as a radio group with "Other", the recommended option preselected for a major gap, "Stage
// answer" (or "Answer and resolve" for a gap a coding agent waits on) and "Not a gap"; a staged gap "Change answer" and "Unstage"; a gap
// whose rebuild runs is read-only with "Resolving…". The "Resolve gaps" button names the staged count, is off or busy with its reason,
// and its confirmation lists each staged gap's answer and who staged it.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";
import type { Gap } from "@/lib/domain/gaps";

stubCss();
let M: Awaited<ReturnType<typeof load>>;
async function load() {
  return {
    OptionCards: (await import("../../components/ui/OptionCards")).OptionCards,
    QuestionBlock: (await import("../../components/ui/QuestionBlock")).QuestionBlock,
    GapCard: (await import("../../components/gaps/GapCard")).GapCard,
    AssigneeMenu: (await import("../../components/gaps/AssigneeMenu")).AssigneeMenu,
    DecisionRow: (await import("../../components/ui/DecisionRow")).DecisionRow,
    ResolveButton: (await import("../../components/gaps/ResolveGaps")).ResolveButton,
    ResolveDialog: (await import("../../components/gaps/ResolveGaps")).ResolveDialog,
    resolveView: (await import("../../components/gaps/gaps-logic")).resolveView,
  };
}
before(async () => {
  M = await load();
});

const options = [
  { value: "1", label: "Refund in full and cancel" },
  { value: "2", label: "Cancel only before shipping" },
];

const gap = (over: Partial<Gap> = {}): Gap => ({
  id: "GAP::1",
  kind: "missing",
  severity: "major",
  status: "open",
  origin: "analysis",
  route: "ask",
  title: "Cancelling a paid order is not specified",
  question: "What happens when a customer cancels an order that is already paid?",
  why: "Checkout and refunds depend on it.",
  blocking: false,
  heldBack: false,
  createdAt: "2026-01-01T10:00:00Z",
  updatedAt: "2026-01-01T10:00:00Z",
  about: [{ specId: "REQ::FR::PLACE_ORDER", label: "Requirement", name: "Place order", aliases: [] }],
  impacts: [{ specId: "REQ::FR::REFUND", label: "Requirement", name: "Refund", aliases: [] }],
  witness: [{ fragmentId: "FRAG::orders::3", quote: "A customer may cancel an order.", lineStart: 12, lineEnd: 14 }],
  options: [
    { rank: 1, label: "Refund in full and cancel", detail: "", recommended: true, edits: 1 },
    { rank: 2, label: "Cancel only before shipping", detail: "", recommended: false, edits: 1 },
    { rank: 3, label: "No cancellation after payment", detail: "", recommended: false, edits: 1 },
  ],
  clarification: null,
  reach: { stories: ["STORY::CHECKOUT"], tasks: ["TASK::REFUND"] },
  assignedTo: null,
  ...over,
});

test("FR-GWEB-04: OptionCards without `recommended` render no mark, the same markup as before", () => {
  const view = renderToStaticMarkup(createElement(M.OptionCards, { label: "Q", options }));
  assert.doesNotMatch(view, /Recommended/);
  assert.doesNotMatch(view, /gdg-oc__rec/);
  const utils = renderToStaticMarkup(createElement(M.QuestionBlock, { question: "Q", options, allowOther: true }));
  assert.doesNotMatch(utils, /Recommended/);
  assert.match(utils, /Add comment/);
});

test("FR-GWEB-04: OptionCards mark exactly the recommended option, inside its radio (part of its accessible name)", () => {
  const view = renderToStaticMarkup(createElement(M.OptionCards, { label: "Q", options, recommended: "1" }));
  assert.equal((view.match(/data-testid="option-recommended"/g) ?? []).length, 1);
  assert.match(
    view,
    /Refund in full and cancel<\/span><span class="gdg-oc__rec"[^>]*><span class="gdg-tag gdg-tag--accent"[^>]*>Recommended/,
  );
  const utils = renderToStaticMarkup(createElement(M.QuestionBlock, { question: "Q", options, readOnly: true, recommended: "2" }));
  assert.match(utils, /Cancel only before shipping<\/span><span class="gdg-oc__rec"/);
});

test("FR-GWEB-04: the question card shows chips, the question with one recommended option, why, nodes, quotes and reach", () => {
  const view = renderToStaticMarkup(createElement(M.GapCard, { gap: gap(), onClose: () => undefined }));
  assert.match(view, /Cancelling a paid order is not specified/);
  assert.match(view, />Major</);
  assert.match(view, />Missing</);
  assert.match(view, /Found by analysis/);
  assert.match(view, /What happens when a customer cancels an order that is already paid\?/);
  assert.equal((view.match(/data-testid="option-recommended"/g) ?? []).length, 1);
  assert.match(view, /Checkout and refunds depend on it\./);
  assert.match(view, /data-testid="gap-about"[\s\S]*REQ::FR::PLACE_ORDER/);
  assert.match(view, /data-testid="gap-impacts"[\s\S]*REQ::FR::REFUND/);
  assert.match(view, /A customer may cancel an order\./);
  assert.match(view, /FRAG::orders::3 · lines 12–14/);
  assert.match(view, /1 story, 1 task/);
  assert.equal((view.match(/role="radio"[^>]*disabled/g) ?? []).length, 3);
});

test("FR-GWEB-04: the read-only card has no answering control: no Stage answer, Other, Not a gap, Unstage, Revert or comment", () => {
  const view = renderToStaticMarkup(createElement(M.GapCard, { gap: gap(), onClose: () => undefined }));
  for (const word of ["Stage answer", "Other", "Not a gap", "Unstage", "Revert", "Add comment", "Mark as not spec", "Resolve gaps"]) {
    assert.doesNotMatch(view, new RegExp(`>${word}<`), word);
  }
  assert.doesNotMatch(view, /<textarea/);
  assert.match(view, />Close</);
});

test("FR-GWEB-04: a gap without options says they are being prepared; an answered one shows the recorded answer chosen", () => {
  assert.match(
    renderToStaticMarkup(createElement(M.GapCard, { gap: gap({ options: [] }), onClose: () => undefined })),
    /Options are being prepared/,
  );
  const answered = gap({
    status: "answered",
    clarification: {
      mode: "option",
      status: "pending",
      optionLabel: "Cancel only before shipping",
      text: null,
      answeredBy: "dana",
      answeredAt: "",
    },
  });
  const view = renderToStaticMarkup(createElement(M.GapCard, { gap: answered, onClose: () => undefined }));
  assert.match(view, /aria-checked="true"[^>]*>[\s\S]*?Cancel only before shipping/);
  assert.match(view, />Staged answer</);
  assert.match(view, /Staged by a person · waits for Resolve gaps/);
  assert.match(view, /data-testid="gap-status"><span class="gdg-tag gdg-tag--info"[^>]*>.*?Staged</);
});

const never = () => Promise.reject(new Error("not in this test"));
const control = {
  gaps: {
    list: never,
    get: never,
    noise: never,
    check: never,
    previewEdits: never,
    answer: never,
    dismiss: never,
    assign: never,
    raise: never,
    applyNow: never,
    aiAnswers: never,
    revert: never,
    noiseRules: never,
    addNoiseRule: never,
    removeNoiseRule: never,
  },
  projectId: "p1",
  members: [
    { userId: "u-1", name: "Dana", email: "dana@example.com", role: "owner" as const, joinedAt: "", isYou: true },
    { userId: "u-2", name: null, email: "lee@example.com", role: "collaborator" as const, joinedAt: "", isYou: false },
  ],
  href: (id: string) => `/projects/p1/gaps?gap=${encodeURIComponent(id)}`,
  onOpenGap: () => undefined,
  onReload: () => Promise.resolve(),
  onChanged: () => undefined,
  onDismissed: () => undefined,
};

test("FR-GWEB-04: with a control, an open major gap has radio options with A recommended and preselected, Other, Stage answer and Not a gap", () => {
  const view = renderToStaticMarkup(createElement(M.GapCard, { gap: gap(), onClose: () => undefined, control }));
  assert.match(view, /role="radiogroup"/);
  assert.equal((view.match(/role="radio"/g) ?? []).length, 4); // three options and Other
  assert.equal((view.match(/role="radio"[^>]*disabled/g) ?? []).length, 0);
  assert.match(view, /role="radio" aria-checked="true"[^>]*>[\s\S]*?Refund in full and cancel/);
  assert.match(view, /Other: answer in your own words/);
  assert.match(view, /data-testid="gap-submit"[^>]*>Stage answer</);
  assert.match(view, /data-testid="gap-dismiss"/);
  assert.match(view, /data-testid="gap-preview-button"/);
  assert.doesNotMatch(view, /data-testid="gap-at-once"/);
  assert.doesNotMatch(view, /data-testid="gap-withdraw"/);
});

test("FR-GWEB-04: an open development gap that holds a coding agent's task is answered and resolved at once, and says so", () => {
  const view = renderToStaticMarkup(
    createElement(M.GapCard, { gap: gap({ origin: "development", blocking: true }), onClose: () => undefined, control }),
  );
  assert.match(view, /data-testid="gap-submit"[^>]*>Answer and resolve</);
  const line = /<p class="gap-text body-sm" id="([^"]+)" data-testid="gap-at-once">([^<]+)</.exec(view);
  assert.ok(line, "the line under the answer");
  assert.match(line[2], /applies at once because a coding agent waits on it/);
  assert.match(view, new RegExp(`aria-describedby="${line[1]}" data-testid="gap-submit"[^>]*>Answer and resolve<`));
});

const pending = {
  clrId: "CLR::1",
  mode: "option" as const,
  status: "pending" as const,
  optionLabel: "Cancel only before shipping",
  text: null,
  answeredBy: "u-1",
  answeredAt: "",
};

test("FR-GWEB-04: a staged gap shows its staged answer and who staged it, Change answer and Unstage, and no Not a gap", () => {
  const view = renderToStaticMarkup(
    createElement(M.GapCard, { gap: gap({ status: "answered", clarification: pending }), onClose: () => undefined, control }),
  );
  assert.match(view, /Staged by Dana · waits for Resolve gaps/);
  assert.match(view, /data-testid="gap-submit"[^>]*>Change answer</);
  assert.match(view, /data-testid="gap-withdraw"[^>]*>Unstage</);
  assert.doesNotMatch(view, /data-testid="gap-dismiss"/);
  assert.doesNotMatch(view, /data-testid="gap-resolving"/);
});

test("FR-GWEB-04: a gap whose rebuild runs is Staged with Resolving… (a spinner and the word) and read-only", () => {
  const view = renderToStaticMarkup(
    createElement(M.GapCard, { gap: gap({ status: "applying", clarification: pending }), onClose: () => undefined, control }),
  );
  assert.match(view, /data-testid="gap-status"><span class="gdg-tag gdg-tag--info"[^>]*>.*?Staged</);
  assert.match(
    view,
    /data-testid="gap-resolving"><span class="gdg-tag gdg-tag--pending"[^>]*><span class="gdg-tag__spin"[^>]*><\/span>Resolving…</,
  );
  assert.match(view, /Staged by Dana · being resolved/);
  for (const id of ["gap-submit", "gap-withdraw", "gap-dismiss", "gap-answer-form"]) {
    assert.doesNotMatch(view, new RegExp(`data-testid="${id}"`), id);
  }
  assert.equal((view.match(/role="radio"[^>]*disabled/g) ?? []).length, 3);
});

test("FR-GWEB-04: a minor gap preselects nothing; a resolved gap is read-only and offers Withdraw answer for its applied answer", () => {
  const utils = renderToStaticMarkup(createElement(M.GapCard, { gap: gap({ severity: "minor" }), onClose: () => undefined, control }));
  assert.doesNotMatch(utils, /aria-checked="true"/);
  const resolved = gap({
    status: "resolved",
    clarification: {
      clrId: "CLR::1",
      mode: "option",
      status: "applied",
      optionLabel: "Refund in full and cancel",
      text: null,
      answeredBy: "dana",
      answeredAt: "",
      appliedBuildId: "gb-9",
    },
    history: {
      events: [
        {
          gapId: "GAP::1",
          clrId: null,
          at: "2026-01-02T10:00:00Z",
          actor: "reconcile",
          event: "reconciled",
          detail: { outcome: "absorbed" },
        },
      ],
      followUps: [{ gapId: "GAP::2", reason: "reverted", direction: "continued_by" }],
    },
  });
  const view = renderToStaticMarkup(createElement(M.GapCard, { gap: resolved, onClose: () => undefined, control }));
  assert.doesNotMatch(view, /data-testid="gap-submit"/);
  assert.doesNotMatch(view, /data-testid="gap-dismiss"/);
  assert.match(view, /data-testid="gap-withdraw"[^>]*>Withdraw answer</);
  assert.match(view, /data-testid="gap-status"><span class="gdg-tag gdg-tag--success"[^>]*>.*?Resolved</);
  assert.match(view, /applied by build gb-9/);
  assert.match(view, /Checked against a new source revision: found in the source file/);
  assert.match(view, /href="\/projects\/p1\/gaps\?gap=GAP%3A%3A2"/);
  assert.doesNotMatch(view, /\{&quot;outcome/);
});

test("FR-GWEB-06: the card has no assignee control, open or resolved, with or without a control", () => {
  for (const g of [gap({ assignedTo: "u-2" }), gap({ status: "resolved", assignedTo: "u-1" })]) {
    const view = renderToStaticMarkup(createElement(M.GapCard, { gap: g, onClose: () => undefined, control }));
    assert.doesNotMatch(view, /data-testid="gap-assignee"/);
    assert.doesNotMatch(view, /Assigned to/);
  }
});

const assign = { people: control.members, assign: () => Promise.resolve() };

test("FR-GWEB-06: an open gap's row cell is a menu trigger with the assignee's avatar and name, beside the row's button", () => {
  const menu = createElement(M.AssigneeMenu, { gap: gap({ assignedTo: "u-2" }), control: assign });
  const view = renderToStaticMarkup(
    createElement(M.DecisionRow, { title: "Cancelling a paid order", domain: "Missing", severity: "high", aside: menu }),
  );
  const parts = view.split('<div class="gdg-drow__aside">');
  assert.equal(parts.length, 2, "the menu sits beside the row's button");
  const [row, aside] = parts;
  assert.doesNotMatch(row, /aria-haspopup/); // nothing interactive inside the row's button but the button itself
  assert.match(aside, /aria-haspopup="menu"/);
  assert.match(aside, /aria-label="Assigned: lee@example\.com\. Change the assignee"/);
  assert.match(aside, />lee@example\.com</);
});

test("FR-GWEB-06: an unassigned gap's row cell says Unassigned and opens the same menu", () => {
  const view = renderToStaticMarkup(createElement(M.AssigneeMenu, { gap: gap(), control: assign }));
  assert.match(view, /aria-label="Assigned: Unassigned\. Change the assignee"/);
  assert.match(view, />Unassigned</);
});

test("FR-GWEB-06: a resolved gap's row names its assignee without a menu", () => {
  const view = renderToStaticMarkup(
    createElement(M.AssigneeMenu, { gap: gap({ status: "resolved", assignedTo: "u-1" }), control: assign }),
  );
  assert.doesNotMatch(view, /aria-haspopup/);
  assert.match(view, />Dana</);
});

test("FR-GWEB-04: an inbox row of a gap whose rebuild runs carries the Resolving… tag beside its title", () => {
  const badge = createElement("span", { "data-testid": "gap-row-resolving" }, "Resolving…");
  const view = renderToStaticMarkup(
    createElement(M.DecisionRow, { title: "Cancelling a paid order", domain: "Missing", severity: "high", badge }),
  );
  assert.match(
    view,
    /<span class="gdg-drow__line"><b>Cancelling a paid order<\/b><span data-testid="gap-row-resolving">Resolving…<\/span><\/span>/,
  );
  const utils = renderToStaticMarkup(
    createElement(M.DecisionRow, { title: "Cancelling a paid order", domain: "Missing", severity: "high" }),
  );
  assert.doesNotMatch(utils, /gdg-drow__line/);
});

const resolveButton = (staged: number, resolving: boolean) =>
  renderToStaticMarkup(createElement(M.ResolveButton, { view: M.resolveView(staged, resolving), onOpen: () => undefined }));

test("FR-GWEB-04: Resolve gaps names the staged count and is the primary button when answers are staged", () => {
  const view = resolveButton(3, false);
  assert.match(view, /class="gdg-btn gdg-btn--primary gaps-resolve"/);
  assert.match(view, /data-state="ready"[^>]*>Resolve 3 gaps</);
  assert.doesNotMatch(view, /aria-disabled/);
});

test("FR-GWEB-04: with nothing staged Resolve gaps is aria-disabled with its reason as tooltip and description", () => {
  const view = resolveButton(0, false);
  assert.match(view, /aria-disabled="true"/);
  assert.match(view, /data-tip="Stage an answer first\."/);
  const hint = /aria-describedby="([^"]+)"/.exec(view);
  assert.ok(hint);
  assert.match(view, new RegExp(`id="${hint[1]}" class="shell-sr" data-testid="gaps-resolve-hint">Stage an answer first\\.<`));
  assert.match(view, /data-state="off"[^>]*>Resolve gaps</);
});

test("FR-GWEB-04: while a rebuild runs Resolve gaps is busy: aria-disabled, a spinner and Resolving…", () => {
  const view = resolveButton(2, true);
  assert.match(view, /aria-disabled="true"/);
  assert.match(view, /gaps-resolve--busy/);
  assert.match(view, /<span class="gdg-spinner gaps-resolve__spin" aria-hidden="true"><\/span>Resolving…/);
});

test("FR-GWEB-04: the Resolve gaps confirmation lists each staged gap, its answer and who staged it, says it resolves everyone's, and counts the rest", () => {
  const staged = [
    gap({ id: "GAP::1", status: "answered", clarification: pending }),
    gap({
      id: "GAP::2",
      title: "Prices are shown in euro",
      status: "answered",
      clarification: { ...pending, clrId: "CLR::2", mode: "ai", optionLabel: null, text: "All in euro.", answeredBy: "ai" },
    }),
    gap({
      id: "GAP::3",
      title: "Return window",
      status: "answered",
      clarification: { ...pending, mode: "own", optionLabel: null, text: "30 days", answeredBy: "u-2" },
    }),
  ];
  const dialog = (rows: Gap[]) =>
    renderToStaticMarkup(
      createElement(M.ResolveDialog, {
        gaps: control.gaps,
        projectId: "p1",
        staged: rows,
        names: { "u-1": "Dana", "u-2": "lee@example.com" },
        onClose: () => undefined,
        onResolved: () => undefined,
      }),
    );
  const view = dialog(staged);
  assert.match(view, />Resolve 3 gaps\?</);
  assert.match(view, /One rebuild applies every staged answer in this project, not only yours\./);
  const items = view.split('data-testid="gaps-resolve-item"').slice(1);
  assert.equal(items.length, 3);
  assert.match(items[0], /Cancelling a paid order is not specified<\/b>.*Cancel only before shipping · Staged by Dana/);
  assert.match(items[1], /Prices are shown in euro<\/b>.*Own answer · Staged by AI/);
  assert.match(items[2], /Return window<\/b>.*Own answer · Staged by lee@example\.com/);
  assert.match(view, /data-testid="gaps-resolve-confirm"[^>]*>Resolve 3 gaps</);
  assert.doesNotMatch(view, /gaps-resolve-more/);
  const many = Array.from({ length: 53 }, (_, i) => gap({ id: `GAP::${String(i)}`, status: "answered", clarification: pending }));
  const utils = dialog(many);
  assert.equal(utils.split('data-testid="gaps-resolve-item"').length - 1, 50);
  assert.match(utils, /data-testid="gaps-resolve-more">and 3 more</);
});
