// Stub of the GitHub Projects plan destination (docs/github/projects.md, docs/mission-control/integrations-api.md "GitHub Projects"), in
// memory: the destination of a project, any member may change it, a scripted access check and the sync status `seed` says (nothing is
// exported to GitHub). The acting member is the GitHub binding of the project: without one a PUT is 409 `github_not_connected` and the
// check says so as an answer. The repository must be one of the project's repositories (stub-github.mjs).
import { StubError } from "./stub-members.mjs";

export const PROJECTS = [
  { node_id: "PVT_stub1", number: 1, title: "Plan", url: "https://github.com/orgs/acme/projects/1" },
  { node_id: "PVT_stub2", number: 2, title: "Roadmap", url: "https://github.com/orgs/acme/projects/2" },
];
const ERRORS = {
  github_not_connected: [409, "github_not_connected"],
  github_reconnect_required: [409, "github_reconnect_required"],
  github_unreachable: [502, "github_unreachable"],
  github_auth_failed: [502, "github_auth_failed"],
  github_repo_not_accessible: [404, "github_repo_not_accessible"],
  github_not_an_organization: [422, "github_not_an_organization"],
  github_repo_read_only: [422, "github_repo_read_only"],
  github_permissions_missing: [409, "github_permissions_missing"],
  github_project_not_found: [404, "github_project_not_found"],
};
const FIELDS = {
  github_repo_not_accessible: "repository",
  github_not_an_organization: "repository",
  github_repo_read_only: "repository",
  github_project_not_found: "project",
};
const COUNTS = { created: 0, updated: 0, commented: 0, removed: 0, ranked: 0, unchanged: 0, failed: 0 };

const validation = (...errors) => new StubError(422, "validation_failed", { errors });

/** `bound(pid)` is whether the project has a GitHub binding that can act; `repositoryOf(pid, id)` is the project's repository row or undefined. */
export function createGitHubProjectsModel(bound, repositoryOf) {
  const state = { available: true, organization: "acme" };
  const targets = new Map(); // pid -> {repository_id, repository, project_node_id, project_title, project_url, enabled, sync}
  const access = new Map(); // pid -> "ok" | {reason, field?}
  let created = 0;

  const out = (pid) => {
    const t = targets.get(pid);
    return {
      available: state.available,
      repository_id: t?.repository_id ?? null,
      repository: t?.repository ?? null,
      project_node_id: t?.project_node_id ?? null,
      project_title: t?.project_title ?? null,
      project_url: t?.project_url ?? null,
      enabled: t?.enabled ?? false,
      sync: t?.sync ?? null,
      connected_by_name: t === undefined ? null : "dev",
      connected_by_me: t !== undefined,
    };
  };

  function parse(body, withEnabled) {
    const allowed = ["repository_id", "project_node_id", "new_project_title", ...(withEnabled ? ["enabled"] : [])];
    const keys = typeof body === "object" && body !== null && !Array.isArray(body) ? Object.keys(body) : [];
    if (keys.some((k) => !allowed.includes(k))) throw validation({ loc: ["body"], type: "extra_forbidden" });
    if (typeof body.repository_id !== "string" || body.repository_id === "")
      throw validation({ loc: ["body", "repository_id"], type: "uuid_type" });
    const node = body.project_node_id ?? null;
    const title = typeof body.new_project_title === "string" ? body.new_project_title.trim() || null : null;
    if ((node === null) === (title === null)) throw validation({ loc: ["body"], type: "value_error" });
    if (withEnabled && typeof body.enabled !== "boolean") throw validation({ loc: ["body", "enabled"], type: "bool_type" });
    return { repository_id: body.repository_id, node, title, enabled: body.enabled };
  }

  function refusal(pid) {
    const scripted = access.get(pid) ?? "ok";
    return scripted === "ok" ? null : scripted;
  }

  function put(pid, body) {
    if (!state.available) throw new StubError(503, "github_unavailable");
    const spec = parse(body, true);
    const repo = repositoryOf(pid, spec.repository_id);
    if (repo === undefined) throw new StubError(404, "repository_not_found");
    if (!bound(pid)) throw new StubError(409, "github_not_connected");
    const refused = refusal(pid);
    if (refused !== null) throw new StubError(...(ERRORS[refused.reason] ?? [502, "github_auth_failed"]));
    const picked = PROJECTS.find((p) => p.node_id === spec.node);
    created += spec.node === null ? 1 : 0;
    const project =
      spec.node === null
        ? { node_id: `PVT_new${created}`, title: spec.title, url: `https://github.com/orgs/acme/projects/${100 + created}` }
        : (picked ?? { node_id: spec.node, title: spec.node, url: "https://github.com/orgs/acme/projects/9" });
    const before = targets.get(pid);
    const same = before?.repository_id === spec.repository_id && before?.project_node_id === project.node_id;
    targets.set(pid, {
      repository_id: spec.repository_id,
      repository: repo.full_name,
      project_node_id: project.node_id,
      project_title: project.title,
      project_url: project.url,
      enabled: spec.enabled,
      // A new destination starts over; the same one keeps what its last pass said.
      sync: same ? before.sync : status(pid, repo.full_name, project.title, spec.enabled),
    });
    return out(pid);
  }

  const status = (pid, repository, project_title, enabled) => ({
    project_id: pid,
    enabled,
    status: enabled ? "succeeded" : "idle",
    queued: false,
    trigger: enabled ? "settings" : null,
    started_at: enabled ? "2026-11-25T09:00:00Z" : null,
    finished_at: enabled ? "2026-11-25T09:00:05Z" : null,
    error_code: null,
    counts: enabled ? { ...COUNTS } : null,
    repository,
    project_title,
  });

  function check(pid, body) {
    if (!state.available) throw new StubError(503, "github_unavailable");
    const spec = parse(body, false);
    if (repositoryOf(pid, spec.repository_id) === undefined) throw new StubError(404, "repository_not_found");
    if (!bound(pid)) throw new StubError(409, "github_not_connected");
    const refused = refusal(pid);
    if (refused !== null) return { result: "failed", reason: refused.reason, field: refused.field ?? FIELDS[refused.reason] ?? null };
    const title = spec.node === null ? null : (PROJECTS.find((p) => p.node_id === spec.node)?.title ?? spec.node);
    return { result: "ok", organization: state.organization, project_title: title };
  }

  function projects(pid, repositoryId) {
    if (!state.available) throw new StubError(503, "github_unavailable");
    if (repositoryOf(pid, repositoryId) === undefined) throw new StubError(404, "repository_not_found");
    if (!bound(pid)) throw new StubError(409, "github_not_connected");
    return { organization: state.organization, projects: state.organization === null ? [] : PROJECTS };
  }

  /**
   * Test seam (`POST /seed-github-projects`): `available`, `organization` (null = the repository belongs to a user), the destination of a
   * project (null removes it; `sync` as the API reports it, `enabled`, and the repository and project fields) and the next access answer.
   * @param {{pid?: string, available?: boolean, organization?: string | null, target?: object | null, access?: "ok" | {reason: string, field?: string | null}}} spec
   */
  function seed({ pid, available, organization, target, access: next }) {
    if (typeof available === "boolean") state.available = available;
    if (organization !== undefined) state.organization = organization;
    if (pid === undefined) return;
    if (target === null) targets.delete(pid);
    else if (target !== undefined) {
      const sync =
        target.sync === undefined
          ? status(pid, target.repository, target.project_title, target.enabled !== false)
          : { ...status(pid, target.repository, target.project_title, true), ...target.sync };
      targets.set(pid, { enabled: true, project_url: "https://github.com/orgs/acme/projects/1", ...target, sync });
    }
    if (next !== undefined) access.set(pid, next);
  }

  return { state, get: out, put, check, projects, seed };
}
