// FE-BP-01, FE-BP-03 (TECH-SPECS §1.1): the ONE place that knows a path. Screens get a RouteBuilder from
// useRoutes() and never build a path string by hand. Pure: no React, no fetch.
import { INTEGRATION_APPS, type IntegrationCardId } from "@/lib/domain/integrations";
import { isSameSitePath } from "@/lib/same-site-path";

export type AppMode = "production";
export type MissionControlView = "readme" | "business" | "architecture" | "files" | "sources"; // "sources": Phase 1 sources screen only
export type PlanView = "plan" | "work" | "develop"; // P20 (Plan page, AUTO-17)
export type SettingsTab = "general" | "jira" | "slack" | "github"; // the Settings tabs (docs/projects/ui.md#route-and-sidebar)
export type RouteKey =
  "home" | "canvas" | "integrations" | "missionControl" | "gaps" | "plan" | "specbot" | "settings" | "connections" | "guides";
/** The Guides topics (MCP-M10): `/guides/{topic}`. Any other topic answers 404. */
export const GUIDE_TOPICS = ["specflow-101", "best-practices", "mcp-server"] as const;
export type GuideTopic = (typeof GUIDE_TOPICS)[number];

/** Path templates WITHOUT the tree prefix; `{name}` marks a param. `trees` = where a page file must exist. */
export const ROUTES: Record<RouteKey, { template: string; trees: readonly AppMode[] }> = {
  home: { template: "", trees: ["production"] },
  canvas: { template: "/projects/{projectId}", trees: ["production"] }, // Mission Control V2: the canvas of a project (app/(v2))
  integrations: { template: "/projects/{projectId}/integrations", trees: ["production"] }, // V2: ?github= | ?atlassian= | ?next=
  missionControl: { template: "/projects/{projectId}/mission-control", trees: ["production"] },
  gaps: { template: "/projects/{projectId}/gaps", trees: ["production"] }, // FR-GWEB-04: the Gaps screen (was Decisions)
  plan: { template: "/projects/{projectId}/plan", trees: ["production"] }, // P20; page files in P21
  specbot: { template: "/projects/{projectId}/specbot", trees: ["production"] },
  settings: { template: "/projects/{projectId}/settings", trees: ["production"] }, // ?tab=general | jira | slack | github
  connections: { template: "/connections", trees: ["production"] },
  guides: { template: "/guides", trees: ["production"] }, // + "/guides/{topic}": app/(app)/guides/[topic]/page.tsx
};

export const TREE_PREFIX: Record<AppMode, ""> = { production: "" };

export interface RouteBuilder {
  home(): string;
  canvas(projectId: string, opts?: { add?: string }): string; // V2: the project's canvas; `add` = the connection form to open (back from Integrations)
  integrations(projectId: string, opts?: { setup?: IntegrationCardId; next?: string }): string; // V2: `setup` opens that app; `next` = a same-site path to return to after a connect
  missionControl(projectId: string, opts?: { view?: MissionControlView }): string;
  file(projectId: string, ref: { path: string; anchor?: string }): string; // view=files&file=...&section=...
  gaps(projectId: string, opts?: { gapId?: string }): string; // `?gap=<id>` opens that gap's card
  plan(projectId: string, opts?: { view?: PlanView }): string; // no `view` → clean path (Stage 2 opens on Plan)
  specbot(projectId: string): string;
  settings(projectId: string, opts?: { tab?: SettingsTab }): string; // no `tab` -> clean path (opens on General)
  connections(): string;
  guides(topic?: GuideTopic): string; // "/guides" or "/guides/{topic}"
  login(): string; // "/login"
  asset(path: string): string; // "/ds/" + path
}

const VIEWS: readonly MissionControlView[] = ["readme", "business", "architecture", "files", "sources"];
const PLAN_VIEWS: readonly PlanView[] = ["plan", "work", "develop"];
const SETTINGS_TABS: readonly SettingsTab[] = ["general", "jira", "slack", "github"];

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
    canvas: (projectId, opts) => withQuery(at("canvas", { projectId }), { add: opts?.add }),
    integrations: (projectId, opts) => withQuery(at("integrations", { projectId }), { setup: opts?.setup, next: opts?.next }),
    missionControl: (projectId, opts) => withQuery(at("missionControl", { projectId }), { view: opts?.view }),
    file: (projectId, ref) => withQuery(at("missionControl", { projectId }), { view: "files", file: ref.path, section: ref.anchor }),
    gaps: (projectId, opts) => withQuery(at("gaps", { projectId }), { gap: opts?.gapId }),
    plan: (projectId, opts) => withQuery(at("plan", { projectId }), { view: opts?.view }),
    specbot: (projectId) => at("specbot", { projectId }),
    settings: (projectId, opts) => withQuery(at("settings", { projectId }), { tab: opts?.tab }),
    connections: () => at("connections"),
    guides: (topic) => at("guides") + (topic === undefined ? "" : `/${encodeURIComponent(topic)}`),
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

/** FR-GWEB-04: `gap` of the Gaps route (the open card); missing -> null. */
export function parseGapsQuery(q: URLSearchParams): { gapId: string | null } {
  return { gapId: q.get("gap") };
}

/** FE-PLAN-05 (TECH-SPECS §1.1): `view` of the Plan route; missing or unknown -> null, never an error. */
export function parsePlanQuery(q: URLSearchParams): { view: PlanView | null } {
  const v = q.get("view");
  return { view: PLAN_VIEWS.includes(v as PlanView) ? (v as PlanView) : null };
}

/** `tab` of the Settings route; missing or unknown -> `general`, never an error (docs/projects/ui.md#route-and-sidebar). */
export function parseSettingsQuery(q: URLSearchParams): { tab: SettingsTab } {
  const t = q.get("tab");
  return { tab: SETTINGS_TABS.includes(t as SettingsTab) ? (t as SettingsTab) : "general" };
}

/** A `{topic}` path segment as a known Guides topic; anything else -> null (the page answers 404). */
export function parseGuideTopic(topic: string): GuideTopic | null {
  return (GUIDE_TOPICS as readonly string[]).includes(topic) ? (topic as GuideTopic) : null;
}

/**
 * The Integrations page query: the OAuth return codes (`?github=`, `?atlassian=`, `?linear=`), the app whose set-up opens at once (`?setup=`) and
 * the same-site `next` path the page returns to after a connect (the canvas form that asked). Anything else is null.
 */
export function parseIntegrationsQuery(q: URLSearchParams): {
  github: string | null;
  atlassian: string | null;
  linear: string | null;
  setup: IntegrationCardId | null;
  next: string | null;
} {
  const next = q.get("next");
  const setup = [...INTEGRATION_APPS, "mcp" as const].find((a) => a === q.get("setup")) ?? null;
  return {
    github: q.get("github"),
    atlassian: q.get("atlassian"),
    linear: q.get("linear"),
    setup,
    next: isSameSitePath(next) ? next : null,
  };
}

/** `add` of the canvas route: the connection whose form opens after the Integrations round trip; the canvas checks it against its registry. */
export function parseCanvasQuery(q: URLSearchParams): { add: string | null } {
  return { add: q.get("add") };
}
