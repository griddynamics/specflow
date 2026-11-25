// The Jira gap sub-tasks on the canvas (10F): what the node says in each state the API reports, the registry entry, the form before and
// after its read, and the shared read gate. Pure and server-rendered: no browser.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { CanvasInput, NodeView } from "@/components/v2/canvas/canvas-types";
import { JiraGapFormBody } from "@/components/v2/canvas/JiraGapForm";
import { ReadGate } from "@/components/v2/canvas/ReadGate";
import type { FormProps } from "@/components/v2/connector-types";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import type { JiraGaps } from "@/lib/domain/jira-gaps";
import { routesFor } from "@/lib/routes";
import { defined } from "../support/defined";
import { BUILT, GAPS, integration } from "./canvas-fixtures";

const TARGET: JiraGaps = {
  available: true,
  jiraProject: "NWO",
  grouping: "one",
  enabled: true,
  storyKey: "NWO-5",
  storyUrl: "https://northwind.atlassian.test/browse/NWO-5",
  posted: 7,
  answered: 2,
  lastPolledAt: null,
  errorCode: null,
  pollSeconds: 60,
};
const nodeOf = (over: Partial<JiraGaps>, input: Partial<CanvasInput> = {}): NodeView =>
  defined(
    buildCanvasModel(BUILT({ gaps: GAPS, jiraGaps: { ...TARGET, ...over }, ...input }), NODE_MAPPERS).zones.gap.nodes.find(
      (n) => n.kind === "jira_gap",
    ),
  );

test("the node counts the questions posted and answered, under Gaps, with the Jira mark", () => {
  const n = nodeOf({});
  assert.deepEqual(
    [n.zone, n.mark, n.name, n.state, n.badge, n.meta, n.wire],
    ["gap", "jira", "Jira · NWO gaps", "on", "ok", "7 posted · 2 answered", "on"],
  );
  assert.match(n.detail.text, /sub-tasks of one Story; answers come back as comments\. Story NWO-5\./);
  assert.equal(n.detail.reconnect, null);
  assert.equal(nodeOf({ storyKey: null }).detail.text.includes("Story NWO"), false);
});

test("the wire moves only while a gap run goes on", () => {
  assert.equal(nodeOf({}, { gaps: { ...GAPS, phase: "done" } }).wire, "on");
  // A running run has not found its gaps yet: the node waits, dashed.
  const waiting = nodeOf({}, { gaps: { ...GAPS, phase: "running" } });
  assert.deepEqual([waiting.state, waiting.wire, waiting.meta], ["wait", "off", "Waits for gaps"]);
});

test("it waits while posting is off or the gaps are not found; nothing is set up means no node", () => {
  assert.deepEqual([nodeOf({ enabled: false }).state, nodeOf({ enabled: false }).meta], ["wait", "Posting is off"]);
  assert.equal(nodeOf({}, { gaps: null }).meta, "Waits for gaps");
  const none = buildCanvasModel(BUILT({ gaps: GAPS, jiraGaps: { ...TARGET, jiraProject: null } }), NODE_MAPPERS);
  assert.equal(
    none.zones.gap.nodes.some((n) => n.kind === "jira_gap"),
    false,
  );
  assert.equal(
    buildCanvasModel(BUILT({ gaps: GAPS }), NODE_MAPPERS).zones.gap.nodes.some((n) => n.kind === "jira_gap"),
    false,
  );
});

test("it stops only for what the API reports: a lost Atlassian connection offers Reconnect, another closed code says what it is", () => {
  for (const code of ["jira_not_connected", "jira_reconnect_required"]) {
    const n = nodeOf({ errorCode: code });
    assert.deepEqual(
      [n.state, n.badge, n.wire, n.meta, n.detail.reconnect],
      ["stopped", "stop", "off", "Stopped · reconnect", "atlassian"],
    );
  }
  const bound = nodeOf({}, { integrations: [integration("atlassian", { health: "reconnect_required" })] });
  assert.deepEqual([bound.state, bound.detail.reconnect], ["stopped", "atlassian"]);
  const forbidden = nodeOf({ errorCode: "jira_forbidden" });
  assert.deepEqual([forbidden.state, forbidden.meta, forbidden.detail.reconnect], ["stopped", "Stopped", null]);
  assert.match(forbidden.detail.text, /\w/);
});

test("the registry has the Jira sub-tasks under Gaps, needing Atlassian, with the add label Add channel", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "jira_gap"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["gap", "atlassian", "jira", "Jira sub-tasks"]);
  assert.deepEqual(spec.check, { test: "Check access", testing: "Checking…", add: "Add channel" });
});

const noop = () => undefined;
const noPorts: Partial<DataPorts> = {};
const ui = createUiStore();
const context: DataContextValue = { ui, ports: noPorts as DataPorts, routes: routesFor("production"), handoff: createUiHandoff(ui) };
const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));
const props = (): FormProps => ({
  projectId: "p1",
  zone: "gap",
  spec: defined(CONNECTORS.find((c) => c.kind === "jira_gap")),
  mcp: { mcpUrl: "", clientId: "", serverName: "" },
  reload: { current: noop },
  onBack: noop,
  onClose: noop,
});

test("the form asks for the space key and the grouping, says how often answers are read (from the API), and Check access is off until there is a key", () => {
  const view = html(createElement(JiraGapFormBody, { ...props(), pollSeconds: 45 }));
  assert.match(view, />Jira space key<\/label>/);
  assert.match(view, />Sub-tasks<\/label>/);
  assert.match(view, /<option value="one" selected="">One per question<\/option>/);
  assert.match(view, /<option value="area">One per work area, its questions numbered<\/option>/);
  assert.match(view, /about every 45 seconds/);
  assert.match(view, /disabled=""[^>]*>Check access<\/button>/);
});

test("the read gate shows its line as a status, a failed read as an alert with Try again", () => {
  const gate = (state: "loading" | "failed" | "closed") => html(createElement(ReadGate, { state, text: "T", onBack: noop, onRetry: noop }));
  assert.doesNotMatch(gate("closed"), /Try again/);
  assert.match(gate("closed"), /role="status"[^>]*>T</);
  assert.match(gate("failed"), /role="alert"[^>]*>T</);
  assert.match(gate("failed"), /Try again/);
});
