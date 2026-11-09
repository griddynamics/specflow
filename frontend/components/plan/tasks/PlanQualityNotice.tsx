"use client";
// FE-PLAN-06: how the run that wrote the plan went, above the outline. It shows only when there is something to say (fallback tasks, an
// unplanned project step, review findings); the text comes from `qualityLines`.
import type { PlanQuality } from "@/lib/domain/plan";
import { Icon } from "@/components/ui/Icon";
import { qualityLines } from "./task-logic";

export function PlanQualityNotice({ quality, ruleTasks }: Readonly<{ quality: PlanQuality | null; ruleTasks: number }>) {
  const lines = qualityLines(quality, ruleTasks);
  if (lines.length === 0) return null;
  const partial = quality?.status === "partially_succeeded";
  return (
    <section className="plan-quality" data-testid="plan-quality" role="status" aria-label="Plan quality">
      <h2 className="plan-quality__title body">
        <Icon name="warning-circle" size={16} />
        {partial ? "Part of this plan was not written by the planner" : "The review found issues in this plan"}
      </h2>
      {lines.map((line) => (
        <p key={line} className="plan-quality__line body-sm">
          {line}
        </p>
      ))}
    </section>
  );
}
