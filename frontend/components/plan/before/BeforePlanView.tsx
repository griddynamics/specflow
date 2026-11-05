"use client";
// FE-PLAN-02, FE-PLAN-03 (TECH-SPECS §2.15): Stage 0 slot. Reads `plan.preview(projectId)` (the features to plan and the run estimate),
// calls `plan.create`. A run that failed before a plan existed shows its error code in a note above the table.
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ShellHeader } from "@/components/shell/ShellHeader";
import { Skeleton } from "@/components/ui/Skeleton";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { useToast } from "@/components/ui/Toast";
import { usePlanPort, usePlanRoutes } from "../use-plan-ports";
import { PlanViewSwitch, usePlanView } from "../PlanViewSwitch";
import { DevelopTab, type DevelopInfo } from "../develop/DevelopTab";
import type { PlanPreview } from "@/lib/domain/plan";
import { PlanFeatureTable } from "./PlanFeatureTable";
import { createFailToast, createPress, tableView } from "./create-press";
import { CREATE_PLAN_LABEL, NO_FEATURES_BODY, NO_FEATURES_TITLE, failureNote, previewSummary } from "./plan-text";
import "./before.css";
import { tabStop } from "@/components/ui/utils";

/** The table slot: a skeleton while loading, the note with "Try again" when the read failed, else the table (P22-4). */
function TableSlot({
  view,
  table,
  onRetry,
  onOpenMissionControl,
}: Readonly<{
  view: ReturnType<typeof tableView>;
  table: PlanPreview | "failed" | null;
  onRetry: () => void;
  onOpenMissionControl: () => void;
}>) {
  if (view === "loading") return <Skeleton variant="block" height={280} />;
  if (view === "failed" || table === "failed" || table === null) {
    return (
      <p className="plan-before__note body-sm" role="status">
        The table could not be loaded.{" "}
        <Button variant="plain" size="sm" onClick={onRetry}>
          Try again
        </Button>
      </p>
    );
  }
  if (table.features.length === 0) {
    return (
      <EmptyState title={NO_FEATURES_TITLE} icon="kanban" actions={<Button onClick={onOpenMissionControl}>Open Mission Control</Button>}>
        {NO_FEATURES_BODY}
      </EmptyState>
    );
  }
  return (
    <>
      <p className="plan-before__summary body-sm" data-testid="plan-summary">
        {previewSummary(table.features.length, table.calls, table.inputTokens)}
      </p>
      <div className="plan-table-wrap" role="region" aria-label="Features to plan" {...tabStop(true)}>
        <PlanFeatureTable preview={table} />
      </div>
    </>
  );
}

export function BeforePlanView({ projectId, develop, failure }: Readonly<{ projectId: string; develop: DevelopInfo; failure?: string }>) {
  const planView = usePlanView();
  const plan = usePlanPort();
  const routes = usePlanRoutes();
  const router = useRouter();
  const [table, setTable] = useState<PlanPreview | "failed" | null>(null);
  const [tries, setTries] = useState(0);
  const toast = useToast();

  useEffect(() => {
    let alive = true;
    plan.preview(projectId).then(
      (t) => {
        if (alive) setTable(t);
      },
      () => {
        if (alive) setTable("failed");
      },
    );
    return () => {
      alive = false;
    };
  }, [plan, projectId, tries]);

  // FE-PLAN-03.AC3/AC4 (P22-3): one run per press burst; the logic is in `createPress` (unit-tested).
  const create = useMemo(
    () =>
      createPress(
        () => plan.create(projectId),
        (e) => {
          toast(createFailToast(e));
        },
      ),
    [plan, projectId, toast],
  );
  const view = tableView(table);

  return (
    <div className="plan-before plan-scroll" data-testid="plan-before" data-project={projectId} data-view={planView}>
      {/* FE-PLAN-03.AC1, AC5-AC7 (Q16, P27-1, P27-15; MCP-M10 U5): the shell's header row holds the shared Plan | Develop switch (as Stage 2) and "Create PLAN"
          as its `tools`, so the button leaves with this view. */}
      <ShellHeader
        tabs={<PlanViewSwitch projectId={projectId} view={planView} />}
        tools={
          <Button
            onClick={() => {
              void create();
            }}
          >
            {CREATE_PLAN_LABEL}
          </Button>
        }
      />
      {failure !== undefined && planView !== "develop" ? (
        <p className="plan-before__note body-sm" role="status" data-testid="plan-failure">
          {failureNote(failure)}
        </p>
      ) : null}
      {planView === "develop" ? (
        <DevelopTab projectId={projectId} info={develop} />
      ) : (
        <div className="plan-before__preview" data-testid="plan-preview" data-loaded={view === "loading" ? undefined : ""}>
          <TableSlot
            view={view}
            table={table}
            onRetry={() => {
              setTable(null);
              setTries((n) => n + 1);
            }}
            onOpenMissionControl={() => {
              router.push(routes.missionControl(projectId));
            }}
          />
        </div>
      )}
    </div>
  );
}
