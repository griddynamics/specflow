// Not a test: the node tests the gap channels and plan destinations on the canvas share (Jira and Linear gaps, GitHub and Linear plan).
import assert from "node:assert/strict";
import { test } from "node:test";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { CanvasInput, ConnectorKind, NodeView, ZoneId } from "@/components/v2/canvas/canvas-types";
import { NODE_MAPPERS } from "@/components/v2/connectors";
import { BUILT, GAPS } from "./canvas-fixtures";

type NodeOf = (over: { enabled?: boolean }, input?: Partial<CanvasInput>) => NodeView;

const hasNode = (input: Partial<CanvasInput>, zone: ZoneId, kind: ConnectorKind): boolean =>
  buildCanvasModel(BUILT({ gaps: GAPS, ...input }), NODE_MAPPERS).zones[zone].nodes.some((n) => n.kind === kind);

/** The gap channel's wire (it moves only while a gap run goes on) and waits (off, no gaps yet, nothing set up means no node). */
export function gapWaitTests(c: { kind: ConnectorKind; nodeOf: NodeOf; unset: Partial<CanvasInput> }): void {
  test("the wire moves only while a gap run goes on", () => {
    assert.equal(c.nodeOf({}, { gaps: { ...GAPS, phase: "done" } }).wire, "on");
    // A running run has not found its gaps yet: the node waits, dashed.
    const waiting = c.nodeOf({}, { gaps: { ...GAPS, phase: "running" } });
    assert.deepEqual([waiting.state, waiting.wire, waiting.meta], ["wait", "off", "Waits for gaps"]);
  });

  test("it waits while posting is off or the gaps are not found; nothing is set up means no node", () => {
    assert.deepEqual([c.nodeOf({ enabled: false }).state, c.nodeOf({ enabled: false }).meta], ["wait", "Posting is off"]);
    assert.equal(c.nodeOf({}, { gaps: null }).meta, "Waits for gaps");
    assert.equal(hasNode(c.unset, "gap", c.kind), false);
    assert.equal(hasNode({}, "gap", c.kind), false);
  });
}

/** The plan destination's skipped pass: a wait with the words of its code, not a stop and not "Not synced yet". */
export function skippedPassTest(nodeOf: (sync: { status: "skipped"; errorCode: string; counts: null }) => NodeView): void {
  test("a skipped pass is a wait with the words of its code, not a stop and not 'Not synced yet'", () => {
    for (const [code, words] of [
      ["no_plan", "Waits for the plan"],
      ["plan_applying", "The plan is being applied"],
      ["target_changed", "The destination changed; the next pass uses the new one"],
    ] as const) {
      const n = nodeOf({ status: "skipped", errorCode: code, counts: null });
      assert.deepEqual([n.state, n.badge, n.wire, n.meta], ["wait", null, "off", words]);
    }
  });
}
