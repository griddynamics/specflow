"use client";

// The Settings screens read their data ports and routes through these hooks, so the view components stay free of data-access calls.
import { usePorts } from "@/lib/data/provider";

/** The members port (list, add, remove, cancel invite, leave). */
export function useMembersPort() {
  return usePorts().members;
}

/** The projects port (`get`, `rename`). */
export function useProjectsPort() {
  return usePorts().projects;
}
