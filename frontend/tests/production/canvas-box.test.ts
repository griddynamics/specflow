// The Box link source on the canvas: what its nodes say in each state the sources API reports, where a node opens, the words of a failed check or add,
// the line of a passed check and the file list, the toast of the way back from Box, the form before any answer and the Box card of the Integrations page. Pure and
// server-rendered: no browser.
import "../support/css-stub-first.mjs"; // the components under test pull Graphico styles (.css)
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { boxCheckText, boxFailureText, boxOpen, boxToast, folderHint, idOfFile, selectionText } from "@/components/v2/canvas/box-logic";
import { BoxForm } from "@/components/v2/canvas/BoxForm";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { NodeView } from "@/components/v2/canvas/canvas-types";
import { checkAll, checkNone, excludeOf, toggle } from "@/components/v2/canvas/pick-logic";
import type { FormProps } from "@/components/v2/connector-types";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { DataError } from "@/lib/domain/errors";
import type { BoxCheck, BoxCheckFile } from "@/lib/domain/sources";
import { routesFor } from "@/lib/routes";
import { defined } from "../support/defined";
import { input, statusOf, wireSource } from "./canvas-fixtures";

const LINK = "https://app.box.com/folder/12345";
const nodeOf = (counts: Parameters<typeof wireSource>[2], over: Record<string, unknown> = {}): NodeView => {
  const i = input({ status: statusOf([wireSource("c1", "box", counts, { link: LINK, ...over })]) });
  return defined(buildCanvasModel(i, NODE_MAPPERS).zones.graph.nodes[0]);
};

test("a Box source importing shows files with a bar, a spinner and a moving wire; listing says so", () => {
  const busy = nodeOf({ done: 3, processing: 9 });
  assert.deepEqual([busy.name, busy.state, busy.badge, busy.wire, busy.meta], ["Project files", "busy", "spin", "flow", "3 of 12 files"]);
  assert.equal(busy.progress, 0.25);
  assert.equal(busy.mark, "box");
  const listing = nodeOf({}, { listing_finished: false });
  assert.deepEqual([listing.listing, listing.detail.text], [true, "Listing files…"]);
});

test("a finished Box source is ink with the file count and the app; its dialog says it was read once and opens the Box link", () => {
  const done = nodeOf({ done: 42 });
  assert.deepEqual([done.state, done.badge, done.meta, done.wire], ["on", "ok", "42 files · Box", "on"]);
  assert.match(done.detail.text, /^42 files\. Imported when it was added/);
  assert.deepEqual([done.detail.app, done.detail.remove, done.detail.reconnect], ["Box", "source", null]);
  assert.deepEqual(done.detail.open, { href: LINK, label: "Open in Box" });
  assert.equal(nodeOf({ done: 1 }).meta, "1 file · Box");
  assert.match(done.label, /^Box Project files: 42 files$/);
});

test("a Box import the API reports failed stops: no access offers to reconnect Box, any other reason says it failed", () => {
  const noAccess = nodeOf({ failed: 1 }, { failure_reason: "no_access" });
  assert.deepEqual(
    [noAccess.state, noAccess.badge, noAccess.wire, noAccess.meta, noAccess.detail.reconnect],
    ["stopped", "stop", "off", "Cannot read it", "box"],
  );
  assert.match(noAccess.detail.text, /Another member reconnects Box/);
  const failed = nodeOf({ failed: 1 }, { failure_reason: "unknown_error" });
  assert.deepEqual([failed.meta, failed.detail.reconnect], ["Import failed", null]);
  assert.match(failed.detail.text, /Add the link again/);
});

test("a node opens only an https link on Box; anything else is no link", () => {
  assert.deepEqual(boxOpen("https://acme.app.box.com/file/9"), { href: "https://acme.app.box.com/file/9", label: "Open in Box" });
  for (const bad of [
    null,
    "",
    "javascript:alert(1)",
    `http://app.box.com/folder/1`,
    "https://evil.example.com/folder/1",
    "https://app.box.com.evil.test/x",
  ]) {
    assert.equal(boxOpen(bad), undefined, String(bad));
  }
  assert.equal(nodeOf({ done: 1 }, { link: null }).detail.open, undefined);
  assert.equal(
    defined(
      buildCanvasModel(input({ status: statusOf([wireSource("c2", "confluence", { done: 1 }, { link: "https://x" })]) }), NODE_MAPPERS)
        .zones.graph.nodes[0],
    ).detail.open,
    undefined,
  );
});

const failure = (apiCode: string) => boxFailureText(new DataError("validation", false, { apiCode }));

test("each closed code of the check and the add has its own words; an unknown one is the general failure", () => {
  assert.match(failure("box_link_invalid"), /not a link to a Box folder, file or shared link/);
  assert.match(failure("box_link_password"), /has a password/);
  assert.match(failure("box_no_access"), /cannot open this folder or file/);
  assert.match(failure("box_not_connected"), /Connect Box in Integrations first/);
  assert.match(failure("box_reconnect_required"), /needs a reconnect/);
  assert.match(failure("box_unreachable"), /did not answer/);
  assert.match(failure("box_unavailable"), /not set up on this server/);
  assert.equal(failure("something_new"), "Something went wrong. Try again.");
  assert.equal(boxFailureText(new Error("x")), "Something went wrong. Try again.");
});

const check = (over: Partial<BoxCheck> = {}): BoxCheck => ({
  kind: "folder",
  name: "Project files",
  files: Array.from({ length: 12 }, (_, i) => file(i + 1)),
  filesCapped: false,
  skipped: 3,
  folders: 2,
  bytes: 12 * 102400,
  capped: false,
  ...over,
});
function file(n: number, path = ""): BoxCheckFile {
  return { id: String(4000 + n), name: `File ${String(n)}.pdf`, path, category: "pdf" };
}

test("a passed check says what is there: a folder or a file, the size, the skipped types and the folders; the cap is not said twice", () => {
  assert.equal(boxCheckText(check()), "Folder Project files · 12 files (1.2 MB), 3 skipped (other types), 2 folders.");
  assert.equal(
    boxCheckText(check({ files: [file(1)], skipped: 0, folders: 1, bytes: 2048 })),
    "Folder Project files · 1 file (2.0 KB), 1 folder.",
  );
  assert.equal(
    boxCheckText(check({ kind: "file", name: "Spec.pdf", files: [file(1)], skipped: 0, folders: 0, bytes: 2048 })),
    "File Spec.pdf · 1 file (2.0 KB).",
  );
  assert.equal(
    boxCheckText(check({ files: [], bytes: 0, skipped: 4, folders: 0 })),
    "Folder Project files · 0 files, 4 skipped (other types).",
  );
  assert.doesNotMatch(boxCheckText(check({ capped: true, filesCapped: true })), /first ones/);
});

test("the selection line appears only when a file is unchecked, and the ids left out are what exclude carries", () => {
  const files = check().files;
  const rows = files.map(idOfFile);
  assert.equal(selectionText(files, checkAll()), null);
  assert.equal(selectionText(files, toggle(checkAll(), "4002", false)), "11 of 12 files");
  assert.equal(selectionText(files, checkNone(rows)), "No files");
  assert.deepEqual(excludeOf(rows, toggle(toggle(checkAll(), "4005", false), "4002", false)), ["4002", "4005"]);
  assert.equal(selectionText([file(1)], toggle(checkAll(), "4001", false)), "No files");
});

test("the folder after a file name is its path, and nothing for a file in the root", () => {
  assert.equal(folderHint(file(1)), undefined);
  assert.equal(folderHint(file(1, "design/archive")), "design/archive");
});

test("the way back from Box toasts success or the reason in words", () => {
  assert.deepEqual(boxToast("connected"), { title: "Box connected", tone: "success" });
  assert.equal(boxToast("box_consent_denied").tone, "danger");
  assert.match(boxToast("box_state_invalid").title, /sign-in expired/);
  assert.match(boxToast("something_new").title, /could not be connected/);
});

const noop = () => undefined;
const ports: Partial<DataPorts> = {};
const ui = createUiStore();
const context: DataContextValue = { ui, ports: ports as DataPorts, routes: routesFor("production"), handoff: createUiHandoff(ui) };
const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));

test("the registry has the Box link in Graph, needing Box, with its check labels", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "box"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["graph", "box", "box", "Box link"]);
  assert.deepEqual(spec.check, { test: "Check link", testing: "Checking…", add: "Add source" });
});

test("the Box form asks for one link, says it reads once, shows no list before a check, and Check link is off until there is a link", () => {
  const props: FormProps = {
    projectId: "p1",
    zone: "graph",
    spec: defined(CONNECTORS.find((c) => c.kind === "box")),
    mcp: { mcpUrl: "", clientId: "", serverName: "" },
    reload: { current: noop },
    onBack: noop,
    onClose: noop,
  };
  const view = html(createElement(BoxForm, props));
  assert.match(view, />Box link<\/label>/);
  assert.match(view, /A folder, a file or a shared link from Box/);
  assert.match(view, /read once when it is added/);
  assert.doesNotMatch(view, /only reads/, "the card says it, the form does not repeat it");
  assert.doesNotMatch(view, /Files to add/);
  assert.match(view, /disabled=""[^>]*>Check link<\/button>/);
});
