// Mission Control V2, README "Integrations" and DECISIONS D1-D6: the rules of the Integrations page (what a card shows from the API's
// answer, who may disconnect, what a failure says), the Slack set-up texts and the V2 route builder. Pure: no React, no server.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  cardViewOf,
  disconnectsThroughSlackRoute,
  integrationFailure,
  isLinked,
  returnToast,
} from "@/components/v2/integrations/integration-logic";
import { channelLabel, slackLandingToast, slackTestFailure } from "@/components/v2/integrations/slack-logic";
import { COPY } from "@/lib/copy";
import { DataError } from "@/lib/domain/errors";
import { canDisconnect, type Integration } from "@/lib/domain/integrations";
import { parseIntegrationsQuery, routesFor } from "@/lib/routes";

const row = (over: Partial<Integration> = {}): Integration => ({
  app: "github",
  available: true,
  connectedBy: { userId: "u1", name: "Alex Kim" },
  workspace: { id: "octo", name: "octo" },
  connectedAt: "2026-11-20T09:30:00Z",
  health: "active",
  ...over,
});
const NONE = { connectedBy: null, workspace: null, connectedAt: null, health: "none" } as const;
const apiError = (apiCode: string) => new DataError("conflict", false, { apiCode });

test("a card nobody connected is grey and says Set up, with no tag", () => {
  assert.deepEqual(cardViewOf(row(NONE)), { state: "off", actionLabel: "Set up" });
});

test("a connected card is ink, says Connected by <name> and shows the API's health word", () => {
  assert.deepEqual(cardViewOf(row()), { state: "on", badge: "Active", by: "Connected by Alex Kim", actionLabel: "Manage" });
});

test("an expired connection is reconnect_required: the card stops being ink but keeps who connected it", () => {
  assert.deepEqual(cardViewOf(row({ health: "reconnect_required" })), {
    state: "stopped",
    badge: "Reconnect required",
    by: "Connected by Alex Kim",
    actionLabel: "Manage",
  });
});

test("a binding whose member left still shows its workspace, stopped, with nobody named", () => {
  const view = cardViewOf(row({ connectedBy: null, health: "none" }));
  assert.equal(view.state, "stopped");
  assert.equal(view.badge, "Stopped");
  assert.equal(view.by, undefined);
});

test("an app the server does not run is unavailable, whatever else the row says", () => {
  assert.equal(cardViewOf(row({ available: false })).state, "unavailable");
  assert.equal(cardViewOf(row({ available: false, ...NONE })).actionLabel, "Unavailable");
});

test("Disconnect: the member who connected it or the owner; nothing to disconnect on an empty card", () => {
  assert.equal(canDisconnect(row(), "u1", false), true);
  assert.equal(canDisconnect(row(), "u2", false), false);
  assert.equal(canDisconnect(row(), "u2", true), true);
  assert.equal(canDisconnect(row(), null, false), false);
  assert.equal(canDisconnect(row(NONE), "u1", true), false);
  assert.equal(canDisconnect(row({ connectedBy: null }), "u2", false), false);
  assert.equal(canDisconnect(row({ connectedBy: null }), "u2", true), true);
});

test("Slack is disconnected through its own route, and only by the member who connected it or the owner", () => {
  const slack = row({ app: "slack" });
  assert.equal(canDisconnect(slack, "u1", false), true, "the connector");
  assert.equal(canDisconnect(slack, "u2", false), false, "another member");
  assert.equal(canDisconnect(slack, "u2", true), true, "the owner");
  assert.equal(disconnectsThroughSlackRoute("slack"), true);
  assert.equal(disconnectsThroughSlackRoute("github"), false);
  assert.equal(disconnectsThroughSlackRoute("atlassian"), false);
});

test("a failed connect or disconnect says what to do; the project being gone is not a message", () => {
  assert.deepEqual(integrationFailure(apiError("github_not_connected")), { kind: "text", text: "Connect your own account first." });
  assert.match(String(Reflect.get(integrationFailure(apiError("integration_workspace_mismatch")), "text")), /another workspace/);
  assert.match(String(Reflect.get(integrationFailure(apiError("integration_forbidden")), "text")), /connected it, or the owner/);
  assert.match(
    String(Reflect.get(integrationFailure(apiError("integration_already_connected")), "text")),
    /Another member has already connected .* remove it first/,
  );
  assert.deepEqual(integrationFailure(apiError("project_not_found")), { kind: "gone" });
  assert.equal(Reflect.get(integrationFailure(apiError("something_new")), "text"), "Something went wrong. Try again.");
  assert.equal(Reflect.get(integrationFailure(new Error("boom")), "text"), "Something went wrong. Try again.");
});

test("the return of an app: `connected` links the account; other codes are the toasts of the Settings tabs", () => {
  assert.equal(isLinked("connected"), true);
  assert.equal(isLinked("installed"), false);
  assert.deepEqual(returnToast("github", "installed"), { title: COPY.githubInstalled, tone: "success" });
  assert.deepEqual(returnToast("github", "github_consent_denied"), { title: COPY.githubConsentDenied, tone: "danger" });
  assert.deepEqual(returnToast("atlassian", "atlassian_site_not_granted"), { title: COPY.atlassianSiteNotGranted, tone: "danger" });
  assert.equal(returnToast("atlassian", "nonsense").tone, "danger");
});

test("the Slack test failures have words; an unknown code is the generic one", () => {
  assert.match(slackTestFailure(apiError("slack_not_connected")), /No channel is connected/);
  assert.match(slackTestFailure(apiError("slack_not_in_channel")), /Invite it again/);
  assert.match(slackTestFailure(apiError("slack_unreachable")), /could not be reached/);
  assert.match(slackTestFailure(apiError("slack_unavailable")), /not set up/);
  assert.equal(slackTestFailure(apiError("x")), "Something went wrong. Try again.");
  assert.equal(channelLabel({ name: "gaps", private: false }), "#gaps");
  assert.equal(channelLabel({ name: "gaps", private: true }), "#gaps (private)");
});

test("V2 routes: the canvas and the Integrations page; `/` stays home", () => {
  const routes = routesFor("production");
  assert.equal(routes.home(), "/");
  assert.equal(routes.canvas("a b"), "/projects/a%20b");
  assert.equal(routes.integrations("p1"), "/projects/p1/integrations");
  assert.equal(
    routes.integrations("p1", { setup: "github", next: "/projects/p1?add=github" }),
    "/projects/p1/integrations?setup=github&next=%2Fprojects%2Fp1%3Fadd%3Dgithub",
  );
  assert.equal(routes.missionControl("p1"), "/projects/p1/mission-control"); // V1 stays where it was
});

test("the Integrations query: return codes, the app to open and a same-site `next` only", () => {
  const q = (s: string) => parseIntegrationsQuery(new URLSearchParams(s));
  assert.deepEqual(q(""), { github: null, atlassian: null, notion: null, setup: null, next: null });
  assert.deepEqual(q("github=connected&setup=github&next=/projects/p1"), {
    github: "connected",
    atlassian: null,
    notion: null,
    setup: "github",
    next: "/projects/p1",
  });
  assert.equal(q("atlassian=atlassian_consent_denied").atlassian, "atlassian_consent_denied");
  assert.equal(q("setup=notion").setup, "notion");
  assert.equal(q("setup=linear").setup, null);
  assert.equal(q("notion=connected").notion, "connected");
  for (const bad of ["//evil.example", "https://evil.example", "/a/../b", "/\\evil"])
    assert.equal(q(`next=${encodeURIComponent(bad)}`).next, null, bad);
});

test("the Slack link landing: linked succeeds, an expired link and an app that is off say so, anything else is a failure", () => {
  assert.equal(slackLandingToast("linked").tone, "success");
  assert.match(slackLandingToast("slack_link_invalid").title, /expired/);
  assert.match(slackLandingToast("slack_unavailable").title, /not set up/);
  assert.equal(slackLandingToast("anything else").tone, "danger");
});
