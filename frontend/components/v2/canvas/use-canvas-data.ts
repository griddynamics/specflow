"use client";
// What the canvas reads from the ports: the project's sources and build (the V1 hook, polled), the gap run and its counts, the plan, and the
// integrations. Each read that fails leaves its part `null`: the canvas shows what it knows and nothing invented. Ports only, through
// `use-v2-data.ts`.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSourcesStatus } from "@/components/mission-control/sources/use-sources-data";
import { isCloning, startPolling } from "@/components/settings/github-logic";
import { isRunning } from "@/components/settings/jira-logic";
import type { PlanState } from "@/lib/domain/plan";
import { useV2Ports } from "../use-v2-data";
import { openGapRows, summarizeGaps, SUMMARY_QUERY } from "./canvas-gaps";
import type { CanvasInput, GapRow, GapsSummary } from "./canvas-types";
import { usePolledRead } from "./use-polled-read";

const GAPS_POLL_MS = 5000;
const CLOCK_MS = 30_000;

interface GapsRead {
  summary: GapsSummary;
  rows: readonly GapRow[];
}

function useGapsRead(projectId: string, active: boolean): GapsRead | null {
  const { gaps } = useV2Ports();
  const [read, setRead] = useState<GapsRead | null>(null);
  const load = useCallback(() => {
    Promise.all([gaps.check(projectId), gaps.list(projectId, SUMMARY_QUERY)]).then(
      ([check, page]) => {
        setRead({ summary: summarizeGaps(check, page), rows: openGapRows(page) });
      },
      () => undefined,
    );
  }, [gaps, projectId]);
  useEffect(() => {
    if (!active) return undefined;
    load();
    return startPolling(true, load, GAPS_POLL_MS);
  }, [active, load]);
  return active ? read : null;
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

function useIntegrationList(projectId: string) {
  const { integrations } = useV2Ports();
  return usePolledRead(
    useCallback(() => integrations.list(projectId), [integrations, projectId]),
    () => null,
  );
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

const PENDING_MS = 15_000;

/**
 * A start (a build, a plan) that the read side has not shown yet. `mark` remembers the state signal at the press; it is cleared when the work is
 * seen running, when the signal moves (it ended before it was seen), or after a while (the start failed: the status is the truth again).
 */
function usePending(running: boolean, signal: string): [boolean, () => void] {
  const [mark, setMark] = useState<string | null>(null);
  useEffect(() => {
    if (mark !== null && (running || signal !== mark)) setMark(null);
  }, [mark, running, signal]);
  useEffect(() => {
    if (mark === null) return undefined;
    const timer = setTimeout(() => {
      setMark(null);
    }, PENDING_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [mark]);
  return [
    mark !== null,
    () => {
      setMark(signal);
    },
  ];
}

const GITHUB_POLL_MS = 3000;
const JIRA_POLL_MS = 5000;
const SLACK_POLL_MS = 5000;

/** The project's repositories, Jira target and Slack channel, each read again while it changes (a clone runs, a sync runs, a channel is awaited). */
function useConnectionReads(projectId: string) {
  const { github, jira, slack } = useV2Ports();
  const repos = usePolledRead(
    useCallback(() => github.list(projectId), [github, projectId]),
    (v) => (isCloning(v) ? GITHUB_POLL_MS : null),
  );
  const target = usePolledRead(
    useCallback(() => jira.get(projectId), [jira, projectId]),
    (v) => (isRunning(v) ? JIRA_POLL_MS : null),
  );
  const channel = usePolledRead(
    useCallback(() => slack.get(projectId), [slack, projectId]),
    (v) => (v?.channel === null || v === null ? SLACK_POLL_MS : null),
  );
  return { repos, target, channel };
}

export function useCanvasData(projectId: string) {
  const { knowledge } = useV2Ports();
  const { status, reload: reloadStatus } = useSourcesStatus(knowledge, projectId);
  const gapsRead = useGapsRead(projectId, status?.ready === true);
  const plan = usePlanState(projectId);
  const integrations = useIntegrationList(projectId);
  const { repos, target, channel } = useConnectionReads(projectId);
  const now = useNow(status?.build != null);
  const planStale = usePlanStale(status?.lastBuild?.finishedAt ?? null, status !== null, plan?.phase === "ready");
  const [pendingBuild, markBuild] = usePending(
    status?.build != null,
    `${status?.lastBuild?.finishedAt ?? ""}|${status?.buildFailure?.code ?? ""}`,
  );
  const [pendingPlan, markPlan] = usePending(plan?.phase === "creating", plan?.phase === "ready" ? plan.plan.planId : (plan?.phase ?? ""));
  const input = useMemo<CanvasInput>(
    () => ({
      now,
      pending: { build: pendingBuild, plan: pendingPlan },
      status,
      gaps: gapsRead?.summary ?? null,
      gapRows: gapsRead?.rows ?? [],
      plan,
      planStale,
      integrations: integrations.value,
      github: repos.value,
      jira: target.value,
      slack: channel.value,
    }),
    [now, pendingBuild, pendingPlan, status, gapsRead, plan, planStale, integrations.value, repos.value, target.value, channel.value],
  );
  // One handle for every form: it reads the sources, repositories, target and channel again.
  const reload = useRef<() => void>(() => undefined);
  useEffect(() => {
    reload.current = () => {
      reloadStatus.current();
      integrations.reload();
      repos.reload();
      target.reload();
      channel.reload();
    };
  });
  return { input, reload, markBuild, markPlan };
}
