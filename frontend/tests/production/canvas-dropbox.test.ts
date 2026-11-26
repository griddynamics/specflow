// The Dropbox link source on the canvas: what its nodes say in each state the sources API reports, where a node opens, the words of a failed check or add,
// the line of a passed check, the selection line, the toast of the way back from Dropbox, the form before any answer and the registry entry. Pure and
// server-rendered: no browser.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { NodeView } from "@/components/v2/canvas/canvas-types";
import { DropboxForm } from "@/components/v2/canvas/DropboxForm";
import { boxCheckText, folderHint, idOfFile, selectionText } from "@/components/v2/canvas/box-logic";
import { dropboxFailureText, dropboxOpen, dropboxToast } from "@/components/v2/canvas/dropbox-logic";
import { checkAll, excludeOf, toggle } from "@/components/v2/canvas/pick-logic";
import type { FormProps } from "@/components/v2/connector-types";
import { CONNECTORS, NODE_MAPPERS } from "@/components/v2/connectors";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { DataError } from "@/lib/domain/errors";
import type { DropboxCheck, DropboxCheckFile } from "@/lib/domain/sources";
import { routesFor } from "@/lib/routes";
import { defined } from "../support/defined";
import { input, statusOf, wireSource } from "./canvas-fixtures";

const LINK = "https://www.dropbox.com/home/Projects/Specs";
const nodeOf = (counts: Parameters<typeof wireSource>[2], over: Record<string, unknown> = {}): NodeView => {
  const i = input({ status: statusOf([wireSource("c1", "dropbox", counts, { link: LINK, ...over })]) });
  return defined(buildCanvasModel(i, NODE_MAPPERS).zones.graph.nodes[0]);
};

test("a Dropbox source importing shows files with a bar, a spinner and a moving wire; listing says so", () => {
  const busy = nodeOf({ done: 3, processing: 9 });
  assert.deepEqual([busy.name, busy.state, busy.badge, busy.wire, busy.meta], ["Project files", "busy", "spin", "flow", "3 of 12 files"]);
  assert.equal(busy.progress, 0.25);
  assert.equal(busy.mark, "dropbox");
  const listing = nodeOf({}, { listing_finished: false });
  assert.deepEqual([listing.listing, listing.detail.text], [true, "Listing files…"]);
});

test("a finished Dropbox source is ink with the file count and the app; its dialog says it was read once and opens the Dropbox link", () => {
  const done = nodeOf({ done: 42 });
  assert.deepEqual([done.state, done.badge, done.meta, done.wire], ["on", "ok", "42 files · Dropbox", "on"]);
  assert.match(done.detail.text, /^42 files\. Imported when it was added/);
  assert.deepEqual([done.detail.app, done.detail.remove, done.detail.reconnect], ["Dropbox", "source", null]);
  assert.deepEqual(done.detail.open, { href: LINK, label: "Open in Dropbox" });
  assert.equal(nodeOf({ done: 1 }).meta, "1 file · Dropbox");
  assert.match(done.label, /^Dropbox Project files: 42 files$/);
});

test("a Dropbox import the API reports failed stops: no access offers to reconnect Dropbox, any other reason says it failed", () => {
  const noAccess = nodeOf({ failed: 1 }, { failure_reason: "no_access" });
  assert.deepEqual(
    [noAccess.state, noAccess.badge, noAccess.wire, noAccess.meta, noAccess.detail.reconnect],
    ["stopped", "stop", "off", "Cannot read it", "dropbox"],
  );
  assert.match(noAccess.detail.text, /Dropbox needs a reconnect in Integrations/);
  const failed = nodeOf({ failed: 1 }, { failure_reason: "unknown_error" });
  assert.deepEqual([failed.state, failed.meta, failed.detail.reconnect], ["stopped", "Import failed", null]);
  assert.match(failed.detail.text, /Add the link again/);
});

test("a node opens only an https link on www.dropbox.com; anything else is no link", () => {
  assert.deepEqual(dropboxOpen(LINK), { href: LINK, label: "Open in Dropbox" });
  for (const bad of [
    null,
    "",
    "javascript:alert(1)",
    "ht" + "tp://www.dropbox.com/home/x",
    "https://evil.example.com/home/x",
    "https://www.dropbox.com.evil.test/home/x",
    "https://dropbox.com/home/x",
  ]) {
    assert.equal(dropboxOpen(bad), undefined, String(bad));
  }
  assert.equal(nodeOf({ done: 1 }, { link: null }).detail.open, undefined);
});

const failure = (apiCode: string) => dropboxFailureText(new DataError("validation", false, { apiCode }));

test("each closed code of the check and the add has its own words; an unknown one is the general failure", () => {
  assert.match(failure("dropbox_link_invalid"), /not a link to a Dropbox folder or file, or it has a password/);
  assert.match(failure("dropbox_no_access"), /cannot open this folder or file\. Add it to that Dropbox first\./);
  assert.match(failure("dropbox_not_connected"), /Connect Dropbox in Integrations first/);
  assert.match(failure("dropbox_reconnect_required"), /needs a reconnect/);
  assert.match(failure("dropbox_unreachable"), /did not answer/);
  assert.match(failure("dropbox_unavailable"), /not set up on this server/);
  assert.match(failure("dropbox_auth_failed"), /refused the request of this server/);
  assert.match(failure("integration_workspace_mismatch"), /./);
  assert.match(failure("source_not_found"), /removed/);
  assert.equal(failure("something_new"), "Something went wrong. Try again.");
  assert.equal(dropboxFailureText(new Error("x")), "Something went wrong. Try again.");
});

const check = (over: Partial<DropboxCheck> = {}): DropboxCheck => ({
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
function file(n: number, path = ""): DropboxCheckFile {
  return { id: `id:f${String(n)}`, name: `File ${String(n)}.pdf`, path, category: "pdf" };
}

// The check line, the selection line and the folder hint are the Box link's (canvas-box.test.ts); a Dropbox check reads through them as one.
test("a Dropbox check reads through the Box link's file list: its line, its `id:...` rows and what exclude carries", () => {
  const files = check().files;
  const rows = files.map(idOfFile);
  assert.equal(boxCheckText(check()), "Folder Project files · 12 files (1.2 MB), 3 skipped (other types), 2 folders.");
  assert.equal(selectionText(files, toggle(checkAll(), "id:f2", false)), "11 of 12 files");
  assert.deepEqual(excludeOf(rows, toggle(toggle(checkAll(), "id:f5", false), "id:f2", false)), ["id:f2", "id:f5"]);
  assert.equal(folderHint(file(1, "design/archive")), "design/archive");
});

test("the way back from Dropbox toasts success or the reason in words", () => {
  assert.deepEqual(dropboxToast("connected"), { title: "Dropbox connected", tone: "success" });
  assert.equal(dropboxToast("dropbox_consent_denied").tone, "danger");
  assert.match(dropboxToast("dropbox_state_invalid").title, /sign-in expired/);
  assert.match(dropboxToast("dropbox_code_invalid").title, /did not accept/);
  assert.match(dropboxToast("something_new").title, /could not be connected/);
});

const noop = () => undefined;
const ports: Partial<DataPorts> = {};
const ui = createUiStore();
const context: DataContextValue = { ui, ports: ports as DataPorts, routes: routesFor("production"), handoff: createUiHandoff(ui) };
const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));

test("the registry has the Dropbox link in Graph, needing Dropbox, with its check labels", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "dropbox"));
  assert.deepEqual([spec.zone, spec.app, spec.mark, spec.name], ["graph", "dropbox", "dropbox", "Dropbox link"]);
  assert.deepEqual(spec.check, { test: "Check link", testing: "Checking…", add: "Add source" });
});

test("the Dropbox form asks for one link, says it reads once, shows no list before a check, and Check link is off until there is a link", () => {
  const props: FormProps = {
    projectId: "p1",
    zone: "graph",
    spec: defined(CONNECTORS.find((c) => c.kind === "dropbox")),
    mcp: { mcpUrl: "", clientId: "", serverName: "" },
    reload: { current: noop },
    onBack: noop,
    onClose: noop,
  };
  const view = html(createElement(DropboxForm, props));
  assert.match(view, />Dropbox link<\/label>/);
  assert.match(view, /A shared link, or the address of a folder or file in your Dropbox/);
  assert.match(view, /read once when it is added/);
  assert.doesNotMatch(view, /only reads/, "the card says it, the form does not repeat it");
  assert.doesNotMatch(view, /Files to add/);
  assert.match(view, /disabled=""[^>]*>Check link<\/button>/);
});
