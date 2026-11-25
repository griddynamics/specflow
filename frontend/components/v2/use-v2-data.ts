"use client";
// The V2 components read their ports and routes through these hooks, so the view components stay free of data-access calls (as the
// Settings screens do with `use-settings-ports.ts`).
import { usePorts, useRoutes } from "@/lib/data/provider";

export function useV2Ports() {
  return usePorts();
}

export function useV2Routes() {
  return useRoutes();
}
