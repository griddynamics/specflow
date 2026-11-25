// Stub of the project integrations API (docs/mission-control/integrations-api.md), in memory: who each app is connected by for a project,
// connect (`PUT`), remove (`DELETE`) and the Slack routes (`GET`/`DELETE /projects/{pid}/slack`, `POST /projects/{pid}/slack/test`). A test
// double next to stub-api.mjs, close enough to drive the http adapter, the BFF and the V2 Integrations page; the real API lives in backend/src/api.
// The caller is `u1` (the stub's `/me`). A binding the caller made takes its health from the caller's own Atlassian or GitHub connection (the
// Jira and GitHub stubs); a seeded binding of another member carries its own `health`. Slack is bound by `seed` (the command in the channel).
import { StubError } from "./stub-members.mjs";

const ME_ID = "u1";
const APPS = ["atlassian", "github", "slack"];
const BINDABLE = ["atlassian", "github"];
const NOT_CONNECTED = { atlassian: "atlassian_not_connected", github: "github_not_connected" };
const UNAVAILABLE = { atlassian: "atlassian_oauth_unavailable", github: "github_unavailable" };
const TEST_STATUS = { slack_not_connected: 409, slack_not_in_channel: 409, slack_unreachable: 502, slack_unavailable: 503 };
const validation = (...errors) => new StubError(422, "validation_failed", { errors });

/**
 * `roleOf(pid)` is the caller's role ("owner" or "collaborator"; throws 404 for a non-member). `jira` and `github` are the stub models whose
 * `state.connection` is the caller's own connection. `displayName` is the caller's name.
 */
export function createIntegrationsModel({ roleOf, jira, github, displayName }) {
  const state = { slackAvailable: true, slackTest: "ok" };
  const projects = new Map(); // pid -> {atlassian, github, slack: row | null, channel: {id, name, private} | null}
  const mine = (pid) => {
    if (!projects.has(pid)) projects.set(pid, { atlassian: null, github: null, slack: null, channel: null });
    return projects.get(pid);
  };

  const availableOf = (app) =>
    app === "atlassian" ? jira.state.mode === "oauth" : app === "github" ? github.state.enabled : state.slackAvailable;
  const callerConnection = (app) => (app === "atlassian" ? jira.state.connection : github.state.connection);

  function healthOf(app, row) {
    if (row === null) return "none";
    if (row.health !== undefined) return row.health;
    if (app === "slack") return mine(row.pid).channel === null ? "none" : "active";
    if (row.user_id === null) return "none";
    const own = callerConnection(app);
    if (own === null) return "none";
    return own.status === "active" ? "active" : "reconnect_required";
  }

  function out(app, row) {
    return {
      app,
      available: availableOf(app),
      connected_by: row === null || row.user_id === null ? null : { user_id: row.user_id, name: row.name },
      workspace: row === null ? null : row.workspace,
      connected_at: row === null ? null : row.connected_at,
      health: healthOf(app, row),
    };
  }

  function list(pid) {
    roleOf(pid);
    const p = mine(pid);
    return { integrations: APPS.map((app) => out(app, p[app])) };
  }

  function connect(pid, app) {
    roleOf(pid);
    if (!BINDABLE.includes(app)) throw validation({ loc: ["path", "app"], type: "enum" });
    if (!availableOf(app)) throw new StubError(503, UNAVAILABLE[app]);
    const own = callerConnection(app);
    if (own === null || own.status !== "active") throw new StubError(409, NOT_CONNECTED[app]);
    const workspace = app === "atlassian" ? { id: "cloud-stub", name: "example.atlassian.net" } : { id: own.login, name: own.login };
    const p = mine(pid);
    const current = p[app];
    if (current !== null && current.user_id !== null && current.workspace.id !== workspace.id)
      throw new StubError(409, "integration_workspace_mismatch");
    p[app] = { user_id: ME_ID, name: displayName, workspace, connected_at: new Date().toISOString() };
    return out(app, p[app]);
  }

  function disconnect(pid, app) {
    const role = roleOf(pid);
    if (!APPS.includes(app)) throw validation({ loc: ["path", "app"], type: "enum" });
    const p = mine(pid);
    const row = p[app];
    if (row === null) throw new StubError(404, "integration_not_found");
    if (row.user_id !== ME_ID && role !== "owner") throw new StubError(403, "integration_forbidden");
    p[app] = null;
    if (app === "slack") p.channel = null;
  }

  const slackGet = (pid) => {
    const p = mine(pid);
    roleOf(pid);
    return { available: state.slackAvailable, channel: p.channel, editable: true };
  };

  function slackUnbind(pid) {
    roleOf(pid);
    const p = mine(pid);
    p.channel = null;
    p.slack = null;
  }

  function slackTest(pid) {
    roleOf(pid);
    if (!state.slackAvailable) throw new StubError(503, "slack_unavailable");
    const p = mine(pid);
    if (p.channel === null) throw new StubError(409, "slack_not_connected");
    if (state.slackTest !== "ok") throw new StubError(TEST_STATUS[state.slackTest] ?? 502, state.slackTest);
    return { posted: true, channel: p.channel };
  }

  /**
   * Test seam (`POST /seed-integrations`): `slackAvailable`, `slackTest` ("ok" or an API code the next test messages answer with) and, with
   * `pid`, the bindings of one project: `atlassian` and `github` are null (none) or {by?: {user_id, name} | null, workspace?: {id, name},
   * health?}; `slack` is null or {channel: {id, name, private}, by?}. `by: null` is a binding whose member left.
   */
  /** @param {{pid?: string, reset?: boolean, slackAvailable?: boolean, slackTest?: string, [app: string]: any}} spec */
  function seed({ pid, reset, slackAvailable, slackTest: outcome, ...bindings }) {
    if (reset === true) Object.assign(state, { slackAvailable: true, slackTest: "ok" });
    if (slackAvailable !== undefined) state.slackAvailable = slackAvailable;
    if (outcome !== undefined) state.slackTest = outcome;
    if (pid === undefined) return;
    const p = mine(pid);
    for (const app of APPS) {
      if (!(app in bindings)) continue;
      const spec = bindings[app];
      if (spec === null) {
        p[app] = null;
        if (app === "slack") p.channel = null;
        continue;
      }
      const by = spec.by === undefined ? { user_id: ME_ID, name: displayName } : spec.by;
      const workspace = spec.workspace ?? { id: `${app}-stub`, name: `${app}-workspace` };
      p[app] = {
        pid,
        user_id: by === null ? null : by.user_id,
        name: by === null ? null : by.name,
        workspace,
        connected_at: "2026-11-20T09:30:00Z",
        ...(spec.health === undefined ? {} : { health: spec.health }),
      };
      if (app === "slack") p.channel = spec.channel ?? { id: "C0STUB", name: "stub-gaps", private: false };
    }
  }

  return { state, list, connect, disconnect, slackGet, slackUnbind, slackTest, seed };
}
