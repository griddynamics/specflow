// FR-GWEB-04: the option cards' "Recommended" mark and the gap question card, rendered on the server. OptionCards and QuestionBlock
// without `recommended` render exactly as before (no mark); the card shows the chips, the question with the recommended option, why, the
// ABOUT and IMPACTS nodes, the quotes and the reach. Without a card control (read-only use) it has no answering control; with one, an open
// gap has the options as a radio group with "Other", the recommended option preselected for a major gap, Submit and "Not a gap".
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

test("FR-GWEB-04: the read-only card has no answering control: no Submit, Other, Not a gap, Revert or comment", () => {
  const view = renderToStaticMarkup(createElement(M.GapCard, { gap: gap(), onClose: () => undefined }));
  for (const word of ["Submit", "Other", "Not a gap", "Revert", "Add comment", "Mark as not spec", "Apply"]) {
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
  assert.match(view, /Answered by dana/);
  assert.match(view, />Answered</);
});

const never = () => Promise.reject(new Error("not in this test"));
const control = {
  gaps: {
    list: never,
    get: never,
    noise: never,
    check: never,
    documents: never,
    previewEdits: never,
    answer: never,
    dismiss: never,
    raise: never,
    applyNow: never,
    aiAnswers: never,
    revert: never,
    noiseRules: never,
    addNoiseRule: never,
    removeNoiseRule: never,
    answerDocuments: never,
    exportUrl: () => "",
  },
  projectId: "p1",
  href: (id: string) => `/projects/p1/gaps?gap=${encodeURIComponent(id)}`,
  onOpenGap: () => undefined,
  onReload: () => Promise.resolve(),
  onChanged: () => undefined,
  onDismissed: () => undefined,
};

test("FR-GWEB-04: with a control, an open major gap has radio options with A recommended and preselected, Other, Submit and Not a gap", () => {
  const view = renderToStaticMarkup(createElement(M.GapCard, { gap: gap(), onClose: () => undefined, control }));
  assert.match(view, /role="radiogroup"/);
  assert.equal((view.match(/role="radio"/g) ?? []).length, 4); // three options and Other
  assert.equal((view.match(/role="radio"[^>]*disabled/g) ?? []).length, 0);
  assert.match(view, /role="radio" aria-checked="true"[^>]*>[\s\S]*?Refund in full and cancel/);
  assert.match(view, /Other: answer in your own words/);
  assert.match(view, /data-testid="gap-submit"/);
  assert.match(view, /data-testid="gap-dismiss"/);
  assert.match(view, /data-testid="gap-preview-button"/);
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
  assert.match(view, /data-testid="gap-withdraw"/);
  assert.match(view, /applied by build gb-9/);
  assert.match(view, /Checked against a new source revision: found in the source file/);
  assert.match(view, /href="\/projects\/p1\/gaps\?gap=GAP%3A%3A2"/);
  assert.doesNotMatch(view, /\{&quot;outcome/);
});
