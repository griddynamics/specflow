// The Linear source on the canvas: what its nodes say in each state the sources API reports, the words of a failed check or add, the line of a
// passed check and of the selection, the registry entry and the form. Pure and server-rendered: no browser.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { NodeView } from "@/components/v2/canvas/canvas-types";
import { LinearSourceForm } from "@/components/v2/canvas/LinearSourceForm";
import {
  idOfItem,
  linearAddable,
  linearSelectionText,
  linearSourceCheckText,
  linearSourceFailureText,
} from "@/components/v2/canvas/linear-source-logic";
import { checkAll, checkNone, toggle } from "@/components/v2/canvas/pick-logic";
import type { FormProps } from "@/components/v2/connector-types";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { DataError } from "@/lib/domain/errors";
import type { LinearSourceCheck, LinearSourceItem } from "@/lib/domain/linear-source";
import { routesFor } from "@/lib/routes";
import { defined } from "../support/defined";
import { input, statusOf, wireSource } from "./canvas-fixtures";

const nodeOf = (counts: Parameters<typeof wireSource>[2], over: Record<string, unknown> = {}): NodeView => {
  const wire = { ...wireSource("e1", "jira_epic", counts), kind: "linear_project", name: "Checkout", ...over };
  const i = input({ status: statusOf([wire as ReturnType<typeof wireSource>]) });
  return defined(buildCanvasModel(i, NODE_MAPPERS).zones.graph.nodes[0]);
};

test("a Linear project node shows the Linear mark and counts issues", () => {
  const busy = nodeOf({ done: 5, processing: 13 });
  assert.deepEqual(
    [busy.mark, busy.kind, busy.state, busy.badge, busy.wire, busy.meta],
    ["linear", "linear_project", "busy", "spin", "flow", "5 of 18 issues"],
  );
  const done = nodeOf({ done: 18 });
  assert.deepEqual([done.state, done.meta, done.detail.app], ["on", "18 issues · Linear", "Linear"]);
});

test("a Linear import the API reports failed stops and offers to reconnect Linear, not Atlassian", () => {
  const noAccess = nodeOf({ failed: 1 }, { failure_reason: "no_access" });
  assert.deepEqual([noAccess.state, noAccess.meta, noAccess.detail.reconnect], ["stopped", "Cannot read it", "linear"]);
  assert.match(noAccess.detail.text, /cannot read this project now/);
  const failed = nodeOf({ failed: 1 }, { failure_reason: "unknown_error" });
  assert.deepEqual([failed.meta, failed.detail.reconnect], ["Import failed", null]);
  assert.match(failed.detail.text, /Add the project again/);
});

const failure = (apiCode: string) => linearSourceFailureText(new DataError("validation", false, { apiCode }));

test("each closed code of the check and the add has its own words; an unknown one is the general failure", () => {
  assert.match(failure("linear_not_connected"), /Connect Linear in Integrations first/);
  assert.match(failure("linear_reconnect_required"), /needs a reconnect/);
  assert.match(failure("linear_unavailable"), /not set up on this server/);
  assert.match(failure("linear_link_invalid"), /not a Linear project or issue link/);
  assert.match(failure("linear_not_found"), /no such project or issue/);
  assert.match(failure("linear_forbidden"), /not allowed to read this/);
  assert.match(failure("linear_rate_limited"), /limiting requests/);
  assert.match(failure("linear_unreachable"), /did not answer/);
  assert.match(failure("source_not_found"), /The source was removed\. Add the project again\./);
  assert.equal(failure("something_new"), "Something went wrong. Try again.");
});

const item = (n: number, parent: string | null = null): LinearSourceItem => ({
  identifier: `GRA-${String(n)}`,
  title: `Issue ${String(n)}`,
  stateName: "Todo",
  parent,
});
const project = (n: number, documents = 0): LinearSourceCheck => ({
  kind: "project",
  name: "Checkout",
  issues: n,
  items: Array.from({ length: n }, (_, i) => item(i + 1)),
  capped: false,
  documents,
});
const issue = (n: number): LinearSourceCheck => ({
  kind: "issue",
  name: "GRA-9 Card flow",
  issues: n,
  items: Array.from({ length: n }, (_, i) => item(101 + i, "GRA-9")),
  capped: false,
  documents: 0,
});

test("a passed check says the name, the issues and the documents; the cap is said once, in the note of the list", () => {
  assert.equal(linearSourceCheckText(project(18, 2)), "Checkout · 18 issues · 2 documents.");
  assert.equal(linearSourceCheckText(project(1, 1)), "Checkout · 1 issue · 1 document.");
  assert.equal(linearSourceCheckText(project(0)), "Checkout · 0 issues.");
  assert.equal(linearSourceCheckText(issue(3)), "GRA-9 Card flow · 3 sub-issues.");
  assert.equal(linearSourceCheckText(issue(1)), "GRA-9 Card flow · 1 sub-issue.");
});

test("the status line says what an add takes: N of M issues and the documents, only the documents, only the issue, or nothing", () => {
  const rows = project(4, 2).items.map(idOfItem);
  assert.equal(linearSelectionText(project(4, 2), checkAll()), "4 of 4 issues, and 2 documents");
  assert.equal(linearSelectionText(project(4, 2), toggle(checkAll(), "GRA-3", false)), "3 of 4 issues, and 2 documents");
  assert.equal(linearSelectionText(project(4, 2), checkNone(rows)), "Only 2 documents");
  assert.equal(linearSelectionText(project(4), checkAll()), "4 of 4 issues");
  assert.equal(linearSelectionText(project(4), checkNone(rows)), "Nothing to add");
  assert.equal(linearSelectionText(issue(3), toggle(checkAll(), "GRA-101", false)), "2 of 3 sub-issues, and the issue");
  assert.equal(linearSelectionText(issue(3), checkNone(issue(3).items.map(idOfItem))), "Only the issue");
});

test("an add needs something to take: an issue always, a project only with an issue left checked or a document", () => {
  const none = (c: LinearSourceCheck) => checkNone(c.items.map(idOfItem));
  assert.equal(linearAddable(issue(2), none(issue(2))), true);
  assert.equal(linearAddable(project(2, 1), none(project(2, 1))), true);
  assert.equal(linearAddable(project(2), none(project(2))), false);
  assert.equal(linearAddable(project(2), checkAll()), true);
  assert.equal(linearAddable(project(0), checkAll()), false);
});

const noop = () => undefined;
const ports: Partial<DataPorts> = {};
const ui = createUiStore();
const context: DataContextValue = { ui, ports: ports as DataPorts, routes: routesFor("production"), handoff: createUiHandoff(ui) };
const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));

test("the registry has the Linear project in Graph, needing Linear, with its check labels", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "linear_project"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["graph", "linear", "linear", "Linear project"]);
  assert.deepEqual(spec.check, { test: "Check link", testing: "Checking…", add: "Add source" });
});

test("the form asks for a link or identifier, says what is read once, and Check link is off until there is one", () => {
  const props: FormProps = {
    projectId: "p1",
    zone: "graph",
    spec: defined(CONNECTORS.find((c) => c.kind === "linear_project")),
    mcp: { mcpUrl: "", clientId: "", serverName: "" },
    reload: { current: noop },
    onBack: noop,
    onClose: noop,
  };
  const view = html(createElement(LinearSourceForm, props));
  assert.match(view, />Linear project, issue link or identifier<\/label>/);
  assert.match(view, /with their comments, once when it is added/);
  assert.match(view, /disabled=""[^>]*>Check link<\/button>/);
});
