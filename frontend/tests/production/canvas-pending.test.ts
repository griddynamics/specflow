// A start that the read side has not shown yet (a build or a plan just started on the canvas): when it ends, and that the 15 s timeout ends it
// when nothing else does. The timer runs on fake time.
import assert from "node:assert/strict";
import { mock, test } from "node:test";
import { clearsPending, PENDING_MS, startPendingTimer } from "@/components/v2/canvas/pending";

test("a start ends when the work is seen running, or when the state's signal moved", () => {
  assert.equal(clearsPending(null, true, "a"), false, "nothing was started");
  assert.equal(clearsPending("a", false, "a"), false, "not seen yet");
  assert.equal(clearsPending("a", true, "a"), true, "seen running");
  assert.equal(clearsPending("a", false, "b"), true, "ended before it was seen");
});

test("the timeout is 15 s: nothing happens before, the start ends at 15 s", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    let expired = 0;
    startPendingTimer(() => {
      expired += 1;
    });
    assert.equal(PENDING_MS, 15_000);
    mock.timers.tick(14_999);
    assert.equal(expired, 0);
    mock.timers.tick(1);
    assert.equal(expired, 1);
    mock.timers.tick(60_000);
    assert.equal(expired, 1, "once");
  } finally {
    mock.timers.reset();
  }
});

test("a start that ends first cancels its timeout", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    let expired = 0;
    const cancel = startPendingTimer(() => {
      expired += 1;
    });
    mock.timers.tick(5_000);
    cancel();
    mock.timers.tick(60_000);
    assert.equal(expired, 0);
  } finally {
    mock.timers.reset();
  }
});
