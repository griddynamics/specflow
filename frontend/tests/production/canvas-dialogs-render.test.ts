// The dialogs and forms of 06b rendered on the server with plain props: a node's details (Remove, Reconnect), Build or Update the graph with
// the reasoning levels, Build the plan or Replan, the read-only Gaps list, and the GitHub, Slack and Jira forms before any answer has come.
// The presses, the ports and the stub round trips are in e2e-production/canvas-connectors-production.spec.ts.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { BuildDialog } from "@/components/v2/canvas/BuildDialog";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import { GapsDialog } from "@/components/v2/canvas/GapsDialog";
import { GitHubForm } from "@/components/v2/canvas/GitHubForm";
import { JiraForm } from "@/components/v2/canvas/JiraForm";
import { NodeDialog } from "@/components/v2/canvas/NodeDialog";
import { PlanDialog } from "@/components/v2/canvas/PlanDialog";
import { SlackForm } from "@/components/v2/canvas/SlackForm";
import type { FormProps } from "@/components/v2/connector-types";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { routesFor } from "@/lib/routes";
import { defined } from "../support/defined";
import { BUILT, GAPS, integration, repo, repos, SCENE_3, SUCCEEDED, statusOf, wireSource, graphWire } from "./canvas-fixtures";

const noop = () => undefined;
const ui = createUiStore();
const ports: Partial<DataPorts> = {};
const routes = routesFor("production");
const context: DataContextValue = { ui, ports: ports as DataPorts, routes, handoff: createUiHandoff(ui) };
const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));
const count = (text: string, part: string) => text.split(part).length - 1;
const nodeOf = (input: Parameters<typeof buildCanvasModel>[0], kind: string) =>
  defined(
    Object.values(buildCanvasModel(input, NODE_MAPPERS).zones)
      .flatMap((z) => z.nodes)
      .find((n) => n.kind === kind),
  );

test("a source's dialog: the state, who added it, Remove and Done; no Reconnect", () => {
  const node = nodeOf(SCENE_3, "gdrive");
  const view = html(createElement(NodeDialog, { node, projectId: "p1", routes, onChanged: noop, onClose: noop }));
  assert.match(view, /24 files\. Imported when it was added/);
  assert.match(view, /Added by dev/);
  assert.match(view, />Remove<\/button>/);
  assert.match(view, />Done<\/button>/);
  assert.doesNotMatch(view, /Reconnect/);
  assert.doesNotMatch(view, /ConfirmPanel|alertdialog/, "the confirmation only after Remove is pressed");
});

test("a stopped repository's dialog offers to reconnect GitHub on the Integrations page, and returns to the canvas", () => {
  const stopped = BUILT({ github: repos([repo()]), integrations: [integration("github", { health: "reconnect_required" })] });
  const view = html(
    createElement(NodeDialog, { node: nodeOf(stopped, "github"), projectId: "p1", routes, onChanged: noop, onClose: noop }),
  );
  assert.match(view, /<a [^>]*href="\/projects\/p1\/integrations\?setup=github&amp;next=%2Fprojects%2Fp1"[^>]*>Reconnect GitHub<\/a>/);
  assert.match(view, /no access now/);
  assert.match(view, />Remove<\/button>/);
  assert.doesNotMatch(view, />Done</);
});

test("a node of an app has no Remove: the channel and the destination are changed on the Integrations page", () => {
  const view = html(
    createElement(NodeDialog, {
      node: nodeOf(BUILT({ slack: { available: true, editable: true, channel: { id: "C1", name: "specs", private: false } } }), "slack"),
      projectId: "p1",
      routes,
      onChanged: noop,
      onClose: noop,
    }),
  );
  assert.doesNotMatch(view, />Remove</);
  assert.match(view, /Gaps are posted here/);
});

const status = defined(SCENE_3.status);

test("Build the graph: five reasoning levels with Medium pressed, the V1 estimate, and the yellow Build", () => {
  const view = html(createElement(BuildDialog, { projectId: "p1", status, update: false, onClose: noop }));
  assert.match(view, /Graphico reads 30 documents from 2 sources and builds one graph\. The gaps follow on their own\./);
  assert.deepEqual(
    [...view.matchAll(/<button[^>]*aria-pressed="(true|false)"[^>]*>([^<]+)<\/button>/g)].map((m) => [m[2], m[1]]),
    [
      ["Low", "false"],
      ["Medium", "true"],
      ["High", "false"],
      ["Extra high", "false"],
      ["Max", "false"],
    ],
  );
  assert.match(view, /Takes about \d+–\d+ minutes\. Higher levels read more carefully and take longer\./);
  assert.match(view, /gx-btn--yellow[^>]*>Build<\/button>/);
  assert.match(view, />Cancel</);
});

test("Update the graph says open gaps stay open and the button is 'Update'; a failed last build is said with its code", () => {
  const failed = statusOf(
    [wireSource("s1", "gdrive", { done: 5 })],
    graphWire({
      status: "failed",
      mode: "first",
      errorCode: "boom",
      startedAt: "2026-10-26T09:00:00Z",
      finishedAt: "2026-10-26T09:01:00Z",
    }),
    5,
  );
  const view = html(createElement(BuildDialog, { projectId: "p1", status: failed, update: true, onClose: noop }));
  assert.match(view, /updates the graph\. Open gaps stay open/);
  assert.match(view, /The last build failed \(boom\)\. You can try again\./);
  assert.match(view, /gx-btn--yellow[^>]*>Update<\/button>/);
  assert.equal(
    count(html(createElement(BuildDialog, { projectId: "p1", status: defined(BUILT().status), update: false, onClose: noop })), "alert"),
    0,
  );
  assert.equal(SUCCEEDED.status, "succeeded");
});

test("Build the plan names the destination and the open gaps; Replan plans every area again and keeps claimed work", () => {
  const props = { projectId: "p1", destination: null, onClose: noop };
  const view = {
    first: html(createElement(PlanDialog, { ...props, replan: false, gaps: GAPS, destination: "Jira · NWO-1" })),
    again: html(createElement(PlanDialog, { ...props, replan: true, gaps: { ...GAPS, critical: 0, answered: 14 } })),
  };
  assert.match(view.first, /turns the graph into areas, stories and tasks/);
  assert.match(view.first, /They go to Jira · NWO-1 and are kept up to date\./);
  assert.match(view.first, /12 gaps are still open, 3 of them critical\./);
  assert.match(view.first, /gx-btn--yellow[^>]*>Build plan<\/button>/);
  assert.match(view.again, /plans every area again from the current graph\. Work that is claimed or finished stays\./);
  assert.doesNotMatch(view.again, /gaps are still open|They go to/);
  assert.match(view.again, />Replan<\/button>/);
});

test("the Gaps dialog lists the open gaps with their kind and severity, read-only, and says where they are answered", () => {
  const rows = [
    { id: "g1", title: "Refunds for paid orders", kind: "contradiction", severity: "blocker" },
    { id: "g2", title: "Reporting API owner", kind: "missing", severity: "minor" },
  ] as const;
  const view = html(createElement(GapsDialog, { rows, onClose: noop }));
  assert.match(view, /in Slack, in Jira, or by a coding agent over MCP/);
  assert.match(view, /<b>Refunds for paid orders<\/b><small>Contradiction · Blocker<\/small>/);
  assert.match(view, /<b>Reporting API owner<\/b><small>Missing · Minor<\/small>/);
  assert.doesNotMatch(view, /<input|<textarea|Answer</, "no answering in the app (D10)");
  assert.match(html(createElement(GapsDialog, { rows: [], onClose: noop })), /No gap is open\./);
  const many = Array.from(
    { length: 53 },
    (_, i) => ({ id: `g${String(i)}`, title: `Gap ${String(i)}`, kind: "missing", severity: "minor" }) as const,
  );
  const long = { view: html(createElement(GapsDialog, { rows: many, onClose: noop })) };
  assert.equal(count(long.view, "<li>"), 50);
  assert.match(long.view, /and 3 more/);
});

const form = (kind: string): FormProps => ({
  projectId: "p1",
  zone: "graph",
  spec: defined(CONNECTORS.find((c) => c.kind === kind)),
  mcp: { mcpUrl: "https://mcp.example.com/mcp", clientId: "c", serverName: "specflow" },
  reload: { current: noop },
  onBack: noop,
  onClose: noop,
});

test("the GitHub form loads, says what a pick does and points to Integrations for the choice on GitHub", () => {
  const view = html(createElement(GitHubForm, form("github")));
  assert.match(view, /Loading/);
  assert.match(view, /Each repository becomes its own node\. It is cloned; the build does not read it yet\./);
  assert.match(view, /<a [^>]*href="\/projects\/p1\/integrations\?setup=github"[^>]*>Choose repositories on GitHub in Integrations\.<\/a>/);
  assert.match(view, /disabled=""[^>]*>Add repositories<\/button>/);
  assert.match(view, />Back</);
});

test("the Slack form shows the invite and the connect command of main with a Copy each, and waits for the channel", () => {
  const view = html(createElement(SlackForm, form("slack")));
  assert.match(view, /<code[^>]*>\/invite @SpecFlow<\/code>/);
  assert.match(view, /<code[^>]*>\/specflow connect p1<\/code>/);
  assert.equal(count(view, ">Copy<"), 2);
  assert.match(view, /Waiting for the channel\./);
  assert.doesNotMatch(view, /Test connection/, "the test message arrives with session 07");
});

test("the Jira form asks for the space and an optional epic; 'Check access' is off until there is a space", () => {
  const view = html(createElement(JiraForm, form("jira")));
  assert.match(view, />Jira space key<\/label>/);
  assert.match(view, />Epic key<\/label>/);
  assert.match(view, /Leave the epic empty and Graphico creates one named after the project\./);
  assert.match(view, /disabled=""[^>]*>Check access<\/button>/);
});
