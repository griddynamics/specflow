// Stub of the project collaborators API (docs/projects/api.md), in memory: members, invites, leave, rename, suggestions and the
// `collaborators` of Create Project. A test double next to stub-api.mjs, close enough to drive the http adapter and the BFF; the
// real API lives in backend/src/api. One caller (the stub's `/me`), with a role per project: `admin` for a project it created, or
// whatever `seed` says. Answers and codes follow the spec: 404 `project_not_found` for a non-member, 403 `admin_required`, the
// 409/422 codes of api.md#errors, `details.errors[{loc, type}]` for a 422 of create.
import crypto from "node:crypto";

/** Thrown for an error answer; `details` is the envelope's (the 422 of create and rename carries `errors`). */
export class StubError extends Error {
  constructor(status, code, details = {}) {
    super(code);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const ME = { id: "11111111-1111-4111-8111-111111111111", name: "dev", email: "dev@example.test" };
const LIMIT = 50;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** api.md#email-rules: strip, lower-case; one `@`, non-empty parts, no whitespace or control character, at most 254 characters. */
export function normaliseEmail(raw) {
  if (typeof raw !== "string") return null;
  const e = raw.trim().toLowerCase();
  const parts = e.split("@");
  if (e.length > 254 || parts.length !== 2 || parts[0] === "" || parts[1] === "") return null;
  const hasControl = Array.from(e).some((ch) => ch.charCodeAt(0) < 32 || ch.charCodeAt(0) === 127);
  return hasControl || /\s/.test(e) ? null : e;
}

const validation = (...errors) => new StubError(422, "validation_failed", { errors });
const only = (body, keys) =>
  typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).every((k) => keys.includes(k));

export function createMembersModel() {
  const state = {
    /** Domains that may sign in (AUTH_ALLOWED_EMAIL_DOMAINS). */
    allowedDomains: ["example.test"],
    /** What a removal or a leave reports: story claims freed, and whether freeing some failed. */
    claimsReleased: 0,
    claimsFailed: false,
    /** Users who signed in before: the suggestion directory and the "member" outcome of an add. */
    directory: [
      { name: "Alice Anders", email: "alice.anders@example.test" },
      { name: "Alan Turner", email: "alan.turner@example.test" },
      { name: "Bob Brown", email: "bob.brown@example.test" },
      { name: "alone@example.test", email: "alone@example.test" },
      { name: "Mallory Elsewhere", email: "mallory@other.test" },
    ],
  };
  const projects = new Map(); // pid -> {role, members[], invites[]}
  const userIds = new Map(); // email -> user id
  let tick = 0;
  const stamp = () => new Date(Date.UTC(2026, 0, 1, 0, 0, ++tick)).toISOString();
  const idOf = (email) => userIds.get(email) ?? userIds.set(email, crypto.randomUUID()).get(email);
  const member = (email, name, role) => ({ user_id: idOf(email), name, email, role, joined_at: stamp(), is_you: email === ME.email });
  const invite = (email) => ({ id: crypto.randomUUID(), email, invited_at: stamp() });
  userIds.set(ME.email, ME.id);

  const get = (pid) => {
    const p = projects.get(pid);
    if (!p) throw new StubError(404, "project_not_found");
    return p;
  };
  const admin = (pid) => {
    const p = get(pid);
    if (p.role !== "admin") throw new StubError(403, "admin_required");
    return p;
  };
  const domainOk = (email) => state.allowedDomains.includes(email.split("@")[1]);
  const people = (p) => p.members.length + p.invites.length;

  /** Registers a project the caller created (admin) with already checked collaborators. */
  function create(pid, emails = []) {
    const members = [member(ME.email, ME.name, "admin")];
    const invites = [];
    for (const email of emails) {
      const known = state.directory.find((u) => u.email === email);
      if (known) members.push(member(email, known.name === email ? null : known.name, "collaborator"));
      else invites.push(invite(email));
    }
    projects.set(pid, { role: "admin", members, invites });
  }

  /** Create Project body checks (api.md#create-with-collaborators): all-or-nothing; returns the emails to keep, in order. */
  function collaboratorsOf(body) {
    if (body.collaborators === undefined) return [];
    const list = body.collaborators;
    if (!Array.isArray(list)) throw validation({ loc: ["body", "collaborators"], type: "list_type" });
    if (list.length > LIMIT - 1) throw validation({ loc: ["body", "collaborators"], type: "too_long" });
    const errors = [];
    const kept = [];
    list.forEach((raw, i) => {
      const email = normaliseEmail(raw);
      // Like the real API (`api/errors.py`): every `loc` part is a string, so the index is "0", "1", ...
      if (email === null) errors.push({ loc: ["body", "collaborators", String(i)], type: "invalid_email" });
      else if (!domainOk(email)) errors.push({ loc: ["body", "collaborators", String(i)], type: "email_domain_not_allowed" });
      else if (email !== ME.email && !kept.includes(email)) kept.push(email);
    });
    if (errors.length > 0) throw validation(...errors);
    return kept;
  }

  /**
   * Test seam: a project where the caller has `role`, with the given members and invite emails (the admin comes first).
   * @param {string} pid
   * @param {{role?: "admin" | "collaborator", members?: {email: string, name?: string | null, role?: string}[], invites?: string[]}} spec
   */
  function seed(pid, { role = "admin", members = [], invites = [] }) {
    const rows = members.map((m) => member(m.email, m.name ?? null, m.role ?? "collaborator"));
    if (!rows.some((m) => m.role === "admin")) {
      const owner = role === "admin" ? ME : { email: "owner@example.test", name: "Olivia Owner" };
      rows.unshift(member(owner.email, owner.name, "admin"));
    }
    if (role !== "admin" && !rows.some((m) => m.is_you)) rows.push(member(ME.email, ME.name, "collaborator"));
    rows.sort((a, b) => (a.role === "admin" ? -1 : b.role === "admin" ? 1 : 0));
    projects.set(pid, { role, members: rows, invites: invites.map(invite) });
  }

  function rename(pid, body) {
    admin(pid);
    if (!only(body, ["name"])) throw validation({ loc: ["body"], type: "extra_forbidden" });
    const name = typeof body.name === "string" ? body.name.trim() : "";
    const length = Array.from(name).length;
    if (length < 1 || length > 200) throw validation({ loc: ["body", "name"], type: length < 1 ? "string_too_short" : "string_too_long" });
    return name;
  }

  function list(pid) {
    const p = get(pid);
    return { role: p.role, limit: LIMIT, members: p.members, invites: p.invites };
  }

  function add(pid, body) {
    const p = admin(pid);
    if (!only(body, ["email"])) throw validation({ loc: ["body"], type: "extra_forbidden" });
    const email = normaliseEmail(body.email);
    if (email === null) throw new StubError(422, "invalid_email");
    if (!domainOk(email)) throw new StubError(422, "email_domain_not_allowed");
    if (people(p) >= LIMIT) throw new StubError(409, "member_limit");
    const known = state.directory.find((u) => u.email === email) ?? (email === ME.email ? ME : null);
    if (known) {
      if (p.members.some((m) => m.email === email)) throw new StubError(409, "already_member");
      const row = member(email, known.name === email ? null : known.name, "collaborator");
      p.members.push(row);
      return { kind: "member", member: row };
    }
    if (p.invites.some((i) => i.email === email)) throw new StubError(409, "already_invited");
    const row = invite(email);
    p.invites.push(row);
    return { kind: "invite", invite: row };
  }

  const outcome = (released = 0, failed = false) => ({ claims_released: released, claims_release_failed: failed });

  function remove(pid, userId) {
    const p = admin(pid);
    const at = UUID.test(userId) ? p.members.findIndex((m) => m.user_id === userId) : -1;
    if (at < 0) throw new StubError(404, "member_not_found");
    if (p.members[at].role === "admin") throw new StubError(409, "cannot_remove_admin");
    p.members.splice(at, 1);
    return outcome(state.claimsReleased, state.claimsFailed);
  }

  function cancel(pid, inviteId) {
    const p = admin(pid);
    const at = UUID.test(inviteId) ? p.invites.findIndex((i) => i.id === inviteId) : -1;
    if (at < 0) throw new StubError(404, "invite_not_found");
    p.invites.splice(at, 1);
  }

  function leave(pid) {
    const p = get(pid);
    if (p.role === "admin") throw new StubError(409, "admin_cannot_leave");
    projects.delete(pid); // the caller is no member any more: the next read answers 404
    return outcome(state.claimsReleased, state.claimsFailed);
  }

  /** api.md#suggestions. */
  function suggest(body) {
    if (!only(body, ["q", "project_id"])) throw validation({ loc: ["body"], type: "extra_forbidden" });
    const q = typeof body.q === "string" ? body.q.trim().toLowerCase() : "";
    const length = Array.from(q).length;
    if (length < 3 || length > 254) throw validation({ loc: ["body", "q"], type: length < 3 ? "string_too_short" : "string_too_long" });
    const taken = new Set([ME.email]);
    if (body.project_id !== undefined && body.project_id !== null) {
      const p = typeof body.project_id === "string" ? projects.get(body.project_id) : undefined;
      if (!p) throw new StubError(404, "project_not_found");
      p.members.forEach((m) => taken.add(m.email));
      p.invites.forEach((i) => taken.add(i.email));
    }
    const items = state.directory
      .filter((u) => domainOk(u.email) && !taken.has(u.email))
      .filter(
        (u) =>
          u.email.startsWith(q) ||
          u.name
            .toLowerCase()
            .split(/\s+/)
            .some((w) => w.startsWith(q)) ||
          u.name.toLowerCase().startsWith(q),
      )
      .sort((a, b) => a.name.localeCompare(b.name) || a.email.localeCompare(b.email))
      .slice(0, 8)
      .map((u) => ({ name: u.name, email: u.email }));
    return { items };
  }

  return { state, has: (pid) => projects.has(pid), create, collaboratorsOf, seed, rename, list, add, remove, cancel, leave, suggest };
}
