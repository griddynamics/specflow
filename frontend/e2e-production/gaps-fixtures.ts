// FR-GWEB-04, FR-GWEB-06: the shop-corpus gaps the Gaps specs seed into the stub API (rows as `GapView` answers them, plus the stub-only
// `stories` filter list) and the latest gap run with its noise report.
export const REQUIREMENT = "Requirement";
export const CANCEL_TITLE = "Cancelling a paid order is not specified";
export const PAYMENT_PROVIDER_TITLE = "Which payment provider takes the payment?";
export const GUEST_TITLE = "Guest checkout is assumed";
/** The old `FND::` id of the coding agent's question, a finding before the gap module (an alias of GAP::PAYMENT_PROVIDER). */
export const PAYMENT_PROVIDER_FINDING = "FND::PAYMENT_PROVIDER";
/** An `ABOUT` / `IMPACTS` node; `aliases` (its other spec ids) only when given, as an older API sends none. */
export const node = (specId: string, label: string, name: string, aliases?: string[]) => ({
  spec_id: specId,
  label,
  name,
  ...(aliases === undefined ? {} : { aliases }),
});
export const option = (gapId: string, rank: number, label: string, detail: string, recommended = false) => ({
  gap_id: gapId,
  rank,
  label,
  detail,
  recommended,
  basis: [],
  edits: [
    {
      placement: "amend",
      target: { fragment_id: "FRAG::orders::4", anchor_quote: "A customer may cancel an order before it ships." },
      operation: "insert_after",
      text: detail,
    },
  ],
  created_run: "run-7",
});

/** The stub's caller (`ME` of tests/support/stub-members.mjs), the owner of every project a spec creates. */
export const ME_ID = "11111111-1111-4111-8111-111111111111";
/** The corpus lineage of the orders specification. */
export const ORDERS_LINEAGE = "doc:ORDERS";
const ORDERS_WITNESS = {
  fragment_id: "FRAG::orders::4",
  quote: "A customer may cancel an order before it ships.",
  line_start: 41,
  line_end: 42,
  document_id: ORDERS_LINEAGE,
  document_title: "Orders service specification",
  document_path: "Local files/orders-spec.md",
  heading_path: ["Orders", "Cancel an order"],
};
export const gap = (id: string, over: Record<string, unknown>) => ({
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

/** The shop corpus: the cancel-paid-order gap (3 options, A recommended; its first witness names its document and section), a blocker
 *  contradiction, a person's minor gap, a coding agent's blocking question (a migrated finding: its old FND id resolves to it), a gap AI
 *  answered (resolved, its answer in the clarifications document; not in the inbox), a resolved gap whose own answer amends the orders
 *  specification, and a minor assumption. */
export function shopGaps() {
  return [
    gap("GAP::CANCEL_PAID", {
      title: CANCEL_TITLE,
      question: "What happens when a customer cancels an order that is already paid?",
      why: "Checkout and refunds both depend on it: without an answer the refund flow cannot be built.",
      about: [node("REQ::FR::PLACE_ORDER", REQUIREMENT, "Place order"), node("FEAT::CHECKOUT", "Feature", "Checkout")],
      impacts: [node("REQ::FR::REFUND", REQUIREMENT, "Refund a payment")],
      witness: [
        ORDERS_WITNESS,
        { fragment_id: "FRAG::payments::2", quote: "Payment is captured when the order is placed.", line_start: 12, line_end: 12 },
      ],
      options: [
        option("GAP::CANCEL_PAID", 1, "Refund in full and cancel", "The payment is refunded and the order is cancelled.", true),
        option("GAP::CANCEL_PAID", 2, "Cancel only before shipping", "After shipping the customer must return the goods."),
        option("GAP::CANCEL_PAID", 3, "No cancellation after payment", "A paid order can no longer be cancelled."),
      ],
      reach: { stories: ["STORY::CHECKOUT"], tasks: ["TASK::PAY", "TASK::ORDER_API"] },
      stories: ["STORY::CHECKOUT"],
    }),
    gap("GAP::TOTAL_ROUNDING", {
      kind: "contradiction",
      severity: "blocker",
      title: "Order totals are rounded differently",
      question: "Are order totals rounded per line or on the total?",
      assigned_to: ME_ID,
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
      title: PAYMENT_PROVIDER_TITLE,
      question: "Which payment provider should the order API call?",
      created_at: "2026-11-01T10:00:00Z",
      about: [node("REQ::FR::PAY", REQUIREMENT, "Take the payment")],
      old_ids: [PAYMENT_PROVIDER_FINDING], // stub-only: a migrated finding's old id, which `GET /gaps/{id}` resolves
    }),
    gap("GAP::AI_CURRENCY", {
      route: "ai",
      status: "resolved",
      resolution: "ai_answered",
      severity: "minor",
      title: "Prices are shown in euro",
      question: "Which currency are prices shown in?",
      about: [node("REQ::FR::PRICES", REQUIREMENT, "Show prices")],
      clarification: {
        clr_id: "CLR::AI_CURRENCY",
        mode: "ai",
        placement: "document",
        status: "applied",
        option_label: "Euro everywhere",
        text: "All prices are shown in euro.",
        answered_by: "ai",
        answered_at: "2026-11-10T11:00:00Z",
        applied_build_id: "gb-1",
        basis: [{ fragment_id: "FRAG::payments::5", quote: "All amounts are charged in EUR." }],
        confidence: 0.87,
      },
    }),
    gap("GAP::RETURN_WINDOW", {
      status: "resolved",
      resolution: "answered",
      severity: "minor",
      title: "The return window is not stated",
      question: "How many days may a customer return goods?",
      witness: [ORDERS_WITNESS],
      clarification: {
        clr_id: "CLR::RETURN_WINDOW",
        mode: "own",
        placement: "amend",
        status: "applied",
        option_label: null,
        text: "Goods may be returned within 30 days.",
        answered_by: "dana",
        answered_at: "2026-11-09T11:00:00Z",
        applied_build_id: "gb-1",
        basis: [],
        confidence: null,
      },
      history: {
        events: [
          { gap_id: "GAP::RETURN_WINDOW", clr_id: null, at: "2026-11-08T10:00:00Z", actor: "gap_run", event: "created", detail: {} },
          {
            gap_id: "GAP::RETURN_WINDOW",
            clr_id: "CLR::RETURN_WINDOW",
            at: "2026-11-09T11:00:00Z",
            actor: "u-dana",
            event: "answered",
            detail: { mode: "own" },
          },
          {
            gap_id: "GAP::RETURN_WINDOW",
            clr_id: "CLR::RETURN_WINDOW",
            at: "2026-11-09T12:00:00Z",
            actor: "reconcile",
            event: "reconciled",
            detail: { outcome: "reanchored", similarity: 0.93 },
          },
        ],
        follow_ups: [],
      },
    }),
    gap("GAP::GUEST_CHECKOUT", {
      kind: "assumption",
      severity: "minor",
      title: GUEST_TITLE,
      question: "May a customer order without an account?",
    }),
  ];
}

/** `n` more open questions for people from one run, newer than the shop gaps (minor, so they sort before the shop's minor gaps). */
export function foundGaps(n: number) {
  return Array.from({ length: n }, (_, i) => {
    const k = String(i + 1).padStart(2, "0");
    return gap(`GAP::FOUND_${k}`, {
      kind: "open_question",
      severity: "minor",
      title: `Found question ${k}`,
      question: `What does requirement ${k} leave open?`,
      created_at: "2026-11-11T10:00:00Z",
    });
  });
}

/** When the running gap run below started: after the shop gaps and the found gaps were made, before the gaps it records. */
export const RUN_STARTED = "2026-11-12T09:00:00Z";
/** A gap run investigating its sites and recording their gaps (step 6 of 7, 3 of 12 sites recorded). */
export const RUNNING_RUN = {
  status: "running",
  run_id: "run-8",
  mode: "full",
  step: "investigate",
  steps_done: 5,
  steps_total: 7,
  step_items_done: 3,
  step_items_total: 12,
  started_at: RUN_STARTED,
  result: null,
};

/** The `n`th gap the running run records (1..59): a major open question for people, made after the run started. */
export function recordedGap(n: number) {
  const at = `2026-11-12T09:${String(n).padStart(2, "0")}:00Z`;
  return gap(`GAP::RECORDED_${String(n)}`, {
    kind: "open_question",
    title: `Recorded question ${String(n)}`,
    question: `What does site ${String(n)} of the run leave open?`,
    created_at: at,
    updated_at: at,
    run_id: "run-8",
  });
}

export const NOISE_RUN = {
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

/** The running run, finished. */
export const FINISHED_RUN = {
  ...RUNNING_RUN,
  status: "completed",
  step: "finish",
  steps_done: 7,
  step_items_done: null,
  step_items_total: null,
  finished_at: "2026-11-12T09:20:00Z",
  result: { ...NOISE_RUN.result, run_id: "run-8" },
};
