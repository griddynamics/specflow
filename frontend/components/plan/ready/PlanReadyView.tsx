"use client";
// FE-PLAN-05, FE-PLAN-13 (TECH-SPECS §1.1, §2.15; DECISIONS P23-*, P27-1, P27-8, P27-9, P27-13): Stage 2. The Plan | Develop switch sits in the shell's
// header row (ShellHeader, G-HDR-1) and exists here only: Stages 0 and 1 never mount it. "Recreate Plan" (a glass Button, Q22) is the `tools` of the same
// ShellHeader, so it sits right of the switch on the same line, and only in Stage 2. It calls `plan.create` from `ready` with no confirm dialog; the stage
// event then moves the screen to Stage 1, and CreatingView cleans `?view=` and forgets the open phases, so the new plan opens on Plan, first phase open. The tab comes from the URL (`?view=plan|develop`; missing or unknown shows
// Plan); picking a segment pushes the URL with the native history API, which Next.js syncs with `useSearchParams` (as Mission Control).
// The Stage 1 -> 2 move cleans the URL (CreatingView), so a plan that has just been created opens on Plan. The slot props are fixed (P21-5).
import { useSearchParams } from "next/navigation";
import { useMemo } from "react";
import { useToast } from "@/components/ui/Toast";
import { ShellHeader } from "@/components/shell/ShellHeader";
import { Button } from "@/components/ui/Button";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { usePlanPort, usePlanRoutes } from "../use-plan-ports";
import type { PlanSnapshot } from "@/lib/domain/plan";
import { parsePlanQuery, type PlanView } from "@/lib/routes";
import { createFailToast, createPress } from "../before/create-press";
import { RECREATE_PLAN_LABEL } from "../before/plan-text";
import { DevelopTab } from "../develop/DevelopTab";
import { PlanTab } from "../tasks/PlanTab";

const VIEW_OPTIONS: { value: PlanView; label: string }[] = [
  { value: "plan", label: "Plan" },
  { value: "develop", label: "Develop" },
];

export function PlanReadyView({ projectId, plan }: Readonly<{ projectId: string; plan: PlanSnapshot }>) {
  const routes = usePlanRoutes();
  const planPort = usePlanPort();
  const toast = useToast();
  const query = useSearchParams();
  const view: PlanView = useMemo(() => parsePlanQuery(new URLSearchParams(query.toString())).view ?? "plan", [query]);
  // FE-PLAN-13.AC10 (P27-13): one run per press burst, as "Create PLAN"; any failure but `conflict` shows the danger toast.
  const recreate = useMemo(
    () =>
      createPress(
        () => planPort.create(projectId),
        (e) => {
          toast(createFailToast(e));
        },
      ),
    [planPort, projectId, toast],
  );
  return (
    <div className="plan-scroll" data-testid="plan-ready" data-project={projectId} data-view={view}>
      <ShellHeader
        tabs={
          <SegmentedControl
            label="Plan view"
            options={VIEW_OPTIONS}
            value={view}
            onChange={(v) => {
              if (v !== view) window.history.pushState(null, "", routes.plan(projectId, { view: v as PlanView }));
            }}
          />
        }
        tools={
          <Button
            variant="glass"
            onClick={() => {
              void recreate();
            }}
          >
            {RECREATE_PLAN_LABEL}
          </Button>
        }
      />
      {view === "develop" ? <DevelopTab projectId={projectId} /> : <PlanTab projectId={projectId} plan={plan} />}
    </div>
  );
}
