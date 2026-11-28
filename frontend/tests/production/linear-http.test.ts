// plans/LINEAR/LINEAR-PLAN.md stream S1: the production LinearPort against the stub API (tests/support/stub-linear.mjs) and the words of the Linear
// return codes. The real `lib/api.ts` and the adapter run; only `fetch` is replaced (tests/support/stub-fetch.ts). Layer U: the wire parser.
// Layer P: each route's request, response and error rows. Layer W: the toast and the failure table. Port 3288 (stub API).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHttpLinear, toLinearConnection } from "@/lib/data/http/linear";
import { isDataError } from "@/lib/domain/errors";
import { LINEAR_FAILURE, linearToast } from "@/components/v2/integrations/linear-logic";
import { LINEAR_CODES } from "@/lib/linear-callback";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const PORT = 3288;
const ctx = serveStub(PORT, (api) => {
  api.linear.reset();
});
const { sent } = ctx;
const linear = createHttpLinear();
const last = () => sent[sent.length - 1];
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response");
};
const BACK = "/projects/p1/integrations?setup=linear";
const WORKSPACE = { id: "org-1", name: "Acme", url_key: "acme" };
const wire = (over: Record<string, unknown> = {}) => ({
  available: true,
  status: "active",
  user_name: "Ann",
  workspace: WORKSPACE,
  ...over,
});

// ---- Layer U ----

test("toLinearConnection: not connected, active and expired read; a status that disagrees with its fields is invalid", () => {
  assert.deepEqual(toLinearConnection(wire({ status: "none", user_name: null, workspace: null })), {
    available: true,
    status: "none",
    userName: null,
    workspace: null,
  });
  assert.deepEqual(toLinearConnection(wire()), {
    available: true,
    status: "active",
    userName: "Ann",
    workspace: { id: "org-1", name: "Acme", urlKey: "acme" },
  });
  assert.equal(toLinearConnection(wire({ status: "expired" })).status, "expired");
  assert.equal(toLinearConnection(wire({ status: "revoked" })).status, "revoked");
  assert.equal(toLinearConnection(wire({ available: false, status: "none", user_name: null, workspace: null })).available, false);
  invalid(() => toLinearConnection(wire({ status: "gone" })));
  invalid(() => toLinearConnection(wire({ workspace: null })));
  invalid(() => toLinearConnection(wire({ user_name: "" })));
  invalid(() => toLinearConnection(wire({ status: "none" })));
  invalid(() => toLinearConnection(wire({ workspace: { id: "o", name: "A" } })));
  invalid(() => toLinearConnection({ status: "none", user_name: null, workspace: null }));
});

// ---- Layer P ----

test("connection reads the caller's state: off, then connected through the callback", async () => {
  assert.equal((await linear.connection()).status, "none");
  assert.deepEqual(last(), { method: "GET", path: "/api/backend/integrations/linear", body: null });

  const url = new URL(await linear.authorize(BACK));
  assert.equal(url.origin + url.pathname, "https://linear.app/oauth/authorize");
  assert.deepEqual(last(), {
    method: "POST",
    path: "/api/backend/integrations/linear/authorize",
    body: JSON.stringify({ return_to: BACK }),
  });

  ctx.api.linear.callback({ code: "c", state: url.searchParams.get("state") });
  const mine = await linear.connection();
  assert.deepEqual([mine.status, mine.userName, mine.workspace?.name], ["active", "dev", "Acme"]);
});

test("an expired connection reads as expired; a server without Linear says unavailable and refuses the rest", async () => {
  ctx.api.linear.seed({ connection: { status: "expired" } });
  assert.equal((await linear.connection()).status, "expired");
  ctx.api.linear.seed({ available: false });
  assert.equal((await linear.connection()).available, false);
  await rejects(linear.authorize(BACK), "unavailable", "linear_unavailable");
});

test("authorize refuses a return that is not a path of this site before any consent", async () => {
  await rejects(linear.authorize("https://evil.example/x"), "validation", "validation_failed");
});

// ---- Layer W ----

test("the toast of a return code: connected succeeds, every known failure and an unknown one says something", () => {
  assert.deepEqual(linearToast("connected"), { title: "Connected", tone: "success" });
  const titles = new Set<string>();
  for (const code of LINEAR_CODES.filter((c) => c !== "connected")) {
    const toast = linearToast(code);
    assert.equal(toast.tone, "danger", code);
    assert.ok(toast.title.length > 0, code);
    titles.add(toast.title);
  }
  assert.ok(titles.size >= 4);
  assert.equal(linearToast("something_else").title, "Something went wrong. Try again.");
});

test("a failed connect names its reason by API code", () => {
  assert.equal(LINEAR_FAILURE.linear_not_connected, "Connect your own account first.");
  assert.ok(Object.values(LINEAR_FAILURE).every((text) => text.length > 0));
});
