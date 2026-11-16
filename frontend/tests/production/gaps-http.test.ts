// FR-GWEB-04 (read part), FR-GRTE-06: the production gaps port over `/projects/{pid}/gaps*`, `/projects/{pid}/gap/runs/latest` and the
// documents list, and the pure logic of the Gaps screen. Layer U: the wire parsers and the words. Layer P: the port against a fake `fetch`.
import { test, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import { createHttpGaps, gapQueryString, toGap, toNoise } from "@/lib/data/http/gaps";
import { isDataError } from "@/lib/domain/errors";
import type { Gap } from "@/lib/domain/gaps";
import {
  DEFAULT_GAP_FILTERS,
  clarificationText,
  countText,
  failureOf,
  gapQuery,
  headingText,
  inboxOrder,
  patchOf,
  quoteSource,
  removedText,
} from "@/components/gaps/gaps-logic";

const PID = "11111111-1111-4111-8111-111111111111";
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
  created_at: "2026-01-01T10:00:00Z",
  updated_at: "2026-01-01T10:00:00Z",
  blocking: false,
  held_back: false,
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
  assert.deepEqual(g.about, [{ specId: "REQ::FR::PLACE_ORDER", label: "Requirement", name: "Place order" }]);
  assert.deepEqual(g.witness[0], { fragmentId: "FRAG::orders::3", quote: "A customer may cancel an order.", lineStart: 12, lineEnd: 14 });
  assert.deepEqual(g.reach, { stories: ["STORY::CHECKOUT"], tasks: ["TASK::REFUND"] });
  assert.equal(g.heldBack, false);
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
  assert.throws(() => toGap(wireGap({ held_back: "yes" })), invalid);
});

test("FR-GWEB-04: toNoise reads the latest finished run's sections; before any finished run there is none", () => {
  const n = toNoise({
    status: "completed",
    run_id: "run-1",
    result: {
      run_id: "run-1",
      mode: "full",
      status: "completed",
      noise: {
        sections: [
          {
            lineage: "L1",
            heading_path: ["Meeting notes"],
            relevance: "tracking",
            by: "rules",
            signals: ["dated heading"],
            removed: ["A", "B"],
          },
        ],
      },
    },
  });
  assert.equal(n.runId, "run-1");
  assert.equal(n.sections[0].relevance, "tracking");
  assert.deepEqual(n.sections[0].removed, ["A", "B"]);
  assert.deepEqual(toNoise({ status: "idle", result: null }), { status: "idle", runId: null, sections: [] });
  assert.throws(() => toNoise({ status: "sleeping" }), invalid);
});

test("FR-GWEB-04, FR-GRTE-06: the inbox query asks for route ask and held_back=false; the held-back view for held_back=true; list filters repeat", () => {
  assert.equal(gapQueryString(gapQuery(DEFAULT_GAP_FILTERS, false)), "route=ask&held_back=false");
  assert.equal(gapQueryString(gapQuery(DEFAULT_GAP_FILTERS, true)), "route=ask&held_back=true");
  const f = {
    ...DEFAULT_GAP_FILTERS,
    status: "all" as const,
    kind: "missing" as const,
    severity: "blocker" as const,
    origin: "development" as const,
  };
  assert.equal(
    gapQueryString(gapQuery({ ...f, story: "STORY::A", document: "doc-1" }, false)),
    "status=open&status=answered&status=applying&status=resolved&kind=missing&severity=blocker&origin=development&route=ask&story_id=STORY%3A%3AA&document=doc-1&held_back=false",
  );
  assert.equal(gapQueryString({ heldBack: false, cursor: "c2" }), "held_back=false&cursor=c2");
});

test("FR-GWEB-04: a filter value outside its vocabulary is the default", () => {
  assert.deepEqual(patchOf("kind", "idea"), { kind: "all" });
  assert.deepEqual(patchOf("kind", "assumption"), { kind: "assumption" });
  assert.deepEqual(patchOf("status", "nope"), { status: "active" });
  assert.deepEqual(patchOf("story", ""), { story: "all" });
  assert.deepEqual(patchOf("document", "doc-1"), { document: "doc-1" });
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

test("FR-GWEB-04, FR-GRTE-06: list sends the filters with held_back and reads the page and its cursor", async () => {
  answer(200, { gaps: [wireGap({ options: [], reach: null })], next: "c2" });
  const page = await createHttpGaps().list(PID, gapQuery(DEFAULT_GAP_FILTERS, false));
  assert.equal(page.gaps.length, 1);
  assert.equal(page.next, "c2");
  assert.deepEqual(calls, [`GET /projects/${PID}/gaps?route=ask&held_back=false`]);
  answer(200, { gaps: "none" });
  await assert.rejects(createHttpGaps().list(PID, {}), invalid);
});

test("FR-GWEB-04: get reads one gap; an unknown gap is not_found; gap runs not configured reject as unavailable with gap_unavailable", async () => {
  answer(200, wireGap());
  const gaps = createHttpGaps();
  assert.equal((await gaps.get(PID, "GAP::1")).options.length, 3);
  assert.deepEqual(calls, [`GET /projects/${PID}/gaps/GAP%3A%3A1`]);
  answer(404, apiError("gap_not_found"));
  await assert.rejects(gaps.get(PID, "GAP::9"), (e) => isDataError(e) && e.code === "not_found");
  answer(503, apiError("gap_unavailable"));
  let caught: unknown;
  try {
    await gaps.list(PID, {});
  } catch (e) {
    caught = e;
  }
  assert.ok(isDataError(caught) && caught.code === "unavailable");
  assert.equal(failureOf(caught), "unavailable");
  assert.equal(failureOf(new Error("boom")), "failed");
});

test("FR-GWEB-04: noise reads the latest gap run; documents read the project's documents for the Document filter", async () => {
  answer(200, { status: "idle", result: null });
  const gaps = createHttpGaps();
  assert.equal((await gaps.noise(PID)).runId, null);
  assert.deepEqual(calls, [`GET /projects/${PID}/gap/runs/latest`]);
  answer(200, { items: [{ id: "d1", original_filename: "orders.md", status: "parsed" }], next_cursor: null });
  assert.deepEqual(await gaps.documents(PID), [{ id: "d1", name: "orders.md" }]);
  assert.deepEqual(calls, [`GET /projects/${PID}/documents?limit=100`]);
});
