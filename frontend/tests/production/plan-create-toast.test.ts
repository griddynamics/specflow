// SPL-DEMO-02: "Create PLAN" on the unavailable plan port shows one neutral toast; any other failure keeps the danger toast.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createFailToast, createPress } from "@/components/plan/before/create-press";
import { unavailable } from "@/lib/data/unavailable";
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

test("SPL-DEMO-02: the unavailable plan port, pressed twice in a row, calls onFail once per settled press", async () => {
  const seen: unknown[] = [];
  const press = createPress(
    () => unavailable.plan.create("p1"),
    (e) => seen.push(e),
  );
  await press();
  await press();
  assert.equal(seen.length, 2);
  assert.ok(seen.every((e) => e instanceof DataError && e.code === "not_available"));
});
