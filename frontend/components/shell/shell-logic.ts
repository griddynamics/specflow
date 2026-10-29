// FE-SHELL-01, FE-SHELL-02 (TECH-SPECS §1.3, §1.8, §5.2): the pure rules of the app shell.
// No React, no fetch.
import { isDataError } from "@/lib/domain/errors";
import { ROUTES, TREE_PREFIX, type RouteBuilder } from "@/lib/routes";
import type { IconName } from "@/components/ui/Icon";
import { truthy } from "@/components/ui/utils";

/**
 * The five sections, in order (FE-SHELL-01.AC2); the sidebar shows `SIDEBAR_SECTIONS`. Icons are Phosphor Regular names from the icon map.
 * The same tree is declared in `validation/nav-spec.json`, which the nav-tree check compares this array to.
 */
export const SECTIONS: readonly { readonly value: SectionKey; readonly label: string; readonly icon: IconName }[] = [
  { value: "missionControl", label: "Mission Control", icon: "squares-four" },
  { value: "decisions", label: "Decisions", icon: "list-checks" },
  { value: "plan", label: "Plan", icon: "kanban" },
  { value: "specbot", label: "SpecBot", icon: "sparkle" },
  { value: "connections", label: "Connections", icon: "plug" },
];

export type SectionKey = "missionControl" | "decisions" | "plan" | "specbot" | "connections";

/**
 * The sidebar's items (owner decision 2026-10-15): Connections is hidden from the sidebar; its page, route and components stay for reuse
 * on Mission Control (sources). `SECTIONS` keeps it, so the route still matches a section.
 */
export const SIDEBAR_SECTIONS = SECTIONS.filter((s) => s.value !== "connections");

const escape = (t: string) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** One matcher per section and tree, built from the ROUTES templates: `{param}` = one path segment, optional trailing slash. */
const SECTION_MATCHERS: [SectionKey, RegExp][] = SECTIONS.flatMap((s) =>
  (Object.values(TREE_PREFIX) as string[]).map((prefix): [SectionKey, RegExp] => [
    s.value,
    new RegExp(
      `^${escape(prefix)}${ROUTES[s.value].template
        .split(/(\{\w+\})/)
        .map((part) => (/^\{\w+\}$/.test(part) ? "[^/]+" : escape(part)))
        .join("")}/?$`,
    ),
  ]),
);

/** Which section a pathname belongs to from the ROUTES templates. null = none (e.g. `/`, `/projects/x`). */
export function sectionOf(pathname: string): SectionKey | null {
  return SECTION_MATCHERS.find(([, re]) => re.test(pathname))?.[0] ?? null;
}

/** Sidebar target. Project sections need a project; without one they lead to the entry route (TECH-SPECS §1.8). */
export function sectionHref(routes: RouteBuilder, key: SectionKey, projectId: string | null): string {
  if (key === "connections") return routes.connections();
  if (!truthy(projectId)) return routes.home();
  if (key === "missionControl") return routes.missionControl(projectId);
  if (key === "decisions") return routes.decisions(projectId);
  return key === "plan" ? routes.plan(projectId) : routes.specbot(projectId);
}

/** §1.8: project used when the route has none and the store has none: the newest (list[0]). */
export function fallbackProjectId(newestFirst: string[]): string | null {
  return newestFirst[0] ?? null;
}

/** §1.8, PA27: an unknown or inaccessible project id (`not_found`, `validation`) redirects; any other failure does not. */
export function gateOutcome(err: unknown): "redirect" | "error" {
  return isDataError(err) && (err.code === "not_found" || err.code === "validation") ? "redirect" : "error";
}
