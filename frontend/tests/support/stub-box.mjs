// Stub of the Box API of docs/box/oauth.md and docs/box/source.md, in memory: the caller's Box connection (`createBoxModel`) and what a Box link is worth to
// the check and to the import (`checkBox`). A test double next to stub-api.mjs, close enough to drive the http adapter, the BFF and the V2 Integrations page and
// Box form; the real API lives in backend/src/api. It never calls Box. `state.enabled` false = Box is not configured (503 `box_unavailable`). The authorize
// URL carries a `state` with `return_to` like the API's (`nonce.payload.signature`); `callback` accepts such a state unless `state.callbackError` names a code.
// Reserved links (app.box.com):
//   /folder/0                    All Files: 422 box_link_invalid
//   /folder/403, /folder/404     409 box_no_access
//   /folder/9999                 a folder "Big archive", 1000 files, 20 skipped, capped
//   /folder/<other>              a folder "Project files", 12 files, 3 skipped, 2 folders
//   /file/<id>                   a file "Spec.pdf", 1 file (/file/404 is 409 box_no_access)
//   /s/password                  422 box_link_password
//   /s/<token>, /v/<name>        a shared folder "Shared folder", 5 files
//   another host, http, a path   422 box_link_invalid
import { StubError } from "./stub-members.mjs";

export const AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize";
const STATE_SIGNATURE = "stub-signature";
const ROUTE_STATUS = { box_state_invalid: 400, box_code_invalid: 400, box_unreachable: 502, box_unavailable: 503 };
const MAX_FILES = 1000;

const validation = (loc) => new StubError(422, "validation_failed", { errors: [{ loc, type: "value_error" }] });
const only = (body, keys) =>
  typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).every((k) => keys.includes(k));
const sameSite = (r) => typeof r === "string" && r.startsWith("/") && !r.startsWith("//") && !/[\\\s]/.test(r) && r.length <= 2048;

/** @returns {{status: number, code: string} | {status: 200, body: object}} */
export function checkBox(link) {
  let url;
  try {
    url = new URL(link);
  } catch {
    return { status: 422, code: "box_link_invalid" };
  }
  const host = url.hostname;
  const boxHost = host === "app.box.com" || host.endsWith(".app.box.com");
  if (url.protocol !== "https:" || url.username !== "" || url.password !== "" || !boxHost) return { status: 422, code: "box_link_invalid" };
  const folder = /^\/folder\/(\d+)\/?$/.exec(url.pathname)?.[1];
  if (folder !== undefined) {
    if (folder === "0") return { status: 422, code: "box_link_invalid" };
    if (folder === "403" || folder === "404") return { status: 409, code: "box_no_access" };
    const big = folder === "9999";
    return {
      status: 200,
      body: {
        kind: "folder",
        name: big ? "Big archive" : "Project files",
        files: big ? MAX_FILES : 12,
        skipped: big ? 20 : 3,
        folders: big ? 300 : 2,
        capped: big,
      },
    };
  }
  const file = /^\/file\/(\d+)\/?$/.exec(url.pathname)?.[1];
  if (file !== undefined) {
    if (file === "403" || file === "404") return { status: 409, code: "box_no_access" };
    return { status: 200, body: { kind: "file", name: "Spec.pdf", files: 1, skipped: 0, folders: 0, capped: false } };
  }
  const shared = /^\/(?:s|v)\/([^/]+)\/?$/.exec(url.pathname)?.[1];
  if (shared === "password") return { status: 422, code: "box_link_password" };
  if (shared !== undefined) {
    return { status: 200, body: { kind: "folder", name: "Shared folder", files: 5, skipped: 0, folders: 1, capped: false } };
  }
  return { status: 422, code: "box_link_invalid" };
}

/** The caller's Box connection. `roleOf` is accepted for symmetry with the other models; no route here needs a role. */
export function createBoxModel() {
  /** @type {{enabled: boolean, connection: {status: string, name: string, enterprise_name: string | null, connected_at?: string, last_used_at?: string} | null,
   *   callbackError: string | null}} */
  const state = {
    enabled: true,
    connection: null, // null or {status: "active" | "expired", name, enterprise_name, connected_at?}
    callbackError: null,
  };

  function connection() {
    const mine = state.enabled ? state.connection : null;
    return {
      enabled: state.enabled,
      connected: mine !== null,
      status: mine?.status ?? null,
      name: mine?.name ?? null,
      enterprise_name: mine?.enterprise_name ?? null,
      connected_at: mine === null ? null : (mine.connected_at ?? "2026-11-20T09:30:00Z"),
      last_used_at: mine === null ? null : (mine.last_used_at ?? null),
    };
  }

  const asEnabled = () => {
    if (!state.enabled) throw new StubError(503, "box_unavailable");
  };

  function authorize(body) {
    if (!only(body, ["return_to"]) || !sameSite(body.return_to)) throw validation(["body", "return_to"]);
    asEnabled();
    const payload = Buffer.from(
      JSON.stringify({ u: "u1", r: body.return_to, p: "authorize", e: Math.floor(Date.now() / 1000) + 600 }),
    ).toString("base64url");
    const url = new URL(AUTHORIZE_URL);
    url.searchParams.set("response_type", "code");
    url.searchParams.set("client_id", "stub-client");
    url.searchParams.set("scope", "root_readwrite");
    url.searchParams.set("state", `nonce.${payload}.${STATE_SIGNATURE}`);
    return { url: url.href };
  }

  function stateOf(raw) {
    const [, payload, signature] = String(raw).split(".");
    if (signature !== STATE_SIGNATURE) throw new StubError(400, "box_state_invalid");
    const parsed = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    if (parsed.p !== undefined && parsed.p !== "authorize") throw new StubError(400, "box_state_invalid");
    return parsed.r;
  }

  function callback(body) {
    if (!only(body, ["code", "state"]) || typeof body.code !== "string" || typeof body.state !== "string" || body.code === "")
      throw validation(["body"]);
    asEnabled();
    if (state.callbackError !== null) throw new StubError(ROUTE_STATUS[state.callbackError] ?? 400, state.callbackError);
    const r = stateOf(body.state);
    state.connection = { status: "active", name: "Dana Example", enterprise_name: "Acme Corp", connected_at: new Date().toISOString() };
    return { connection: connection(), return_to: r };
  }

  function disconnect() {
    asEnabled();
    state.connection = null;
  }

  /** Test seam (`POST /seed-box`): `reset`, `enabled`, `connection` (null or {status, name?, enterprise_name?}), `callbackError`. */
  /** @param {{reset?: boolean, enabled?: boolean, connection?: {status: string, name?: string, enterprise_name?: string | null} | null, callbackError?: string | null}} spec */
  function seed({ reset, enabled, connection: next, callbackError }) {
    if (reset === true) Object.assign(state, { enabled: true, connection: null, callbackError: null });
    if (enabled !== undefined) state.enabled = enabled;
    if (callbackError !== undefined) state.callbackError = callbackError;
    if (next !== undefined) {
      state.connection =
        next === null ? null : { name: "Dana Example", enterprise_name: "Acme Corp", connected_at: "2026-11-20T09:30:00Z", ...next };
    }
  }

  return { state, connection, authorize, callback, disconnect, seed };
}
