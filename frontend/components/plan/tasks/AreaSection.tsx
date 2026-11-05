"use client";
// FE-PLAN-06.AC2, AC3, AC5, AC6 (G-PLAN-2): one work area of the Plan tab. The header is a `<button aria-expanded>` inside an h2 (Enter and Space
// toggle it natively) with the area's progress; the body (kept mounted with `hidden`, so no height animates) holds the area description, the
// members held as no code work, the review findings about the area, then its stories.
import type { ReactNode } from "react";
import { Icon } from "@/components/ui/Icon";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { Tag } from "@/components/ui/Tag";
import type { AreaGroup } from "@/lib/domain/logic/plan";
import { NOT_BUILDABLE_REASONS, areaTitle, doneProgress } from "./task-logic";

export function AreaSection({
  group,
  open,
  onToggle,
  children,
}: Readonly<{ group: AreaGroup; open: boolean; onToggle: () => void; children: ReactNode }>) {
  const { area, tasks, review } = group;
  const { done, total, percent } = doneProgress(tasks);
  const bodyId = `plan-area-${area.areaId.replace(/[^\w-]/g, "-")}`;
  const blocking = review.filter((r) => r.severity === "blocking").length;
  return (
    <section className="plan-area" data-testid="plan-area" data-area={area.areaId}>
      <h2 className="plan-area__heading">
        <button
          type="button"
          className="plan-area__head"
          data-testid="plan-area-head"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={onToggle}
        >
          <Icon name={open ? "caret-down" : "caret-right"} size={16} />
          <span className="plan-area__name" data-testid="plan-area-name">
            {areaTitle(area)}
          </span>
          {area.kind === "project" ? <Tag>Project-wide</Tag> : null}
          {review.length > 0 ? (
            <Tag
              tone={blocking > 0 ? "danger" : "warning"}
            >{`${String(review.length)} review ${review.length === 1 ? "finding" : "findings"}`}</Tag>
          ) : null}
          <span className="plan-area__count body-sm" data-testid="plan-area-count">{`${String(done)} of ${String(total)} done`}</span>
        </button>
      </h2>
      <div className="plan-area__bar" aria-hidden>
        <ProgressBar value={percent} />
      </div>
      <div id={bodyId} className="plan-area__body" hidden={!open}>
        {area.description !== null && area.description !== "" ? <p className="plan-area__desc body-sm">{area.description}</p> : null}
        {review.length > 0 ? (
          <ul className="plan-area__review" data-testid="plan-area-review" aria-label="Review findings">
            {review.map((r, i) => (
              <li key={`${r.taskId ?? "area"}-${String(i)}`} className="plan-area__finding body-sm">
                <Tag tone={r.severity === "blocking" ? "danger" : "warning"}>{r.severity === "blocking" ? "Blocking" : "Advisory"}</Tag>
                <span>{r.problem}</span>
              </li>
            ))}
          </ul>
        ) : null}
        {area.notBuildable.length > 0 ? (
          <details className="plan-area__nb" data-testid="plan-not-buildable">
            <summary className="gdg-hit body-sm">{`Not code work (${String(area.notBuildable.length)})`}</summary>
            <ul className="plan-area__nb-list">
              {area.notBuildable.map((n) => (
                <li key={n.specId} className="body-sm">
                  <span className="plan-area__nb-name">{n.name ?? n.specId}</span>
                  {` · ${NOT_BUILDABLE_REASONS[n.reason] ?? n.reason}`}
                  {n.note !== null && n.note !== "" ? `: ${n.note}` : null}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
        <div className="plan-area__stories">{children}</div>
      </div>
    </section>
  );
}
