// docs/slack/gap-answering.md "The channel flow": the wire parser of the production SlackPort (`GET`/`PUT /projects/{pid}/slack`).
// Layer U only: any shape other than the API's answer is `invalid_response`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { toSlackSettings } from "@/lib/data/http/slack";
import { isDataError } from "@/lib/domain/errors";

const wire = (over: Record<string, unknown> = {}) => ({
  available: true,
  editable: true,
  channel: { id: "C1", name: "shop-gaps", private: true },
  channels: [
    { id: "C1", name: "shop-gaps", private: true },
    { id: "C2", name: "general", private: false },
  ],
  ...over,
});
const invalid = (v: unknown) => {
  assert.throws(
    () => toSlackSettings(v),
    (e) => isDataError(e) && e.code === "invalid_response",
  );
};

test("the Slack settings keep the bound channel and the channels to choose from", () => {
  const s = toSlackSettings(wire());
  assert.deepEqual(s.channel, { id: "C1", name: "shop-gaps", private: true });
  assert.deepEqual(
    s.channels.map((c) => c.id),
    ["C1", "C2"],
  );
  assert.equal(s.editable, true);
  assert.equal(toSlackSettings(wire({ channel: null, available: false, channels: [] })).channel, null);
});

test("any other shape is invalid_response", () => {
  invalid(null);
  invalid(wire({ available: "yes" }));
  invalid(wire({ channels: [{ id: "C1", name: 3, private: false }] }));
  invalid(wire({ channel: { id: "C1" } }));
});
