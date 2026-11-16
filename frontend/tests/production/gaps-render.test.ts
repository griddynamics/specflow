// FR-GWEB-04 (read part): the option cards' "Recommended" mark and the gap question card, rendered on the server. OptionCards and QuestionBlock
// without `recommended` render exactly as before (no mark); the card shows the chips, the question with the recommended option, why, the
// ABOUT and IMPACTS nodes, the quotes and the reach, and has no answering control (no Submit, Other, Not a gap or Revert).
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
  about: [{ specId: "REQ::FR::PLACE_ORDER", label: "Requirement", name: "Place order" }],
  impacts: [{ specId: "REQ::FR::REFUND", label: "Requirement", name: "Refund" }],
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
