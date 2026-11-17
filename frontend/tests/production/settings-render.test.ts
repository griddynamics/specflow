// docs/projects/ui.md#general-tab, #errors-shown (FR-PRJ-UI-02..06, UI-10): the General tab rendered on the server for both roles, and its
// rules (error placement, confirm, add, rename, leave) run against fake ports. The switcher refresh (UI-07) and the redirect after leaving
// (UI-08) are proven in the browser e2e; here the port calls and the effects that lead to them are asserted.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";
import { DataError } from "@/lib/domain/errors";
import type { AddedPerson, ClaimsOutcome, MembersPort, MembersState } from "@/lib/domain/members";
import type { Project, ProjectsPort } from "@/lib/domain/projects";
import * as A from "../../components/ui/app-logic";
import type { GeneralViewProps } from "../../components/settings/parts";

stubCss();
async function load() {
  return {
    P: await import("../../components/settings/parts"),
    L: await import("../../components/settings/settings-logic"),
  };
}
let P: Awaited<ReturnType<typeof load>>["P"];
let L: Awaited<ReturnType<typeof load>>["L"];
before(async () => {
  ({ P, L } = await load());
});

const member = (id: string, name: string | null, email: string, role: "owner" | "collaborator" = "collaborator", isYou = false) => ({
  userId: id,
  name,
  email,
  role,
  joinedAt: "2026-01-01T00:00:00Z",
  isYou,
});
const STATE = (role: "owner" | "collaborator"): MembersState => ({
  role,
  limit: 50,
  members: [
    member("u1", "Anna Kowalska", "anna@example.com", "owner", role === "owner"),
    member("u2", "Mark Lee", "mark@example.com", "collaborator", role === "collaborator"),
    member("u3", null, "nameless@example.com"),
  ],
  invites: [{ id: "i1", email: "jonas@example.com", invitedAt: "2026-01-02T00:00:00Z" }],
});

const noop = () => undefined;
const NEW = "new@example.com";
function markup(role: "owner" | "collaborator", over: Partial<GeneralViewProps> = {}): string {
  const add: ReactElement = createElement(P.AddRowView, {
    text: "",
    items: [],
    open: false,
    active: 0,
    busy: false,
    onText: noop,
    onActive: noop,
    onOpen: noop,
    onPick: noop,
    onAdd: noop,
  });
  return renderToStaticMarkup(
    createElement(P.GeneralView, {
      state: STATE(role),
      draft: "Checkout revamp",
      canSave: false,
      saving: false,
      confirm: null,
      confirming: false,
      addSlot: add,
      onDraft: noop,
      onSave: noop,
      onRow: noop,
      onLeave: noop,
      onConfirm: noop,
      onDismiss: noop,
      ...over,
    }),
  );
}
const count = (text: string, re: RegExp) => [...text.matchAll(re)].length;

test("UI-03, UI-06: the owner sees name + Save, the add row, the list with x on every row but the owner's, and the count", () => {
  const view = markup("owner");
  assert.match(view, /data-testid="settings-name"/);
  assert.match(view, /Project name/);
  assert.match(view, /data-testid="settings-save"/);
  assert.match(view, /data-testid="settings-add"/);
  assert.match(view, /role="combobox"/);
  assert.match(view, />Add</);
  assert.equal(count(view, /data-testid="settings-row"/g), 4);
  assert.equal(count(view, /data-testid="settings-row-x"/g), 3); // two members and the invite; not the owner
  assert.match(view, /aria-label="Remove Mark Lee"/);
  assert.match(view, /aria-label="Remove nameless@example.com"/); // no name: the email is the title
  assert.match(view, /aria-label="Cancel invite jonas@example.com"/);
  assert.doesNotMatch(view, /aria-label="Remove Anna Kowalska"/);
  assert.match(view, />Owner</);
  assert.match(view, />Invited</);
  assert.match(view, />Not signed in yet</);
  assert.match(view, />4 of 50</);
  assert.doesNotMatch(view, /settings-leave/);
});

test("UI-04: a collaborator sees the read-only list and Leave project: no name block, no textbox, no Add, no x", () => {
  const view = markup("collaborator");
  assert.doesNotMatch(view, /Project name/);
  assert.doesNotMatch(view, /<input/);
  assert.doesNotMatch(view, /<form/);
  assert.doesNotMatch(view, /role="combobox"/);
  assert.doesNotMatch(view, /settings-add/);
  assert.doesNotMatch(view, /settings-save/);
  assert.equal(count(view, /data-testid="settings-row-x"/g), 0);
  assert.equal(count(view, /data-testid="settings-row"/g), 4);
  assert.match(view, /data-testid="settings-leave"/);
  assert.match(view, />Leave project</);
  assert.match(view, />4 of 50</);
  assert.match(view, />Collaborators</);
});

test("UI-02: the body holds no h1 and no tab row (the tab is in the shell header); Collaborators is a section heading", () => {
  for (const role of ["owner", "collaborator"] as const) {
    const view = markup(role);
    assert.doesNotMatch(view, /<h1/);
    assert.doesNotMatch(view, /gdg-seg/);
    assert.match(view, /<h2[^>]*>Collaborators</);
  }
});

test("UI-06: the count is members plus invites over the limit", () => {
  const state: MembersState = { ...STATE("owner"), members: STATE("owner").members.slice(0, 3), invites: [] };
  assert.match(markup("owner", { state }), />3 of 50</);
});

test("UI-05: remove, cancel and leave each get a dialog that names the person; opening it sends nothing", () => {
  const rows = L.rowsOf(STATE("owner"), true);
  const x = rows.filter((r) => r.action !== null).map((r) => L.confirmOf(r));
  assert.equal(x.length, 3);
  const view = markup("owner", { confirm: x[0] });
  assert.match(view, /role="dialog"/);
  assert.match(view, /Remove this collaborator\?/);
  assert.match(view, /Mark Lee loses access/);
  assert.match(view, /data-testid="settings-confirm-cancel"/);
  assert.match(markup("owner", { confirm: x[2] }), /jonas@example.com will not get access/);
  assert.match(markup("collaborator", { confirm: { kind: "leave" } }), /Leave this project\?/);
  // that opening sends nothing is proven in the e2e (Cancel sends no request); runConfirm is the only path to a port
});

// ---- rules against fake ports ----------------------------------------------------------------------------------------------------

function recorder(
  calls: string[],
  fail?: Partial<Record<"add" | "remove" | "cancelInvite" | "leave", Error>>,
  outcome?: ClaimsOutcome,
): MembersPort {
  const out = outcome ?? { claimsReleased: 1, claimsReleaseFailed: false };
  return {
    list: () => Promise.resolve(STATE("owner")),
    add: (_p, email) => {
      calls.push(`add:${email}`);
      if (fail?.add !== undefined) return Promise.reject(fail.add);
      const added: AddedPerson = { kind: "invite", invite: { id: "i9", email, invitedAt: "x" } };
      return Promise.resolve(added);
    },
    remove: (_p, id) => {
      calls.push(`remove:${id}`);
      return fail?.remove !== undefined ? Promise.reject(fail.remove) : Promise.resolve(out);
    },
    cancelInvite: (_p, id) => {
      calls.push(`cancel:${id}`);
      return fail?.cancelInvite !== undefined ? Promise.reject(fail.cancelInvite) : Promise.resolve();
    },
    leave: () => {
      calls.push("leave");
      return fail?.leave !== undefined ? Promise.reject(fail.leave) : Promise.resolve(out);
    },
    suggest: () => Promise.resolve([]),
  };
}
function effects() {
  const log: string[] = [];
  return {
    log,
    fx: {
      toast: (t: { title: string }) => log.push(`toast:${t.title}`),
      reload: () => log.push("reload"),
      left: () => log.push("left"),
      gone: () => log.push("gone"),
    },
  };
}
const api = (code: DataError["code"], apiCode: string) => new DataError(code, false, { apiCode });

test("UI-10: each add error shows under the field with its text", async () => {
  const cases: [DataError, string][] = [
    [api("validation", "invalid_email"), "Enter a valid email address."],
    [api("validation", "email_domain_not_allowed"), "This email domain cannot sign in to SpecFlow."],
    [api("conflict", "already_member"), "Already a collaborator."],
    [api("conflict", "already_invited"), "Already invited."],
    [api("conflict", "member_limit"), "This project has 50 people. Remove someone first."],
  ];
  for (const [err, text] of cases) {
    const { fx, log } = effects();
    const r = await L.runAdd(NEW, "p1", recorder([], { add: err }), fx);
    assert.deepEqual(r, { ok: false, error: text });
    assert.deepEqual(log, []); // the field says it, no toast
  }
});

test("UI-10: other add failures are a toast; a missing project goes home; a bad format never reaches the port", async () => {
  let t = effects();
  const r = await L.runAdd(NEW, "p1", recorder([], { add: api("unavailable", "database_unavailable") }), t.fx);
  assert.deepEqual(r, { ok: false, error: null });
  assert.deepEqual(t.log, ["toast:Could not save. Try again."]);
  t = effects();
  await L.runAdd(NEW, "p1", recorder([], { add: api("forbidden", "owner_required") }), t.fx);
  assert.deepEqual(t.log, ["toast:Could not save. Try again."]);
  t = effects();
  await L.runAdd(NEW, "p1", recorder([], { add: api("not_found", "project_not_found") }), t.fx);
  assert.deepEqual(t.log, ["gone"]);
  const calls: string[] = [];
  t = effects();
  const bad = await L.runAdd("not-an-email", "p1", recorder(calls), t.fx);
  assert.deepEqual(bad, { ok: false, error: "Enter a valid email address." });
  assert.deepEqual(calls, []);
});

test("UI-03: a successful add sends the trimmed email, toasts and reloads the list", async () => {
  const calls: string[] = [];
  const { fx, log } = effects();
  const r = await L.runAdd("  new@example.com ", "p1", recorder(calls), fx);
  assert.equal(r.ok, true);
  assert.deepEqual(calls, ["add:new@example.com"]);
  assert.deepEqual(log, ["toast:Invite saved", "reload"]);
});

test("UI-05: a confirmed remove and cancel call the port, toast and reload; leave goes through `left`", async () => {
  const calls: string[] = [];
  let t = effects();
  assert.equal(await L.runConfirm({ kind: "remove", userId: "u2", label: "Mark Lee" }, "p1", recorder(calls), t.fx), true);
  assert.deepEqual(t.log, ["toast:Collaborator removed", "reload"]);
  t = effects();
  await L.runConfirm({ kind: "cancel", inviteId: "i1", label: "jonas@example.com" }, "p1", recorder(calls), t.fx);
  assert.deepEqual(t.log, ["toast:Invite cancelled", "reload"]);
  t = effects();
  await L.runConfirm({ kind: "leave" }, "p1", recorder(calls), t.fx);
  assert.deepEqual(t.log, ["toast:You left the project", "left"]);
  assert.deepEqual(calls, ["remove:u2", "cancel:i1", "leave"]);
});

test("UI-10: a stale list reloads with the list-changed toast; claims that could not be freed point to Plan > Work", async () => {
  for (const [kind, code] of [
    ["remove", "member_not_found"],
    ["remove", "cannot_remove_owner"],
    ["cancel", "invite_not_found"],
  ] as const) {
    const t = effects();
    const err = api(code === "cannot_remove_owner" ? "conflict" : "not_found", code);
    const c = kind === "remove" ? ({ kind, userId: "u", label: "x" } as const) : ({ kind, inviteId: "i", label: "x" } as const);
    const ok = await L.runConfirm(c, "p1", recorder([], { remove: err, cancelInvite: err }), t.fx);
    assert.equal(ok, false);
    assert.deepEqual(t.log, ["toast:The list changed. It is up to date now.", "reload"]);
  }
  const t = effects();
  await L.runConfirm(
    { kind: "remove", userId: "u2", label: "Mark Lee" },
    "p1",
    recorder([], undefined, { claimsReleased: 0, claimsReleaseFailed: true }),
    t.fx,
  );
  assert.deepEqual(t.log, ["toast:Some story claims could not be freed. Free the rest in Plan > Work.", "reload"]);
  const l = effects();
  await L.runConfirm({ kind: "leave" }, "p1", recorder([], { leave: api("conflict", "owner_cannot_leave") }), l.fx);
  assert.deepEqual(l.log, ["toast:Could not save. Try again."]); // no redirect when leaving failed
});

function projectsPort(calls: string[], fail?: Error): ProjectsPort {
  return {
    list: () => Promise.resolve([]),
    get: () => Promise.reject(new Error("unused")),
    rename: (id, name) => {
      calls.push(`rename:${id}:${name}`);
      if (fail !== undefined) return Promise.reject(fail);
      const p: Project = { id, name, createdAt: "x", status: "unknown" };
      return Promise.resolve(p);
    },
    subscribe: () => noop,
  };
}

test("UI-07: Save renames through the port (which notifies the switcher) and returns the saved name", async () => {
  const calls: string[] = [];
  const { fx, log } = effects();
  const r = await L.runRename("New name", "p1", projectsPort(calls), fx);
  assert.deepEqual(r, { ok: true, name: "New name" });
  assert.deepEqual(calls, ["rename:p1:New name"]);
  assert.deepEqual(log, ["toast:Project renamed"]);
});

test("the name rule: Save only for a changed, valid name; the messages are the Create Project ones", async () => {
  assert.deepEqual(L.checkName("  Same ", "Same"), { kind: "same" });
  assert.deepEqual(L.checkName(" New ", "Same"), { kind: "ok", name: "New" });
  assert.deepEqual(L.checkName("   ", "Same"), { kind: "empty" });
  assert.deepEqual(L.checkName("x".repeat(201), "Same"), { kind: "too_long" });
  assert.deepEqual(L.checkName("😀".repeat(200), "Same").kind, "ok"); // code points, as the API counts
  assert.equal(L.nameErrorText({ kind: "empty" }), "Enter a project name.");
  assert.equal(L.nameErrorText({ kind: "too_long" }), "Use 200 characters or fewer.");
  assert.equal(L.nameErrorText({ kind: "same" }), undefined);
  const { fx, log } = effects();
  const r = await L.runRename(
    "x",
    "p1",
    projectsPort(
      [],
      new DataError("validation", false, { apiCode: "validation_failed", errors: [{ loc: ["body", "name"], type: "string_too_long" }] }),
    ),
    fx,
  );
  assert.deepEqual(r, { ok: false, error: "Use 200 characters or fewer." });
  const t = effects();
  await L.runRename("x", "p1", projectsPort([], api("forbidden", "owner_required")), t.fx);
  assert.deepEqual(t.log, ["toast:Could not save. Try again."]);
  assert.deepEqual(log, []);
});

test("the email format check accepts plain addresses and refuses the rest", () => {
  for (const ok of ["a@b.co", "first.last+tag@sub.example.com"]) assert.equal(A.isEmailShape(ok), true, ok);
  for (const bad of ["", "a", "a@b", "a@@b.co", "a b@c.de", "@b.co", "a@.co", "a@b..co"]) assert.equal(A.isEmailShape(bad), false, bad);
});

test("F1: ArrowDown before suggestions arrive keeps index 0, and Enter then picks the first one, never undefined", () => {
  const none = { active: -1, count: 0, showList: false, busy: false };
  assert.deepEqual(L.addRowKey("ArrowDown", none), { kind: "move", index: 0, open: true });
  assert.deepEqual(L.addRowKey("ArrowUp", none), { kind: "move", index: 0, open: false });
  assert.deepEqual(L.addRowKey("Enter", none), { kind: "add" });
  assert.equal(L.addRowKey("Enter", { ...none, busy: true }), null);
  // the list is open but still empty: Enter must not pick
  assert.deepEqual(L.addRowKey("Enter", { active: 0, count: 0, showList: true, busy: false }), { kind: "add" });
  // answer arrived with two items
  const two = { active: 0, count: 2, showList: true, busy: false };
  assert.deepEqual(L.addRowKey("Enter", two), { kind: "pick", index: 0 });
  assert.deepEqual(L.addRowKey("ArrowDown", two), { kind: "move", index: 1, open: true });
  assert.deepEqual(L.addRowKey("ArrowDown", { ...two, active: 1 }), { kind: "move", index: 1, open: true });
  assert.deepEqual(L.addRowKey("Escape", two), { kind: "close" });
  assert.equal(L.addRowKey("Escape", { ...two, showList: false }), null);
});

test("suggestions: the list shows only when open and not empty; a row is an option with name and email; the active one is selected", () => {
  const base = {
    text: "mar",
    items: [
      { name: "Maria Nowak", email: "maria@example.com" },
      { name: "Martin Cole", email: "martin@example.com" },
    ],
    open: true,
    active: 1,
    busy: false,
    onText: noop,
    onActive: noop,
    onOpen: noop,
    onPick: noop,
    onAdd: noop,
  };
  const view = renderToStaticMarkup(createElement(P.AddRowView, base));
  assert.match(view, /role="listbox"/);
  assert.equal(count(view, /role="option"/g), 2);
  assert.match(view, /maria@example.com/);
  assert.match(view, /aria-selected="true"[^>]*class="gdg-pp__opt is-active"|class="gdg-pp__opt is-active"[^>]*aria-selected="true"/);
  assert.match(view, /aria-expanded="true"/);
  assert.doesNotMatch(renderToStaticMarkup(createElement(P.AddRowView, { ...base, open: false })), /role="listbox"/);
  assert.doesNotMatch(renderToStaticMarkup(createElement(P.AddRowView, { ...base, items: [] })), /role="listbox"/);
});

test("the add error is rendered under the field", () => {
  const view = renderToStaticMarkup(
    createElement(P.AddRowView, {
      text: "a@b.co",
      error: "Already invited.",
      items: [],
      open: false,
      active: 0,
      busy: false,
      onText: noop,
      onActive: noop,
      onOpen: noop,
      onPick: noop,
      onAdd: noop,
    }),
  );
  assert.match(view, /role="alert"[^>]*>Already invited\./);
  assert.match(view, /aria-invalid="true"/);
});
