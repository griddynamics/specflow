"use client";

// The plan screens read their data port and routes through these hooks, so the view components stay free of data-access calls.
import { usePorts, useRoutes } from "@/lib/data/provider";

/** The plan port (`plan.develop`, `plan.tasks`, ...). */
export function usePlanPort() {
  return usePorts().plan;
}

/** The app routes (links between the plan tabs and the other screens). */
export function usePlanRoutes() {
  return useRoutes();
}
