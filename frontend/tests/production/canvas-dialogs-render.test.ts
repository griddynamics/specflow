// The dialogs and forms of 06b rendered on the server with plain props: a node's details (Remove, Reconnect), Build or Update the graph with
// the reasoning levels, Build the plan or Replan, the read-only Gaps list, and the GitHub, Slack and Jira forms before any answer has come.
// The presses, the ports and the stub round trips are in e2e-production/canvas-connectors-production.spec.ts.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { BuildDialog } from "@/components/v2/canvas/BuildDialog";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import { GapsDialog, gapsTitle } from "@/components/v2/canvas/GapsDialog";
import { GitHubForm, offerOf, withoutAdded } from "@/components/v2/canvas/GitHubForm";
import { gateOf, JiraForm, JiraFormBody } from "@/components/v2/canvas/JiraForm";
import { NodeDialog } from "@/components/v2/canvas/NodeDialog";
import { PlanDialog, planStartFailure } from "@/components/v2/canvas/PlanDialog";
import { SlackForm, SlackSteps, type SlackStepsProps } from "@/components/v2/canvas/SlackForm";
import type { FormProps } from "@/components/v2/connector-types";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { routesFor } from "@/lib/routes";
import { DataError } from "@/lib/domain/errors";
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
  const view = html(createElement(BuildDialog, { projectId: "p1", status, update: false, onStarted: noop, onClose: noop }));
  assert.match(view, /Graphico reads 30 files from 2 sources and builds one graph\. The gaps follow on their own\./);
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
  const view = html(createElement(BuildDialog, { projectId: "p1", status: failed, update: true, onStarted: noop, onClose: noop }));
  assert.match(view, /updates the graph\. Open gaps stay open/);
  assert.match(view, /The last build failed \(boom\)\. You can try again\./);
  assert.match(view, /gx-btn--yellow[^>]*>Update<\/button>/);
  assert.equal(
    count(
      html(createElement(BuildDialog, { projectId: "p1", status: defined(BUILT().status), update: false, onStarted: noop, onClose: noop })),
      "alert",
    ),
    0,
  );
  assert.equal(SUCCEEDED.status, "succeeded");
});

test("Build the plan names the destination and the open gaps; Replan plans every area again and keeps claimed work", () => {
  const props = { projectId: "p1", destination: null, onStarted: noop, onClose: noop };
  const view = {
    first: html(createElement(PlanDialog, { ...props, replan: false, gaps: GAPS, destination: "Jira · NWO-1", onStarted: noop })),
    again: html(createElement(PlanDialog, { ...props, replan: true, gaps: { ...GAPS, critical: 0, answered: 14 }, onStarted: noop })),
  };
  assert.match(view.first, /turns the graph into areas, stories and tasks/);
  assert.match(view.first, /They go to Jira · NWO-1 and are kept up to date\./);
  assert.match(view.first, /12 gaps are still open, 3 of them critical\./);
  assert.match(view.first, /gx-btn--yellow[^>]*>Build plan<\/button>/);
  assert.match(view.again, /plans every area again from the current graph\. Work that is claimed or finished stays\./);
  assert.doesNotMatch(view.again, /gaps are still open|They go to/);
  assert.match(view.again, />Replan<\/button>/);
});

test("the Gaps dialog counts the open gaps by category and severity, read-only, and says where they are answered", () => {
  const rows = [
    { id: "g1", title: "Refunds for paid orders", kind: "contradiction", severity: "blocker" },
    { id: "g2", title: "Reporting API owner", kind: "missing", severity: "minor" },
    { id: "g3", title: "Export contract", kind: "missing", severity: "minor" },
  ] as const;
  const view = html(createElement(GapsDialog, { rows, onClose: noop }));
  assert.match(view, /in Slack, in Jira, or by a coding agent over MCP/);
  assert.match(view, /<th scope="col">Category<\/th><th scope="col">Blocker<\/th><th scope="col">Minor<\/th><th scope="col">Total<\/th>/);
  assert.match(
    view,
    /<th scope="row">Missing<\/th><td data-zero="">0<\/td><td>2<\/td><td>2<\/td><\/tr><tr><th scope="row">Contradiction<\/th>/,
  );
  assert.doesNotMatch(view, /Refunds for paid orders/, "a summary, not the list of gaps");
  assert.doesNotMatch(view, /<input|<textarea|Answer</, "no answering in the app (D10)");
  assert.match(html(createElement(GapsDialog, { rows: [], onClose: noop })), /No gap is open\./);
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
  assert.match(view, /<a [^>]*href="\/projects\/p1\/integrations\?setup=github"[^>]*>Choose on GitHub in Integrations\.<\/a>/);
  assert.match(view, /disabled=""[^>]*>Add selected<\/button>/);
  assert.match(view, />Back</);
});

test("the Slack form shows the invite and the connect command of main with a Copy each, and waits for the channel", () => {
  const view = html(createElement(SlackForm, form("slack")));
  assert.match(view, /<code[^>]*>\/invite @SpecFlow<\/code>/);
  assert.match(view, /<code[^>]*>\/specflow connect p1<\/code>/);
  assert.equal(count(view, 'class="gx-copyrow__btn"'), 2, "a copy icon in each code box");
  assert.match(view, /aria-label="Copy invite command"/);
  assert.match(
    view,
    /<details class="gx-how">.*Show me how<\/summary><div class="gx-how__body"><ol.*Type \/invite @SpecFlow and press Enter\./,
  );
  assert.match(view, /Waiting for the channel\./);
  assert.match(view, /<li>Test it: Graphico posts a welcome message in the channel\.<\/li>/);
  assert.match(view, /disabled=""[^>]*>Test connection<\/button>/, "Test connection waits for the binding");
});

const steps = (over: Partial<SlackStepsProps> = {}): string =>
  html(
    createElement(SlackSteps, {
      projectId: "p1",
      channel: { id: "C1", name: "northwind-specs", private: false },
      unreachable: false,
      labels: { test: "Test connection", testing: "Posting…", add: "Add channel" },
      testing: false,
      result: null,
      onTest: noop,
      onAdd: noop,
      onBack: noop,
      ...over,
    }),
  );

test("Slack step 3: once the channel is bound Test connection is on; while it posts it says so; a failure keeps Test connection and says why", () => {
  const view = {
    bound: steps(),
    posting: steps({ testing: true }),
    failed: steps({ result: { ok: false, text: "The app is not in the channel any more. Invite it again." } }),
  };
  assert.match(view.bound, /#northwind-specs is connected\./);
  assert.match(view.bound, /<button type="button" class="gx-btn">Test connection<\/button>/);
  assert.match(view.posting, /aria-busy="true"[^>]*>Posting…<\/button>/);
  assert.match(view.failed, /role="alert">The app is not in the channel any more/);
  assert.match(view.failed, />Test connection<\/button>/);
  assert.doesNotMatch(view.failed, />Add channel</);
});

test("Slack step 3: a posted test names the channel and the project, and the button becomes Add channel", () => {
  const view = steps({
    result: {
      ok: true,
      text: "Posted in #northwind-specs: “Welcome to the workspace of project Northwind ordering.” If it arrived, add the channel.",
    },
  });
  assert.match(view, /Posted in #northwind-specs: “Welcome to the workspace of project Northwind ordering\.”/);
  assert.match(view, /<button type="button" class="gx-btn">Add channel<\/button>/);
  assert.doesNotMatch(view, />Test connection<\/button>/);
});

test("the Jira form asks for the space and an optional epic; 'Check access' is off until there is a space; before `jira.get` answers it only loads", () => {
  const view = { open: html(createElement(JiraFormBody, form("jira"))), loading: html(createElement(JiraForm, form("jira"))) };
  assert.match(view.open, />Jira space key<\/label>/);
  assert.match(view.open, />Epic key<\/label>/);
  assert.match(view.open, /Leave the epic empty and Graphico creates one named after the project\./);
  assert.match(view.open, /disabled=""[^>]*>Check access<\/button>/);
  assert.match(view.loading, /Loading/);
  assert.doesNotMatch(view.loading, /Jira space key|Check access/);
});

// ---- what the API says about the caller, partial adds, a plan that is already running ----

const AVAILABLE = {
  truncated: false,
  repositories: [101, 102, 103].map((githubRepoId) => ({
    githubRepoId,
    fullName: `acme/r${String(githubRepoId)}`,
    htmlUrl: "",
    private: false,
    defaultBranch: "main",
    sizeKb: 1,
    tooLarge: githubRepoId === 103,
  })),
};

test("the GitHub offer is the readable repositories the project does not have, with the size-capped one listed but blocked; `canManage` rides along", () => {
  const offer = offerOf(true, false, AVAILABLE, new Set([102]));
  assert.deepEqual(
    offer.rows.map((r) => [r.repo.githubRepoId, r.blocked]),
    [
      [101, null],
      [103, "too_large"],
    ],
  );
  assert.equal(offer.canManage, false, "what the API said about the caller is kept, not guessed");
});

test("after a partial add the offer loses the repositories that got in, the rest stay pickable", () => {
  const offer = offerOf(true, true, AVAILABLE, new Set());
  assert.deepEqual(
    withoutAdded(offer, new Set([101])).rows.map((r) => r.repo.githubRepoId),
    [102, 103],
  );
  assert.equal(withoutAdded(offer, new Set()).rows.length, 3);
});

test("the Jira form is open only for what `jira.get` allows: unavailable and read-only each say so, loading waits", () => {
  assert.equal(gateOf(null), "loading");
  assert.equal(gateOf({ available: false, editable: true }), "unavailable");
  assert.equal(gateOf({ available: true, editable: false }), "read_only");
  assert.equal(gateOf({ available: true, editable: true }), "open");
});

test("a plan start the API refuses with a conflict says a plan is already running; any other failure says it could not start", () => {
  assert.match(planStartFailure(new DataError("conflict", false)), /already being made/);
  assert.match(planStartFailure(new DataError("unavailable", true)), /could not be started/);
  assert.match(planStartFailure(new Error("x")), /could not be started/);
});

test("the Gaps title says what the list holds: the open gaps, counted as the list shows them", () => {
  assert.equal(gapsTitle([], null), "Gaps");
  assert.equal(gapsTitle([{ id: "a", title: "t", kind: "missing", severity: "blocker" }], null), "1 open gap");
  const five = Array.from(
    { length: 5 },
    (_, i) => ({ id: String(i), title: "t", kind: "missing", severity: i === 0 ? "blocker" : "minor" }) as const,
  );
  assert.equal(gapsTitle(five, null), "5 open gaps");
  // With the API's counts the title is the total of open gaps, not the first page: 14 found, 2 settled = 12 open, "+" when the page was full.
  assert.equal(gapsTitle(five, { ...GAPS, total: 14, answered: 2, more: false }), "12 open gaps");
  assert.equal(gapsTitle(five, { ...GAPS, total: 200, answered: 2, more: true }), "198+ open gaps");
  assert.equal(gapsTitle(five, { ...GAPS, total: 3, answered: 3, more: false }), "Gaps");
  assert.equal(gapsTitle(five, { ...GAPS, total: 1, answered: 0, more: false }), "1 open gap");
  const view = html(createElement(GapsDialog, { rows: five, onClose: noop }));
  assert.match(view, /1 of them critical: they block the work they are about until they are answered\./);
  const minor = [{ id: "m", title: "t", kind: "missing", severity: "minor" }] as const;
  assert.doesNotMatch(html(createElement(GapsDialog, { rows: minor, onClose: noop })), /of them critical/);
});

test("the Slack form carries V1's note about who runs the command and how the account is linked", () => {
  const view = html(createElement(SlackForm, form("slack")));
  assert.match(view, /Add SpecFlow to the channel first \(\/invite @SpecFlow\)\. Only you, the owner, can run it/);
});

test("a repository the caller cannot manage has no Remove in its dialog", () => {
  const node = nodeOf(BUILT({ github: repos([repo()], false) }), "github");
  const view = html(createElement(NodeDialog, { node, projectId: "p1", routes, onChanged: noop, onClose: noop }));
  assert.doesNotMatch(view, />Remove</);
  assert.match(view, />Done</);
});

test("an upload's dialog: the files inside (loading first), Rename beside Remove; a Drive folder's has the files but no Rename", () => {
  const dialogOf = (kind: string, input = SCENE_3): string =>
    html(createElement(NodeDialog, { node: nodeOf(input, kind), projectId: "p1", routes, onChanged: noop, onClose: noop }));
  const view = {
    upload: dialogOf("upload"),
    drive: dialogOf("gdrive"),
    repository: dialogOf("github", BUILT({ github: repos([repo()]), integrations: [integration("github", { health: "active" })] })),
  };
  assert.match(view.upload, /<b>Files<\/b>/);
  assert.match(view.upload, />Loading</);
  assert.match(view.upload, />Rename<\/button>/);
  assert.match(view.upload, />Remove<\/button>/);
  assert.match(view.drive, /<b>Files<\/b>/);
  assert.doesNotMatch(view.drive, />Rename</);
  assert.doesNotMatch(view.repository, /<b>Files<\/b>/, "a repository is not a source of the sources API");
});
