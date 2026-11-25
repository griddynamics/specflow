// Stub of the Linear gap channel (docs/linear/gaps.md), in memory: the target of a project, any member may change it, a scripted access check and
// the counts `seed` says (nothing posts to Linear and no webhook arrives). The acting member is the Linear binding of the project: without one a PUT
// is 409 `linear_not_connected` and the check says so as an answer.
import { StubError } from "./stub-members.mjs";

const TEAM = /^[A-Z][A-Z0-9]{0,14}$/;
const FIELDS = ["team_key", "linear_project", "grouping", "enabled"];

const validation = (...errors) => new StubError(422, "validation_failed", { errors });

/** `bound(pid)` is whether the project has a Linear binding (a member who can act). */
export function createLinearGapsModel(bound) {
  const state = { available: true };
  const targets = new Map(); // pid -> {team_key, linear_project_id, grouping, enabled, parent_identifier, posted, answered, error_code}
  const access = new Map(); // pid -> "ok" | {reason, field}

  const out = (pid) => {
    const t = targets.get(pid);
    return {
      available: state.available,
      enabled: t?.enabled ?? false,
      team_id: t == null ? null : `team-${t.team_key}`,
      team_key: t?.team_key ?? null,
      linear_project_id: t?.linear_project_id ?? null,
      grouping: t?.grouping ?? null,
      parent_identifier: t?.parent_identifier ?? null,
      parent_url: t?.parent_identifier == null ? null : `https://linear.app/northwind/issue/${t.parent_identifier}`,
      posted: t?.posted ?? 0,
      answered: t?.answered ?? 0,
      error_code: t?.error_code ?? null,
    };
  };

  function keyOf(body) {
    const isObject = typeof body === "object" && body !== null && !Array.isArray(body);
    if (isObject && Object.keys(body).some((k) => !FIELDS.includes(k))) throw validation({ loc: ["body"], type: "extra_forbidden" });
    const team = isObject && typeof body.team_key === "string" ? body.team_key.trim().toUpperCase() : "";
    if (!TEAM.test(team)) throw validation({ loc: ["body", "team_key"], type: "string_pattern_mismatch" });
    return team;
  }

  function put(pid, body) {
    if (!state.available) throw new StubError(503, "gap_unavailable");
    const team = keyOf(body);
    if (body.grouping !== "one" && body.grouping !== "area") throw validation({ loc: ["body", "grouping"], type: "literal_error" });
    if (typeof body.enabled !== "boolean") throw validation({ loc: ["body", "enabled"], type: "bool_type" });
    if (!bound(pid)) throw new StubError(409, "linear_not_connected");
    const before = targets.get(pid);
    const project = typeof body.linear_project === "string" && body.linear_project.trim() !== "" ? body.linear_project.trim() : null;
    const same = before?.team_key === team && before?.linear_project_id === project;
    // Another team or project forgets what was posted; the same one keeps it.
    targets.set(pid, {
      ...(same ? before : { parent_identifier: null, posted: 0, answered: 0, error_code: null }),
      team_key: team,
      linear_project_id: project,
      grouping: body.grouping,
      enabled: body.enabled,
    });
    return out(pid);
  }

  function check(pid, body) {
    if (!state.available) throw new StubError(503, "gap_unavailable");
    const team = keyOf({ team_key: body?.team_key });
    if (!bound(pid)) return { result: "failed", reason: "linear_not_connected", field: null };
    const scripted = access.get(pid) ?? "ok";
    if (scripted === "ok") return { result: "ok", team_name: `Team ${team}`, linear_project_name: body?.linear_project ?? null };
    return { result: "failed", reason: scripted.reason, field: scripted.field ?? null };
  }

  /**
   * Test seam (`POST /seed-linear-gaps`): `available`, the target of a project (null removes it; `parent_identifier`, `posted`, `answered`,
   * `error_code` as the API reports them) and the next access-check answer.
   * @param {{pid: string, available?: boolean, target?: object | null, access?: "ok" | {reason: string, field?: string | null}}} spec
   */
  function seed({ pid, available, target, access: next }) {
    if (typeof available === "boolean") state.available = available;
    if (target === null) targets.delete(pid);
    else if (target !== undefined) {
      targets.set(pid, { linear_project_id: null, parent_identifier: null, posted: 0, answered: 0, error_code: null, ...target });
    }
    if (next !== undefined) access.set(pid, next);
  }

  return { state, get: out, put, check, seed };
}
