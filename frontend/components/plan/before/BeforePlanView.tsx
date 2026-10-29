"use client";
// FE-PLAN-02, FE-PLAN-03 (TECH-SPECS §2.15): Stage 0 slot. P22 builds it: reads `plan.table(projectId)`, calls `plan.create`. P21 fixes the
// props only; a later phase must not change them (P21-5). Q7: nothing here imports from `components/decisions/`. Q11: no readiness text.
import { useEffect, useMemo, useState } from "react";
import { ShellHeader } from "@/components/shell/ShellHeader";
import { Skeleton } from "@/components/ui/Skeleton";
import { Button } from "@/components/ui/Button";
import { useToast } from "@/components/ui/Toast";
import { usePlanPort } from "../use-plan-ports";
import type { PlanTable } from "@/lib/domain/plan";
import { PlanFeatureTable } from "./PlanFeatureTable";
import { createFailToast, createPress, tableView } from "./create-press";
import { CREATE_PLAN_LABEL } from "./plan-text";
import "./before.css";
import { tabStop } from "@/components/ui/utils";

/** The table slot: a skeleton while loading, the note with "Try again" when the read failed, else the table (P22-4). */
function TableSlot({
  view,
  table,
  onRetry,
}: Readonly<{ view: ReturnType<typeof tableView>; table: PlanTable | "failed" | null; onRetry: () => void }>) {
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
  return (
    <div className="plan-table-wrap" role="region" aria-label="Open decisions by Feature" {...tabStop(true)}>
      <PlanFeatureTable table={table} />
    </div>
  );
}

export function BeforePlanView({ projectId }: Readonly<{ projectId: string }>) {
  const plan = usePlanPort();
  const [table, setTable] = useState<PlanTable | "failed" | null>(null);
  const [tries, setTries] = useState(0);
  const toast = useToast();

  useEffect(() => {
    let alive = true;
    plan.table(projectId).then(
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
    <div className="plan-before plan-scroll" data-testid="plan-before" data-project={projectId}>
      {/* FE-PLAN-03.AC1, AC5-AC7 (Q16, P27-1, P27-15): "Create PLAN" lives in the shell's header row, so it leaves with this view. Stage 0 has no
          tabs, so the button is the first line (`tabs`): `tools` would sit behind an empty first line, one flex gap to the right of the left edge. */}
      <ShellHeader
        tabs={
          <Button
            onClick={() => {
              void create();
            }}
          >
            {CREATE_PLAN_LABEL}
          </Button>
        }
      />
      <TableSlot
        view={view}
        table={table}
        onRetry={() => {
          setTable(null);
          setTries((n) => n + 1);
        }}
      />
    </div>
  );
}
