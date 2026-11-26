// Stub of the caller's Linear connection of plans/LINEAR (stream S1: GET/DELETE /integrations/linear, POST .../authorize and .../callback), in
// memory. A test double next to stub-api.mjs, close enough to drive the http adapter, the BFF and the Integrations page; the real API lives in
// backend/src/api. It never calls Linear. `state.available` false = Linear is not configured (503 `linear_unavailable`). The authorize URL
// carries a `state` with `return_to` like the API's (`nonce.payload.signature`); `callback` accepts such a state unless `state.callbackError`
// names a route code, and then connects the caller to the stub workspace.
import { StubError } from "./stub-members.mjs";

export const AUTHORIZE_URL = "https://linear.app/oauth/authorize";
const STATE_SIGNATURE = "stub-signature";
const ROUTE_STATUS = { linear_state_invalid: 400, linear_code_invalid: 400, linear_unreachable: 502, linear_auth_failed: 502 };
const STUB_WORKSPACE = { id: "org-stub", name: "Acme", url_key: "acme" };

const validation = (loc) => new StubError(422, "validation_failed", { errors: [{ loc, type: "value_error" }] });
const only = (body, keys) =>
  typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).every((k) => keys.includes(k));
const sameSite = (r) => typeof r === "string" && r.startsWith("/") && !r.startsWith("//") && !/[\\\s]/.test(r) && r.length <= 2048;
const text = (v) => typeof v === "string" && v !== "";

/** `displayName` is the caller's Specflow name, the member name of a fresh connection. */
export function createLinearModel(displayName = "dev") {
  /** @type {{available: boolean, connection: {status: string, user_name: string, workspace: object} | null, callbackError: string | null}} */
  const state = { available: true, connection: null, callbackError: null };

  const asAvailable = () => {
    if (!state.available) throw new StubError(503, "linear_unavailable");
  };

  function connection() {
    const mine = state.available ? state.connection : null;
    return {
      available: state.available,
      status: mine?.status ?? "none",
      user_name: mine?.user_name ?? null,
      workspace: mine?.workspace ?? null,
    };
  }

  function authorize(body) {
    if (!only(body, ["return_to"]) || !sameSite(body.return_to)) throw validation(["body", "return_to"]);
    asAvailable();
    const payload = Buffer.from(JSON.stringify({ u: "u1", r: body.return_to, e: Math.floor(Date.now() / 1000) + 600 })).toString(
      "base64url",
    );
    const url = new URL(AUTHORIZE_URL);
    url.searchParams.set("state", `nonce.${payload}.${STATE_SIGNATURE}`);
    return { url: url.href };
  }

  function returnToOf(value) {
    const parts = value.split(".");
    if (parts.length === 3 && parts[2] === STATE_SIGNATURE) {
      try {
        const r = JSON.parse(Buffer.from(parts[1], "base64url").toString()).r;
        if (sameSite(r)) return r;
      } catch {
        // not a stub state: refused below
      }
    }
    throw new StubError(400, "linear_state_invalid");
  }

  function callback(body) {
    if (!only(body, ["code", "state"]) || !text(body.code) || !text(body.state)) throw validation(["body"]);
    asAvailable();
    if (state.callbackError !== null) throw new StubError(ROUTE_STATUS[state.callbackError] ?? 400, state.callbackError);
    const returnTo = returnToOf(body.state);
    state.connection = { status: "active", user_name: displayName, workspace: STUB_WORKSPACE };
    return { connection: connection(), return_to: returnTo };
  }

  function disconnect() {
    asAvailable();
    state.connection = null;
  }

  /**
   * Test seam (`POST /seed-linear`): the global knobs (available, connection, callbackError). `connection` is null (none) or
   * {status?: "active" | "expired" | "revoked", user_name?, workspace?: {id, name, url_key}}.
   */
  function seed(knobs) {
    for (const [k, v] of Object.entries(knobs)) {
      if (!(k in state) || v === undefined) continue;
      state[k] = k === "connection" && v !== null ? { status: "active", user_name: displayName, workspace: STUB_WORKSPACE, ...v } : v;
    }
  }

  /** Puts every global knob back (between cases). */
  function reset() {
    Object.assign(state, { available: true, connection: null, callbackError: null });
  }

  return { state, connection, authorize, callback, disconnect, seed, reset };
}
