// FR-GWEB-04, FR-GWEB-05: the shop-corpus gaps the Gaps specs seed into the stub API (rows as `GapView` answers them, plus the stub-only
// `stories` and `documents` filter lists), the latest gap run with its noise report, and the corpus documents of the Documents tab.
export const REQUIREMENT = "Requirement";
export const CANCEL_TITLE = "Cancelling a paid order is not specified";
export const node = (specId: string, label: string, name: string) => ({ spec_id: specId, label, name });
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

/** The corpus lineage of the orders specification, and its document as the Documents tab lists it. */
export const ORDERS_LINEAGE = "doc:ORDERS";
export const ORDERS_DOC_ID = "4b1d0c1e-0000-4000-8000-0000000000a1";
export const CORPUS = [
  {
    document_id: ORDERS_DOC_ID,
    lineage: ORDERS_LINEAGE,
    path: "Local files/orders-spec.md",
    title: "Orders service specification",
    markdown: "# Orders service specification\n\n## Cancel an order\n\nA customer may cancel an order before it ships.\n",
  },
  {
    document_id: "4b1d0c1e-0000-4000-8000-0000000000a2",
    lineage: "doc:PAYMENTS",
    path: "Local files/payments-spec.md",
    title: "Payments specification",
    markdown: "# Payments specification\n\nPayment is captured when the order is placed.\n",
  },
];
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

/** The shop corpus: the cancel-paid-order gap (3 options, A recommended; its first witness names its document and section), a blocker
 *  contradiction, a person's minor gap, a coding agent's blocking question, a gap AI answered (resolved, its answer in the clarifications
 *  document; not in the inbox), a resolved gap whose own answer amends the orders specification, and one question the run held back
 *  (FR-GRTE-06). */
export function shopGaps(documentId: string) {
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
        edits: [
          {
            placement: "document",
            target: { heading: "Prices are shown in euro" },
            operation: "append_section",
            text: "All prices are shown in euro.",
          },
        ],
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
        edits: [
          {
            placement: "amend",
            target: { fragment_id: "FRAG::orders::4", anchor_quote: "A customer may cancel an order before it ships." },
            operation: "insert_after",
            text: "Goods may be returned within 30 days.",
          },
        ],
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
      held_back: true,
      title: "Guest checkout is assumed",
      question: "May a customer order without an account?",
    }),
  ];
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
