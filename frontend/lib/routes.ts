// FE-BP-01, FE-BP-03 (TECH-SPECS §1.1): the ONE place that knows a path. Screens get a RouteBuilder from
// useRoutes() and never build a path string by hand. Pure: no React, no fetch.
import { INTEGRATION_APPS, type IntegrationCardId } from "@/lib/domain/integrations";
import { isSameSitePath } from "@/lib/same-site-path";

export type AppMode = "production";
export type RouteKey = "home" | "canvas" | "integrations";

/** Path templates WITHOUT the tree prefix; `{name}` marks a param. `trees` = where a page file must exist. */
export const ROUTES: Record<RouteKey, { template: string; trees: readonly AppMode[] }> = {
  home: { template: "", trees: ["production"] },
  canvas: { template: "/projects/{projectId}", trees: ["production"] }, // Mission Control V2: the canvas of a project (app/(v2))
  integrations: { template: "/projects/{projectId}/integrations", trees: ["production"] }, // V2: ?github= | ?atlassian= | ?linear= | ?box= | ?dropbox= | ?next=
};

export const TREE_PREFIX: Record<AppMode, ""> = { production: "" };

export interface RouteBuilder {
  home(): string;
  canvas(projectId: string, opts?: { add?: string }): string; // V2: the project's canvas; `add` = the connection form to open (back from Integrations)
  integrations(projectId: string, opts?: { setup?: IntegrationCardId; next?: string }): string; // V2: `setup` opens that app; `next` = a same-site path to return to after a connect
  login(): string; // "/login"
}

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
    login: () => "/login",
  };
}

/**
 * The Integrations page query: the OAuth return codes (`?github=`, `?atlassian=`, `?linear=`, `?box=`, `?dropbox=`, `?notion=`), the app whose set-up opens at once (`?setup=`) and
 * the same-site `next` path the page returns to after a connect (the canvas form that asked). Anything else is null.
 */
export function parseIntegrationsQuery(q: URLSearchParams): {
  github: string | null;
  atlassian: string | null;
  linear: string | null;
  box: string | null;
  dropbox: string | null;
  notion: string | null;
  setup: IntegrationCardId | null;
  next: string | null;
} {
  const next = q.get("next");
  const setup = [...INTEGRATION_APPS, "mcp" as const].find((a) => a === q.get("setup")) ?? null;
  return {
    github: q.get("github"),
    atlassian: q.get("atlassian"),
    linear: q.get("linear"),
    box: q.get("box"),
    dropbox: q.get("dropbox"),
    notion: q.get("notion"),
    setup,
    next: isSameSitePath(next) ? next : null,
  };
}

/** `add` of the canvas route: the connection whose form opens after the Integrations round trip; the canvas checks it against its registry. */
export function parseCanvasQuery(q: URLSearchParams): { add: string | null } {
  return { add: q.get("add") };
}
