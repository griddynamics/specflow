// docs/projects/api.md, ui.md#ports: the production MembersPort, the rename and the create-with-collaborators calls of the http adapter,
// against the stub API (tests/support/stub-api.mjs). The real `lib/api.ts` and adapters run; only `fetch` is replaced, to send the
// `/api/backend/*` URLs to the stub as `/api/v1/*` and to record every request (method, path, body). No BFF, no browser.
// Layer U: the wire parsers and the error mapping. Layer P: every route's request and response mapping, and the error rows.
// Port 3295 (stub API, no issuer).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHttpMembers, toAdded, toClaimsOutcome, toInvite, toMember, toMembersState, toSuggestions } from "@/lib/data/http/members";
import { createHttpProjects } from "@/lib/data/http/projects";
import { toDataError } from "@/lib/data/http/errors";
import { ApiError } from "@/lib/api";
import { DataError, isDataError } from "@/lib/domain/errors";
import type { AddedPerson, Member } from "@/lib/domain/members";
import { rejectsWith as rejects, serveStub } from "../support/stub-fetch";

const PORT = 3295;
const ALICE = "alice.anders@example.test";
const ctx = serveStub(PORT, (api) => {
  api.people.state.claimsReleased = 0;
  api.people.state.claimsFailed = false;
});
const { sent } = ctx;

const members = createHttpMembers();
const { projects, createProject } = createHttpProjects();
const last = () => sent[sent.length - 1];
const OTHER = "22222222-2222-4222-8222-222222222222";

async function newProject(name = "P") {
  const p = await createProject.create({ name });
  sent.length = 0;
  return p.id;
}

// ---- Layer U ----

const wireMember = (over: Record<string, unknown> = {}) => ({
  user_id: "u-1",
  name: "Ann",
  email: "ann@example.test",
  role: "collaborator",
  joined_at: "2026-01-01T00:00:00Z",
  is_you: false,
  ...over,
});
const wireInvite = (over: Record<string, unknown> = {}) => ({
  id: "i-1",
  email: "new@example.test",
  invited_at: "2026-01-02T00:00:00Z",
  ...over,
});
const invalid = (fn: () => unknown) => {
  assert.throws(fn, (e) => isDataError(e) && e.code === "invalid_response" && e.retryable);
};
const memberOf = (added: AddedPerson): Member => {
  if (added.kind !== "member") throw new Error("expected a member");
  return added.member;
};

test("toMember reads a member (name may be null); any other shape is invalid_response", () => {
  assert.deepEqual(toMember(wireMember()), {
    userId: "u-1",
    name: "Ann",
    email: "ann@example.test",
    role: "collaborator",
    joinedAt: "2026-01-01T00:00:00Z",
    isYou: false,
  });
  assert.equal(toMember(wireMember({ name: null })).name, null);
  invalid(() => toMember(wireMember({ role: "superuser" })));
  invalid(() => toMember(wireMember({ user_id: "" })));
  invalid(() => toMember(wireMember({ name: 3 })));
  invalid(() => toMember(wireMember({ is_you: "no" })));
  invalid(() => toMember(null));
});

test("toInvite, toAdded, toClaimsOutcome and toSuggestions check their shapes", () => {
  assert.deepEqual(toInvite(wireInvite()), { id: "i-1", email: "new@example.test", invitedAt: "2026-01-02T00:00:00Z" });
  invalid(() => toInvite(wireInvite({ id: 5 })));
  assert.equal(toAdded({ kind: "member", member: wireMember() }).kind, "member");
  assert.equal(toAdded({ kind: "invite", invite: wireInvite() }).kind, "invite");
  invalid(() => toAdded({ kind: "other" }));
  invalid(() => toAdded({ kind: "member" }));
  assert.deepEqual(toClaimsOutcome({ claims_released: 2, claims_release_failed: true }), { claimsReleased: 2, claimsReleaseFailed: true });
  invalid(() => toClaimsOutcome({ claims_released: -1, claims_release_failed: false }));
  invalid(() => toClaimsOutcome({ claims_released: 1 }));
  assert.deepEqual(toSuggestions({ items: [{ name: "A", email: "a@example.test" }] }), [{ name: "A", email: "a@example.test" }]);
  invalid(() => toSuggestions({ items: [{ name: "A" }] }));
  invalid(() => toSuggestions({}));
  const state = toMembersState({ role: "owner", limit: 50, members: [wireMember({ role: "owner" })], invites: [wireInvite()] });
  assert.equal(state.role, "owner");
  assert.equal(state.invites[0].email, "new@example.test");
  invalid(() => toMembersState({ role: "owner", limit: 0, members: [], invites: [] }));
  invalid(() => toMembersState({ role: "owner", limit: 50, members: {}, invites: [] }));
});

test("error mapping: owner_required is forbidden; the member codes keep their API code; a bare 403 and csrf_rejected stay unknown", () => {
  const code = (status: number, apiCode: string) => toDataError(new ApiError(status, apiCode));
  assert.equal(code(403, "owner_required").code, "forbidden");
  assert.equal(code(403, "owner_required").detail?.apiCode, "owner_required");
  assert.equal(code(403, "csrf_rejected").code, "unknown");
  assert.equal(code(403, "http_403").code, "unknown");
  for (const [status, apiCode, want] of [
    [422, "invalid_email", "validation"],
    [422, "email_domain_not_allowed", "validation"],
    [409, "already_member", "conflict"],
    [409, "already_invited", "conflict"],
    [409, "member_limit", "conflict"],
    [409, "cannot_remove_owner", "conflict"],
    [409, "owner_cannot_leave", "conflict"],
    [404, "member_not_found", "not_found"],
    [404, "invite_not_found", "not_found"],
  ] as const) {
    const e = code(status, apiCode);
    assert.equal(e.code, want, apiCode);
    assert.equal(e.retryable, false);
    assert.equal(e.detail?.apiCode, apiCode);
  }
});

test("a 422 keeps details.errors (loc, type) on the DataError and drops any other entry", () => {
  const e = toDataError(
    new ApiError(422, "validation_failed", "x", undefined, [{ loc: ["body", "collaborators", 1], type: "invalid_email" }]),
  );
  assert.deepEqual(e.detail?.errors, [{ loc: ["body", "collaborators", 1], type: "invalid_email" }]);
  assert.equal(toDataError(new ApiError(422, "validation_failed")).detail?.errors, undefined);
});

// ---- Layer P: the routes ----

test("GET /projects/{pid}/members: the caller's role, the limit, members and invites in order", async () => {
  const pid = await newProject();
  const state = await members.list(pid);
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "GET", path: `/api/backend/projects/${pid}/members`, body: null },
  );
  assert.equal(state.role, "owner");
  assert.equal(state.limit, 50);
  assert.equal(state.members.length, 1);
  assert.equal(state.members[0].role, "owner");
  assert.equal(state.members[0].isYou, true);
  assert.deepEqual(state.invites, []);
});

test("POST /projects/{pid}/members {email}: a signed-in person becomes a member, anyone else an invite", async () => {
  const pid = await newProject();
  const added = await members.add(pid, "  Alice.Anders@Example.test ");
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    {
      method: "POST",
      path: `/api/backend/projects/${pid}/members`,
      body: JSON.stringify({ email: "  Alice.Anders@Example.test " }),
    },
  );
  assert.equal(memberOf(added).email, ALICE);
  assert.equal(memberOf(added).role, "collaborator");
  const invited = await members.add(pid, "newcomer@example.test");
  assert.equal(invited.kind, "invite");
  const state = await members.list(pid);
  assert.deepEqual(
    state.members.map((m) => m.role),
    ["owner", "collaborator"],
  );
  assert.deepEqual(
    state.invites.map((i) => i.email),
    ["newcomer@example.test"],
  );
});

test("add errors: invalid_email and email_domain_not_allowed (validation), already_member and already_invited (conflict), member_limit", async () => {
  const pid = await newProject();
  await rejects(members.add(pid, "not-an-email"), "validation", "invalid_email");
  await rejects(members.add(pid, "someone@elsewhere.test"), "validation", "email_domain_not_allowed");
  await members.add(pid, "bob.brown@example.test");
  await rejects(members.add(pid, "BOB.brown@example.test"), "conflict", "already_member");
  await rejects(members.add(pid, "dev@example.test"), "conflict", "already_member"); // the owner's own email
  await members.add(pid, "x1@example.test");
  await rejects(members.add(pid, "X1@example.test"), "conflict", "already_invited");
  for (let i = 2; i < 49; i++) await members.add(pid, `x${String(i)}@example.test`); // 1 owner + 1 member + 48 invites = 50
  await rejects(members.add(pid, "one-too-many@example.test"), "conflict", "member_limit");
});

test("DELETE /projects/{pid}/members/{user_id}: ClaimsOutcome with the released count and the failure flag", async () => {
  const pid = await newProject();
  const userId = memberOf(await members.add(pid, ALICE)).userId;
  ctx.api.people.state.claimsReleased = 2;
  const out = await members.remove(pid, userId);
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "DELETE", path: `/api/backend/projects/${pid}/members/${userId}`, body: null },
  );
  assert.deepEqual(out, { claimsReleased: 2, claimsReleaseFailed: false });
  ctx.api.people.state.claimsFailed = true;
  const again = memberOf(await members.add(pid, ALICE));
  const out2 = await members.remove(pid, again.userId);
  assert.equal(out2.claimsReleaseFailed, true);
  assert.equal((await members.list(pid)).members.length, 1);
});

test("remove errors: member_not_found (also for a malformed id) and cannot_remove_owner", async () => {
  const pid = await newProject();
  await rejects(members.remove(pid, OTHER), "not_found", "member_not_found");
  await rejects(members.remove(pid, "not-a-uuid"), "not_found", "member_not_found");
  const owner = (await members.list(pid)).members[0];
  await rejects(members.remove(pid, owner.userId), "conflict", "cannot_remove_owner");
});

test("DELETE /projects/{pid}/invites/{invite_id}: 204 cancels; an unknown or malformed id is invite_not_found", async () => {
  const pid = await newProject();
  await members.add(pid, "pending@example.test");
  const [invite] = (await members.list(pid)).invites;
  await members.cancelInvite(pid, invite.id);
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "DELETE", path: `/api/backend/projects/${pid}/invites/${invite.id}`, body: null },
  );
  assert.deepEqual((await members.list(pid)).invites, []);
  await rejects(members.cancelInvite(pid, invite.id), "not_found", "invite_not_found");
  await rejects(members.cancelInvite(pid, "nope"), "not_found", "invite_not_found");
});

test("a collaborator: 403 owner_required (forbidden) on every owner route, reads allowed, POST /leave frees the way out", async () => {
  const pid = await newProject();
  ctx.api.people.seed(pid, {
    role: "collaborator",
    members: [{ email: "peer@example.test", name: "Pat Peer" }],
    invites: ["wait@example.test"],
  });
  const state = await members.list(pid);
  assert.equal(state.role, "collaborator");
  assert.equal(state.members[0].role, "owner");
  assert.equal(
    state.members.some((m) => m.isYou),
    true,
  );
  await rejects(members.add(pid, "x@example.test"), "forbidden", "owner_required");
  await rejects(members.remove(pid, state.members[1].userId), "forbidden", "owner_required");
  await rejects(members.cancelInvite(pid, state.invites[0].id), "forbidden", "owner_required");
  await rejects(projects.rename(pid, "New"), "forbidden", "owner_required");
  assert.equal((await members.list(pid)).members.length, state.members.length, "a 403 changes nothing");
  const out = await members.leave(pid);
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "POST", path: `/api/backend/projects/${pid}/leave`, body: null },
  );
  assert.deepEqual(out, { claimsReleased: 0, claimsReleaseFailed: false });
  await rejects(members.list(pid), "not_found", "project_not_found"); // access ended: the next read is 404
});

test("POST /leave: the owner cannot leave (owner_cannot_leave)", async () => {
  const pid = await newProject();
  await rejects(members.leave(pid), "conflict", "owner_cannot_leave");
});

test("a non-member gets project_not_found on every route", async () => {
  await rejects(members.list(OTHER), "not_found", "project_not_found");
  await rejects(members.add(OTHER, "a@example.test"), "not_found", "project_not_found");
  await rejects(members.leave(OTHER), "not_found", "project_not_found");
  await rejects(projects.rename(OTHER, "X"), "not_found", "project_not_found");
  await rejects(members.suggest("ali", OTHER), "not_found", "project_not_found");
});

test("POST /users/suggestions: {q} for Create Project, {q, project_id} for Settings; the typed text and emails stay out of the URL", async () => {
  const pid = await newProject();
  await rejects(members.suggest("al"), "validation", "validation_failed"); // below 3 characters
  sent.length = 0;
  const items = await members.suggest("ali");
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "POST", path: "/api/backend/users/suggestions", body: JSON.stringify({ q: "ali" }) },
  );
  assert.deepEqual(items, [{ name: "Alice Anders", email: ALICE }]);
  await members.add(pid, ALICE);
  sent.length = 0;
  assert.deepEqual(await members.suggest("ali", pid), [], "members are left out");
  assert.equal(last().body, JSON.stringify({ q: "ali", project_id: pid }));
  assert.deepEqual(
    (await members.suggest("ala", pid)).map((s) => s.email),
    ["alan.turner@example.test"],
  );
  assert.deepEqual(
    (await members.suggest("mal")).map((s) => s.email),
    [],
    "a domain that cannot sign in is left out",
  );
  for (const s of sent) assert.doesNotMatch(s.path, /@|%40/, "no email in a URL");
});

test("PATCH /projects/{pid} {name}: renames, answers the project and tells the switcher", async () => {
  const pid = await newProject("Before");
  let notified = 0;
  const off = projects.subscribe(() => {
    notified++;
  });
  const renamed = await projects.rename(pid, "  After  ");
  off();
  assert.deepEqual(
    { method: last().method, path: last().path, body: last().body },
    { method: "PATCH", path: `/api/backend/projects/${pid}`, body: JSON.stringify({ name: "After" }) },
  );
  assert.equal(renamed.name, "After");
  assert.equal((await projects.get(pid)).name, "After");
  assert.equal(notified, 1);
  sent.length = 0;
  await assert.rejects(projects.rename(pid, "   "), (e) => e instanceof DataError && e.code === "validation");
  await assert.rejects(projects.rename(pid, "x".repeat(201)), (e) => e instanceof DataError && e.code === "validation");
  assert.equal(sent.length, 0, "the name rule is checked before any request");
});

test("POST /projects: {name} without emails, {name, collaborators} with at least one", async () => {
  await createProject.create({ name: "Plain" });
  assert.equal(last().body, JSON.stringify({ name: "Plain" }));
  await createProject.create({ name: "Plain2", collaboratorEmails: [] });
  assert.equal(last().body, JSON.stringify({ name: "Plain2" }));
  const created = await createProject.create({ name: "Team", collaboratorEmails: [ALICE, "wait@example.test"] });
  assert.equal(last().body, JSON.stringify({ name: "Team", collaborators: [ALICE, "wait@example.test"] }));
  const state = await members.list(created.id);
  assert.deepEqual(
    state.members.map((m) => m.email),
    ["dev@example.test", ALICE],
  );
  assert.deepEqual(
    state.invites.map((i) => i.email),
    ["wait@example.test"],
  );
});

test("create with bad emails: 422 validation with details.errors[{loc, type}] and nothing written", async () => {
  const before = (await projects.list()).length;
  await assert.rejects(
    createProject.create({ name: "Bad", collaboratorEmails: ["ok@example.test", "nope", "x@elsewhere.test"] }),
    (e) =>
      isDataError(e) &&
      e.code === "validation" &&
      e.detail?.apiCode === "validation_failed" &&
      JSON.stringify(e.detail.errors) ===
        JSON.stringify([
          // every loc part is a string, as the real API sends it (api/errors.py)
          { loc: ["body", "collaborators", "1"], type: "invalid_email" },
          { loc: ["body", "collaborators", "2"], type: "email_domain_not_allowed" },
        ]),
  );
  const many = Array.from({ length: 50 }, (_, i) => `m${String(i)}@example.test`);
  await assert.rejects(
    createProject.create({ name: "Many", collaboratorEmails: many }),
    (e) => isDataError(e) && JSON.stringify(e.detail?.errors) === JSON.stringify([{ loc: ["body", "collaborators"], type: "too_long" }]),
  );
  assert.equal((await projects.list()).length, before);
});

test("the leaver's project is gone from the list; the others stay", async () => {
  const keep = await newProject("Keep");
  const gone = await newProject("Gone");
  ctx.api.people.seed(gone, { role: "collaborator" });
  await members.leave(gone);
  const ids = (await projects.list()).map((p) => p.id);
  assert.ok(ids.includes(keep));
  assert.ok(!ids.includes(gone));
});
