// The Linear source against the stub API (tests/support/stub-linear-source.mjs): the check lists the issues, the add sends the identifiers left
// out as `exclude` (and nothing when none is), a bad link or identifier is refused with its closed code. Port 3341 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHttpLinearSource, toLinearSourceCheck } from "@/lib/data/http/linear-source";
import { createHttpProjects } from "@/lib/data/http/projects";
import { rejectsWith, serveStub } from "../support/stub-fetch";

const ctx = serveStub(3341, () => undefined);
const { sent } = ctx;
const linear = createHttpLinearSource();
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const PROJECT = "https://linear.app/graphico-app/project/checkout-0a1b2c3d4e5f";

async function project() {
  const p = await createProject.create({ name: "P" });
  sent.length = 0;
  return p.id;
}

test("the check of a project maps its issues in order, the documents and the cap flag", async () => {
  const pid = await project();
  const found = await linear.check(pid, PROJECT);
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/linear/check`);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: PROJECT });
  assert.deepEqual([found.kind, found.name, found.issues, found.documents, found.capped], ["project", "Checkout", 18, 2, false]);
  assert.deepEqual(found.items[0], { identifier: "GRA-1", title: "Cart totals", stateName: "Backlog", parent: null });
  const over = await linear.check(pid, "https://linear.app/graphico-app/project/everything-0a1b2c3d4e60");
  assert.deepEqual([over.items.length, over.capped], [200, true]);
});

test("the check of an issue by identifier lists its sub-issues under it and no documents", async () => {
  const pid = await project();
  const found = await linear.check(pid, "gra-12");
  assert.deepEqual([found.kind, found.name, found.issues, found.documents], ["issue", "GRA-12 Checkout redesign", 6, 0]);
  assert.deepEqual(
    found.items.map((i) => [i.identifier, i.parent]),
    [101, 102, 103, 104, 105, 106].map((n) => [`GRA-${String(n)}`, "GRA-12"]),
  );
});

test("add sends the identifiers left out as exclude, and no exclude when every issue is picked", async () => {
  const pid = await project();
  await linear.add(pid, PROJECT, ["GRA-2", "GRA-5"]);
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/linear`);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: PROJECT, exclude: ["GRA-2", "GRA-5"] });
  await linear.add(pid, PROJECT, []);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: PROJECT });
});

test("an identifier that is not one of the checked issues is refused as validation_failed", async () => {
  const pid = await project();
  await rejectsWith(linear.add(pid, PROJECT, ["GRA-999"]), "validation", "validation_failed");
});

test("each closed code of the API reaches the form as the API code", async () => {
  const pid = await project();
  await rejectsWith(linear.check(pid, "not a link"), "validation", "linear_link_invalid");
  await rejectsWith(linear.check(pid, "GRA-404"), "conflict", "linear_not_found");
  await rejectsWith(linear.check(pid, "GRA-403"), "conflict", "linear_forbidden");
  await rejectsWith(linear.check(pid, "GRA-401"), "conflict", "linear_reconnect_required");
});

test("a check answer of another shape is invalid_response", () => {
  const wire = {
    kind: "project",
    name: "N",
    issues: 1,
    items: [{ identifier: "GRA-1", title: "T", state: "Todo", parent: null }],
    capped: false,
    documents: 0,
  };
  assert.deepEqual(toLinearSourceCheck(wire), { ...wire, items: [{ identifier: "GRA-1", title: "T", stateName: "Todo", parent: null }] });
  for (const bad of [
    { ...wire, kind: "team" },
    { ...wire, issues: -1 },
    { ...wire, documents: "2" },
    { ...wire, items: [{ identifier: "", title: "T", state: "Todo", parent: null }] },
    { ...wire, items: [{ identifier: "GRA-1", title: "T", state: "Todo" }] },
    { kind: "issue", name: "N", issues: 0, capped: false },
  ]) {
    assert.throws(() => toLinearSourceCheck(bad), /./u);
  }
});
