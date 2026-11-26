// Stub of the Linear plan destination (plans/LINEAR/LINEAR-PLAN.md, stream S3), in memory: the destination of a project (a team and a Linear
// project, or none to let Graphico create one), any member may change it, the teams and projects of the acting member, a scripted access check
// and the sync status `seed` says (nothing is exported to Linear). The acting member is the Linear binding of the project: without one every
// call is 409 `linear_not_connected`.
import { StubError } from "./stub-members.mjs";

export const TEAMS = [
  { id: "team-eng", key: "ENG", name: "Engineering" },
  { id: "team-ops", key: "OPS", name: "Operations" },
];
export const PROJECTS = [
  { id: "proj-roadmap", team_id: "team-eng", name: "Roadmap", url: "https://linear.app/acme/project/roadmap-1a2b3c" },
  { id: "proj-board", team_id: "team-ops", name: "Ops board", url: "https://linear.app/acme/project/ops-board-4d5e6f" },
];
const ERRORS = {
  linear_not_connected: [409, "linear_not_connected"],
  linear_reconnect_required: [409, "linear_reconnect_required"],
  linear_unreachable: [502, "linear_unreachable"],
  linear_forbidden: [409, "linear_forbidden"],
  linear_rate_limited: [429, "linear_rate_limited"],
  linear_team_not_found: [404, "linear_team_not_found"],
  linear_project_not_found: [404, "linear_project_not_found"],
};
const FIELDS = { linear_team_not_found: "team", linear_project_not_found: "project" };
const COUNTS = { created: 0, updated: 0, commented: 0, removed: 0, relations_added: 0, estimate_refused: 0, unchanged: 0, failed: 0 };

const validation = (...errors) => new StubError(422, "validation_failed", { errors });

/** `bound(pid)` is whether the project has a Linear binding that can act; `nameOf(pid)` is the name of the Graphico project. */
export function createLinearPlanModel(bound, nameOf = () => "Project") {
  const state = { available: true, workspace: "Graphico" };
  const targets = new Map(); // pid -> {team_id, team_key, project_id, project_name, project_url, created_by_graphico, enabled, sync}
  const access = new Map(); // pid -> "ok" | {reason, field?}
  const unbound = new Set(); // pids whose Linear binding a test took away

  const out = (pid) => {
    const t = targets.get(pid);
    return {
      available: state.available,
      team_id: t?.team_id ?? null,
      team_key: t?.team_key ?? null,
      project_id: t?.project_id ?? null,
      project_name: t?.project_name ?? null,
      project_url: t?.project_url ?? null,
      created_by_graphico: t?.created_by_graphico ?? false,
      enabled: t?.enabled ?? false,
      sync: t?.sync ?? null,
      workspace: t === undefined ? null : state.workspace,
      connected_by_me: t !== undefined,
    };
  };

  function parse(body, withEnabled) {
    const allowed = ["team_id", "project_id", ...(withEnabled ? ["enabled"] : [])];
    const keys = typeof body === "object" && body !== null && !Array.isArray(body) ? Object.keys(body) : [];
    if (keys.some((k) => !allowed.includes(k))) throw validation({ loc: ["body"], type: "extra_forbidden" });
    if (typeof body.team_id !== "string" || body.team_id === "") throw validation({ loc: ["body", "team_id"], type: "string_too_short" });
    const project = typeof body.project_id === "string" && body.project_id.trim() !== "" ? body.project_id : null;
    if (withEnabled && typeof body.enabled !== "boolean") throw validation({ loc: ["body", "enabled"], type: "bool_type" });
    return { team_id: body.team_id, project_id: project, enabled: body.enabled };
  }

  const gate = (pid) => {
    if (!state.available) throw new StubError(503, "linear_unavailable");
    if (unbound.has(pid) || !bound(pid)) throw new StubError(409, "linear_not_connected");
  };

  const refusal = (pid) => {
    const scripted = access.get(pid) ?? "ok";
    return scripted === "ok" ? null : scripted;
  };

  const status = (pid, team_key, linear_project, enabled, url) => ({
    project_id: pid,
    enabled,
    status: enabled ? "succeeded" : "idle",
    queued: false,
    trigger: enabled ? "settings" : null,
    started_at: enabled ? "2026-11-25T09:00:00Z" : null,
    finished_at: enabled ? "2026-11-25T09:00:05Z" : null,
    error_code: null,
    counts: enabled ? { ...COUNTS } : null,
    team_key,
    linear_project,
    linear_project_url: url ?? null,
    progress: null,
  });

  function resolve(pid, spec) {
    const team = TEAMS.find((t) => t.id === spec.team_id);
    if (team === undefined) throw new StubError(...ERRORS.linear_team_not_found);
    if (spec.project_id === null) return { team, project: null };
    const project = PROJECTS.find((p) => p.id === spec.project_id && p.team_id === team.id);
    if (project === undefined) throw new StubError(...ERRORS.linear_project_not_found);
    return { team, project };
  }

  function put(pid, body) {
    if (!state.available) throw new StubError(503, "linear_unavailable");
    const spec = parse(body, true);
    gate(pid);
    const refused = refusal(pid);
    if (refused !== null) throw new StubError(...(ERRORS[refused.reason] ?? [502, "linear_unreachable"]));
    const { team, project } = resolve(pid, spec);
    const made = project === null;
    const chosen = project ?? { id: `proj-new-${pid}`, name: nameOf(pid), url: "https://linear.app/acme/project/new-7a8b9c" };
    const before = targets.get(pid);
    const same = before?.team_id === team.id && before?.project_id === chosen.id;
    targets.set(pid, {
      team_id: team.id,
      team_key: team.key,
      project_id: chosen.id,
      project_name: chosen.name,
      project_url: chosen.url,
      created_by_graphico: made,
      enabled: spec.enabled,
      // A new destination starts over; the same one keeps what its last pass said.
      sync: same ? before.sync : status(pid, team.key, chosen.name, spec.enabled, chosen.url),
    });
    return out(pid);
  }

  function check(pid, body) {
    gate(pid);
    const spec = parse(body, false);
    const refused = refusal(pid);
    if (refused !== null) return { result: "failed", reason: refused.reason, field: refused.field ?? FIELDS[refused.reason] ?? null };
    const team = TEAMS.find((t) => t.id === spec.team_id);
    if (team === undefined) return { result: "failed", reason: "linear_team_not_found", field: "team" };
    const project = spec.project_id === null ? null : PROJECTS.find((p) => p.id === spec.project_id && p.team_id === team.id);
    if (project === undefined) return { result: "failed", reason: "linear_project_not_found", field: "project" };
    return { result: "ok", team_key: team.key, project_name: project?.name ?? null };
  }

  function teams(pid) {
    gate(pid);
    return { workspace: state.workspace, teams: TEAMS };
  }

  function projects(pid, teamId) {
    gate(pid);
    return { projects: PROJECTS.filter((p) => p.team_id === teamId).map(({ id, name, url }) => ({ id, name, url })) };
  }

  /**
   * Test seam (`POST /seed-linear-plan`): `available`, `workspace`, the destination of a project (null removes it; `sync` as the API reports
   * it, `enabled`, and the team and project fields) the next access answer and `bound` false (the project has no Linear binding that can act).
   * @param {{pid?: string, available?: boolean, workspace?: string, target?: object | null, access?: "ok" | {reason: string, field?: string | null}, bound?: boolean}} spec
   */
  function seed({ pid, available, workspace, target, access: next, bound: binding }) {
    if (typeof available === "boolean") state.available = available;
    if (typeof workspace === "string") state.workspace = workspace;
    if (pid === undefined) return;
    if (target === null) targets.delete(pid);
    else if (target !== undefined) {
      const enabled = target.enabled !== false;
      const base = status(pid, target.team_key, target.project_name, enabled, target.project_url);
      const sync =
        target.sync === undefined
          ? base
          : { ...status(pid, target.team_key, target.project_name, true, target.project_url), ...target.sync };
      targets.set(pid, { enabled, created_by_graphico: false, team_id: "team-eng", project_id: "proj-roadmap", ...target, sync });
    }
    if (next !== undefined) access.set(pid, next);
    if (binding === false) unbound.add(pid);
    if (binding === true) unbound.delete(pid);
  }

  return { state, get: out, put, check, teams, projects, seed };
}
