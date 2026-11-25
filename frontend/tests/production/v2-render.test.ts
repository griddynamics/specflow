// Mission Control V2 (README "Header", "Integrations"; DECISIONS D11, D12): the Members dialog body for each role, the Create project
// dialog (first run and plain), the Integrations cards in each state and the set-up bodies of the three apps, rendered on the server with
// plain props. The browser behaviour (dialogs opening, the menu, the OAuth round trip) is in e2e-production/v2-production.spec.ts.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AtlassianSetup } from "@/components/v2/integrations/AtlassianSetup";
import { GitHubSetup } from "@/components/v2/integrations/GitHubSetup";
import { IntegrationCards } from "@/components/v2/integrations/IntegrationCards";
import { SlackSetup } from "@/components/v2/integrations/SlackSetup";
import type { SetupProps } from "@/components/v2/integrations/setup-props";
import { MembersView, type MembersViewProps } from "@/components/v2/members/MembersView";
import { CreateProjectDialog } from "@/components/v2/shell/CreateProjectDialog";
import { DataContext, type DataContextValue } from "@/lib/data/provider";
import { createUiHandoff, createUiStore } from "@/lib/data/ui-store";
import type { DataPorts } from "@/lib/data/ports";
import type { Integration } from "@/lib/domain/integrations";
import type { MembersState } from "@/lib/domain/members";
import { routesFor } from "@/lib/routes";

const noop = () => undefined;
const ui = createUiStore();
const ports: Partial<DataPorts> = {}; // the parts under test read no port while rendering on the server
const context: DataContextValue = { ui, ports: ports as DataPorts, routes: routesFor("production"), handoff: createUiHandoff(ui) };
const shown = (node: ReactElement): { html: string } => ({
  html: renderToStaticMarkup(createElement(DataContext.Provider, { value: context }, node)),
});

// ---- the Members dialog body ----

interface Seat {
  id: string;
  name: string | null;
  email: string;
  owner?: boolean;
}
const memberOf = (seat: Seat, you: boolean): MembersState["members"][number] => ({
  userId: seat.id,
  name: seat.name,
  email: seat.email,
  role: seat.owner === true ? "owner" : "collaborator",
  joinedAt: "2026-03-05T10:00:00Z",
  isYou: you,
});
const ANNA: Seat = { id: "u1", name: "Anna Kowalska", email: "anna@example.com", owner: true };
const MARK: Seat = { id: "u2", name: "Mark Lee", email: "mark@example.com" };
const STATE = (role: "owner" | "collaborator"): MembersState => ({
  role,
  limit: 50,
  members: [memberOf(ANNA, role === "owner"), memberOf(MARK, role === "collaborator")],
  invites: [{ id: "i1", email: "jonas@example.com", invitedAt: "2026-03-06T10:00:00Z" }],
});
const members = (role: "owner" | "collaborator", over: Partial<MembersViewProps> = {}): { html: string } =>
  shown(
    createElement(MembersView, {
      state: STATE(role),
      projectId: "p1",
      draft: "Northwind ordering",
      canSave: false,
      saving: false,
      confirm: null,
      confirming: false,
      addKey: 0,
      onAdd: () => Promise.resolve({ ok: true }),
      onDraft: noop,
      onSave: noop,
      onRow: noop,
      onLeave: noop,
      onConfirm: noop,
      onDismiss: noop,
      onDone: noop,
      ...over,
    }),
  );

test("Members, owner: the name with Save, the count, the add field, every row, a cross on each row but the owner's, no Leave", () => {
  const { html: out } = members("owner");
  assert.match(out, /<label class="gx-field__label" for="[^"]+">Project name<\/label>/);
  assert.match(out, /value="Northwind ordering"/);
  assert.match(out, /disabled="" type="button" class="gx-btn gx-btn--ghost">Save<\/button>/);
  assert.match(out, /<p class="gx-notice" role="status">3 of 50<\/p>/);
  assert.match(out, />Email address<\/label>/);
  assert.doesNotMatch(out, /placeholder=/, "the label says it; the placeholder does not repeat it");
  assert.doesNotMatch(out, /<b>Collaborators<\/b>/, "no section head over the count");
  assert.match(out, /role="combobox"/);
  assert.match(out, /<b>Anna Kowalska \(you\)<\/b><small>anna@example\.com<\/small>/);
  assert.match(out, /<span class="gx-tag">Owner<\/span>/);
  assert.match(out, /aria-label="Remove Mark Lee"/);
  assert.match(out, /aria-label="Cancel invite jonas@example\.com"/);
  assert.match(out, /<small>Not signed in yet<\/small>/);
  assert.match(out, /<span class="gx-tag">Invited<\/span>/);
  assert.equal(out.match(/gx-person__x/g)?.length, 2, "no cross on the owner's own row");
  assert.doesNotMatch(out, /Leave project/);
  assert.doesNotMatch(out, /aria-label="Remove Anna/);
});

test("Members, owner: Save is on when the name changed, and a name error is an alert under the field", () => {
  const { html: out } = members("owner", { canSave: true, nameError: "Enter a project name." });
  assert.match(out, /type="button" class="gx-btn gx-btn--ghost">Save<\/button>/);
  assert.doesNotMatch(out, /disabled="" type="button" class="gx-btn gx-btn--ghost">Save/);
  assert.match(out, /<p id="[^"]+-error" class="gx-field__error" role="alert">Enter a project name\.<\/p>/);
});

test("Members, collaborator: the list is read-only (no name, no add field, no cross) and they can leave", () => {
  const { html: out } = members("collaborator");
  assert.doesNotMatch(out, /Project name/);
  assert.doesNotMatch(out, /combobox/);
  assert.doesNotMatch(out, /gx-person__x/);
  assert.match(out, /<b>Mark Lee \(you\)<\/b>/);
  assert.match(out, /Leave project/);
  assert.match(out, /3 of 50/);
});

test("Members: a confirmation names what it does, and replaces the Leave button", () => {
  const { html: remove } = members("owner", { confirm: { kind: "remove", userId: "u2", label: "Mark Lee" } });
  assert.match(remove, /role="alertdialog" aria-label="Remove this collaborator\?"/);
  assert.match(remove, /Mark Lee loses access to this project\./);
  assert.match(remove, />Keep<\/button>/);
  const { html: invite } = members("owner", { confirm: { kind: "cancel", inviteId: "i1", label: "jonas@example.com" } });
  assert.match(invite, /Cancel this invite\?/);
  const { html: leave } = members("collaborator", { confirm: { kind: "leave" }, confirming: true });
  assert.match(leave, /Leave this project\?/);
  assert.doesNotMatch(leave, /<button[^>]*>Leave project<\/button><\/div><div class="gx-foot"/);
  assert.match(leave, /disabled="" type="button" class="gx-btn gx-btn--sm">Leave project<\/button>/);
});

// ---- Create project ----

test("first run: the window has no close button and no Cancel, and Create waits for a name", () => {
  const { html: out } = shown(createElement(CreateProjectDialog, { open: true, firstRun: true, onClose: noop, onCreated: noop }));
  assert.match(out, /Create your first project/);
  assert.match(out, /Welcome to Graphico/);
  assert.doesNotMatch(out, /gx-dialog__close/);
  assert.doesNotMatch(out, />Cancel</);
  assert.match(out, /<label class="gx-field__label" for="v2-project-name">Project name<\/label>/);
  assert.match(out, /disabled="" type="submit" class="gx-btn">Create project<\/button>/);
  assert.equal(out.match(/<input/g)?.length, 1, "the name only: people are invited from Members");
  assert.doesNotMatch(out, /placeholder=/, "the label says it; no example repeats it");
});

test("Create new project: a close button, Cancel and no form while the dialog is closed", () => {
  const { html: out } = shown(createElement(CreateProjectDialog, { open: true, onClose: noop, onCreated: noop }));
  assert.match(out, /Create a project/);
  assert.match(out, /gx-dialog__close/);
  assert.match(out, />Cancel<\/button>/);
  const { html: closed } = shown(createElement(CreateProjectDialog, { open: false, onClose: noop, onCreated: noop }));
  assert.doesNotMatch(closed, /<input/);
});

// ---- Integrations cards ----

const base = (app: Integration["app"], over: Partial<Integration> = {}): Integration => ({
  app,
  available: true,
  connectedBy: null,
  workspace: null,
  connectedAt: null,
  health: "none",
  ...over,
});
const ALEX = { userId: "u1", name: "Alex Kim" };
const cards = (rows: Integration[]): { html: string } => shown(createElement(IntegrationCards, { rows, onOpen: noop }));

test("cards, nothing connected: three grey cards in order, Set up on each, no tag", () => {
  const { html: out } = cards([base("atlassian"), base("github"), base("slack")]);
  assert.deepEqual(
    [...out.matchAll(/data-state="(\w+)"/g)].map((m) => m[1]),
    ["off", "off", "off"],
  );
  assert.deepEqual(
    [...out.matchAll(/class="gx-igcard__name">([^<]+)</g)].map((m) => m[1]),
    ["Atlassian", "GitHub", "Slack"],
  );
  assert.equal(out.match(/>Set up<\/button>/g)?.length, 3);
  assert.doesNotMatch(out, /gx-tag/);
  assert.doesNotMatch(out, /<h1/);
});

test("cards, connected: ink, the health word of the API, Connected by <name>", () => {
  const { html: out } = cards([
    base("atlassian", { connectedBy: ALEX, workspace: { id: "c", name: "northwind.atlassian.net" }, health: "active" }),
    base("github"),
    base("slack"),
  ]);
  assert.match(out, /data-state="on"/);
  assert.match(out, /<span class="gx-tag gx-tag--solid">Active<\/span>/);
  assert.match(out, /Connected by Alex Kim/);
  assert.match(out, />Manage<\/button>/);
});

test("cards, expired: Reconnect required, not ink; a member who left: Stopped; an app off on this server: disabled", () => {
  const { html: out } = cards([
    base("atlassian", { connectedBy: ALEX, workspace: { id: "c", name: "a" }, health: "reconnect_required" }),
    base("github", { connectedBy: null, workspace: { id: "octo", name: "octo" }, health: "none" }),
    base("slack", { available: false }),
  ]);
  assert.deepEqual(
    [...out.matchAll(/data-state="(\w+)"/g)].map((m) => m[1]),
    ["stopped", "stopped", "unavailable"],
  );
  assert.match(out, /<span class="gx-tag">Reconnect required<\/span>/);
  assert.match(out, /<span class="gx-tag">Stopped<\/span>/);
  assert.match(out, /Not on this server/);
  assert.match(out, /disabled="" type="button" class="gx-btn gx-btn--sm">Unavailable/);
});

test("cards: Upload, Google Drive and MCP are listed under them as always on; no uses and no switches on a card", () => {
  const { html: out } = cards([base("atlassian"), base("github"), base("slack")]);
  assert.match(out, /Built in · always on/);
  assert.match(out, /<b>Upload and Google Drive<\/b>/);
  assert.match(out, /<b>MCP<\/b>/);
  assert.doesNotMatch(out, /role="switch"|type="checkbox"/);
});

// ---- the set-up bodies ----

const setup = (integration: Integration, over: Partial<SetupProps> = {}): SetupProps => ({
  projectId: "0b6f-7",
  integration,
  canDisconnect: false,
  returnTo: "/projects/0b6f-7/integrations",
  onConnect: () => Promise.resolve(true),
  onDisconnect: () => Promise.resolve(true),
  onRefresh: noop,
  onError: noop,
  onClose: noop,
  onDone: noop,
  ...over,
});
const active = (app: Integration["app"]) => base(app, { connectedBy: ALEX, workspace: { id: "w", name: "Northwind" }, health: "active" });

test("Atlassian set-up: connect when nobody did, reconnect when it stopped, nothing to press while it works", () => {
  assert.match(shown(createElement(AtlassianSetup, setup(base("atlassian")))).html, />Connect with Atlassian<\/button>/);
  const { html: stopped } = shown(createElement(AtlassianSetup, setup({ ...active("atlassian"), health: "reconnect_required" })));
  assert.match(stopped, />Reconnect Atlassian<\/button>/);
  assert.match(stopped, /The account that connected it has no access now/);
  const { html: working } = shown(createElement(AtlassianSetup, setup(active("atlassian"))));
  assert.doesNotMatch(working, /Connect with Atlassian|Reconnect Atlassian/);
  assert.match(working, /Northwind · Connected by Alex Kim/);
  assert.match(working, /Uses the account of the member who connected it/);
});

test("set-up: Disconnect only when the caller may; Done always", () => {
  assert.match(shown(createElement(AtlassianSetup, setup(active("atlassian"), { canDisconnect: true }))).html, />Disconnect<\/button>/);
  const { html: no } = shown(createElement(AtlassianSetup, setup(active("atlassian"), { canDisconnect: false })));
  assert.doesNotMatch(no, />Disconnect</);
  assert.match(no, />Done<\/button>/);
  assert.match(no, /Uses the account of the member who connected it\./);
});

test("GitHub set-up: connect first; a working binding shows its repositories (loading until read)", () => {
  assert.match(
    shown(createElement(GitHubSetup, setup(base("github")))).html,
    /disabled="" type="button" class="gx-btn">Connect GitHub<\/button>/,
  );
  const { html: working } = shown(createElement(GitHubSetup, setup(active("github"))));
  assert.doesNotMatch(working, /Connect GitHub/);
  assert.match(working, /Loading/);
  const { html: reconnect } = shown(createElement(GitHubSetup, setup({ ...active("github"), health: "reconnect_required" })));
  assert.match(reconnect, />Reconnect GitHub<\/button>/);
});

test("Slack set-up: the command with the project id while no channel is bound; Test connection once it is; a note when the app is off", () => {
  const { html: waiting } = shown(createElement(SlackSetup, setup(base("slack"))));
  assert.match(waiting, /<code class="gx-code">\/specflow connect 0b6f-7<\/code>/);
  assert.match(waiting, /Waiting for the channel\./);
  assert.doesNotMatch(waiting, /Test connection/);
  assert.doesNotMatch(waiting, /Uses the account of the member/, "nothing is connected yet: the rule has no account to name");
  const { html: bound } = shown(createElement(SlackSetup, setup(active("slack"), { canDisconnect: true })));
  assert.match(bound, />Test connection<\/button>/);
  assert.doesNotMatch(bound, /specflow connect/);
  assert.match(bound, />Disconnect<\/button>/);
  assert.match(bound, /Uses the account of the member who connected it\./);
  const { html: off } = shown(createElement(SlackSetup, setup(base("slack", { available: false }))));
  assert.match(off, /Slack is not set up on this server\./);
});
