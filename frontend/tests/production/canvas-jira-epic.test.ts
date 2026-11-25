// The Jira epic source on the canvas (09F): what its nodes say in each state the sources API reports, the words of a failed check or add, the
// line of a passed check, the form, and the stub's reserved keys through the production port. Pure and server-rendered: no browser.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { NodeView } from "@/components/v2/canvas/canvas-types";
import { idOfChild, jiraEpicCheckText, jiraEpicFailureText, selectionText } from "@/components/v2/canvas/jira-epic-logic";
import { checkAll, checkNone, toggle } from "@/components/v2/canvas/pick-logic";
import { JiraEpicForm } from "@/components/v2/canvas/JiraEpicForm";
import { unitText } from "@/components/v2/canvas/nodes-sources";
import type { FormProps } from "@/components/v2/connector-types";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { toJiraEpicCheck } from "@/lib/data/http/sources";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { DataError, isDataError } from "@/lib/domain/errors";
import type { JiraEpicChild, JiraEpicCheck } from "@/lib/domain/sources";
import { routesFor } from "@/lib/routes";
import { defined } from "../support/defined";
import { input, statusOf, wireSource } from "./canvas-fixtures";

const nodeOf = (counts: Parameters<typeof wireSource>[2], over: Record<string, unknown> = {}): NodeView => {
  const i = input({ status: statusOf([wireSource("e1", "jira_epic", counts, { name: "NWO-12: Checkout redesign", ...over })]) });
  return defined(buildCanvasModel(i, NODE_MAPPERS).zones.graph.nodes[0]);
};

test("a Jira epic node is named by the key, shows the Jira mark and counts issues; the full name is in its label", () => {
  const busy = nodeOf({ done: 5, processing: 13 });
  assert.deepEqual(
    [busy.name, busy.mark, busy.kind, busy.state, busy.badge, busy.wire, busy.meta],
    ["NWO-12", "jira", "jira_epic", "busy", "spin", "flow", "5 of 18 issues"],
  );
  assert.match(busy.label, /^Jira NWO-12: Checkout redesign: 5 of 18 issues$/);
  const done = nodeOf({ done: 18 });
  assert.deepEqual([done.state, done.badge, done.meta, done.wire], ["on", "ok", "18 issues · Jira", "on"]);
  assert.match(done.detail.text, /^18 issues\. Imported when it was added/);
  assert.deepEqual([done.detail.app, done.detail.remove], ["Jira", "source"]);
  assert.equal(nodeOf({ done: 1 }).meta, "1 issue · Jira");
});

test("a Jira epic import the API reports failed stops: no access offers to reconnect Atlassian, any other reason says it failed", () => {
  const noAccess = nodeOf({ failed: 1 }, { failure_reason: "no_access" });
  assert.deepEqual(
    [noAccess.state, noAccess.badge, noAccess.wire, noAccess.meta, noAccess.detail.reconnect],
    ["stopped", "stop", "off", "Cannot read it", "atlassian"],
  );
  assert.match(noAccess.detail.text, /cannot read this epic now/);
  const failed = nodeOf({ failed: 1 }, { failure_reason: "unknown_error" });
  assert.deepEqual([failed.meta, failed.detail.reconnect], ["Import failed", null]);
  assert.match(failed.detail.text, /Add the epic again/);
});

test("the unit follows the kind: issues for an epic, pages for Confluence, files for the rest", () => {
  assert.equal(unitText("jira_epic", "3 of 12 files"), "3 of 12 issues");
  assert.equal(unitText("confluence", "1 file"), "1 page");
  assert.equal(unitText("upload", "1 file"), "1 file");
});

const CHECK: JiraEpicCheck = { key: "", summary: "", issues: 0, children: [], capped: false };
const failure = (apiCode: string) => jiraEpicFailureText(new DataError("validation", false, { apiCode }));

test("each closed code of the check and the add has its own words; an unknown one is the general failure", () => {
  assert.match(failure("atlassian_not_connected"), /Connect Atlassian in Integrations first/);
  assert.match(failure("atlassian_site_mismatch"), /another Atlassian site/);
  assert.match(failure("jira_epic_link_invalid"), /not an epic key \(such as ABC-12\)/);
  assert.match(failure("jira_epic_not_an_epic"), /not an epic/);
  assert.match(failure("jira_epic_not_found"), /no issue with this key/);
  assert.match(failure("jira_epic_forbidden"), /not allowed to read this epic/);
  assert.match(failure("source_not_found"), /The source was removed\. Add the epic again\./);
  assert.equal(failure("something_new"), "Something went wrong. Try again.");
});

test("a passed check says the key, the summary and the issues; the cap is said once, in the note of the list", () => {
  assert.equal(
    jiraEpicCheckText({ ...CHECK, key: "NWO-12", summary: "Checkout redesign", issues: 18 }),
    "NWO-12 · Checkout redesign · 18 issues.",
  );
  assert.equal(jiraEpicCheckText({ ...CHECK, key: "NWO-1", summary: "One", issues: 1 }), "NWO-1 · One · 1 issue.");
  assert.equal(jiraEpicCheckText({ ...CHECK, key: "NWO-250", summary: "Everything", issues: 250 }), "NWO-250 · Everything · 250 issues.");
  const wire = {
    key: "NWO-12",
    summary: "S",
    issues: 1,
    capped: false,
    children: [{ key: "NWO-101", summary: "A", type: "Story", status: "Done" }],
  };
  assert.deepEqual(toJiraEpicCheck(wire), { ...wire, children: [{ key: "NWO-101", summary: "A", type: "Story", statusName: "Done" }] });
  assert.throws(
    () => toJiraEpicCheck({ ...wire, key: "" }),
    (e) => isDataError(e) && e.code === "invalid_response",
  );
  assert.throws(
    () => toJiraEpicCheck({ ...wire, children: [{ key: "NWO-101" }] }),
    (e) => isDataError(e) && e.code === "invalid_response",
  );
  assert.throws(
    () => toJiraEpicCheck({ key: "NWO-12", summary: "S", issues: 1 }),
    (e) => isDataError(e) && e.code === "invalid_response",
  );
});

const noop = () => undefined;
const ports: Partial<DataPorts> = {};
const ui = createUiStore();
const context: DataContextValue = { ui, ports: ports as DataPorts, routes: routesFor("production"), handoff: createUiHandoff(ui) };
const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));

test("the registry has the Jira epic in Graph, needing Atlassian, with its check labels", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "jira_epic"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["graph", "atlassian", "jira", "Jira epic"]);
  assert.deepEqual(spec.check, { test: "Check epic", testing: "Checking…", add: "Add source" });
});

test("the Jira epic form asks for a key or link, says it reads the epic and its comments once, and Check epic is off until there is a key", () => {
  const props: FormProps = {
    projectId: "p1",
    zone: "graph",
    spec: defined(CONNECTORS.find((c) => c.kind === "jira_epic")),
    mcp: { mcpUrl: "", clientId: "", serverName: "" },
    reload: { current: noop },
    onBack: noop,
    onClose: noop,
  };
  const view = html(createElement(JiraEpicForm, props));
  assert.match(view, />Epic key or link<\/label>/);
  assert.match(view, /with their comments, once when it is added/);
  assert.match(view, /disabled=""[^>]*>Check epic<\/button>/);
});

const child = (n: number): JiraEpicChild => ({
  key: `NWO-${String(100 + n)}`,
  summary: `Issue ${String(n)}`,
  type: "Story",
  statusName: "To Do",
});
const FOUR = [1, 2, 3, 4].map(child);

test("the status line says what an add takes: N of M issues and the epic, or only the epic", () => {
  const rows = FOUR.map(idOfChild);
  assert.equal(selectionText(FOUR, checkAll()), "4 of 4 issues, and the epic");
  assert.equal(selectionText(FOUR, toggle(checkAll(), "NWO-103", false)), "3 of 4 issues, and the epic");
  assert.equal(selectionText(FOUR, checkNone(rows)), "Only the epic");
  assert.equal(selectionText([child(1)], checkAll()), "1 of 1 issue, and the epic");
});
