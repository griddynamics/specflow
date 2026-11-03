"use client";
// FE-PLAN-05, FE-PLAN-13 (TECH-SPECS §1.1, §2.15; DECISIONS P23-*, P27-1, P27-8, P27-9, P27-13): Stage 2. The Plan | Develop switch sits in the shell's
// header row (ShellHeader, G-HDR-1) and is shared with Stage 0 (PlanViewSwitch, U5); Stage 1 never mounts it. "Recreate Plan" (a glass Button, Q22) is the `tools` of the same
// ShellHeader, so it sits right of the switch on the same line, and only in Stage 2. It calls `plan.create` from `ready` with no confirm dialog; the stage
// event then moves the screen to Stage 1, and CreatingView cleans `?view=` and forgets the open phases, so the new plan opens on Plan, first phase open. The tab comes from the URL (`?view=plan|develop`; missing or unknown shows
// Plan); picking a segment pushes the URL with the native history API, which Next.js syncs with `useSearchParams` (as Mission Control).
// The Stage 1 -> 2 move cleans the URL (CreatingView), so a plan that has just been created opens on Plan. The slot props are fixed (P21-5).
import { useMemo } from "react";
import { useToast } from "@/components/ui/Toast";
import { ShellHeader } from "@/components/shell/ShellHeader";
import { Button } from "@/components/ui/Button";
import { usePlanPort } from "../use-plan-ports";
import { PlanViewSwitch, usePlanView } from "../PlanViewSwitch";
import type { PlanSnapshot } from "@/lib/domain/plan";
import { createFailToast, createPress } from "../before/create-press";
import { RECREATE_PLAN_LABEL } from "../before/plan-text";
import { DevelopTab, type DevelopInfo } from "../develop/DevelopTab";
import { PlanTab } from "../tasks/PlanTab";

export function PlanReadyView({ projectId, plan, develop }: Readonly<{ projectId: string; plan: PlanSnapshot; develop: DevelopInfo }>) {
  const planPort = usePlanPort();
  const toast = useToast();
  const view = usePlanView();
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
        tabs={<PlanViewSwitch projectId={projectId} view={view} />}
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
      {view === "develop" ? <DevelopTab projectId={projectId} info={develop} /> : <PlanTab projectId={projectId} plan={plan} />}
    </div>
  );
}
