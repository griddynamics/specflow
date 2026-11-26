// The Notion plan destination on the canvas (E2): what the node says in each state the API reports, the words of every closed reason, the line
// of a passed check, the registry entry, the form, and the check-then-save flow against the stub model (`createNotionPlanModel`). Server-rendered
// and in-process: no browser, no server.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import { NotionPlanFormBody } from "@/components/v2/canvas/NotionPlanForm";
import {
  notionAccessOutcome,
  notionCountsText,
  notionDatabaseLink,
  notionPassNote,
  notionPassText,
  notionPlanCheckText,
  notionPlanFailureText,
} from "@/components/v2/canvas/notion-plan-logic";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataError } from "@/lib/domain/errors";
import { defined } from "../support/defined";
import { formProps, html } from "./canvas-form-support";
import { BUILT, GAPS, integration, READY_PLAN } from "./canvas-fixtures";
import { COUNTS, DATABASE_PARENT, maybeNodeOf, modelPort, nodeOf, NOT_SHARED, PARENT, sync, TARGET } from "./notion-plan-fixtures";

const failure = (apiCode: string) => notionPlanFailureText(new DataError("conflict", false, { apiCode }));

test("the node is named after the parent page, under Plan, with the Notion mark, and counts the rows made", () => {
  const n = nodeOf({});
  assert.deepEqual(
    [n.zone, n.mark, n.name, n.state, n.badge, n.meta, n.wire, n.kind],
    ["plan", "notion", "Plans", "on", "ok", "6 rows created", "on", "notion_plan"],
  );
  assert.equal(n.detail.app, "Notion");
  assert.equal(n.detail.reconnect, null);
  assert.equal(nodeOf(sync({ counts: { ...COUNTS, created: 1 } })).meta, "1 row created");
  assert.equal(nodeOf(sync({ counts: { ...COUNTS, created: 3, updated: 2 } })).meta, "3 rows created · 2 updated");
  assert.equal(nodeOf(sync({ counts: { ...COUNTS, updated: 1 } })).meta, "1 row updated");
  assert.equal(nodeOf(sync({ counts: COUNTS })).meta, "In sync");
  const never = nodeOf(sync({ status: "idle", counts: null }));
  assert.deepEqual([never.meta, never.badge, never.state], ["Not synced yet", null, "on"]);
  assert.equal(nodeOf({ sync: null }).meta, "Not synced yet");
});

test("the name is the parent page title (Notion when it has none); Notion is only in the accessible label", () => {
  const n = nodeOf({});
  assert.equal(n.label, "Notion Plans: 6 rows created");
  const bare = nodeOf({ parentPageTitle: null });
  assert.deepEqual([bare.name, bare.label], ["Notion", "Notion: 6 rows created"]);
});

test("the dialog links the plan database only when it is an https Notion link", () => {
  const open = (databaseUrl: string | null) => nodeOf({ databaseUrl }).detail.link ?? null;
  assert.equal(open("https://www.notion.so/Plan-db-1"), "https://www.notion.so/Plan-db-1");
  assert.equal(open("https://acme.notion.site/x"), null);
  assert.equal(open(null), null);
  for (const bad of ["http://www.notion.so/x", "javascript:alert(1)", "https://evil.example/notion.so", "https://notion.so.evil.example/x", "nope"])
    assert.equal(notionDatabaseLink(bad), null, bad);
});

test("a 422 validation_failed (a link over the cap) reads as not a link", () => {
  const e = new DataError("validation", false, { apiCode: "validation_failed" });
  assert.equal(notionPlanFailureText(e), "This is not a link to a Notion page.");
});

test("the dialog says what a partial pass could not do and that deferred rows follow", () => {
  const partial = nodeOf(sync({ status: "partial", counts: { ...COUNTS, created: 120, failed: 2, deferred: 60 } }));
  assert.match(partial.detail.text, /could not do everything/);
  assert.match(partial.detail.text, /Notion refused some rows/);
  assert.match(partial.detail.text, /More rows follow in the next pass/);
  assert.equal(notionPassNote("succeeded", { ...COUNTS, created: 1 }), "");
  assert.equal(nodeOf({}).detail.text, "Stories and tasks become rows of a Notion database, kept up to date.");
});

test("the wire moves only while a pass runs", () => {
  const run = nodeOf(sync({ status: "running", counts: null }));
  assert.deepEqual([run.state, run.badge, run.wire, run.meta], ["busy", "spin", "flow", "Syncing…"]);
  assert.equal(nodeOf({}).wire, "on");
});

test("it waits for the plan, and while it is off; no parent page is no node", () => {
  const waiting = nodeOf({}, { plan: null });
  assert.deepEqual([waiting.state, waiting.wire, waiting.meta, waiting.name], ["wait", "off", "Waits for the plan", "Plans"]);
  const off = nodeOf({ enabled: false });
  assert.deepEqual([off.state, off.meta], ["wait", "Sync is off"]);
  assert.equal(maybeNodeOf({ parentPageId: null, parentPageTitle: null }), undefined);
  const none = buildCanvasModel(BUILT({ gaps: GAPS, plan: READY_PLAN }), NODE_MAPPERS);
  assert.equal(
    none.zones.plan.nodes.some((n) => n.kind === "notion_plan"),
    false,
  );
});

test("it stops for a lost Notion connection with Reconnect Notion, and for the code the last pass ended on", () => {
  for (const code of ["notion_not_connected", "notion_reconnect_required"]) {
    const n = nodeOf(sync({ status: "failed", errorCode: code }));
    assert.deepEqual([n.state, n.badge, n.wire, n.meta, n.detail.reconnect], ["stopped", "stop", "off", "Stopped · reconnect", "notion"]);
  }
  const bound = nodeOf({}, { integrations: [integration("notion", { health: "reconnect_required" })] });
  assert.deepEqual([bound.state, bound.detail.reconnect, bound.detail.by], ["stopped", "notion", "Connected by Alex Kim"]);
  const permissions = nodeOf(sync({ status: "failed", errorCode: "notion_permissions_missing" }));
  assert.deepEqual([permissions.state, permissions.detail.reconnect], ["stopped", "notion"]);
  const gone = nodeOf(sync({ status: "failed", errorCode: "notion_database_gone" }));
  assert.deepEqual([gone.state, gone.meta, gone.detail.reconnect], ["stopped", "Stopped", null]);
  assert.match(gone.detail.text, /plan database was deleted/);
  const unknown = nodeOf(sync({ status: "failed", errorCode: "unexpected_error" }));
  assert.match(unknown.detail.text, /reason Graphico does not know/);
  assert.equal(nodeOf(sync({ status: "failed", errorCode: "something_new" })).detail.text, "Something went wrong. Try again.");
});

test("a skipped pass is a wait with the words of its code, not a stop", () => {
  for (const [code, words] of [
    ["no_plan", "Waits for the plan"],
    ["plan_applying", "The plan is being applied"],
    ["target_changed", "The destination changed; the next pass uses the new one"],
  ] as const) {
    const n = nodeOf(sync({ status: "skipped", errorCode: code, counts: null }));
    assert.deepEqual([n.state, n.badge, n.wire, n.meta], ["wait", null, "off", words]);
  }
  assert.match(notionPassText("notion_page_gone"), /not made again$/);
  assert.match(notionPassText("notion_item_invalid"), /did not accept a row/);
});

test("each closed code has its own words; an unknown one is the general failure", () => {
  const codes = [
    "notion_not_connected",
    "notion_reconnect_required",
    "notion_unavailable",
    "notion_unreachable",
    "notion_auth_failed",
    "notion_rate_limited",
    "notion_link_invalid",
    "notion_not_shared",
    "notion_parent_not_a_page",
    "notion_permissions_missing",
  ];
  const words = codes.map(failure);
  assert.equal(new Set(words).size, codes.length);
  for (const w of words) assert.notEqual(w, "Something went wrong. Try again.");
  assert.match(failure("notion_parent_not_a_page"), /link to a database/);
  assert.match(failure("notion_not_shared"), /did not share it/);
  assert.equal(failure("something_new"), "Something went wrong. Try again.");
  assert.equal(notionPlanFailureText(new Error("x")), "Something went wrong. Try again.");
});

test("a failed access answer becomes the refusal of its code; a passed one names the page", () => {
  assert.throws(
    () => notionAccessOutcome({ result: "failed", reason: "notion_reconnect_required" }),
    (e) => e instanceof DataError && e.detail?.apiCode === "notion_reconnect_required",
  );
  const found = notionAccessOutcome({ result: "ok", parentPageTitle: "Plans" });
  assert.equal(notionPlanCheckText(found), "Page “Plans”. The plan database is created under it.");
  assert.equal(notionCountsText({ ...COUNTS, created: 2 }), "2 rows created");
  assert.equal(notionCountsText({ ...COUNTS, failed: 2, deferred: 60 }), "2 rows failed · 60 more to follow");
  assert.equal(notionCountsText({ ...COUNTS, removed: 3 }), "3 rows removed");
  assert.equal(notionCountsText({ ...COUNTS, commented: 1, unchanged: 9 }), "1 row commented");
  assert.equal(notionCountsText({ ...COUNTS, unchanged: 9 }), "In sync");
});

test("the registry has Notion under Plan, needing Notion, with the add label Add destination", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "notion_plan"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["plan", "notion", "notion", "Notion"]);
  assert.deepEqual(spec.check, { test: "Check access", testing: "Checking…", add: "Add destination" });
});

test("the form asks for the parent page link, once, with Check access", () => {
  const view = html(createElement(NotionPlanFormBody, formProps("plan", "notion_plan")));
  assert.equal(view.match(/Parent page link/g)?.length, 1);
  assert.match(view, /link of a page you shared with Graphico/);
  assert.match(view, /disabled=""[^>]*>Check access<\/button>/);
});

test("check then add: a page link passes and is saved on; the project ends with the destination", async () => {
  const { port } = modelPort("p1");
  const access = await port.checkAccess("p1", PARENT);
  assert.deepEqual(access, { result: "ok", parentPageTitle: "Plans" });
  const saved = await port.save("p1", { link: PARENT, enabled: true });
  assert.deepEqual([saved.enabled, saved.parentPageTitle, saved.databaseUrl], [true, "Plans", null]);
  const n = nodeOf(saved);
  assert.equal(n.name, "Plans");
});

test("every refusal of the check reaches the form in its own words", async () => {
  const { port, model } = modelPort("p1");
  const words = async (link: string) => {
    const access = await port.checkAccess("p1", link);
    try {
      notionAccessOutcome(access);
    } catch (e) {
      return notionPlanFailureText(e);
    }
    return null;
  };
  assert.match(defined(await words("not a link")), /not a link to a Notion page/);
  assert.match(defined(await words(NOT_SHARED)), /did not share it/);
  model.seed({ pid: "p1", access: { reason: "notion_parent_not_a_page" } });
  assert.match(defined(await words(DATABASE_PARENT)), /link to a database/);
  model.seed({ pid: "p1", access: { reason: "notion_permissions_missing" } });
  assert.match(defined(await words(PARENT)), /Reconnect Notion/);
});

test("an unbound or expired project is a failed check, and its save is a 409 in words", async () => {
  for (const [health, code, text] of [
    ["none", "notion_not_connected", /Connect Notion in Integrations first/],
    ["reconnect_required", "notion_reconnect_required", /needs a reconnect/],
  ] as const) {
    const { port } = modelPort("p1", health);
    assert.deepEqual(await port.checkAccess("p1", PARENT), { result: "failed", reason: code });
    await assert.rejects(
      () => port.save("p1", { link: PARENT, enabled: true }),
      (e) => e instanceof DataError && e.detail?.apiCode === code && text.test(notionPlanFailureText(e)),
    );
  }
});

test("the stub's seeded destination drives the node through its states", async () => {
  const { port, model } = modelPort("p1");
  model.seed({ pid: "p1", target: { parent_page_title: "Roadmap", sync: { status: "running", counts: null } } });
  const running = nodeOf(await port.get("p1"));
  assert.deepEqual([running.name, running.state, running.meta], ["Roadmap", "busy", "Syncing…"]);
  model.seed({
    pid: "p1",
    target: { parent_page_title: "Roadmap", sync: { status: "failed", error_code: "notion_reconnect_required", counts: null } },
  });
  assert.equal(nodeOf(await port.get("p1")).detail.reconnect, "notion");
  model.seed({ pid: "p1", available: false });
  assert.equal((await port.get("p1")).available, false);
  assert.equal(TARGET.parentPageTitle, "Plans");
});
