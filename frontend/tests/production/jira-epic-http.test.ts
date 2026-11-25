// The Jira epic source against the stub API (tests/support/stub-jira-epic.mjs): the check lists the children, the add sends the keys left out as
// `exclude` (and nothing when none is), a bad key is refused. Port 3299 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHttpProjects } from "@/lib/data/http/projects";
import { createHttpSources } from "@/lib/data/http/sources";
import { isDataError } from "@/lib/domain/errors";
import { serveStub } from "../support/stub-fetch";

const ctx = serveStub(3299, () => undefined);
const { sent } = ctx;
const changed: string[] = [];
const sources = createHttpSources({ changed: (pid) => changed.push(pid) });
const { createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];

async function project() {
  const p = await createProject.create({ name: "P" });
  sent.length = 0;
  changed.length = 0;
  return p.id;
}

test("the check maps the children (key, summary, type, status) in key order and the cap flag", async () => {
  const pid = await project();
  const found = await sources.checkJiraEpic(pid, "NWO-12");
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/jira-epic/check`);
  assert.equal(found.children.length, 18);
  assert.deepEqual(found.children[0], { key: "NWO-101", summary: "Cart totals", type: "Story", statusName: "To Do" });
  assert.deepEqual(
    found.children.map((c) => c.key),
    found.children.map((c) => c.key).toSorted((a, b) => a.localeCompare(b, "en", { numeric: true })),
  );
  assert.equal(found.capped, false);
  const over = await sources.checkJiraEpic(pid, "NWO-250");
  assert.deepEqual([over.children.length, over.capped], [200, true]);
});

test("add sends the keys left out as exclude, and no exclude when every issue is picked", async () => {
  const pid = await project();
  await sources.addJiraEpic(pid, "NWO-12", ["NWO-102", "NWO-105"]);
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/jira-epic`);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { key_or_link: "NWO-12", exclude: ["NWO-102", "NWO-105"] });
  await sources.addJiraEpic(pid, "NWO-12", []);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { key_or_link: "NWO-12" });
  assert.deepEqual(changed, [pid, pid]);
});

test("a key that is not a child is refused as validation_failed, and the port is still told", async () => {
  const pid = await project();
  const e = await sources.addJiraEpic(pid, "NWO-12", ["NWO-999"]).then(
    () => null,
    (error: unknown) => error,
  );
  assert.ok(isDataError(e), "a DataError");
  assert.equal(e.detail?.apiCode, "validation_failed");
  assert.deepEqual(changed, [pid]);
});
