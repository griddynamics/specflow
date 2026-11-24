"use client";

// The Settings screens read their data ports and routes through these hooks, so the view components stay free of data-access calls.
import { usePorts, useRoutes } from "@/lib/data/provider";

/** The members port (list, add, remove, cancel invite, leave). */
export function useMembersPort() {
  return usePorts().members;
}

/** The Jira port (`get`, `save`, `checkAccess`). */
export function useJiraPort() {
  return usePorts().jira;
}

/** The Slack port (`get`, `unbind`). */
export function useSlackPort() {
  return usePorts().slack;
}

/** The projects port (`get`, `rename`). */
export function useProjectsPort() {
  return usePorts().projects;
}

/** The app routes (home, settings). */
export function useSettingsRoutes() {
  return useRoutes();
}
