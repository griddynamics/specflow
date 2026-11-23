// Stub of the GitHub API of plans/GITHUB-INTEGRATION/contract.md#api, in memory: the caller's GitHub connection, the repositories that
// connection can read, and the repositories of each project with a simulated clone check. A test double next to stub-api.mjs, close
// enough to drive the http adapter, the BFF and the Settings tab; the real API lives in backend/src/api. It never calls GitHub.
// `state.enabled` false = GitHub is not configured (503 `github_unavailable`). The authorize and install URLs carry a `state` with
// `return_to` like the API's (`nonce.payload.signature`); `callback` and `setup` accept such a state unless `state.callbackError` /
// `state.setupError` names a route code. An added repository starts `pending`, turns `cloning`, then after `state.cloneMs` ends as
// `state.cloneOutcome` ("cloned" or a failure code); `cloneMs` null keeps it running.
import { randomUUID } from "node:crypto";
import { StubError } from "./stub-members.mjs";

export const AUTHORIZE_URL = "https://github.com/login/oauth/authorize";
export const INSTALL_URL = "https://github.com/apps/specflow-stub/installations/new";
export const MANAGE_URL = "https://github.com/settings/installations";
const STATE_SIGNATURE = "stub-signature";
const ROUTE_STATUS = { github_state_invalid: 400, github_code_invalid: 400, github_unreachable: 502, github_unavailable: 503 };
const SIZE_CAP_KB = 1048576;

const validation = (loc) => new StubError(422, "validation_failed", { errors: [{ loc, type: "value_error" }] });
const only = (body, keys) =>
  typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).every((k) => keys.includes(k));
const sameSite = (r) => typeof r === "string" && r.startsWith("/") && !r.startsWith("//") && !/[\\\s]/.test(r) && r.length <= 2048;

/** The repositories a fresh stub's caller can read: two small ones (one private) and one over the size cap. */
export const DEFAULT_AVAILABLE = [
  { github_repo_id: 101, full_name: "acme/payments-api", private: true, default_branch: "main", size_kb: 2048 },
  { github_repo_id: 102, full_name: "acme/web-app", private: false, default_branch: "develop", size_kb: 512 },
  { github_repo_id: 103, full_name: "acme/monorepo", private: true, default_branch: "main", size_kb: SIZE_CAP_KB + 1 },
];

const repoOut = (r) => ({
  github_repo_id: r.github_repo_id,
  full_name: r.full_name,
  html_url: r.html_url ?? `https://github.com/${r.full_name}`,
  private: r.private ?? false,
  default_branch: r.default_branch ?? null,
  size_kb: r.size_kb ?? 0,
  too_large: (r.size_kb ?? 0) > SIZE_CAP_KB,
});

/** `roleOf(pid)` is the caller's role in the project ("owner" or "collaborator"); `displayName` the caller's Specflow name. */
export function createGitHubModel(roleOf, displayName = "dev") {
  /** @type {{enabled: boolean, connection: {status: string, login: string, connected_at?: string, last_used_at?: string} | null,
   *   callbackError: string | null, setupError: string | null, available: object[], truncated: boolean, availableError: string | null,
   *   cloneMs: number | null, cloneOutcome: string}} */
  const state = {
    enabled: true,
    connection: null, // null or {status: "active" | "expired", login, connected_at?}
    callbackError: null,
    setupError: null,
    available: DEFAULT_AVAILABLE,
    truncated: false,
    availableError: null, // null or an API code the repository list answers with
    cloneMs: 1500,
    cloneOutcome: "cloned",
  };
  const projects = new Map(); // pid -> ProjectRepositoryOut[] (newest first)
  const timers = new Set();
  const later = (ms, fn) => {
    if (ms === null || ms === Infinity) return;
    const t = setTimeout(() => {
      timers.delete(t);
      fn();
    }, ms);
    timers.add(t);
  };

  function connection() {
    const mine = state.enabled ? state.connection : null;
    return {
      enabled: state.enabled,
      connected: mine !== null,
      status: mine?.status ?? null,
      login: mine?.login ?? null,
      connected_at: mine === null ? null : (mine.connected_at ?? "2026-11-20T09:30:00Z"),
      last_used_at: mine === null ? null : (mine.last_used_at ?? null),
      manage_url: state.enabled ? MANAGE_URL : null,
    };
  }

  const asEnabled = () => {
    if (!state.enabled) throw new StubError(503, "github_unavailable");
  };
  function asConnected() {
    if (state.connection === null) throw new StubError(409, "github_not_connected");
    if (state.connection.status !== "active") throw new StubError(409, "github_reconnect_required");
  }

  function signedUrl(base, body, purpose) {
    if (!only(body, ["return_to"]) || !sameSite(body.return_to)) throw validation(["body", "return_to"]);
    asEnabled();
    const payload = Buffer.from(
      JSON.stringify({ u: "u1", r: body.return_to, p: purpose, e: Math.floor(Date.now() / 1000) + 600 }),
    ).toString("base64url");
    const url = new URL(base);
    if (purpose === "authorize") url.searchParams.set("client_id", "stub-client");
    url.searchParams.set("state", `nonce.${payload}.${STATE_SIGNATURE}`);
    return { url: url.href };
  }

  function stateOf(raw, purpose) {
    const [, payload, signature] = String(raw).split(".");
    if (signature !== STATE_SIGNATURE) throw new StubError(400, "github_state_invalid");
    const parsed = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    if (parsed.p !== undefined && parsed.p !== purpose) throw new StubError(400, "github_state_invalid");
    return parsed.r;
  }

  function callback(body) {
    if (!only(body, ["code", "state"]) || typeof body.code !== "string" || typeof body.state !== "string" || body.code === "")
      throw validation(["body"]);
    asEnabled();
    if (state.callbackError !== null) throw new StubError(ROUTE_STATUS[state.callbackError] ?? 400, state.callbackError);
    const r = stateOf(body.state, "authorize");
    state.connection = { status: "active", login: "octo-dev", connected_at: new Date().toISOString() };
    return { connection: connection(), return_to: r };
  }

  function setup(body) {
    if (!only(body, ["state"]) || typeof body.state !== "string" || body.state === "") throw validation(["body", "state"]);
    asEnabled();
    if (state.setupError !== null) throw new StubError(ROUTE_STATUS[state.setupError] ?? 400, state.setupError);
    return { return_to: stateOf(body.state, "install") };
  }

  function disconnect() {
    asEnabled();
    state.connection = null;
  }

  function available() {
    asEnabled();
    asConnected();
    if (state.availableError !== null) throw new StubError(ROUTE_STATUS[state.availableError] ?? 409, state.availableError);
    return { repositories: state.available.map(repoOut), truncated: state.truncated };
  }

  const listOf = (pid) => {
    if (!projects.has(pid)) projects.set(pid, []);
    return projects.get(pid);
  };

  function list(pid) {
    const role = roleOf(pid); // throws for a non-member
    return { enabled: state.enabled, can_manage: role === "owner", repositories: state.enabled ? listOf(pid) : [] };
  }

  function asOwner(pid) {
    if (roleOf(pid) !== "owner") throw new StubError(403, "owner_required");
    asEnabled();
  }

  /** Runs the simulated clone check of `row`: pending, then cloning, then the outcome after `cloneMs`. */
  function runCheck(row) {
    Object.assign(row, { clone_state: "pending", failure_code: null });
    later(Math.min(state.cloneMs ?? Infinity, 300), () => {
      if (row.clone_state === "pending") row.clone_state = "cloning";
    });
    later(state.cloneMs, () => {
      const now = new Date().toISOString();
      if (state.cloneOutcome === "cloned") {
        Object.assign(row, {
          clone_state: "cloned",
          failure_code: null,
          head_sha: "3f9c2e1d4b5a69788766554433221100ffeeddcc",
          file_count: 1204,
          size_bytes: 9_437_184,
          snapshot_bytes: 3_355_443,
          checked_at: now,
          cloned_at: now,
        });
      } else Object.assign(row, { clone_state: "failed", failure_code: state.cloneOutcome, checked_at: now });
    });
  }

  function add(pid, body) {
    asOwner(pid);
    if (!only(body, ["github_repo_id"]) || !Number.isSafeInteger(body.github_repo_id)) throw validation(["body", "github_repo_id"]);
    asConnected();
    const repo = state.available.find((r) => r.github_repo_id === body.github_repo_id);
    if (repo === undefined) throw new StubError(404, "github_repo_not_accessible");
    if (repoOut(repo).too_large) throw new StubError(422, "github_repo_too_large");
    const rows = listOf(pid);
    if (rows.some((r) => r.github_repo_id === repo.github_repo_id)) throw new StubError(409, "repository_exists");
    const out = repoOut(repo);
    const row = {
      id: randomUUID(),
      github_repo_id: out.github_repo_id,
      full_name: out.full_name,
      html_url: out.html_url,
      private: out.private,
      added_by_name: displayName,
      added_by_me: true,
      clone_state: "pending",
      failure_code: null,
      default_branch: out.default_branch,
      head_sha: null,
      file_count: null,
      size_bytes: null,
      snapshot_bytes: null,
      checked_at: null,
      cloned_at: null,
      created_at: new Date().toISOString(),
    };
    rows.unshift(row);
    runCheck(row);
    return { ...row };
  }

  function rowOf(pid, rid) {
    const row = listOf(pid).find((r) => r.id === rid);
    if (row === undefined) throw new StubError(404, "repository_not_found");
    return row;
  }

  function remove(pid, rid) {
    asOwner(pid);
    rowOf(pid, rid);
    projects.set(
      pid,
      listOf(pid).filter((r) => r.id !== rid),
    );
  }

  function clone(pid, rid) {
    asOwner(pid);
    const row = rowOf(pid, rid);
    if (row.clone_state !== "pending" && row.clone_state !== "cloning") runCheck(row);
    return { ...row };
  }

  /**
   * Test seam (`POST /seed-github`): the global knobs (enabled, connection, callbackError, setupError, available, truncated,
   * availableError, cloneMs, cloneOutcome) and, with `pid`, the project's repositories (`repositories`: rows of ProjectRepositoryOut;
   * missing fields get defaults).
   */
  function seed({ pid, repositories, ...knobs }) {
    for (const [k, v] of Object.entries(knobs)) if (k in state && v !== undefined) state[k] = v;
    if (pid !== undefined && repositories !== undefined) {
      projects.set(
        pid,
        repositories.map((r, i) => ({
          id: randomUUID(),
          github_repo_id: 9000 + i,
          html_url: `https://github.com/${r.full_name}`,
          private: false,
          added_by_name: displayName,
          added_by_me: true,
          clone_state: "cloned",
          failure_code: null,
          default_branch: "main",
          head_sha: null,
          file_count: null,
          size_bytes: null,
          snapshot_bytes: null,
          checked_at: null,
          cloned_at: null,
          created_at: "2026-11-20T09:30:00Z",
          ...r,
        })),
      );
    }
  }

  /** Puts every global knob back (between cases); each project keeps its repositories (cases seed their own projects). */
  function reset() {
    Object.assign(state, {
      enabled: true,
      connection: null,
      callbackError: null,
      setupError: null,
      available: DEFAULT_AVAILABLE,
      truncated: false,
      availableError: null,
      cloneMs: 1500,
      cloneOutcome: "cloned",
    });
  }

  return {
    state,
    connection,
    authorize: (body) => signedUrl(AUTHORIZE_URL, body, "authorize"),
    install: (body) => signedUrl(INSTALL_URL, body, "install"),
    callback,
    setup,
    disconnect,
    available,
    list,
    add,
    remove,
    clone,
    seed,
    reset,
    close: () => {
      for (const t of timers) clearTimeout(t);
      timers.clear();
    },
  };
}
