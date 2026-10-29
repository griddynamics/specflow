"use client";
// FE-PLAN-06.AC2, AC3, AC5, AC6 (G-PLAN-2; DECISIONS P24-2): one phase of the Plan tab. The header is a `<button aria-expanded>`
// inside an h2 (Enter and Space toggle it natively); the task list stays mounted with `hidden`, so no height animates.
import type { ReactNode } from "react";
import { Icon } from "@/components/ui/Icon";
import { ProgressBar } from "@/components/ui/ProgressBar";
import type { PlanTask, TaskPhase } from "@/lib/domain/plan";
import { PHASE_NAMES, phaseProgress } from "./task-logic";

export function PhaseSection({
  phase,
  tasks,
  open,
  onToggle,
  children,
}: Readonly<{
  phase: TaskPhase;
  tasks: PlanTask[];
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}>) {
  const { done, total, percent } = phaseProgress(tasks);
  const listId = `plan-phase-${phase}`;
  return (
    <section className="plan-phase" data-testid="plan-phase" data-phase={phase}>
      <h2 className="plan-phase__heading">
        <button
          type="button"
          className="plan-phase__head"
          data-testid="plan-phase-head"
          aria-expanded={open}
          aria-controls={listId}
          onClick={onToggle}
        >
          <Icon name={open ? "caret-down" : "caret-right"} size={16} />
          <span className="plan-phase__name" data-testid="plan-phase-name">
            {PHASE_NAMES[phase]}
          </span>
          <span className="plan-phase__count body-sm" data-testid="plan-phase-count">{`${String(done)} of ${String(total)} done`}</span>
        </button>
      </h2>
      <div className="plan-phase__bar" aria-hidden>
        <ProgressBar value={percent} />
      </div>
      <ul id={listId} className="plan-phase__tasks" hidden={!open}>
        {children}
      </ul>
    </section>
  );
}
