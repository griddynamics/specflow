// The canvas and its plus dialog rendered on the server with plain props (06a Do 2 and 3): the three zones on one line, the hubs with no name
// under them, one yellow decision at most, no heading in the body, the plus list of a stage with "Set up first", the Drive form and the MCP
// dialog. The browser behaviour (presses, dialogs, the round trip, motion) is in e2e-production/canvas-production.spec.ts.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AddList } from "@/components/v2/canvas/AddDialog";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import { CanvasView, type CanvasViewProps } from "@/components/v2/canvas/CanvasView";
import { DriveForm } from "@/components/v2/canvas/DriveForm";
import { McpForm } from "@/components/v2/canvas/McpForm";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import type { ConnectorSpec, FormProps } from "@/components/v2/connector-types";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { routesFor } from "@/lib/routes";
import { defined } from "../support/defined";
import { BUILT, GAPS, input, READY_PLAN, SCENE_1, SCENE_2, SCENE_3 } from "./canvas-fixtures";
import type { CanvasInput } from "@/components/v2/canvas/canvas-types";

const noop = () => undefined;
const ui = createUiStore();
const ports: Partial<DataPorts> = {}; // the parts under test read no port while rendering on the server
const context: DataContextValue = { ui, ports: ports as DataPorts, routes: routesFor("production"), handoff: createUiHandoff(ui) };
const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));

const canvas = (i: CanvasInput): string => {
  const props: CanvasViewProps = {
    model: buildCanvasModel(i, NODE_MAPPERS),
    geometry: {
      width: 1280,
      height: 720,
      wires: [],
      backdrop: { width: 1280, height: 720, dots: [], lines: [], holes: [], clearPaths: [] },
    },
    rootRef: noop,
    onAdd: noop,
    onNode: (n) => (n.kind === "mcp" ? noop : undefined),
    onHub: () => undefined,
  };
  return html(createElement(CanvasView, props));
};
const YELLOW = 'data-yellow="true"';
const count = (text: string, part: string) => text.split(part).length - 1;

test("scene 1: three zones in order on one canvas, three node-sized plus tiles, no heading, no yellow", () => {
  const view = canvas(SCENE_1);
  assert.deepEqual(
    [...view.matchAll(/data-zone="(\w+)"/g)].map((m) => m[1]),
    ["graph", "gap", "plan"],
  );
  assert.deepEqual(
    [...view.matchAll(/gx-zone__label[^>]*>([^<]+)</g)].map((m) => m[1]),
    ["01 · Graph", "02 · Gap", "03 · Plan"],
  );
  assert.equal(count(view, "gx-add--big"), 3);
  assert.equal(count(view, "Add source"), 1);
  assert.equal(count(view, "Add channel"), 1);
  assert.equal(count(view, "Add destination"), 1);
  assert.equal(count(view, YELLOW), 0);
  assert.equal(count(view, "<h1"), 0);
  assert.equal(count(view, "<h2"), 0);
  assert.match(view, /aria-label="Mission Control canvas"/);
});

test("hubs carry no name or count under them: the only text under a hub is a decision", () => {
  const idle = canvas(BUILT({ gaps: GAPS, plan: READY_PLAN }));
  assert.equal(count(idle, "gx-hub__decision"), 0);
  for (const name of ["Graph", "Gaps", "Plan"]) assert.equal(idle.includes(`>${name}<`), false, name);
  assert.match(idle, /title="14 found · 2 answered"/);
  assert.match(idle, />3 critical</);
  const open = canvas(SCENE_3);
  assert.equal(count(open, "gx-hub__decision"), 1);
  assert.match(open, /gx-hub__decision[^>]*>Build graph</);
});

test("exactly one yellow control per view, whichever decision comes first", () => {
  assert.equal(count(canvas(SCENE_3), YELLOW), 1);
  assert.equal(count(canvas(BUILT({ gaps: GAPS })), YELLOW), 1);
  assert.match(canvas(BUILT({ gaps: GAPS })), /gx-hub__decision[^>]*>Build plan</);
  assert.equal(count(canvas(BUILT({ gaps: GAPS, plan: READY_PLAN, planStale: true })), YELLOW), 1);
  assert.equal(count(canvas(SCENE_2), YELLOW), 0);
});

test("importing sources show a bar and a spinner badge; MCP nodes are Automatic and the only pressable ones", () => {
  const importing = canvas(SCENE_2);
  assert.equal(count(importing, 'data-badge="spin"'), 2);
  assert.equal(count(importing, "gx-cnode__bar"), 2);
  assert.equal(count(importing, "<button"), 3, "the three plus tiles are the only buttons");
  const full = canvas(BUILT({ gaps: GAPS, plan: READY_PLAN }));
  assert.equal(count(full, "Automatic"), 2);
  assert.equal(count(full, 'data-kind="mcp"'), 2);
  assert.match(full, /<button[^>]*aria-label="MCP, added automatically: how to connect a coding agent"/);
});

const spec = (kind: string): ConnectorSpec => defined(CONNECTORS.find((c) => c.kind === kind));
const addList = (
  zone: "graph" | "gap" | "plan",
  connectors: readonly ConnectorSpec[],
  usable: boolean | ((c: ConnectorSpec) => boolean),
): string =>
  html(
    createElement(AddList, {
      zone,
      connectors,
      usable: typeof usable === "boolean" ? () => usable : usable,
      appName: () => "GitHub",
      onPick: noop,
      onSetUp: noop,
    }),
  );

test("the plus list shows its own stage first, the others dashed under 'Connections for the other stages'; MCP is not listed", () => {
  const view = {
    graph: addList("graph", CONNECTORS, true),
    gap: addList("gap", CONNECTORS, true),
    none: addList("gap", [spec("upload")], true),
  };
  assert.deepEqual(
    [...view.graph.matchAll(/<b>([^<]+)<\/b>/g)].map((m) => m[1]),
    ["Upload", "Google Drive", "GitHub repository", "Confluence link", "Jira epic", "Slack channel", "Jira"],
    "its own stage first, then the others",
  );
  assert.equal(count(view.graph, "Available"), 5);
  assert.equal(count(view.graph, "MCP"), 0);
  assert.match(view.gap, /<b>Slack channel<\/b>/);
  assert.doesNotMatch(view.gap, /Nothing can be added/);
  assert.match(view.none, /Nothing can be added to this stage yet/);
  assert.match(view.gap, /Connections for the other stages<\/summary>/);
  assert.equal(count(view.gap, 'aria-disabled="true"'), 6);
  assert.equal(count(view.gap, "For Graph"), 5);
  assert.equal(count(view.gap, "For Plan"), 1);
});

test("a connection whose app is not connected says 'Set up first' and names the app", () => {
  const view = addList("graph", CONNECTORS, (c) => c.app === null);
  assert.match(view, /Set up first/);
  assert.match(view, /Connect GitHub in Integrations first\./);
  assert.equal(count(view, "Cloned from the project"), 0, "the line gives way to the instruction");
  assert.equal(count(view, "Available"), 2, "Upload and Drive need no app");
});

const formProps = (spec: ConnectorSpec, zone: "graph" | "gap" | "plan"): FormProps => ({
  projectId: "p1",
  zone,
  spec,
  mcp: { mcpUrl: "https://mcp.example.com/mcp", clientId: "specflow-test", serverName: "specflow" },
  reload: { current: noop },
  onBack: noop,
  onClose: noop,
});

test("the MCP dialog gives the Guides' commands and the tools of the stage", () => {
  const view = {
    gap: html(createElement(McpForm, formProps(spec("mcp"), "gap"))),
    plan: html(createElement(McpForm, formProps(spec("mcp"), "plan"))),
  };
  assert.match(
    view.gap,
    /claude mcp add --transport http --scope user --client-id specflow-test --callback-port 8765 specflow https:\/\/mcp\.example\.com\/mcp/,
  );
  assert.match(view.gap, /\/mcp__specflow__select_project p1/);
  for (const tool of ["list_gaps", "answer_gap", "add_finding"]) assert.match(view.gap, new RegExp(`>${tool}<`));
  assert.equal(view.gap.includes(">claim_story<"), false);
  for (const tool of ["list_areas", "claim_story", "complete_story"]) assert.match(view.plan, new RegExp(`>${tool}<`));
  assert.equal(view.plan.includes(">answer_gap<"), false);
});

test("the Drive form: the account, the guide, the link field and 'Test connection' that is off until there is a link", () => {
  const view = html(createElement(DriveForm, formProps(spec("gdrive"), "graph")));
  assert.match(view, /Share the folder with this account as a Viewer:/);
  assert.match(view, /How to share a folder \(4 steps\)/);
  assert.match(view, /<label[^>]*>Google Drive folder link<\/label>/);
  assert.match(
    view,
    /<button[^>]*disabled=""[^>]*type="submit"[^>]*>Test connection<|<button[^>]*type="submit"[^>]*disabled=""[^>]*>Test connection</,
  );
  assert.match(view, />Back</);
  assert.equal(input({}).now instanceof Date, true);
});
