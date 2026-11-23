// Stub of the Jira settings API (docs/jira-sync/api.md#web-routes), in memory: the target of a project, the owner rule, the access check
// with scripted answers and the `available` switch (the server has no Atlassian account: 503 `jira_unavailable`). A test double next to
// stub-api.mjs, close enough to drive the http adapter, the BFF and the Settings tab; the real API lives in backend/src/api. It never
// calls Jira and never starts a sync: the status of a target is what `seed` says (a seeded `sync` may carry `progress`: {step, done, total}). The caller's role comes from the members model.
import { StubError } from "./stub-members.mjs";

const SITE_URL = "https://example.atlassian.net";
const PROJECT = /^[A-Z][A-Z0-9_]{1,9}$/;
const EPIC = /^[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]*$/;
const OK = { result: "ok", project_name: "Payments platform", epic_summary: "Checkout revamp" };

const validation = (...errors) => new StubError(422, "validation_failed", { errors });
const only = (body, keys) =>
  typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).every((k) => keys.includes(k));

/** `roleOf(pid)` is the caller's role in the project: "owner" or "collaborator" (throws 404 for a non-member). */
export function createJiraModel(roleOf) {
  const state = { available: true };
  const targets = new Map(); // pid -> {jira_project, epic_key, created_epic_key, enabled, sync}
  const access = new Map(); // pid -> "ok" | {reason, field}

  const idleSync = (pid, t) => ({
    project_id: pid,
    enabled: t.enabled,
    status: "idle",
    queued: false,
    trigger: null,
    started_at: null,
    finished_at: null,
    error_code: null,
    counts: null,
    progress: null,
    jira_project: t.jira_project,
    epic_key: t.epic_key ?? t.created_epic_key ?? null,
  });

  function out(pid) {
    const t = targets.get(pid);
    return {
      available: state.available,
      editable: roleOf(pid) === "owner",
      site_url: state.available ? SITE_URL : null,
      jira_project: t?.jira_project ?? null,
      epic_key: t?.epic_key ?? null,
      created_epic_key: t?.created_epic_key ?? null,
      enabled: t?.enabled ?? false,
      sync: t === undefined ? null : (t.sync ?? idleSync(pid, t)),
    };
  }

  /** The body rules of a PUT and of the access check: keys as api.md#web-routes says, trimmed and upper-cased. */
  function keysOf(body, extra) {
    if (!only(body, ["jira_project", "epic_key", ...extra])) throw validation({ loc: ["body"], type: "extra_forbidden" });
    const project = typeof body.jira_project === "string" ? body.jira_project.trim().toUpperCase() : "";
    const raw = typeof body.epic_key === "string" ? body.epic_key.trim().toUpperCase() : "";
    const epic = raw === "" ? null : raw;
    const errors = [];
    if (!PROJECT.test(project)) errors.push({ loc: ["body", "jira_project"], type: "string_pattern_mismatch" });
    if (epic !== null && !(EPIC.test(epic) && epic.startsWith(`${project}-`)))
      errors.push({ loc: ["body", "epic_key"], type: "string_pattern_mismatch" });
    if (errors.length > 0) throw validation(...errors);
    return { project, epic };
  }

  function asOwner(pid) {
    if (roleOf(pid) !== "owner") throw new StubError(403, "owner_required");
    if (!state.available) throw new StubError(503, "jira_unavailable");
  }

  function put(pid, body) {
    asOwner(pid);
    const { project, epic } = keysOf(body, ["enabled"]);
    if (typeof body.enabled !== "boolean") throw validation({ loc: ["body", "enabled"], type: "bool_type" });
    const before = targets.get(pid);
    const same = before?.jira_project === project;
    targets.set(pid, {
      jira_project: project,
      epic_key: epic,
      created_epic_key: same ? before.created_epic_key : null,
      enabled: body.enabled,
      sync: same ? before.sync : undefined, // a new Jira project starts from nothing
    });
    return out(pid);
  }

  function check(pid, body) {
    asOwner(pid);
    const { epic } = keysOf(body, []);
    const scripted = access.get(pid) ?? "ok";
    if (scripted === "ok") return { ...OK, epic_summary: epic === null ? null : OK.epic_summary };
    return { result: "failed", reason: scripted.reason, field: scripted.field ?? null };
  }

  /**
   * Test seam (`POST /seed-jira`): the switch, the target of a project (null removes it) and the next access-check answer.
   * @param {{pid: string, available?: boolean, target?: object | null, access?: "ok" | {reason: string, field?: string | null}}} spec
   */
  function seed({ pid, available, target, access: next }) {
    if (typeof available === "boolean") state.available = available;
    if (target === null) targets.delete(pid);
    else if (target !== undefined) targets.set(pid, { created_epic_key: null, sync: undefined, ...target });
    if (next !== undefined) access.set(pid, next);
  }

  return { state, get: out, put, check, seed };
}
