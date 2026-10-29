"use client";
// FE-PLAN-10, FE-DEC-02.AC5, FE-BP-03.AC8 (TECH-SPECS §2.15, §2.10; DECISIONS AUTO-23, P21-2, D19): the "not Ready" gate shared by Plan and Decisions.
//  - One `missionControl.snapshot(projectId)` read; `ready` opens the gate, any other phase blocks it.
//  - A failed read (network, 4xx/5xx, not_found, invalid_response) counts as blocked (P21-2).
//  - `missionControl.leave(projectId)` runs after the read settled (only while mounted) and on unmount: no poll survives (production, AUTO-23).
//  - The gate never starts, retries or subscribes to Mission Control (R11); it never uploads or dismisses.
//  - The caller is keyed by `projectId` (`<Screen key={projectId}>`): the hook stores no project id.
import { useEffect, useState } from "react";
import { usePorts } from "@/lib/data/provider";
import type { MissionControlSnapshot } from "@/lib/domain/pipeline";

export type ReadyGate = "checking" | "blocked" | "open";

/** `ready` → open; any other phase or a rejected read → blocked. Never rejects. */
export function readyGateOf(snapshot: Promise<MissionControlSnapshot>): Promise<Exclude<ReadyGate, "checking">> {
  return snapshot.then(
    (s) => (s.phase === "ready" ? "open" : "blocked"),
    () => "blocked",
  );
}

export function useReadyGate(projectId: string): ReadyGate {
  const { missionControl } = usePorts();
  const [gate, setGate] = useState<ReadyGate>("checking");

  // The gate: one snapshot read, then `leave`. A failed read counts as not Ready (P21-2).
  useEffect(() => {
    let alive = true;
    const settle = (next: Exclude<ReadyGate, "checking">) => {
      if (!alive) return; // unmounted: the cleanup already left
      missionControl.leave(projectId);
      setGate(next);
    };
    readyGateOf(missionControl.snapshot(projectId)).then(settle);
    return () => {
      alive = false;
      missionControl.leave(projectId); // a read still pending at unmount must leave no poll (idempotent)
    };
  }, [missionControl, projectId]);

  return gate;
}
