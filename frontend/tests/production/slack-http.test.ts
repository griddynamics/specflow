// docs/slack/gap-answering.md "The channel flow": the wire parser of the production SlackPort (`GET /projects/{pid}/slack`).
// Layer U only: any shape other than the API's answer is `invalid_response`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { toSlackSettings } from "@/lib/data/http/slack";
import { isDataError } from "@/lib/domain/errors";
import { slackConnectCommand } from "@/lib/domain/slack";

const wire = (over: Record<string, unknown> = {}) => ({
  available: true,
  editable: true,
  channel: { id: "C1", name: "shop-gaps", private: true },
  ...over,
});
const invalid = (v: unknown) => {
  assert.throws(
    () => toSlackSettings(v),
    (e) => isDataError(e) && e.code === "invalid_response",
  );
};

test("the Slack settings keep only the project's own channel", () => {
  const s = toSlackSettings(wire({ channels: [{ id: "C2", name: "general", private: false }] }));
  assert.deepEqual(s, { available: true, channel: { id: "C1", name: "shop-gaps", private: true }, editable: true });
  assert.equal(toSlackSettings(wire({ channel: null, available: false })).channel, null);
});

test("the connect command names the project by its id", () => {
  assert.equal(slackConnectCommand("0b6f-7"), "/specflow connect 0b6f-7");
});

test("any other shape is invalid_response", () => {
  invalid(null);
  invalid(wire({ available: "yes" }));
  invalid(wire({ editable: "no" }));
  invalid(wire({ channel: { id: "C1" } }));
});
