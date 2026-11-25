// Stub of the Jira gap channel (docs/jira-sync/gaps.md, docs/mission-control/integrations-api.md "Jira gap channel"), in memory: the target of a
// project, any member may change it, a scripted access check and the counts `seed` says (nothing posts to Jira and nothing is polled). The
// acting member is the Atlassian binding of the project: without one a PUT is 409 `jira_not_connected` and the check says so as an answer.
import { StubError } from "./stub-members.mjs";

const PROJECT = /^[A-Z][A-Z0-9_]{1,9}$/;
export const POLL_SECONDS = 60;

const validation = (...errors) => new StubError(422, "validation_failed", { errors });

/** `bound(pid)` is whether the project has an Atlassian binding (a member who can act). */
export function createJiraGapsModel(bound) {
  const state = { available: true };
  const targets = new Map(); // pid -> {jira_project, grouping, enabled, story_key, posted, answered, error_code}
  const access = new Map(); // pid -> "ok" | {reason, field}

  const out = (pid) => {
    const t = targets.get(pid);
    return {
      available: state.available,
      jira_project: t?.jira_project ?? null,
      grouping: t?.grouping ?? null,
      enabled: t?.enabled ?? false,
      story_key: t?.story_key ?? null,
      story_url: t?.story_key == null ? null : `https://northwind.atlassian.test/browse/${t.story_key}`,
      posted: t?.posted ?? 0,
      answered: t?.answered ?? 0,
      last_polled_at: t?.last_polled_at ?? null,
      error_code: t?.error_code ?? null,
      poll_seconds: POLL_SECONDS,
    };
  };

  function keyOf(body) {
    const keys = typeof body === "object" && body !== null && !Array.isArray(body) ? Object.keys(body) : [];
    if (keys.some((k) => !["jira_project", "grouping", "enabled"].includes(k)))
      throw validation({ loc: ["body"], type: "extra_forbidden" });
    const project = typeof body.jira_project === "string" ? body.jira_project.trim().toUpperCase() : "";
    if (!PROJECT.test(project)) throw validation({ loc: ["body", "jira_project"], type: "string_pattern_mismatch" });
    return project;
  }

  function put(pid, body) {
    if (!state.available) throw new StubError(503, "gap_unavailable");
    const project = keyOf(body);
    if (body.grouping !== "one" && body.grouping !== "area") throw validation({ loc: ["body", "grouping"], type: "literal_error" });
    if (typeof body.enabled !== "boolean") throw validation({ loc: ["body", "enabled"], type: "bool_type" });
    if (!bound(pid)) throw new StubError(409, "jira_not_connected");
    const before = targets.get(pid);
    const same = before?.jira_project === project;
    // Another Jira project forgets what was posted; the same one keeps it.
    targets.set(pid, {
      ...(same ? before : { story_key: null, posted: 0, answered: 0, last_polled_at: null, error_code: null }),
      jira_project: project,
      grouping: body.grouping,
      enabled: body.enabled,
    });
    return out(pid);
  }

  function check(pid, body) {
    if (!state.available) throw new StubError(503, "gap_unavailable");
    keyOf({ jira_project: body?.jira_project });
    if (!bound(pid)) return { result: "failed", reason: "jira_not_connected", field: null };
    const scripted = access.get(pid) ?? "ok";
    if (scripted === "ok") return { result: "ok", project_name: "Northwind", epic_summary: null };
    return { result: "failed", reason: scripted.reason, field: scripted.field ?? null };
  }

  /**
   * Test seam (`POST /seed-jira-gaps`): `available`, the target of a project (null removes it; `story_key`, `posted`, `answered`,
   * `error_code`, `last_polled_at` as the API reports them) and the next access-check answer.
   * @param {{pid: string, available?: boolean, target?: object | null, access?: "ok" | {reason: string, field?: string | null}}} spec
   */
  function seed({ pid, available, target, access: next }) {
    if (typeof available === "boolean") state.available = available;
    if (target === null) targets.delete(pid);
    else if (target !== undefined) targets.set(pid, { story_key: null, posted: 0, answered: 0, error_code: null, ...target });
    if (next !== undefined) access.set(pid, next);
  }

  return { state, get: out, put, check, seed };
}
