// Stub of the Jira settings API (docs/jira-sync/api.md#web-routes), in memory: the target of a project, the owner rule, the access check
// with scripted answers and the `available` switch (the server has no Atlassian account: 503 `jira_unavailable`). A test double next to
// stub-api.mjs, close enough to drive the http adapter, the BFF and the Settings tab; the real API lives in backend/src/api. It never
// calls Jira and never starts a sync: the status of a target is what `seed` says (a seeded `sync` may carry `progress`: {step, done, total}). The caller's role comes from the members model.
// The Atlassian connection (docs/jira-sync/oauth.md#routes): `state.mode` is "token" (default, the settings carry no `connection`), "oauth"
// or null (off); `state.connection` is the caller's own one (null, or {status, account_name}); a target remembers who saved it last
// (`acting`: "me", or a seeded {connected_by_name, connected_by_me, status}). The consent is not run: `authorize` answers an Atlassian URL
// whose `state` carries `return_to` like the API's (`nonce.payload.signature`), and `callback` accepts any code with such a state unless
// `state.callbackError` names a route code.
import { StubError } from "./stub-members.mjs";

const SITE_URL = "https://example.atlassian.net";
const PROJECT = /^[A-Z][A-Z0-9_]{1,9}$/;
const EPIC = /^[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]*$/;
const OK = { result: "ok", project_name: "Payments platform", epic_summary: "Checkout revamp" };

const validation = (...errors) => new StubError(422, "validation_failed", { errors });
const only = (body, keys) =>
  typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).every((k) => keys.includes(k));

/** `roleOf(pid)` is the caller's role in the project: "owner" or "collaborator" (throws 404 for a non-member). */
const CALLBACK_STATUS = {
  atlassian_state_invalid: 400,
  atlassian_code_invalid: 400,
  atlassian_site_not_granted: 403,
  atlassian_unreachable: 502,
  atlassian_oauth_unavailable: 503,
};
export const AUTHORIZE_URL = "https://auth.atlassian.com/authorize";
const STATE_SIGNATURE = "stub-signature";
const sameSite = (r) => typeof r === "string" && r.startsWith("/") && !r.startsWith("//") && !/[\\\s]/.test(r) && r.length <= 2048;

export function createJiraModel(roleOf) {
  /** @type {{available: boolean, mode: "token" | "oauth" | null, connection: {status: string, account_name: string, connected_at?: string, last_used_at?: string} | null, callbackError: string | null}} */
  const state = { available: true, mode: "token", connection: null, callbackError: null };
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

  /** The acting account of a target in the `oauth` mode (`connection` of JiraSettingsOut). */
  function actingOf(t) {
    if (state.mode !== "oauth" || t === undefined) return null;
    if (t.acting === "me") {
      const mine = state.connection;
      return mine === null
        ? { connected_by_name: null, connected_by_me: true, status: "missing" }
        : { connected_by_name: mine.account_name, connected_by_me: true, status: mine.status };
    }
    return t.acting ?? { connected_by_name: null, connected_by_me: false, status: "missing" };
  }

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
      ...(state.mode === "oauth" ? { connection: actingOf(t) } : {}),
    };
  }

  /** GET /integrations/atlassian: every mode answers; only `oauth` has a connection. */
  function connection() {
    const mine = state.mode === "oauth" ? state.connection : null;
    return {
      mode: state.available ? state.mode : null,
      connected: mine !== null,
      status: mine?.status ?? null,
      account_name: mine?.account_name ?? null,
      connected_at: mine === null ? null : (mine.connected_at ?? "2026-11-20T09:30:00Z"),
      last_used_at: mine === null ? null : (mine.last_used_at ?? "2026-11-22T16:00:00Z"),
    };
  }

  function asOAuth() {
    if (!state.available || state.mode !== "oauth") throw new StubError(503, "atlassian_oauth_unavailable");
  }

  function authorize(body) {
    if (!only(body, ["return_to"]) || !sameSite(body.return_to)) throw validation({ loc: ["body", "return_to"], type: "value_error" });
    asOAuth();
    const payload = Buffer.from(JSON.stringify({ u: "u1", r: body.return_to, e: Math.floor(Date.now() / 1000) + 600 })).toString(
      "base64url",
    );
    const url = new URL(AUTHORIZE_URL);
    url.searchParams.set("client_id", "stub-client");
    url.searchParams.set("response_type", "code");
    url.searchParams.set("prompt", "consent");
    url.searchParams.set("state", `nonce.${payload}.${STATE_SIGNATURE}`);
    return { url: url.href };
  }

  function callback(body) {
    if (!only(body, ["code", "state"]) || typeof body.code !== "string" || typeof body.state !== "string" || body.code === "")
      throw validation({ loc: ["body"], type: "missing" });
    asOAuth();
    if (state.callbackError !== null) throw new StubError(CALLBACK_STATUS[state.callbackError] ?? 400, state.callbackError);
    const [, payload, signature] = body.state.split(".");
    if (signature !== STATE_SIGNATURE) throw new StubError(400, "atlassian_state_invalid");
    const { r } = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    state.connection = { status: "active", account_name: "Ann Lee", connected_at: new Date().toISOString() };
    return { connection: connection(), return_to: r };
  }

  function disconnect() {
    asOAuth();
    state.connection = null;
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
    if (state.mode === "oauth" && state.connection?.status !== "active") throw new StubError(409, "jira_not_connected");
    const before = targets.get(pid);
    const same = before?.jira_project === project;
    targets.set(pid, {
      jira_project: project,
      epic_key: epic,
      created_epic_key: same ? before.created_epic_key : null,
      enabled: body.enabled,
      sync: same ? before.sync : undefined, // a new Jira project starts from nothing
      acting: "me", // every PUT makes the saving owner the acting account
    });
    return out(pid);
  }

  function check(pid, body) {
    asOwner(pid);
    const { epic } = keysOf(body, []);
    if (state.mode === "oauth" && state.connection === null) return { result: "failed", reason: "jira_not_connected", field: null };
    if (state.mode === "oauth" && state.connection.status !== "active")
      return { result: "failed", reason: "jira_reconnect_required", field: null };
    const scripted = access.get(pid) ?? "ok";
    if (scripted === "ok") return { ...OK, epic_summary: epic === null ? null : OK.epic_summary };
    return { result: "failed", reason: scripted.reason, field: scripted.field ?? null };
  }

  /**
   * Test seam (`POST /seed-jira`): the switch, the target of a project (null removes it) and the next access-check answer; and the
   * global Atlassian knobs: `mode` ("token" | "oauth" | null), the caller's `connection` (null or {status, account_name}) and
   * `callbackError` (null or a route code). A target's `acting` is "me" or {connected_by_name, connected_by_me, status}.
   * @param {{pid: string, available?: boolean, target?: object | null, access?: "ok" | {reason: string, field?: string | null},
   *   mode?: "token" | "oauth" | null, connection?: object | null, callbackError?: string | null}} spec
   */
  function seed({ pid, available, target, access: next, mode, connection: mine, callbackError }) {
    if (typeof available === "boolean") state.available = available;
    if (mode !== undefined) state.mode = mode;
    if (mine !== undefined) state.connection = mine;
    if (callbackError !== undefined) state.callbackError = callbackError;
    if (target === null) targets.delete(pid);
    else if (target !== undefined) targets.set(pid, { created_epic_key: null, sync: undefined, ...target });
    if (next !== undefined) access.set(pid, next);
  }

  return { state, get: out, put, check, seed, connection, authorize, callback, disconnect };
}
