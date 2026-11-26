// The per-user OAuth routes both the Box and the Dropbox stub answer the same way (`/integrations/<app>/authorize` and `/callback`): the sign-in URL with a signed
// `state` that carries `return_to`, and the callback that accepts such a state unless `state.callbackError` names a code. The app supplies its model `state`
// (`enabled`, `callbackError`), the authorize URL and query parameters, and what a successful callback stores.
import { StubError } from "./stub-members.mjs";

const STATE_SIGNATURE = "stub-signature";
const ROUTE_STATUS = { state_invalid: 400, code_invalid: 400, unreachable: 502, unavailable: 503 };

export const validation = (loc) => new StubError(422, "validation_failed", { errors: [{ loc, type: "value_error" }] });
const only = (body, keys) =>
  typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).every((k) => keys.includes(k));
const sameSite = (r) => typeof r === "string" && r.startsWith("/") && !r.startsWith("//") && !/[\\\s]/.test(r) && r.length <= 2048;

/**
 * @param {{app: string, authorizeUrl: string, params: Record<string, string>, state: {enabled: boolean, callbackError: string | null}, connect: () => object, connection: () => object}} spec
 */
export function oauthRoutes({ app, authorizeUrl, params, state, connect, connection }) {
  const asEnabled = () => {
    if (!state.enabled) throw new StubError(503, `${app}_unavailable`);
  };

  function authorize(body) {
    if (!only(body, ["return_to"]) || !sameSite(body.return_to)) throw validation(["body", "return_to"]);
    asEnabled();
    const payload = Buffer.from(
      JSON.stringify({ u: "u1", r: body.return_to, p: "authorize", e: Math.floor(Date.now() / 1000) + 600 }),
    ).toString("base64url");
    const url = new URL(authorizeUrl);
    url.searchParams.set("response_type", "code");
    url.searchParams.set("client_id", "stub-client");
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
    url.searchParams.set("state", `nonce.${payload}.${STATE_SIGNATURE}`);
    return { url: url.href };
  }

  function stateOf(raw) {
    const [, payload, signature] = String(raw).split(".");
    if (signature !== STATE_SIGNATURE) throw new StubError(400, `${app}_state_invalid`);
    const parsed = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    if (parsed.p !== undefined && parsed.p !== "authorize") throw new StubError(400, `${app}_state_invalid`);
    return parsed.r;
  }

  function callback(body) {
    if (!only(body, ["code", "state"]) || typeof body.code !== "string" || typeof body.state !== "string" || body.code === "")
      throw validation(["body"]);
    asEnabled();
    if (state.callbackError !== null) {
      throw new StubError(ROUTE_STATUS[state.callbackError.slice(app.length + 1)] ?? 400, state.callbackError);
    }
    const r = stateOf(body.state);
    connect();
    return { connection: connection(), return_to: r };
  }

  return { asEnabled, authorize, callback };
}

/**
 * The caller's own connection of an app (`GET`, `DELETE /integrations/<app>`, authorize, callback) and its test seam (`POST /seed-<app>`): `reset`, `enabled`,
 * `connection` (null or {status, name?, <workspaceKey>?}), `callbackError`. `workspaceKey` is the app's workspace field (`enterprise_name`, `team_name`).
 * @param {{app: string, authorizeUrl: string, params: Record<string, string>, workspaceKey: string, workspaceName: string}} spec
 */
export function createConnectionModel({ app, authorizeUrl, params, workspaceKey, workspaceName }) {
  /** @type {{enabled: boolean, connection: Record<string, any> | null, callbackError: string | null}} */
  const state = { enabled: true, connection: null, callbackError: null };
  const own = { name: "Dana Example", [workspaceKey]: workspaceName };

  function connection() {
    const mine = state.enabled ? state.connection : null;
    return {
      enabled: state.enabled,
      connected: mine !== null,
      status: mine?.status ?? null,
      name: mine?.name ?? null,
      [workspaceKey]: mine?.[workspaceKey] ?? null,
      connected_at: mine === null ? null : (mine.connected_at ?? "2026-11-20T09:30:00Z"),
      last_used_at: mine === null ? null : (mine.last_used_at ?? null),
    };
  }

  const { asEnabled, authorize, callback } = oauthRoutes({
    app,
    authorizeUrl,
    params,
    state,
    connect: () => {
      state.connection = { status: "active", ...own, connected_at: new Date().toISOString() };
      return state.connection;
    },
    connection,
  });

  function disconnect() {
    asEnabled();
    state.connection = null;
  }

  /** @param {{reset?: boolean, enabled?: boolean, connection?: Record<string, any> | null, callbackError?: string | null}} spec */
  function seed({ reset, enabled, connection: next, callbackError }) {
    if (reset === true) Object.assign(state, { enabled: true, connection: null, callbackError: null });
    if (enabled !== undefined) state.enabled = enabled;
    if (callbackError !== undefined) state.callbackError = callbackError;
    if (next !== undefined) {
      state.connection = next === null ? null : { ...own, connected_at: "2026-11-20T09:30:00Z", ...next };
    }
  }

  return { state, connection, authorize, callback, disconnect, seed };
}
