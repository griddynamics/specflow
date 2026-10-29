"use client";
// FE-PLAN-01, FE-PLAN-10, FE-PLAN-09 (TECH-SPECS §2.15; DECISIONS AUTO-21, AUTO-23, P21-1..6): the Plan screen. The path, the export name,
// the `projectId` prop and the root marker `screen-plan` are bound by the page files (both trees).
//  - Gate (FE-PLAN-10): `useReadyGate` (components/shell/use-ready-gate.ts); not Ready → `NotReadyState` "No plan yet" with "Open Mission Control":
//    no table, no "Create PLAN".
//  - Stage = `plan.state` fed by `plan.subscribe` (store state, not URL, FE-DEMO-03): `data-stage` is `empty` (gate) | `idle` | `creating`
//    | `ready`, one slot each. Slots read the ports themselves (`usePorts()`, `useRoutes()`); their props are fixed here (P21-5).
//  - Leaving the screen (unmount) calls `plan.leave`: a run in progress goes back to idle, a ready plan stays (FE-PLAN-09).
//  - The h1 is visually hidden: the sidebar names the page (G-HDR-3). Stage 0 puts "Create PLAN" in the shell header (P27-1), Stage 2 the Plan | Develop switch and "Recreate Plan"; Stage 1 nothing (GAPS "Header row").
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { PageTitle } from "@/components/ui/TopBar";
import { NotReadyState } from "@/components/shell/NotReadyState";
import { useReadyGate, type ReadyGate } from "@/components/shell/use-ready-gate";
import { usePlanPort, usePlanRoutes } from "./use-plan-ports";
import type { PlanState } from "@/lib/domain/plan";
import { BeforePlanView } from "./before/BeforePlanView";
import { CreatingView } from "./progress/CreatingView";
import { PlanReadyView } from "./ready/PlanReadyView";
import "@/components/shell/ready-gate.css";
import "./plan-gaps.css";

/** `key={projectId}`: another project starts with a fresh gate and stage. */
export function PlanScreen({ projectId }: Readonly<{ projectId: string }>) {
  return <Plan key={projectId} projectId={projectId} />;
}

/** What the plan screen shows for the gate and the stage: the "no plan yet" note, the failure with "Try again", the skeleton, or a stage view. */
function PlanStage({
  gate,
  state,
  projectId,
  onOpenMissionControl,
  onRetry,
}: Readonly<{
  gate: ReadyGate;
  state: PlanState | "failed" | null;
  projectId: string;
  onOpenMissionControl: () => void;
  onRetry: () => void;
}>) {
  if (gate === "blocked") {
    return (
      <NotReadyState testId="plan-gate" icon="kanban" title="No plan yet" onOpenMissionControl={onOpenMissionControl}>
        A plan appears after Specflow has processed this project&apos;s files.
      </NotReadyState>
    );
  }
  if (state === "failed") {
    return (
      <EmptyState title="Plan could not be loaded" icon="warning-circle" actions={<Button onClick={onRetry}>Try again</Button>}>
        Check your connection and try again.
      </EmptyState>
    );
  }
  if (state === null) {
    return (
      <div className="plan-skel" aria-hidden>
        <Skeleton variant="block" height={44} />
        <Skeleton variant="block" height={280} />
      </div>
    );
  }
  if (state.phase === "idle") return <BeforePlanView projectId={projectId} />;
  if (state.phase === "creating") return <CreatingView projectId={projectId} run={state} />;
  return <PlanReadyView projectId={projectId} plan={state.plan} />;
}

function Plan({ projectId }: Readonly<{ projectId: string }>) {
  const plan = usePlanPort();
  const routes = usePlanRoutes();
  const router = useRouter();
  const gate = useReadyGate(projectId);
  const [state, setState] = useState<PlanState | "failed" | null>(null);
  const [tries, setTries] = useState(0);

  // The stage: events first, then the state (an event that arrives before the state wins). Cleanup = leaving the screen (FE-PLAN-09).
  useEffect(() => {
    if (gate !== "open") return;
    let alive = true;
    const off = plan.subscribe(projectId, (_e, next) => {
      if (alive) setState(next);
    });
    plan.state(projectId).then(
      (s) => {
        if (alive) setState((prev) => (prev !== null && prev !== "failed" ? prev : s));
      },
      () => {
        if (alive) setState((prev) => (prev !== null && prev !== "failed" ? prev : "failed"));
      },
    );
    return () => {
      alive = false;
      off();
      plan.leave(projectId);
    };
  }, [gate, plan, projectId, tries]);

  let stage: "empty" | PlanState["phase"] | null = null;
  if (gate === "blocked") stage = "empty";
  else if (gate === "open" && state !== null && state !== "failed") stage = state.phase;

  return (
    <section className="plan-screen" data-testid="screen-plan" data-project={projectId} data-stage={stage ?? undefined}>
      <PageTitle className="shell-sr">Plan</PageTitle>
      <PlanStage
        gate={gate}
        state={state}
        projectId={projectId}
        onOpenMissionControl={() => {
          router.push(routes.missionControl(projectId));
        }}
        onRetry={() => {
          setState(null);
          setTries((n) => n + 1);
        }}
      />
    </section>
  );
}
