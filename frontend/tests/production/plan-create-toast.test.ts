// FE-PLAN-03: "Create PLAN" failures: a not-available port and the API's refusals give a neutral toast; any other failure the danger toast.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createFailToast, createPress } from "@/components/plan/before/create-press";
import { DataError } from "@/lib/domain/errors";

test("SPL-DEMO-02: a not-available create gives the neutral info toast", () => {
  const t = createFailToast(new DataError("not_available", false));
  assert.equal(t.title, "Plan creation is not available yet.");
  assert.equal(t.tone, "info");
});

test("SPL-DEMO-02: another failure gives the danger toast", () => {
  assert.equal(createFailToast(new DataError("network", true)).tone, "danger");
  assert.equal(createFailToast(new Error("x")).tone, "danger");
});

const refused = (apiCode: string) => new DataError("conflict", false, { status: 409, apiCode });

test("FE-PLAN-03: the API's refusals give their own info toast", () => {
  assert.deepEqual(createFailToast(refused("no_graph_build")), {
    title: "Build the knowledge graph before creating the plan.",
    tone: "info",
  });
  assert.equal(createFailToast(refused("graph_build_running")).tone, "info");
});

test("FE-PLAN-03: a press that fails calls onFail once per settled press; a plain conflict (a run already exists) is swallowed", async () => {
  const seen: unknown[] = [];
  const failing = createPress(
    () => Promise.reject(refused("no_graph_build")),
    (e) => seen.push(e),
  );
  await failing();
  await failing();
  assert.equal(seen.length, 2);
  const running = createPress(
    () => Promise.reject(new DataError("conflict", false, { status: 409, apiCode: "conflict" })),
    (e) => seen.push(e),
  );
  await running();
  assert.equal(seen.length, 2);
});
