// The Confluence link source on the canvas (08F): what its nodes say in each state the sources API reports, the words of a failed check or add,
// the line of a passed check, and the form before any answer. Pure and server-rendered: no browser.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { NodeView } from "@/components/v2/canvas/canvas-types";
import { confluenceCheckText, confluenceFailureText } from "@/components/v2/canvas/confluence-logic";
import { ConfluenceForm } from "@/components/v2/canvas/ConfluenceForm";
import { unitText } from "@/components/v2/canvas/nodes-sources";
import type { FormProps } from "@/components/v2/connector-types";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { DataError } from "@/lib/domain/errors";
import { routesFor } from "@/lib/routes";
import { defined } from "../support/defined";
import { input, statusOf, wireSource } from "./canvas-fixtures";

const nodeOf = (counts: Parameters<typeof wireSource>[2], over: Record<string, unknown> = {}): NodeView => {
  const i = input({ status: statusOf([wireSource("c1", "confluence", counts, { name: "Ordering", ...over })]) });
  return defined(buildCanvasModel(i, NODE_MAPPERS).zones.graph.nodes[0]);
};

test("a Confluence source importing shows pages with a bar, a spinner and a moving wire; listing says so", () => {
  const busy = nodeOf({ done: 3, processing: 9 });
  assert.deepEqual([busy.name, busy.state, busy.badge, busy.wire, busy.meta], ["Ordering", "busy", "spin", "flow", "3 of 12 pages"]);
  assert.equal(busy.progress, 0.25);
  assert.equal(busy.mark, "confluence");
  const listing = nodeOf({}, { listing_finished: false });
  assert.deepEqual([listing.listing, listing.detail.text], [true, "Listing files…"]);
});

test("a finished Confluence source is ink with the page count and the app; its dialog says it was read once", () => {
  const done = nodeOf({ done: 42 });
  assert.deepEqual([done.state, done.badge, done.meta, done.wire], ["on", "ok", "42 pages · Confluence", "on"]);
  assert.match(done.detail.text, /^42 pages\. Imported when it was added/);
  assert.deepEqual([done.detail.app, done.detail.remove, done.detail.reconnect], ["Confluence", "source", null]);
  assert.equal(nodeOf({ done: 1 }).meta, "1 page · Confluence");
  assert.match(done.label, /^Confluence Ordering: 42 pages$/);
});

test("a Confluence import the API reports failed stops: no access offers to reconnect Atlassian, any other reason says it failed", () => {
  const noAccess = nodeOf({ failed: 1 }, { failure_reason: "no_access" });
  assert.deepEqual(
    [noAccess.state, noAccess.badge, noAccess.wire, noAccess.meta, noAccess.detail.reconnect],
    ["stopped", "stop", "off", "Cannot read it", "atlassian"],
  );
  assert.match(noAccess.detail.text, /Another member reconnects Atlassian/);
  const failed = nodeOf({ failed: 1 }, { failure_reason: "unknown_error" });
  assert.deepEqual([failed.meta, failed.detail.reconnect], ["Import failed", null]);
  assert.match(failed.detail.text, /Add the link again/);
});

test("the unit follows the kind: Confluence counts pages, the others files", () => {
  assert.equal(unitText("confluence", "3 of 12 files"), "3 of 12 pages");
  assert.equal(unitText("confluence", "1 file"), "1 page");
  assert.equal(unitText("gdrive", "3 of 12 files"), "3 of 12 files");
});

const failure = (apiCode: string) => confluenceFailureText(new DataError("validation", false, { apiCode }));

test("each closed code of the check and the add has its own words; an unknown one is the general failure", () => {
  assert.match(failure("atlassian_not_connected"), /Connect Atlassian in Integrations first/);
  assert.match(failure("atlassian_reconnect_required"), /needs a reconnect/);
  assert.match(failure("atlassian_site_mismatch"), /another Atlassian site/);
  assert.match(failure("confluence_link_invalid"), /not a link to a Confluence page or space/);
  assert.match(failure("confluence_short_link_unsupported"), /Paste the full address/);
  assert.match(failure("confluence_not_found"), /no page or space at this link/);
  assert.match(failure("confluence_forbidden"), /not allowed to read/);
  assert.match(failure("atlassian_unreachable"), /did not answer/);
  assert.match(failure("atlassian_oauth_unavailable"), /not set up on this server/);
  assert.equal(failure("something_new"), "Something went wrong. Try again.");
  assert.equal(confluenceFailureText(new Error("x")), "Something went wrong. Try again.");
});

test("a passed check says what is there: a page, a space, the cap, and the linked issues only when links are followed", () => {
  const page = { kind: "page", title: "Checkout notes", pages: 4, capped: false, linkedIssues: 3 } as const;
  assert.equal(confluenceCheckText(page, true), "Page Checkout notes · 4 pages, 3 linked Jira issues.");
  assert.equal(confluenceCheckText(page, false), "Page Checkout notes · 4 pages.");
  assert.equal(confluenceCheckText({ ...page, pages: 1, linkedIssues: 1 }, true), "Page Checkout notes · 1 page, 1 linked Jira issue.");
  assert.equal(
    confluenceCheckText({ kind: "space", title: "Ordering", pages: 42, capped: false, linkedIssues: 0 }, true),
    "Space Ordering · 42 pages.",
  );
  assert.match(
    confluenceCheckText({ kind: "space", title: "Big", pages: 100, capped: true, linkedIssues: 0 }, false),
    /100 pages \(the first ones; more were found\)/,
  );
});

const noop = () => undefined;
const ports: Partial<DataPorts> = {};
const ui = createUiStore();
const context: DataContextValue = { ui, ports: ports as DataPorts, routes: routesFor("production"), handoff: createUiHandoff(ui) };
const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));

test("the registry has the Confluence link in Graph, needing Atlassian, with its check labels", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "confluence"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["graph", "atlassian", "confluence", "Confluence link"]);
  assert.deepEqual(spec.check, { test: "Check link", testing: "Checking…", add: "Add source" });
});

test("the Confluence form asks for the link and Follow links (on), says it reads once, and Check link is off until there is a link", () => {
  const props: FormProps = {
    projectId: "p1",
    zone: "graph",
    spec: defined(CONNECTORS.find((c) => c.kind === "confluence")),
    mcp: { mcpUrl: "", clientId: "", serverName: "" },
    reload: { current: noop },
    onBack: noop,
    onClose: noop,
  };
  const view = html(createElement(ConfluenceForm, props));
  assert.match(view, />Confluence page or space link<\/label>/);
  assert.match(view, /<input type="checkbox" checked=""/);
  assert.match(view, /Follow links to other pages and Jira issues/);
  assert.match(view, /read once when it is added/);
  assert.match(view, /disabled=""[^>]*>Check link<\/button>/);
});
