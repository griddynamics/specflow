// Stub of the Notion integration (docs/notion/{oauth,source,plan}.md), in memory, next to stub-api.mjs: the caller's Notion connection
// (`/integrations/notion`), what a Notion link is worth to the source check and import, and the plan destination of a project
// (`/projects/{pid}/notion-plan`). Nothing calls Notion. Close enough to drive the http adapter, the BFF and the V2 pages; the real API lives in
// backend/src/api. The acting member is the Notion binding of the project (the integrations stub): without one a PUT is 409 `notion_not_connected`
// and the check says so as an answer. Reserved source links (host `www.notion.so`, id = the last 32 hex digits of the path):
//   ...0404 (the id ends in 0404) 409 notion_not_shared      ...0403 409 notion_not_shared (the member did not tick it)
//   ...00db (ends in 00db)        a database "Requirements", 42 rows      ...0b16 a database, 100 rows, capped
//   any other id                  a page "Engineering Wiki", 4 pages       another host or no id: 422 notion_link_invalid
import { StubError } from "./stub-members.mjs";

const MAX_PAGES = 100;
const ERRORS = {
  notion_not_connected: [409, "notion_not_connected"],
  notion_reconnect_required: [409, "notion_reconnect_required"],
  notion_unreachable: [502, "notion_unreachable"],
  notion_auth_failed: [502, "notion_auth_failed"],
  notion_link_invalid: [422, "notion_link_invalid"],
  notion_not_shared: [409, "notion_not_shared"],
  notion_parent_not_a_page: [422, "notion_parent_not_a_page"],
  notion_permissions_missing: [409, "notion_permissions_missing"],
};
const ROUTE_STATUS = { notion_state_invalid: 400, notion_code_invalid: 400, notion_unreachable: 502 };
const COUNTS = { created: 0, updated: 0, commented: 0, removed: 0, unchanged: 0, failed: 0, deferred: 0 };
const ID = /([0-9a-f]{32})\/?$/i;

const validation = (...errors) => new StubError(422, "validation_failed", { errors });
const only = (body, keys) =>
  typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).every((k) => keys.includes(k));

/** The check of a source link. @returns {{status: number, code: string} | {status: 200, body: object}} */
export function checkNotion(link) {
  let url;
  try {
    url = new URL(link);
  } catch {
    return { status: 422, code: "notion_link_invalid" };
  }
  if (url.protocol !== "https:" || url.username !== "" || url.hostname !== "www.notion.so")
    return { status: 422, code: "notion_link_invalid" };
  const id = ID.exec(url.pathname)?.[1];
  if (id === undefined) return { status: 422, code: "notion_link_invalid" };
  if (id.endsWith("0404") || id.endsWith("0403")) return { status: 409, code: "notion_not_shared" };
  if (id.endsWith("0b16")) return { status: 200, body: { kind: "database", title: "Big database", pages: MAX_PAGES, capped: true } };
  if (id.endsWith("00db")) return { status: 200, body: { kind: "database", title: "Requirements", pages: 42, capped: false } };
  return { status: 200, body: { kind: "page", title: "Engineering Wiki", pages: 4, capped: false } };
}

/**
 * The caller's connection: `state.connection` is null or {status: "active" | "expired", workspace_name, connected_at?}. `callbackError` is an
 * API code the callback answers with.
 */
export function createNotionModel() {
  /** @type {{enabled: boolean, connection: null | {status: string, workspace_name?: string, connected_at?: string, last_used_at?: string}, callbackError: string | null}} */
  const state = { enabled: true, connection: null, callbackError: null };
  const asEnabled = () => {
    if (!state.enabled) throw new StubError(503, "notion_unavailable");
  };

  function connection() {
    const mine = state.enabled ? state.connection : null;
    return {
      enabled: state.enabled,
      connected: mine !== null,
      status: mine?.status ?? null,
      workspace_name: mine?.workspace_name ?? null,
      connected_at: mine === null ? null : (mine.connected_at ?? "2026-11-20T09:30:00Z"),
      last_used_at: mine === null ? null : (mine.last_used_at ?? null),
    };
  }

  function authorize(body) {
    if (
      !only(body, ["return_to"]) ||
      typeof body.return_to !== "string" ||
      !body.return_to.startsWith("/") ||
      body.return_to.startsWith("//")
    )
      throw validation({ loc: ["body", "return_to"], type: "value_error" });
    asEnabled();
    return { url: `https://api.notion.com/v1/oauth/authorize?state=${encodeURIComponent(`stub:${body.return_to}`)}` };
  }

  function callback(body) {
    if (!only(body, ["code", "state"]) || typeof body.code !== "string" || typeof body.state !== "string" || body.code === "")
      throw validation({ loc: ["body"], type: "value_error" });
    asEnabled();
    if (state.callbackError !== null) throw new StubError(ROUTE_STATUS[state.callbackError] ?? 400, state.callbackError);
    if (!body.state.startsWith("stub:")) throw new StubError(400, "notion_state_invalid");
    state.connection = { status: "active", workspace_name: "Sample Workspace", connected_at: new Date().toISOString() };
    return { connection: connection(), return_to: body.state.slice("stub:".length) };
  }

  function disconnect() {
    asEnabled();
    state.connection = null;
  }

  return { state, connection, authorize, callback, disconnect };
}

/** `bound(pid)` is whether the project has a Notion binding that can act. */
export function createNotionPlanModel(bound) {
  const state = { available: true };
  const targets = new Map(); // pid -> {parent_page_id, parent_page_title, parent_page_url, database_url, enabled, sync}
  const access = new Map(); // pid -> "ok" | {reason}

  const out = (pid) => {
    const t = targets.get(pid);
    return {
      available: state.available,
      parent_page_id: t?.parent_page_id ?? null,
      parent_page_title: t?.parent_page_title ?? null,
      parent_page_url: t?.parent_page_url ?? null,
      database_url: t?.database_url ?? null,
      enabled: t?.enabled ?? false,
      sync: t?.sync ?? null,
      connected_by_name: t === undefined ? null : "Sample Workspace",
      connected_by_me: t !== undefined,
    };
  };

  function parse(body, withEnabled) {
    if (!only(body, withEnabled ? ["link", "enabled"] : ["link"]) || typeof body.link !== "string")
      throw validation({ loc: ["body"], type: "value_error" });
    if (withEnabled && typeof body.enabled !== "boolean") throw validation({ loc: ["body", "enabled"], type: "bool_type" });
    return { link: body.link.trim(), enabled: body.enabled };
  }

  const page = (link) => {
    const url = (() => {
      try {
        return new URL(link);
      } catch {
        return null;
      }
    })();
    const id = url !== null && url.protocol === "https:" && url.hostname === "www.notion.so" ? ID.exec(url.pathname)?.[1] : undefined;
    if (id === undefined) throw new StubError(...ERRORS.notion_link_invalid);
    if (id.endsWith("0404") || id.endsWith("0403")) throw new StubError(...ERRORS.notion_not_shared);
    return { page_id: id, title: "Plans", url: link };
  };

  const status = (pid, parent, enabled) => ({
    project_id: pid,
    enabled,
    status: enabled ? "succeeded" : "idle",
    queued: false,
    trigger: enabled ? "settings" : null,
    started_at: enabled ? "2026-11-25T09:00:00Z" : null,
    finished_at: enabled ? "2026-11-25T09:00:05Z" : null,
    error_code: null,
    counts: enabled ? { ...COUNTS } : null,
    parent_page_title: parent,
  });

  function put(pid, body) {
    if (!state.available) throw new StubError(503, "notion_unavailable");
    const spec = parse(body, true);
    if (!bound(pid)) throw new StubError(409, "notion_not_connected");
    const refused = access.get(pid) ?? "ok";
    if (refused !== "ok") throw new StubError(...(ERRORS[refused.reason] ?? [502, "notion_auth_failed"]));
    const parent = page(spec.link);
    const before = targets.get(pid);
    const same = before?.parent_page_id === parent.page_id;
    targets.set(pid, {
      parent_page_id: parent.page_id,
      parent_page_title: parent.title,
      parent_page_url: parent.url,
      database_url: same ? before.database_url : null, // the first pass makes the database
      enabled: spec.enabled,
      sync: same ? before.sync : status(pid, parent.title, spec.enabled),
    });
    return out(pid);
  }

  function check(pid, body) {
    if (!state.available) throw new StubError(503, "notion_unavailable");
    const spec = parse(body, false);
    if (!bound(pid)) throw new StubError(409, "notion_not_connected");
    const refused = access.get(pid) ?? "ok";
    if (refused !== "ok") return { result: "failed", reason: refused.reason };
    try {
      return { result: "ok", parent_page_title: page(spec.link).title };
    } catch (e) {
      if (e instanceof StubError) return { result: "failed", reason: e.code };
      throw e;
    }
  }

  /**
   * Test seam: `available`, the destination of a project (null removes it; `sync` as the API reports it, `enabled`, `database_url`, and the
   * parent page fields) and the next access answer.
   * @param {{pid?: string, available?: boolean, target?: object | null, access?: "ok" | {reason: string}}} spec
   */
  function seed({ pid, available, target, access: next }) {
    if (typeof available === "boolean") state.available = available;
    if (pid === undefined) return;
    if (target === null) targets.delete(pid);
    else if (target !== undefined) {
      const title = target.parent_page_title ?? "Plans";
      const sync =
        target.sync === undefined ? status(pid, title, target.enabled !== false) : { ...status(pid, title, true), ...target.sync };
      targets.set(pid, {
        enabled: true,
        parent_page_id: "423ed33606458163a8dde12275fc660d",
        parent_page_url: "https://www.notion.so/Plans-423ed33606458163a8dde12275fc660d",
        database_url: null,
        ...target,
        parent_page_title: title,
        sync,
      });
    }
    if (next !== undefined) access.set(pid, next);
  }

  return { state, get: out, put, check, seed };
}
