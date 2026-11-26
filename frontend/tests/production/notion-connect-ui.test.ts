// Stream C2: the Notion card of the Integrations page. The rules without React (what the dialog offers from the project's binding and the
// caller's own connection, as the stub API answers it), the `?notion=` toasts, the Notion failures of a connect or disconnect, and the card body
// rendered on the server. The browser round trip runs in the production e2e.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NotionSetup } from "@/components/v2/integrations/NotionSetup";
import { integrationFailure, isLinked, notionToast, returnToast } from "@/components/v2/integrations/integration-logic";
import { NOTION_CODES } from "@/lib/notion-callback";
import { NOTION_WORDS, notionReturnWord, notionSetupViewOf } from "@/components/v2/integrations/notion-setup-logic";
import type { SetupProps } from "@/components/v2/integrations/setup-props";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import type { DataPorts } from "@/lib/data/ports";
import { toNotionConnection } from "@/lib/data/http/notion-connection";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import { DataError } from "@/lib/domain/errors";
import type { Integration } from "@/lib/domain/integrations";
import { routesFor } from "@/lib/routes";
import { createNotionModel } from "../support/stub-notion.mjs";

const ALEX = { userId: "u1", name: "Alex Kim" };
const NONE: Integration = { app: "notion", available: true, connectedBy: null, workspace: null, connectedAt: null, health: "none" };
const WORKING: Integration = {
  ...NONE,
  connectedBy: ALEX,
  workspace: { id: "w", name: "Sample Workspace" },
  connectedAt: "2026-11-20T09:30:00Z",
  health: "active",
};
const STOPPED: Integration = { ...WORKING, health: "reconnect_required" };

/** The caller's connection as the stub API answers it, read by the production adapter's shape check. */
function mine(connection: { status: string; workspace_name?: string } | null, enabled = true) {
  const model = createNotionModel();
  model.state.enabled = enabled;
  model.state.connection = connection;
  return toNotionConnection(model.connection());
}

test("nobody connected and the caller has no connection: Connect Notion, with the how-to, going to Notion", () => {
  const v = notionSetupViewOf(NONE, false, mine(null));
  assert.deepEqual(v, {
    state: "ready",
    primary: "connect",
    howTo: true,
    retry: false,
    stoppedNotice: false,
    addPages: false,
    forget: false,
    bindsOnly: false,
  });
});

test("the caller is connected but the project is not bound: the button only binds, and Forget is offered", () => {
  const v = notionSetupViewOf(NONE, false, mine({ status: "active", workspace_name: "Sample Workspace" }));
  assert.equal(v.primary, "connect");
  assert.equal(v.bindsOnly, true);
  assert.equal(v.forget, true);
  assert.equal(v.addPages, false);
});

test("an expired connection of the caller is Reconnect and goes to Notion, not a bind", () => {
  const v = notionSetupViewOf(NONE, false, mine({ status: "expired" }));
  assert.equal(v.primary, "reconnect");
  assert.equal(v.bindsOnly, false);
  assert.equal(v.forget, true);
});

test("a stopped binding is Reconnect; when the caller's own connection works the button binds it again", () => {
  assert.equal(notionSetupViewOf(STOPPED, true, mine(null)).primary, "reconnect");
  assert.equal(notionSetupViewOf(STOPPED, true, mine(null)).bindsOnly, false);
  const own = notionSetupViewOf(STOPPED, true, mine({ status: "active" }));
  assert.equal(own.primary, "reconnect");
  assert.equal(own.bindsOnly, true);
});

test("a working binding has no primary button and no how-to; Add pages only for who may manage it and is connected", () => {
  const active = mine({ status: "active" });
  assert.deepEqual(notionSetupViewOf(WORKING, true, active), {
    state: "ready",
    primary: null,
    howTo: false,
    retry: false,
    stoppedNotice: false,
    addPages: true,
    forget: true,
    bindsOnly: false,
  });
  assert.equal(notionSetupViewOf(WORKING, false, active).addPages, false);
  assert.equal(notionSetupViewOf(WORKING, true, mine(null)).addPages, false);
});

test("a working binding with the caller's own expired connection: Reconnect for the caller, Add pages stays reachable, no how-to", () => {
  const v = notionSetupViewOf(WORKING, true, mine({ status: "expired" }));
  assert.equal(v.primary, "reconnect");
  assert.equal(v.bindsOnly, false);
  assert.equal(v.howTo, false);
  assert.equal(v.addPages, true);
  assert.equal(v.forget, true);
});

test("Notion off on the server is a note and no button; before the read it is loading with the button waiting", () => {
  const off = notionSetupViewOf(NONE, false, mine(null, false));
  assert.equal(off.state, "unavailable");
  assert.equal(off.primary, null);
  assert.equal(off.howTo, false);
  const loading = notionSetupViewOf(NONE, false, null);
  assert.equal(loading.state, "loading");
  assert.equal(loading.primary, "connect");
});

test("a failed read of the caller's connection is not a stuck loading: Retry is offered and the button works", () => {
  const v = notionSetupViewOf(NONE, false, "failed");
  assert.equal(v.state, "failed");
  assert.equal(v.retry, true);
  assert.equal(v.primary, "connect");
  assert.equal(notionSetupViewOf(NONE, false, null).retry, false);
  assert.equal(notionSetupViewOf(NONE, false, mine(null)).retry, false);
});

test("Forget always asks first, and the words say every project that uses the account stops", () => {
  assert.match(NOTION_WORDS.forgetBody, /Every project that uses this account stops reading and writing Notion until someone reconnects\./);
  assert.match(NOTION_WORDS.forgotten, /Projects that used it need reconnecting/);
  assert.equal(NOTION_WORDS.reconnectFirst, "Notion access ended. Reconnect Notion first.");
});

test("a bound project that does not work shows the reconnect notice; a working or unbound one does not", () => {
  assert.equal(notionSetupViewOf(STOPPED, true, mine(null)).stoppedNotice, true);
  assert.equal(notionSetupViewOf(WORKING, true, mine(null)).stoppedNotice, false);
  assert.equal(notionSetupViewOf(NONE, true, mine(null)).stoppedNotice, false);
});

test("?notion=connected is the success toast; every other code of the callback has its own words, never the raw code", () => {
  assert.deepEqual(notionToast("connected"), { title: "Connected", tone: "success" });
  assert.equal(isLinked("connected"), true);
  const seen = new Set<string>();
  for (const code of NOTION_CODES.filter((c) => c !== "connected")) {
    const t = notionToast(code);
    assert.equal(t.tone, "danger");
    assert.doesNotMatch(t.title, /notion_/);
    assert.equal(t.title, notionReturnWord(code));
    seen.add(t.title);
  }
  assert.equal(notionToast("notion_unexpected").title, NOTION_WORDS.unexpected);
  assert.equal(notionToast("something_else").title, NOTION_WORDS.unexpected);
  assert.equal(notionToast("hasOwnProperty").title, NOTION_WORDS.unexpected);
  assert.ok(seen.size >= 6, "the refusals are told apart");
  assert.deepEqual(returnToast("notion", "connected"), notionToast("connected"));
  assert.deepEqual(returnToast("notion", "notion_state_invalid"), notionToast("notion_state_invalid"));
});

test("failed connects: notion_unavailable (503 of the PUT), not connected and reconnect required have words; the rest is the generic one", () => {
  const e = (apiCode: string, kind: "unavailable" | "conflict" = "conflict") => new DataError(kind, false, { apiCode });
  assert.deepEqual(integrationFailure(e("notion_unavailable", "unavailable")), {
    kind: "text",
    text: "Notion is not available on this server.",
  });
  assert.deepEqual(integrationFailure(e("notion_not_connected")), { kind: "text", text: "Connect your own account first." });
  assert.deepEqual(integrationFailure(e("notion_reconnect_required")), { kind: "text", text: NOTION_WORDS.reconnectFirst });
  assert.deepEqual(integrationFailure(e("notion_whatever")), { kind: "text", text: "Something went wrong. Try again." });
  assert.deepEqual(integrationFailure(e("project_not_found")), { kind: "gone" });
});

// ---- the card body, rendered on the server (effects do not run: the caller's connection is not read yet) ----

const noop = () => undefined;
const ports: Partial<DataPorts> = {};
const ui = createUiStore();
const context: DataContextValue = {
  ui,
  ports: ports as DataPorts,
  routes: routesFor("production"),
  handoff: createUiHandoff(ui),
};
const html = (node: ReactElement): string => renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node));
const props = (integration: Integration, over: Partial<SetupProps> = {}): SetupProps => ({
  projectId: "p1",
  integration,
  canDisconnect: false,
  returnTo: "/projects/p1/integrations?setup=notion",
  onConnect: () => Promise.resolve(true),
  onDisconnect: () => Promise.resolve(true),
  onRefresh: noop,
  onError: noop,
  onClose: noop,
  onDone: noop,
  ...over,
});

test("the Notion card before anything is bound: a waiting Connect Notion button, the how-to and the acts-as rule", () => {
  const view = html(createElement(NotionSetup, props(NONE)));
  assert.match(view, /disabled="" type="button" class="gx-btn">Connect Notion<\/button>/);
  assert.match(view, /<details class="gx-how">.*Show me how/);
  assert.match(view, /tick the pages and databases/);
  assert.match(view, /Uses the account of the member who connected it/);
  assert.doesNotMatch(view, />Disconnect</);
});

test("the Notion card of a stopped binding says Reconnect Notion and why; a working one has nothing to press but Done and Disconnect", () => {
  {
    const view = html(createElement(NotionSetup, props(STOPPED)));
    assert.match(view, />Reconnect Notion<\/button>/);
    assert.match(view, /The account that connected it has no access now/);
  }
  const view = html(createElement(NotionSetup, props(WORKING, { canDisconnect: true })));
  assert.doesNotMatch(view, /Connect Notion|Reconnect Notion|Show me how/);
  assert.match(view, /Sample Workspace · Connected by Alex Kim/);
  assert.match(view, />Disconnect<\/button>/);
  assert.match(view, />Done<\/button>/);
});
