// The diagrams.net link source on the canvas: what its nodes say in each state the sources API reports, the words of a failed check or add
// (with the storage named), the line of a passed check, the registry entry and the form before any answer. Pure and server-rendered: no browser.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { NodeView } from "@/components/v2/canvas/canvas-types";
import { drawioCheckText, drawioFailureText, drawioStorage } from "@/components/v2/canvas/drawio-logic";
import { DrawioForm } from "@/components/v2/canvas/DrawioForm";
import { unitText } from "@/components/v2/canvas/nodes-sources";
import type { FormProps } from "@/components/v2/connector-types";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { DataError } from "@/lib/domain/errors";
import type { DrawioCheck } from "@/lib/domain/sources";
import { routesFor } from "@/lib/routes";
import { defined } from "../support/defined";
import { input, statusOf, wireSource } from "./canvas-fixtures";

const nodeOf = (counts: Parameters<typeof wireSource>[2], over: Record<string, unknown> = {}): NodeView => {
  const i = input({ status: statusOf([wireSource("d1", "drawio", counts, { name: "Release flow", ...over })]) });
  return defined(buildCanvasModel(i, NODE_MAPPERS).zones.graph.nodes[0]);
};

test("a diagrams.net node is named after the diagram, shows its mark and counts pages while it imports", () => {
  const busy = nodeOf({ done: 1, processing: 1 });
  assert.deepEqual([busy.name, busy.state, busy.badge, busy.wire, busy.meta], ["Release flow", "busy", "spin", "flow", "1 of 2 pages"]);
  assert.equal(busy.mark, "drawio");
  const done = nodeOf({ done: 2 });
  assert.deepEqual([done.state, done.badge, done.meta, done.detail.app], ["on", "ok", "2 pages · diagrams.net", "diagrams.net"]);
  assert.equal(nodeOf({ done: 1 }).meta, "1 page · diagrams.net");
});

test("a diagrams.net import that failed stops and says to add the link again; Remove removes the source", () => {
  const failed = nodeOf({ failed: 1 }, { failure_reason: "unknown_error" });
  assert.deepEqual(
    [failed.state, failed.meta, failed.detail.remove, failed.detail.reconnect],
    ["stopped", "Import failed", "source", null],
  );
  assert.match(failed.detail.text, /Add the link again/);
});

test("the unit follows the kind: a diagram counts pages", () => {
  assert.equal(unitText("drawio", "1 of 2 files"), "1 of 2 pages");
});

const BASE = "https://app.diagrams.net/";
const failure = (apiCode: string, link = `${BASE}#Rabc`) => drawioFailureText(new DataError("validation", false, { apiCode }), link);

test("each closed code has its own words; an unknown one is the general failure", () => {
  assert.equal(failure("drawio_link_invalid"), "Not a diagrams.net link");
  assert.match(failure("drawio_fetch_failed"), /could not be fetched/);
  assert.match(failure("drawio_not_a_diagram"), /does not hold a diagram/);
  assert.match(failure("drawio_too_large"), /too large/);
  assert.equal(failure("something_new"), "Something went wrong. Try again.");
  assert.equal(drawioFailureText(new Error("x"), BASE), "Something went wrong. Try again.");
});

test("an unsupported storage is named from the link and the words send the reader to Upload", () => {
  const names: Record<string, string> = {
    G: "Google Drive",
    H: "GitHub",
    A: "GitLab",
    W: "OneDrive",
    T: "Trello",
    D: "Dropbox",
    L: "your browser",
  };
  for (const [letter, name] of Object.entries(names)) {
    assert.equal(
      failure("drawio_link_unsupported", `${BASE}#${letter}1abc`),
      `This diagram is stored in ${name}. Download it as .drawio (File → Export as → XML / .drawio) and add it with Upload.`,
    );
  }
  assert.match(failure("drawio_link_unsupported", `${BASE}#Zx`), /stored in a storage that cannot be read\./);
  assert.equal(drawioStorage("not a link"), "a storage that cannot be read");
});

const check: DrawioCheck = {
  name: "Release flow",
  pages: [
    { name: "Main", shapes: 41, arrows: 41 },
    { name: "Details", shapes: 12, arrows: 9 },
  ],
  capped: false,
};

test("a passed check says the name, the pages and the shapes; a cap says only part is read", () => {
  assert.equal(drawioCheckText(check), "Release flow · 2 pages · 53 shapes");
  assert.equal(drawioCheckText({ name: "One", pages: [{ name: "P", shapes: 1, arrows: 0 }], capped: false }), "One · 1 page · 1 shape");
  assert.equal(drawioCheckText({ ...check, capped: true }), "Release flow · 2 pages · 53 shapes. Only part of this diagram is read.");
});

const noop = () => undefined;
const ports: Partial<DataPorts> = {};
const ui = createUiStore();
const context: DataContextValue = { ui, ports: ports as DataPorts, routes: routesFor("production"), handoff: createUiHandoff(ui) };
const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));

test("the registry has the diagrams.net link in Graph, needing no app, with its check labels", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "drawio"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["graph", null, "drawio", "diagrams.net link"]);
  assert.deepEqual(spec.check, { test: "Check link", testing: "Checking…", add: "Add source" });
});

test("the form is one labelled link field, with Check link off until there is a link, and no repeated header text", () => {
  const props: FormProps = {
    projectId: "p1",
    zone: "graph",
    spec: defined(CONNECTORS.find((c) => c.kind === "drawio")),
    mcp: { mcpUrl: "", clientId: "", serverName: "" },
    reload: { current: noop },
    onBack: noop,
    onClose: noop,
  };
  const view = html(createElement(DrawioForm, props));
  assert.match(view, /<label [^>]*for="([^"]+)"[^>]*>Diagram link<\/label>/);
  assert.match(view, /<input [^>]*type="text"/);
  assert.doesNotMatch(view, /maxLength|maxlength/);
  assert.match(view, /disabled=""[^>]*>Check link<\/button>/);
  assert.doesNotMatch(view, /Add a diagrams\.net link/);
  assert.doesNotMatch(view, /aria-invalid/);
  assert.match(view, /File → Publish → Link/);
  assert.match(view, /Paste it here and press Check link/);
});
