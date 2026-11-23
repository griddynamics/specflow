// docs/jira-sync/oauth.md#jira-tab: the Jira tab in the `oauth` mode rendered on the server, and its rules without React. The owner's
// connection row (connected, not connected, expired), the gate on Check access, Save and the switch, the acting account line (as you, as
// another owner, expired, missing), the member's read-only view, the token mode unchanged, the `?atlassian=` toast texts, the new sync and
// access-check codes on the status line and the Plan line. The clicks (connect, callback, disconnect) run in the production e2e.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";
import { DataError } from "@/lib/domain/errors";
import type { AtlassianConnection, JiraActing, JiraSettings } from "@/lib/domain/jira";
import type { JiraViewProps } from "../../components/settings/jira-parts";

stubCss();
async function load() {
  return {
    P: await import("../../components/settings/jira-parts"),
    L: await import("../../components/settings/jira-logic"),
    J: await import("../../components/plan/ready/JiraSyncLine"),
  };
}
let P: Awaited<ReturnType<typeof load>>["P"];
let L: Awaited<ReturnType<typeof load>>["L"];
let J: Awaited<ReturnType<typeof load>>["J"];
before(async () => {
  ({ P, L, J } = await load());
});

const noop = () => undefined;
const conn = (over: Partial<AtlassianConnection> = {}): AtlassianConnection => ({
  mode: "oauth",
  connected: true,
  status: "active",
  accountName: "Ann Lee",
  connectedAt: "2026-11-20T09:30:00Z",
  lastUsedAt: null,
  ...over,
});
const NONE = conn({ connected: false, status: null, accountName: null, connectedAt: null });
const TOKEN = conn({ mode: "token", connected: false, status: null, accountName: null, connectedAt: null });
const acting = (over: Partial<JiraActing> = {}): JiraActing => ({
  connectedByName: "Ann Lee",
  connectedByMe: true,
  status: "active",
  ...over,
});
const SAVED: JiraSettings = {
  available: true,
  editable: true,
  siteUrl: "https://example.atlassian.net",
  jiraProject: "PAY",
  epicKey: null,
  createdEpicKey: null,
  enabled: true,
  sync: { status: "succeeded", finishedAt: "2026-02-01T10:00:00Z", errorCode: null, progress: null },
  connection: acting(),
};
const EMPTY: JiraSettings = { ...SAVED, jiraProject: null, enabled: false, sync: null, connection: null };

function markup(over: Partial<JiraViewProps> = {}): string {
  return renderToStaticMarkup(
    createElement(P.JiraView, {
      settings: SAVED,
      atlassian: conn(),
      projectName: "Checkout revamp",
      project: "PAY",
      epic: "",
      phase: "check",
      outcome: null,
      switching: false,
      onProject: noop,
      onEpic: noop,
      onPrimary: noop,
      onSwitch: noop,
      ...over,
    }),
  );
}
const primaryDisabled = (view: string) => /<button[^>]*disabled=""[^>]*data-testid="jira-primary"/.test(view);
const switchDisabled = (view: string) => /<button[^>]*role="switch"[^>]*disabled=""/.test(view);

test("connected owner: 'Connected to Atlassian as Ann Lee' with Disconnect and the Connected apps note; the form works as before", () => {
  const view = markup();
  assert.match(view, /data-testid="atlassian-row" data-state="connected"/);
  assert.match(view, /Connected to Atlassian as Ann Lee/);
  assert.match(view, /Connected Nov 20, 2026/);
  assert.match(view, /data-testid="atlassian-disconnect" class="[^"]*">Disconnect</);
  assert.doesNotMatch(view, /atlassian-connect/);
  assert.match(view, /href="https:\/\/id\.atlassian\.com\/manage-profile\/apps"[^>]*target="_blank"[^>]*rel="noopener noreferrer"/);
  assert.doesNotMatch(view, /jira-gate/);
  assert.ok(primaryDisabled(view)); // the saved target acts as me: nothing to check or save
  assert.ok(!switchDisabled(view));
  assert.match(view, /data-testid="jira-acting" data-warning="false"><span>Syncs to Jira as you<\/span><\/p>/);
  assert.match(markup({ project: "PAYX" }), /<button type="submit" data-testid="jira-primary" class="gdg-btn gdg-btn--primary"/);
});

test("not connected owner: Connect Atlassian (the one yellow button); Check access, Save and the switch are off with the reason", () => {
  const view = markup({ settings: EMPTY, atlassian: NONE, project: "PAY" });
  assert.match(view, /data-state="none"/);
  assert.match(view, /Not connected to Atlassian/);
  assert.match(view, /data-testid="atlassian-connect" class="gdg-btn gdg-btn--primary gdg-btn--sm">Connect Atlassian</);
  assert.doesNotMatch(view, /atlassian-grant-note|atlassian-disconnect/);
  assert.ok(primaryDisabled(view));
  assert.match(view, /data-testid="jira-primary" class="gdg-btn gdg-btn--glass"/);
  assert.match(view, /data-testid="jira-gate">Connect Atlassian to check access and save\.</);
  assert.ok(switchDisabled(view));
  assert.doesNotMatch(view, /<input[^>]*disabled/); // the keys stay editable
  assert.doesNotMatch(view, /jira-acting/); // no target, no acting line
  assert.ok(
    primaryDisabled(
      markup({
        settings: EMPTY,
        atlassian: NONE,
        project: "PAY",
        phase: "save",
        outcome: { kind: "ok", projectName: "P", epicSummary: null },
      }),
    ),
  );
});

test("expired owner connection: 'Your Atlassian connection expired' with Reconnect; the form is gated with the reconnect reason", () => {
  const view = markup({ atlassian: conn({ status: "expired" }), settings: { ...SAVED, connection: acting({ status: "expired" }) } });
  assert.match(view, /data-state="expired"/);
  assert.match(view, /Your Atlassian connection expired/);
  assert.match(view, /data-testid="atlassian-connect" class="[^"]*">Reconnect</);
  assert.match(view, /atlassian-grant-note/);
  assert.match(view, /data-testid="jira-gate">Reconnect Atlassian to check access and save\.</);
  assert.ok(primaryDisabled(view));
  assert.ok(switchDisabled(view));
  assert.match(
    view,
    /Syncs to Jira as you<\/span><span class="settings-acting__warning" role="note">.*Reconnect Atlassian to keep syncing\./,
  );
});

test("acting account: another owner active, expired, and missing; the owner may save the unchanged target to take it over", () => {
  let view = markup({ settings: { ...SAVED, connection: acting({ connectedByMe: false, connectedByName: "Bo Chen" }) } });
  assert.match(view, /data-warning="false"><span>Syncs to Jira as Bo Chen</);
  assert.ok(!primaryDisabled(view)); // take over: Check access, then Save
  view = markup({ settings: { ...SAVED, connection: acting({ connectedByMe: false, connectedByName: "Bo Chen", status: "expired" }) } });
  assert.match(view, /data-warning="true"><span>Syncs to Jira as Bo Chen<\/span>/);
  assert.match(view, /Bo Chen must reconnect Atlassian\. Save the target to sync as you\./);
  view = markup({ settings: { ...SAVED, connection: acting({ connectedByMe: false, connectedByName: null, status: "missing" }) } });
  assert.match(view, /Syncs to Jira as nobody yet/);
  assert.match(view, /Save the target to sync as you\./);
  assert.ok(!primaryDisabled(view));
});

test("member: the acting line read-only ('as Ann Lee', the owner's part in the warning), no connection row and no button", () => {
  const member = (c: JiraActing) => markup({ settings: { ...SAVED, editable: false, connection: c }, atlassian: NONE });
  const view = member(acting({ connectedByMe: false }));
  assert.doesNotMatch(view, /atlassian-row|atlassian-connect|jira-primary|jira-gate/);
  assert.match(view, /Syncs to Jira as Ann Lee/);
  assert.match(member(acting({ connectedByMe: false, status: "expired" })), /Ann Lee must reconnect Atlassian\.<\/span>/);
  assert.match(
    member(acting({ connectedByMe: false, connectedByName: null, status: "missing" })),
    /An owner connects Atlassian and saves the target\./,
  );
});

test("token mode (and Jira off, or the connection not read): the tab is unchanged, no row, no gate, no acting line", () => {
  for (const atlassian of [TOKEN, null, conn({ mode: null, connected: false, status: null })]) {
    const view = markup({ atlassian, settings: { ...SAVED, connection: null }, project: "PAYX" });
    assert.doesNotMatch(view, /atlassian-row|jira-gate|jira-acting/);
    assert.ok(!primaryDisabled(view));
    assert.ok(!switchDisabled(view));
  }
  // Jira off: the notice, and no connection row even in the oauth mode
  assert.doesNotMatch(markup({ settings: { ...SAVED, available: false }, atlassian: NONE }), /atlassian-row|jira-gate/);
});

test("connectRowOf, connectGate, actingLineOf, canTakeOver", () => {
  assert.equal(L.connectRowOf(SAVED, TOKEN), null);
  assert.equal(L.connectRowOf({ ...SAVED, editable: false }, conn()), null);
  assert.deepEqual(L.connectRowOf(SAVED, conn()), { kind: "connected", name: "Ann Lee" });
  assert.deepEqual(L.connectRowOf(SAVED, conn({ status: "expired" })), { kind: "expired" });
  assert.deepEqual(L.connectRowOf(SAVED, NONE), { kind: "none" });
  assert.equal(L.connectGate(null), null);
  assert.equal(L.connectGate({ kind: "connected", name: "A" }), null);
  assert.equal(L.actingLineOf(null, true), null);
  assert.equal(L.canTakeOver(null), false);
  assert.equal(L.canTakeOver(acting()), false);
  assert.equal(L.canTakeOver(acting({ status: "expired" })), true);
  assert.equal(L.canTakeOver(acting({ connectedByMe: false })), true);
});

test("?atlassian= toasts: connected succeeds; every code has its text; an unknown value is the generic failure", () => {
  assert.deepEqual(L.atlassianToast("connected"), { title: "Atlassian connected", tone: "success" });
  const texts: Record<string, string> = {
    atlassian_consent_denied: "Atlassian access was not granted.",
    atlassian_state_invalid: "The Atlassian sign-in expired. Try again.",
    atlassian_code_invalid: "Atlassian did not accept the sign-in. Try again.",
    atlassian_site_not_granted: "Your Atlassian account has no access to this Jira site.",
    atlassian_unreachable: "Atlassian could not be reached. Try again later.",
    atlassian_oauth_unavailable: "Atlassian connections are not set up on this Specflow server.",
    atlassian_unexpected: "Could not connect Atlassian. Try again.",
    "<script>": "Could not connect Atlassian. Try again.",
    constructor: "Could not connect Atlassian. Try again.",
  };
  for (const [code, title] of Object.entries(texts)) assert.deepEqual(L.atlassianToast(code), { title, tone: "danger" }, code);
});

test("the new codes: access-check reasons as the alert line, sync codes on the status line name the owner's part, a 409 PUT is the alert", () => {
  assert.deepEqual(L.outcomeOf({ result: "failed", reason: "jira_not_connected", field: null }), {
    kind: "problem",
    texts: { alert: "Connect your Atlassian account first." },
  });
  assert.equal(L.reasonText("jira_reconnect_required"), "Your Atlassian connection expired. Reconnect Atlassian.");
  assert.equal(L.syncErrorText("jira_not_connected"), "No Atlassian account to sync as. An owner connects Atlassian and saves the target.");
  assert.equal(
    L.syncErrorText("jira_reconnect_required"),
    "The Atlassian connection this target syncs as expired. Its owner reconnects Atlassian.",
  );
  assert.equal(L.syncErrorText("jira_unreachable"), "Jira did not answer. Try again.");
  const failed = { ...SAVED, sync: { status: "failed" as const, finishedAt: null, errorCode: "jira_reconnect_required", progress: null } };
  assert.match(markup({ settings: failed }), /Its owner reconnects Atlassian\./);
  const refused = new DataError("conflict", false, { status: 409, apiCode: "jira_not_connected" });
  assert.deepEqual(L.failureOf(refused), { kind: "disconnected", text: "Connect your Atlassian account first." });
  const st = L.tabReducer(
    { ...L.INITIAL_TAB, settings: SAVED, run: 3, busy: "save" },
    { type: "failed", kind: "save", run: 3, failure: L.failureOf(refused) },
  );
  assert.deepEqual(st.outcome, { kind: "problem", texts: { alert: "Connect your Atlassian account first." } });
  assert.equal(st.busy, null);
});

test("tabReducer: loaded keeps the connection; disconnected clears it and the form's answer; a later load replaces both", () => {
  let st = L.tabReducer(L.INITIAL_TAB, { type: "loaded", settings: SAVED, atlassian: conn() });
  assert.equal(st.atlassian?.accountName, "Ann Lee");
  const later = L.tabReducer(
    { ...st, project: "TYPED" },
    { type: "loaded", settings: { ...SAVED, connection: acting({ status: "missing" }) }, atlassian: NONE },
  );
  assert.equal(typeof later.settings === "object" && later.settings?.connection?.status, "missing");
  assert.deepEqual(later.atlassian, NONE);
  assert.equal(later.project, "TYPED"); // the typed keys are seeded once
  st = { ...st, outcome: { kind: "ok", projectName: "P", epicSummary: null } };
  st = L.tabReducer(st, { type: "disconnected" });
  assert.deepEqual(st.atlassian, NONE);
  assert.equal(st.outcome, null);
  assert.equal(L.tabReducer(L.INITIAL_TAB, { type: "disconnected" }).atlassian, null);
});

test("plan line: a failed pass on the target's Atlassian connection says an owner must connect again; other failures unchanged", () => {
  const line = (errorCode: string | null) =>
    renderToStaticMarkup(
      createElement(J.JiraSyncLineView, {
        settings: { ...SAVED, sync: { status: "failed", finishedAt: null, errorCode, progress: null } },
        settingsHref: "/projects/p1/settings?tab=jira",
      }),
    );
  assert.match(line("jira_not_connected"), /Jira sync failed: an owner must connect Atlassian again\./);
  assert.match(line("jira_reconnect_required"), /an owner must connect Atlassian again/);
  assert.match(line("jira_unreachable"), />Jira sync failed\.</);
  assert.match(line(null), /Open Jira settings/);
});
