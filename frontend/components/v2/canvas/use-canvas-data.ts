"use client";
// What the canvas reads from the ports: the project's sources and build (the V1 hook, polled), the gap run and its counts, the plan, and the
// integrations. Each read that fails leaves its part `null`: the canvas shows what it knows and nothing invented. Ports only, through
// `use-v2-data.ts`.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSourcesStatus } from "@/components/mission-control/sources/use-sources-data";
import { startPolling } from "@/components/settings/github-logic";
import type { Integration } from "@/lib/domain/integrations";
import type { PlanState } from "@/lib/domain/plan";
import { useV2Ports } from "../use-v2-data";
import { summarizeGaps, SUMMARY_QUERY } from "./canvas-gaps";
import type { CanvasInput, GapsSummary } from "./canvas-types";

const GAPS_POLL_MS = 5000;
const CLOCK_MS = 30_000;

function useGapsSummary(projectId: string, active: boolean): GapsSummary | null {
  const { gaps } = useV2Ports();
  const [summary, setSummary] = useState<GapsSummary | null>(null);
  const load = useCallback(() => {
    Promise.all([gaps.check(projectId), gaps.list(projectId, SUMMARY_QUERY)]).then(
      ([check, page]) => {
        setSummary(summarizeGaps(check, page));
      },
      () => undefined,
    );
  }, [gaps, projectId]);
  useEffect(() => {
    if (!active) return undefined;
    load();
    return startPolling(true, load, GAPS_POLL_MS);
  }, [active, load]);
  return active ? summary : null;
}

function usePlanState(projectId: string): PlanState | null {
  const { plan } = useV2Ports();
  const [state, setState] = useState<PlanState | null>(null);
  useEffect(() => {
    let alive = true;
    plan.state(projectId).then(
      (s) => {
        if (alive) setState(s);
      },
      () => undefined,
    );
    const off = plan.subscribe(projectId, (_event, next) => {
      if (alive) setState(next);
    });
    return () => {
      alive = false;
      off();
      plan.leave(projectId);
    };
  }, [plan, projectId]);
  return state;
}

function useIntegrationList(projectId: string): readonly Integration[] | null {
  const { integrations } = useV2Ports();
  const [rows, setRows] = useState<readonly Integration[] | null>(null);
  useEffect(() => {
    let alive = true;
    integrations.list(projectId).then(
      (list) => {
        if (alive) setRows(list);
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [integrations, projectId]);
  return rows;
}

/** The plan is due for a replan when a graph build finished after this screen saw the plan (the API reports no plan time; D9). */
export function isPlanStale(seenAt: string | null | undefined, finishedAt: string | null, planReady: boolean): boolean {
  return planReady && seenAt !== undefined && finishedAt !== seenAt;
}

function usePlanStale(finishedAt: string | null, known: boolean, planReady: boolean): boolean {
  const [seen, setSeen] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    if (!planReady) setSeen(undefined);
    else if (known) setSeen((was) => (was === undefined ? finishedAt : was));
  }, [planReady, known, finishedAt]);
  return isPlanStale(seen, finishedAt, planReady);
}

/** The clock the elapsed time of a build reads: it ticks while a build runs. */
function useNow(running: boolean): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (!running) return undefined;
    const tick = () => {
      setNow(new Date());
    };
    tick();
    return startPolling(true, tick, CLOCK_MS);
  }, [running]);
  return now;
}

export function useCanvasData(projectId: string) {
  const { knowledge } = useV2Ports();
  const { status, reload } = useSourcesStatus(knowledge, projectId);
  const gaps = useGapsSummary(projectId, status?.ready === true);
  const plan = usePlanState(projectId);
  const integrations = useIntegrationList(projectId);
  const now = useNow(status?.build != null);
  const planStale = usePlanStale(status?.lastBuild?.finishedAt ?? null, status !== null, plan?.phase === "ready");
  const input = useMemo<CanvasInput>(
    () => ({ now, status, gaps, plan, planStale, integrations }),
    [now, status, gaps, plan, planStale, integrations],
  );
  return { input, reload };
}
