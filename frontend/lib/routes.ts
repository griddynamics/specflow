// FE-BP-01, FE-BP-03 (TECH-SPECS §1.1): the ONE place that knows a path. Screens get a RouteBuilder from
// useRoutes() and never build a path string by hand. Pure: no React, no fetch.
export type AppMode = "production";
export type MissionControlView = "readme" | "business" | "architecture" | "files" | "sources"; // "sources": Phase 1 sources screen only
export type PlanView = "plan" | "develop"; // P20 (Plan page, AUTO-17)
export type RouteKey = "home" | "missionControl" | "decisions" | "plan" | "specbot" | "connections";

/** Path templates WITHOUT the tree prefix; `{name}` marks a param. `trees` = where a page file must exist. */
export const ROUTES: Record<RouteKey, { template: string; trees: readonly AppMode[] }> = {
  home: { template: "", trees: ["production"] },
  missionControl: { template: "/projects/{projectId}/mission-control", trees: ["production"] },
  decisions: { template: "/projects/{projectId}/decisions", trees: ["production"] },
  plan: { template: "/projects/{projectId}/plan", trees: ["production"] }, // P20; page files in P21
  specbot: { template: "/projects/{projectId}/specbot", trees: ["production"] },
  connections: { template: "/connections", trees: ["production"] },
};

export const TREE_PREFIX: Record<AppMode, ""> = { production: "" };

export interface RouteBuilder {
  home(): string;
  missionControl(projectId: string, opts?: { view?: MissionControlView }): string;
  file(projectId: string, ref: { path: string; anchor?: string }): string; // view=files&file=...&section=...
  decisions(projectId: string, opts?: { decisionId?: string }): string;
  plan(projectId: string, opts?: { view?: PlanView }): string; // no `view` → clean path (Stage 2 opens on Plan)
  specbot(projectId: string): string;
  connections(): string;
  login(): string; // "/login"
  asset(path: string): string; // "/ds/" + path
}

const VIEWS: readonly MissionControlView[] = ["readme", "business", "architecture", "files", "sources"];
const PLAN_VIEWS: readonly PlanView[] = ["plan", "develop"];

function withQuery(path: string, params: Record<string, string | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined) q.set(k, v);
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}

function fill(template: string, params: Record<string, string>): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => encodeURIComponent(params[name] ?? ""));
}

export function routesFor(mode: AppMode): RouteBuilder {
  const prefix = TREE_PREFIX[mode];
  const at = (key: RouteKey, params: Record<string, string> = {}) => prefix + fill(ROUTES[key].template, params);
  return {
    home: () => "/",
    missionControl: (projectId, opts) => withQuery(at("missionControl", { projectId }), { view: opts?.view }),
    file: (projectId, ref) => withQuery(at("missionControl", { projectId }), { view: "files", file: ref.path, section: ref.anchor }),
    decisions: (projectId, opts) => withQuery(at("decisions", { projectId }), { decision: opts?.decisionId }),
    plan: (projectId, opts) => withQuery(at("plan", { projectId }), { view: opts?.view }),
    specbot: (projectId) => at("specbot", { projectId }),
    connections: () => at("connections"),
    login: () => "/login",
    asset: (path) => `/ds/${path.replace(/^\/+/, "")}`,
  };
}

export function parseMissionControlQuery(q: URLSearchParams): {
  view: MissionControlView | null;
  file: string | null;
  section: string | null;
} {
  const v = q.get("view");
  return {
    view: VIEWS.includes(v as MissionControlView) ? (v as MissionControlView) : null,
    file: q.get("file"),
    section: q.get("section"),
  };
}

export function parseDecisionsQuery(q: URLSearchParams): { decisionId: string | null } {
  return { decisionId: q.get("decision") };
}

/** FE-PLAN-05 (TECH-SPECS §1.1): `view` of the Plan route; missing or unknown -> null, never an error. */
export function parsePlanQuery(q: URLSearchParams): { view: PlanView | null } {
  const v = q.get("view");
  return { view: PLAN_VIEWS.includes(v as PlanView) ? (v as PlanView) : null };
}
